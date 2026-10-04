"""Enumerations for the core domain.

Every value is a lowercase string so persisted state and audit events stay
readable and stable across versions.
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    RECOVERING = "recovering"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED, ExecutionStatus.FAILED}
)


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    VALIDATING = "validating"
    RECOVERING = "recovering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
        TaskStatus.CANCELLED,
    }
)


class Confidence(StrEnum):
    """How sure the platform is that an outcome is what it claims to be."""

    CONFIRMED = "confirmed"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"
    FAILED = "failed"


class KnowledgeStatus(StrEnum):
    """Provenance marker required by the external-research policy."""

    KNOWN = "known"
    RETRIEVED = "retrieved"
    INFERRED = "inferred"
    ASSUMED = "assumed"
    UNVERIFIED = "unverified"


class FailureCategory(StrEnum):
    TRANSIENT = "transient"
    TOOL = "tool"
    MCP = "mcp"
    MODEL = "model"
    CONTEXT = "context"
    DEPENDENCY = "dependency"
    PERMISSION = "permission"
    VALIDATION = "validation"
    EXECUTION = "execution"
    LOGICAL = "logical"
    UNKNOWN = "unknown"


class RecoveryStrategy(StrEnum):
    RETRY = "retry"
    MODIFY_PARAMETERS = "modify_parameters"
    ALTERNATE_TOOL = "alternate_tool"
    ALTERNATE_AGENT = "alternate_agent"
    ALTERNATE_MODEL = "alternate_model"
    REPLAN = "replan"
    ROLLBACK = "rollback"
    REDUCE_SCOPE = "reduce_scope"
    REQUEST_HUMAN_INPUT = "request_human_input"
    TERMINATE = "terminate"


class RiskLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _RISK_ORDER[self]


_RISK_ORDER = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class WaitReason(StrEnum):
    APPROVAL = "approval"
    INPUT = "input"
    CREDENTIAL = "credential"
    DECISION = "decision"


class PlanStrategy(StrEnum):
    FULL = "full"
    ITERATIVE = "iterative"
    ADAPTIVE = "adaptive"


class OrchestrationPattern(StrEnum):
    SINGLE_AGENT = "single_agent"
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    ROUTER = "router"
    ORCHESTRATOR_WORKER = "orchestrator_worker"
    EVALUATOR_OPTIMIZER = "evaluator_optimizer"
    DYNAMIC_DAG = "dynamic_dag"
    HIERARCHICAL = "hierarchical"
    HANDOFF = "handoff"


class ToolSource(StrEnum):
    BUILTIN = "builtin"
    NATIVE = "native"
    PLUGIN = "plugin"
    MCP = "mcp"
    ADAPTER = "adapter"


class ModelCapability(StrEnum):
    TEXT_GENERATION = "text_generation"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    VISION = "vision"
    LONG_CONTEXT = "long_context"
    STREAMING = "streaming"
    REASONING = "reasoning"
    EMBEDDINGS = "embeddings"


class IsolationLevel(StrEnum):
    NONE = "none"
    RESTRICTED = "restricted"
    SANDBOX = "sandbox"
    CONTAINER = "container"
    REMOTE = "remote"


class EvidenceType(StrEnum):
    COMMAND = "command"
    TEST = "test"
    SCHEMA = "schema"
    API = "api"
    FILE = "file"
    MCP_TOOL = "mcp_tool"
    MODEL_JUDGEMENT = "model_judgement"
    HUMAN = "human"
    REFERENCE = "reference"
    DETERMINISTIC_CHECK = "deterministic_check"


class ArtifactType(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    TEXT = "text"
    JSON = "json"
    BINARY = "binary"
    REFERENCE = "reference"


class MemoryTier(StrEnum):
    WORKING = "working"
    LONG_TERM = "long_term"
    PROJECT = "project"


class ContextKind(StrEnum):
    """What a context item is, used for relevance ranking and eviction."""

    OBJECTIVE = "objective"
    CONSTRAINT = "constraint"
    TASK = "task"
    RESULT = "result"
    ARTIFACT_REF = "artifact_ref"
    MEMORY = "memory"
    HISTORY = "history"
    SUMMARY = "summary"
    TOOL_SPEC = "tool_spec"
