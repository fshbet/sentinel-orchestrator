# Tools

## One path for everything

Every callable capability reaches an agent through `ToolRegistry`, whatever its
origin - builtin, native, plugin, MCP, or adapter. That is what makes
permissions, timeouts, retries, idempotency, and audit uniform rather than
per-integration.

```python
from orchestrator.core.domain.models import ToolSpec

platform.tools.register(
    ToolSpec(
        id="my.tool",
        name="my_tool",
        description="What it does, in a sentence a model can act on.",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        permissions=["my.permission"],
        risk="medium",
        timeout_seconds=30,
        idempotent=False,
    ),
    handler,   # (arguments: dict, context: ToolContext) -> Any, sync or async
)
```

## Built-in tools

**Bookkeeping** (enabled by default, safe):

- `orchestrator.record_note` - store a keyed note in working memory
- `orchestrator.emit_artifact` - publish a durable artifact

**Filesystem** (opt-in): `fs.read_file`, `fs.list_directory`, `fs.write_file`.
Confined to the workspace; traversal outside it is refused before anything opens.

**Process** (opt-in): `process.run`. The executable allow-list is **mandatory** -
there is no "run anything" mode. Rated HIGH risk, so it needs approval by default.

**HTTP** (opt-in, needs `httpx`): `http.request`, with an optional host
allow-list.

## What happens on a call

```
authorise            scope, then permissions, then policy
idempotency check    replay the recorded result if this ran before
execute              with the tool's timeout
retry                only if the tool declares itself idempotent
audit                call, result, duration, and any denial
```

A failure is returned as data (`ToolResult.ok is False`), not raised - so an
agent can react to it. A permission denial is raised, because routing around a
denial is not something an agent gets to attempt.

## Idempotency

A tool with `idempotent: false` gets a deterministic operation key derived from
the execution, task, tool, and arguments. If that key has been seen, the original
result is replayed instead of the side effect happening twice. This is what makes
retry-after-crash safe.

## Scope

An agent sees only tools its scope allows and whose permissions it holds. This is
enforced twice: `for_scope()` filters what the model is even told about, and
`authorize()` re-checks at call time.

See `docs/security.md` for the permission vocabulary.
