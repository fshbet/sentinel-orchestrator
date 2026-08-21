"""Failure classification.

Failures are data, not exceptions to be swallowed (spec section 36). Every
failure is put into a category before anything decides what to do about it, and
the category comes from the structured error code rather than from parsing a
message.
"""

from __future__ import annotations

from typing import Any

from ..core.domain.enums import FailureCategory
from ..core.domain.models import Failure
from ..errors import (
    ApprovalRequired,
    ContextOverflow,
    ExecutionCancelled,
    InvalidStateTransition,
    InvalidWorkflow,
    MCPError,
    MCPTimeout,
    ModelError,
    ModelTimeout,
    ModelUnavailable,
    NoCapableAgent,
    NoCapableModel,
    OrchestratorError,
    PermissionDenied,
    PolicyViolation,
    ResourceLimitExceeded,
    ToolError,
    ToolNotFound,
    ToolTimeout,
)

# Error code -> category. Checked before the type hierarchy so a plugin can
# raise a plain OrchestratorError with a known code.
CODE_CATEGORIES: dict[str, FailureCategory] = {
    "tool_timeout": FailureCategory.TRANSIENT,
    "mcp_timeout": FailureCategory.TRANSIENT,
    "model_timeout": FailureCategory.TRANSIENT,
    "model_unavailable": FailureCategory.TRANSIENT,
    "tool_not_found": FailureCategory.TOOL,
    "tool_error": FailureCategory.TOOL,
    "mcp_error": FailureCategory.MCP,
    "mcp_protocol_error": FailureCategory.MCP,
    "model_error": FailureCategory.MODEL,
    "no_capable_model": FailureCategory.MODEL,
    "no_capable_agent": FailureCategory.DEPENDENCY,
    "context_overflow": FailureCategory.CONTEXT,
    "permission_denied": FailureCategory.PERMISSION,
    "policy_violation": FailureCategory.PERMISSION,
    "approval_required": FailureCategory.PERMISSION,
    "invalid_workflow": FailureCategory.LOGICAL,
    "invalid_state_transition": FailureCategory.LOGICAL,
    "resource_limit_exceeded": FailureCategory.EXECUTION,
    "validation_failed": FailureCategory.VALIDATION,
    "schema_error": FailureCategory.VALIDATION,
    "execution_cancelled": FailureCategory.EXECUTION,
}

TYPE_CATEGORIES: list[tuple[type, FailureCategory]] = [
    (ToolTimeout, FailureCategory.TRANSIENT),
    (MCPTimeout, FailureCategory.TRANSIENT),
    (ModelTimeout, FailureCategory.TRANSIENT),
    (ModelUnavailable, FailureCategory.TRANSIENT),
    (ApprovalRequired, FailureCategory.PERMISSION),
    (PermissionDenied, FailureCategory.PERMISSION),
    (PolicyViolation, FailureCategory.PERMISSION),
    (ToolNotFound, FailureCategory.TOOL),
    (ToolError, FailureCategory.TOOL),
    (MCPError, FailureCategory.MCP),
    (NoCapableModel, FailureCategory.MODEL),
    (ModelError, FailureCategory.MODEL),
    (ContextOverflow, FailureCategory.CONTEXT),
    (NoCapableAgent, FailureCategory.DEPENDENCY),
    (InvalidWorkflow, FailureCategory.LOGICAL),
    (InvalidStateTransition, FailureCategory.LOGICAL),
    (ResourceLimitExceeded, FailureCategory.EXECUTION),
    (ExecutionCancelled, FailureCategory.EXECUTION),
    (TimeoutError, FailureCategory.TRANSIENT),
    (ConnectionError, FailureCategory.TRANSIENT),
    (OSError, FailureCategory.EXECUTION),
]

# Categories where retrying the identical operation can plausibly help.
RETRYABLE = frozenset(
    {
        FailureCategory.TRANSIENT,
        FailureCategory.MCP,
        FailureCategory.MODEL,
        FailureCategory.TOOL,
    }
)

# Categories where retrying unchanged is pointless.
NOT_RETRYABLE = frozenset(
    {
        FailureCategory.PERMISSION,
        FailureCategory.LOGICAL,
        FailureCategory.VALIDATION,
    }
)


def classify(error: BaseException) -> FailureCategory:
    if isinstance(error, OrchestratorError):
        category = CODE_CATEGORIES.get(error.code)
        if category is not None:
            return category
    for error_type, category in TYPE_CATEGORIES:
        if isinstance(error, error_type):
            return category
    return FailureCategory.UNKNOWN


def to_failure(
    error: BaseException,
    *,
    task_id: str | None = None,
    attempt: int = 0,
    extra: dict[str, Any] | None = None,
) -> Failure:
    category = classify(error)
    if isinstance(error, OrchestratorError):
        code = error.code
        message = error.message
        details = dict(error.details)
    else:
        code = type(error).__name__
        message = str(error)
        details = {}
    details.update(extra or {})
    return Failure(
        category=category,
        code=code,
        message=message[:2000],
        task_id=task_id,
        attempt=attempt,
        details=details,
    )


def validation_failure(
    message: str, *, task_id: str | None = None, attempt: int = 0, **details: Any
) -> Failure:
    return Failure(
        category=FailureCategory.VALIDATION,
        code="validation_failed",
        message=message[:2000],
        task_id=task_id,
        attempt=attempt,
        details=details,
    )


def is_retryable(category: FailureCategory) -> bool:
    return category in RETRYABLE
