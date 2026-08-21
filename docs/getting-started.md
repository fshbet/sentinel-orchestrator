# Getting started

## The shortest useful path

```bash
pip install -e ".[all]"
orchestrator init .
```

`init` writes `.orchestrator/config.yaml`. Add a model provider - here a local
Ollama daemon, which needs no API key:

```yaml
models:
  providers:
    - type: ollama
      base_url: http://localhost:11434
      models:
        - id: local/qwen
          model: qwen2.5:7b
          capabilities: [text_generation, tool_calling, structured_output]
          context_window: 32768
```

Then run an objective:

```bash
orchestrator run "Summarise the three files in ./notes and list what they disagree about."
```

## What just happened

```bash
orchestrator inspect <execution-id>
```

shows the goal analysis, the generated task graph, which agent and model handled
each task, what validated and what did not, and any failures with the recovery
that was chosen.

```bash
orchestrator audit <execution-id>
```

shows every decision in order, which is the record to reach for when the outcome
was not what you expected.

## Embedding it

```python
import asyncio
from orchestrator import Orchestrator

async def main():
    platform = await Orchestrator.create()
    execution = await platform.run("Accomplish this objective.")
    print(execution.status, execution.confidence)
    print(execution.summary)
    await platform.close()

asyncio.run(main())
```

## Reading the result

`execution.status` is where the run ended. `execution.confidence` is how much to
believe it:

| Confidence | Meaning |
|---|---|
| `confirmed` | Every mandatory check passed deterministically |
| `likely` | Passed, but some judgement was involved |
| `uncertain` | Passed, but something could not be verified |
| `blocked` | Stopped waiting on something external |
| `failed` | Did not achieve the objective |

A `completed` execution with `uncertain` confidence is telling you something
real: the work was done, but the platform could not prove it. Declare a
validator for the success criteria to move it to `confirmed`.

## When it stops and asks

High-risk tasks wait for a human by default:

```bash
orchestrator status <execution-id>          # shows the pending approval
orchestrator approve <execution-id> <approval-id>
orchestrator approve <execution-id> <approval-id> --reject
```

## Next

- `docs/configuration.md` - every setting
- `docs/orchestration.md` - how a run proceeds
- `docs/mcp.md` - connecting MCP servers
- `docs/security.md` - what is enforced and how
