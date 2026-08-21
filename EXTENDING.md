# Extending

Every extension point, and which one to reach for.

| You want to | Use | Detail |
|---|---|---|
| Add an action an agent can take | A tool | §1 |
| Add a check that decides "done" | A validator | §2 |
| Add a worker role | An agent definition | §3 |
| Name a new kind of ability | A capability | §4 |
| Teach agents how to approach something | A skill | §4a |
| Use an external agent system | An execution adapter | §5 |
| Add a model backend | An `LLMProvider` | §6 |
| Change where state lives | A `StateStore` | §7 |
| Add stronger durability | A `WorkflowBackend` | §8 |
| Export telemetry | Audit subscription | §9 |
| Ship a repeatable shape | A workflow definition | §10 |
| Package several of the above | A plugin | §11 |

Nothing here requires modifying the engine.

---

## 1. Tools

```python
from orchestrator.core.domain.models import ToolSpec

def handler(arguments: dict, context) -> dict:
    """Sync or async. `context` carries execution_id, task_id, scope, workspace."""
    return {"result": do_something(arguments["input"])}

platform.tools.register(
    ToolSpec(
        id="my.tool",
        name="my_tool",
        description="What it does, in a sentence a model can act on.",
        input_schema={
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        },
        permissions=["my.permission"],
        risk="medium",
        timeout_seconds=30,
        idempotent=False,      # false disables retry and enables the operation log
        max_retries=2,
    ),
    handler,
)
```

Get these right and the rest of the platform behaves correctly around your tool:

- **`permissions`** — an agent without them cannot call it, or see it.
- **`risk`** — a floor, not a ceiling. A HIGH tool needs approval by default.
- **`idempotent`** — `false` means it is never auto-retried and its result is
  recorded against an operation key, so a retry after a crash replays instead of
  repeating the side effect.

Raise `ToolError` for failure (returned to the agent as an observation it can
react to) and `PermissionDenied` for refusal (which the agent may not route
around).

---

## 2. Validators

A validator is what makes `COMPLETED` mean something.

```python
from orchestrator.validation.validators import Validator

class ReconcilesToZero(Validator):
    name = "reconciles_to_zero"

    async def validate(self, spec, context):
        residual = (context.output() or {}).get("residual", 1)
        return self._result(
            spec, context,
            passed=abs(residual) < 0.01,
            message=f"residual {residual}",
        )

platform.validators.register(ReconcilesToZero())
```

```yaml
validations:
  - validator: reconciles_to_zero
    mandatory: true
```

Rules that hold for every validator:

- It **never** asks the agent whether the work is done. It checks something
  observable.
- It returns structured **evidence**, not just a boolean.
- A validator that raises **fails closed**. A broken check is not a pass.
- If it cannot really check anything, return `Confidence.UNCERTAIN` and say so.
  Reporting honest uncertainty is the intended behaviour, not a failure.

---

## 3. Agent definitions

```yaml
agents:
  definitions:
    - id: reconciler
      description: "Reconciles supplied records and reports discrepancies."
      capabilities: [reconciliation]
      tools: ["fs.read_file", "my.tool", "orchestrator.*"]
      permissions: ["fs.read", "my.permission", "artifact.write"]
      model_requirements: [structured_output]
      mcp_servers: [ledger]
      runtime: generic          # or the name of a registered adapter
      version: "1.0.0"
      instructions: "Report what you could not reconcile, not only what you could."
      constraints:
        max_iterations: 10
        max_tool_calls: 50
        timeout_seconds: 900
```

An agent's effective scope is the **intersection** of what it declares and what
its task needs, so declaring broadly does not grant broadly.

Bump `version` when the definition changes meaningfully. Executions pin the
version they ran with.

---

## 4. Capabilities

```yaml
capabilities:
  - id: reconciliation
    description: "Compares record sets and identifies discrepancies."
    requirements:
      tools: ["my.tool"]
      permissions: ["my.permission"]
      model_capabilities: [structured_output]
```

Declaring a capability's requirements means an agent selected for it gets exactly
those tools and permissions, and nothing else. That is how least privilege stays
automatic instead of manual.

### 4a. Skills

