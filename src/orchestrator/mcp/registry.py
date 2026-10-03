"""MCP server registry and tool bridge.

Connects configured servers, runs the trust lifecycle over what they advertise,
and registers the authorised tools into the platform's single tool registry so
an agent calls an MCP tool exactly the way it calls any other tool.

MCP is a capability boundary here, not the workflow engine (spec section 104).
Nothing in the orchestration core imports this module.
"""

from __future__ import annotations

import asyncio

# `builtins` is imported because the registries below expose a public
# `list()` method, which shadows the builtin inside their own class body.
# `-> builtins.list[X]` is the annotation that keeps the method name.
import builtins
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.domain.enums import RiskLevel, ToolSource
from ..core.domain.models import ToolSpec
from ..core.policy.engine import PolicyEngine
from ..errors import MCPError, NotFound, ToolError
from ..observability.audit import AuditLog, EventType
from ..tools.registry import ToolContext, ToolRegistry
from .client import MCPClient, MCPTool
from .policy import MCPAuthorizer, MCPServerPolicy, ToolAuthorization

# Tool ids are namespaced so two servers offering "search" do not collide.
TOOL_ID_TEMPLATE = "mcp.{server}.{tool}"


@dataclass
class ServerRecord:
    server_id: str
    config: dict[str, Any]
    policy: MCPServerPolicy
    client: MCPClient | None = None
    tools: list[MCPTool] = field(default_factory=list)
    authorizations: list[ToolAuthorization] = field(default_factory=list)
    registered_tool_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.initialized

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server_id,
            "connected": self.connected,
            "transport": self.config.get("transport")
            or ("http" if self.config.get("url") else "stdio"),
            "protocol_version": self.client.protocol_version if self.client else None,
            "tools_offered": [t.name for t in self.tools],
            "tools_registered": list(self.registered_tool_ids),
            "tools_denied": [a.to_dict() for a in self.authorizations if not a.authorized],
            "error": self.error,
        }


