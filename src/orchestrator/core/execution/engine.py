"""The orchestration engine.

This is the deterministic core: it owns the state machine, the task graph, task
dispatch, gates, recovery, approvals, and limits. Models are consulted for
reasoning (understanding the goal, planning, doing the work, judging subjective
output) and for nothing else (spec sections 9, 106).

The engine is re-entrant. Calling ``run`` again on a paused, waiting, or
interrupted execution picks up from persisted state rather than starting over
(spec sections 27, 66).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from ...agents.capabilities import CapabilityRegistry
from ...agents.registry import AgentRegistry
from ...agents.runtime import AgentRunContext, RuntimeRegistry
from ...agents.selection import AgentSelector
from ...agents.skills import SkillRegistry
from ...context.manager import ContextManager
from ...errors import (
    ApprovalRequired,
    NoCapableAgent,
    OrchestratorError,
    PolicyViolation,
    ResourceLimitExceeded,
)
from ...observability.audit import AuditLog, EventType
from ...planning.decomposition import Planner, PlanningContext
from ...planning.goal import GoalAnalyzer
from ...planning.strategy import OrchestrationChoice, choose
from ...recovery.engine import RecoveryEngine
from ...tools import permissions as perms
from ...tools.registry import ToolContext, ToolRegistry
from ...validation.gates import GateRunner, can_complete
from ...validation.validators import ValidationContext, ValidatorRegistry
from ..domain.enums import (
    TERMINAL_EXECUTION_STATUSES,
    ApprovalStatus,
    Confidence,
    ExecutionStatus,
    OrchestrationPattern,
    PlanStrategy,
    RiskLevel,
    TaskStatus,
    WaitReason,
)
from ..domain.models import (
    Approval,
    Execution,
    Plan,
    ResourceLimits,
    Task,
    Usage,
)
from ..policy.engine import PermissionScope, PolicyEngine
from ..scheduler.scheduler import Scheduler
from ..state.manager import StateManager
from ..workflow.graph import TaskGraph
from ..workflow.verification import (
    VerificationContext,
    VerificationReport,
    verify,
)
from .limits import LimitGuard
from .nested import resolve_runtime_name


@dataclass
class EngineConfig:
    workspace: str | None = None
    # Where published artifacts are written as files. Distinct from the
    # workspace: an agent may publish a result without being allowed to write
    # into the project it is working on.
    artifact_dir: str | None = None
    default_limits: ResourceLimits = field(default_factory=ResourceLimits)
    # Grant an agent that declares no permissions the safe default set.
    default_permissions: tuple[str, ...] = perms.SAFE_DEFAULTS
    # Ask a human before running anything at or above this risk level.
    approval_threshold: RiskLevel = RiskLevel.HIGH
    # Cap on how many planning rounds an iterative execution may take.
    max_planning_rounds: int = 12
    persist_every_task: bool = True


@dataclass
class EngineComponents:
    state: StateManager
    goal: GoalAnalyzer
    planner: Planner
    selector: AgentSelector
    runtimes: RuntimeRegistry
    tools: ToolRegistry
    gates: GateRunner
    recovery: RecoveryEngine
    context: ContextManager
    validators: ValidatorRegistry
    capabilities: CapabilityRegistry
    agents: AgentRegistry
    policy: PolicyEngine
    skills: SkillRegistry = field(default_factory=SkillRegistry)
    scheduler: Scheduler = field(default_factory=Scheduler)
    mcp_servers: Sequence[str] = ()


class ExecutionEngine:
    def __init__(
        self,
        components: EngineComponents,
        *,
        config: EngineConfig | None = None,
        metrics=None,
    ) -> None:
        self.c = components
        self.config = config or EngineConfig()
        self.metrics = metrics

    @property
    def audit(self) -> AuditLog:
        return self.c.state.audit

    def _record(self, method: str, *args, **kwargs) -> None:
        """Record a metric without ever failing the execution.

        Instrumentation is not the work. A collector that raises must not turn
        a completed execution into a failed one.
        """
        if self.metrics is None:
            return
        try:
            getattr(self.metrics, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001, S110 - deliberate: metrics never fail work
            pass

    def _record_terminal(self, execution: Execution) -> None:
        """Outcome and wall time, once, when an execution stops for good."""
        from datetime import datetime

        try:
            started = execution.created_at
            if isinstance(started, str):
                started = datetime.fromisoformat(started)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            seconds = (datetime.now(UTC) - started).total_seconds()
        except Exception:  # noqa: BLE001 - a bad timestamp is not worth failing on
            seconds = 0.0
        self._record(
            "execution_finished",
            execution.status.value,
            execution.confidence.value,
            max(0.0, seconds),
        )

    # -- public API --------------------------------------------------------

    async def start(
        self,
        objective: str,
        *,
        limits: ResourceLimits | None = None,
        context: dict[str, Any] | None = None,
        pattern: OrchestrationPattern | None = None,
        plan_strategy: PlanStrategy | None = None,
        parent_execution_id: str | None = None,
    ) -> Execution:
        """Create an execution without running it."""
        execution = await self.c.state.create_execution(
            objective,
            limits=limits or self.config.default_limits,
            context=dict(context or {}),
            parent_execution_id=parent_execution_id,
        )
        execution.workflow.agent_versions = self.c.agents.snapshot_versions()
        execution.workflow.skill_versions = self.c.skills.snapshot_versions()
        execution.workflow.policy_version = self.c.policy.config.version
        if pattern is not None:
            execution.context["forced_pattern"] = pattern.value
        if plan_strategy is not None:
            execution.context["forced_strategy"] = plan_strategy.value
        await self.c.state.persist(execution)
        return execution

    async def run(self, execution_id: str) -> Execution:
        """Drive an execution until it reaches a terminal or waiting state.

        A thin wrapper so the terminal outcome is recorded on *every* exit
        path. Recording inside the loop missed the ones that return directly
        — review, failure, and cancellation — which is to say most of the
        interesting ones.
        """
        execution = await self._run(execution_id)
        if execution.status in TERMINAL_EXECUTION_STATUSES:
            self._record_terminal(execution)
        return execution

    async def _run(self, execution_id: str) -> Execution:
        execution = await self.c.state.load(execution_id)
        guard = LimitGuard(execution.limits, usage=execution.usage)
        rounds = 0

        # How long this sat between being created and being picked up. The
        # signal that says "add capacity" rather than "the models are slow".
        if execution.status is ExecutionStatus.CREATED:
            from datetime import datetime

            try:
                created = execution.created_at
                if isinstance(created, str):
                    created = datetime.fromisoformat(created)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                self._record(
                    "queue_wait",
                    max(0.0, (datetime.now(UTC) - created).total_seconds()),
                )
            except Exception:  # noqa: BLE001, S110
                pass

        while True:
            rounds += 1
            if rounds > self.config.max_planning_rounds * 4:
                return await self._fail(
                    execution, "engine exceeded its round budget without converging"
                )

            if execution.cancel_requested and execution.status not in (
                ExecutionStatus.CANCELLING,
                ExecutionStatus.CANCELLED,
            ):
                return await self._cancel(execution)

            if execution.pause_requested and execution.status not in (
                ExecutionStatus.PAUSING,
                ExecutionStatus.PAUSED,
            ):
                return await self._pause(execution)

            if execution.pending_approval() is not None:
                return await self._wait_for_human(execution)

            status = guard.check()
            if status.exceeded:
                self.audit.record(
                    EventType.LIMIT_EXCEEDED,
                    execution_id=execution.id,
                    limit=status.limit,
                    used=status.used,
                    allowed=status.allowed,
                )
                return await self._fail(execution, status.message)

            if execution.status is ExecutionStatus.CREATED:
                await self._understand(execution)
                continue

            if execution.status is ExecutionStatus.PLANNING:
                await self._plan(execution)
                continue

            if execution.status in (ExecutionStatus.READY, ExecutionStatus.RECOVERING):
                self.c.state.transition(execution, ExecutionStatus.RUNNING)
                await self.c.state.persist(execution)
                continue

            if execution.status is ExecutionStatus.RUNNING:
                await self._execute_tasks(execution, guard)
                continue

            if execution.status is ExecutionStatus.VALIDATING:
                await self._validate_objective(execution)
                continue

            if execution.status is ExecutionStatus.REVIEWING:
                return await self._review(execution)

            if execution.status in (
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
                ExecutionStatus.PAUSED,
                ExecutionStatus.WAITING,
            ):
                return execution

            # Any other state means the engine has nothing left to do.
            return execution

    async def resume(self, execution_id: str) -> Execution:
        """Continue a paused or waiting execution."""
        execution = await self.c.state.load(execution_id)
        if execution.status is ExecutionStatus.PAUSED:
            execution.pause_requested = False
            self.c.state.transition(
                execution, ExecutionStatus.READY, reason="resumed by request"
            )
            self.audit.record(EventType.EXECUTION_RESUMED, execution_id=execution.id)
            await self.c.state.persist(execution)
        elif execution.status is ExecutionStatus.WAITING:
            pending = execution.pending_approval()
            if pending is not None:
                return execution  # still blocked on a human
            await self._apply_approvals(execution)
            self.c.state.transition(
                execution, ExecutionStatus.READY, reason="human input received"
            )
            self.audit.record(EventType.EXECUTION_RESUMED, execution_id=execution.id)
            await self.c.state.persist(execution)
        return await self.run(execution.id)

    # -- phases ------------------------------------------------------------

    async def _understand(self, execution: Execution) -> None:
        """Goal understanding happens before any work is planned."""
        analysis = await self.c.goal.analyze(
            execution.objective,
            context=execution.context,
            execution_id=execution.id,
            available_tools=[spec.id for spec in self.c.tools.list()],
            available_capabilities=sorted(
                self.c.capabilities.ids() | self.c.agents.capabilities()
            ),
        )
        execution.requirements = analysis.requirements
        self.audit.record(
            "goal.analysed",
            execution_id=execution.id,
            source=analysis.source,
            explicit=len(analysis.requirements.explicit),
            assumptions=len(analysis.requirements.assumptions),
            unknowns=len(analysis.requirements.unknowns),
            criteria=len(analysis.requirements.success_criteria),
        )

        if analysis.clarification_needed and analysis.clarifying_question:
            # Only genuinely blocking gaps stop the run (spec section 54).
            approval = Approval(
                execution_id=execution.id,
                task_id=None,
                reason=WaitReason.INPUT,
                risk=RiskLevel.LOW,
                prompt=analysis.clarifying_question,
                options=[],
            )
            self.c.state.request_approval(execution, approval)
            await self.c.state.persist(execution)
            return

        self.c.state.transition(
            execution, ExecutionStatus.PLANNING, reason="objective understood"
        )
        await self.c.state.persist(execution)

    async def _plan(self, execution: Execution) -> None:
        choice = self._choice_for(execution)
        planning_context = PlanningContext(
            available_capabilities=sorted(
                self.c.capabilities.ids() | self.c.agents.capabilities()
            ),
            available_tools=[spec.id for spec in self.c.tools.list()],
            available_validators=self.c.validators.names(),
            mcp_servers=list(self.c.mcp_servers),
        )

        if execution.plan is None:
            plan = await self.c.planner.plan(execution, choice, planning_context)
        elif (
            execution.plan.strategy is PlanStrategy.ITERATIVE
            and not execution.plan.complete
        ):
            plan = await self.c.planner.plan_next(execution, choice, planning_context)
        else:
            plan = await self.c.planner.replan(
                execution,
                choice,
                planning_context,
                reason=execution.context.get("replan_reason", ""),
            )
            execution.context.pop("replan_reason", None)

        report = self._verify(execution, plan)
        if not report.ok:
            await self._fail(
                execution,
                "plan verification failed: "
                + "; ".join(issue.message for issue in report.errors),
            )
            return

        self._adopt(execution, plan)

        if not execution.tasks:
            await self._fail(execution, "planning produced no tasks")
            return

        self.c.state.transition(
            execution, ExecutionStatus.READY, reason=f"plan v{plan.version} ready"
        )
        await self.c.state.persist(execution)

    def _verify(self, execution: Execution, plan: Plan) -> VerificationReport:
        """Check the plan against the registries before anything runs."""
        report = verify(
            plan,
            VerificationContext(
                capabilities=self.c.capabilities,
                agents=self.c.agents,
                tools=self.c.tools,
                validators=self.c.validators,
                skills=self.c.skills,
                router=getattr(self.c.planner, "router", None),
                allow_dynamic_agents=self.c.selector.policy.allow_dynamic_agents,
            ),
            existing=list(execution.tasks.values()),
        )
        if report.issues:
            self.audit.record(
                "plan.verified",
                execution_id=execution.id,
                version=plan.version,
                **report.to_dict(),
            )
        return report

    def _choice_for(self, execution: Execution) -> OrchestrationChoice:
        forced_pattern = execution.context.get("forced_pattern")
        forced_strategy = execution.context.get("forced_strategy")
        choice = choose(
            execution.objective,
            requirements=execution.requirements,
            available_capabilities=sorted(self.c.agents.capabilities()),
            max_parallel=execution.limits.max_parallel_tasks,
            force_pattern=OrchestrationPattern(forced_pattern) if forced_pattern else None,
            force_strategy=PlanStrategy(forced_strategy) if forced_strategy else None,
        )
        self.audit.record(
            EventType.PATTERN_SELECTED,
            execution_id=execution.id,
            **choice.to_dict(),
        )
        return choice

    def _adopt(self, execution: Execution, plan: Plan) -> None:
        for task in plan.tasks:
            task.execution_id = execution.id
            execution.tasks[task.id] = task
        execution.plan = plan
        execution.plan_version = plan.version

    async def _execute_tasks(self, execution: Execution, guard: LimitGuard) -> None:
        graph = TaskGraph(list(execution.tasks.values()))

        # Tasks whose dependencies died can never run; record that explicitly.
        for task in graph.blocked_tasks():
            self.c.state.transition_task(
                execution, task, TaskStatus.SKIPPED, reason="a dependency did not succeed"
            )

        def should_continue() -> bool:
            if execution.cancel_requested or execution.pause_requested:
                return False
            if execution.pending_approval() is not None:
                return False
            return not guard.check().exceeded

        report = await self.c.scheduler.run(
            graph,
            lambda task: self._run_task(execution, task, guard),
            should_continue=should_continue,
            max_parallel=execution.limits.max_parallel_tasks,
        )
        execution.usage = guard.snapshot()

        if execution.cancel_requested:
            await self._cancel(execution)
            return
        if execution.pause_requested:
            await self._pause(execution)
            return
        if execution.pending_approval() is not None:
            await self._wait_for_human(execution)
            return

        status = guard.check()
        if status.exceeded:
            self.audit.record(
                EventType.LIMIT_EXCEEDED,
                execution_id=execution.id,
                limit=status.limit,
                used=status.used,
                allowed=status.allowed,
            )
            await self._fail(execution, status.message)
            return

        graph = TaskGraph(list(execution.tasks.values()))

        # An iterative plan may have more steps to generate.
        if (
            execution.plan is not None
            and not execution.plan.complete
            and graph.is_complete()
            and execution.plan_version < self.config.max_planning_rounds
        ):
            self.c.state.transition(
                execution, ExecutionStatus.PLANNING, reason="iterative planning continues"
            )
            await self.c.state.persist(execution)
            return

        if graph.is_complete():
            self.c.state.transition(
                execution, ExecutionStatus.VALIDATING, reason="all tasks are terminal"
            )
        elif report.stopped_early:
            self.c.state.transition(
                execution,
                ExecutionStatus.READY,
                reason=report.stop_reason or "scheduler stopped early",
            )
        else:
            # Runnable work remains (a recovery put a task back); loop again.
            self.c.state.transition(
                execution, ExecutionStatus.READY, reason="more work is runnable"
            )
        await self.c.state.persist(execution)

    async def _validate_objective(self, execution: Execution) -> None:
        context = ValidationContext(
            execution=execution,
            tools=self.c.tools,
            tool_context=ToolContext(
                execution_id=execution.id,
                scope=PermissionScope(permissions=tuple(perms.ALL), tools=("*",)),
                workspace=self.config.workspace,
            ),
            workspace=self.config.workspace,
            # The store boundary, so artifact_exists can confirm a
            # recorded location is actually inside it.
            extras={"artifact_dir": self.config.artifact_dir},
        )
        outcome = await self.c.gates.run_for_objective(execution, context)
        execution.confidence = outcome.confidence

        if outcome.passed:
            self.c.state.transition(
                execution, ExecutionStatus.REVIEWING, reason="objective gate passed"
            )
        else:
            recovery = self.c.recovery.handle_objective_failure(
                execution, f"objective validation failed: {outcome.message}"
            )
            if recovery.replan:
                execution.context["replan_reason"] = outcome.message
                self.c.state.transition(
                    execution, ExecutionStatus.PLANNING, reason="objective gate failed"
                )
            elif recovery.escalate:
                await self._wait_for_human(execution)
                return
            else:
                await self._fail(execution, outcome.message)
                return
        await self.c.state.persist(execution)

    async def _review(self, execution: Execution) -> Execution:
        allowed, reason = can_complete(execution)
        if not allowed:
            self.audit.record("review.blocked", execution_id=execution.id, reason=reason)
            return await self._fail(execution, reason)

        execution.summary = self._summarise(execution)
        if execution.confidence in (Confidence.FAILED, Confidence.BLOCKED):
            execution.confidence = Confidence.UNCERTAIN
        self.c.state.transition(execution, ExecutionStatus.COMPLETED, reason=reason)
        self.audit.record(
            EventType.EXECUTION_COMPLETED,
            execution_id=execution.id,
            confidence=execution.confidence.value,
            tasks=len(execution.tasks),
            artifacts=len(execution.artifacts),
        )
        return await self.c.state.persist(execution)

    # -- task execution ----------------------------------------------------

    async def _run_task(self, execution: Execution, task: Task, guard: LimitGuard) -> None:
        if task.status is TaskStatus.PENDING:
            self.c.state.transition_task(execution, task, TaskStatus.READY)

        if self._needs_approval(task) and not self._approval_granted(execution, task):
            self._request_task_approval(execution, task)
            self.c.state.transition_task(
                execution, task, TaskStatus.WAITING, reason="awaiting approval"
            )
            return

        try:
            selection = self._select_agent(execution, task)
        except NoCapableAgent as exc:
            self.c.recovery.handle_exception(execution, task, exc)
            return

        agent = selection.agent
        task.assigned_agent = agent.id
        scope = self._scope_for(agent, task)

        self.c.state.transition_task(
            execution, task, TaskStatus.RUNNING, reason=f"assigned to {agent.id}"
        )
        task.attempts += 1
        self.audit.record(
            EventType.TASK_STARTED,
            execution_id=execution.id,
            task_id=task.id,
            agent=agent.id,
            attempt=task.attempts,
            capabilities=task.required_capabilities,
        )
        if self.config.persist_every_task:
            await self.c.state.persist(execution)

        runtime = self.c.runtimes.get(resolve_runtime_name(task, agent))

        # An agent that asked for isolation must get it. Running with less than
        # was declared, silently, is the failure mode this check exists to stop
        # (spec section 63).
        required_isolation = agent.constraints.isolation
        supported = getattr(runtime, "supported_isolation", None)
        if supported is not None and required_isolation not in supported:
            self.c.recovery.handle_exception(
                execution,
                task,
                PolicyViolation(
                    f"agent {agent.id} requires {required_isolation.value} isolation,"
                    f" which the {runtime.name} runtime cannot provide",
                    agent_id=agent.id,
                    runtime=runtime.name,
                    required=required_isolation.value,
                    available=sorted(level.value for level in supported),
                ),
            )
            await self.c.state.persist(execution)
            return

        started = time.monotonic()
        try:
            result = await runtime.run(
                AgentRunContext(
                    execution=execution,
                    task=task,
                    agent=agent,
                    scope=scope,
                    workspace=self.config.workspace,
                    artifact_dir=self.config.artifact_dir,
                )
            )
        except ApprovalRequired as exc:
            self._request_task_approval(execution, task, prompt=exc.message)
            self.c.state.transition_task(
                execution, task, TaskStatus.WAITING, reason="tool requires approval"
            )
            await self.c.state.persist(execution)
            return
        except ResourceLimitExceeded as exc:
            self.c.recovery.handle_exception(execution, task, exc)
            await self.c.state.persist(execution)
            return
        except OrchestratorError as exc:
            self.c.recovery.handle_exception(execution, task, exc)
            await self.c.state.persist(execution)
            return
        except Exception as exc:  # noqa: BLE001 - a runtime defect is a task failure
            self.c.recovery.handle_exception(execution, task, exc)
            await self.c.state.persist(execution)
            return

        result.task_id = task.id
        task.result = result
        guard.add(result.usage.add(Usage(wall_seconds=time.monotonic() - started)))
        execution.usage = guard.snapshot()

        if not result.ok:
            self.c.recovery.handle_exception(
                execution,
                task,
                OrchestratorError(result.summary or "the agent reported failure"),
            )
            await self.c.state.persist(execution)
            return

        await self._validate_task(execution, task, scope)
        if self.config.persist_every_task:
            await self.c.state.persist(execution)

    async def _validate_task(
        self, execution: Execution, task: Task, scope: PermissionScope
    ) -> None:
        self.c.state.transition_task(execution, task, TaskStatus.VALIDATING)
        context = ValidationContext(
            execution=execution,
            task=task,
            tools=self.c.tools,
            tool_context=ToolContext(
                execution_id=execution.id,
                task_id=task.id,
                agent_id=task.assigned_agent,
                scope=scope,
                workspace=self.config.workspace,
            ),
            workspace=self.config.workspace,
            # The store boundary, so artifact_exists can confirm a
            # recorded location is actually inside it.
            extras={"artifact_dir": self.config.artifact_dir},
        )
        outcome = await self.c.gates.run_for_task(execution, task, context)

        if not outcome.passed:
            self.c.recovery.handle_validation_failure(
                execution,
                task,
                outcome.message,
                validators=[r.validator for r in outcome.failed_mandatory],
            )
            return

        # An evaluator task may send the work it judged back for another round.
        if self._optimizer_should_iterate(execution, task):
            self._spawn_optimizer_iteration(execution, task)

        self.c.state.transition_task(
            execution, task, TaskStatus.SUCCEEDED, reason=outcome.message[:200]
        )
        self.c.recovery.mark_recovered(execution, task)
        if task.result is not None:
            # Deduplicate by stored location, keeping the newest record for
            # each file. Keying on the *name* would be wrong now that a
            # collision with different content is versioned to a new filename:
            # two records legitimately share a name while describing two
            # different files, and dropping one would lose a deliverable.
            # Records with no location (reference-only artifacts) fall back to
            # the name, which is all they have.
            for artifact in task.result.artifacts:
                key = artifact.location or f"name:{artifact.name}"
                execution.artifacts[:] = [
                    existing
                    for existing in execution.artifacts
                    if (existing.location or f"name:{existing.name}") != key
                ]
                execution.artifacts.append(artifact)
                self.c.context.remember_artifact(execution, artifact)
        self.c.context.remember_result(execution, task)
        self.audit.record(
            EventType.TASK_FINISHED,
            execution_id=execution.id,
            task_id=task.id,
            confidence=(task.result.confidence.value if task.result else "unknown"),
            artifacts=len(task.result.artifacts) if task.result else 0,
        )

        # A router decision prunes the branches it did not choose.
        self._apply_route_decision(execution, task)
        # A task may hand its remaining work to a different specialist.
        self._apply_handoff(execution, task)

    # -- router -------------------------------------------------------------

    def _apply_route_decision(self, execution: Execution, task: Task) -> None:
        """Skip the branches a router task did not select (spec section 5).

        Every branch is materialised at planning time so the decision is
        auditable. Once the decision is made, the unchosen branches are skipped
        rather than left pending, which is what lets the graph reach quiescence.
        """
        routes = task.metadata.get("routes")
        if not routes or task.result is None:
            return

        chosen = _chosen_route(task.result, [str(r) for r in routes])
        if chosen is None:
            # No decision could be read. Leaving every branch to run would be
            # worse than saying so: the router failed at the one thing it does.
            self.audit.record(
                "route.undecided",
                execution_id=execution.id,
                task_id=task.id,
                offered=[str(r) for r in routes],
                summary=(task.result.summary or "")[:300],
            )
            return

        skipped: list[str] = []
        for candidate in execution.tasks.values():
            group = candidate.group or ""
            if not group.startswith("route:") or candidate.is_terminal:
                continue
            if group == f"route:{chosen}":
                continue
            self.c.state.transition_task(
                execution,
                candidate,
                TaskStatus.SKIPPED,
                reason=f"route '{chosen}' was selected instead",
            )
            skipped.append(candidate.id)

        self.audit.record(
            "route.selected",
            execution_id=execution.id,
            task_id=task.id,
            route=chosen,
            offered=[str(r) for r in routes],
            skipped=skipped,
        )

    # -- handoff ------------------------------------------------------------

    def _apply_handoff(self, execution: Execution, task: Task) -> None:
        """Continue work a task handed to a different specialist (spec section 5).

        Handoff is an *outcome* a task may declare, not a control structure: the
        orchestrator decides whether to honour it, appends the follow-on task to
        the graph, and bounds the chain so peers cannot pass work in a circle.
        """
        if task.result is None or not task.result.handoff_to:
            return

        target = str(task.result.handoff_to)
        depth = int(task.metadata.get("handoff_depth", 0)) + 1
        limit = int(execution.limits.max_optimizer_iterations)

        if depth > limit:
            self.audit.record(
                "handoff.refused",
                execution_id=execution.id,
                task_id=task.id,
                target=target,
                reason=f"handoff chain reached its limit of {limit}",
            )
            return

        capability_known = self.c.capabilities.has(target)
        agent_known = self.c.agents.has(target)
        if not capability_known and not agent_known:
            self.audit.record(
                "handoff.refused",
                execution_id=execution.id,
                task_id=task.id,
                target=target,
                reason="handoff target is neither a registered agent nor capability",
            )
            return

        follow_on = Task(
            execution_id=execution.id,
            name=f"{task.name} -> {target}",
            objective=(
                f"Continue the work handed over from '{task.name}'.\n"
                f"What was done: {(task.result.summary or '')[:1000]}\n"
                f"Original objective: {task.objective}"
            ),
            inputs={**task.inputs, "_handed_over_from": task.id},
            expected_outputs=list(task.expected_outputs),
            dependencies=[task.id],
            required_capabilities=[target] if capability_known else [],
            allowed_tools=list(task.allowed_tools),
            validations=list(task.validations),
            completion_criteria=list(task.completion_criteria),
            resources=list(task.resources),
            pattern=OrchestrationPattern.HANDOFF,
            group="handoff",
            metadata={"handoff_depth": depth, "handed_over_from": task.id},
        )
        if agent_known and not capability_known:
            follow_on.assigned_agent = target

        execution.tasks[follow_on.id] = follow_on
        self.audit.record(
            "handoff.accepted",
            execution_id=execution.id,
            task_id=task.id,
            target=target,
            follow_on_task=follow_on.id,
            depth=depth,
        )

    # -- evaluator-optimizer ----------------------------------------------

    def _optimizer_should_iterate(self, execution: Execution, task: Task) -> bool:
        target_id = task.metadata.get("optimizes")
        if not target_id or target_id not in execution.tasks:
            return False
        verdict = _verdict(task.result.output if task.result else None)
        if verdict is not False:
            return False
        iteration = int(task.metadata.get("iteration", 1))
        limit = int(
            task.metadata.get("max_iterations", execution.limits.max_optimizer_iterations)
        )
        return iteration < limit

    def _spawn_optimizer_iteration(self, execution: Execution, evaluator: Task) -> None:
        """Add another generate/evaluate pair rather than reviving a finished task.

        Terminal statuses stay terminal, so the audit trail shows each round of
        the loop as its own pair of tasks.
        """
        target = execution.tasks[str(evaluator.metadata["optimizes"])]
        iteration = int(evaluator.metadata.get("iteration", 1)) + 1
        feedback = (evaluator.result.summary if evaluator.result else "") or ""

        regenerated = Task(
            execution_id=execution.id,
            name=f"{target.name} (revision {iteration})",
            objective=target.objective,
            inputs={**target.inputs, "_evaluator_feedback": feedback[:4000]},
            expected_outputs=list(target.expected_outputs),
            dependencies=list(target.dependencies),
            required_capabilities=list(target.required_capabilities),
            allowed_tools=list(target.allowed_tools),
            validations=list(target.validations),
            completion_criteria=list(target.completion_criteria),
            resources=list(target.resources),
            pattern=OrchestrationPattern.EVALUATOR_OPTIMIZER,
            group="generate",
            metadata={**target.metadata, "iteration": iteration},
        )
        next_evaluator = Task(
            execution_id=execution.id,
            name=f"{evaluator.name} (round {iteration})",
            objective=evaluator.objective,
            dependencies=[regenerated.id],
            required_capabilities=list(evaluator.required_capabilities),
            validations=list(evaluator.validations),
            pattern=OrchestrationPattern.EVALUATOR_OPTIMIZER,
            group="evaluate",
            metadata={
                **evaluator.metadata,
                "optimizes": regenerated.id,
                "iteration": iteration,
            },
        )
        regenerated.metadata["optimized_by"] = next_evaluator.id
        execution.tasks[regenerated.id] = regenerated
        execution.tasks[next_evaluator.id] = next_evaluator
        self.audit.record(
            "optimizer.iteration",
            execution_id=execution.id,
            task_id=evaluator.id,
            iteration=iteration,
            regenerated_task=regenerated.id,
            feedback=feedback[:300],
        )

    # -- agents, scope, approvals -----------------------------------------

    def _select_agent(self, execution: Execution, task: Task):
        excluded = set(task.metadata.get("excluded_agents", []))

        # An explicitly named agent (a handoff target, or a caller's choice) is
        # honoured, unless recovery already excluded it.
        named = task.assigned_agent
        if named and named not in excluded and self.c.agents.has(named):
            from ...agents.selection import Selection

            selection = Selection(
                agent=self.c.agents.get(named),
                score=0.0,
                rationale="explicitly assigned to this task",
            )
            self.audit.record(
                EventType.AGENT_SELECTED,
                execution_id=execution.id,
                task_id=task.id,
                agent=named,
                rationale=selection.rationale,
                ephemeral=selection.agent.ephemeral,
            )
            return selection

        selection = self.c.selector.select(task)
        if selection.agent.id in excluded:
            candidates = [
                candidate
                for candidate in self.c.selector.candidates(task.required_capabilities)
                if candidate[0].id not in excluded
            ]
            if candidates:
                agent, score, rationale = candidates[0]
                selection.agent = agent
                selection.score = score
                selection.rationale = rationale
            else:
                selection.agent = self.c.selector.create_agent(
                    task, task.required_capabilities
                )
                selection.created = True
        self.audit.record(
            EventType.AGENT_CREATED if selection.created else EventType.AGENT_SELECTED,
            execution_id=execution.id,
            task_id=task.id,
            agent=selection.agent.id,
            rationale=selection.rationale,
            ephemeral=selection.agent.ephemeral,
        )
        return selection

    def _scope_for(self, agent, task: Task) -> PermissionScope:
        """Least privilege: the intersection of what the agent and task allow."""
        granted = set(agent.permissions) or set(self.config.default_permissions)
        capability_requirements = self.c.capabilities.requirements_for(
            task.required_capabilities
        )
        granted |= set(capability_requirements.permissions)

        tool_patterns = set(agent.tools) | set(task.allowed_tools)
        tool_patterns |= set(capability_requirements.tools)

        # A tool the orchestrator explicitly assigned to this task carries the
        # permissions it declares. Withholding them would hand an agent a tool
        # it is structurally unable to call, which reads as the agent failing
        # when it is really the configuration that is broken. Risk is still
        # gated: the policy engine evaluates every call regardless.
        for pattern in sorted(tool_patterns):
            if "*" in pattern or "?" in pattern:
                continue
            if self.c.tools.has(pattern):
                granted |= set(self.c.tools.get(pattern).permissions)

        if not tool_patterns:
            # No declared tools means bookkeeping only, never everything.
            tool_patterns = {"orchestrator.*"}

        return PermissionScope(
            permissions=tuple(sorted(granted)),
            tools=tuple(sorted(tool_patterns)),
            mcp_servers=tuple(agent.mcp_servers),
            resources=tuple(task.resources),
        )

    def _needs_approval(self, task: Task) -> bool:
        return (
            task.requires_approval or task.risk.rank >= self.config.approval_threshold.rank
        )

    @staticmethod
    def _approval_granted(execution: Execution, task: Task) -> bool:
        for approval in execution.approvals:
            if approval.task_id == task.id and approval.status is ApprovalStatus.APPROVED:
                return True
        return False

    def _request_task_approval(
        self, execution: Execution, task: Task, *, prompt: str = ""
    ) -> Approval:
        existing = next(
            (
                a
                for a in execution.approvals
                if a.task_id == task.id and a.status is ApprovalStatus.PENDING
            ),
            None,
        )
        if existing is not None:
            return existing
        approval = Approval(
            execution_id=execution.id,
            task_id=task.id,
            reason=WaitReason.APPROVAL,
            risk=task.risk,
            prompt=prompt
            or (
                f"Task '{task.name}' is rated {task.risk.value} risk and needs "
                f"approval before it runs.\nObjective: {task.objective[:500]}"
            ),
            options=["approve", "reject"],
        )
        return self.c.state.request_approval(execution, approval)

    async def _apply_approvals(self, execution: Execution) -> None:
        """Translate resolved approvals into task state."""
        for approval in execution.approvals:
            if approval.task_id is None or approval.status is ApprovalStatus.PENDING:
                continue
            task = execution.tasks.get(approval.task_id)
            if task is None or task.status is not TaskStatus.WAITING:
                continue
            if approval.status is ApprovalStatus.APPROVED:
                response = str(approval.response or "").lower()
                if response == "skip":
                    self.c.state.transition_task(
                        execution, task, TaskStatus.SKIPPED, reason="skipped by a human"
                    )
                else:
                    self.c.state.transition_task(
                        execution, task, TaskStatus.READY, reason="approved by a human"
                    )
                    if isinstance(approval.response, str) and approval.response:
                        task.inputs = {
                            **task.inputs,
                            "_human_guidance": approval.response,
                        }
            else:
                self.c.state.transition_task(
                    execution, task, TaskStatus.SKIPPED, reason="rejected by a human"
                )

    # -- terminal transitions ---------------------------------------------

    async def _wait_for_human(self, execution: Execution) -> Execution:
        pending = execution.pending_approval()
        reason = pending.reason if pending else WaitReason.INPUT
        if execution.status is not ExecutionStatus.WAITING:
            self.c.state.enter_wait(
                execution, reason, detail=pending.prompt[:200] if pending else ""
            )
        return await self.c.state.persist(execution)

    async def _pause(self, execution: Execution) -> Execution:
        if execution.status is not ExecutionStatus.PAUSED:
            self.c.state.transition(
                execution, ExecutionStatus.PAUSING, reason="pause requested"
            )
            self.c.state.transition(execution, ExecutionStatus.PAUSED, reason="paused")
        self.audit.record(EventType.EXECUTION_PAUSED, execution_id=execution.id)
        return await self.c.state.persist(execution)

    async def _cancel(self, execution: Execution) -> Execution:
        if execution.status is not ExecutionStatus.CANCELLED:
            if execution.status is not ExecutionStatus.CANCELLING:
                self.c.state.transition(
                    execution, ExecutionStatus.CANCELLING, reason="cancel requested"
                )
            for task in execution.tasks.values():
                if not task.is_terminal:
                    self.c.state.transition_task(
                        execution, task, TaskStatus.CANCELLED, reason="execution cancelled"
                    )
            self.c.state.transition(
                execution, ExecutionStatus.CANCELLED, reason="cancelled"
            )
        execution.confidence = Confidence.BLOCKED
        self.audit.record(EventType.EXECUTION_CANCELLED, execution_id=execution.id)
        return await self.c.state.persist(execution)

    async def _fail(self, execution: Execution, reason: str) -> Execution:
        execution.summary = reason
        execution.confidence = Confidence.FAILED
        if execution.status is not ExecutionStatus.FAILED:
            self.c.state.transition(execution, ExecutionStatus.FAILED, reason=reason[:300])
        self.audit.record(
            EventType.EXECUTION_FAILED, execution_id=execution.id, reason=reason[:500]
        )
        return await self.c.state.persist(execution)

    # -- reporting ---------------------------------------------------------

    @staticmethod
    def _summarise(execution: Execution) -> str:
        succeeded = [
            t for t in execution.tasks.values() if t.status is TaskStatus.SUCCEEDED
        ]
        lines = [
            f"Objective: {execution.objective}",
            f"Completed {len(succeeded)} of {len(execution.tasks)} tasks.",
        ]
        for task in succeeded[-6:]:
            if task.result and task.result.summary:
                lines.append(f"- {task.name}: {task.result.summary[:300]}")
        if execution.artifacts:
            lines.append(
                "Artifacts: " + ", ".join(a.name for a in execution.artifacts[:10])
            )
        unverified = [
            v for v in execution.validations if v.confidence is Confidence.UNCERTAIN
        ]
        if unverified:
            lines.append(
                f"{len(unverified)} checks could not be verified deterministically."
            )
        return "\n".join(lines)


def _chosen_route(result: Any, routes: Sequence[str]) -> str | None:
    """Read a router's decision out of its result.

    Structured output is authoritative. Prose is only consulted when exactly one
    route is named in it, because picking the first mention out of text that
    discusses several would be guessing at a branch decision.
    """
    output = getattr(result, "output", None)
    if isinstance(output, dict):
        for key in ("route", "branch", "choice", "selected"):
            value = output.get(key)
            if isinstance(value, str) and value in routes:
                return value
    if isinstance(output, str) and output.strip() in routes:
        return output.strip()

    text = " ".join(
        part
        for part in (
            getattr(result, "summary", ""),
            output if isinstance(output, str) else "",
        )
        if isinstance(part, str)
    ).lower()
    mentioned = [route for route in routes if route.lower() in text]
    return mentioned[0] if len(mentioned) == 1 else None


def _verdict(output: Any) -> bool | None:
    """Extract an explicit pass/fail from an evaluator's structured output.

    Returns None when the evaluator did not state one, which is treated as
    "no objection" rather than as a failure.
    """
    if isinstance(output, dict):
        for key in ("passed", "ok", "satisfied", "approved", "success"):
            if key in output:
                return bool(output[key])
    return None
