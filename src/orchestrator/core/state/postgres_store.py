"""PostgreSQL state backend.

The reason this exists: SQLite is single-node. Two orchestrator processes
against one database file corrupt state or deadlock, which capped deployment
at a single instance regardless of how stateless the rest of the platform is.

Three properties the engine depends on, and how each is preserved here:

* **Optimistic concurrency.** ``UPDATE ... WHERE id = $1 AND revision = $2``
  and check the row count. No ``SELECT FOR UPDATE``: the revision *is* the
  concurrency control, it is the same one SQLite uses, and holding a row lock
  across an async round-trip is how a busy instance stalls every other one.
* **Append-only audit ordering.** ``(execution_id, sequence)`` is the primary
  key, so a duplicate sequence is a constraint violation rather than a
  silently reordered trail.
* **Atomic execution+audit updates.** An execution and its audit events are
  written in one transaction. A trail that survives its execution is a record
  attributable to nothing.

Everything is parameterised — ``$1``-style placeholders, never string
interpolation — including the retention query, which is the one place a
status list is built dynamically.

The driver is asyncpg, an optional dependency. Importing this module without
it raises a message naming the extra to install rather than an ImportError
from three frames down.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from ...errors import ConfigurationError, NotFound
from ..domain.enums import TERMINAL_EXECUTION_STATUSES, ExecutionStatus
from ..domain.models import AuditEvent, Execution
from . import migrations as _migrations
from .store import ConcurrentModification, ExecutionSummary, StateStore

# Only terminal executions may be deleted by age. Derived from the enum rather
# than retyped, so a status added later defaults to protected.
PRUNABLE_STATUSES: tuple[str, ...] = tuple(
    sorted(status.value for status in TERMINAL_EXECUTION_STATUSES)
)

ENV_DSN = "ORCHESTRATOR_POSTGRES_DSN"  # noqa: S105 - a variable name, not a secret

# Resolved here, at module level, where `list` still means the builtin.
# Inside the class body it names the `list` method that StateStore requires.
_SummaryList = list[ExecutionSummary]
_AuditList = list[AuditEvent]
_IntList = list[int]

# Guards schema migration so two instances starting together do not both
# try to apply the same step. Any stable integer works; this one is
# arbitrary and only has to not collide with another advisory lock.
_SCHEMA_LOCK_KEY = 776_452_101


def _require_asyncpg():
    try:
        import asyncpg  # noqa: F401

        return asyncpg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ConfigurationError(
            "the PostgreSQL backend requires asyncpg; install "
            "universal-orchestrator[postgres]",
            remedy="pip install 'universal-orchestrator[postgres]'",
        ) from exc


def resolve_dsn(config: dict[str, Any] | None = None) -> str:
    """Read the DSN from the environment variable the config names.

    A DSN carries a password. The config holds the *name* of the variable, so
    a config file can be committed without thought — the same rule the model
    providers already follow.
    """
    config = config or {}
    variable = str(config.get("dsn_env") or ENV_DSN).strip()

    if "dsn" in config or "url" in config:
        raise ConfigurationError(
            "storage.postgres.dsn must not appear in configuration: a DSN "
            "carries a password. Use storage.postgres.dsn_env to name an "
            "environment variable instead.",
            remedy=f"set {variable} in the environment",
        )

    dsn = os.environ.get(variable, "").strip()
    if not dsn:
        raise ConfigurationError(
            f"the PostgreSQL backend is selected but {variable} is not set. "
            f"Set it to a connection string such as "
            f"postgresql://user:password@host:5432/orchestrator",
            remedy=f"set {variable}",
        )
    return dsn


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


class PostgresStateStore(StateStore):
    """Multi-instance state, on PostgreSQL.

    Construct with :meth:`connect`, which opens the pool and runs migrations.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        apply_migrations: bool = True,
        command_timeout: float = 30.0,
    ) -> PostgresStateStore:
        asyncpg = _require_asyncpg()
        pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=command_timeout,
        )
        store = cls(pool)
        if apply_migrations:
            await store.migrate()
        else:
            await store.check_schema()
        return store

    async def close(self) -> None:
        await self._pool.close()

    # -- migrations --------------------------------------------------------

    async def applied_migrations(self) -> _IntList:
        async with self._pool.acquire() as connection:
            await connection.execute(_migrations.BOOTSTRAP[_migrations.POSTGRES])
            rows = await connection.fetch(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        return [row["version"] for row in rows]

    async def check_schema(self) -> None:
        """Refuse a database newer than this build understands."""
        _migrations.check_compatible(await self.applied_migrations())

    async def migrate(self) -> dict[str, Any]:
        """Apply pending migrations. Idempotent.

        Each migration runs in its own transaction with an advisory lock held,
        so two instances starting at once do not both try to add the same
        column — one applies it and the other finds nothing pending.
        """
        applied = await self.applied_migrations()
        _migrations.check_compatible(applied)

        performed: list[str] = []
        async with self._pool.acquire() as connection:
            # Guards the schema, not a row, so any stable integer will do.
            await connection.execute("SELECT pg_advisory_lock($1)", _SCHEMA_LOCK_KEY)
            try:
                rows = await connection.fetch(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
                done = {row["version"] for row in rows}
                for migration in _migrations.MIGRATIONS:
                    if migration.version in done:
                        continue
                    async with connection.transaction():
                        for statement in migration.statements(_migrations.POSTGRES):
                            await connection.execute(statement)
                        await connection.execute(
                            "INSERT INTO schema_migrations (version, name, applied_at) "
                            "VALUES ($1, $2, now()) ON CONFLICT (version) DO NOTHING",
                            migration.version,
                            migration.name,
                        )
                    performed.append(migration.name)
            finally:
                await connection.execute("SELECT pg_advisory_unlock($1)", _SCHEMA_LOCK_KEY)

        return {
            "applied": performed,
            "state": _migrations.describe(await self.applied_migrations()),
        }

    # -- executions --------------------------------------------------------

    async def create(self, execution: Execution) -> Execution:
        document = json.dumps(execution.to_dict(), default=str)
        tenant = str((execution.context or {}).get("__tenant") or "default")
        async with self._pool.acquire() as connection:
            inserted = await connection.execute(
                "INSERT INTO executions "
                "(id, objective, status, revision, created_at, updated_at, "
                " document, tenant) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8) "
                "ON CONFLICT (id) DO NOTHING",
                execution.id,
                execution.objective,
                execution.status.value,
                execution.revision,
                _as_datetime(execution.created_at),
                _as_datetime(execution.updated_at),
                document,
                tenant,
            )
        if inserted.endswith("0"):
            raise ConfigurationError(
                f"execution {execution.id} already exists",
                execution_id=execution.id,
            )
        return execution

    async def get(self, execution_id: str) -> Execution:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT document FROM executions WHERE id = $1", execution_id
            )
        if row is None:
            raise NotFound(f"execution {execution_id} was not found")
        return Execution.from_dict(json.loads(row["document"]))

    async def save(
        self, execution: Execution, expected_revision: int | None = None
    ) -> Execution:
        """Save under an optimistic-concurrency check.

        The UPDATE carries the expected revision in its WHERE clause, so a
        concurrent writer loses rather than silently overwriting. Zero rows
        affected means either the row is gone or somebody else got there
        first, and the follow-up query distinguishes them.
        """
        expected = execution.revision if expected_revision is None else expected_revision
        execution.revision = expected + 1
        execution.updated_at = datetime.now(UTC)
        document = json.dumps(execution.to_dict(), default=str)
        tenant = str((execution.context or {}).get("__tenant") or "default")

        async with self._pool.acquire() as connection:
            result = await connection.execute(
                "UPDATE executions SET objective = $1, status = $2, "
                "revision = $3, updated_at = $4, document = $5::jsonb, "
                "tenant = $6 "
                "WHERE id = $7 AND revision = $8",
                execution.objective,
                execution.status.value,
                execution.revision,
                execution.updated_at,
                document,
                tenant,
                execution.id,
                expected,
            )
            if result.endswith(" 0"):
                actual = await connection.fetchval(
                    "SELECT revision FROM executions WHERE id = $1", execution.id
                )
                if actual is None:
                    raise NotFound(f"execution {execution.id} was not found")
                execution.revision = expected
                raise ConcurrentModification(execution.id, expected, actual)
        return execution

    async def list(
        self,
        *,
        status: ExecutionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        tenant: str | None = None,
    ) -> _SummaryList:
        """Summaries, newest first, filtered in the database.

        ``tenant`` is a real WHERE clause here rather than a Python filter
        over an over-fetched page, so pagination stays correct: filtering
        after LIMIT returns short pages and eventually skips rows.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            params.append(status.value)
            clauses.append(f"status = ${len(params)}")
        if tenant is not None:
            params.append(tenant)
            clauses.append(f"tenant = ${len(params)}")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])

        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                # noqa justified: `where` is built from fixed fragments and
                # placeholder numbers only; every value travels in *params.
                # Proved by test_no_caller_value_is_ever_interpolated_into_sql.
                f"SELECT id, objective, status, created_at, updated_at, revision "  # noqa: S608
                f"FROM executions {where} "
                f"ORDER BY updated_at DESC, id DESC "
                f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                *params,
            )

        return [
            ExecutionSummary(
                id=row["id"],
                objective=row["objective"],
                status=ExecutionStatus(row["status"]),
                created_at=_as_datetime(row["created_at"]),
                updated_at=_as_datetime(row["updated_at"]),
                revision=row["revision"],
            )
            for row in rows
        ]

    async def delete(self, execution_id: str) -> None:
        """Delete an execution and its trail together, in one transaction."""
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM audit_events WHERE execution_id = $1", execution_id
                )
                await connection.execute(
                    "DELETE FROM executions WHERE id = $1", execution_id
                )

    # -- audit -------------------------------------------------------------

    async def append_audit(self, events: Sequence[AuditEvent]) -> None:
        if not events:
            return
        rows = [
            (
                event.execution_id,
                event.sequence,
                event.id,
                event.type.value if hasattr(event.type, "value") else str(event.type),
                event.task_id,
                event.actor,
                _as_datetime(event.timestamp),
                json.dumps(event.payload, default=str),
            )
            for event in events
        ]
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.executemany(
                    "INSERT INTO audit_events "
                    "(execution_id, sequence, id, type, task_id, actor, "
                    " timestamp, payload) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb) "
                    "ON CONFLICT (execution_id, sequence) DO NOTHING",
                    rows,
                )

    async def audit(
        self, execution_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> _AuditList:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT execution_id, sequence, id, type, task_id, actor, "
                "       timestamp, payload "
                "FROM audit_events WHERE execution_id = $1 AND sequence > $2 "
                "ORDER BY sequence LIMIT $3",
                execution_id,
                after_sequence,
                limit,
            )
        return [
            AuditEvent(
                id=row["id"],
                execution_id=row["execution_id"],
                sequence=row["sequence"],
                type=row["type"],
                task_id=row["task_id"],
                actor=row["actor"],
                timestamp=_as_datetime(row["timestamp"]),
                payload=json.loads(row["payload"]) if row["payload"] else {},
            )
            for row in rows
        ]

    # -- idempotency -------------------------------------------------------

    async def record_operation(self, key: str, result: Any) -> tuple[bool, Any]:
        """Record an operation, returning whether this call was the first.

        ``ON CONFLICT DO NOTHING`` plus a RETURNING clause makes the check and
        the write one statement, so two instances racing on the same key
        cannot both believe they were first.
        """
        payload = json.dumps(result, default=str)
        async with self._pool.acquire() as connection:
            inserted = await connection.fetchval(
                "INSERT INTO idempotency (key, result, created_at) "
                "VALUES ($1, $2::jsonb, now()) "
                "ON CONFLICT (key) DO NOTHING RETURNING key",
                key,
                payload,
            )
            if inserted is not None:
                return True, result
            existing = await connection.fetchval(
                "SELECT result FROM idempotency WHERE key = $1", key
            )
        return False, json.loads(existing) if existing else None

    async def lookup_operation(self, key: str) -> Any | None:
        async with self._pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT result FROM idempotency WHERE key = $1", key
            )
        return json.loads(value) if value else None

    # -- retention ---------------------------------------------------------

    async def prune(
        self,
        *,
        older_than_days: int = 90,
        statuses: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Delete finished executions older than a cutoff.

        Same two invariants as the SQLite backend, for the same reasons: only
        terminal executions are eligible, and an execution and its trail go
        together. A caller naming a non-terminal status is refused rather than
        quietly ignored — the difference between those is somebody's
        in-flight work.
        """
        if older_than_days < 1:
            raise ValueError("older_than_days must be at least 1")

        requested = tuple(statuses) or PRUNABLE_STATUSES
        unsafe = sorted(set(requested) - set(PRUNABLE_STATUSES))
        if unsafe:
            raise ValueError(
                f"refusing to prune non-terminal status(es): {', '.join(unsafe)}. "
                f"Only {', '.join(PRUNABLE_STATUSES)} may be deleted by age; "
                f"anything else is still in flight."
            )

        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                ids = [
                    row["id"]
                    for row in await connection.fetch(
                        "SELECT id FROM executions "
                        "WHERE updated_at < $1 AND status = ANY($2::text[])",
                        cutoff,
                        list(requested),
                    )
                ]
                events = 0
                if ids:
                    deleted = await connection.execute(
                        "DELETE FROM audit_events WHERE execution_id = ANY($1::text[])",
                        ids,
                    )
                    events = int(deleted.rsplit(" ", 1)[-1] or 0)
                    await connection.execute(
                        "DELETE FROM executions WHERE id = ANY($1::text[])", ids
                    )
                keys = await connection.execute(
                    "DELETE FROM idempotency WHERE created_at < $1", cutoff
                )

        return {
            "executions_removed": len(ids),
            "audit_events_removed": events,
            "idempotency_keys_removed": int(keys.rsplit(" ", 1)[-1] or 0),
            "cutoff": cutoff.isoformat(),
            "ids": ids,
        }

    async def vacuum(self) -> None:
        """No-op: PostgreSQL autovacuum handles this.

        Present so callers do not have to know which backend they hold. A
        manual VACUUM FULL takes an exclusive lock and is not something a
        retention job should do behind an operator's back.
        """
        return None
