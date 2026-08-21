# ADR-012: Every loop has an enforced bound

**Status:** accepted

## Context

Agent systems fail by not stopping: retry loops, re-planning loops, self-critique
loops, agents calling each other. Each individual step looks locally reasonable.
Prompt instructions to "not loop forever" are not a control mechanism.

## Decision

Every loop is bounded in software, and the bound is counted rather than requested:

| Loop | Bound | Enforced in |
|---|---|---|
| Agent iterations | `AgentConstraints.max_iterations` | `agents/runtime.py` |
| Tool calls per agent | `max_tool_calls` | `agents/runtime.py` |
| Model calls per agent | `max_model_calls` | `agents/runtime.py` |
| Task attempts | `max_task_attempts` | `recovery/strategies.py` |
| Re-plans | `max_replans` | `recovery/strategies.py` |
| Optimizer rounds | `max_optimizer_iterations` | `core/execution/engine.py` |
| Model fallbacks | `max_fallbacks` | `llm/routing.py` |
| Engine rounds | `max_planning_rounds * 4` | `core/execution/engine.py` |
| Wall clock, tokens, cost | `ResourceLimits` | `core/execution/limits.py` |

Every recovery ladder terminates at human escalation or a safe stop. When
escalation is disabled, the ladder ends at `TERMINATE`.

The evaluator-optimizer loop appends new tasks per round rather than reviving
finished ones, so the bound is visible in the graph and in the audit trail.

## Consequences

**Good.** A run cannot spin. Cost is bounded before it is incurred, not after.
Every bound is configurable, and every one is tested.

**Cost.** A genuinely hard objective can hit a bound and stop before finishing. It
stops *visibly*, with a recorded reason and an escalation, which is correct.

## Alternatives rejected

- **Prompt-level instructions not to loop.** Not enforcement.
- **A single global timeout.** Too coarse; says nothing about which loop ran away.
- **Unbounded self-reflection.** The failure mode being prevented.
