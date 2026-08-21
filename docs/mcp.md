# MCP

MCP is the platform's preferred interoperability boundary. It is a *capability
boundary*, not the workflow engine - nothing in the orchestration core imports
the MCP package.

## As a client

```yaml
mcp:
  servers:
    filesystem:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
    remote:
      url: https://mcp.example.com/mcp
      headers: {Authorization: "Bearer ..."}
      timeout: 60
  policies:
    - server: filesystem
      max_risk: medium
      require_approval_above: low
      deny_tools: ["*delete*"]
      permissions: [mcp.invoke]
```

Implemented natively over JSON-RPC 2.0 - no SDK dependency. stdio needs nothing;
HTTP needs `httpx`.

Supported: version negotiation, capability discovery, tools, resources, prompts,
long-running tasks, pagination, cacheable list responses with change-notification
invalidation, progress, cancellation, ping, and health.

**Protocol versions are negotiated, not assumed.** The client offers the
revisions it knows, newest first, and adopts what the server answers with. An
unrecognised revision is used on a best-effort basis and says so.

## The trust lifecycle

```
DISCOVER -> DESCRIBE -> POLICY CHECK -> AUTHORIZE -> REGISTER -> USE
```

A discovered tool is not a trusted tool. Server annotations (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`) are hints that can only
**raise** assessed risk, never lower it. A tool called `delete_everything` that
claims `readOnlyHint: true` is still treated as destructive - there is a test
asserting exactly this.

Authorised tools are registered into the ordinary tool registry as
`mcp.<server>.<tool>`, so an agent calls an MCP tool the same way it calls
anything else. Refused tools are recorded with a reason:

```bash
orchestrator mcp
```

shows each server's status, transport, negotiated protocol version, registered
tools, and refused tools with reasons.

## An unreachable server

Never breaks startup. It is recorded as disconnected with the error, and its
tools are simply absent.

## Scoping an agent to specific servers

```yaml
agents:
  definitions:
    - id: researcher
      capabilities: [research]
      tools: ["mcp.*"]            # which tools
      permissions: ["mcp.invoke"]
      mcp_servers: [research]     # which servers - both must allow it
```

The two lists are independent checks. A broad tool glob does not grant access to
every server's tools: anything from a server outside `mcp_servers` is invisible
to the agent and refused if called.

## As a server

```bash
orchestrator mcp-serve                    # read-only surface
orchestrator mcp-serve --allow-control    # plus cancel and approval response
```

Exposed read-only: `start_execution`, `get_execution`, `list_executions`,
`get_artifacts`, `get_audit`, `describe_platform`.

Behind `--allow-control`: `respond_to_approval`, `cancel_execution`.

Nothing exposed can bypass a gate, grant a tool, or force completion.
