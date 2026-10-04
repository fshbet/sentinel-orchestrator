"""Recovery strategy selection.

Given a classified failure and how many times it has already happened, pick the
next thing to try. The ladder always ends at human escalation or a safe stop,
never at an unbounded retry loop (spec sections 37, 95, 98).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..core.domain.enums import FailureCategory, RecoveryStrategy
from ..core.domain.models import Failure, ResourceLimits, Task

# Ordered ladders per category. Position in the list is the attempt number.
LADDERS: dict[FailureCategory, tuple[RecoveryStrategy, ...]] = {
    FailureCategory.TRANSIENT: (
        RecoveryStrategy.RETRY,
        RecoveryStrategy.RETRY,
        RecoveryStrategy.ALTERNATE_MODEL,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
    FailureCategory.TOOL: (
        RecoveryStrategy.MODIFY_PARAMETERS,
        RecoveryStrategy.ALTERNATE_TOOL,
        RecoveryStrategy.REPLAN,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
    FailureCategory.MCP: (
        RecoveryStrategy.RETRY,
        RecoveryStrategy.ALTERNATE_TOOL,
        RecoveryStrategy.REPLAN,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
    FailureCategory.MODEL: (
        RecoveryStrategy.ALTERNATE_MODEL,
        RecoveryStrategy.MODIFY_PARAMETERS,
        RecoveryStrategy.ALTERNATE_AGENT,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
    FailureCategory.CONTEXT: (
        # Shrinking the input is the fix; retrying the same prompt is not.
        RecoveryStrategy.REDUCE_SCOPE,
        RecoveryStrategy.ALTERNATE_MODEL,
        RecoveryStrategy.REPLAN,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
    FailureCategory.DEPENDENCY: (
        RecoveryStrategy.ALTERNATE_AGENT,
        RecoveryStrategy.REPLAN,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
    FailureCategory.PERMISSION: (
        # Never silently work around a refusal: ask.
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
        RecoveryStrategy.REDUCE_SCOPE,
        RecoveryStrategy.TERMINATE,
    ),
    FailureCategory.VALIDATION: (
        RecoveryStrategy.MODIFY_PARAMETERS,
        RecoveryStrategy.ALTERNATE_AGENT,
        RecoveryStrategy.REPLAN,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
    FailureCategory.EXECUTION: (
        RecoveryStrategy.RETRY,
        RecoveryStrategy.REDUCE_SCOPE,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
    FailureCategory.LOGICAL: (
        RecoveryStrategy.REPLAN,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
        RecoveryStrategy.TERMINATE,
    ),
    FailureCategory.UNKNOWN: (
        RecoveryStrategy.RETRY,
        RecoveryStrategy.REPLAN,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
    ),
}

TERMINAL_STRATEGIES = frozenset(
    {RecoveryStrategy.TERMINATE, RecoveryStrategy.REQUEST_HUMAN_INPUT}
)

# Strategies that only mean something when a specific task failed. An
# execution-level failure (an objective gate, for example) can only re-plan,
# escalate, or stop.
TASK_SCOPED = frozenset(
    {
        RecoveryStrategy.RETRY,
        RecoveryStrategy.MODIFY_PARAMETERS,
        RecoveryStrategy.ALTERNATE_TOOL,
        RecoveryStrategy.ALTERNATE_AGENT,
        RecoveryStrategy.ALTERNATE_MODEL,
        RecoveryStrategy.REDUCE_SCOPE,
        RecoveryStrategy.ROLLBACK,
    }
)


@dataclass
class RecoveryPolicy:
    """Which strategies are permitted, and how far the ladder may be climbed."""

    allow_replan: bool = True
    allow_dynamic_agents: bool = True
    allow_human_escalation: bool = True
    max_attempts_per_task: int = 3
    max_replans: int = 3
    disabled: frozenset[RecoveryStrategy] = field(default_factory=frozenset)

    def permits(self, strategy: RecoveryStrategy) -> bool:
        if strategy in self.disabled:
            return False
        if strategy is RecoveryStrategy.REPLAN and not self.allow_replan:
            return False
        if strategy is RecoveryStrategy.ALTERNATE_AGENT and not self.allow_dynamic_agents:
            return False
        if (
            strategy is RecoveryStrategy.REQUEST_HUMAN_INPUT
            and not self.allow_human_escalation
        ):
            return False
        return True


@dataclass
class StrategyChoice:
    strategy: RecoveryStrategy
    rationale: str
    exhausted: bool = False


def previous_strategies(
    failures: Sequence[Failure], task_id: str | None
) -> list[RecoveryStrategy]:
    return [
        failure.recovery
        for failure in failures
        if failure.task_id == task_id and failure.recovery is not None
    ]


def select(
    failure: Failure,
    *,
    task: Task | None = None,
    history: Sequence[Failure] = (),
    policy: RecoveryPolicy | None = None,
    limits: ResourceLimits | None = None,
    replans_used: int = 0,
) -> StrategyChoice:
    """Choose the next recovery strategy for a failure."""
    policy = policy or RecoveryPolicy()
    limits = limits or ResourceLimits()
    ladder = LADDERS.get(failure.category, LADDERS[FailureCategory.UNKNOWN])

    attempts = len(previous_strategies(history, failure.task_id))
    max_attempts = min(
        policy.max_attempts_per_task,
        task.max_attempts if task is not None else policy.max_attempts_per_task,
        limits.max_task_attempts,
    )

    if attempts >= max_attempts:
        return _escalate(
            policy,
            f"{attempts} recovery attempts already made for this task"
            f" (limit {max_attempts})",
        )

    for index in range(attempts, len(ladder)):
        candidate = ladder[index]
        if not policy.permits(candidate):
            continue
        if task is None and candidate in TASK_SCOPED:
            continue
        if candidate is RecoveryStrategy.REPLAN and (
            replans_used >= min(policy.max_replans, limits.max_replans)
        ):
            continue
        return StrategyChoice(
            strategy=candidate,
            rationale=(
                f"{failure.category.value} failure, attempt {attempts + 1}"
                f" of {max_attempts}: {candidate.value}"
            ),
        )

    return _escalate(policy, "the recovery ladder for this failure type is exhausted")


def _escalate(policy: RecoveryPolicy, reason: str) -> StrategyChoice:
    if policy.allow_human_escalation:
        return StrategyChoice(
            strategy=RecoveryStrategy.REQUEST_HUMAN_INPUT,
            rationale=f"escalating to a human: {reason}",
            exhausted=True,
        )
    return StrategyChoice(
        strategy=RecoveryStrategy.TERMINATE,
        rationale=f"stopping safely: {reason}",
        exhausted=True,
    )
