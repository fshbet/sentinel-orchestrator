# ADR-001: Two-layer orchestration (state machine + dynamic DAG)

**Status:** accepted

## Context

The platform must handle both "what is the lifecycle of a run" (created,
planning, running, waiting for a human, cancelled, complete) and "what work does
this objective imply" (a graph of interdependent tasks generated per objective).

These are different shapes. A state machine cannot express a graph generated at
runtime. A graph cannot express "this run is paused pending a human decision"
without smearing lifecycle concerns across every node.

Systems that pick one and stretch it end up either with rigid workflows that
cannot adapt, or with an emergent control flow nobody can reason about.

## Decision

Use both, layered.

- **Lifecycle**: an explicit, total state machine (`core/state/machine.py`).
  Every transition is enumerated; anything else raises `InvalidStateTransition`.
- **Work**: a dynamic DAG (`core/workflow/graph.py`) generated per objective and
  validated structurally before execution.

The state machine owns *when* work runs. The graph owns *what* runs.

## Consequences

**Good.** Invariants become testable properties of the transition table rather
than hopes about code paths. "A failed mandatory gate cannot reach COMPLETED" is
enforced by the absence of an edge. The graph can be regenerated mid-run
(`VALIDATING -> PLANNING`) without the lifecycle losing coherence.

**Cost.** Two structures to keep in sync. A new phase means editing the
transition table, and forgetting to do so surfaces as a raised exception rather
than as silent misbehaviour - which is the failure mode we want, but it is still
friction.

## Alternatives rejected

- **Graph only.** Cannot express pause, approval, or cancel without every node
  knowing about them.
- **State machine only.** Cannot express a plan generated at runtime.
- **A model driving control flow.** Non-deterministic, untestable, and
  unenforceable - the thing this architecture exists to avoid.
