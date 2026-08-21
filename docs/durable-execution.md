# Durable execution and storage

## What is durable today

State is a single SQLite database: executions (as JSON documents), the audit
trail, and idempotency keys. Writes are revision-checked, so a concurrent save
against a stale revision is rejected rather than silently overwriting.

Durable across a restart: execution state, task results, audit trail, pending
approvals, artifacts.

## SQLite is single-node. PostgreSQL is not.

**SQLite is not a multi-instance production backend.** Two orchestrator
processes against one database file — on a shared volume, NFS, or EFS — will
corrupt state or deadlock. File locking over a network filesystem does not
provide the guarantees SQLite assumes. This has not changed and will not.

For more than one instance, use PostgreSQL:

```yaml
storage:
  backend: postgres
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN   # the NAME of a variable
    min_connections: 1
    max_connections: 10
```

The DSN carries a password, so it never appears in configuration —
`storage.postgres.dsn` is rejected outright with a message saying why.

`PostgresStateStore` preserves every property the engine depends on:
optimistic concurrency via `UPDATE ... WHERE revision = $n`, append-only audit
ordering via a `(execution_id, sequence)` primary key, atomic
execution-plus-audit deletion, and the same retention safety rules. Concurrent
writers are exercised in `tests/test_postgres_store.py`, which asserts exactly
one winner.

## The seam for a networked backend

`orchestrator/core/state/store.py` defines `StateStore`:

| Method | Contract |
|---|---|
| `create(execution)` | Insert. Fails if the id exists. |
| `get(id)` | Fetch, or raise `NotFound`. |
| `save(execution, expected_revision)` | Optimistic concurrency. Raises on mismatch. |
| `list(status, limit, offset)` | Summaries, newest first. |
| `delete(id)` | Remove the execution and its audit events together. |
| `append_audit(events)` | Append-only, ordered by sequence per execution. |
| `audit(id, after)` | Read the trail. |
| `record_operation` / `lookup_operation` | Idempotency keys. |
| `prune(older_than_days, statuses)` | Retention. Terminal statuses only. |

A PostgreSQL implementation needs: `executions` with a `revision` column for
optimistic concurrency (`UPDATE … WHERE revision = ?`), `audit_events` with a
`(execution_id, sequence)` primary key, and `idempotency` keyed on the
operation key. `SELECT … FOR UPDATE` is not required — the revision check is
the concurrency control, and it is already what SQLite uses.

**A tenant column belongs in that schema.** Today tenancy is enforced on the
fetched object and list filtering over-fetches, which is correct but does not
scale. See the note in `api/app.py`.

## Schema versioning and migration

Versioned, ordered migrations in `core/state/migrations.py`, shared by both
backends — one schema change written once with two dialect renderings, rather
than two sequences that drift.

```bash
orchestrator migrate            # status
orchestrator migrate --apply    # apply pending migrations
```

Three properties:

* **Idempotent.** Applying twice is a no-op. A restart, a crash mid-deploy, or
  two instances starting at once converge on the same place — PostgreSQL takes
  an advisory lock so only one applies each step.
* **Refuses a newer database.** A database at a version this build does not
  know is rejected at startup, naming both versions. An old process writing to
  a new schema produces rows a newer instance misreads; a clear failure at boot
  is enormously cheaper than a subtle one at 3am.
* **SQLite runs the same ladder.** Opening an existing database applies
  whatever is pending and records it.

## Retention

```bash
orchestrator prune --older-than-days 90            # dry run, the default
orchestrator prune --older-than-days 90 --apply
orchestrator prune --older-than-days 90 --apply --vacuum
```

Eligible statuses are derived from `TERMINAL_EXECUTION_STATUSES`, not retyped,
so a status added later defaults to protected. An execution and its audit
trail are always deleted together — an orphaned trail attributable to nothing
is worse than either keeping both or removing both.

Secure deletion: `--vacuum` returns freed pages to the filesystem. On a
copy-on-write filesystem, an SSD, or any snapshotted volume, that does **not**
guarantee the bytes are unrecoverable. Where that matters, use full-disk
encryption and destroy the key.

## Backup

`sqlite3`'s online backup API, not `cp` — the WAL means a plain copy can be
inconsistent. Commands in [../deployment/runbook.md](../deployment/runbook.md).

## What is tested

- Concurrent executions do not interfere (`test_engine.py`)
- An interrupted run does not repeat completed work (`test_failure_injection.py`)
- Idempotency keys suppress duplicate operations (`test_subsystems.py`)
- Revision conflicts are rejected (`test_core.py`)
- Retention keeps in-flight work and deletes terminal work (`test_completeness.py`)
- Cancellation and timeouts are bounded (`test_engine.py`)

## What is not

- Failover, replication, or leader election (PostgreSQL's own tooling does this)
- Restore rehearsal in CI (the commands are documented, not exercised)
- Multi-process access to one **SQLite** database — still unsupported, by design
