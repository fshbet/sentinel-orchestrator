# Recovery

## Failures are data

Every failure is classified before anything decides what to do about it, and the
category comes from the structured error code rather than from parsing a message.

| Category | Typical cause |
|---|---|
| `transient` | Timeout, model unavailable, connection blip |
| `tool` | A tool failed or does not exist |
| `mcp` | Protocol or server error |
| `model` | Model rejected the request, or nothing capable is registered |
| `context` | Input exceeded the window |
| `dependency` | No capable agent |
| `permission` | Policy refused |
| `validation` | A gate failed |
| `execution` | Resource limit, process problem |
| `logical` | Invalid workflow or transition |
| `unknown` | Everything else |

## Strategy ladders

Each category has an ordered ladder. Position is the attempt number.

```
transient:   retry -> retry -> alternate model -> ask a human
tool:        modify parameters -> alternate tool -> re-plan -> ask
model:       alternate model -> modify parameters -> alternate agent -> ask
context:     reduce scope -> alternate model -> re-plan -> ask
permission:  ask -> reduce scope -> terminate
validation:  modify parameters -> alternate agent -> re-plan -> ask
logical:     re-plan -> ask -> terminate
```

Two deliberate choices:

- **Permission failures escalate first.** Silently working around a refusal is
  exactly the behaviour a policy engine exists to prevent.
- **Context overflow reduces scope rather than retrying.** Retrying the same
  oversized prompt cannot succeed.

Objective-level failures skip every task-scoped strategy: they can only re-plan,
escalate, or stop.

## Bounds

Every ladder terminates at human escalation or a safe stop. `max_task_attempts`
and `max_replans` bound how far it climbs. With
`recovery.allow_human_escalation: false`, ladders end at `TERMINATE` instead.

## What each strategy does

| Strategy | Effect |
|---|---|
| `retry` | Task back to READY |
| `modify_parameters` | Records the failure in the task's inputs, then retries |
| `alternate_tool` | Removes the failed tool from the task's allow-list, retries |
| `alternate_agent` | Excludes the agent, clears the assignment, retries |
| `alternate_model` | Excludes the model, retries |
| `reduce_scope` | Marks the task reduced, strips bulk inputs, retries |
| `replan` | Fails the task, returns the execution to PLANNING |
| `rollback` | Discards the result and validations, retries |
| `request_human_input` | Creates a pending approval, task to WAITING |
| `terminate` | Fails the task; no further automatic action |

A model exclusion is a *hint*: if it would leave nothing able to run, the runtime
falls back to the excluded model rather than stranding the task.

## Observing it

```bash
orchestrator inspect <id>     # failures with the chosen recovery
orchestrator audit <id>       # failure, recovery.selected, recovery.result
```
