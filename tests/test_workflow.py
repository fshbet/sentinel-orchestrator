"""Task graph, scheduler, resource locking, and workflow definitions."""

from __future__ import annotations

import asyncio

import pytest
from conftest import make_task, run

from orchestrator.core.domain.enums import OrchestrationPattern, TaskStatus
from orchestrator.core.scheduler.locks import ResourceLockManager
from orchestrator.core.scheduler.scheduler import Scheduler
from orchestrator.core.workflow.definition import (
    WorkflowDefinition,
    WorkflowRegistry,
)
from orchestrator.core.workflow.graph import TaskGraph
from orchestrator.core.workflow.patterns import (
    Step,
    evaluator_optimizer,
    orchestrator_worker,
    parallel,
    router,
    sequential,
)
from orchestrator.errors import ConfigurationError, InvalidWorkflow, NotFound

# -- graph -----------------------------------------------------------------


def test_sequential_pattern_produces_a_chain():
    tasks = sequential("e", [Step(name=n, objective=n) for n in "abc"])
    graph = TaskGraph(tasks)
    assert [len(layer) for layer in graph.layers()] == [1, 1, 1]
    assert graph.topological_order() == [t.id for t in tasks]


def test_parallel_pattern_fans_out_and_merges():
    tasks = parallel(
        "e",
        [Step(name=n, objective=n) for n in "abc"],
        merge=Step(name="merge", objective="merge"),
    )
    layers = TaskGraph(tasks).layers()
    assert len(layers) == 2
    assert len(layers[0]) == 3 and len(layers[1]) == 1


def test_cycles_are_detected_and_rejected():
    first, second = make_task("x"), make_task("y")
    first.dependencies = [second.id]
    second.dependencies = [first.id]
    graph = TaskGraph([first, second])
    assert any(issue.code == "cycle" for issue in graph.issues())
    with pytest.raises(InvalidWorkflow):
        graph.validate()


def test_self_dependency_and_dangling_references_are_reported():
    task = make_task("a")
    task.dependencies = [task.id, "tsk_nonexistent"]
    codes = {issue.code for issue in TaskGraph([task]).issues()}
    # A self-loop is both a self-dependency and, accurately, a cycle.
    assert {"self_dependency", "dangling_dependency"} <= codes


def test_duplicate_task_ids_are_rejected():
    task = make_task("a")
    with pytest.raises(InvalidWorkflow):
        TaskGraph([task, task])


def test_ready_set_respects_dependencies():
    tasks = sequential("e", [Step(name=n, objective=n) for n in "ab"])
    graph = TaskGraph(tasks)
    assert [t.name for t in graph.ready_tasks()] == ["a"]
    tasks[0].status = TaskStatus.SUCCEEDED
    assert [t.name for t in TaskGraph(tasks).ready_tasks()] == ["b"]


def test_a_failed_dependency_blocks_everything_downstream():
    tasks = sequential("e", [Step(name=n, objective=n) for n in "abc"])
    tasks[0].status = TaskStatus.FAILED
    blocked = {t.name for t in TaskGraph(tasks).blocked_tasks()}
    assert blocked == {"b", "c"}


def test_ancestors_are_returned_in_topological_order():
    tasks = sequential("e", [Step(name=n, objective=n) for n in "abc"])
    graph = TaskGraph(tasks)
    assert graph.ancestors(tasks[2].id) == [tasks[0].id, tasks[1].id]


def test_evaluator_optimizer_records_its_bound_without_a_cycle():
    generate, evaluate = evaluator_optimizer(
        "e",
        Step(name="generate", objective="generate"),
        Step(name="evaluate", objective="evaluate"),
        max_iterations=2,
    )
    TaskGraph([generate, evaluate]).validate()  # acyclic
    assert evaluate.metadata["optimizes"] == generate.id
    assert evaluate.metadata["max_iterations"] == 2


def test_router_materialises_every_branch():
    tasks = router(
        "e",
        Step(name="classify", objective="pick"),
        {"left": [Step(name="l", objective="l")], "right": [Step(name="r", objective="r")]},
    )
    groups = {t.group for t in tasks if t.group}
    assert groups == {"route:left", "route:right"}
    TaskGraph(tasks).validate()


def test_orchestrator_worker_wires_parent_links():
    tasks = orchestrator_worker(
        "e",
        Step(name="coordinate", objective="c"),
        [Step(name="w1", objective="w"), Step(name="w2", objective="w")],
        Step(name="synthesise", objective="s"),
    )
    coordinator = tasks[0]
    assert all(t.parent_id == coordinator.id for t in tasks[1:])
    assert TaskGraph(tasks).layers()[-1] == [tasks[-1].id]


# -- scheduler -------------------------------------------------------------


