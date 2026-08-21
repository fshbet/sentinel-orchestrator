"""Who is calling, and what they may do.

The previous model was one bearer token meaning "everything". That is a
reasonable default for a single-user desktop install and an unreasonable one
for anything else: a token handed to a CI job to read execution status also
lets that job cancel other people's work and read every audit trail.

So a token now resolves to a **principal** — an identity with scopes and,
optionally, a tenant. Three properties of that design matter more than the
mechanics:

* **Scopes are checked centrally, not per route.** A route added later is
  denied by default rather than accidentally public. The mapping from path to
  required scope lives in one table that can be read in full.
* **Ownership is enforced on the object, not the query.** Filtering a list by
  tenant is easy to get right and easy to forget on the next endpoint. Reading
  an execution therefore checks the execution's own tenant, so a caller who
  guesses an id still gets a 404.
* **No password storage of any kind.** Tokens are opaque bearer credentials
  compared in constant time, and the principal model is deliberately shaped so
  that an OIDC subject, a reverse-proxy header, or an mTLS identity can supply
  a ``Principal`` instead. That integration point is ``resolve``.

Backwards compatibility is preserved: ``ORCHESTRATOR_API_TOKEN`` still works
and still means full access, as a principal named ``legacy-token`` with every
scope. Nothing that worked before stops working.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import ConfigurationError

# --------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------

EXECUTIONS_READ = "executions.read"
EXECUTIONS_WRITE = "executions.write"
APPROVALS_RESPOND = "approvals.respond"
AUDIT_READ = "audit.read"
ADMIN = "admin"

ALL_SCOPES = (
    EXECUTIONS_READ,
    EXECUTIONS_WRITE,
    APPROVALS_RESPOND,
    AUDIT_READ,
    ADMIN,
)

# Read-only is the sensible starting point for a principal whose scopes were
# not stated. Granting nothing would be stricter but produces a principal that
# cannot do anything at all, which reads as a bug rather than as a policy.
DEFAULT_SCOPES: tuple[str, ...] = (EXECUTIONS_READ,)

# admin implies the rest. Stated once here rather than expanded at every
# check, so "what does admin mean" has one answer.
_IMPLIED: dict[str, tuple[str, ...]] = {
    ADMIN: (EXECUTIONS_READ, EXECUTIONS_WRITE, APPROVALS_RESPOND, AUDIT_READ),
}

# The tenant a principal belongs to when tenancy is not in use. Every object
# created in single-tenant mode carries this, so the same ownership check runs
# in both modes and there is no untested second path.
SINGLE_TENANT = "default"


def expand_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Resolve implied scopes."""
    out: set[str] = set()
    for scope in scopes:
        out.add(scope)
        out.update(_IMPLIED.get(scope, ()))
    return frozenset(out)


class Unauthorized(Exception):
    """No usable credential was presented."""


class Forbidden(Exception):
    """A valid credential without the required scope, or the wrong tenant."""

    def __init__(self, message: str, *, required: str = "", tenant: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.required = required
        self.tenant = tenant


@dataclass(frozen=True)
class Principal:
    """An authenticated caller."""

    id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    tenant: str = SINGLE_TENANT
    # Which credential was used, for the audit trail. Never the credential.
    token_id: str = ""
    # Where the identity came from: "token", "oidc", "proxy", "mtls".
    source: str = "token"
    display_name: str = ""

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def require(self, scope: str) -> None:
        if not self.has(scope):
            raise Forbidden(
                f"this credential does not have the {scope} scope",
                required=scope,
            )

    def owns(self, tenant: str | None) -> bool:
        """Whether this principal may see an object belonging to ``tenant``.

        An object with no recorded tenant predates tenancy. It is treated as
        belonging to the default tenant rather than as belonging to everyone,
        so enabling tenancy on an existing deployment hides old records from
        other tenants instead of exposing them to all of them.
        """
        return (tenant or SINGLE_TENANT) == self.tenant

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant": self.tenant,
            "scopes": sorted(self.scopes),
            "source": self.source,
            "token_id": self.token_id,
        }


# The principal used when authentication is switched off entirely — a
# loopback desktop install. Full scopes, because the alternative is a local
# console that cannot do anything, and the boundary being relied on here is
# the network one.
ANONYMOUS = Principal(
    id="local",
    scopes=expand_scopes([ADMIN]),
    tenant=SINGLE_TENANT,
    source="unauthenticated",
    display_name="unauthenticated local caller",
)


