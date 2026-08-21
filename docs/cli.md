# CLI

Human-readable by default, machine-readable with `--json`.

## Commands

```bash
orchestrator init [DIR]                   # write a project configuration
orchestrator run "<objective>"            # run an objective
orchestrator run "<objective>" --detach   # create without running
orchestrator status [ID]                  # one execution, or list recent
orchestrator inspect ID                   # task graph, validations, failures
orchestrator audit ID [--after N]         # structured decision trail
orchestrator pause ID
orchestrator resume ID
orchestrator cancel ID [--reason TEXT]
orchestrator approve ID APPROVAL_ID [--reject] [--response TEXT]
orchestrator agents
orchestrator capabilities
orchestrator skills
orchestrator tools
orchestrator models
orchestrator workflows
orchestrator mcp                          # server health and authorisation state
orchestrator health
orchestrator validate                     # check config without running
orchestrator serve [--host H] [--port P]  # REST API
orchestrator mcp-serve [--allow-control]  # expose orchestration over MCP
```

Global: `--config PATH`, `--workspace PATH`, `--json`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error (not found, configuration, runtime) |
| 2 | The run failed, or validation found problems |

## Forcing a shape

```bash
orchestrator run "..." --pattern parallel --strategy iterative
```

Patterns: `single_agent`, `sequential`, `parallel`, `router`,
`orchestrator_worker`, `evaluator_optimizer`, `dynamic_dag`, `hierarchical`.
Strategies: `full`, `iterative`, `adaptive`.

An override is honoured exactly. The platform does not second-guess it.

## Scripting

```bash
ID=$(orchestrator run "..." --detach --json | jq -r .id)
orchestrator status "$ID" --json | jq -r .status
orchestrator audit "$ID" --json | jq -r '.[] | select(.type=="tool.denied")'
```
