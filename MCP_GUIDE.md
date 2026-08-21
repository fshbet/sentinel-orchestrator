# MCP guide

MCP is the platform's preferred interoperability boundary, in both directions.

It is a **capability boundary, not the workflow engine**. Nothing in the
orchestration core imports the MCP package, and there is a test asserting it.

---

## 1. Implementation

The client half of the protocol is implemented natively over JSON-RPC 2.0
(`mcp/transport.py`, `mcp/client.py`). No SDK dependency: stdio needs nothing at
all, HTTP needs `httpx`.

Supported: version negotiation, capability discovery, tools, resources, prompts,
long-running tasks, pagination, cacheable list responses with change-notification
invalidation, progress, cancellation, ping, and health.

**Versions are negotiated, not assumed.** The client offers the revisions it
knows, newest first, and adopts whatever the server answers with:

```python
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-06-18", "2025-03-26", "2024-11-05")
```

A server answering with an unrecognised revision is used on a best-effort basis
and says so in its instructions, rather than being rejected or silently
mishandled. No deprecated behaviour is hard-coded.

---

## 2. Connecting servers

```yaml
mcp:
  servers:
    filesystem:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      env: {}
      cwd: null

    remote:
      url: https://mcp.example.com/mcp
      headers:
        Authorization: "Bearer ${MCP_TOKEN}"
      timeout: 60
```

`transport` is inferred: `command` means stdio, `url` means streamable HTTP.

```bash
orchestrator mcp
```

shows each server's status, transport, negotiated protocol version, registered
tools, and — importantly — the tools that were **refused**, with reasons.

An unreachable server never breaks startup. It is recorded as disconnected with
its error, and its tools are simply absent.

---

## 3. The trust lifecycle

```
DISCOVER  →  DESCRIBE  →  POLICY CHECK  →  AUTHORIZE  →  REGISTER  →  USE
```

**A discovered tool is not a trusted tool.** This is the part that matters most,
so it is worth being precise about.

Servers describe their tools with annotations — `readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`. Those are **self-reported**.
A server can describe a destructive tool as read-only, by error or by design.

So here, annotations can only ever **raise** the assessed risk, never lower it.
On top of that, names and descriptions are scanned for markers (delete, remove,
drop, purge, payment, transfer, refund, credential, grant, admin) which raise it
further.

The shipped test suite includes a tool named `delete_everything` that claims
`readOnlyHint: true`. It is still assessed as destructive and refused.

Authorised tools are registered into the ordinary tool registry as
`mcp.<server>.<tool>`, so an agent calls an MCP tool exactly the way it calls
anything else — same permissions, same timeouts, same audit trail.

---

## 4. Per-server policy

```yaml
mcp:
  policies:
    - server: filesystem
      max_risk: medium                # tools assessed above this are refused
      require_approval_above: low     # and above this, a human is asked first
      allow_tools: []                 # empty means everything the server offers
      deny_tools: ["*delete*", "*write*"]
      permissions: [mcp.invoke]       # granted to tools from this server
      trusted: false
      timeout: 60
```

A worked example — a production database server that may only be read:

```yaml
    - server: production-db
      max_risk: low
      deny_tools: ["*write*", "*delete*", "*drop*", "*truncate*"]
      require_approval_above: none
```

---

## 5. Scoping MCP access to an agent

An agent sees MCP tools only if its scope allows them and it holds
`mcp.invoke`:

```yaml
agents:
  definitions:
    - id: researcher
      capabilities: [research]
      tools: ["mcp.research.*"]       # this server only
      permissions: ["mcp.invoke"]
      mcp_servers: [research]
```

The agent is not merely prevented from calling other servers' tools — it is never
told they exist.

---

## 6. Exposing orchestration over MCP

```bash
orchestrator mcp-serve                    # read-only
orchestrator mcp-serve --allow-control    # plus cancel and approval response
```

Always available:

| Tool | Does |
|---|---|
| `start_execution` | Start an orchestrated run; `wait: true` blocks until it stops |
| `get_execution` | Status, task graph, validations, pending approvals |
| `list_executions` | Recent runs, optionally filtered by status |
| `get_artifacts` | What a run produced |
| `get_audit` | The structured decision trail |
| `describe_platform` | Tools, agents, capabilities, models, validators |

Behind `--allow-control` only:

| Tool | Does |
|---|---|
| `respond_to_approval` | Answer a pending approval and resume |
| `cancel_execution` | Cancel gracefully |

Nothing exposed can bypass a gate, grant a tool, or force completion. Control
operations are gated because answering an approval on a human's behalf is
exactly the kind of administrative action that should require an explicit
decision to expose.

Register it with an MCP client the usual way:

```json
{
  "mcpServers": {
    "orchestrator": {
      "command": "orchestrator",
      "args": ["mcp-serve"]
    }
  }
}
```

---

## 7. Using the client directly

```python
from orchestrator.mcp.client import MCPClient

client = await MCPClient.connect(
    "example",
    {"transport": "stdio", "command": "npx", "args": ["-y", "some-mcp-server"]},
)

print(client.protocol_version, sorted(client.capabilities))

for tool in await client.list_tools():
    print(tool.name, tool.annotations)

result = await client.call_tool("search", {"query": "..."})
print(result.value())        # structured content if present, else text

print(await client.health())
await client.close()
```

Long-running server-side tasks, when the server advertises the capability:

```python
if client.supports_tasks():
    task = await client.create_task("long_job", {...})
    final = await client.await_task(task["taskId"], timeout=900)
```

Calling `create_task` on a server that does not advertise `tasks` raises rather
than guessing.

---

## 8. Troubleshooting

**A tool did not register.** `orchestrator mcp` gives the reason. Usually its
assessed risk exceeded the server policy's `max_risk`, or it matched
`deny_tools`. Raise the ceiling deliberately, not reflexively.

**The server will not start.** Check the command runs standalone and speaks
stdio rather than HTTP. Server stderr is captured and surfaced in the health
detail.

**Calls time out.** Raise the per-server `timeout`. The client sends a
`notifications/cancelled` when it gives up, so the server can stop working.

**Everything is refused.** Check `policy.approval_threshold` and the server's
`require_approval_above`. Both default to conservative values.

---

## 9. Further reading

- [docs/mcp.md](docs/mcp.md) — reference
- [docs/security.md](docs/security.md) — how MCP risk fits the wider model
- [docs/adr/ADR-003-mcp-native-client.md](docs/adr/ADR-003-mcp-native-client.md) —
  why there is no SDK dependency