@dataclass(frozen=True)
class TokenPrincipal:
    """A configured token and the identity it grants."""

    token: str
    principal: Principal


class IdentityRegistry:
    """Resolves credentials to principals."""

    def __init__(
        self,
        entries: Sequence[TokenPrincipal] = (),
        *,
        multi_tenant: bool = False,
        store: Any = None,
        audit: Any = None,
    ) -> None:
        from .tokens import TokenStore

        self._entries = list(entries)
        self.multi_tenant = multi_tenant
        # One resolver. Entries handed in directly are issued into it, so the
        # plaintext is hashed at construction and not retained here.
        self._store = store if store is not None else TokenStore(audit=audit)
        for entry in self._entries:
            # An empty token means the credential is already in the store —
            # `from_config` builds it there and passes metadata-only entries.
            # Issuing those would call generate_token() and mint a phantom
            # credential nobody holds, inflating len() and appearing in the
            # admin listing as a second token per principal.
            if not entry.token:
                continue
            self._store.issue(
                entry.principal.id,
                scopes=entry.principal.scopes,
                tenant=entry.principal.tenant,
                description=entry.principal.display_name,
                token=entry.token,
            )
        # Drop the plaintext now that it is hashed.
        self._entries = [
            TokenPrincipal(token="", principal=e.principal) for e in self._entries
        ]

    def __len__(self) -> int:
        return len(self._store)

    @property
    def enabled(self) -> bool:
        return len(self._store) > 0

    def principals(self) -> list[Principal]:
        return [entry.principal for entry in self._entries]

    def list_tokens(self) -> list[dict[str, Any]]:
        """Token metadata, never secrets."""
        return self._store.list_tokens()

    def revoke(self, token_id: str, *, reason: str = "", actor: str = "") -> bool:
        """Revoke by token id. Effective on the next request.

        ``actor`` is accepted and recorded in the reason so the two registries
        share one signature — the caller should not have to know which it
        holds, and a try/except around a TypeError is a poor substitute for
        agreeing on the interface.
        """
        note = f"{reason} (by {actor})" if actor else reason
        return self._store.revoke(token_id, reason=note)

    def resolve(self, presented: str | None) -> Principal:
        """Find the principal for a presented credential.

        Delegates to a :class:`~orchestrator.api.tokens.TokenStore`, which is
        the single resolution path: it is the one that enforces expiry,
        not-before, and revocation. This class previously compared raw token
        strings itself, which meant a token could be expired according to one
        component and valid according to the other — and the one the
        middleware asked was the one without lifecycle checks.

        Kept as a facade rather than deleted so existing callers and tests
        keep working; it holds no plaintext credentials of its own.
        """
        return self._store.resolve(presented)

    @property
    def tokens(self):
        """The underlying store, for revocation and metadata listing."""
        return self._store

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(
        cls, api: dict[str, Any] | None = None, *, audit: Any = None
    ) -> IdentityRegistry:
        """Build from the ``api`` config section plus the environment.

        Config names an environment variable per principal; it never contains
        a token. A config file gets committed.
        """
        from .tokens import TokenStore

        api = api or {}
        multi_tenant = str(api.get("tenancy", "single")).lower() == "multi"
        entries: list[TokenPrincipal] = []
        # TokenStore.from_config reads the same principals and applies their
        # lifetimes; the loop below exists only to validate the config shape
        # and to expose principal metadata.
        store = TokenStore.from_config(api, audit=audit)

        for index, raw in enumerate(api.get("principals") or []):
            if not isinstance(raw, dict):
                raise ConfigurationError(
                    f"api.principals[{index}] must be a mapping"
                )
            principal_id = str(raw.get("id") or "").strip()
            if not principal_id:
                raise ConfigurationError(
                    f"api.principals[{index}] needs an id"
                )

            variable = str(raw.get("token_env") or "").strip()
            if not variable:
                raise ConfigurationError(
                    f"api.principals[{index}] ({principal_id}) needs token_env, "
                    f"the NAME of an environment variable holding its token. "
                    f"Tokens must not be written into a config file."
                )
            token = os.environ.get(variable, "").strip()
            if not token:
                # Skipped rather than fatal: an instance that serves three of
                # four principals is more useful than one that will not start,
                # and the missing one fails closed anyway.
                continue

            scopes = raw.get("scopes")
            resolved_scopes: tuple[str, ...] = (
                DEFAULT_SCOPES if scopes is None
                else tuple(str(s).strip() for s in scopes)
            )
            unknown = sorted(set(resolved_scopes) - set(ALL_SCOPES))
            if unknown:
                raise ConfigurationError(
                    f"api.principals[{index}] ({principal_id}) has unknown "
                    f"scope(s): {', '.join(unknown)}. Valid scopes are: "
                    f"{', '.join(ALL_SCOPES)}"
                )

            tenant = str(raw.get("tenant") or SINGLE_TENANT)
            if not multi_tenant and tenant != SINGLE_TENANT:
                raise ConfigurationError(
                    f"api.principals[{index}] ({principal_id}) sets tenant "
                    f"{tenant!r}, but api.tenancy is 'single'. Set "
                    f"api.tenancy: multi, or remove the tenant."
                )

            entries.append(
                TokenPrincipal(
                    token=token,
                    principal=Principal(
                        id=principal_id,
                        scopes=expand_scopes(resolved_scopes),
                        tenant=tenant,
                        token_id=variable,
                        source="token",
                        display_name=str(raw.get("name") or principal_id),
                    ),
                )
            )

        # Backwards compatibility. The old single token still works and still
        # means full access.
        legacy = os.environ.get("ORCHESTRATOR_API_TOKEN", "")
        for position, token in enumerate(t.strip() for t in legacy.split(",")):
            if not token:
                continue
            entries.append(
                TokenPrincipal(
                    token=token,
                    principal=Principal(
                        id="legacy-token" if position == 0 else f"legacy-token-{position}",
                        scopes=expand_scopes([ADMIN]),
                        tenant=SINGLE_TENANT,
                        token_id="ORCHESTRATOR_API_TOKEN",  # noqa: S106 - a variable name
                        source="token",
                        display_name="legacy full-access token",
                    ),
                )
            )

        # Entries carry no plaintext: the store already holds the digests.
        return cls(
            [TokenPrincipal(token="", principal=e.principal) for e in entries],
            multi_tenant=multi_tenant,
            store=store,
        )


