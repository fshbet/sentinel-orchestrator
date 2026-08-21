"""Explicit state machines for executions and tasks.

The transition tables here are the only place a status may change. An illegal
transition raises rather than being silently coerced, which is what makes
"a failed mandatory gate can never yield COMPLETED" an enforceable invariant
rather than a hope about model behaviour (spec sections 26 and 73).
"""

from __future__ import annotations

from ..domain.enums import (
    TERMINAL_EXECUTION_STATUSES,
    TERMINAL_TASK_STATUSES,
    ExecutionStatus,
    TaskStatus,
)
from ...errors import InvalidStateTransition

# Execution lifecycle. Read as: from -> allowed next states.
EXECUTION_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.CREATED: frozenset(
        {
            ExecutionStatus.PLANNING,
            ExecutionStatus.WAITING,
            ExecutionStatus.PAUSING,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.PLANNING: frozenset(
        {
            ExecutionStatus.READY,
            ExecutionStatus.WAITING,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.PAUSING,
        }
    ),
    ExecutionStatus.READY: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.PLANNING,
            ExecutionStatus.WAITING,
            ExecutionStatus.PAUSING,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.VALIDATING,
            ExecutionStatus.WAITING,
            ExecutionStatus.RECOVERING,
            ExecutionStatus.PLANNING,
            ExecutionStatus.PAUSING,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.READY,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.WAITING: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.READY,
            ExecutionStatus.PLANNING,
            ExecutionStatus.RECOVERING,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.VALIDATING: frozenset(
        {
            ExecutionStatus.REVIEWING,
            ExecutionStatus.RECOVERING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.READY,
            # A failed objective gate can send the whole plan back for revision.
            ExecutionStatus.PLANNING,
            ExecutionStatus.WAITING,
            ExecutionStatus.PAUSING,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.REVIEWING: frozenset(
        {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.RECOVERING,
            ExecutionStatus.PLANNING,
            ExecutionStatus.WAITING,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.RECOVERING: frozenset(
        {
            ExecutionStatus.RUNNING,
            ExecutionStatus.READY,
            ExecutionStatus.PLANNING,
            ExecutionStatus.WAITING,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.FAILED,
        }
    ),
    ExecutionStatus.PAUSING: frozenset(
        {ExecutionStatus.PAUSED, ExecutionStatus.CANCELLING, ExecutionStatus.FAILED}
    ),
    ExecutionStatus.PAUSED: frozenset(
        {
            ExecutionStatus.READY,
            ExecutionStatus.RUNNING,
            ExecutionStatus.PLANNING,
            ExecutionStatus.CANCELLING,
            ExecutionStatus.CANCELLED,
        }
    ),
    ExecutionStatus.CANCELLING: frozenset({ExecutionStatus.CANCELLED}),
    # Terminal states have no outgoing edges at all.
    ExecutionStatus.CANCELLED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.COMPLETED: frozenset(),
}

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.RECOVERING,
            TaskStatus.SKIPPED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING,
            TaskStatus.PENDING,
            TaskStatus.RECOVERING,
            TaskStatus.SKIPPED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.VALIDATING,
            TaskStatus.WAITING,
            TaskStatus.RECOVERING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.READY,
            TaskStatus.RECOVERING,
            TaskStatus.SKIPPED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.VALIDATING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.RECOVERING,
            TaskStatus.FAILED,
            TaskStatus.WAITING,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RECOVERING: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.WAITING,
            TaskStatus.FAILED,
            TaskStatus.SKIPPED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.SKIPPED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def can_transition_execution(current: ExecutionStatus, target: ExecutionStatus) -> bool:
    if current is target:
        return True
    return target in EXECUTION_TRANSITIONS.get(current, frozenset())


def can_transition_task(current: TaskStatus, target: TaskStatus) -> bool:
    if current is target:
        return True
    return target in TASK_TRANSITIONS.get(current, frozenset())


def assert_execution_transition(
    current: ExecutionStatus, target: ExecutionStatus, *, execution_id: str = ""
) -> None:
    if not can_transition_execution(current, target):
        raise InvalidStateTransition(
            f"execution cannot move from {current.value} to {target.value}",
            execution_id=execution_id,
            current=current.value,
            target=target.value,
        )


def assert_task_transition(
    current: TaskStatus, target: TaskStatus, *, task_id: str = ""
) -> None:
    if not can_transition_task(current, target):
        raise InvalidStateTransition(
            f"task cannot move from {current.value} to {target.value}",
            task_id=task_id,
            current=current.value,
            target=target.value,
        )


def is_terminal_execution(status: ExecutionStatus) -> bool:
    return status in TERMINAL_EXECUTION_STATUSES


def is_terminal_task(status: TaskStatus) -> bool:
    return status in TERMINAL_TASK_STATUSES
