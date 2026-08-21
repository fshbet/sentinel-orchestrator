"""Structured error hierarchy shared by every subsystem.

Errors carry a machine-readable ``code`` so the recovery engine can classify a
failure without string-matching human prose.
"""

from __future__ import annotations

from typing import Any


class OrchestratorError(Exception):
    """Base class for every error raised by the platform."""

    code = "orchestrator_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigurationError(OrchestratorError):
    code = "configuration_error"


class ValidationSchemaError(OrchestratorError):
    """A payload failed schema/shape validation (not a task validator failure)."""

    code = "schema_error"


class InvalidStateTransition(OrchestratorError):
    code = "invalid_state_transition"


class InvalidWorkflow(OrchestratorError):
    """The task graph is structurally unusable (cycle, dangling dependency...)."""

    code = "invalid_workflow"


class PolicyViolation(OrchestratorError):
    """The policy engine refused an action."""

    code = "policy_violation"


class PermissionDenied(PolicyViolation):
    code = "permission_denied"


class ApprovalRequired(OrchestratorError):
    """Raised to suspend execution until a human decides."""

    code = "approval_required"

    def __init__(self, message: str, approval_id: str, **details: Any) -> None:
        super().__init__(message, approval_id=approval_id, **details)
        self.approval_id = approval_id


class ResourceLimitExceeded(OrchestratorError):
    code = "resource_limit_exceeded"


class ToolError(OrchestratorError):
    code = "tool_error"


class ToolNotFound(ToolError):
    code = "tool_not_found"


class ToolTimeout(ToolError):
    code = "tool_timeout"


class MCPError(OrchestratorError):
    code = "mcp_error"


class MCPTimeout(MCPError):
    code = "mcp_timeout"


class MCPProtocolError(MCPError):
    code = "mcp_protocol_error"


class ModelError(OrchestratorError):
    code = "model_error"


class ModelTimeout(ModelError):
    code = "model_timeout"


class ModelUnavailable(ModelError):
    code = "model_unavailable"


class ContextOverflow(OrchestratorError):
    code = "context_overflow"


class NoCapableAgent(OrchestratorError):
    code = "no_capable_agent"


class NoCapableModel(OrchestratorError):
    code = "no_capable_model"


class ExecutionCancelled(OrchestratorError):
    code = "execution_cancelled"


class NotFound(OrchestratorError):
    code = "not_found"
