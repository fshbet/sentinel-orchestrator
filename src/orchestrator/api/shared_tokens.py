"""Token state shared across replicas, in PostgreSQL.

The in-process :class:`~orchestrator.api.tokens.TokenStore` is correct for one
process and wrong for several. Revocation on replica A was invisible to
replica B until B restarted, which meant "revoked" was a promise the
deployment could not keep — the credential kept working on roughly half the
traffic, and nothing said so.

This store keeps the same interface and puts the state in the database every
replica already shares.

**Two hashes, doing different jobs.** This is the part worth understanding:

``lookup_hash``
    A deterministic digest of the presented token. Deterministic by
    necessity — an index needs a stable key — so a request is one indexed
    ``SELECT`` rather than a scan over every token comparing digests. At a
    hundred tokens a scan is merely wasteful; at ten thousand it is an
    availability problem on the authentication path.

``digest``
    A per-row salted derivation, compared with :func:`hmac.compare_digest`.
    This is what actually authenticates. The lookup hash narrows to one
    candidate row; the digest decides whether it matches.

Using only the lookup hash would mean an unsalted, deployment-independent
digest was the whole credential check. Using only salted digests would mean
scanning. Both, each for its own job.

**The raw token is written nowhere** — not to the database, not to logs, not
to the audit trail. It exists in the environment variable it came from and in
memory for the duration of one request.

**No caching.** A cache would make revocation eventually-consistent, and the
whole point of this module is that it is not. One indexed lookup on a shared
connection pool is well under a millisecond; a five-second cache would buy
almost nothing and give back exactly the property being fixed. If a future
deployment needs one, it must be fail-closed — revocation checked live, the
cache holding only the digest comparison.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from .identity import SINGLE_TENANT, Principal, Unauthorized, expand_scopes
from .tokens import (
    CLOCK_SKEW,
    TokenExpired,
    TokenNotYetValid,
    TokenRecord,
    TokenRevoked,
    generate_token,
    hash_token,
)

# Domain separator for the lookup digest. Versioned so the scheme can change
# without a silent collision between old and new rows.
_LOOKUP_LABEL = b"orchestrator-token-lookup-v1"


def lookup_hash(token: str) -> str:
    """A deterministic, indexable digest of a token.

    Not the credential check — see the module docstring. This narrows the
    query to one row; ``TokenRecord.matches`` decides.
    """
    return hashlib.sha256(_LOOKUP_LABEL + token.encode("utf-8")).hexdigest()


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


class PostgresTokenStore:
    """Token lifecycle backed by shared PostgreSQL state.

    Interface-compatible with :class:`~orchestrator.api.tokens.TokenStore`, so
    the middleware does not know which one it holds.
    """

    #: Callers can check this rather than isinstance.
    distributed = True

    def __init__(self, pool: Any = None, *, audit=None, factory=None) -> None:
        """Take a pool, or a factory that will build one on first use.

        The factory exists because an asyncpg pool is bound to the event loop
        that created it. Building one during `create_app` — which runs before
        uvicorn's loop exists — produced a pool attached to a loop that was
        already closed, and every request then failed with "Event loop is
        closed". The factory is awaited once, inside the loop that will
        actually serve.
        """
        self._pool = pool
        self._factory = factory
        self._audit = audit
        self._lock = None

    async def _acquire_pool(self):
        if self._pool is not None:
            return self._pool
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._pool is None:
                self._pool = await self._factory()
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- helpers -----------------------------------------------------------

    def _record_event(self, event: str, **payload: Any) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(event, **payload)
        except Exception:  # noqa: BLE001,S110 - auditing must not break auth
            pass

    @staticmethod
    def _to_record(row: Any) -> TokenRecord:
        return TokenRecord(
            token_id=row["token_id"],
            principal_id=row["principal_id"],
            digest=row["digest"],
            salt=row["salt"],
            scopes=expand_scopes(list(row["scopes"] or ())),
            tenant=row["tenant"],
            issued_at=_aware(row["issued_at"]) or datetime.now(UTC),
            not_before=_aware(row["not_before"]),
            expires_at=_aware(row["expires_at"]),
            description=row["description"] or "",
        )

    # -- issuing -----------------------------------------------------------

    async def issue(
        self,
        principal_id: str,
        *,
        scopes=(),
        tenant: str = SINGLE_TENANT,
        lifetime: timedelta | None = None,
        not_before: datetime | None = None,
        description: str = "",
        token: str | None = None,
    ) -> tuple[str, TokenRecord]:
        """Issue or re-register a token. Returns the secret once.

        ``ON CONFLICT (lookup_hash)`` updates the row rather than inserting a
        duplicate, so every replica seeding the same configured tokens at
        startup converges on one row per credential instead of one per
        replica — and a token revoked earlier is not silently un-revoked by
        the next replica that boots.
        """
        secret = token or generate_token()
        salt = secrets.token_hex(16)
        now = datetime.now(UTC)
        token_id = f"tok_{secrets.token_hex(8)}"
        expires_at = now + lifetime if lifetime else None
        scope_list = sorted(expand_scopes(scopes))

        pool = await self._acquire_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "INSERT INTO api_tokens (token_id, lookup_hash, digest, salt, "
                "  principal_id, scopes, tenant, description, issued_at, "
                "  not_before, expires_at) "
                "VALUES ($1,$2,$3,$4,$5,$6::text[],$7,$8,$9,$10,$11) "
                "ON CONFLICT (lookup_hash) DO UPDATE SET "
                "  principal_id = EXCLUDED.principal_id, "
                "  scopes = EXCLUDED.scopes, "
                "  tenant = EXCLUDED.tenant, "
                "  description = EXCLUDED.description, "
                "  not_before = EXCLUDED.not_before, "
                "  expires_at = EXCLUDED.expires_at "
                "RETURNING *",
                token_id, lookup_hash(secret), hash_token(secret, salt), salt,
                principal_id, scope_list, tenant, description, now,
                not_before, expires_at,
            )

        record = self._to_record(row)
        self._record_event("token.issued", **record.describe())
        return secret, record

    # -- revocation --------------------------------------------------------

    async def revoke(self, token_id: str, *, reason: str = "", actor: str = "") -> bool:
        """Revoke a token for every replica at once.

        The write is the revocation. There is no broadcast, no cache to
        invalidate, and no window: the next request on any replica reads this
        row.
        """
        pool = await self._acquire_pool()
        async with pool.acquire() as connection:
            result = await connection.execute(
                "UPDATE api_tokens SET revoked_at = now(), revoked_by = $2, "
                "  revoke_reason = $3 "
                "WHERE token_id = $1 AND revoked_at IS NULL",
                token_id, actor or "unknown", reason,
            )
            if result.endswith(" 0"):
                # Either unknown or already revoked; distinguish them so a
                # second revoke is idempotent rather than a false negative.
                exists = await connection.fetchval(
                    "SELECT 1 FROM api_tokens WHERE token_id = $1", token_id
                )
                if not exists:
                    return False

        self._record_event(
            "token.revoked", token_id=token_id, reason=reason, revoked_by=actor
        )
        return True

    async def is_revoked(self, token_id: str) -> bool:
        pool = await self._acquire_pool()
        async with pool.acquire() as connection:
            return bool(await connection.fetchval(
                "SELECT revoked_at IS NOT NULL FROM api_tokens WHERE token_id = $1",
                token_id,
            ))

    # -- resolution --------------------------------------------------------

    async def resolve(self, presented: str | None, *, now: datetime | None = None):
        """Resolve a credential against shared state.

        One indexed lookup. Constant-time digest comparison. Revocation and
        the validity window are read from the row on every request, so a
        revocation performed on another replica a moment ago is honoured here.
        """
        if not presented:
            raise Unauthorized("no credential presented")

        pool = await self._acquire_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM api_tokens WHERE lookup_hash = $1",
                lookup_hash(presented),
            )

        if row is None:
            self._record_event("token.rejected", reason="unrecognised")
            raise Unauthorized("credential not recognised")

        record = self._to_record(row)

        # Constant-time, even though the lookup already narrowed to one row:
        # a lookup-hash collision or a tampered row must not authenticate.
        if not record.matches(presented):
            self._record_event(
                "token.rejected", token_id=record.token_id, reason="digest_mismatch"
            )
            raise Unauthorized("credential not recognised")

        if row["revoked_at"] is not None:
            self._record_event(
                "token.rejected", token_id=record.token_id, reason="revoked"
            )
            raise TokenRevoked(f"token {record.token_id} has been revoked")

        try:
            record.check_window(now)
        except (TokenExpired, TokenNotYetValid) as exc:
            self._record_event(
                "token.rejected",
                token_id=record.token_id,
                reason="expired" if isinstance(exc, TokenExpired) else "not_yet_valid",
            )
            raise

        return record.to_principal()

    # -- inspection --------------------------------------------------------

    async def list_tokens(self) -> list[dict[str, Any]]:
        """Metadata for every token. Never a secret, never a digest."""
        now = datetime.now(UTC)
        pool = await self._acquire_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT * FROM api_tokens ORDER BY issued_at, token_id"
            )

        out = []
        for row in rows:
            record = self._to_record(row)
            described = record.describe()
            described["revoked"] = row["revoked_at"] is not None
            revoked_at = _aware(row["revoked_at"])
            described["revoked_at"] = revoked_at.isoformat() if revoked_at else None
            described["revoked_by"] = row["revoked_by"]
            expires = _aware(row["expires_at"])
            described["expired"] = bool(expires and now - CLOCK_SKEW > expires)
            out.append(described)
        return out

    async def count(self) -> int:
        pool = await self._acquire_pool()
        async with pool.acquire() as connection:
            return int(await connection.fetchval("SELECT count(*) FROM api_tokens"))

    async def active(self) -> int:
        pool = await self._acquire_pool()
        async with pool.acquire() as connection:
            return int(await connection.fetchval(
                "SELECT count(*) FROM api_tokens WHERE revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now()) "
                "AND (not_before IS NULL OR not_before <= now())"
            ))

    # -- seeding -----------------------------------------------------------

    async def seed_from_config(self, api: dict[str, Any] | None = None) -> int:
        """Register the tokens named in configuration.

        Idempotent across replicas and restarts: the upsert keys on
        ``lookup_hash``, so the same credential yields the same row however
        many replicas boot. A token revoked through the API stays revoked —
        the upsert deliberately does not clear ``revoked_at``, because a
        rolling restart must not resurrect a credential somebody killed.
        """
        import os

        api = api or {}
        seeded = 0

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
            await self.issue(
                principal_id,
                scopes=scopes if scopes is not None else ("executions.read",),
                tenant=str(raw.get("tenant") or SINGLE_TENANT),
                lifetime=lifetime,
                description=str(raw.get("name") or principal_id),
                token=secret,
            )
            seeded += 1

        # Legacy. Registered so it authenticates, and so its metadata is
        # visible — but see the note in SECURITY.md: removing it needs a
        # config change and a restart, because the environment is its source
        # of truth and nothing here can edit the environment.
        legacy = os.environ.get("ORCHESTRATOR_API_TOKEN", "")
        for position, token in enumerate(t.strip() for t in legacy.split(",")):
            if not token:
                continue
            await self.issue(
                "legacy-token" if position == 0 else f"legacy-token-{position}",
                scopes=("admin",),
                description="legacy ORCHESTRATOR_API_TOKEN (re-seeded on restart)",
                token=token,
            )
            seeded += 1

        return seeded


class SharedIdentityRegistry:
    """The middleware's view of shared token state.

    Same shape as :class:`~orchestrator.api.identity.IdentityRegistry` so the
    middleware is unchanged, but every method is a coroutine because the
    answers live in the database.
    """

    def __init__(self, store: PostgresTokenStore, *, multi_tenant: bool = False,
                 token_count: int = 0, seed: dict | None = None) -> None:
        self._store = store
        self.multi_tenant = multi_tenant
        # The config section to seed from, applied during app startup on the
        # serving loop rather than at construction.
        self.seed = seed
        # Captured at startup so `enabled` — consulted by the insecure-binding
        # check before any request — does not need a query.
        self._token_count = token_count

    @property
    def enabled(self) -> bool:
        return self._token_count > 0

    def __len__(self) -> int:
        return self._token_count

    @property
    def tokens(self) -> PostgresTokenStore:
        return self._store

    def principals(self) -> list[Principal]:
        # Metadata lives in the database; the middleware does not need this.
        return []

    async def resolve(self, presented: str | None) -> Principal:
        return await self._store.resolve(presented)

    async def list_tokens(self) -> list[dict[str, Any]]:
        return await self._store.list_tokens()

    async def revoke(self, token_id: str, *, reason: str = "", actor: str = "") -> bool:
        return await self._store.revoke(token_id, reason=reason, actor=actor)
