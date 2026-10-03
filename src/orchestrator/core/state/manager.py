"""State manager.

The single choke point through which execution and task status may change.
Everything else in the platform asks the manager to move state; nothing mutates
``execution.status`` directly. Each transition is validated against the state
machine and recorded in the audit trail before it is persisted.
"""

from __future__ import annotations

from typing import Any

from ...errors import NotFound
from ...observability.audit import AuditLog, EventType
from ..domain.enums import (
    ApprovalStatus,
    Confidence,
    ExecutionStatus,
    TaskStatus,
    WaitReason,
)
from ..domain.models import Approval, Execution, ResourceLimits, Task, WorkflowRef
from ..domain.serde import utcnow
from .machine import (
    assert_execution_transition,
    assert_task_transition,
    is_terminal_execution,
)
from .store import StateStore


class StateManager:
    def __init__(self, store: StateStore, audit: AuditLog) -> None:
        self.store = store
        self.audit = audit

    # -- lifecycle ---------------------------------------------------------

    async def create_execution(
        self,
        objective: str,
        *,
        limits: ResourceLimits | None = None,
        workflow: WorkflowRef | None = None,
        context: dict[str, Any] | None = None,
        parent_execution_id: str | None = None,
    ) -> Execution:
        execution = Execution(
            objective=objective,
            limits=limits or ResourceLimits(),
            workflow=workflow or WorkflowRef(),
            context=dict(context or {}),
            parent_execution_id=parent_execution_id,
        )
        await self.store.create(execution)
        self.audit.record(
            EventType.EXECUTION_CREATED,
            execution_id=execution.id,
            objective=objective,
            workflow_version=execution.workflow.version,
        )
        await self.flush()
        return execution

    async def load(self, execution_id: str) -> Execution:
        return await self.store.get(execution_id)

    async def persist(self, execution: Execution) -> Execution:
        saved = await self.store.save(execution)
        await self.audit.flush()
        return saved

    async def flush(self) -> None:
        await self.audit.flush()

    # -- transitions -------------------------------------------------------

    def transition(
        self,
        execution: Execution,
        target: ExecutionStatus,
        *,
        reason: str = "",
        **payload: Any,
    ) -> Execution:
        current = execution.status
        assert_execution_transition(current, target, execution_id=execution.id)
        if current is not target:
            execution.status = target
            execution.updated_at = utcnow()
            self.audit.record(
                EventType.EXECUTION_TRANSITION,
                execution_id=execution.id,
                **{"from": current.value, "to": target.value, "reason": reason},
                **payload,
            )
        return execution

    def transition_task(
        self,
        execution: Execution,
        task: Task,
        target: TaskStatus,
        *,
        reason: str = "",
        **payload: Any,
    ) -> Task:
        current = task.status
        assert_task_transition(current, target, task_id=task.id)
        if current is not target:
            task.status = target
            if target is TaskStatus.RUNNING and task.started_at is None:
                task.started_at = utcnow()
            if target in (
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.SKIPPED,
                TaskStatus.CANCELLED,
            ):
                task.finished_at = utcnow()
            execution.updated_at = utcnow()
            self.audit.record(
                EventType.TASK_TRANSITION,
                execution_id=execution.id,
                task_id=task.id,
                **{"from": current.value, "to": target.value, "reason": reason},
                **payload,
            )
        return task

    # -- control -----------------------------------------------------------

    async def request_cancel(self, execution_id: str, *, reason: str = "") -> Execution:
        execution = await self.load(execution_id)
        if is_terminal_execution(execution.status):
            return execution
        execution.cancel_requested = True
        self.audit.record(
            "execution.cancel_requested", execution_id=execution.id, reason=reason
        )
        if execution.status in (
            ExecutionStatus.CREATED,
            ExecutionStatus.PAUSED,
            ExecutionStatus.WAITING,
        ):
            # Nothing is in flight, so stop immediately.
            self.transition(execution, ExecutionStatus.CANCELLING, reason=reason)
            for task in execution.tasks.values():
                if not task.is_terminal:
                    self.transition_task(
                        execution, task, TaskStatus.CANCELLED, reason="execution cancelled"
                    )
            self.transition(execution, ExecutionStatus.CANCELLED, reason=reason)
            execution.confidence = Confidence.BLOCKED
        return await self.persist(execution)

    async def request_pause(self, execution_id: str, *, reason: str = "") -> Execution:
        execution = await self.load(execution_id)
        if is_terminal_execution(execution.status):
            return execution
        execution.pause_requested = True
        self.audit.record(
            "execution.pause_requested", execution_id=execution.id, reason=reason
        )
        if execution.status in (ExecutionStatus.CREATED, ExecutionStatus.READY):
            self.transition(execution, ExecutionStatus.PAUSING, reason=reason)
            self.transition(execution, ExecutionStatus.PAUSED, reason=reason)
        return await self.persist(execution)

    # -- human in the loop -------------------------------------------------

    def request_approval(
        self,
        execution: Execution,
        approval: Approval,
    ) -> Approval:
        approval.execution_id = execution.id
        execution.approvals.append(approval)
        execution.wait_reason = approval.reason
        self.audit.record(
            EventType.APPROVAL_REQUESTED,
            execution_id=execution.id,
            task_id=approval.task_id,
            approval_id=approval.id,
            reason=approval.reason.value,
            risk=approval.risk.value,
            prompt=approval.prompt,
        )
        return approval

    async def resolve_approval(
        self,
        execution_id: str,
        approval_id: str,
        *,
        approved: bool,
        response: Any = None,
        responder: str | None = None,
    ) -> Execution:
        execution = await self.load(execution_id)
        for approval in execution.approvals:
            if approval.id == approval_id:
                break
        else:
            raise NotFound(
                f"approval {approval_id} not found on execution {execution_id}",
                approval_id=approval_id,
            )
        if approval.status is not ApprovalStatus.PENDING:
            return execution
        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.response = response
        approval.responder = responder
        approval.resolved_at = utcnow()
        if execution.pending_approval() is None:
            execution.wait_reason = None
        self.audit.record(
            EventType.APPROVAL_RESOLVED,
            execution_id=execution.id,
            task_id=approval.task_id,
            approval_id=approval.id,
            approved=approved,
            responder=responder,
        )
        return await self.persist(execution)

    def enter_wait(
        self, execution: Execution, reason: WaitReason, *, detail: str = ""
    ) -> Execution:
        execution.wait_reason = reason
        return self.transition(
            execution, ExecutionStatus.WAITING, reason=detail or reason.value
        )
