"""Token lifecycle: expiry, revocation, and hashed storage.

The previous model was a bearer string compared against an environment
variable. Three things were missing and each has a cost:

* **No expiry.** A token handed to a contractor in March still works in
  December.
* **No revocation without a restart.** The response to a leaked credential was
  "redeploy", which is exactly the wrong latency for that event.
* **Raw storage.** Tokens lived in memory as plaintext, so anything that
  dumped process state dumped every credential.

What this module does about each:

* Tokens carry ``not_before`` and ``expires_at``, checked on every use against
  a small clock skew allowance. An expired token is rejected as expired, which
  is a different message from "not recognised" — the first tells an operator
  to reissue, the second sends them hunting.
* Revocation is a set of token ids consulted per request, so revoking takes
  effect on the next call rather than the next deploy.
* Only a salted SHA-256 of the token is retained. The presented value is
  hashed and compared in constant time; the original is never held.

**No password storage of any kind.** These are machine credentials — issued,
carried, and revoked. There is no login, no reset, and no user record. That is
a deliberate boundary: password handling is a different problem with different
requirements, and the right answer for human identity is an external provider
(see ``proxy_identity`` below), not a table in this database.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..errors import ConfigurationError
from .identity import SINGLE_TENANT, Principal, expand_scopes

# Clocks disagree. A token issued on one host and checked on another can look
# not-yet-valid by a second or two; rejecting that is a self-inflicted outage.
CLOCK_SKEW = timedelta(seconds=60)

# Iterations for the derivation below. Machine tokens are high-entropy random
# strings, not passwords, so this does not need to be slow — it needs to be
# one-way. The salt is what stops a precomputed table.
_HASH_NAME = "sha256"


class TokenExpired(Exception):
    """A credential that was valid and no longer is."""


class TokenNotYetValid(Exception):
    """A credential presented before its not-before time."""


class TokenRevoked(Exception):
    """A credential that was explicitly revoked."""


def generate_token() -> str:
    """A new machine credential.

    256 bits from the OS CSPRNG, URL-safe so it survives being pasted into a
    header, a YAML file, or a shell.
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str, salt: str) -> str:
    """A one-way digest of a token.

    Salted per record so two principals sharing a token — which should not
    happen, but does — do not produce the same digest, and so a digest lifted
    from one deployment says nothing about another.
    """
    return hashlib.pbkdf2_hmac(
        _HASH_NAME, token.encode("utf-8"), salt.encode("utf-8"), 1
    ).hex()


@dataclass(frozen=True)
class TokenRecord:
    """An issued credential, stored without its secret."""

    token_id: str
    principal_id: str
    digest: str
    salt: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    tenant: str = SINGLE_TENANT
    issued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    not_before: datetime | None = None
    expires_at: datetime | None = None
    description: str = ""

    def matches(self, presented: str) -> bool:
        return hmac.compare_digest(hash_token(presented, self.salt), self.digest)

    def check_window(self, now: datetime | None = None) -> None:
        """Raise if this token is outside its validity window."""
        moment = now or datetime.now(UTC)
        if self.not_before and moment + CLOCK_SKEW < self.not_before:
            raise TokenNotYetValid(
                f"token {self.token_id} is not valid until {self.not_before.isoformat()}"
            )
        if self.expires_at and moment - CLOCK_SKEW > self.expires_at:
            raise TokenExpired(
                f"token {self.token_id} expired at {self.expires_at.isoformat()}"
            )

    def to_principal(self) -> Principal:
        return Principal(
            id=self.principal_id,
            scopes=self.scopes,
            tenant=self.tenant,
            token_id=self.token_id,
            source="token",
            display_name=self.description or self.principal_id,
        )

    def describe(self) -> dict[str, Any]:
        """Everything except the secret. Safe for logs and the audit trail."""
        return {
            "token_id": self.token_id,
            "principal": self.principal_id,
            "tenant": self.tenant,
            "scopes": sorted(self.scopes),
            "issued_at": self.issued_at.isoformat(),
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "description": self.description,
        }


