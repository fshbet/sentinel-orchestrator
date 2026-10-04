"""Schema versioning and migration.

Previously the schema was created with ``CREATE TABLE IF NOT EXISTS`` and a
version string was written into a metadata table that nothing ever read. That
is adequate for exactly one schema version and silently wrong for the second:
an old binary opening a new database would find columns it does not understand
and carry on, and a new binary opening an old one would fail somewhere
unrelated, at query time, with an error naming the wrong cause.

So this module does three things, and the third is the one that matters:

* **Versioned, ordered migrations.** Each has an integer version, a name, and
  the statements to apply it. They run in order inside a transaction.
* **Idempotent upgrade.** Applying twice is a no-op — the applied set is
  recorded, so a restart, a crash mid-deploy, or two instances starting at
  once converge on the same place.
* **Refusal, not adaptation.** A database newer than the code is refused at
  startup with a message naming both versions. The alternative is a process
  that appears to work while writing rows a newer instance will misread, and a
  clear failure at boot is enormously cheaper than a subtle one at 3am.

Backends differ only in dialect. ``MIGRATIONS`` carries both variants for each
step, so a schema change is written once with two renderings rather than as
two independent sequences that drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...errors import OrchestratorError

SQLITE = "sqlite"
POSTGRES = "postgres"


class SchemaTooNew(OrchestratorError):
    """The database was written by a newer version of this software."""


class MigrationFailed(OrchestratorError):
    """A migration could not be applied."""


@dataclass(frozen=True)
class Migration:
    """One ordered, named schema change."""

    version: int
    name: str
    sqlite: tuple[str, ...] = ()
    postgres: tuple[str, ...] = ()

    def statements(self, dialect: str) -> tuple[str, ...]:
        return self.sqlite if dialect == SQLITE else self.postgres


# The bookkeeping table itself, created before anything else and never
# migrated. It has to exist before the first migration can record that it ran.
BOOTSTRAP: dict[str, str] = {
    SQLITE: """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
    """,
    POSTGRES: """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """,
}


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        sqlite=(
            """
            CREATE TABLE IF NOT EXISTS executions (
                id          TEXT PRIMARY KEY,
                objective   TEXT NOT NULL,
                status      TEXT NOT NULL,
                revision    INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                document    TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_executions_status "
            "ON executions(status, updated_at DESC)",
            """
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                key        TEXT PRIMARY KEY,
                result     TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        ),
        postgres=(
            """
            CREATE TABLE IF NOT EXISTS executions (
                id          TEXT PRIMARY KEY,
                objective   TEXT NOT NULL,
                status      TEXT NOT NULL,
                revision    BIGINT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL,
                updated_at  TIMESTAMPTZ NOT NULL,
                document    JSONB NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_executions_status "
            "ON executions(status, updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                execution_id TEXT NOT NULL,
                sequence     BIGINT NOT NULL,
                id           TEXT NOT NULL,
                type         TEXT NOT NULL,
                task_id      TEXT,
                actor        TEXT,
                timestamp    TIMESTAMPTZ NOT NULL,
                payload      JSONB NOT NULL,
                PRIMARY KEY (execution_id, sequence)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                key        TEXT PRIMARY KEY,
                result     JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=2,
        name="tenant_column",
        # Tenancy is enforced today by reading the fetched execution's own
        # context, which is correct but means listing over-fetches and filters
        # in Python. A real column lets the database do it, and lets an index
        # make pagination correct under load.
        sqlite=(
            "ALTER TABLE executions ADD COLUMN tenant TEXT NOT NULL DEFAULT 'default'",
            "CREATE INDEX IF NOT EXISTS idx_executions_tenant "
            "ON executions(tenant, status, updated_at DESC)",
        ),
        postgres=(
            "ALTER TABLE executions ADD COLUMN IF NOT EXISTS tenant "
            "TEXT NOT NULL DEFAULT 'default'",
            "CREATE INDEX IF NOT EXISTS idx_executions_tenant "
            "ON executions(tenant, status, updated_at DESC)",
        ),
    ),
    Migration(
        version=3,
        name="audit_retention_index",
        # Retention scans by age. Without this it is a full table scan on the
        # one operation you run when the table is already large.
        sqlite=(
            "CREATE INDEX IF NOT EXISTS idx_executions_updated ON executions(updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency(created_at)",
        ),
        postgres=(
            "CREATE INDEX IF NOT EXISTS idx_executions_updated ON executions(updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency(created_at)",
        ),
    ),
    Migration(
        version=4,
        name="api_tokens",
        # Token state has to be shared, not per-process. Revocation on one
        # replica was invisible to the others until restart, which made
        # "revoked" a promise the deployment could not keep.
        #
        # Two hashes per row, doing different jobs:
        #
        #   lookup_hash  a deterministic digest of the presented token, so a
        #                request is one indexed SELECT rather than a scan of
        #                every token. Deterministic by necessity — an index
        #                needs a stable key.
        #   digest       a per-row salted derivation, compared in constant
        #                time. This is what actually authenticates. The lookup
        #                hash narrows to one row; the digest decides.
        #
        # The raw token is in neither column and is never written anywhere.
        sqlite=(
            """
            CREATE TABLE IF NOT EXISTS api_tokens (
                token_id     TEXT PRIMARY KEY,
                lookup_hash  TEXT NOT NULL,
                digest       TEXT NOT NULL,
                salt         TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                scopes       TEXT NOT NULL,
                tenant       TEXT NOT NULL DEFAULT 'default',
                description  TEXT NOT NULL DEFAULT '',
                issued_at    TEXT NOT NULL,
                not_before   TEXT,
                expires_at   TEXT,
                revoked_at   TEXT,
                revoked_by   TEXT,
                revoke_reason TEXT
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_tokens_lookup "
            "ON api_tokens(lookup_hash)",
            "CREATE INDEX IF NOT EXISTS idx_api_tokens_principal "
            "ON api_tokens(principal_id)",
        ),
        postgres=(
            """
            CREATE TABLE IF NOT EXISTS api_tokens (
                token_id     TEXT PRIMARY KEY,
                lookup_hash  TEXT NOT NULL,
                digest       TEXT NOT NULL,
                salt         TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                scopes       TEXT[] NOT NULL,
                tenant       TEXT NOT NULL DEFAULT 'default',
                description  TEXT NOT NULL DEFAULT '',
                issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                not_before   TIMESTAMPTZ,
                expires_at   TIMESTAMPTZ,
                revoked_at   TIMESTAMPTZ,
                revoked_by   TEXT,
                revoke_reason TEXT
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_tokens_lookup "
            "ON api_tokens(lookup_hash)",
            "CREATE INDEX IF NOT EXISTS idx_api_tokens_principal "
            "ON api_tokens(principal_id)",
        ),
    ),
    Migration(
        version=5,
        name="rate_limit_buckets",
        # Shared rate limiting. The in-process limiter counts per replica, so
        # N replicas permitted N times the configured rate and every deploy
        # reset the counters. Counting in the database that already holds
        # shared state avoids adding Redis for one table.
        sqlite=(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_hits (
                bucket   TEXT NOT NULL,
                hit_at   TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket "
            "ON rate_limit_hits(bucket, hit_at)",
        ),
        postgres=(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_hits (
                bucket   TEXT NOT NULL,
                hit_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket "
            "ON rate_limit_hits(bucket, hit_at)",
        ),
    ),
)

# The newest version this code knows how to work with.
TARGET_VERSION = max(m.version for m in MIGRATIONS)


def pending(applied: Sequence[int]) -> list[Migration]:
    """Migrations not yet applied, in order."""
    done = set(applied)
    return [m for m in MIGRATIONS if m.version not in done]


def check_compatible(applied: Sequence[int]) -> None:
    """Refuse a database written by a newer build.

    Refusing at startup rather than adapting: an old process writing to a new
    schema produces rows a newer instance misreads, and finding that out later
    costs far more than not starting.
    """
    if not applied:
        return
    highest = max(applied)
    if highest > TARGET_VERSION:
        raise SchemaTooNew(
            f"the database is at schema version {highest} but this build "
            f"supports up to {TARGET_VERSION}. It was written by a newer "
            f"version of the orchestrator. Upgrade this deployment, or point "
            f"it at a database matching its version.",
            database_version=highest,
            supported_version=TARGET_VERSION,
        )


def describe(applied: Sequence[int]) -> dict[str, object]:
    """Migration state, for `orchestrator migrate --status`."""
    done = set(applied)
    return {
        "current_version": max(applied) if applied else 0,
        "target_version": TARGET_VERSION,
        "up_to_date": all(m.version in done for m in MIGRATIONS),
        "migrations": [
            {
                "version": m.version,
                "name": m.name,
                "applied": m.version in done,
            }
            for m in MIGRATIONS
        ],
        "pending": [m.name for m in pending(applied)],
    }
