"""Recovery engine.

Turns a classified failure and a chosen strategy into concrete state changes:
retry the task, swap the agent or model, shrink the input, request a re-plan,
ask a human, or stop safely. The engine mutates the execution; it does not
decide policy and it does not call models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.domain.enums import (
    RecoveryStrategy,
    RiskLevel,
    TaskStatus,
    WaitReason,
)
from ..core.domain.models import (
    Approval,
    Execution,
    Failure,
    RecoveryAttempt,
    Task,
)
from ..core.state.manager import StateManager
from ..observability.audit import EventType
from .classification import to_failure, validation_failure
from .strategies import RecoveryPolicy, StrategyChoice, select


@dataclass
class RecoveryOutcome:
    """What the caller should do next."""

    attempt: RecoveryAttempt
    failure: Failure
    # The task is runnable again.
    retry: bool = False
    # The plan should be regenerated before continuing.
    replan: bool = False
    # Execution must wait for a human.
    escalate: bool = False
    # No further automatic action is possible.
    terminal: bool = False
    approval: Approval | None = None
    message: str = ""


class RecoveryEngine:
    def __init__(
        self,
        state: StateManager,
        *,
        policy: RecoveryPolicy | None = None,
    ) -> None:
        self.state = state
        self.policy = policy or RecoveryPolicy()

    # -- entry points ------------------------------------------------------

    def handle_exception(
        self,
        execution: Execution,
        task: Task | None,
        error: BaseException,
        **extra: Any,
    ) -> RecoveryOutcome:
        failure = to_failure(
            error,
            task_id=task.id if task else None,
            attempt=task.attempts if task else 0,
            extra=extra,
        )
        return self.handle(execution, task, failure)

    def handle_validation_failure(
        self, execution: Execution, task: Task, message: str, **details: Any
    ) -> RecoveryOutcome:
        failure = validation_failure(
            message, task_id=task.id, attempt=task.attempts, **details
        )
        return self.handle(execution, task, failure)

    def handle_objective_failure(
        self, execution: Execution, message: str, **details: Any
    ) -> RecoveryOutcome:
        """A failure of the objective as a whole, not of any single task."""
        failure = validation_failure(message, task_id=None, **details)
        return self.handle(execution, None, failure)

    def handle(
        self, execution: Execution, task: Task | None, failure: Failure
    ) -> RecoveryOutcome:
        choice = select(
            failure,
            task=task,
            history=execution.failures,
            policy=self.policy,
            limits=execution.limits,
            replans_used=execution.replans,
        )
        failure.recovery = choice.strategy
        execution.failures.append(failure)

        self.state.audit.record(
            EventType.FAILURE,
            execution_id=execution.id,
            task_id=failure.task_id,
            category=failure.category.value,
            code=failure.code,
            message=failure.message[:500],
            attempt=failure.attempt,
        )
        self.state.audit.record(
            EventType.RECOVERY_SELECTED,
            execution_id=execution.id,
            task_id=failure.task_id,
            strategy=choice.strategy.value,
            rationale=choice.rationale,
            exhausted=choice.exhausted,
        )

        outcome = self._apply(execution, task, failure, choice)
        execution.recoveries.append(outcome.attempt)
        self.state.audit.record(
            EventType.RECOVERY_RESULT,
            execution_id=execution.id,
            task_id=failure.task_id,
            strategy=choice.strategy.value,
            retry=outcome.retry,
            replan=outcome.replan,
            escalate=outcome.escalate,
            terminal=outcome.terminal,
        )
        return outcome

    # -- strategy application ---------------------------------------------

    def _apply(
        self,
        execution: Execution,
        task: Task | None,
        failure: Failure,
        choice: StrategyChoice,
    ) -> RecoveryOutcome:
        attempt = RecoveryAttempt(
            failure_id=failure.id,
            strategy=choice.strategy,
            rationale=choice.rationale,
        )
        strategy = choice.strategy

        if strategy is RecoveryStrategy.RETRY and task is not None:
            self._make_runnable(execution, task, "retrying after transient failure")
            attempt.succeeded = None
            return RecoveryOutcome(
                attempt=attempt, failure=failure, retry=True, message="retry"
            )

        if strategy is RecoveryStrategy.MODIFY_PARAMETERS and task is not None:
            # Record the failure so the next attempt sees what went wrong.
            task.inputs = {
                **task.inputs,
                "_previous_failure": {
                    "category": failure.category.value,
                    "message": failure.message[:1000],
                },
            }
            attempt.applied_changes = {"inputs._previous_failure": failure.code}
            self._make_runnable(execution, task, "retrying with adjusted inputs")
            return RecoveryOutcome(
                attempt=attempt, failure=failure, retry=True, message="modified inputs"
            )

        if strategy is RecoveryStrategy.ALTERNATE_TOOL and task is not None:
            failed_tool = failure.details.get("tool_id")
            if failed_tool and failed_tool in task.allowed_tools:
                task.allowed_tools = [t for t in task.allowed_tools if t != failed_tool]
                attempt.applied_changes = {"removed_tool": failed_tool}
            self._make_runnable(execution, task, "retrying without the failed tool")
            return RecoveryOutcome(
                attempt=attempt, failure=failure, retry=True, message="alternate tool"
            )

        if strategy is RecoveryStrategy.ALTERNATE_AGENT and task is not None:
            previous = task.assigned_agent
            task.metadata = {
                **task.metadata,
                "excluded_agents": sorted(
                    set(task.metadata.get("excluded_agents", []))
                    | ({previous} if previous else set())
                ),
            }
            task.assigned_agent = None
            attempt.applied_changes = {"excluded_agent": previous}
            self._make_runnable(execution, task, "retrying with a different agent")
            return RecoveryOutcome(
                attempt=attempt, failure=failure, retry=True, message="alternate agent"
            )

        if strategy is RecoveryStrategy.ALTERNATE_MODEL and task is not None:
            previous = task.assigned_model
            task.metadata = {
                **task.metadata,
                "excluded_models": sorted(
                    set(task.metadata.get("excluded_models", []))
                    | ({previous} if previous else set())
                ),
            }
            task.assigned_model = None
            attempt.applied_changes = {"excluded_model": previous}
            self._make_runnable(execution, task, "retrying with a different model")
            return RecoveryOutcome(
                attempt=attempt, failure=failure, retry=True, message="alternate model"
            )

        if strategy is RecoveryStrategy.REDUCE_SCOPE and task is not None:
            task.metadata = {**task.metadata, "reduced_scope": True}
            task.inputs = {
                key: value
                for key, value in task.inputs.items()
                if not key.startswith("_bulk")
            }
            attempt.applied_changes = {"reduced_scope": True}
            self._make_runnable(execution, task, "retrying with reduced scope")
            return RecoveryOutcome(
                attempt=attempt, failure=failure, retry=True, message="reduced scope"
            )

        if strategy is RecoveryStrategy.REPLAN:
            execution.replans += 1
            attempt.applied_changes = {"replan_count": execution.replans}
            if task is not None and not task.is_terminal:
                self.state.transition_task(
                    execution, task, TaskStatus.FAILED, reason="superseded by re-plan"
                )
            return RecoveryOutcome(
                attempt=attempt, failure=failure, replan=True, message="re-plan"
            )

        if strategy is RecoveryStrategy.ROLLBACK and task is not None:
            task.result = None
            task.validation_results = []
            self._make_runnable(execution, task, "rolled back and retrying")
            attempt.applied_changes = {"rolled_back": True}
            return RecoveryOutcome(
                attempt=attempt, failure=failure, retry=True, message="rollback"
            )

        if strategy is RecoveryStrategy.REQUEST_HUMAN_INPUT:
            approval = Approval(
                execution_id=execution.id,
                task_id=task.id if task else None,
                reason=WaitReason.DECISION,
                risk=RiskLevel.MEDIUM,
                prompt=(
                    f"Automatic recovery is exhausted for "
                    f"{'task ' + (task.name or task.id) if task else 'this execution'}.\n"
                    f"Failure: [{failure.category.value}] {failure.message[:500]}\n"
                    "Choose how to proceed."
                ),
                options=["retry", "skip", "abort", "provide_guidance"],
            )
            self.state.request_approval(execution, approval)
            if task is not None and not task.is_terminal:
                self.state.transition_task(
                    execution, task, TaskStatus.WAITING, reason="awaiting human decision"
                )
            return RecoveryOutcome(
                attempt=attempt,
                failure=failure,
                escalate=True,
                approval=approval,
                message="escalated to a human",
            )

        # TERMINATE, or a strategy that needs a task and did not get one.
        if task is not None and not task.is_terminal:
            self.state.transition_task(
                execution, task, TaskStatus.FAILED, reason=failure.message[:200]
            )
        attempt.succeeded = False
        return RecoveryOutcome(
            attempt=attempt,
            failure=failure,
            terminal=True,
            message="no further automatic recovery is available",
        )

    def _make_runnable(self, execution: Execution, task: Task, reason: str) -> None:
        if task.status is not TaskStatus.RECOVERING:
            self.state.transition_task(
                execution, task, TaskStatus.RECOVERING, reason=reason
            )
        self.state.transition_task(execution, task, TaskStatus.READY, reason=reason)

    def mark_recovered(self, execution: Execution, task: Task) -> None:
        """Close out the open failures for a task that has now succeeded."""
        for failure in execution.failures:
            if failure.task_id == task.id and not failure.recovered:
                failure.recovered = True
        for attempt in execution.recoveries:
            if attempt.succeeded is None:
                match = next(
                    (f for f in execution.failures if f.id == attempt.failure_id), None
                )
                if match is not None and match.task_id == task.id:
                    attempt.succeeded = True