class TokenStore:
    """Issued tokens, their validity windows, and revocations.

    In-process: revocation applies to this instance immediately. Behind a load
    balancer, revoking must reach every replica — the audit event is emitted
    so an operator can confirm it did, and the limitation is stated in
    SECURITY.md rather than papered over.
    """

    def __init__(self, records: Iterable[TokenRecord] = (), *, audit=None) -> None:
        self._records: list[TokenRecord] = list(records)
        self._revoked: set[str] = set()
        self._audit = audit

    def __len__(self) -> int:
        """How many tokens are configured, revoked or not.

        Deliberately not "how many are currently usable". Callers use this to
        decide *whether authentication is configured* — and revoking the last
        token must not answer "no". It previously did, which meant revoking a
        single-token deployment's only credential silently switched the API
        back to the legacy comparison path and returned "no bearer token"
        instead of "revoked". Use :meth:`active` for the usable count.
        """
        return len(self._records)

    def active(self) -> int:
        """Tokens that are neither revoked nor outside their validity window."""
        now = datetime.now(UTC)
        usable = 0
        for record in self._records:
            if record.token_id in self._revoked:
                continue
            try:
                record.check_window(now)
            except (TokenExpired, TokenNotYetValid):
                continue
            usable += 1
        return usable

    def _record_event(self, event: str, **payload: Any) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(event, **payload)
        except Exception:  # noqa: BLE001,S110 - auditing must not break auth
            pass

    # -- issuing -----------------------------------------------------------

    def issue(
        self,
        principal_id: str,
        *,
        scopes: Iterable[str] = (),
        tenant: str = SINGLE_TENANT,
        lifetime: timedelta | None = None,
        not_before: datetime | None = None,
        description: str = "",
        token: str | None = None,
    ) -> tuple[str, TokenRecord]:
        """Issue a token. Returns the secret once; only its digest is kept.

        The caller gets exactly one chance to record the value. That is not an
        inconvenience to design around — it is the property that makes the
        store safe to dump.
        """
        secret = token or generate_token()
        salt = secrets.token_hex(16)
        now = datetime.now(UTC)
        record = TokenRecord(
            token_id=f"tok_{secrets.token_hex(8)}",
            principal_id=principal_id,
            digest=hash_token(secret, salt),
            salt=salt,
            scopes=expand_scopes(scopes),
            tenant=tenant,
            issued_at=now,
            not_before=not_before,
            expires_at=now + lifetime if lifetime else None,
            description=description,
        )
        self._records.append(record)
        self._record_event("token.issued", **record.describe())
        return secret, record

    # -- revocation --------------------------------------------------------

    def revoke(self, token_id: str, *, reason: str = "") -> bool:
        """Revoke one token. Effective on the next request, not the next deploy."""
        known = any(r.token_id == token_id for r in self._records)
        if not known:
            return False
        self._revoked.add(token_id)
        self._record_event("token.revoked", token_id=token_id, reason=reason)
        return True

    def is_revoked(self, token_id: str) -> bool:
        return token_id in self._revoked

    def revoked_ids(self) -> frozenset[str]:
        return frozenset(self._revoked)

    # -- resolution --------------------------------------------------------

    def resolve(self, presented: str | None, *, now: datetime | None = None) -> Principal:
        """Find the principal for a credential, honouring its lifecycle.

        Every record is checked even after a match so timing does not reveal
        how many tokens exist or which one was presented.
        """
        from .identity import Unauthorized

        if not presented:
            raise Unauthorized("no credential presented")

        found: TokenRecord | None = None
        for record in self._records:
            if record.matches(presented):
                found = record

        if found is None:
            self._record_event("token.rejected", reason="unrecognised")
            raise Unauthorized("credential not recognised")

        if self.is_revoked(found.token_id):
            self._record_event("token.rejected", token_id=found.token_id, reason="revoked")
            raise TokenRevoked(f"token {found.token_id} has been revoked")

        try:
            found.check_window(now)
        except (TokenExpired, TokenNotYetValid) as exc:
            self._record_event(
                "token.rejected",
                token_id=found.token_id,
                reason="expired" if isinstance(exc, TokenExpired) else "not_yet_valid",
            )
            raise

        return found.to_principal()

    def list_tokens(self) -> list[dict[str, Any]]:
        """Every token's metadata, with revocation and expiry status."""
        now = datetime.now(UTC)
        out = []
        for record in self._records:
            described = record.describe()
            described["revoked"] = self.is_revoked(record.token_id)
            described["expired"] = bool(
                record.expires_at and now - CLOCK_SKEW > record.expires_at
            )
            out.append(described)
        return out

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, api: dict[str, Any] | None = None, *, audit=None) -> TokenStore:
        """Build from ``api.principals``, honouring per-principal lifetimes.

        Token values still come from environment variables. They are hashed on
        load and the plaintext is dropped, so a memory dump does not yield
        working credentials even though the environment still holds them.
        """
        api = api or {}
        store = cls(audit=audit)

        for raw in api.get("principals") or []:
            if not isinstance(raw, dict):
                continue
            principal_id = str(raw.get("id") or "").strip()
            variable = str(raw.get("token_env") or "").strip()
            if not principal_id or not variable:
                continue
            secret = os.environ.get(variable, "").strip()
            if not secret:
                continue

            lifetime = None
            if raw.get("lifetime_days"):
                lifetime = timedelta(days=float(raw["lifetime_days"]))
            elif raw.get("lifetime_hours"):
                lifetime = timedelta(hours=float(raw["lifetime_hours"]))

            scopes = raw.get("scopes")
            store.issue(
                principal_id,
                scopes=scopes if scopes is not None else ("executions.read",),
                tenant=str(raw.get("tenant") or SINGLE_TENANT),
                lifetime=lifetime,
                description=str(raw.get("name") or principal_id),
                token=secret,
            )

        # Legacy: the single full-access token, unchanged in behaviour.
        legacy = os.environ.get("ORCHESTRATOR_API_TOKEN", "")
        for position, token in enumerate(t.strip() for t in legacy.split(",")):
            if not token:
                continue
            store.issue(
                "legacy-token" if position == 0 else f"legacy-token-{position}",
                scopes=("admin",),
                description="legacy full-access token (no expiry)",
                token=token,
            )

        return store