# --------------------------------------------------------------------------
# What each route requires
# --------------------------------------------------------------------------
#
# One table rather than a decorator per route. A route absent from this table
# gets ADMIN, so forgetting to add an entry fails closed and is noticed
# immediately, rather than quietly publishing a new endpoint.

_ROUTE_SCOPES: tuple[tuple[str, str, str], ...] = (
    # (method, path prefix, required scope) — longest prefix wins.
    ("POST", "/v1/executions", EXECUTIONS_WRITE),
    ("GET", "/v1/executions", EXECUTIONS_READ),
    ("GET", "/v1/agents", EXECUTIONS_READ),
    ("GET", "/v1/capabilities", EXECUTIONS_READ),
    ("GET", "/v1/tools", EXECUTIONS_READ),
    ("GET", "/v1/models", EXECUTIONS_READ),
    ("GET", "/v1/skills", EXECUTIONS_READ),
    ("GET", "/v1/workflows", EXECUTIONS_READ),
    ("GET", "/v1/mcp", EXECUTIONS_READ),
    ("GET", "/metrics", ADMIN),
    # Detailed health names providers, models, storage, and config paths.
    # Admin, like /metrics, for the same reason.
    ("GET", "/health", ADMIN),
    ("GET", "/v1/health", ADMIN),
    # Token metadata and revocation are administrative by nature.
    ("GET", "/v1/tokens", ADMIN),
    ("POST", "/v1/tokens", ADMIN),
)


def required_scope(method: str, path: str) -> str:
    """The scope a request needs. Unknown routes require admin."""
    method = (method or "GET").upper()

    # Specific paths first, since they are narrower than the prefixes above.
    if "/approvals/" in path:
        return APPROVALS_RESPOND
    if path.endswith("/audit"):
        return AUDIT_READ

    best: tuple[int, str] = (-1, ADMIN)
    for route_method, prefix, scope in _ROUTE_SCOPES:
        if route_method != method:
            continue
        if path.startswith(prefix) and len(prefix) > best[0]:
            best = (len(prefix), scope)
    return best[1]
