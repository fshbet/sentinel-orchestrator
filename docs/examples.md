# Examples

All generic. The platform ships no domain examples on purpose - see
`docs/research/ORCHESTRATION_RESEARCH.md` for why.

## A simple objective

```bash
orchestrator run "Summarise the three files in ./notes and list what they disagree about."
```

One agent, one validation, done. Complexity assessment scores this as simple and
does not build a graph for it.

## Parallel research and synthesis

```bash
orchestrator run "Compare three approaches to X against our constraints:
- what each one assumes
- what each one costs
- what each one rules out
Then recommend one and say what would change your mind."
```

Enumerated items plus comparison language yields a parallel fan-out with a merge.

## Iterative problem solving

```bash
orchestrator run "Figure out why the nightly job is unreliable and fix it."
```

Expressed uncertainty selects iterative planning: diagnose, then plan the next
step from what was actually found.

## Bounded refinement

```bash
orchestrator run "Draft the summary, then keep improving it until it is clear enough for someone with no background." --pattern evaluator_optimizer
```

Generate, judge with a separate evaluator, revise - bounded by
`max_optimizer_iterations`.

## With MCP tools

```yaml
mcp:
  servers:
    filesystem:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
  policies:
    - server: filesystem
      max_risk: medium
      deny_tools: ["*delete*"]
```

```bash
orchestrator mcp                 # confirm what registered and what was refused
orchestrator run "Inventory the project and report anything inconsistent."
```

## Embedded, with a host application's model

```python
from orchestrator import Orchestrator
from orchestrator.llm.providers.scripted import CallableProvider

async def my_model(request):
    return await my_existing_llm_client(request.system, request.messages)

platform = await Orchestrator.create(providers=[CallableProvider(my_model)])
execution = await platform.run("Accomplish this objective.")
```

## Human approval in the loop

```python
execution = await platform.run("Do something irreversible.")
if execution.status is ExecutionStatus.WAITING:
    approval = execution.pending_approval()
    print(approval.prompt)
    execution = await platform.approve(execution.id, approval.id, approved=True)
```

## Nested orchestration

When a piece of work cannot be decomposed until it starts, make it a run of its
own:

```python
task.metadata["sub_execution"] = True
```

The child gets its own goal analysis, plan, agents, and gates, and reports back
one result. Its budget comes out of the parent's remaining budget, and depth is
bounded by `limits.max_nesting_depth`.

## Packaged generic workflows

`workflows/` contains eight shapes, all structural rather than domain-specific:

| Workflow | Shape |
|---|---|
| `generic.sequential` | Plan, execute, verify |
| `generic.parallel` | Independent branches, then synthesis |
| `generic.evaluator_optimizer` | Generate, judge, bounded revision |
| `generic.orchestrator_worker` | Coordinator, workers, synthesis |
| `generic.research` | Scope, gather, evaluate, synthesise |
| `generic.creation` | Plan, create, validate, review, deliver |
| `generic.problem_solving` | Diagnose, hypothesise, test, correct, verify |
| `generic.complex_project` | Decompose, parallel workstreams, merge and verify |

The last four are the four worked examples from the specification.