# ==========================================================================
# External identity: trusted reverse proxy — NOT INTEGRATED
# ==========================================================================
#
# **This is an unintegrated extension. The API middleware does not call it,
# and no shipped deployment enables it.** The supported identity mode is
# bearer tokens (``TokenStore`` above), which is what the middleware resolves
# and what `deployment/config.production.yaml` configures.
#
# What exists here: the header-trust logic, unit-tested, that a proxy-identity
# integration would need. What does not exist: the middleware call site, an
# authenticating proxy in the Compose stack, and any end-to-end test of the
# two together. `api.proxy_identity.enabled: true` is rejected by
# configuration validation rather than silently doing nothing, because a
# setting that appears to enable authentication and does not is worse than
# one that is absent.
#
# Finishing it means Option A in full: an OIDC-aware proxy (oauth2-proxy,
# Pomerium, an ALB with OIDC) in front, the middleware consulting
# `proxy_identity` before token resolution, and end-to-end tests covering a
# trusted proxy, a spoofed direct request, absent headers, and group→scope
# mapping. Until all of that exists and is verified, this is scaffolding and
# is labelled as such.
#
# One integration, implemented completely, rather than two half-done.
#
# The alternative was OIDC JWT validation. Doing that properly means RSA/ECDSA
# signature verification, JWKS fetching with rotation and caching, and issuer
# and audience checks — and doing it *improperly* means hand-rolled crypto in
# an authentication path, which is worse than not offering it. This platform's
# core carries no third-party dependencies, so there is no vetted JWT library
# already present to lean on.
#
# The proxy mode is the honest choice: an OIDC-aware proxy (oauth2-proxy,
# Envoy, Istio, an ALB with OIDC, Pomerium) does the token validation it is
# already good at, and passes the result as headers. What this platform must
# then get right is one thing, and it is the thing that is usually got wrong:
# **never believe those headers from anyone but the proxy.**


@dataclass(frozen=True)
class ProxyIdentityConfig:
    """Trusting an authenticating reverse proxy."""

    enabled: bool = False
    # Peer addresses whose identity headers are believed. Empty means none:
    # without this, any caller can claim to be any user by setting a header.
    trusted_proxies: tuple[str, ...] = ()
    user_header: str = "x-forwarded-user"
    groups_header: str = "x-forwarded-groups"
    tenant_header: str = "x-forwarded-tenant"
    # Group name -> scopes. A group not listed grants nothing.
    group_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    default_scopes: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.enabled and not self.trusted_proxies:
            raise ConfigurationError(
                "api.proxy_identity.enabled is true but no trusted_proxies are "
                "listed. Without them any caller could set the identity header "
                "and become any user. List the proxy's address.",
                remedy="set api.proxy_identity.trusted_proxies",
            )


def proxy_identity(
    peer: str,
    headers: dict[str, str],
    config: ProxyIdentityConfig,
) -> Principal | None:
    """Resolve an identity asserted by a trusted proxy, or None.

    Returns None rather than raising when the peer is untrusted: an
    unauthenticated request is not an error here, it just falls through to
    token authentication.
    """
    if not config.enabled:
        return None
    if peer not in config.trusted_proxies:
        # The header may well be present. It is not believed.
        return None

    lowered = {str(k).lower(): v for k, v in headers.items()}
    user = str(lowered.get(config.user_header, "")).strip()
    if not user:
        return None

    raw_groups = str(lowered.get(config.groups_header, ""))
    groups = [g.strip() for g in raw_groups.replace(";", ",").split(",") if g.strip()]

    scopes: set[str] = set(config.default_scopes)
    for group in groups:
        scopes.update(config.group_scopes.get(group, ()))

    tenant = str(lowered.get(config.tenant_header, "")).strip() or SINGLE_TENANT

    return Principal(
        id=user,
        scopes=expand_scopes(scopes),
        tenant=tenant,
        token_id=f"proxy:{peer}",
        source="proxy",
        display_name=user,
    )
