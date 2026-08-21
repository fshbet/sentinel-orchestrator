"""Universal AI Orchestration Platform.

A domain-agnostic, MCP-first orchestration engine. Describe the goal; the
platform decides how to decompose it, which capabilities it needs, what runs in
parallel, how results are validated, and when the objective is actually done.

The engine owns state, scheduling, permissions, validation, and limits
deterministically. Models are used for reasoning, planning, work, and judgement
-- never as the source of truth about workflow state.

Typical use::

    from orchestrator import Orchestrator

    platform = await Orchestrator.create()
    execution = await platform.run("Accomplish this objective.")
    print(execution.status, execution.summary)
"""

from .config.loader import Config, load as load_config
from .core.domain.enums import (
    Confidence,
    ExecutionStatus,
    OrchestrationPattern,
    PlanStrategy,
    RiskLevel,
    TaskStatus,
)
from .core.domain.models import (
    AgentSpec,
    Approval,
    Artifact,
    Capability,
    Evidence,
    Execution,
    ModelSpec,
    Plan,
    Requirements,
    ResourceLimits,
    Task,
    TaskResult,
    ToolSpec,
    ValidationResult,
    ValidationSpec,
)
from .errors import (
    ConfigurationError,
    InvalidStateTransition,
    InvalidWorkflow,
    OrchestratorError,
    PermissionDenied,
    PolicyViolation,
)
from .platform import Orchestrator

__version__ = "0.1.0"

__all__ = [
    "Orchestrator",
    "Config",
    "load_config",
    # Domain
    "Execution",
    "Task",
    "TaskResult",
    "Plan",
    "Requirements",
    "ResourceLimits",
    "AgentSpec",
    "Capability",
    "ToolSpec",
    "ModelSpec",
    "Artifact",
    "Evidence",
    "Approval",
    "ValidationSpec",
    "ValidationResult",
    # Enums
    "ExecutionStatus",
    "TaskStatus",
    "Confidence",
    "OrchestrationPattern",
    "PlanStrategy",
    "RiskLevel",
    # Errors
    "OrchestratorError",
    "ConfigurationError",
    "InvalidWorkflow",
    "InvalidStateTransition",
    "PolicyViolation",
    "PermissionDenied",
    "__version__",
]
