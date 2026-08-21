"""Dynamic task-graph planning.

The planner produces structured data - a validated DAG of tasks with
dependencies, required capabilities, and declared validations - not a
natural-language plan (spec section 6).

Three planning strategies are supported (spec section 7):

* FULL      - generate the whole graph up front.
* ITERATIVE - plan the next meaningful step, execute, observe, plan again.
* ADAPTIVE  - generate an initial graph and revise it as observations arrive.

When no model is configured, or when the model returns something unusable, the
planner falls back to a deterministic pattern-based decomposition. That keeps
the platform working without a provider and gives a broken model response
somewhere safe to land.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..core.domain.enums import (
    ModelCapability,
    OrchestrationPattern,
    PlanStrategy,
    RiskLevel,
)
from ..core.domain.models import Execution, Plan, Task, ValidationSpec
from ..core.workflow.graph import TaskGraph
from ..core.workflow.patterns import (
    Step,
    evaluator_optimizer,
    orchestrator_worker,
    parallel,
    sequential,
    single,
)
from ..errors import InvalidWorkflow
from ..llm.base import CompletionRequest, Message
from ..llm.routing import ModelRouter, RoutingRequirements
from ..observability.audit import AuditLog, EventType
from .goal import _extract_json
from .strategy import OrchestrationChoice

PLAN_SCHEMA: dict[str, Any] = {
    "title": "plan",
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short unique identifier used for dependencies.",
                    },
                    "name": {"type": "string"},
                    "objective": {
                        "type": "string",
                        "description": "What this task must accomplish, self-contained.",
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys of tasks that must succeed first.",
                    },
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Ids of tools this task needs, chosen from the "
                            "available list. A task that must read, write, or "
                            "call something will get nothing unless it asks here."
                        ),
                    },
                    "expected_outputs": {"type": "array", "items": {"type": "string"}},
                    "completion_criteria": {"type": "array", "items": {"type": "string"}},
                    "resources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Named resources this task writes to, for locking.",
                    },
                    "validation": {
                        "type": ["object", "null"],
                        "properties": {
                            "validator": {"type": "string"},
                            "config": {"type": "object"},
                        },
                    },
                    "risk": {
                        "type": "string",
                        "enum": ["none", "low", "medium", "high", "critical"],
                    },
                },
                "required": ["key", "name", "objective"],
            },
        },
        "complete": {
            "type": "boolean",
            "description": "False if more planning will be needed after these tasks.",
        },
    },
    "required": ["tasks"],
}

SYSTEM_PROMPT = """You decompose an objective into a dependency graph of tasks.

Rules:
- Produce the smallest graph that does the job. Do not invent review, approval,
  or coordination tasks that the objective does not need.
- Every task objective must be self-contained: a worker sees only that text,
  the results of its dependencies, and the tools it was granted.
- depends_on must reference keys defined in this same plan. No cycles.
- Put work that can genuinely run at the same time at the same dependency depth.
- Use only capabilities from the provided list. If none fits, leave the list
  empty rather than inventing one.
- List the tools each task needs, by exact id, from the available tools. A task
  is granted only what it asks for, so a task that must read a file and does not
  request a read tool will be unable to do its job. Ask for nothing you do not
  need.
- Declare a validation when something checkable exists. Do not claim a check
  that cannot actually run.
- Do not assume any particular domain, technology, language, or tooling.

