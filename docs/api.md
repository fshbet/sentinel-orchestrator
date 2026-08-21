# REST API

Optional. `pip install -e ".[api]"`, then `orchestrator serve`.

Versioned under `/v1`. Errors are structured: `{"error": {"code", "message",
"details"}}`.

## Executions

```
POST   /v1/executions                                   create, optionally run
GET    /v1/executions?status=&limit=&offset=            list
GET    /v1/executions/{id}                              full record
POST   /v1/executions/{id}/pause
POST   /v1/executions/{id}/resume
POST   /v1/executions/{id}/cancel?reason=
GET    /v1/executions/{id}/audit?after=                 decision trail
GET    /v1/executions/{id}/artifacts
POST   /v1/executions/{id}/approvals/{approval_id}      answer an approval
```

Create:

```json
{
  "objective": "Accomplish this objective.",
  "context": {},
  "pattern": "parallel",
  "plan_strategy": "adaptive",
  "run": true,
  "limits": {"max_model_calls": 50}
}
```

Approve:

```json
{"approved": true, "response": "proceed", "responder": "alice"}
```

## Registries

```
GET /v1/agents
GET /v1/capabilities
GET /v1/skills
GET /v1/tools
GET /v1/models
GET /v1/workflows
GET /v1/mcp
```

## Operations

```
GET /health     platform, model, and MCP health
GET /metrics    Prometheus text format
```

## Notes

There is no endpoint that bypasses a gate, grants a tool, or forces completion.
The API exposes exactly what the CLI does.

Authentication is deliberately not built in - deployments differ too much. Put it
behind a reverse proxy or a gateway, or wrap `create_app()` with your own
middleware.
