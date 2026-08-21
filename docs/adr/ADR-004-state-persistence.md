# ADR-004: SQLite document store with an optimistic-concurrency revision

**Status:** accepted

## Context

Execution state must be authoritative and survive process death. It must also be
cheap enough that local development needs no infrastructure.

The state is one aggregate - an execution with its tasks, validations, failures,
approvals, and artifacts - that is almost always read and written whole.

## Decision

Store the execution aggregate as a single JSON document in SQLite, guarded by an
integer `revision`, with audit events in a separate append-only table
(`core/state/sqlite_store.py`). Writes go through `StateStore`, an abstract
interface, so PostgreSQL or a distributed store can replace it without the engine
changing.

Blocking SQLite calls run on a worker thread so the async engine is never
stalled. WAL mode is enabled for crash durability.

Concurrent writes are detected, not merged: a save with a stale
`expected_revision` raises `ConcurrentModification`.

## Consequences

**Good.** Zero install. Crash-durable. The whole aggregate round-trips through
JSON, so a test cannot accidentally pass by sharing mutable objects with the
engine. Lost updates are impossible.

**Cost.** No partial updates: a large execution rewrites its whole document on
every save. At the scale of one orchestrated run this is not the bottleneck -
model calls are - but it would be at very high task counts.

## Alternatives rejected

- **Normalised relational schema.** More machinery, and the access pattern is
  whole-aggregate anyway.
- **Event sourcing as the primary store.** The audit log already provides event
  history; making it authoritative would mean replaying to read.
- **In-memory with periodic snapshots.** Loses the crash window, which is exactly
  where durability matters.
