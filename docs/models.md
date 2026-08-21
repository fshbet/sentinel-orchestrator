# Models

## Provider-independent

The engine never imports a provider SDK. It builds a `CompletionRequest`, hands
it to whatever the router selected, and gets a `ModelResponse`.

Shipped providers: `openai_compatible` (OpenAI, vLLM, LM Studio, llama.cpp,
gateways), `anthropic`, `ollama`, and `scripted` / `callable` for tests and for
embedding a host application's own model access.

Adding one means implementing `LLMProvider`: `generate`, `models`, and
optionally `health`, `stream`, `embed`.

## Capability-based routing

Callers ask for what a task needs, never for a model by name:

```python
RoutingRequirements(
    capabilities=[ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT],
    min_context_tokens=32000,
    max_cost_per_1k=0.01,
    prefer_local=True,
)
```

Selection: filter to models that satisfy the requirements, then order by
priority, then cost, with a larger context window breaking ties.

## Fallback

A failed model is put in an exponential cool-off so a broken backend is not
re-selected on the next task. The router then tries the next candidate, up to
`max_fallbacks`.

Two deliberate refinements:

- **A cool-off is a preference, not a prohibition.** If every capable model is
  cooling off, the cool-offs are cleared rather than stranding the task.
- **A single failing model re-raises its original error**, so a timeout stays
  classified as transient rather than being flattened into a generic model
  error. The recovery ladder depends on that distinction.

## Health

```bash
orchestrator models --json
orchestrator health --json
```

Health reports availability, served models, latency, and any error, per provider.
Health checks never raise; an unreachable provider reports as unavailable.

## Cost

Declare `cost_per_1k_input` / `cost_per_1k_output` and cost is tracked per
execution and enforced against `limits.max_cost`. Local models with no declared
cost are preferred when `prefer_local` is set.
