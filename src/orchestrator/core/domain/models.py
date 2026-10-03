"""The core domain model.

These dataclasses are the authoritative vocabulary of the platform. They are
deliberately free of any reference to a domain (software, research, finance),
to a model provider, to MCP, and to any execution backend.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from .enums import (
    ApprovalStatus,
    ArtifactType,
    Confidence,
    EvidenceType,
    ExecutionStatus,
    FailureCategory,
    IsolationLevel,
    KnowledgeStatus,
    ModelCapability,
    OrchestrationPattern,
    PlanStrategy,
    RecoveryStrategy,
    RiskLevel,
    TaskStatus,
    ToolSource,
    WaitReason,
)
from .ids import new_id
from .serde import from_dict, to_jsonable, utcnow


class DomainModel:
    """Mixin adding uniform dict conversion to every domain dataclass."""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        return from_dict(cls, data)

    def replace(self, **changes: Any):
        # The cast states what every subclass guarantees and the mixin
        # cannot: that `self` is a dataclass instance.
        return dataclasses.replace(cast("Any", self), **changes)


# --------------------------------------------------------------------------
# Requirements and success criteria
# --------------------------------------------------------------------------


@dataclass
class SuccessCriterion(DomainModel):
    """One condition that must hold for the objective to be considered met."""

    id: str = field(default_factory=lambda: new_id("crit"))
    description: str = ""
    # Name of a registered validator; None means no deterministic check exists.
    validator: str | None = None
    validator_config: dict[str, Any] = field(default_factory=dict)
    mandatory: bool = True


@dataclass
class Requirements(DomainModel):
    """Structured form of a natural-language objective (spec section 55)."""

    explicit: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    success_criteria: list[SuccessCriterion] = field(default_factory=list)
    # Requirement text -> provenance, so an assumption is never silently
    # promoted into a requirement.
    provenance: dict[str, KnowledgeStatus] = field(default_factory=dict)

    def is_ambiguous(self) -> bool:
        return bool(self.unknowns) and not self.explicit


# --------------------------------------------------------------------------
# Capabilities, agents, tools, models
# --------------------------------------------------------------------------


@dataclass
class CapabilityRequirements(DomainModel):
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    model_capabilities: list[ModelCapability] = field(default_factory=list)


@dataclass
class Capability(DomainModel):
    """A unit of ability an agent can advertise and a task can require."""

    id: str = ""
    description: str = ""
    requirements: CapabilityRequirements = field(default_factory=CapabilityRequirements)
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)


@dataclass
class AgentConstraints(DomainModel):
    max_iterations: int = 12
    max_tool_calls: int = 60
    max_model_calls: int = 30
    timeout_seconds: float = 900.0
    isolation: IsolationLevel = IsolationLevel.NONE


@dataclass
class AgentSpec(DomainModel):
    """A registered worker definition. Roles are data, never hard-coded types."""

    id: str = ""
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    # Model capabilities are preferred over model names (spec section 61).
    model_requirements: list[ModelCapability] = field(default_factory=list)
    model: str | None = None
    permissions: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    constraints: AgentConstraints = field(default_factory=AgentConstraints)
    runtime: str = "generic"
    version: str = "1.0.0"
    instructions: str = ""
    ephemeral: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec(DomainModel):
    id: str = ""
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    source: ToolSource = ToolSource.NATIVE
    source_ref: str | None = None
    risk: RiskLevel = RiskLevel.LOW
    timeout_seconds: float = 60.0
    max_retries: int = 2
    idempotent: bool = True
    version: str = "1.0.0"


@dataclass
class ToolCall(DomainModel):
    id: str = field(default_factory=lambda: new_id("call"))
    tool_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass
class ToolResult(DomainModel):
    call_id: str = ""
    tool_id: str = ""
    ok: bool = True
    output: Any = None
    error: dict[str, Any] | None = None
    duration_ms: float = 0.0
    started_at: datetime = field(default_factory=utcnow)


@dataclass
class ModelSpec(DomainModel):
    id: str = ""
    provider: str = ""
    model: str = ""
    capabilities: list[ModelCapability] = field(default_factory=list)
    context_window: int = 8192
    max_output_tokens: int = 2048
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    # Lower sorts first when several models satisfy the same requirements.
    priority: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)

    def supports(self, required: list[ModelCapability]) -> bool:
        return set(required).issubset(set(self.capabilities))


# --------------------------------------------------------------------------
# Evidence, validation, artifacts
# --------------------------------------------------------------------------


@dataclass
class Evidence(DomainModel):
    """Proof attached to a validation outcome (spec sections 33, 34, 78)."""

    id: str = field(default_factory=lambda: new_id("evd"))
    type: EvidenceType = EvidenceType.DETERMINISTIC_CHECK
    source: str = ""
    location: str | None = None
    summary: str = ""
    detail: Any = None
    confidence: Confidence = Confidence.CONFIRMED
    knowledge_status: KnowledgeStatus = KnowledgeStatus.KNOWN
    timestamp: datetime = field(default_factory=utcnow)


@dataclass
class ValidationSpec(DomainModel):
    """Declares how a task or criterion is to be checked."""

    id: str = field(default_factory=lambda: new_id("vspec"))
    validator: str = "noop"
    config: dict[str, Any] = field(default_factory=dict)
    mandatory: bool = True
    description: str = ""


@dataclass
class ValidationResult(DomainModel):
    id: str = field(default_factory=lambda: new_id("val"))
    spec_id: str = ""
    validator: str = ""
    target_id: str = ""
    passed: bool = False
    confidence: Confidence = Confidence.UNCERTAIN
    message: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    mandatory: bool = True
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Artifact(DomainModel):
    id: str = field(default_factory=lambda: new_id("art"))
    type: ArtifactType = ArtifactType.TEXT
    name: str = ""
    location: str = ""
    content: Any = None
    media_type: str | None = None
    size_bytes: int | None = None
    # SHA-256 of the bytes actually written, so a validator can prove the file
    # on disk is the artifact this record describes rather than trusting that
    # a path exists.
    checksum: str | None = None
    produced_by: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Failures, recovery, approvals
# --------------------------------------------------------------------------


@dataclass
class Failure(DomainModel):
    id: str = field(default_factory=lambda: new_id("fail"))
    category: FailureCategory = FailureCategory.UNKNOWN
    code: str = ""
    message: str = ""
    task_id: str | None = None
    attempt: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    recovery: RecoveryStrategy | None = None
    recovered: bool = False
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class RecoveryAttempt(DomainModel):
    id: str = field(default_factory=lambda: new_id("rec"))
    failure_id: str = ""
    strategy: RecoveryStrategy = RecoveryStrategy.RETRY
    rationale: str = ""
    applied_changes: dict[str, Any] = field(default_factory=dict)
    succeeded: bool | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Approval(DomainModel):
    """A first-class pause point requiring a human decision."""

    id: str = field(default_factory=lambda: new_id("apr"))
    execution_id: str = ""
    task_id: str | None = None
    reason: WaitReason = WaitReason.APPROVAL
    prompt: str = ""
    risk: RiskLevel = RiskLevel.MEDIUM
    options: list[str] = field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    response: Any = None
    responder: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    resolved_at: datetime | None = None


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


@dataclass
class ResourceLimits(DomainModel):
    """Hard ceilings enforced deterministically, never by the model."""

    max_wall_seconds: float = 3600.0
    max_model_calls: int = 300
    max_tool_calls: int = 1000
    max_tokens: int = 2000000
    max_cost: float | None = None
    max_parallel_tasks: int = 4
    max_task_attempts: int = 3
    max_replans: int = 3
    max_optimizer_iterations: int = 3
    max_external_requests: int = 500


@dataclass
class Usage(DomainModel):
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    external_requests: int = 0
    wall_seconds: float = 0.0

    def add(self, other: Usage) -> Usage:
        return Usage(
            model_calls=self.model_calls + other.model_calls,
            tool_calls=self.tool_calls + other.tool_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost=self.cost + other.cost,
            external_requests=self.external_requests + other.external_requests,
            wall_seconds=self.wall_seconds + other.wall_seconds,
        )


@dataclass
class TaskResult(DomainModel):
    """Structured hand-off between agents (spec section 43)."""

    task_id: str = ""
    ok: bool = True
    summary: str = ""
    output: Any = None
    confidence: Confidence = Confidence.UNCERTAIN
    artifacts: list[Artifact] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    messages: list[dict[str, Any]] = field(default_factory=list)
    handoff_to: str | None = None
    error: dict[str, Any] | None = None


@dataclass
class Task(DomainModel):
    """A node in the dynamic task graph."""

    id: str = field(default_factory=lambda: new_id("tsk"))
    execution_id: str = ""
    name: str = ""
    objective: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    expected_outputs: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    model_requirements: list[ModelCapability] = field(default_factory=list)
    validations: list[ValidationSpec] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)
    # Named resources this task mutates; the scheduler locks them.
    resources: list[str] = field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False

    status: TaskStatus = TaskStatus.PENDING
    assigned_agent: str | None = None
    assigned_model: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    parent_id: str | None = None
    group: str | None = None
    pattern: OrchestrationPattern = OrchestrationPattern.SINGLE_AGENT

    result: TaskResult | None = None
    validation_results: list[ValidationResult] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        from .enums import TERMINAL_TASK_STATUSES

        return self.status in TERMINAL_TASK_STATUSES


# --------------------------------------------------------------------------
# Plan and execution
# --------------------------------------------------------------------------


@dataclass
class Plan(DomainModel):
    id: str = field(default_factory=lambda: new_id("plan"))
    execution_id: str = ""
    version: int = 1
    strategy: PlanStrategy = PlanStrategy.ADAPTIVE
    pattern: OrchestrationPattern = OrchestrationPattern.DYNAMIC_DAG
    rationale: str = ""
    tasks: list[Task] = field(default_factory=list)
    # False when an iterative planner intends to extend the graph later.
    complete: bool = True
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class WorkflowRef(DomainModel):
    """Pinned definition versions, so a running execution never shifts under us."""

    id: str = "dynamic"
    version: str = "1.0.0"
    agent_versions: dict[str, str] = field(default_factory=dict)
    skill_versions: dict[str, str] = field(default_factory=dict)
    policy_version: str = "1.0.0"
    config_fingerprint: str = ""


@dataclass
class Execution(DomainModel):
    """The authoritative aggregate root. State lives here, not in the model."""

    id: str = field(default_factory=lambda: new_id("exe"))
    objective: str = ""
    status: ExecutionStatus = ExecutionStatus.CREATED
    requirements: Requirements = field(default_factory=Requirements)
    workflow: WorkflowRef = field(default_factory=WorkflowRef)
    plan: Plan | None = None
    plan_version: int = 0
    tasks: dict[str, Task] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    validations: list[ValidationResult] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    recoveries: list[RecoveryAttempt] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    usage: Usage = field(default_factory=Usage)
    confidence: Confidence = Confidence.UNCERTAIN
    summary: str = ""
    wait_reason: WaitReason | None = None
    cancel_requested: bool = False
    pause_requested: bool = False
    replans: int = 0
    parent_execution_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    revision: int = 0

    def task(self, task_id: str) -> Task:
        try:
            return self.tasks[task_id]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(f"unknown task {task_id}") from exc

    def pending_approval(self) -> Approval | None:
        for approval in self.approvals:
            if approval.status is ApprovalStatus.PENDING:
                return approval
        return None


@dataclass
class AuditEvent(DomainModel):
    """Append-only structured record. Never contains secrets."""

    id: str = field(default_factory=lambda: new_id("aud"))
    execution_id: str = ""
    sequence: int = 0
    type: str = ""
    task_id: str | None = None
    actor: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utcnow)
