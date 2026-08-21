"""Composable orchestration patterns.

Each builder turns a list of abstract steps into task-graph fragments. They are
compositional on purpose: a plan may be a parallel fan-out whose merge step is
itself an evaluator-optimizer loop. Nothing here knows what the work is about
(spec sections 5, 92).

Controlled iteration (evaluator-optimizer) is expressed as bounded repetition
driven by the engine, never as a cycle in the graph, so the DAG stays acyclic
and every loop has a hard stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..domain.enums import ModelCapability, OrchestrationPattern, RiskLevel
from ..domain.models import Task, ValidationSpec


@dataclass
class Step:
    """A pattern-neutral description of one unit of work."""

    name: str
    objective: str
    capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    model_requirements: list[ModelCapability] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    expected_outputs: list[str] = field(default_factory=list)
    validations: list[ValidationSpec] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    max_attempts: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_task(self, execution_id: str, **overrides: Any) -> Task:
        task = Task(
            execution_id=execution_id,
            name=self.name,
            objective=self.objective,
            required_capabilities=list(self.capabilities),
            allowed_tools=list(self.tools),
            model_requirements=list(self.model_requirements),
            inputs=dict(self.inputs),
            expected_outputs=list(self.expected_outputs),
            validations=list(self.validations),
            completion_criteria=list(self.completion_criteria),
            resources=list(self.resources),
            risk=self.risk,
            requires_approval=self.requires_approval,
            max_attempts=self.max_attempts,
            metadata=dict(self.metadata),
        )
        for key, value in overrides.items():
            setattr(task, key, value)
        return task


def sequential(
    execution_id: str, steps: Sequence[Step], *, after: Sequence[str] = ()
) -> list[Task]:
    """A -> B -> C."""
    tasks: list[Task] = []
    previous: list[str] = list(after)
    for step in steps:
        task = step.to_task(
            execution_id,
            dependencies=list(previous),
            pattern=OrchestrationPattern.SEQUENTIAL,
        )
        tasks.append(task)
        previous = [task.id]
    return tasks


def parallel(
    execution_id: str,
    steps: Sequence[Step],
    *,
    merge: Step | None = None,
    after: Sequence[str] = (),
) -> list[Task]:
    """Fan-out into independent branches, then optionally fan-in to a merge."""
    branches = [
        step.to_task(
            execution_id,
            dependencies=list(after),
            pattern=OrchestrationPattern.PARALLEL,
            group="parallel",
        )
        for step in steps
    ]
    tasks = list(branches)
    if merge is not None:
        tasks.append(
            merge.to_task(
                execution_id,
                dependencies=[b.id for b in branches],
                pattern=OrchestrationPattern.PARALLEL,
                group="merge",
            )
        )
    return tasks


def router(
    execution_id: str,
    classify: Step,
    routes: dict[str, Sequence[Step]],
    *,
    after: Sequence[str] = (),
) -> list[Task]:
    """A classification step followed by mutually exclusive branches.

    Every branch is materialised, and the engine skips the branches the router
    did not choose. Recording the unchosen branches keeps the decision auditable
    instead of invisible.
    """
    decision = classify.to_task(
        execution_id,
        dependencies=list(after),
        pattern=OrchestrationPattern.ROUTER,
        expected_outputs=["route"],
        metadata={**classify.metadata, "routes": sorted(routes)},
    )
    tasks: list[Task] = [decision]
    for route_name, steps in routes.items():
        branch = sequential(execution_id, steps, after=[decision.id])
        for task in branch:
            task.pattern = OrchestrationPattern.ROUTER
            task.group = f"route:{route_name}"
            task.metadata = {**task.metadata, "route": route_name}
        tasks.extend(branch)
    return tasks


def orchestrator_worker(
    execution_id: str,
    plan_step: Step,
    worker_steps: Sequence[Step],
    synthesis: Step,
    *,
    after: Sequence[str] = (),
) -> list[Task]:
    """A coordinating task, N workers, and a synthesis task."""
    coordinator = plan_step.to_task(
        execution_id,
        dependencies=list(after),
        pattern=OrchestrationPattern.ORCHESTRATOR_WORKER,
        group="orchestrator",
    )
    workers = [
        step.to_task(
            execution_id,
            dependencies=[coordinator.id],
            parent_id=coordinator.id,
            pattern=OrchestrationPattern.ORCHESTRATOR_WORKER,
            group="worker",
        )
        for step in worker_steps
    ]
    merge = synthesis.to_task(
        execution_id,
        dependencies=[w.id for w in workers] or [coordinator.id],
        parent_id=coordinator.id,
        pattern=OrchestrationPattern.ORCHESTRATOR_WORKER,
        group="synthesis",
    )
    return [coordinator, *workers, merge]


def evaluator_optimizer(
    execution_id: str,
    generate: Step,
    evaluate: Step,
    *,
    max_iterations: int = 3,
    after: Sequence[str] = (),
) -> list[Task]:
    """Generate, then evaluate, with bounded re-generation on failure.

    The loop bound lives in task metadata; the engine re-runs the generate task
    when evaluation fails, up to ``max_iterations``. Unbounded self-reflection
    is not representable (spec section 95).
    """
    producer = generate.to_task(
        execution_id,
        dependencies=list(after),
        pattern=OrchestrationPattern.EVALUATOR_OPTIMIZER,
        group="generate",
    )
    critic = evaluate.to_task(
        execution_id,
        dependencies=[producer.id],
        pattern=OrchestrationPattern.EVALUATOR_OPTIMIZER,
        group="evaluate",
        metadata={
            **evaluate.metadata,
            "optimizes": producer.id,
            "max_iterations": max_iterations,
        },
    )
    producer.metadata = {
        **producer.metadata,
        "optimized_by": critic.id,
        "max_iterations": max_iterations,
    }
    return [producer, critic]


def hierarchical(
    execution_id: str,
    parent: Step,
    children: Sequence[Step],
    *,
    after: Sequence[str] = (),
) -> list[Task]:
    """A sub-orchestrator that owns its own children."""
    root = parent.to_task(
        execution_id,
        dependencies=list(after),
        pattern=OrchestrationPattern.HIERARCHICAL,
        group="sub_orchestrator",
    )
    tasks = [root]
    for step in children:
        tasks.append(
            step.to_task(
                execution_id,
                dependencies=[root.id],
                parent_id=root.id,
                pattern=OrchestrationPattern.HIERARCHICAL,
            )
        )
    return tasks


def single(execution_id: str, step: Step, *, after: Sequence[str] = ()) -> list[Task]:
    """The whole plan is one task. The right answer for simple objectives."""
    return [
        step.to_task(
            execution_id,
            dependencies=list(after),
            pattern=OrchestrationPattern.SINGLE_AGENT,
        )
    ]


BUILDERS = {
    OrchestrationPattern.SINGLE_AGENT: single,
    OrchestrationPattern.SEQUENTIAL: sequential,
    OrchestrationPattern.PARALLEL: parallel,
    OrchestrationPattern.ROUTER: router,
    OrchestrationPattern.ORCHESTRATOR_WORKER: orchestrator_worker,
    OrchestrationPattern.EVALUATOR_OPTIMIZER: evaluator_optimizer,
    OrchestrationPattern.HIERARCHICAL: hierarchical,
}
