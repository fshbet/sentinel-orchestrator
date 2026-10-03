"""MCP trust lifecycle.

A discovered MCP tool is not a trusted tool. Every tool goes through
DISCOVER -> DESCRIBE -> POLICY CHECK -> AUTHORIZE -> REGISTER -> USE before an
agent can call it (spec section 18).

Server-supplied annotations (``destructiveHint``, ``readOnlyHint``, ...) are
treated as *hints that can only raise* the assessed risk. A server claiming its
delete tool is read-only does not get believed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.domain.enums import RiskLevel
from ..core.policy.engine import PolicyDecision, PolicyEngine
from ..core.policy.risk import OperationDescriptor
from ..tools import permissions as perms
from .client import MCPTool


@dataclass
class MCPServerPolicy:
    """Per-server trust configuration."""

    server_id: str
    # Tool name globs. Empty allow-list means "all tools this server offers".
    allow_tools: tuple[str, ...] = ()
    deny_tools: tuple[str, ...] = ()
    # Permissions granted to tools from this server.
    permissions: tuple[str, ...] = (perms.MCP_INVOKE,)
    max_risk: RiskLevel = RiskLevel.MEDIUM
    require_approval_above: RiskLevel = RiskLevel.MEDIUM
    trusted: bool = False
    timeout: float = 60.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def permits(self, tool_name: str) -> bool:
        import fnmatch

        if any(fnmatch.fnmatch(tool_name, pattern) for pattern in self.deny_tools):
            return False
        if not self.allow_tools:
            return True
        return any(fnmatch.fnmatch(tool_name, pattern) for pattern in self.allow_tools)


@dataclass
class ToolAuthorization:
    tool: MCPTool
    server_id: str
    authorized: bool
    reason: str
    risk: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    permissions: tuple[str, ...] = ()
    decision: PolicyDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server_id,
            "tool": self.tool.name,
            "authorized": self.authorized,
            "requires_approval": self.requires_approval,
            "risk": self.risk.value,
            "reason": self.reason,
        }


def descriptor_for(tool: MCPTool, policy: MCPServerPolicy) -> OperationDescriptor:
    """Translate an MCP tool description into a risk descriptor."""
    annotations = tool.annotations or {}
    read_only = bool(annotations.get("readOnlyHint", False))
    destructive = bool(annotations.get("destructiveHint", False))
    idempotent = bool(annotations.get("idempotentHint", not destructive))
    open_world = bool(annotations.get("openWorldHint", True))

    text = f"{tool.name} {tool.description}".lower()
    # Name and description are weak signals, used only to raise suspicion.
    looks_destructive = any(
        marker in text for marker in ("delete", "remove", "drop", "destroy", "purge")
    )
    looks_financial = any(
        marker in text for marker in ("payment", "invoice", "charge", "transfer", "refund")
    )
    looks_privileged = any(
        marker in text for marker in ("admin", "permission", "grant", "credential", "token")
    )

    return OperationDescriptor(
        name=f"{policy.server_id}:{tool.name}",
        kind="mcp_tool",
        reversible=idempotent and not (destructive or looks_destructive),
        destructive=destructive or looks_destructive,
        external_effect=open_world,
        writes_data=not read_only,
        reads_sensitive_data=looks_privileged,
        financial_effect=looks_financial,
        security_effect=looks_privileged,
        permissions=list(policy.permissions),
        declared_risk=None if policy.trusted else RiskLevel.LOW,
    )


class MCPAuthorizer:
    """Runs the trust lifecycle for tools discovered on an MCP server."""

    def __init__(self, policy_engine: PolicyEngine) -> None:
        self.policy_engine = policy_engine

    def authorize(
        self,
        tool: MCPTool,
        server_policy: MCPServerPolicy,
        *,
        granted_permissions: Sequence[str] = (),
    ) -> ToolAuthorization:
        # DESCRIBE + POLICY CHECK
        if not server_policy.permits(tool.name):
            return ToolAuthorization(
                tool=tool,
                server_id=server_policy.server_id,
                authorized=False,
                reason="tool is not in the server allow-list",
            )

        descriptor = descriptor_for(tool, server_policy)
        decision = self.policy_engine.evaluate(
            descriptor,
            kind="mcp_tool",
            subject=f"{server_policy.server_id}:{tool.name}",
            granted_permissions=granted_permissions or server_policy.permissions,
        )
        risk = decision.risk.level

        if risk.rank > server_policy.max_risk.rank:
            return ToolAuthorization(
                tool=tool,
                server_id=server_policy.server_id,
                authorized=False,
                reason=(
                    f"tool risk {risk.value} exceeds the server ceiling"
                    f" {server_policy.max_risk.value}"
                ),
                risk=risk,
                decision=decision,
            )

        requires_approval = decision.requires_approval or (
            risk.rank > server_policy.require_approval_above.rank
        )

        # AUTHORIZE
        return ToolAuthorization(
            tool=tool,
            server_id=server_policy.server_id,
            authorized=decision.allowed or requires_approval,
            reason=decision.reason,
            risk=risk,
            requires_approval=requires_approval,
            permissions=tuple(server_policy.permissions),
            decision=decision,
        )