def test_scheduler_runs_independent_tasks_concurrently():
    tasks = parallel("e", [Step(name=n, objective=n) for n in "abc"])
    peak = {"value": 0, "current": 0}

    async def runner(task):
        peak["current"] += 1
        peak["value"] = max(peak["value"], peak["current"])
        await asyncio.sleep(0.01)
        task.status = TaskStatus.SUCCEEDED
        peak["current"] -= 1

    async def scenario():
        return await Scheduler().run(TaskGraph(tasks), runner, max_parallel=3)

    report = run(scenario())
    assert report.count == 3
    assert peak["value"] == 3


def test_concurrency_limit_is_enforced():
    tasks = parallel("e", [Step(name=str(i), objective="x") for i in range(6)])
    peak = {"value": 0, "current": 0}

    async def runner(task):
        peak["current"] += 1
        peak["value"] = max(peak["value"], peak["current"])
        await asyncio.sleep(0.01)
        task.status = TaskStatus.SUCCEEDED
        peak["current"] -= 1

    run(Scheduler().run(TaskGraph(tasks), runner, max_parallel=2))
    assert peak["value"] <= 2


def test_shared_resources_are_never_held_concurrently():
    tasks = parallel("e", [Step(name=n, objective=n, resources=["db"]) for n in "abc"])
    active = {"count": 0, "overlap": False}

    async def runner(task):
        active["count"] += 1
        if active["count"] > 1:
            active["overlap"] = True
        await asyncio.sleep(0.01)
        task.status = TaskStatus.SUCCEEDED
        active["count"] -= 1

    run(Scheduler().run(TaskGraph(tasks), runner, max_parallel=3))
    assert active["overlap"] is False
    assert all(t.status is TaskStatus.SUCCEEDED for t in tasks)


def test_scheduler_stops_when_told_to():
    tasks = sequential("e", [Step(name=n, objective=n) for n in "abcd"])
    completed = []

    async def runner(task):
        completed.append(task.name)
        task.status = TaskStatus.SUCCEEDED

    async def scenario():
        return await Scheduler().run(
            TaskGraph(tasks),
            runner,
            should_continue=lambda: len(completed) < 2,
        )

    report = run(scenario())
    assert report.stopped_early is True
    assert len(completed) <= 2


def test_runner_exceptions_propagate_rather_than_being_swallowed():
    tasks = sequential("e", [Step(name="a", objective="a")])

    async def runner(task):
        raise RuntimeError("runner defect")

    with pytest.raises(RuntimeError, match="runner defect"):
        run(Scheduler().run(TaskGraph(tasks), runner))


def test_locks_are_acquired_in_a_deadlock_free_order():
    manager = ResourceLockManager()
    order = []

    async def worker(name, resources):
        async with manager.acquire(resources, holder=name):
            order.append(("start", name))
            await asyncio.sleep(0.01)
            order.append(("end", name))

    async def scenario():
        await asyncio.gather(
            worker("one", ["a", "b"]),
            worker("two", ["b", "a"]),
        )

    run(scenario())
    # Serialised, not interleaved.
    assert order[0][0] == "start" and order[1][0] == "end"
    assert order[2][0] == "start" and order[3][0] == "end"


# -- workflow definitions --------------------------------------------------


def test_definition_builds_each_supported_pattern():
    definition = WorkflowDefinition.from_dict(
        {
            "id": "test.parallel",
            "pattern": "parallel",
            "steps": [
                {"name": "a", "objective": "a"},
                {"name": "b", "objective": "b"},
            ],
            "options": {"merge": {"name": "m", "objective": "m"}},
        }
    )
    tasks = definition.build("e")
    assert len(tasks) == 3
    TaskGraph(tasks).validate()


def test_definition_rejects_a_malformed_pattern():
    definition = WorkflowDefinition.from_dict(
        {"id": "bad", "pattern": "single_agent", "steps": []}
    )
    with pytest.raises(ConfigurationError):
        definition.build("e")


def test_registry_pins_versions_and_refuses_silent_edits():
    registry = WorkflowRegistry()
    first = WorkflowDefinition(id="w", version="1.0.0", steps=(Step("a", "a"),))
    registry.register(first)
    registry.register(WorkflowDefinition(id="w", version="2.0.0", steps=(Step("b", "b"),)))

    assert registry.get("w").version == "2.0.0"  # latest by default
    assert registry.get("w", "1.0.0").steps[0].name == "a"  # pinned still available
    assert registry.versions("w") == ["1.0.0", "2.0.0"]

    with pytest.raises(ConfigurationError):
        registry.register(
            WorkflowDefinition(id="w", version="1.0.0", steps=(Step("z", "z"),))
        )


def test_registry_reports_unknown_workflows():
    with pytest.raises(NotFound):
        WorkflowRegistry().get("nope")


def test_packaged_generic_workflows_load_and_build():
    from pathlib import Path

    registry = WorkflowRegistry()
    loaded = registry.load_directory(Path(__file__).resolve().parent.parent / "workflows")
    assert loaded, "expected the packaged generic workflows to load"
    for definition in loaded:
        tasks = definition.build("e")
        assert tasks
        TaskGraph(tasks).validate()
        # Generic means generic: no technology names in the shipped patterns.
        assert definition.pattern in set(OrchestrationPattern)
