# Troubleshooting

## "no registered model satisfies the requirements"

The task needs a capability nothing declares. Check what is registered:

```bash
orchestrator models --json
```

Usually the fix is declaring `tool_calling` or `structured_output` on a model
that supports it. Over-declaring is worse than under-declaring: a model routed to
a tool-calling task that cannot call tools fails that task.

## An execution is stuck in `waiting`

Something needs a human.

```bash
orchestrator status <id>
orchestrator approve <id> <approval-id>
```

Common causes: a task rated HIGH risk, a tool needing approval, or recovery
exhausted. `orchestrator inspect <id>` shows which.

## `completed` but `uncertain`

The work was done but could not be verified. This is honest, not a bug.

Give the success criteria a real validator:

```yaml
success_criteria:
  - description: "The report exists"
    validator: artifact_exists
    validator_config: {name: report}
```

## A tool is refused

```bash
orchestrator audit <id> --json | jq '.[] | select(.type=="tool.denied")'
```

The reason distinguishes the three causes: outside the agent's scope, missing a
permission, or refused by policy. Fix the agent's `tools` / `permissions`, or the
policy rule.

## An MCP tool did not register

```bash
orchestrator mcp
```

shows refused tools with reasons. Usually the tool's risk exceeded the server
policy's `max_risk`, or it matched a `deny_tools` pattern. Raise `max_risk`
deliberately, not reflexively - the assessment is usually right.

## An MCP server will not connect

`orchestrator mcp` shows the error. Check the command runs standalone, that it
speaks stdio (not HTTP), and that `args` are correct. Stderr from the server is
captured and shown in the health detail.

## A run hit a limit

```bash
orchestrator audit <id> --json | jq '.[] | select(.type=="limit.exceeded")'
```

names which budget. Raise it in `limits`, or reduce the work.

## Planning produced one task for a complex objective

Complexity assessment reads structure, not subject. Enumerate the parts:

```
- gather the evidence
- compare the options
- write the recommendation
```

Or force it: `--pattern dynamic_dag`.

## The plan says "(deterministic decomposition)"

The model was not used for planning: no provider configured, or its response was
unusable. Check `orchestrator health --json`.

## Everything failed after one model outage

Expected while the cool-off holds. If the model is back, retry - cool-offs clear
automatically when nothing else can run, and always after five minutes.

## Debugging in detail

```yaml
logging: {level: debug, json: true}
```

Then `orchestrator audit <id>` for the ordered decision trail. Every decision the
orchestrator made is in there, including the ones that produced the outcome you
did not expect.
