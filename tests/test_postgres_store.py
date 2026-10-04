"""PostgreSQL backend.

Split in two on purpose:

* Tests that need no database — DSN handling, the migration plan, retention
  safety rules — run everywhere and are the ones that catch most mistakes.
* Tests that need a real server are skipped cleanly unless
  ``ORCHESTRATOR_TEST_POSTGRES_DSN`` is set. CI supplies it from a service
  container; a laptop without PostgreSQL still gets a green suite rather than
  a wall of errors that trains people to ignore failures.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from datetime import UTC

from orchestrator.core.domain.models import Execution
from orchestrator.core.state import migrations as m
from orchestrator.core.state.store import ConcurrentModification, StateStore
from orchestrator.errors import ConfigurationError, NotFound

TEST_DSN_ENV = "ORCHESTRATOR_TEST_POSTGRES_DSN"
DSN = os.environ.get(TEST_DSN_ENV, "").strip()

requires_postgres = pytest.mark.skipif(
    not DSN,
    reason=f"set {TEST_DSN_ENV} to run PostgreSQL integration tests",
)


# ==========================================================================
# No database required
# ==========================================================================


def test_the_store_implements_the_state_store_contract():
    from orchestrator.core.state.postgres_store import PostgresStateStore

    assert issubclass(PostgresStateStore, StateStore)
    for method in (
        "create",
        "get",
        "save",
        "list",
        "delete",
        "append_audit",
        "audit",
        "record_operation",
        "lookup_operation",
        "prune",
        "close",
    ):
        assert hasattr(PostgresStateStore, method), method


def test_a_dsn_in_configuration_is_refused():
    """A DSN carries a password; a config file gets committed."""
    from orchestrator.core.state.postgres_store import resolve_dsn

    with pytest.raises(ConfigurationError) as exc:
        resolve_dsn({"dsn": "postgresql://user:secret@host/db"})
    assert "dsn_env" in str(exc.value)

    with pytest.raises(ConfigurationError):
        resolve_dsn({"url": "postgresql://user:secret@host/db"})


def test_the_dsn_comes_from_the_named_environment_variable(monkeypatch):
    from orchestrator.core.state.postgres_store import resolve_dsn

    monkeypatch.setenv("MY_DB_URL", "postgresql://localhost/x")
    assert resolve_dsn({"dsn_env": "MY_DB_URL"}) == "postgresql://localhost/x"


def test_a_missing_dsn_variable_says_which_one_to_set(monkeypatch):
    from orchestrator.core.state.postgres_store import resolve_dsn

    monkeypatch.delenv("MY_DB_URL", raising=False)
    with pytest.raises(ConfigurationError) as exc:
        resolve_dsn({"dsn_env": "MY_DB_URL"})
    assert "MY_DB_URL" in str(exc.value)


def test_prunable_statuses_are_derived_from_the_enum():
    from orchestrator.core.domain.enums import TERMINAL_EXECUTION_STATUSES
    from orchestrator.core.state.postgres_store import PRUNABLE_STATUSES

    assert set(PRUNABLE_STATUSES) == {s.value for s in TERMINAL_EXECUTION_STATUSES}


def test_the_backend_is_selectable_by_configuration():
    from orchestrator.config.loader import Config
    from orchestrator.platform import _build_store

    with pytest.raises(ConfigurationError) as exc:
        _build_store(Config({"profile": "development", "storage": {"backend": "postgres"}}))
    assert "asynchronously" in str(exc.value)

    with pytest.raises(ConfigurationError) as exc:
        _build_store(Config({"profile": "development", "storage": {"backend": "mysql"}}))
    assert "sqlite" in str(exc.value) and "postgres" in str(exc.value)


# --------------------------------------------------------------------------
# Migration plan
# --------------------------------------------------------------------------


def test_every_migration_has_statements_for_both_dialects():
    """One schema change, two renderings — not two drifting sequences."""
    for migration in m.MIGRATIONS:
        assert migration.sqlite, f"{migration.name} has no sqlite statements"
        assert migration.postgres, f"{migration.name} has no postgres statements"


def test_migration_versions_are_unique_and_ordered():
    versions = [x.version for x in m.MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert m.TARGET_VERSION == max(versions)


def test_pending_reflects_what_has_been_applied():
    assert [x.name for x in m.pending([])] == [x.name for x in m.MIGRATIONS]
    assert m.pending([x.version for x in m.MIGRATIONS]) == []
    # Every version after the first, whatever the current count is.
    assert [x.version for x in m.pending([1])] == [
        x.version for x in m.MIGRATIONS if x.version != 1
    ]


def test_a_database_newer_than_the_code_is_refused():
    """An old process writing a new schema is a subtle 3am failure."""
    with pytest.raises(m.SchemaTooNew) as exc:
        m.check_compatible([m.TARGET_VERSION + 1])
    assert str(m.TARGET_VERSION) in str(exc.value)

    # An older or current database is fine.
    m.check_compatible([])
    m.check_compatible([1])
    m.check_compatible([x.version for x in m.MIGRATIONS])


def test_describe_reports_the_state_a_cli_needs():
    state = m.describe([1])
    assert state["current_version"] == 1
    assert state["target_version"] == m.TARGET_VERSION
    assert state["up_to_date"] is False
    assert "tenant_column" in state["pending"]

    complete = m.describe([x.version for x in m.MIGRATIONS])
    assert complete["up_to_date"] is True
    assert complete["pending"] == []


# --------------------------------------------------------------------------
# SQLite runs the same migrations
# --------------------------------------------------------------------------


def test_sqlite_applies_every_migration_and_is_idempotent(tmp_path):
    from orchestrator.core.state.sqlite_store import SQLiteStateStore

    store = SQLiteStateStore(tmp_path / "state.db")
    try:
        assert asyncio.run(store.applied_migrations()) == [x.version for x in m.MIGRATIONS]
        # Applying again does nothing.
        assert asyncio.run(store.migrate())["applied"] == []

        columns = [r[1] for r in store._conn.execute("PRAGMA table_info(executions)")]
        assert "tenant" in columns
    finally:
        asyncio.run(store.close())


def test_reopening_an_existing_sqlite_database_does_not_re_migrate(tmp_path):
    from orchestrator.core.state.sqlite_store import SQLiteStateStore

    path = tmp_path / "state.db"
    first = SQLiteStateStore(path)
    asyncio.run(first.close())

    second = SQLiteStateStore(path)
    try:
        assert asyncio.run(second.migrate())["applied"] == []
    finally:
        asyncio.run(second.close())


# ==========================================================================
# Requires a real PostgreSQL server
# ==========================================================================


# An asyncpg pool is bound to the event loop that created it. The previous
# structure opened the pool in a fixture with its own `asyncio.run`, then ran
# each test body in a *second* `asyncio.run` — so every operation after the
# first hit a pool attached to a closed loop. The unit-level tests could not
# see this because they never opened a pool.
#
# So the pool and the body share one loop: `with_store` runs connect, the test
# body, and close inside a single `asyncio.run`.


def with_store(body):
    """Run ``body(store)`` against a freshly truncated database.

    One `asyncio.run` for connect, work, and close — see above.
    """
    from orchestrator.core.state.postgres_store import PostgresStateStore

    async def main():
        store = await PostgresStateStore.connect(DSN)
        try:
            async with store._pool.acquire() as connection:
                await connection.execute("TRUNCATE audit_events, executions, idempotency")
            return await body(store)
        finally:
            await store.close()

    return asyncio.run(main())


def _execution(objective="postgres work", **context):
    execution = Execution(objective=objective)
    if context:
        execution.context = dict(context)
    return execution


@requires_postgres
def test_an_execution_round_trips():
    async def body(store):
        created = await store.create(_execution())
        loaded = await store.get(created.id)
        assert loaded.id == created.id
        assert loaded.objective == "postgres work"

    with_store(body)


@requires_postgres
def test_a_missing_execution_raises_not_found():
    async def body(store):
        with pytest.raises(NotFound):
            await store.get("exe_does_not_exist")

    with_store(body)


@requires_postgres
def test_optimistic_concurrency_rejects_a_stale_write():
    async def body(store):
        execution = await store.create(_execution())

        first = await store.get(execution.id)
        second = await store.get(execution.id)

        first.objective = "updated by the first writer"
        await store.save(first)

        second.objective = "updated by the second writer"
        with pytest.raises(ConcurrentModification):
            await store.save(second)

        # The first writer's value survives.
        current = await store.get(execution.id)
        assert current.objective.startswith("updated by the first")

    with_store(body)


@requires_postgres
def test_two_concurrent_writers_produce_exactly_one_winner():
    """The multi-instance property, exercised rather than asserted."""

    async def body(store):
        execution = await store.create(_execution())

        a = await store.get(execution.id)
        b = await store.get(execution.id)
        a.objective = "writer a"
        b.objective = "writer b"

        results = await asyncio.gather(store.save(a), store.save(b), return_exceptions=True)
        failures = [r for r in results if isinstance(r, ConcurrentModification)]
        successes = [r for r in results if not isinstance(r, Exception)]
        assert len(successes) == 1, results
        assert len(failures) == 1, results

    with_store(body)


@requires_postgres
def test_the_audit_trail_stays_ordered_and_appends_only():
    from orchestrator.core.domain.models import AuditEvent

    async def body(store):
        execution = await store.create(_execution())
        events = [
            AuditEvent(
                execution_id=execution.id,
                sequence=n,
                type="execution.created",
                payload={"n": n},
            )
            for n in range(1, 6)
        ]
        await store.append_audit(events)

        read = await store.audit(execution.id)
        assert [e.sequence for e in read] == [1, 2, 3, 4, 5]

        # Re-appending the same sequences does not duplicate or reorder.
        await store.append_audit(events)
        assert len(await store.audit(execution.id)) == 5

    with_store(body)


@requires_postgres
def test_deleting_an_execution_takes_its_audit_trail_with_it():
    from orchestrator.core.domain.models import AuditEvent

    async def body(store):
        execution = await store.create(_execution())
        await store.append_audit(
            [
                AuditEvent(
                    execution_id=execution.id,
                    sequence=1,
                    type="execution.created",
                    payload={},
                )
            ]
        )
        await store.delete(execution.id)
        assert await store.audit(execution.id) == []

    with_store(body)


@requires_postgres
def test_idempotency_records_only_one_winner():
    async def body(store):
        first, _value = await store.record_operation("k1", {"v": 1})
        assert first is True

        again, existing = await store.record_operation("k1", {"v": 2})
        assert again is False
        assert existing == {"v": 1}
        assert await store.lookup_operation("k1") == {"v": 1}

    with_store(body)


@requires_postgres
def test_listing_filters_by_tenant_in_the_database():
    async def body(store):
        for tenant in ("acme", "acme", "globex"):
            await store.create(_execution(f"{tenant} work", __tenant=tenant))

        acme = await store.list(tenant="acme")
        assert len(acme) == 2
        globex = await store.list(tenant="globex")
        assert len(globex) == 1
        assert len(await store.list()) == 3

    with_store(body)


@requires_postgres
def test_pagination_is_correct_under_a_tenant_filter():
    """Filtering after LIMIT returns short pages and eventually skips rows."""

    async def body(store):
        for index in range(10):
            await store.create(_execution(f"acme {index}", __tenant="acme"))
            await store.create(_execution(f"globex {index}", __tenant="globex"))

        seen = []
        for offset in (0, 4, 8):
            page = await store.list(tenant="acme", limit=4, offset=offset)
            seen.extend(p.id for p in page)

        assert len(seen) == 10
        assert len(set(seen)) == 10, "pagination returned a row twice"

    with_store(body)


@requires_postgres
def test_retention_deletes_terminal_work_and_keeps_the_rest():
    from datetime import datetime, timedelta

    async def body(store):
        old = datetime.now(UTC) - timedelta(days=200)
        async with store._pool.acquire() as connection:
            for index, status in enumerate(
                ("completed", "failed", "cancelled", "running", "waiting", "cancelling")
            ):
                await connection.execute(
                    "INSERT INTO executions (id, objective, status, revision, "
                    "created_at, updated_at, document, tenant) "
                    "VALUES ($1,$2,$3,1,$4,$4,'{}'::jsonb,'default')",
                    f"exe_{index}_{status}",
                    "seeded",
                    status,
                    old,
                )

        report = await store.prune(older_than_days=90)
        assert report["executions_removed"] == 3

        remaining = {row.id.rsplit("_", 1)[-1] for row in await store.list()}
        assert remaining == {"running", "waiting", "cancelling"}

    with_store(body)


@requires_postgres
def test_retention_refuses_a_non_terminal_status():
    async def body(store):
        with pytest.raises(ValueError, match="non-terminal"):
            await store.prune(older_than_days=1, statuses=("running",))

    with_store(body)


@requires_postgres
def test_migrations_are_idempotent_against_a_real_database():
    async def body(store):
        assert (await store.migrate())["applied"] == []
        applied = await store.applied_migrations()
        assert applied == [x.version for x in m.MIGRATIONS]

    with_store(body)


@requires_postgres
def test_the_real_schema_has_the_columns_and_indexes_migrations_declare():
    """Migrations that run without creating what they claim are worse than none."""

    async def body(store):
        async with store._pool.acquire() as connection:
            columns = {
                row["column_name"]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'executions'"
                )
            }
            assert {
                "id",
                "objective",
                "status",
                "revision",
                "created_at",
                "updated_at",
                "document",
                "tenant",
            } <= columns

            indexes = {
                row["indexname"]
                for row in await connection.fetch(
                    "SELECT indexname FROM pg_indexes WHERE tablename IN "
                    "('executions', 'idempotency')"
                )
            }
            assert "idx_executions_tenant" in indexes
            assert "idx_executions_updated" in indexes
            assert "idx_idempotency_created" in indexes

    with_store(body)


@requires_postgres
def test_state_survives_a_reconnect():
    """Restart recovery: a new pool sees what the old one wrote."""
    from orchestrator.core.state.postgres_store import PostgresStateStore

    async def main():
        first = await PostgresStateStore.connect(DSN)
        try:
            async with first._pool.acquire() as connection:
                await connection.execute("TRUNCATE audit_events, executions, idempotency")
            execution = await first.create(_execution("survives a restart"))
        finally:
            await first.close()

        second = await PostgresStateStore.connect(DSN)
        try:
            return await second.get(execution.id)
        finally:
            await second.close()

    assert asyncio.run(main()).objective == "survives a restart"


@requires_postgres
def test_a_tenant_name_containing_sql_is_treated_as_data():
    async def body(store):
        malicious = "acme'; DROP TABLE executions; --"
        await store.create(_execution("legitimate", __tenant="acme"))

        # If this were interpolated, the table would be gone and this would raise.
        results = await store.list(tenant=malicious)
        assert results == []

        # The table is intact and the real row is still there.
        assert len(await store.list(tenant="acme")) == 1

    with_store(body)


@requires_postgres
def test_an_orchestrator_starts_on_postgres_and_runs_an_execution():
    """The integration the store's own tests cannot prove: real startup.

    Goes through `Orchestrator.create()` with a config that `load()` validated,
    so it covers the enum, the block validator, the async store factory, and
    the store itself in one path.
    """
    import os
    import tempfile

    from conftest import planning_model

    from orchestrator.config.loader import load
    from orchestrator.platform import Orchestrator

    directory = tempfile.mkdtemp()
    config_path = os.path.join(directory, "config.yaml")
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write(
            "version: 1\n"
            "profile: development\n"
            "storage:\n"
            "  backend: postgres\n"
            "  postgres:\n"
            "    dsn_env: ORCHESTRATOR_TEST_POSTGRES_DSN\n"
            "plugins:\n"
            "  enabled: false\n"
            "workflows:\n"
            "  directories: []\n"
        )

    config = load(paths=[config_path], include_discovered=False)
    assert config.get("storage.backend") == "postgres"

    async def main():
        orchestrator = await Orchestrator.create(
            config=config, connect_mcp=False, providers=[planning_model()]
        )
        try:
            from orchestrator.core.state.postgres_store import PostgresStateStore

            assert isinstance(orchestrator.store, PostgresStateStore), (
                f"startup chose {type(orchestrator.store).__name__}"
            )
            execution = await orchestrator.run("run against postgres")
            reloaded = await orchestrator.status(execution.id)
            return execution, reloaded
        finally:
            await orchestrator.close()

    execution, reloaded = asyncio.run(main())
    assert execution.status.value == "completed", execution.status
    # Durable: read back from the database, not from memory.
    assert reloaded.id == execution.id
    assert reloaded.objective == "run against postgres"


@requires_postgres
def test_no_sqlite_file_is_created_when_the_backend_is_postgres(tmp_path):
    """A silent fallback to SQLite is the failure mode worth catching."""
    import os

    from conftest import planning_model

    from orchestrator.config.loader import load
    from orchestrator.platform import Orchestrator

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "version: 1\n"
        "profile: development\n"
        "storage:\n"
        "  backend: postgres\n"
        "  postgres:\n"
        "    dsn_env: ORCHESTRATOR_TEST_POSTGRES_DSN\n"
        "plugins:\n"
        "  enabled: false\n"
        "workflows:\n"
        "  directories: []\n",
        encoding="utf-8",
    )

    config = load(paths=[str(config_path)], include_discovered=False)

    async def main():
        orchestrator = await Orchestrator.create(
            config=config,
            connect_mcp=False,
            workspace=str(tmp_path),
            providers=[planning_model()],
        )
        try:
            await orchestrator.run("must not touch sqlite")
        finally:
            await orchestrator.close()

    previous = os.getcwd()
    os.chdir(tmp_path)
    try:
        asyncio.run(main())
    finally:
        os.chdir(previous)

    stray = list(tmp_path.rglob("*.db")) + list(tmp_path.rglob("*.sqlite*"))
    assert not stray, f"a SQLite database was created: {stray}"


# --------------------------------------------------------------------------
# SQL construction
# --------------------------------------------------------------------------


def test_only_placeholders_are_interpolated_into_sqlite_sql():
    """The SQLite retention query builds its IN-list dynamically.

    Bandit flags it, correctly, as string-built SQL. What makes it safe is
    that the interpolated value is a run of "?" and commas — never a status
    name. This asserts that, so a future edit that interpolates a value fails
    here rather than in a scanner nobody reads.
    """
    import inspect
    import re

    from orchestrator.core.state.sqlite_store import SQLiteStateStore

    source = inspect.getsource(SQLiteStateStore._prune)
    match = re.search(r"placeholders = ([^\n]+)", source)
    assert match, "expected the placeholder list to be built explicitly"
    built = match.group(1)
    # It joins "?" — not a status, not a caller value.
    assert '"?"' in built, built
    assert "prunable" in built and "status" not in built.replace("statuses", "")

    # And the values are passed as bound parameters.
    assert "(cutoff, *prunable)" in source
