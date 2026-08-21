# Plugins

A plugin extends the platform without editing it.

## Writing one

```python
from orchestrator.core.domain.models import Capability, ToolSpec
from orchestrator.validation.validators import Validator


class SpreadsheetIsBalanced(Validator):
    name = "spreadsheet_balanced"

    async def validate(self, spec, context):
        total = context.output().get("total", 0)
        passed = abs(total) < 0.01
        return self._result(
            spec, context,
            passed=passed,
            message=f"residual {total}",
        )


def register(registry):
    registry.add_capability(Capability(id="spreadsheet-analysis"))
    registry.add_tool(
        ToolSpec(id="sheets.read", name="read_sheet", permissions=["fs.read"]),
        read_sheet_handler,
    )
    registry.add_validator(SpreadsheetIsBalanced())
```

## Registering

By entry point:

```toml
[project.entry-points."orchestrator.plugins"]
spreadsheets = "my_package.plugin"
```

Or by module name:

```yaml
plugins:
  modules: ["my_package.plugin"]
```

## What a plugin may add

| Method | Adds |
|---|---|
| `add_tool(spec, handler)` | A tool |
| `add_validator(validator)` | A check |
| `add_agent(spec)` | A worker definition |
| `add_capability(capability)` | A named ability |
| `add_skill(skill)` | Reusable knowledge for an agent |
| `add_runtime(runtime)` | An execution adapter |
| `add_workflow(definition)` | A repeatable shape |
| `add_model_provider(provider)` | A model backend |
| `add_storage(store)` | A `StateStore`, before anything is persisted |
| `observe(callback)` | An audit-event subscriber, for metrics or tracing |

That is the whole surface. A plugin does not receive the platform object, so it
cannot reach into execution state, and it cannot swap the store out from under a
running execution.

```python
def register(registry):
    registry.observe(lambda event: my_exporter(event.type, event.payload))
    registry.add_storage(MyPostgresStore(dsn))
```

## Failure handling

A plugin that fails to import or raises during `register` is **reported, not
fatal**. It shows in `orchestrator health --json` under `plugins` with the error.
Pass `strict=True` to `load_all` if you want startup to fail instead.

## Testing

```python
from orchestrator import Orchestrator
from orchestrator.config.loader import load

platform = await Orchestrator.create(
    config=load(overrides={"plugins": {"modules": ["my_package.plugin"]}})
)
assert platform.tools.has("sheets.read")
assert platform.validators.has("spreadsheet_balanced")
```
