"""Token state shared across replicas.

The property under test is one the in-process store cannot have: a revocation
performed by one process is honoured by a *different* process, immediately,
with no restart and no message between them. Every test here therefore uses
two independent store instances against the same database — that is what a
second replica is.

Skipped cleanly without ORCHESTRATOR_TEST_POSTGRES_DSN; CI supplies it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.api.identity import ADMIN, EXECUTIONS_READ, Unauthorized
from orchestrator.api.shared_tokens import (
    PostgresTokenStore,
    SharedIdentityRegistry,
    lookup_hash,
)
from orchestrator.api.tokens import CLOCK_SKEW, TokenExpired, TokenNotYetValid, TokenRevoked

DSN = os.environ.get("ORCHESTRATOR_TEST_POSTGRES_DSN", "").strip()
requires_postgres = pytest.mark.skipif(
    not DSN, reason="set ORCHESTRATOR_TEST_POSTGRES_DSN to run these"
)


# --------------------------------------------------------------------------
# No database required
# --------------------------------------------------------------------------


def test_the_lookup_hash_is_deterministic_and_domain_separated():
    """Deterministic so it can be indexed; labelled so schemes cannot collide."""
    assert lookup_hash("a-token") == lookup_hash("a-token")
    assert lookup_hash("a-token") != lookup_hash("b-token")
    assert len(lookup_hash("x")) == 64
    # Not a bare sha256 of the token: the label is mixed in.
    import hashlib

    assert lookup_hash("x") != hashlib.sha256(b"x").hexdigest()


def test_the_store_matches_the_in_process_interface():
    """The middleware must not need to know which one it holds."""
    from orchestrator.api.tokens import TokenStore

    shared = {m for m in dir(PostgresTokenStore) if not m.startswith("_")}
    for method in ("issue", "revoke", "resolve", "list_tokens", "is_revoked"):
        assert method in shared, method
        assert hasattr(TokenStore, method), method


def test_the_shared_store_declares_itself_distributed():
    """Callers report this to operators; guessing would be worse than asking."""
    from orchestrator.api.tokens import TokenStore

    assert PostgresTokenStore.distributed is True
    assert getattr(TokenStore, "distributed", False) is False


# --------------------------------------------------------------------------
# Two replicas, one database
# --------------------------------------------------------------------------


def two_replicas(body):
    """Run ``body(replica_a, replica_b)`` against two independent pools.

    Two pools, not one shared object: a second connection pool is what a
    second container actually has, and a test that shares an object in memory
    would prove nothing about distribution.
    """
    import asyncpg

    async def main():
        pool_a = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        pool_b = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            async with pool_a.acquire() as connection:
                await connection.execute("TRUNCATE api_tokens")
            return await body(PostgresTokenStore(pool_a), PostgresTokenStore(pool_b))
        finally:
            await pool_a.close()
            await pool_b.close()

    return asyncio.run(main())


@requires_postgres
def test_a_token_issued_on_one_replica_resolves_on_the_other():
    async def body(a, b):
        secret, record = await a.issue("ops", scopes=[ADMIN])
        principal = await b.resolve(secret)
        assert principal.id == "ops"
        assert principal.token_id == record.token_id
        assert principal.has(ADMIN)

    two_replicas(body)


@requires_postgres
def test_revoking_on_one_replica_is_immediate_on_the_other():
    """The whole point. No restart, no broadcast, no cache to expire."""

    async def body(a, b):
        secret, record = await a.issue("ci", scopes=[EXECUTIONS_READ])

        assert (await b.resolve(secret)).id == "ci"  # B accepts it
        assert await a.revoke(record.token_id, reason="leaked") is True

        with pytest.raises(TokenRevoked):
            await b.resolve(secret)  # B rejects it now
        with pytest.raises(TokenRevoked):
            await a.resolve(secret)

    two_replicas(body)


@requires_postgres
def test_revoking_one_token_leaves_the_others_working_on_both_replicas():
    async def body(a, b):
        victim, victim_record = await a.issue("ci", scopes=[EXECUTIONS_READ])
        survivor, _ = await a.issue("admin", scopes=[ADMIN])

        await b.revoke(victim_record.token_id, reason="test")

        for store in (a, b):
            with pytest.raises(TokenRevoked):
                await store.resolve(victim)
            assert (await store.resolve(survivor)).id == "admin"

    two_replicas(body)


@requires_postgres
def test_expiry_is_enforced_on_every_replica():
    async def body(a, b):
        secret, _ = await a.issue("ci", scopes=[ADMIN], lifetime=-(CLOCK_SKEW * 10))
        for store in (a, b):
            with pytest.raises(TokenExpired):
                await store.resolve(secret)

    two_replicas(body)


@requires_postgres
def test_not_before_is_enforced_on_every_replica():
    async def body(a, b):
        secret, _ = await a.issue(
            "ci",
            scopes=[ADMIN],
            not_before=datetime.now(UTC) + timedelta(hours=2),
        )
        for store in (a, b):
            with pytest.raises(TokenNotYetValid):
                await store.resolve(secret)

    two_replicas(body)


@requires_postgres
def test_an_unknown_credential_is_rejected_on_every_replica():
    async def body(a, b):
        await a.issue("ops", scopes=[ADMIN])
        for store in (a, b):
            with pytest.raises(Unauthorized):
                await store.resolve("never-issued-anywhere")

    two_replicas(body)


# --------------------------------------------------------------------------
# What the database holds
# --------------------------------------------------------------------------


@requires_postgres
def test_the_database_never_contains_the_raw_token():
    async def body(a, _b):
        secret, _ = await a.issue("ops", scopes=[ADMIN], description="operations console")
        async with a._pool.acquire() as connection:
            rows = await connection.fetch("SELECT * FROM api_tokens")
        dumped = "".join(str(dict(r)) for r in rows)
        assert secret not in dumped, "the raw token was written to the database"
        # And the columns that do exist are the two hashes, not the value.
        assert rows[0]["lookup_hash"] and rows[0]["digest"]
        assert rows[0]["lookup_hash"] != secret
        assert rows[0]["digest"] != secret

    two_replicas(body)


@requires_postgres
def test_listing_returns_metadata_and_never_a_secret_or_digest():
    async def body(a, b):
        secret, record = await a.issue("ops", scopes=[ADMIN])
        listed = await b.list_tokens()
        assert len(listed) == 1
        entry = listed[0]
        assert secret not in str(entry)
        assert "digest" not in entry
        assert "salt" not in entry
        assert entry["token_id"] == record.token_id
        assert entry["revoked"] is False

    two_replicas(body)


@requires_postgres
def test_a_revocation_is_visible_in_the_listing_from_the_other_replica():
    async def body(a, b):
        _secret, record = await a.issue("ci", scopes=[EXECUTIONS_READ])
        await a.revoke(record.token_id, reason="rotated", actor="token-admin")

        entry = [t for t in await b.list_tokens() if t["token_id"] == record.token_id][0]
        assert entry["revoked"] is True
        assert entry["revoked_at"] is not None
        assert entry["revoked_by"] == "token-admin"

    two_replicas(body)


# --------------------------------------------------------------------------
# Lookup is indexed, not a scan
# --------------------------------------------------------------------------


@requires_postgres
def test_resolution_uses_the_index_rather_than_scanning():
    """A scan is wasteful at a hundred tokens and an outage at ten thousand."""

    async def body(a, _b):
        for index in range(200):
            await a.issue(f"principal-{index}", scopes=[EXECUTIONS_READ])
        secret, _ = await a.issue("target", scopes=[ADMIN])

        async with a._pool.acquire() as connection:
            plan = await connection.fetch(
                "EXPLAIN SELECT * FROM api_tokens WHERE lookup_hash = $1",
                lookup_hash(secret),
            )
        text = " ".join(row["QUERY PLAN"] for row in plan)
        assert "Index" in text or "Bitmap" in text, text
        assert "Seq Scan" not in text, text

    two_replicas(body)


# --------------------------------------------------------------------------
# Seeding from configuration
# --------------------------------------------------------------------------


@requires_postgres
def test_seeding_is_idempotent_across_replicas(monkeypatch):
    """Every replica seeds at startup; that must converge, not multiply."""
    monkeypatch.setenv("SEED_OPS_TOKEN", "seeded-ops-value")
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)

    api = {
        "principals": [{"id": "ops", "token_env": "SEED_OPS_TOKEN", "scopes": ["admin"]}]
    }

    async def body(a, b):
        await a.seed_from_config(api)
        await b.seed_from_config(api)  # the second replica booting
        await a.seed_from_config(api)  # a restart

        listed = await a.list_tokens()
        assert len(listed) == 1, [t["principal"] for t in listed]
        assert (await b.resolve("seeded-ops-value")).id == "ops"

    two_replicas(body)


@requires_postgres
def test_a_rolling_restart_does_not_resurrect_a_revoked_token(monkeypatch):
    """Re-seeding must not undo somebody's revocation."""
    monkeypatch.setenv("SEED_CI_TOKEN", "seeded-ci-value")
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)

    api = {
        "principals": [
            {"id": "ci", "token_env": "SEED_CI_TOKEN", "scopes": ["executions.read"]}
        ]
    }

    async def body(a, b):
        await a.seed_from_config(api)
        record = (await a.list_tokens())[0]
        await a.revoke(record["token_id"], reason="leaked")

        # Replica B restarts and re-seeds.
        await b.seed_from_config(api)

        with pytest.raises(TokenRevoked):
            await b.resolve("seeded-ci-value")
        with pytest.raises(TokenRevoked):
            await a.resolve("seeded-ci-value")

    two_replicas(body)


