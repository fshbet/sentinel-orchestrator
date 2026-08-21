# Adapters

Four seams, each an interface the core depends on. None is required.

## Execution adapters

Anything that can take one task and return a structured result.

**Subprocess** - the one to reach for first. Any language, any runtime:

```python
from orchestrator.adapters.execution.subprocess_adapter import SubprocessAdapter

platform.engine.c.runtimes.register(
    SubprocessAdapter(["python", "worker.py"], name="my-worker")
)
```

The worker reads a JSON brief on stdin and writes a JSON result on stdout:

```json
{"ok": true, "summary": "...", "output": {...},
 "artifacts": [{"name": "report", "type": "text", "content": "..."}],
 "evidence": [{"source": "...", "summary": "..."}]}
```

**OpenHands** - optional, never required, and no OpenHands concept appears in the
core:

```python
from orchestrator.adapters.execution.openhands import build

platform.engine.c.runtimes.register(
    build({"base_url": "http://localhost:3000", "timeout": 1800})
)
```

Endpoint paths are configuration rather than pinned constants, because that API
changes between releases.

Point an agent at an adapter with `runtime: my-worker`.

### What an adapter gets, and does not

It receives a self-contained brief: the task objective, the overall objective,
inputs, expected outputs, completion criteria, allowed tools, permissions,
required capabilities, upstream results, and constraints.

It does **not** receive orchestration state. It cannot change the plan, mark
itself complete, or grant itself tools. A reply that does not state success is
treated as failure, and self-reported `confirmed` is capped at `likely`.

## Storage adapters

Implement `StateStore`: `create`, `get`, `save`, `list`, `delete`,
`append_audit`, `audit`. Optionally `record_operation` / `lookup_operation` for
idempotency. Shipped: SQLite and in-memory.

## Workflow backends

Implement `WorkflowBackend`: `submit`, `wait`, `cancel`. Shipped: `local`
(in-process, with interrupted-run recovery) and `sequential`. This is the seam
for a durable engine such as Temporal.

## Observability

Subscribe to the audit log:

```python
platform.state.audit.subscribe(lambda event: my_exporter(event))
```

An OpenTelemetry bridge is included and no-ops entirely when the package is
absent:

```python
from orchestrator.adapters.observability.otel import attach_if_available
attach_if_available(platform.state.audit)
```