Reusable knowledge, as content rather than code. A skill cannot grant anything;
it tells an agent how to approach work it was already authorised to do.

```markdown
---
id: careful-reading
version: 2.1.0
description: How to read source material without over-claiming.
applies_to: [analysis]
requires: [evidence-first]
---

Separate what the source states from what it implies. Quote before paraphrasing.
```

Point `skills.directories` at the folder, or register from a plugin with
`registry.add_skill(...)`. Attach with `skills: [careful-reading]` on an agent
or `metadata: {skills: [...]}` on a task.

Versions are retained and pinned onto each execution, and a missing skill is a
warning rather than a failure. See [docs/skills.md](docs/skills.md).

---

## 5. Execution adapters

**Start here: the subprocess adapter.** Any language, any runtime.

```python
from orchestrator.adapters.execution.subprocess_adapter import SubprocessAdapter

platform.engine.c.runtimes.register(
    SubprocessAdapter(["node", "worker.js"], name="node-worker", timeout=600)
)
```

The worker reads a JSON brief on stdin and writes a JSON result on stdout. See
`examples/external_worker.py` for a complete one.

```json
{"ok": true,
 "summary": "...",
 "output": {...},
 "artifacts": [{"name": "report", "type": "text", "content": "..."}],
 "evidence": [{"source": "...", "summary": "..."}],
 "confidence": "likely"}
```

Point an agent at it with `runtime: node-worker`.

**OpenHands**, optional and never required:

```python
from orchestrator.adapters.execution.openhands import build

platform.engine.c.runtimes.register(
    build({"base_url": "http://localhost:3000", "timeout": 1800})
)
```

Endpoint paths are configuration rather than pinned constants, because that API
changes between releases.

**A custom adapter:**

```python
from orchestrator.adapters.execution.base import ExecutionAdapter, build_brief

class MyAdapter(ExecutionAdapter):
    name = "mine"

    async def run(self, context):
        brief = build_brief(context)
        payload = await my_system.execute(brief.to_dict())
        return self.parse_result(payload, context)
```

What an adapter gets and does not: a self-contained brief — objective, overall
objective, inputs, expected outputs, completion criteria, allowed tools,
permissions, capabilities, upstream results, constraints. It does **not** get
orchestration state. It cannot change the plan, mark itself complete, or grant
itself tools. A reply that does not state success is treated as failure, and
self-reported `confirmed` is capped at `likely`.

**Declare the isolation you actually enforce.** The engine refuses to dispatch a
task whose agent asked for a level the runtime does not claim, so an
under-declaration is safe and an over-declaration is a lie the platform will act
on:

```python
class MyContainerAdapter(ExecutionAdapter):
    name = "containerised"
    supported_isolation = frozenset({IsolationLevel.CONTAINER, IsolationLevel.NONE})
```

---

## 6. Model providers

```python
from orchestrator.core.domain.enums import ModelCapability
from orchestrator.core.domain.models import ModelSpec, Usage
from orchestrator.llm.base import LLMProvider, ModelResponse, ProviderHealth

class MyProvider(LLMProvider):
    name = "mine"

    def models(self):
        return [ModelSpec(
            id="mine/large",
            provider=self.name,
            model="large",
            capabilities=[
                ModelCapability.TEXT_GENERATION,
                ModelCapability.TOOL_CALLING,
                ModelCapability.STRUCTURED_OUTPUT,
            ],
            context_window=128000,
            max_output_tokens=4096,
            cost_per_1k_input=0.001,
            priority=50,
        )]

    async def generate(self, request, model):
        result = await my_backend(
            system=request.system,
            messages=[m.to_dict() for m in request.messages],
            tools=request.tools,
            schema=request.response_schema,
        )
        return ModelResponse(
            text=result.text,
            tool_calls=[...],      # ToolCallRequest per requested call
            model=model.model,
            provider=self.name,
            usage=Usage(model_calls=1, input_tokens=result.in_, output_tokens=result.out),
        )

    async def health(self):
        return ProviderHealth(provider=self.name, available=await my_backend.ping())
```

Two things matter for correct behaviour:

**Declare capabilities accurately.** Routing selects on them. Over-declaring is
worse than under-declaring: a model routed to a tool-calling task that cannot
call tools fails that task and burns a recovery attempt.

