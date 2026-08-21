# Extending

Which seam to use for what.

| You want to | Use | Where |
|---|---|---|
| Add a tool | `ToolRegistry.register` or a plugin | `docs/tools.md` |
| Add a check | A `Validator` subclass | `docs/validation.md` |
| Add a worker role | An agent definition | `docs/agents.md` |
| Use an external agent system | An execution adapter | `docs/adapters.md` |
| Add a model backend | An `LLMProvider` | below |
| Change where state lives | A `StateStore` | `docs/state.md` |
| Add durability | A `WorkflowBackend` | `docs/durable-execution.md` |
| Export telemetry | Subscribe to the audit log | `docs/adapters.md` |
| Add a repeatable shape | A workflow definition | below |

## A model provider

```python
from orchestrator.llm.base import LLMProvider, ModelResponse
from orchestrator.core.domain.models import ModelSpec, Usage


class MyProvider(LLMProvider):
    name = "mine"

    def models(self):
        return [ModelSpec(
            id="mine/model",
            provider=self.name,
            model="model",
            capabilities=[ModelCapability.TEXT_GENERATION],
            context_window=32000,
        )]

    async def generate(self, request, model):
        text = await my_backend(request.system, request.messages)
        return ModelResponse(
            text=text, model=model.model, provider=self.name,
            usage=Usage(model_calls=1),
        )
```

Register it with `Orchestrator.create(providers=[MyProvider()])` or from a
plugin's `add_model_provider`.

Raise `ModelTimeout` / `ModelUnavailable` for transient problems and `ModelError`
for permanent ones - the recovery ladder depends on that distinction.

## A workflow definition

Only generic patterns ship. Add your own repeatable shapes:

```yaml
id: my.review_cycle
version: 1.0.0
pattern: evaluator_optimizer
steps:
  - name: draft
    objective: Produce a first version.
    validations: [{validator: non_empty, mandatory: true}]
  - name: review
    objective: >
      Judge the draft against the criteria. Reply with JSON
      {"passed": bool, "reason": "..."}.
options:
  max_iterations: 3
```

Point `workflows.directories` at the folder. Definitions are immutable per
version: publish a new version rather than editing one in place.

## A custom agent runtime

Implement `AgentRuntime` (`name`, `async run(context) -> TaskResult`) and
register it. Subclass `ExecutionAdapter` to get the brief builder and result
parser for free.

## Testing an extension

The suite's own helpers are the pattern to copy: `tests/conftest.py` builds a
platform against an in-memory store and a scripted model, so an extension can be
exercised end-to-end without a network or a real provider.
