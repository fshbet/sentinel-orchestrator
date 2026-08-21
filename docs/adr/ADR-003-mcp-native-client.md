# ADR-003: A native MCP client rather than an SDK dependency

**Status:** accepted

## Context

MCP is the interoperability boundary the platform bets on. The obvious
implementation is the official SDK.

But the core is meant to be installable with no third-party dependencies, and an
SDK brings its own protocol assumptions, async model, and release cadence. MCP is
also the one place where the platform must not inherit someone else's trust
decisions, because trust is the whole point of the integration.

## Decision

Implement the client half of MCP natively over JSON-RPC 2.0
(`mcp/transport.py`, `mcp/client.py`): stdio and streamable HTTP transports,
version negotiation, capability discovery, tools, resources, prompts, tasks,
pagination, list caching, progress, cancellation, and health.

Protocol versions are **negotiated, not assumed**. The client offers the
revisions it knows, newest first, and adopts whatever the server answers with. A
server on an unrecognised revision is used on a best-effort basis and says so,
rather than being rejected or silently mishandled.

## Consequences

**Good.** MCP works in a dependency-free install (stdio needs nothing; HTTP needs
`httpx`). No inherited assumptions. Protocol handling is directly testable, and
the suite runs a real MCP server subprocess rather than a mock.

**Cost.** New protocol features must be implemented rather than picked up from an
SDK upgrade. Accepted: the surface actually used is small and stable, and
negotiation means an unknown revision degrades rather than breaks.

## Alternatives rejected

- **Official SDK as a hard dependency.** Contradicts the dependency posture.
- **Optional SDK with a native fallback.** Two code paths, two sets of bugs, and
  the fallback would be the under-tested one.