@requires_postgres
def test_the_registry_reports_shared_state():
    async def body(a, _b):
        await a.issue("ops", scopes=[ADMIN])
        registry = SharedIdentityRegistry(a, token_count=await a.count())
        assert registry.enabled is True
        assert registry.tokens.distributed is True

    two_replicas(body)


@requires_postgres
def test_revoking_an_unknown_token_reports_that_it_was_unknown():
    async def body(a, _b):
        assert await a.revoke("tok_does_not_exist") is False

    two_replicas(body)


@requires_postgres
def test_revocation_is_idempotent_across_replicas():
    async def body(a, b):
        secret, record = await a.issue("ci", scopes=[ADMIN])
        assert await a.revoke(record.token_id) is True
        assert await b.revoke(record.token_id) is True  # already revoked
        with pytest.raises(TokenRevoked):
            await b.resolve(secret)

    two_replicas(body)


# --------------------------------------------------------------------------
# Distributed rate limiting
# --------------------------------------------------------------------------
#
# The property the in-process limiter cannot have: a request counted by one
# replica is visible to the other, so N replicas do not permit N times the
# configured rate.


def two_limiters(body):
    """Run ``body(limiter_a, limiter_b)`` on two independent pools."""
    import asyncpg

    from orchestrator.api.ratelimit import PostgresRateLimiter

    async def main():
        pool_a = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        pool_b = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            async with pool_a.acquire() as connection:
                await connection.execute("TRUNCATE rate_limit_hits")
            return await body(PostgresRateLimiter(pool_a), PostgresRateLimiter(pool_b))
        finally:
            await pool_a.close()
            await pool_b.close()

    return asyncio.run(main())


