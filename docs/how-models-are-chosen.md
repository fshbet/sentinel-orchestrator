# How models are chosen

Short answer: **it picks automatically, and it uses several models in a single
run.** You never name a model in your objective. You describe what you want;
the platform works out which model should handle each step.

## The one-sentence version

Each step says what it *needs* — "must be able to call tools", "must return
strict JSON", "must fit 100k tokens" — and the router picks the cheapest
capable model that is currently healthy.

Model names never appear in orchestration logic. That is the whole point: you
can swap your entire model lineup by editing a config file, and nothing about
how work is planned or run changes.

## What actually happens in one run

A single objective typically touches **three or four different models**,
because the stages want different things.

| Stage | What it needs | Why |
| --- | --- | --- |
| Understand the goal | Strict JSON | It turns your sentence into success criteria. Cheap, structured. |
| Plan the work | Strict JSON, good reasoning | Breaking a goal into ordered steps is the hardest thinking in the run. |
| Do each step | Tool calling | This one actually reads files and calls APIs. Different steps can get different models. |
| Argue against the result | Strict JSON | The devil's advocate — deliberately a **separate call** from the one that did the work. |

That last one matters. Asking the same conversation to critique itself gets
agreement, not scrutiny. So the advocate always runs as a fresh call, and
often lands on a different model entirely.

## How a model gets picked

Every model in your config declares what it can do:

```yaml
- id: nvidia/nemotron-3-super-120b
  capabilities: [text_generation, tool_calling, structured_output, long_context]
  context_window: 131072
  priority: 10          # lower number = tried first
  cost_per_1k_input: 0.0
```

When a step needs a model, the router:

1. **Filters** to models that have every capability the step asked for, a big
   enough context window, and a cost under the ceiling.
2. **Drops** any model in cool-off — one that just failed. A broken backend
   should not be picked again on the very next step.
3. **Sorts** by `priority`, then cost, then context size.
4. **Takes the first one.**

Everything in that list is a hard filter except priority. A model that cannot
call tools is never chosen for a step that needs tools, no matter how cheap or
how high in your list it sits.

## What happens when a model fails

It moves down the list. It does not retry the same model forever.

A real run from this workspace:

```
model.selected   nvidia/nemotron-3-super-120b   (priority 10 — first choice)
model.fallback   nvidia → HTTP 400, trying next
model.selected   ollama/llama3.1-8b             (fell through to local)
tool.call        fs.read_file
execution.completed
```

The objective still finished. Nobody had to intervene. Three things made that
work:

* **Cascading fallback** — up to `max_fallbacks` models are tried in order.
* **Cool-off** — a model that just failed is skipped for ~30 seconds, so one
  bad provider does not poison every following step.
* **Cool-off is a preference, not a rule** — if the failing model is the *only*
  one that can do the job, the cool-off is cleared rather than stranding the
  task. Refusing to run at all is worse than trying again.

## Can I force a specific model?

Yes, at three levels, each narrower than the last:

* **Config priority** — reorder your lineup. This is the normal way.
* **Per-agent** — an agent definition can name a preferred model.
* **Per-task** — a plan can assign a model to one step.

A preference is a strong nudge, not a lock. If the preferred model cannot
satisfy the step's actual requirements, capability still wins: getting a wrong
answer from your favourite model is not better than getting a right one from
another.

## Why this design

Three reasons, all of which show up in practice:

**Cost.** The expensive reasoning model plans; a small local model does the
mechanical steps. You do not pay frontier prices to read a file.

**Reliability.** Free tiers rate-limit and providers have outages. A run that
depends on exactly one model fails whenever that model does.

**Portability.** Every model here is behind the same interface. Moving from
Ollama to a cloud provider is a config edit, not a code change.

## Your current lineup

```
NVIDIA      nemotron-3-super-120b     priority 10   ← planner, first choice
OpenRouter  gpt-oss-20b               priority 20
Ollama      llama3.1:8b               priority 30   ← local, always available
Ollama      qwen2.5:14b               priority 40
OpenRouter  glm-5.2                   priority 45
Ollama      qwen3:8b                  priority 50
Ollama      qwen3-coder:30b           priority 60
NVIDIA      llama-3.3-70b             priority 70
```

Cloud models are tried first because they are stronger; local Ollama sits in
the middle as the reliable floor. If every remote provider is down, work still
runs — slower, on your own machine.

## Seeing it happen

Every choice is recorded. Open the **Decision trail** panel in the console, or:

```bash
orchestrator audit <execution-id> | grep model
```

You will see a `model.selected` event for every step, with the reason the
router picked it, and a `model.fallback` event each time one was dropped.
