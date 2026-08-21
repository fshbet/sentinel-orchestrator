# State

## Authoritative, not conversational

The orchestrator's state is the source of truth. The model is not. This is what
makes pause, resume, cancellation, and crash recovery possible.

## The aggregate

`Execution` holds: objective, status, requirements, workflow reference, plan,
tasks, artifacts, validations, failures, recoveries, approvals, limits, usage,
confidence, summary, and a revision number.

Persisted as one JSON document with a monotonic `revision`. Audit events go to a
separate append-only table.

## Transitions

`core/state/machine.py` is the only place a status changes. Every transition is
enumerated; anything else raises `InvalidStateTransition`. Terminal statuses have
no outgoing edges at all, which is what guarantees completed work is never
redone.

Every transition is audited before it is persisted.

## Concurrency

Optimistic: a save with a stale `expected_revision` raises
`ConcurrentModification` rather than clobbering. Concurrent executions are fully
independent.

## Durability and recovery

State is written before the next step is attempted, so a crash loses at most one
in-flight task. `LocalWorkflowBackend.recover_interrupted()` finds executions a
crash left mid-flight and rewinds only the in-flight tasks - finished work stays
finished.

Non-idempotent tool calls are guarded by an operation log, so a retry after a
crash does not repeat the side effect.

## Backends

`sqlite` (default) and `memory`. Both implement `StateStore`; a PostgreSQL or
distributed backend can replace either without the engine changing.