@requires_postgres
def test_two_replicas_share_one_quota():
    """Split across replicas, the total is still the configured limit."""
    from orchestrator.api.ratelimit import RateLimit

    async def body(a, b):
        limit = RateLimit(requests=4, window_seconds=60)

        # Two on A, two on B — that is the whole budget.
        for store in (a, b, a, b):
            assert (await store.check_async("shared:key", limit)).allowed is True

        # The fifth is refused on *either* replica.
        assert (await a.check_async("shared:key", limit)).allowed is False
        assert (await b.check_async("shared:key", limit)).allowed is False

    two_limiters(body)


@requires_postgres
def test_the_remaining_count_decreases_across_replicas():
    from orchestrator.api.ratelimit import RateLimit

    async def body(a, b):
        limit = RateLimit(requests=3, window_seconds=60)
        first = await a.check_async("k", limit)
        second = await b.check_async("k", limit)
        assert first.remaining == 2
        assert second.remaining == 1, "replica B did not see replica A's request"

    two_limiters(body)


@requires_postgres
def test_a_refusal_reports_retry_after():
    from orchestrator.api.ratelimit import RateLimit

    async def body(a, b):
        limit = RateLimit(requests=1, window_seconds=60)
        await a.check_async("k", limit)
        refused = await b.check_async("k", limit)
        assert refused.allowed is False
        assert 0 < refused.retry_after <= 60

    two_limiters(body)


@requires_postgres
def test_separate_keys_have_separate_quotas():
    from orchestrator.api.ratelimit import RateLimit

    async def body(a, b):
        limit = RateLimit(requests=1, window_seconds=60)
        assert (await a.check_async("principal:alice", limit)).allowed is True
        assert (await b.check_async("principal:bob", limit)).allowed is True
        assert (await b.check_async("principal:alice", limit)).allowed is False

    two_limiters(body)


@requires_postgres
def test_the_window_slides():
    from orchestrator.api.ratelimit import RateLimit

    async def body(a, b):
        limit = RateLimit(requests=1, window_seconds=1)
        assert (await a.check_async("k", limit)).allowed is True
        assert (await b.check_async("k", limit)).allowed is False
        await asyncio.sleep(1.2)
        assert (await b.check_async("k", limit)).allowed is True

    two_limiters(body)


@requires_postgres
def test_the_table_does_not_grow_without_bound():
    """Every statement prunes its own bucket, so old rows do not accumulate."""
    from orchestrator.api.ratelimit import RateLimit

    async def body(a, _b):
        limit = RateLimit(requests=100, window_seconds=1)
        for _ in range(20):
            await a.check_async("k", limit)
        await asyncio.sleep(1.2)
        await a.check_async("k", limit)

        async with a._pool.acquire() as connection:
            rows = await connection.fetchval(
                "SELECT count(*) FROM rate_limit_hits WHERE bucket = 'k'"
            )
        assert rows == 1, f"{rows} rows survived the window"

    two_limiters(body)


@requires_postgres
def test_concurrent_replicas_cannot_both_admit_the_last_request():
    """The race the single statement exists to prevent."""
    from orchestrator.api.ratelimit import RateLimit

    async def body(a, b):
        limit = RateLimit(requests=1, window_seconds=60)
        results = await asyncio.gather(
            a.check_async("race", limit), b.check_async("race", limit)
        )
        admitted = [r for r in results if r.allowed]
        assert len(admitted) == 1, [r.allowed for r in results]

    two_limiters(body)


@requires_postgres
def test_strict_categories_are_stricter_than_the_default():
    from orchestrator.api.ratelimit import STRICT_CATEGORIES, RateLimit

    default = RateLimit(requests=60, window_seconds=60)
    for name, limit in STRICT_CATEGORIES.items():
        assert limit.requests < default.requests, name
