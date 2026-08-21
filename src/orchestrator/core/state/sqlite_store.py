"""SQLite-backed state store.

Chosen as the default local backend (ADR-004): zero install, crash-durable with
WAL, and good enough for single-node orchestration. The execution aggregate is
stored as one JSON document guarded by an integer revision, and audit events go
to an append-only table. Blocking sqlite calls are pushed to a worker thread so
the async engine is never stalled.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

from ...errors import NotFound
from ..domain.enums import ExecutionStatus
from ..domain.models import AuditEvent, Execution
from ..domain.serde import utcnow
from . import migrations as _migrations
from .store import ConcurrentModification, ExecutionSummary, StateStore

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id          TEXT PRIMARY KEY,
    objective   TEXT NOT NULL,
    status      TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    document    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_status
    ON executions(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    execution_id TEXT NOT NULL,
    sequence     INTEGER NOT NULL,
    id           TEXT NOT NULL,
    type         TEXT NOT NULL,
    task_id      TEXT,
    actor        TEXT,
    timestamp    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    PRIMARY KEY (execution_id, sequence)
);

CREATE TABLE IF NOT EXISTS idempotency (
    key        TEXT PRIMARY KEY,
    result     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

SCHEMA_VERSION = "1"

# What retention is allowed to delete, derived from the enum rather than
# retyped here.
#
# Stated as "only these may go" rather than "keep all of these". The two read
# as equivalent and are not: with a keep-list, a status added later is absent
# from it and gets deleted, so the failure mode of forgetting to update this
# file is silent data loss. Inverted, the same oversight merely keeps too much,
# which a later prune fixes.
def _prunable_statuses() -> tuple[str, ...]:
    from ..domain.enums import TERMINAL_EXECUTION_STATUSES

    return tuple(sorted(status.value for status in TERMINAL_EXECUTION_STATUSES))


PRUNABLE_STATUSES: tuple[str, ...] = _prunable_statuses()


class SQLiteStateStore(StateStore):
    """Durable local persistence. Safe for concurrent use within one process."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else Path(".orchestrator/state.db")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # The legacy schema is still created first, so an existing
            # database opened by this build is unchanged and a fresh one has
            # the same starting shape. The migration runner then brings both
            # to the current version and records what it applied.
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('version', ?)",
                (SCHEMA_VERSION,),
            )
            self._migrate_locked()

    # -- migrations --------------------------------------------------------

    def _applied_locked(self) -> list[int]:
        self._conn.execute(_migrations.BOOTSTRAP[_migrations.SQLITE])
        rows = self._conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [row[0] for row in rows]

    def _migrate_locked(self) -> list[str]:
        """Apply pending migrations. Idempotent; safe to call every open."""
        from datetime import datetime, timezone

        applied = self._applied_locked()
        _migrations.check_compatible(applied)

        performed: list[str] = []
        for migration in _migrations.pending(applied):
            try:
                for statement in migration.statements(_migrations.SQLITE):
                    self._conn.execute(statement)
            except sqlite3.OperationalError as exc:
                # A column this migration adds may already exist on a database
                # created by the legacy CREATE TABLE. That is the migration's
                # goal already met, not a failure — anything else is real.
                if "duplicate column" not in str(exc).lower():
                    raise _migrations.MigrationFailed(
                        f"migration {migration.version} ({migration.name}) "
                        f"failed: {exc}",
                        version=migration.version,
                        name=migration.name,
                    ) from exc
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (migration.version, migration.name,
                 datetime.now(timezone.utc).isoformat()),
            )
            performed.append(migration.name)
        self._conn.commit()
        return performed

    async def applied_migrations(self) -> list[int]:
        return await self._run(self._applied_locked)

    async def migrate(self) -> dict:
        applied = await self._run(self._migrate_locked)
        return {
            "applied": applied,
            "state": _migrations.describe(await self.applied_migrations()),
        }

    # -- internals ---------------------------------------------------------

    def _run(self, fn, *args):
        return asyncio.to_thread(self._locked, fn, *args)

    def _locked(self, fn, *args):
        with self._lock:
            return fn(*args)

    # -- executions --------------------------------------------------------

    def _create(self, execution: Execution) -> Execution:
        document = json.dumps(execution.to_dict(), separators=(",", ":"))
        try:
            self._conn.execute(
                "INSERT INTO executions"
                "(id, objective, status, revision, created_at, updated_at, document)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    execution.id,
                    execution.objective,
                    execution.status.value,
                    execution.revision,
                    execution.created_at.isoformat(),
                    execution.updated_at.isoformat(),
                    document,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConcurrentModification(execution.id, 0, execution.revision) from exc
        return execution

    async def create(self, execution: Execution) -> Execution:
        return await self._run(self._create, execution)

    def _get(self, execution_id: str) -> Execution:
        row = self._conn.execute(
            "SELECT document FROM executions WHERE id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"execution {execution_id} not found", id=execution_id)
        return Execution.from_dict(json.loads(row["document"]))

    async def get(self, execution_id: str) -> Execution:
        return await self._run(self._get, execution_id)

    def _save(self, execution: Execution, expected_revision: int | None) -> Execution:
        row = self._conn.execute(
            "SELECT revision FROM executions WHERE id = ?", (execution.id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"execution {execution.id} not found", id=execution.id)
        stored = int(row["revision"])
        if expected_revision is not None and stored != expected_revision:
            raise ConcurrentModification(execution.id, expected_revision, stored)

        execution.revision = stored + 1
        execution.updated_at = utcnow()
        document = json.dumps(execution.to_dict(), separators=(",", ":"))
        cursor = self._conn.execute(
            "UPDATE executions SET objective=?, status=?, revision=?, updated_at=?,"
            " document=? WHERE id=? AND revision=?",
            (
                execution.objective,
                execution.status.value,
                execution.revision,
                execution.updated_at.isoformat(),
                document,
                execution.id,
                stored,
            ),
        )
        if cursor.rowcount != 1:  # pragma: no cover - lost race under the lock
            raise ConcurrentModification(execution.id, stored, stored)
        return execution

    async def save(
        self, execution: Execution, *, expected_revision: int | None = None
    ) -> Execution:
        return await self._run(self._save, execution, expected_revision)

    def _list(
        self, status: ExecutionStatus | None, limit: int, offset: int
    ) -> list[ExecutionSummary]:
        sql = (
            "SELECT id, objective, status, revision, created_at, updated_at"
            " FROM executions"
        )
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status.value)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        import datetime as _dt

        return [
            ExecutionSummary(
                id=r["id"],
                objective=r["objective"],
                status=ExecutionStatus(r["status"]),
                created_at=_dt.datetime.fromisoformat(r["created_at"]),
                updated_at=_dt.datetime.fromisoformat(r["updated_at"]),
                revision=int(r["revision"]),
            )
            for r in rows
        ]

    async def list(
        self,
        *,
        status: ExecutionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionSummary]:
        return await self._run(self._list, status, limit, offset)

    def _delete(self, execution_id: str) -> None:
        self._conn.execute("DELETE FROM executions WHERE id = ?", (execution_id,))
        self._conn.execute(
            "DELETE FROM audit_events WHERE execution_id = ?", (execution_id,)
        )

    async def delete(self, execution_id: str) -> None:
        await self._run(self._delete, execution_id)

    # -- retention ---------------------------------------------------------

    def _prune(self, older_than_days: int, prunable: tuple[str, ...]) -> dict:
        """Delete finished executions older than a cutoff.

        Two invariants, because a retention job that loses work in progress is
        worse than a database that grows:

        * **Only terminal executions are eligible.** Anything still running,
          paused, or waiting on a person is kept regardless of age — age is
          not evidence that work is abandoned, and a run waiting three weeks
          for an approval is exactly the run nobody can afford to lose. Note
          that ``cancelling`` is not ``cancelled``: a run mid-transition is
          still in flight and is not eligible.
        * **An execution and its audit trail go together.** Deleting the
          record and orphaning the events would leave a trail that cannot be
          attributed to anything, which is worse than either keeping both or
          removing both.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat()

        # `placeholders` is a run of "?" characters and nothing else — one
        # per status — so the only thing interpolated is punctuation. Every
        # value is bound. Pinned by test_only_placeholders_are_interpolated.
        placeholders = ",".join("?" for _ in prunable)
        rows = self._conn.execute(
            f"SELECT id FROM executions "  # nosec B608 - placeholders only
            f"WHERE updated_at < ? AND status IN ({placeholders})",
            (cutoff, *prunable),
        ).fetchall()
        ids = [row[0] for row in rows]

        removed_events = 0
        for execution_id in ids:
            cursor = self._conn.execute(
                "DELETE FROM audit_events WHERE execution_id = ?", (execution_id,)
            )
            removed_events += cursor.rowcount or 0
            self._conn.execute(
                "DELETE FROM executions WHERE id = ?", (execution_id,)
            )

        # Idempotency keys are a replay guard with a short useful life; once
        # the execution they guarded is gone they protect nothing.
        cursor = self._conn.execute(
            "DELETE FROM idempotency WHERE created_at < ?", (cutoff,)
        )
        removed_keys = cursor.rowcount or 0
        self._conn.commit()

        return {
            "executions_removed": len(ids),
            "audit_events_removed": removed_events,
            "idempotency_keys_removed": removed_keys,
            "cutoff": cutoff,
            "ids": ids,
        }

    async def prune(
        self,
        *,
        older_than_days: int = 90,
        statuses: Sequence[str] = (),
    ) -> dict:
        """Remove finished executions older than ``older_than_days``.

        ``statuses`` narrows what is deleted; it can never widen it. A caller
        naming a non-terminal status is rejected rather than quietly ignored,
        because the difference between those two behaviours is somebody's
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
        return await self._run(self._prune, older_than_days, requested)

    async def vacuum(self) -> None:
        """Return freed pages to the filesystem after a prune."""
        await self._run(lambda: self._conn.execute("VACUUM"))

    # -- audit -------------------------------------------------------------

    def _append_audit(self, events: Sequence[AuditEvent]) -> None:
        if not events:
            return
        by_execution: dict[str, int] = {}
        rows = []
        for event in events:
            execution_id = event.execution_id
            if execution_id not in by_execution:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS s FROM audit_events"
                    " WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                by_execution[execution_id] = int(row["s"])
            by_execution[execution_id] += 1
            event.sequence = by_execution[execution_id]
            rows.append(
                (
                    execution_id,
                    event.sequence,
                    event.id,
                    event.type,
                    event.task_id,
                    event.actor,
                    event.timestamp.isoformat(),
                    json.dumps(event.payload, separators=(",", ":"), default=str),
                )
            )
        self._conn.executemany(
            "INSERT INTO audit_events"
            "(execution_id, sequence, id, type, task_id, actor, timestamp, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    async def append_audit(self, events: Sequence[AuditEvent]) -> None:
        await self._run(self._append_audit, list(events))

    def _audit(
        self, execution_id: str, after_sequence: int, limit: int
    ) -> list[AuditEvent]:
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE execution_id = ? AND sequence > ?"
            " ORDER BY sequence ASC LIMIT ?",
            (execution_id, after_sequence, limit),
        ).fetchall()
        return [
            AuditEvent.from_dict(
                {
                    "id": r["id"],
                    "execution_id": r["execution_id"],
                    "sequence": r["sequence"],
                    "type": r["type"],
                    "task_id": r["task_id"],
                    "actor": r["actor"],
                    "timestamp": r["timestamp"],
                    "payload": json.loads(r["payload"]),
                }
            )
            for r in rows
        ]

    async def audit(
        self, execution_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[AuditEvent]:
        return await self._run(self._audit, execution_id, after_sequence, limit)

    # -- idempotency -------------------------------------------------------

    def _record_operation(self, key: str, result: Any) -> tuple[bool, Any]:
        row = self._conn.execute(
            "SELECT result FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            return False, json.loads(row["result"])
        self._conn.execute(
            "INSERT INTO idempotency(key, result, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(result, default=str), utcnow().isoformat()),
        )
        return True, result

    async def record_operation(self, key: str, result: Any) -> tuple[bool, Any]:
        """Register a side-effecting operation.

        Returns ``(is_new, stored_result)``. A replayed key returns the original
        result so retries do not duplicate external effects (spec section 67).
        """
        return await self._run(self._record_operation, key, result)

    def _lookup_operation(self, key: str) -> Any | None:
        row = self._conn.execute(
            "SELECT result FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else json.loads(row["result"])

    async def lookup_operation(self, key: str) -> Any | None:
        return await self._run(self._lookup_operation, key)

    async def close(self) -> None:
        await self._run(self._conn.close)