**Raise the right error type.** `ModelTimeout` / `ModelUnavailable` are
classified transient and retried; `ModelError` is permanent and triggers a
different ladder. Flattening them loses the distinction recovery depends on.

Already shipped: `openai_compatible` (covers OpenAI, vLLM, LM Studio,
llama.cpp, gateways), `anthropic`, `ollama`, plus `callable` for wrapping a host
application's existing model access.

---

## 7. Storage backends

Implement `StateStore`: `create`, `get`, `save`, `list`, `delete`,
`append_audit`, `audit`. Optionally `record_operation` / `lookup_operation` to
support idempotency.

`save` must honour `expected_revision` and raise `ConcurrentModification` on a
mismatch — that is what prevents lost updates.

---

## 8. Workflow backends

Implement `WorkflowBackend`: `submit`, `wait`, `cancel`. This is the seam for a
durable engine:

```python
class TemporalBackend(WorkflowBackend):
    name = "temporal"
    async def submit(self, execution_id, run): ...
    async def wait(self, handle, *, timeout=None): ...
    async def cancel(self, handle): ...
```

The engine is already durable through write-ahead state; a backend upgrades the
guarantee rather than providing it from nothing. See
[docs/durable-execution.md](docs/durable-execution.md).

---

## 9. Observability

```python
platform.state.audit.subscribe(lambda event: my_exporter(event))
```

Every decision arrives as a structured `AuditEvent`, already redacted. An
OpenTelemetry bridge is included and no-ops entirely when the package is absent:

```python
from orchestrator.adapters.observability.otel import attach_if_available
attach_if_available(platform.state.audit)
```

---

## 10. Workflow definitions

Only generic patterns ship. Add your own repeatable shapes:

```yaml
id: my.review_cycle
version: 1.0.0
description: Draft, judge independently, revise up to three times.
pattern: evaluator_optimizer
steps:
  - name: draft
    objective: Produce a first version.
    validations: [{validator: non_empty, mandatory: true}]
  - name: review
    objective: >
      Judge the draft against the criteria. Reply with JSON
      {"passed": bool, "reason": "...", "missing": [...]}.
      Do not revise the work yourself.
options:
  max_iterations: 3
```

Point `workflows.directories` at the folder.

Definitions are **immutable per version**: re-registering the same id and version
with different content raises. Publish a new version instead, so a running
execution never shifts under itself.

---

## 11. Plugins

Package the above so it installs rather than being wired by hand:

```python
def register(registry):
    registry.add_capability(...)
    registry.add_skill(Skill(id="house-style", content="..."))
    registry.add_tool(spec, handler)
    registry.add_validator(MyValidator())
    registry.add_agent(spec)
    registry.add_runtime(MyAdapter())
    registry.add_workflow(definition)
    registry.add_model_provider(MyProvider())
    registry.add_storage(MyStore())            # before anything is persisted
    registry.observe(my_exporter)              # audit-event subscription
```

```toml
[project.entry-points."orchestrator.plugins"]
my-plugin = "my_package.plugin"
```

or by module name:

```yaml
plugins:
  modules: ["my_package.plugin"]
```

A plugin receives that registration surface, not the platform object, so it
cannot reach into execution state. A plugin that fails to load is **reported, not
fatal** — it appears in `orchestrator health --json`. Pass `strict=True` to
`load_all` if you would rather startup fail.

See `examples/plugin_example.py`.

---

## 12. Testing an extension

Copy the pattern in `tests/conftest.py`: it builds a full platform against an
in-memory store and a scripted model, so an extension can be exercised
end-to-end without a network or a real provider.

```python
from orchestrator import Orchestrator
from orchestrator.config.loader import load

platform = await Orchestrator.create(
    config=load(
        include_discovered=False,
        overrides={
            "storage": {"backend": "memory"},
            "plugins": {"modules": ["my_package.plugin"]},
        },
    ),
    providers=[my_scripted_model],
    connect_mcp=False,
)
assert platform.tools.has("my.tool")
execution = await platform.run("Exercise the extension.")
assert execution.status is ExecutionStatus.COMPLETED
```
