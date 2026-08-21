# How a run proceeds

## Phases

```
CREATED
  goal analysis: structured requirements, success criteria, provenance
     |
PLANNING
  complexity assessment -> pattern + planning strategy -> validated task graph
     |
READY -> RUNNING
  scheduler dispatches ready tasks; per task:
     select agent by capability
     derive least-privilege scope
     run the bounded agent loop
     run the validation gate
     on failure: classify -> recovery ladder
     |
VALIDATING
  objective-level gate over the success criteria
     |
REVIEWING
  structural check: every task terminal, no failed mandatory validation
     |
COMPLETED
```

Any non-terminal phase can move to `WAITING` (a human must decide), `PAUSING ->
PAUSED`, `CANCELLING -> CANCELLED`, or `FAILED`. `VALIDATING` can return to
`PLANNING` when the objective gate fails and re-planning is permitted.

## Pattern selection

Deterministic scoring of the objective's structure - not its subject - decides
the shape. Signals include length, enumerated items, sequencing conjunctions,
comparison language, research language, iteration language, and expressed
uncertainty.

| Band | Pattern | Strategy |
|---|---|---|
| trivial | single agent | full |
| simple | sequential | full |
| moderate | parallel or sequential | adaptive |
| complex | dynamic DAG | adaptive |
| any, with uncertainty | as above | iterative |

High risk adds an independent evaluation step regardless of band.

Override with `--pattern` / `--strategy`, or `pattern=` / `plan_strategy=` in
code. Overriding is honoured exactly; the platform does not second-guess it.

## Composable patterns

`core/workflow/patterns.py` emits graph fragments, so patterns compose. A
parallel fan-out whose merge step is an evaluator-optimizer loop is just a graph.

- `single` - the whole objective, one task
- `sequential` - A then B then C
- `parallel` - fan out, optionally fan in to a merge
- `router` - a classification task with mutually exclusive branches
- `orchestrator_worker` - coordinator, workers, synthesis
- `evaluator_optimizer` - generate, judge, bounded revision
- `hierarchical` - a sub-orchestrator owning its own children

## Scheduling

The scheduler (`core/scheduler/`) is deliberately ignorant of what work means.
It computes the ready set from the graph, respects the concurrency limit,
acquires resource locks in sorted order (which makes deadlock structurally
impossible), and dispatches. It never asks a model what to run next.

A task declaring `resources: [db]` will not run while another task holding `db`
is running, even if both are otherwise ready.

## Iterative planning

When the strategy is `ITERATIVE`, the planner returns `complete: false`. The
engine runs what it has, then returns to `PLANNING` with the results so far. This
continues until the planner says it is done or `max_planning_rounds` is reached.

## The evaluator-optimizer loop

An evaluator task carries `metadata.optimizes` pointing at the task it judges.
When its structured output contains `passed: false` and rounds remain, the engine
**appends a new generate/evaluate pair** carrying the feedback forward. Finished
tasks stay finished, so the graph stays acyclic and every round is separately
visible in the audit trail.

An evaluator that writes only prose is treated as raising no objection. To drive
the loop, it must state a verdict.

## Router branches

A router plan materialises **every** branch, so the decision is visible in the
graph rather than hidden inside a model call. Once the decision task succeeds,
the branches it did not choose are skipped.

The decision is read from structured output first (`route`, `branch`, `choice`,
or `selected`). Prose is consulted only when exactly one route is named in it:
text mentioning several routes is not a decision, and picking the first mention
would be guessing. An undecidable result is recorded as `route.undecided` and no
branch is pruned.

## Handoff

A task may end by handing its remaining work to a different specialist, by
setting `handoff_to` on its result to a registered capability or agent id.

Handoff is an *outcome*, not a control structure. The orchestrator decides
whether to honour it, appends the follow-on task to the graph with the original
work as its dependency, and bounds the chain, so peers cannot pass work in a
circle. A handoff to something unregistered is refused and audited rather than
silently dropped.

## Nested orchestration

A task can be an entire orchestrated run of its own: its own goal analysis,
plan, agents, and gates, reporting back one result.

```yaml
# on a task
metadata:
  sub_execution: true
```

or by pointing an agent at the `sub_orchestrator` runtime.

Two things keep it safe. The child's budget is **carved out of the parent's
remaining budget**, not added to it, so nesting cannot multiply cost. And depth
is bounded by `limits.max_nesting_depth`, so a sub-orchestration that spawns a
sub-orchestration stops rather than recursing.

This is distinct from the hierarchical *pattern*, which puts parent and child
tasks in one graph. Nesting is what a task needs when its work cannot be
decomposed until it starts.
