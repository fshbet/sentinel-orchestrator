# Agents

## Agents are data

An agent is a registered definition, not a class:

```yaml
agents:
  definitions:
    - id: analyst
      description: "Analyses supplied material and reports findings."
      capabilities: [analysis]
      tools: ["fs.read_file", "orchestrator.*"]
      permissions: ["fs.read", "artifact.write"]
      model_requirements: [long_context]
      mcp_servers: [research]
      runtime: generic
      version: "1.0.0"
      instructions: "Report what you could not establish, not only what you could."
      constraints:
        max_iterations: 8
        max_tool_calls: 40
        timeout_seconds: 600
        isolation: none
```

Definitions can also be loaded from a directory (`agents.directories`) or
registered by a plugin.

**The core ships no agents.** Shipping `developer` / `tester` / `researcher`
would encode one worldview into everyone's platform.

## Selection

Deterministic scoring, not a model call (`agents/selection.py`):

1. Reject any agent missing a required capability.
2. Reject any agent carrying more permissions than the selection policy allows.
3. Score the rest: capability coverage, then *fewer* excess capabilities, then
   *fewer* permissions.

Least privilege is the tiebreak, not an afterthought.

## Dynamic creation

When no registered agent covers the requirement, a scoped ephemeral specialist
is created. It gets exactly what its capabilities declare plus what the task
already restricts itself to, and it inherits nothing from the orchestrator.

Creation has a cost/benefit test. If agents *do* advertise the capability and
were rejected on privilege grounds, a near-duplicate would not help, so selection
fails loudly instead. `agent.created` appears in the audit trail whenever a
specialist is made.

## The agent loop

```
build budgeted context  ->  model call  ->  tool calls?  ->  observe  ->  repeat
                                             |
                                             no -> return structured result
```

Every bound is enforced by the loop, not requested of the model: iterations, tool
calls, model calls, and wall time.

A refused tool call becomes an **observation** the agent can react to, not a
crash - except when the refusal is "a human must approve this", which suspends
the task rather than letting the agent route around it.

## Runtimes

`runtime: generic` is the built-in loop. Other values map to registered adapters:

```python
from orchestrator.adapters.execution.subprocess_adapter import SubprocessAdapter

platform.engine.c.runtimes.register(
    SubprocessAdapter(["python", "my_worker.py"], name="my-worker")
)
```

An agent with `runtime: my-worker` is then executed by that adapter. See
`docs/adapters.md`.

A task can also name a runtime directly, which is how one node of a graph
becomes a nested orchestration without inventing an agent for it:

```yaml
metadata:
  runtime: sub_orchestrator     # or: sub_execution: true
```

## Skills

An agent can be given reusable knowledge — how to approach a kind of problem,
what the house conventions are:

```yaml
skills: [careful-reading, house-style]
```

Skills are content, not capability: they cannot grant a permission or a tool. A
declared skill that is not registered is a warning, and the agent runs without
it. See [skills.md](skills.md).

## Isolation

`constraints.isolation` declares what the agent needs: `none`, `restricted`,
`sandbox`, `container`, or `remote`.

Each runtime declares what it can actually provide, and **a task whose agent
asks for a level its runtime cannot honour fails rather than running with less
isolation than it asked for.** Silently under-delivering isolation is the
failure mode this check exists to prevent.

| Runtime | Provides |
|---|---|
| `generic` | `none`, `restricted` (scoped tools, workspace confinement) |
| `subprocess` | `none`, `restricted` (separate process, scrubbed environment, confined cwd) |
| `sub_orchestrator` | `none`, `restricted` |
| `openhands` | `none`, `restricted`, `remote` |

`sandbox` and `container` are not claimed by anything shipped, because nothing
shipped enforces them. An adapter that genuinely provides them declares so:

```python
class MyContainerAdapter(ExecutionAdapter):
    supported_isolation = frozenset({IsolationLevel.CONTAINER})
```

## Versioning

The registry keeps every version it has seen. An execution pins the version of
each agent at planning time, so a completed run can be replayed against exactly
what it ran with.
