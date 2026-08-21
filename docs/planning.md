# Planning

## Goal analysis

Before anything is planned, the objective is turned into structured requirements
(`planning/goal.py`):

| Field | Meaning |
|---|---|
| `explicit` | Stated directly in the objective |
| `inferred` | Strongly implied but not stated |
| `assumptions` | Being assumed, and could be wrong |
| `constraints` | Bounds on how the work may be done |
| `unknowns` | Genuinely needed and genuinely missing |
| `success_criteria` | What would count as done, with a validator where one applies |

Each item carries provenance (`known`, `retrieved`, `inferred`, `assumed`,
`unverified`), so an assumption is never silently promoted into a requirement.

Clarification is requested **only** when work cannot begin at all without an
answer. Missing detail that a sensible default covers is not a blocker.

Without a model configured, a heuristic extractor runs instead: bullet and
sentence splitting for requirements, modal-verb detection for constraints,
question detection for unknowns.

## Decomposition

The planner produces structured tasks through a JSON schema. Each task carries
an objective that must stand alone - a worker sees only that text, its
dependencies' results, and the tools it was granted.

Generated plans are validated before use:

- **Cycles** are rejected. The plan is unusable.
- **Dangling dependencies** are dropped. A transcription error, not a logic error.
- **Unknown capabilities** are dropped. The planner does not get to invent them.
- **Unknown validators** fall back to `non_empty`, so a task always has a check.

## When the model is unhelpful

If no provider is configured, or the response is unparseable, or it contains no
usable tasks, planning falls back to a deterministic decomposition built from
the extracted requirements and the chosen pattern. The plan's `rationale` says
`(deterministic decomposition)` so this is visible rather than silent.

## Verification before anything runs

A plan is checked against the registries that will have to satisfy it before the
execution leaves `PLANNING` (spec section 87). The point is to fail at the start
with a precise reason rather than three tasks in with a confusing one.

| Checked | Outcome if missing |
|---|---|
| Graph structure (cycles, dangling references) | Error |
| Declared validators exist | Error - a missing gate would let unverified work through |
| Named tools exist (globs exempt) | Error - the agent would have nothing to work with |
| Required capabilities are covered | Warning when a specialist can be created, error when not |
| Model requirements are satisfiable | Error |
| Task allows at least one attempt | Error |

Warnings are recorded in the audit trail as `plan.verified` and proceeded past.
A plan referencing a capability that a dynamically created agent will supply is
a normal working situation, not a defect.

Verification runs against the *combined* graph — the execution's existing tasks
plus the new plan's — because an iterative or revised plan legitimately depends
on tasks from an earlier round.

## Re-planning

Triggered by a failed objective gate, or by a recovery ladder reaching `REPLAN`.
The planner sees what has already happened, including failures, and plans only
the remaining work. `max_replans` bounds it.

## Sizing

Complexity assessment sets the expected task count, and the planner is told. A
trivial objective gets one task. The instruction to the planner is explicit that
review, approval, and coordination tasks should not be invented when the
objective does not need them.
