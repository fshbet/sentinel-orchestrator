"""Sub-orchestration.

A task can be a whole orchestrated run of its own: it gets its own goal
analysis, plan, task graph, agents, and gates, and reports back a single result
(spec section 45).

This is the difference between the hierarchical *pattern* — parent and child
tasks in one graph — and genuine nesting. Nesting is what a task needs when its
work cannot be decomposed until it starts.

Two things make it safe:

* **Budget is carved out of the parent, not added to it.** A child receives a
  share of what the parent has left, so nesting cannot multiply cost.
* **Depth is bounded.** A sub-orchestration that spawns a sub-orchestration
  stops at a configured depth rather than recursing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...agents.runtime import AgentRunContext, BaseRuntime
from ...observability.logging import get_logger
from ..domain.enums import Confidence, ExecutionStatus, IsolationLevel
from ..domain.models import ResourceLimits, TaskResult, Usage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .engine import ExecutionEngine

_logger = get_logger("nested")

RUNTIME_NAME = "sub_orchestrator"
DEPTH_KEY = "_nesting_depth"

# What fraction of the parent's remaining budget a child may consume.
DEFAULT_BUDGET_SHARE = 0.5


class SubOrchestrationRuntime(BaseRuntime):
    """Executes a task by running a child execution of the whole engine."""

    name = RUNTIME_NAME
    # The child execution applies the same scoping and confinement the parent
    # does, so it can honour the same levels the generic runtime can.
    supported_isolation = frozenset({IsolationLevel.NONE, IsolationLevel.RESTRICTED})

    def __init__(
        self,
        engine: "ExecutionEngine",
        *,
        max_depth: int = 2,
        budget_share: float = DEFAULT_BUDGET_SHARE,
    ) -> None:
        self.engine = engine
        self.max_depth = max_depth
        self.budget_share = budget_share

    async def run(self, context: AgentRunContext) -> TaskResult:
        parent = context.execution
        task = context.task
        depth = int(parent.context.get(DEPTH_KEY, 0)) + 1

        if depth > self.max_depth:
            return self._failure(
                task,
                f"sub-orchestration would exceed the nesting depth limit of"
                f" {self.max_depth}",
                depth=depth,
            )

        child = await self.engine.start(
            self._objective(context),
            limits=self._child_limits(parent),
            context={
                **{k: v for k, v in parent.context.items() if not k.startswith("forced_")},
                DEPTH_KEY: depth,
                "_parent_task": task.id,
            },
            parent_execution_id=parent.id,
        )
        self.engine.audit.record(
            "sub_execution.started",
            execution_id=parent.id,
            task_id=task.id,
            child_execution=child.id,
            depth=depth,
        )

        finished = await self.engine.run(child.id)

        self.engine.audit.record(
            "sub_execution.finished",
            execution_id=parent.id,
            task_id=task.id,
            child_execution=finished.id,
            status=finished.status.value,
            confidence=finished.confidence.value,
        )
        return self._to_result(context, finished)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _objective(context: AgentRunContext) -> str:
        """Build a self-contained objective for the child run."""
        task = context.task
        parts = [task.objective]
        if task.completion_criteria:
            parts.append(
                "This is complete when:\n"
                + "\n".join(f"- {c}" for c in task.completion_criteria)
            )
        upstream = [
            f"- {dep.name}: {(dep.result.summary or '')[:400]}"
            for dep in (
                context.execution.tasks.get(d) for d in task.dependencies
            )
            if dep is not None and dep.result is not None
        ]
        if upstream:
            parts.append("Results already established:\n" + "\n".join(upstream))
        return "\n\n".join(parts)

    def _child_limits(self, parent) -> ResourceLimits:
        """Carve the child's budget out of what the parent has left."""
        used = parent.usage
        limits = parent.limits
        share = max(0.05, min(1.0, self.budget_share))

        def remaining(total: float, spent: float) -> int:
            return max(1, int(max(0.0, total - spent) * share))

        return ResourceLimits(
            max_wall_seconds=max(
                30.0, (limits.max_wall_seconds - used.wall_seconds) * share
            ),
            max_model_calls=remaining(limits.max_model_calls, used.model_calls),
            max_tool_calls=remaining(limits.max_tool_calls, used.tool_calls),
            max_tokens=remaining(
                limits.max_tokens, used.input_tokens + used.output_tokens
            ),
            max_cost=(
                None
                if limits.max_cost is None
                else max(0.0, (limits.max_cost - used.cost) * share)
            ),
            max_parallel_tasks=max(1, limits.max_parallel_tasks // 2),
            max_task_attempts=limits.max_task_attempts,
            max_replans=max(0, limits.max_replans - 1),
            max_optimizer_iterations=limits.max_optimizer_iterations,
            max_external_requests=remaining(
                limits.max_external_requests, used.external_requests
            ),
        )

    @staticmethod
    def _to_result(context: AgentRunContext, child) -> TaskResult:
        """Map a finished child execution back into a single task result."""
        ok = child.status is ExecutionStatus.COMPLETED
        confidence = child.confidence
        # A child that stopped for a human has not failed; it is blocked, and
        # the parent task should reflect that rather than claiming failure.
        if child.status is ExecutionStatus.WAITING:
            confidence = Confidence.BLOCKED

        usage = Usage(
            model_calls=child.usage.model_calls,
            tool_calls=child.usage.tool_calls,
            input_tokens=child.usage.input_tokens,
            output_tokens=child.usage.output_tokens,
            cost=child.usage.cost,
            external_requests=child.usage.external_requests,
        )
        outputs = [
            t.result.output
            for t in child.tasks.values()
            if t.result is not None and t.result.ok and t.result.output is not None
        ]

        return TaskResult(
            task_id=context.task.id,
            ok=ok,
            summary=child.summary or f"sub-execution ended {child.status.value}",
            output=outputs[0] if len(outputs) == 1 else (outputs or child.summary),
            confidence=confidence,
            artifacts=list(child.artifacts),
            evidence=[e for v in child.validations for e in v.evidence],
            usage=usage,
            error=(
                None
                if ok
                else {
                    "child_execution": child.id,
                    "status": child.status.value,
                    "reason": child.summary[:500],
                }
            ),
        )


def resolve_runtime_name(task: Any, agent: Any) -> str:
    """Which runtime should execute a task.

    A task may name one explicitly; otherwise the agent's declared runtime is
    used. This is what lets a planner mark one node of a graph as a nested
    orchestration without inventing a whole agent for it.
    """
    explicit = task.metadata.get("runtime") if getattr(task, "metadata", None) else None
    if explicit:
        return str(explicit)
    if task.metadata.get("sub_execution"):
        return RUNTIME_NAME
    return agent.runtime
