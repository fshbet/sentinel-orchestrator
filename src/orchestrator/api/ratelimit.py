"""Request rate limiting.

Deliberately an interface with a small in-process implementation behind it,
rather than a good in-process implementation.

An in-memory limiter is wrong for the deployment this platform is heading
toward, and wrong in a way that is easy to miss: its counters live in one
process, so two replicas allow twice the configured rate, and every deploy
resets everyone's budget. It is a real control for a single instance and a
false sense of one behind a load balancer.

So ``RateLimiter`` is the seam. ``InMemoryRateLimiter`` fills it for
single-instance deployments and says what it is. A Redis-backed implementation
is the intended production answer and is not written here; the interface is
three methods so that writing one is not a project. Where a reverse proxy
already enforces limits — the common case in front of anything internet-facing
— leave this disabled and let the proxy do it, since the proxy sheds load
before it reaches Python.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Protocol


@dataclass(frozen=True)
class RateLimit:
    """How many requests, over what window."""

    requests: int = 60
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.requests <= 0:
            raise ValueError("requests must be positive")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: float = 0.0
    limit: int = 0


class RateLimiter(Protocol):
    """The seam a Redis or proxy-backed limiter implements."""

    # True when this limiter's counters are shared across processes. Reported
    # in the readiness detail so an operator can see whether the limit they
    # configured is the limit they actually have.
    distributed: bool

    def check(self, key: str, limit: RateLimit) -> Decision: ...

    def reset(self, key: str) -> None: ...


class NullRateLimiter:
    """Allows everything. The default: limiting belongs at the proxy."""

    distributed = True   # nothing to disagree about across processes

    def check(self, key: str, limit: RateLimit) -> Decision:
        return Decision(allowed=True, remaining=limit.requests, limit=limit.requests)

    def reset(self, key: str) -> None:
        return None


class InMemoryRateLimiter:
    """A sliding-window limiter for one process.

    Correct for a single instance. Behind a load balancer, N replicas permit
    N times the configured rate, and a restart forgets every counter — which
    is why ``distributed`` is False and why the readiness report says so.
    """

    distributed = False

    def __init__(self, *, clock=time.monotonic, max_keys: int = 10_000) -> None:
        self._clock = clock
        self._hits: dict[str, list[float]] = {}
        # Bounded: the key is caller-influenced, so an unbounded dict is a
        # memory-exhaustion vector rather than a rate limiter.
        self._max_keys = max_keys

    def check(self, key: str, limit: RateLimit) -> Decision:
        now = self._clock()
        cutoff = now - limit.window_seconds

        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                self._evict(cutoff)
            hits = self._hits.setdefault(key, [])

        # Drop anything outside the window.
        fresh = [stamp for stamp in hits if stamp > cutoff]

        if len(fresh) >= limit.requests:
            self._hits[key] = fresh
            oldest = min(fresh)
            return Decision(
                allowed=False,
                remaining=0,
                retry_after=max(0.0, oldest + limit.window_seconds - now),
                limit=limit.requests,
            )

        fresh.append(now)
        self._hits[key] = fresh
        return Decision(
            allowed=True,
            remaining=limit.requests - len(fresh),
            limit=limit.requests,
        )

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def _evict(self, cutoff: float) -> None:
        """Drop keys with no recent activity, then oldest-first if still full."""
        stale = [key for key, hits in self._hits.items()
                 if not hits or max(hits) <= cutoff]
        for key in stale:
            self._hits.pop(key, None)
        if len(self._hits) >= self._max_keys:
            ordered = sorted(
                self._hits.items(), key=lambda item: max(item[1]) if item[1] else 0
            )
            for key, _ in ordered[: max(1, self._max_keys // 10)]:
                self._hits.pop(key, None)


def limiter_key(principal_id: str, client: str) -> str:
    """What a limit is counted against.

    The principal where there is one, because a credential is a stabler
    identity than an address: many callers share an egress IP, and one caller
    can move between them.
    """
    return f"principal:{principal_id}" if principal_id and principal_id != "local" \
        else f"client:{client}"


def build(config: dict | None) -> tuple[RateLimiter | None, RateLimit | None]:
    """Construct a limiter from the ``api.rate_limit`` config section."""
    config = config or {}
    if not config.get("enabled"):
        return NullRateLimiter(), None

    limit = RateLimit(
        requests=int(config.get("requests", 60)),
        window_seconds=float(config.get("window_seconds", 60.0)),
    )

    backend = str(config.get("backend", "memory")).lower()
    if backend == "memory":
        return InMemoryRateLimiter(), limit
    if backend == "none":
        return NullRateLimiter(), limit
    if backend == "postgres":
        # None, deliberately: the app constructs it because the app owns
        # the connection pool. Returning the limit anyway lets
        # configuration validation run without a database.
        return None, limit

    from ..errors import ConfigurationError

    raise ConfigurationError(
        f"unknown rate limit backend {backend!r}. 'memory' limits one process; "
        f"'none' defers to a reverse proxy. A shared backend is not built in — "
        f"implement the RateLimiter protocol and pass it to create_app.",
        backend=backend,
    )


# ==========================================================================
# Shared limiting, in PostgreSQL
# ==========================================================================
#
# Why not Redis: the deployment already has one shared datastore and adding a
# second for a single counter table means another component to run, secure,
# back up, and reason about during an incident. The counting is one indexed
# statement per request against a table that never grows — every row outside
# the window is deleted by the same statement that reads it. If a deployment
# already runs Redis, implementing `RateLimiter` against it is ~40 lines; the
# protocol exists for exactly that.
#
# Why it matters: the in-process limiter counts per replica, so two replicas
# permitted twice the configured rate and every deploy reset the counters. A
# limit that a caller can double by being load-balanced is not a limit.


class PostgresRateLimiter:
    """A sliding-window limiter whose counters are shared across replicas."""

    distributed = True

    def __init__(self, pool: Any = None, *, factory=None) -> None:
        """Pool or factory — see PostgresTokenStore for why a factory."""
        self._pool = pool
        self._factory = factory
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

    async def check_async(self, key: str, limit: RateLimit) -> Decision:
        """Prune, count, and conditionally insert, serialised per bucket.

        The obvious version — one statement with a CTE that counts and then
        inserts conditionally — is **not** atomic. Under READ COMMITTED two
        concurrent transactions both see the pre-insert count, both conclude
        they are one under the limit, and both admit. A two-replica test
        caught exactly that: `requests=1` admitted two.

        So the transaction takes a transaction-scoped advisory lock keyed on
        the bucket first. Contention is per principal per category, which is
        precisely the granularity where serialising is harmless, and the lock
        is released when the transaction ends however it ends.
        """
        from datetime import timedelta

        window = timedelta(seconds=limit.window_seconds)

        pool = await self._acquire_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                # hashtext gives a stable int4 for the bucket name. A
                # collision would serialise two unrelated buckets, which
                # costs a little throughput and breaks nothing.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", key
                )
                await connection.execute(
                    "DELETE FROM rate_limit_hits "
                    "WHERE bucket = $1 AND hit_at < now() - $2::interval",
                    key, window,
                )
                row = await connection.fetchrow(
                    "SELECT count(*) AS used, min(hit_at) AS oldest "
                    "FROM rate_limit_hits WHERE bucket = $1",
                    key,
                )
                used = int(row["used"] or 0)
                if used < limit.requests:
                    await connection.execute(
                        "INSERT INTO rate_limit_hits (bucket, hit_at) "
                        "VALUES ($1, now())",
                        key,
                    )
                    return Decision(
                        allowed=True,
                        remaining=max(0, limit.requests - used - 1),
                        limit=limit.requests,
                    )
                oldest = row["oldest"]

        retry_after = 0.0
        if oldest is not None:
            from datetime import datetime

            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            elapsed = (datetime.now(UTC) - oldest).total_seconds()
            retry_after = max(0.0, limit.window_seconds - elapsed)

        return Decision(
            allowed=False, remaining=0, retry_after=retry_after,
            limit=limit.requests,
        )


    def check(self, key: str, limit: RateLimit) -> Decision:
        """Synchronous shim, for callers that cannot await.

        The middleware uses `check_async`. This exists so the class still
        satisfies the RateLimiter protocol; it is not used on the request
        path, where opening a fresh event loop per request would be absurd.
        """
        import asyncio

        return asyncio.run(self.check_async(key, limit))

    def reset(self, key: str) -> None:  # pragma: no cover - administrative
        import asyncio

        async def clear():
            pool = await self._acquire_pool()
            async with pool.acquire() as connection:
                await connection.execute(
                    "DELETE FROM rate_limit_hits WHERE bucket = $1", key
                )

        asyncio.run(clear())


# Categories that need their own, stricter budget. A generous overall limit is
# right for ordinary reads and wrong for these: guessing credentials and
# creating executions are the two ways a caller turns request volume into
# damage, and revocation is an administrative action that should never arrive
# in a flood.
STRICT_CATEGORIES: dict[str, RateLimit] = {
    "auth_failure": RateLimit(requests=10, window_seconds=60),
    "token_admin": RateLimit(requests=20, window_seconds=60),
    "execution_create": RateLimit(requests=30, window_seconds=60),
}


def categorise(method: str, path: str) -> str:
    """Which budget a request draws on."""
    if "/tokens" in path:
        return "token_admin"
    if method == "POST" and path.rstrip("/").endswith("/executions"):
        return "execution_create"
    return "default"