Reply with JSON matching the requested schema and nothing else."""


@dataclass
class PlanningContext:
    """What the planner is allowed to consider."""

    available_capabilities: Sequence[str] = ()
    available_tools: Sequence[str] = ()
    available_validators: Sequence[str] = ()
    mcp_servers: Sequence[str] = ()
    notes: str = ""


class Planner:
    def __init__(
        self,
        *,
        router: ModelRouter | None = None,
        audit: AuditLog | None = None,
        max_tasks: int = 40,
    ) -> None:
        self.router = router
        self.audit = audit
        self.max_tasks = max_tasks

    # -- entry points ------------------------------------------------------

    async def plan(
        self,
        execution: Execution,
        choice: OrchestrationChoice,
        context: PlanningContext | None = None,
    ) -> Plan:
        """Build the initial plan for an execution."""
        context = context or PlanningContext()
        tasks: list[Task] | None = None
        rationale = choice.rationale
        complete = choice.plan_strategy is not PlanStrategy.ITERATIVE

        if self._model_available():
            try:
                tasks, model_rationale, model_complete = await self._plan_with_model(
                    execution, choice, context
                )
                rationale = model_rationale or rationale
                complete = model_complete if tasks else complete
            except Exception as exc:  # noqa: BLE001 - fall back, never fail planning
                self._audit(
                    "plan.model_failed",
                    execution_id=execution.id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                tasks = None

        if not tasks:
            tasks = self._plan_deterministically(execution, choice, context)
            rationale = f"{rationale} (deterministic decomposition)"

        plan = Plan(
            execution_id=execution.id,
            version=execution.plan_version + 1,
            strategy=choice.plan_strategy,
            pattern=choice.pattern,
            rationale=rationale,
            tasks=tasks,
            complete=complete,
        )
        self._validate(plan)
        self._audit(
            EventType.PLAN_CREATED,
            execution_id=execution.id,
            version=plan.version,
            pattern=plan.pattern.value,
            strategy=plan.strategy.value,
            task_count=len(plan.tasks),
            complete=plan.complete,
            rationale=rationale[:500],
        )
        return plan

    async def plan_next(
        self,
        execution: Execution,
        choice: OrchestrationChoice,
        context: PlanningContext | None = None,
    ) -> Plan:
        """Extend an iterative plan with the next step, given what happened."""
        context = context or PlanningContext()
        observations = self._observations(execution)
        tasks: list[Task] | None = None
        rationale = "iterative continuation"
        complete = True

        if self._model_available():
            try:
                tasks, model_rationale, model_complete = await self._plan_with_model(
                    execution, choice, context, observations=observations, incremental=True
                )
                rationale = model_rationale or rationale
                complete = model_complete
            except Exception as exc:  # noqa: BLE001
                self._audit(
                    "plan.model_failed",
                    execution_id=execution.id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                tasks = None

        if not tasks:
            return Plan(
                execution_id=execution.id,
                version=execution.plan_version + 1,
                strategy=choice.plan_strategy,
                pattern=choice.pattern,
                rationale="no further steps could be planned",
                tasks=[],
                complete=True,
            )

        # New tasks depend on everything already finished, so ordering holds.
        finished = [t.id for t in execution.tasks.values() if t.is_terminal]
        for task in tasks:
            if not task.dependencies:
                task.dependencies = list(finished)

        plan = Plan(
            execution_id=execution.id,
            version=execution.plan_version + 1,
            strategy=choice.plan_strategy,
            pattern=choice.pattern,
            rationale=rationale,
            tasks=tasks,
            complete=complete,
        )
        self._audit(
            EventType.PLAN_REVISED,
            execution_id=execution.id,
            version=plan.version,
            task_count=len(plan.tasks),
            complete=plan.complete,
            reason="iterative continuation",
        )
        return plan

    async def replan(
        self,
        execution: Execution,
        choice: OrchestrationChoice,
        context: PlanningContext | None = None,
        *,
        reason: str = "",
    ) -> Plan:
        """Regenerate the remaining work after a failure or new information."""
        context = context or PlanningContext()
        observations = self._observations(execution, include_failures=True)
        tasks: list[Task] | None = None
        rationale = reason or "re-planning after failure"

        if self._model_available():
            try:
                tasks, model_rationale, _ = await self._plan_with_model(
                    execution,
                    choice,
                    context,
                    observations=observations,
                    incremental=True,
                    reason=reason,
                )
                rationale = model_rationale or rationale
            except Exception:  # noqa: BLE001
                tasks = None

        if not tasks:
            # Deterministic fallback: retry the unfinished work as a sequence.
            pending = [
                t
                for t in execution.tasks.values()
                if not t.is_terminal or t.status.value == "failed"
            ]
            tasks = [
                Step(
                    name=f"retry:{t.name}",
                    objective=t.objective,
                    capabilities=list(t.required_capabilities),
                    tools=list(t.allowed_tools),
                    validations=list(t.validations),
                ).to_task(execution.id)
                for t in pending
            ]
            for index in range(1, len(tasks)):
                tasks[index].dependencies = [tasks[index - 1].id]

        plan = Plan(
            execution_id=execution.id,
            version=execution.plan_version + 1,
            strategy=choice.plan_strategy,
            pattern=choice.pattern,
            rationale=rationale,
            tasks=tasks,
            complete=True,
        )
        self._audit(
            EventType.PLAN_REVISED,
            execution_id=execution.id,
            version=plan.version,
            task_count=len(plan.tasks),
            reason=reason[:300],
        )
        return plan

    # -- model-backed planning --------------------------------------------

    def _model_available(self) -> bool:
        return self.router is not None and bool(self.router.all_models())

    async def _plan_with_model(
        self,
        execution: Execution,
        choice: OrchestrationChoice,
        context: PlanningContext,
        *,
        observations: str = "",
        incremental: bool = False,
        reason: str = "",
    ) -> tuple[list[Task], str, bool]:
        requirements = execution.requirements
        sections = [f"Objective:\n{execution.objective}"]
        if requirements.explicit:
            sections.append(
                "Explicit requirements:\n"
                + "\n".join(f"- {r}" for r in requirements.explicit)
            )
        if requirements.constraints:
            sections.append(
                "Constraints:\n" + "\n".join(f"- {c}" for c in requirements.constraints)
            )
        if requirements.success_criteria:
            sections.append(
                "Success criteria:\n"
                + "\n".join(f"- {c.description}" for c in requirements.success_criteria)
            )
        sections.append(
            "Available capabilities: "
            + (", ".join(context.available_capabilities) or "(none registered)")
        )
        sections.append(
            "Available validators: "
            + (", ".join(context.available_validators) or "noop")
        )
        if context.available_tools:
            sections.append(
                "Available tools: " + ", ".join(list(context.available_tools)[:60])
            )
        sections.append(
            f"Chosen orchestration pattern: {choice.pattern.value}"
            f" ({choice.complexity.band} complexity,"
            f" about {choice.complexity.estimated_tasks} tasks expected)"
        )
        if observations:
            sections.append("What has happened so far:\n" + observations)
        if incremental:
            sections.append(
                "Plan only the work that still needs doing. Do not repeat completed work."
            )
        if reason:
            sections.append(f"Reason for re-planning: {reason}")
        if context.notes:
            sections.append(context.notes)

        response = await self.router.complete(
            CompletionRequest(
                system=SYSTEM_PROMPT,
                messages=[Message(role="user", content="\n\n".join(sections))],
                response_schema=PLAN_SCHEMA,
                temperature=0.0,
                max_output_tokens=3000,
            ),
            requirements=RoutingRequirements(
                capabilities=[
                    ModelCapability.TEXT_GENERATION,
                    ModelCapability.STRUCTURED_OUTPUT,
                ]
            ),
            execution_id=execution.id,
        )
        data = response.structured
        if not isinstance(data, dict):
            data = _extract_json(response.text)
        if not isinstance(data, dict) or not data.get("tasks"):
            raise ValueError("planner model did not return a usable task list")

        tasks = self._materialise(execution, data, context)
        return tasks, str(data.get("rationale", "")), bool(data.get("complete", True))

    def _materialise(
        self, execution: Execution, data: dict[str, Any], context: PlanningContext
    ) -> list[Task]:
        """Convert planner output into validated Task objects."""
        entries = list(data.get("tasks", []))[: self.max_tasks]
        known_capabilities = set(context.available_capabilities)
        known_validators = set(context.available_validators) or {"noop"}

        by_key: dict[str, Task] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or entry.get("name") or "").strip()
            if not key or key in by_key:
                key = f"{key or 'task'}_{len(by_key)}"

            validation = entry.get("validation")
            validations: list[ValidationSpec] = []
            if isinstance(validation, dict) and validation.get("validator"):
                validator_name = str(validation["validator"])
                if validator_name in known_validators:
                    validations.append(
                        ValidationSpec(
                            validator=validator_name,
                            config=dict(validation.get("config", {})),
                            mandatory=bool(validation.get("mandatory", True)),
                        )
                    )
            if not validations:
                validations.append(
                    ValidationSpec(validator="non_empty", mandatory=True)
                )

            capabilities = [
                str(c)
                for c in entry.get("capabilities", [])
                if not known_capabilities or str(c) in known_capabilities
            ]
            # Only tools that actually exist. A hallucinated tool id would
            # otherwise become a scope entry that matches nothing.
            known_tools = set(context.available_tools)
            tools = [
                str(t)
                for t in entry.get("tools", []) or []
                if not known_tools or str(t) in known_tools
            ]

            try:
                risk = RiskLevel(str(entry.get("risk", "low")))
            except ValueError:
                risk = RiskLevel.LOW

            task = Task(
                execution_id=execution.id,
                name=str(entry.get("name") or key)[:120],
                objective=str(entry.get("objective", "")),
                required_capabilities=capabilities,
                allowed_tools=tools,
                expected_outputs=[str(o) for o in entry.get("expected_outputs", [])],
                completion_criteria=[
                    str(c) for c in entry.get("completion_criteria", [])
                ],
                resources=[str(r) for r in entry.get("resources", [])],
                validations=validations,
                risk=risk,
                requires_approval=risk.rank >= RiskLevel.HIGH.rank,
                metadata={"plan_key": key},
            )
            by_key[key] = task

        # Wire dependencies by key, dropping references the plan did not define.
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or entry.get("name") or "").strip()
            task = by_key.get(key)
            if task is None:
                continue
            for dependency_key in entry.get("depends_on", []) or []:
                dependency = by_key.get(str(dependency_key))
                if dependency is not None and dependency.id != task.id:
                    task.dependencies.append(dependency.id)

        return list(by_key.values())

    # -- deterministic planning -------------------------------------------

    def _plan_deterministically(
        self,
        execution: Execution,
        choice: OrchestrationChoice,
        context: PlanningContext,
    ) -> list[Task]:
        """Build a plan from the chosen pattern without a model."""
        requirements = execution.requirements
        items = requirements.explicit or [execution.objective]
        default_validation = [ValidationSpec(validator="non_empty", mandatory=True)]

        def step(name: str, objective: str, **kwargs: Any) -> Step:
            return Step(
                name=name,
                objective=objective,
                validations=list(default_validation),
                completion_criteria=[c.description for c in requirements.success_criteria],
                **kwargs,
            )

        pattern = choice.pattern
        if pattern is OrchestrationPattern.SINGLE_AGENT or len(items) == 1:
            tasks = single(execution.id, step("accomplish", execution.objective))
        elif pattern is OrchestrationPattern.PARALLEL:
            branches = [
                step(f"part {index + 1}", item) for index, item in enumerate(items[:6])
            ]
            tasks = parallel(
                execution.id,
                branches,
                merge=step(
                    "synthesise",
                    "Combine the results of the preceding parts into a single "
                    "coherent answer to the overall objective.",
                ),
            )
        elif pattern is OrchestrationPattern.ORCHESTRATOR_WORKER:
            tasks = orchestrator_worker(
                execution.id,
                step("coordinate", f"Break down and coordinate: {execution.objective}"),
                [step(f"part {i + 1}", item) for i, item in enumerate(items[:6])],
                step("synthesise", "Combine the worker results."),
            )
        elif pattern is OrchestrationPattern.DYNAMIC_DAG:
            # Without a model there is no graph to infer, so run the stated
            # requirements in parallel and merge, which is the safe shape.
            branches = [
                step(f"requirement {index + 1}", item)
                for index, item in enumerate(items[:8])
            ]
            tasks = parallel(
                execution.id,
                branches,
                merge=step(
                    "synthesise",
                    "Combine the results into a single answer to the objective, "
                    "noting anything that could not be established.",
                ),
            )
        else:
            tasks = sequential(
                execution.id,
                [step(f"step {index + 1}", item) for index, item in enumerate(items[:8])],
            )

        if choice.use_evaluator and tasks:
            final = tasks[-1]
            critic = evaluator_optimizer(
                execution.id,
                Step(name="_placeholder", objective=""),
                step(
                    "evaluate",
                    "Judge whether the work so far satisfies the objective and "
                    "the success criteria. Report specifically what is missing.",
                ),
            )[1]
            critic.dependencies = [final.id]
            critic.metadata = {**critic.metadata, "optimizes": final.id}
            final.metadata = {
                **final.metadata,
                "optimized_by": critic.id,
                "max_iterations": execution.limits.max_optimizer_iterations,
            }
            tasks.append(critic)

        return tasks

    # -- helpers -----------------------------------------------------------

    def _validate(self, plan: Plan) -> None:
        graph = TaskGraph(plan.tasks)
        issues = graph.issues()
        if not issues:
            return
        # Repair what is safely repairable (dangling references), reject cycles.
        cycles = [i for i in issues if i.code == "cycle"]
        if cycles:
            raise InvalidWorkflow(
                "generated plan contains dependency cycles: "
                + "; ".join(i.message for i in cycles),
                issues=[i.message for i in issues],
            )
        known = {task.id for task in plan.tasks}
        for task in plan.tasks:
            task.dependencies = [d for d in task.dependencies if d in known and d != task.id]
            if task.parent_id not in known:
                task.parent_id = None
        TaskGraph(plan.tasks).validate()

    @staticmethod
    def _observations(execution: Execution, *, include_failures: bool = False) -> str:
        lines: list[str] = []
        for task in execution.tasks.values():
            if task.result is not None:
                lines.append(
                    f"- [{task.status.value}] {task.name}: "
                    f"{(task.result.summary or '')[:300]}"
                )
            elif task.is_terminal:
                lines.append(f"- [{task.status.value}] {task.name}")
        if include_failures:
            for failure in execution.failures[-5:]:
                lines.append(
                    f"- FAILURE [{failure.category.value}] {failure.message[:200]}"
                )
        return "\n".join(lines[-40:])

    def _audit(self, event: str, **payload: Any) -> None:
        if self.audit is not None:
            self.audit.record(event, **payload)