class MCPRegistry:
    """Owns MCP connections and their bridged tools."""

    def __init__(
        self,
        tools: ToolRegistry,
        *,
        policy_engine: PolicyEngine,
        audit: AuditLog | None = None,
    ) -> None:
        self.tools = tools
        self.audit = audit
        self.authorizer = MCPAuthorizer(policy_engine)
        self._servers: dict[str, ServerRecord] = {}

    # -- configuration -----------------------------------------------------

    def configure(
        self, server_id: str, config: dict[str, Any], policy: MCPServerPolicy | None = None
    ) -> ServerRecord:
        record = ServerRecord(
            server_id=server_id,
            config=dict(config),
            policy=policy or MCPServerPolicy(server_id=server_id),
        )
        self._servers[server_id] = record
        return record

    def configure_many(
        self,
        servers: dict[str, dict[str, Any]],
        policies: dict[str, MCPServerPolicy] | None = None,
    ) -> list[ServerRecord]:
        policies = policies or {}
        return [
            self.configure(server_id, config, policies.get(server_id))
            for server_id, config in servers.items()
        ]

    def get(self, server_id: str) -> ServerRecord:
        try:
            return self._servers[server_id]
        except KeyError as exc:
            raise NotFound(
                f"MCP server {server_id} is not configured", id=server_id
            ) from exc

    def list(self) -> list[ServerRecord]:
        return sorted(self._servers.values(), key=lambda r: r.server_id)

    # -- connection lifecycle ---------------------------------------------

    async def connect(self, server_id: str) -> ServerRecord:
        """DISCOVER, DESCRIBE, POLICY CHECK, AUTHORIZE, REGISTER."""
        record = self.get(server_id)
        if record.connected:
            return record
        try:
            record.client = await MCPClient.connect(server_id, record.config)
            record.error = None
        except Exception as exc:  # noqa: BLE001 - a bad server must not kill the run
            record.error = str(exc)
            self._audit(
                EventType.MCP_ERROR, server=server_id, phase="connect", error=str(exc)
            )
            return record

        self._audit(
            EventType.MCP_DISCOVERED,
            server=server_id,
            protocol_version=record.client.protocol_version,
            capabilities=sorted(record.client.capabilities),
            server_info=record.client.server_info,
        )
        await self.refresh(server_id)
        return record

    async def connect_all(self) -> builtins.list[ServerRecord]:
        await asyncio.gather(
            *(self.connect(server_id) for server_id in list(self._servers)),
            return_exceptions=True,
        )
        return self.list()

    async def refresh(self, server_id: str) -> ServerRecord:
        """Re-discover tools and re-run authorisation."""
        record = self.get(server_id)
        if record.client is None:
            raise MCPError(f"MCP server {server_id} is not connected", server=server_id)

        record.client.invalidate_cache()
        record.tools = await record.client.list_tools(use_cache=False)
        self.tools.unregister_source(ToolSource.MCP, server_id)
        record.registered_tool_ids = []
        record.authorizations = []

        for tool in record.tools:
            authorization = self.authorizer.authorize(tool, record.policy)
            record.authorizations.append(authorization)
            if not authorization.authorized:
                self._audit(
                    EventType.MCP_DENIED,
                    server=server_id,
                    tool=tool.name,
                    reason=authorization.reason,
                    risk=authorization.risk.value,
                )
                continue
            tool_id = self._register_tool(record, tool, authorization)
            record.registered_tool_ids.append(tool_id)
            self._audit(
                EventType.MCP_REGISTERED,
                server=server_id,
                tool=tool.name,
                tool_id=tool_id,
                risk=authorization.risk.value,
                requires_approval=authorization.requires_approval,
            )
        return record

    async def disconnect(self, server_id: str) -> None:
        record = self.get(server_id)
        self.tools.unregister_source(ToolSource.MCP, server_id)
        record.registered_tool_ids = []
        if record.client is not None:
            await record.client.close()
            record.client = None

    async def close(self) -> None:
        await asyncio.gather(
            *(self.disconnect(server_id) for server_id in list(self._servers)),
            return_exceptions=True,
        )

    # -- bridging ----------------------------------------------------------

    def _register_tool(
        self, record: ServerRecord, tool: MCPTool, authorization: ToolAuthorization
    ) -> str:
        tool_id = TOOL_ID_TEMPLATE.format(server=record.server_id, tool=tool.name)
        annotations = tool.annotations or {}

        async def handler(
            arguments: dict[str, Any], context: ToolContext, _tool=tool, _record=record
        ) -> Any:
            client = _record.client
            if client is None:
                raise MCPError(
                    f"MCP server {_record.server_id} is not connected",
                    server=_record.server_id,
                )
            self._audit(
                EventType.MCP_CALL,
                server=_record.server_id,
                tool=_tool.name,
                execution_id=context.execution_id,
                task_id=context.task_id,
            )
            result = await client.call_tool(
                _tool.name, arguments, timeout=_record.policy.timeout
            )
            if result.is_error:
                raise ToolError(
                    f"MCP tool {_tool.name} reported an error: {result.text()[:400]}",
                    tool_id=tool_id,
                    server=_record.server_id,
                )
            return result.value()

        spec = ToolSpec(
            id=tool_id,
            name=tool.name,
            description=tool.description or tool.title,
            input_schema=tool.input_schema or {"type": "object", "properties": {}},
            output_schema=tool.output_schema or {},
            permissions=list(authorization.permissions),
            source=ToolSource.MCP,
            source_ref=record.server_id,
            risk=authorization.risk if authorization.risk is not None else RiskLevel.LOW,
            timeout_seconds=record.policy.timeout,
            idempotent=bool(annotations.get("idempotentHint", True)),
            max_retries=1,
        )
        self.tools.register(spec, handler)
        return tool_id

    # -- introspection -----------------------------------------------------

    async def health(self) -> builtins.list[dict[str, Any]]:
        reports = []
        for record in self.list():
            if record.client is None:
                reports.append(
                    {
                        "server": record.server_id,
                        "status": "disconnected",
                        "error": record.error,
                        "transport": record.config.get("transport")
                        or ("http" if record.config.get("url") else "stdio"),
                    }
                )
                continue
            report = await record.client.health()
            report["tools_registered"] = list(record.registered_tool_ids)
            report["tools_denied"] = [
                a.to_dict() for a in record.authorizations if not a.authorized
            ]
            reports.append(report)
        return reports

    def registered_tools(self) -> builtins.list[ToolSpec]:
        return self.tools.list(source=ToolSource.MCP)

    def authorizations(self) -> builtins.list[ToolAuthorization]:
        return [a for record in self.list() for a in record.authorizations]

    def _audit(self, event: str, **payload: Any) -> None:
        if self.audit is not None:
            self.audit.record(event, **payload)


def policies_from_config(raw: Iterable[dict[str, Any]]) -> dict[str, MCPServerPolicy]:
    """Build per-server policies from configuration blocks."""
    policies: dict[str, MCPServerPolicy] = {}
    for entry in raw:
        server_id = str(entry.get("server", ""))
        if not server_id:
            continue
        policies[server_id] = MCPServerPolicy(
            server_id=server_id,
            allow_tools=tuple(entry.get("allow_tools", ())),
            deny_tools=tuple(entry.get("deny_tools", ())),
            permissions=tuple(entry.get("permissions", ("mcp.invoke",))),
            max_risk=RiskLevel(entry.get("max_risk", "medium")),
            require_approval_above=RiskLevel(entry.get("require_approval_above", "medium")),
            trusted=bool(entry.get("trusted", False)),
            timeout=float(entry.get("timeout", 60.0)),
        )
    return policies
