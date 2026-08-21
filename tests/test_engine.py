"""End-to-end orchestration behaviour.

These exercise the engine itself, not an example application: dynamic planning,
parallelism, dependency ordering, agent selection, validation gates, recovery,
model fallback, approvals, pause, resume, cancellation, limits, and versioning
(spec sections 72, 73, 74).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from conftest import (
    build_platform,
    failing_worker_model,
    planning_model,
    make_config,
    run,
)

from orchestrator.core.domain.enums import (
    Confidence,
    ExecutionStatus,
    OrchestrationPattern,
    RiskLevel,
    TaskStatus,
)
from orchestrator.core.domain.models import ResourceLimits
from orchestrator.llm.providers.scripted import CallableProvider, ScriptedProvider
from orchestrator.platform import Orchestrator


def _tasks(*specs):
    return [dict(spec) for spec in specs]


# -- the simple path -------------------------------------------------------


def test_a_simple_objective_completes_with_evidence():
    async def scenario():
        platform = await build_platform(planning_model())
        execution = await platform.run("Summarise the material.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert execution.confidence is Confidence.CONFIRMED
    assert all(t.status is TaskStatus.SUCCEEDED for t in execution.tasks.values())
    assert execution.validations
    assert all(v.evidence for v in execution.validations)


def test_with_no_model_configured_the_run_escalates_instead_of_pretending():
    """Planning still works without a provider; doing the work does not.

    The honest outcome is to stop and ask a human, not to invent a result.
    """

    async def scenario():
        platform = await build_platform(None)
        execution = await platform.run("Do the first thing. Do the second thing.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.WAITING
    assert execution.plan is not None and execution.tasks
    assert execution.failures
    assert execution.pending_approval() is not None


def test_with_no_model_and_no_escalation_the_run_fails_honestly():
    async def scenario():
        platform = await build_platform(
            None, recovery={"allow_human_escalation": False}
        )
        execution = await platform.run("Do the thing.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.FAILED
    assert execution.confidence is Confidence.FAILED
    assert execution.failures


# -- planning and graph shape ---------------------------------------------

def test_dependencies_are_respected_in_order():
    order: list[str] = []

    def worker(request):
        body = request.messages[0].content
        for name in ("first", "second", "third"):
            if f"Do the {name}" in body:
                order.append(name)
                break
        return "done"

    provider = planning_model(
        tasks=_tasks(
            {"key": "a", "name": "a", "objective": "Do the first part."},
            {"key": "b", "name": "b", "objective": "Do the second part.", "depends_on": ["a"]},
            {"key": "c", "name": "c", "objective": "Do the third part.", "depends_on": ["b"]},
        ),
        worker=worker,
    )

    async def scenario():
        platform = await build_platform(provider)
        execution = await platform.run("Three ordered steps.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert order == ["first", "second", "third"]


def test_independent_tasks_run_concurrently():
    active = {"current": 0, "peak": 0}

    async def worker(request):
        active["current"] += 1
        active["peak"] = max(active["peak"], active["current"])
        await asyncio.sleep(0.02)
        active["current"] -= 1
        return "branch complete"

    provider = planning_model(
        tasks=_tasks(
            {"key": "a", "name": "a", "objective": "Branch A."},
            {"key": "b", "name": "b", "objective": "Branch B."},
            {"key": "c", "name": "c", "objective": "Branch C."},
        ),
        worker=worker,
    )

    async def scenario():
        platform = await build_platform(provider)
        execution = await platform.run("Three independent branches.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert active["peak"] > 1


def test_shared_resources_serialise_otherwise_parallel_tasks():
    active = {"current": 0, "overlap": False}

    async def worker(request):
        active["current"] += 1
        if active["current"] > 1:
            active["overlap"] = True
        await asyncio.sleep(0.02)
        active["current"] -= 1
        return "done"

    provider = planning_model(
        tasks=_tasks(
            {"key": "a", "name": "a", "objective": "A.", "resources": ["shared"]},
            {"key": "b", "name": "b", "objective": "B.", "resources": ["shared"]},
        ),
        worker=worker,
    )

    async def scenario():
        platform = await build_platform(provider)
        execution = await platform.run("Two tasks over one resource.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert active["overlap"] is False


def test_a_failed_task_skips_everything_downstream():
    def worker(request):
        body = request.messages[0].content
        return "" if "Fail here" in body else "fine"

    provider = planning_model(
        tasks=_tasks(
            {"key": "a", "name": "a", "objective": "Fail here."},
            {"key": "b", "name": "b", "objective": "Never runs.", "depends_on": ["a"]},
        ),
        worker=worker,
    )

    async def scenario():
        platform = await build_platform(
            provider,
            limits={"max_task_attempts": 1, "max_replans": 0},
            recovery={"allow_human_escalation": False},
        )
        execution = await platform.run("A then B.")
        await platform.close()
        return execution

    execution = run(scenario())
    statuses = {t.name: t.status for t in execution.tasks.values()}
    assert statuses["a"] is TaskStatus.FAILED
    assert statuses["b"] in (TaskStatus.SKIPPED, TaskStatus.CANCELLED)
    assert execution.status is ExecutionStatus.FAILED


# -- agents ----------------------------------------------------------------


def test_an_agent_is_created_for_an_uncovered_capability():
    provider = planning_model(
        tasks=_tasks(
            {
                "key": "a",
                "name": "specialised",
                "objective": "Do specialised work.",
                "capabilities": ["special"],
            }
        )
    )

    async def scenario():
        platform = await build_platform(provider, capabilities=["special"])
        execution = await platform.run("Specialised work.")
        agents = platform.agents.list()
        await platform.close()
        return execution, agents

    execution, agents = run(scenario())
    task = next(iter(execution.tasks.values()))
    assert task.assigned_agent.startswith("dynamic:")
    created = next(a for a in agents if a.id == task.assigned_agent)
    assert created.ephemeral is True
    assert created.capabilities == ["special"]


def test_a_registered_agent_is_preferred_over_creating_one():
    provider = planning_model(
        tasks=_tasks(
            {
                "key": "a",
                "name": "t",
                "objective": "Work.",
                "capabilities": ["analysis"],
            }
        )
    )
    config = make_config(
        capabilities=["analysis"],
        agents={
            "definitions": [
                {
                    "id": "analyst",
                    "description": "Registered analyst.",
                    "capabilities": ["analysis"],
                    "tools": ["orchestrator.*"],
                }
            ]
        },
    )

    async def scenario():
        platform = await Orchestrator.create(
            config=config, providers=[provider], connect_mcp=False
        )
        execution = await platform.run("Analyse something.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert next(iter(execution.tasks.values())).assigned_agent == "analyst"


def test_an_agent_only_sees_the_tools_it_was_granted():
    seen: dict[str, list[str]] = {}

    def worker(request):
        seen["tools"] = [tool["name"] for tool in request.tools]
        return "done"

    provider = planning_model(worker=worker)
    config = make_config(
        tools={
            "bookkeeping": {"enabled": True},
            "filesystem": {"enabled": True, "root": ".", "allow_write": True},
        },
        agents={
            "definitions": [
                {
                    "id": "reader",
                    "capabilities": [],
                    "tools": ["fs.read_file"],
                    "permissions": ["fs.read"],
                }
            ]
        },
    )

    async def scenario():
        platform = await Orchestrator.create(
            config=config, providers=[provider], connect_mcp=False
        )
        await platform.run("Read something.")
        await platform.close()

    run(scenario())
    assert seen["tools"] == ["fs.read_file"]


# -- validation and recovery ----------------------------------------------


def test_validation_failure_triggers_recovery_and_can_succeed():
    async def scenario():
        platform = await build_platform(failing_worker_model(failures=1))
        execution = await platform.run("Produce something.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert execution.failures, "the first attempt should be recorded as a failure"
    assert execution.recoveries
    assert any(f.recovered for f in execution.failures)


def test_repeated_failure_stops_rather_than_looping_forever():
    async def scenario():
        platform = await build_platform(
            failing_worker_model(failures=99),
            limits={"max_task_attempts": 2, "max_replans": 1},
            recovery={"allow_human_escalation": False},
        )
        execution = await platform.run("Produce something.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.FAILED
    assert len(execution.failures) <= 12  # bounded, not runaway


def test_a_claim_of_success_cannot_pass_a_failing_gate():
    """The core invariant: the agent says done, the validator says no."""

    provider = planning_model(
        tasks=_tasks(
            {
                "key": "a",
                "name": "a",
                "objective": "Produce a report containing the word APPROVED.",
                "validation": {
                    "validator": "pattern",
                    "config": {"pattern": "APPROVED"},
                },
            }
        ),
        worker="I have completed the task successfully. Everything is done.",
    )

    async def scenario():
        platform = await build_platform(
            provider,
            limits={"max_task_attempts": 1, "max_replans": 0},
            recovery={"allow_human_escalation": False},
        )
        execution = await platform.run("Produce an approved report.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.FAILED
    assert any(not v.passed for v in execution.validations)


def test_model_failure_falls_back_to_another_model():
    broken = ScriptedProvider(
        [__import__("orchestrator.errors", fromlist=["ModelUnavailable"]).ModelUnavailable("down")],
        name="broken",
        model_id="broken/model",
        repeat_last=True,
    )
    broken.models()[0].priority = 1
    working = planning_model()
    working.models()[0].priority = 5

    async def scenario():
        platform = await Orchestrator.create(
            config=make_config(), providers=[broken, working], connect_mcp=False
        )
        execution = await platform.run("Do the thing.")
        events = await platform.audit(execution.id)
        await platform.close()
        return execution, events

    execution, events = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert any(e.type == "model.fallback" for e in events)


def test_a_tool_failure_is_observed_by_the_agent_not_fatal():
    calls = {"count": 0}

    def worker(request):
        calls["count"] += 1
        if calls["count"] == 1:
            from orchestrator.llm.providers.scripted import response_with_tool_call

            return response_with_tool_call(
                "orchestrator.record_note", {"key": "", "value": "x"}
            )
        body = request.messages[0].content
        assert "record_note" in body
        return "recovered after the tool refused the call"

    provider = planning_model(worker=worker)

    async def scenario():
        platform = await build_platform(provider)
        execution = await platform.run("Use a tool.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert calls["count"] >= 2


# -- human in the loop -----------------------------------------------------


def test_a_high_risk_task_waits_for_approval_then_continues():
    provider = planning_model(
        tasks=_tasks(
            {
                "key": "a",
                "name": "risky",
                "objective": "Do something irreversible.",
                "risk": "critical",
            }
        )
    )

    async def scenario():
        platform = await build_platform(provider)
        waiting = await platform.run("Do something irreversible.")
        approval = waiting.pending_approval()
        resumed = await platform.approve(waiting.id, approval.id, approved=True)
        await platform.close()
        return waiting, approval, resumed

    waiting, approval, resumed = run(scenario())
    assert waiting.status is ExecutionStatus.WAITING
    assert approval is not None and approval.risk is RiskLevel.CRITICAL
    assert resumed.status is ExecutionStatus.COMPLETED


def test_a_rejected_approval_skips_the_task():
    provider = planning_model(
        tasks=_tasks(
            {
                "key": "a",
                "name": "risky",
                "objective": "Do something irreversible.",
                "risk": "critical",
            }
        )
    )

    async def scenario():
        platform = await build_platform(provider)
        waiting = await platform.run("Do something irreversible.")
        approval = waiting.pending_approval()
        resumed = await platform.approve(waiting.id, approval.id, approved=False)
        await platform.close()
        return resumed

    resumed = run(scenario())
    task = next(iter(resumed.tasks.values()))
    assert task.status is TaskStatus.SKIPPED


def test_a_waiting_execution_stays_waiting_until_answered():
    provider = planning_model(
        tasks=_tasks(
            {"key": "a", "name": "risky", "objective": "Risky.", "risk": "high"}
        )
    )

    async def scenario():
        platform = await build_platform(provider)
        first = await platform.run("Risky work.")
        second = await platform.resume(first.id)
        await platform.close()
        return first, second

    first, second = run(scenario())
    assert first.status is ExecutionStatus.WAITING
    assert second.status is ExecutionStatus.WAITING  # still blocked on a human


# -- control ---------------------------------------------------------------


def test_cancellation_stops_work_and_marks_tasks_cancelled():
    async def scenario():
        platform = await build_platform(planning_model())
        execution = await platform.start("Long objective.")
        await platform.cancel(execution.id, reason="operator changed their mind")
        after = await platform.engine.run(execution.id)
        await platform.close()
        return after

    execution = run(scenario())
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.confidence is Confidence.BLOCKED
    assert all(t.is_terminal for t in execution.tasks.values())


def test_a_cancelled_execution_cannot_silently_continue():
    async def scenario():
        platform = await build_platform(planning_model())
        execution = await platform.start("Objective.")
        await platform.cancel(execution.id)
        cancelled = await platform.engine.run(execution.id)
        again = await platform.engine.run(execution.id)
        await platform.close()
        return cancelled, again

    cancelled, again = run(scenario())
    assert cancelled.status is ExecutionStatus.CANCELLED
    assert again.status is ExecutionStatus.CANCELLED
    assert not again.tasks or all(t.is_terminal for t in again.tasks.values())


def test_pause_and_resume_preserve_progress():
    async def scenario():
        platform = await build_platform(planning_model())
        execution = await platform.start("Objective.")
        await platform.pause(execution.id)
        paused = await platform.engine.run(execution.id)
        resumed = await platform.resume(execution.id)
        await platform.close()
        return paused, resumed

    paused, resumed = run(scenario())
    assert paused.status is ExecutionStatus.PAUSED
    assert resumed.status is ExecutionStatus.COMPLETED


def test_state_survives_a_restart_and_resumes_rather_than_restarting():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        database = str(Path(directory) / "state.db")

        async def first_process():
            platform = await Orchestrator.create(
                config=make_config(storage={"backend": "sqlite", "path": database}),
                providers=[planning_model()],
                connect_mcp=False,
            )
            execution = await platform.start("Survive a restart.")
            await platform.pause(execution.id)
            await platform.engine.run(execution.id)
            await platform.close()
            return execution.id

        execution_id = run(first_process())

        async def second_process():
            platform = await Orchestrator.create(
                config=make_config(storage={"backend": "sqlite", "path": database}),
                providers=[planning_model()],
                connect_mcp=False,
            )
            resumed = await platform.resume(execution_id)
            await platform.close()
            return resumed

        resumed = run(second_process())

    assert resumed.id == execution_id
    assert resumed.status is ExecutionStatus.COMPLETED


def test_interrupted_in_flight_tasks_are_rewound_not_duplicated():
    from orchestrator.adapters.workflow.base import LocalWorkflowBackend
    from orchestrator.core.state.memory_store import InMemoryStateStore
    from orchestrator.core.state.manager import StateManager
    from orchestrator.observability.audit import AuditLog

    async def scenario():
        store = InMemoryStateStore()
        manager = StateManager(store, AuditLog(store))
        execution = await manager.create_execution("interrupted")
        from conftest import make_task

        finished = make_task("finished")
        finished.status = TaskStatus.SUCCEEDED
        in_flight = make_task("in flight")
        in_flight.status = TaskStatus.RUNNING
        execution.tasks[finished.id] = finished
        execution.tasks[in_flight.id] = in_flight
        execution.status = ExecutionStatus.RUNNING
        await store.save(execution)

        recovered = await LocalWorkflowBackend(store).recover_interrupted()
        return recovered[0]

    recovered = run(scenario())
    statuses = {t.name: t.status for t in recovered.tasks.values()}
    assert statuses["finished"] is TaskStatus.SUCCEEDED  # not redone
    assert statuses["in flight"] is TaskStatus.READY  # rewound
    assert recovered.status is ExecutionStatus.READY


# -- limits and governance -------------------------------------------------


def test_a_model_call_budget_stops_the_run():
    async def scenario():
        platform = await build_platform(planning_model())
        execution = await platform.run(
            "Do the thing.", limits=ResourceLimits(max_model_calls=1)
        )
        events = await platform.audit(execution.id)
        await platform.close()
        return execution, events

    execution, events = run(scenario())
    assert execution.status is ExecutionStatus.FAILED
    assert any(e.type == "limit.exceeded" for e in events)


# -- reproducibility -------------------------------------------------------


def test_an_execution_pins_the_versions_it_ran_with():
    config = make_config(
        agents={
            "definitions": [
                {"id": "worker", "version": "2.1.0", "capabilities": [], "tools": []}
            ]
        }
    )

    async def scenario():
        platform = await Orchestrator.create(
            config=config, providers=[planning_model()], connect_mcp=False
        )
        execution = await platform.run("Do the thing.")
        await platform.close()
        return execution, config.fingerprint()

    execution, fingerprint = run(scenario())
    assert execution.workflow.agent_versions["worker"] == "2.1.0"
    assert execution.workflow.config_fingerprint == fingerprint
    assert execution.plan_version >= 1


def test_the_audit_trail_is_complete_and_ordered():
    async def scenario():
        platform = await build_platform(planning_model())
        execution = await platform.run("Do the thing.")
        events = await platform.audit(execution.id)
        await platform.close()
        return events

    events = run(scenario())
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    types = {e.type for e in events}
    for required in (
        "execution.created",
        "goal.analysed",
        "pattern.selected",
        "plan.created",
        "agent.created",
        "model.selected",
        "task.started",
        "validation.result",
        "gate.result",
        "execution.completed",
    ):
        assert required in types, f"missing audit event {required}"


def test_concurrent_executions_do_not_interfere():
    async def scenario():
        platform = await build_platform(planning_model())
        results = await asyncio.gather(
            *(platform.run(f"Objective {i}.") for i in range(4))
        )
        listed = await platform.list()
        await platform.close()
        return results, listed

    results, listed = run(scenario())
    assert all(e.status is ExecutionStatus.COMPLETED for e in results)
    assert len({e.id for e in results}) == 4
    assert len(listed) == 4


# -- meta-orchestration ----------------------------------------------------


def test_a_trivial_objective_does_not_get_an_elaborate_plan():
    async def scenario():
        platform = await build_platform(None)
        execution = await platform.start("What is 2 + 2?")
        await platform.engine._understand(execution)
        await platform.engine._plan(execution)
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.plan.pattern is OrchestrationPattern.SINGLE_AGENT
    assert len(execution.tasks) == 1


def test_forcing_a_pattern_is_honoured():
    async def scenario():
        platform = await build_platform(None)
        execution = await platform.start(
            "Do the thing.", pattern=OrchestrationPattern.PARALLEL
        )
        await platform.engine._understand(execution)
        await platform.engine._plan(execution)
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.plan.pattern is OrchestrationPattern.PARALLEL


def test_an_iterative_plan_continues_until_it_says_it_is_done():
    rounds = {"count": 0}

    def respond(request):
        system = request.system or ""
        if "extract structured requirements" in system:
            return {"explicit": ["work"], "success_criteria": [{"description": "done"}]}
        if "decompose an objective" in system:
            rounds["count"] += 1
            return {
                "tasks": [
                    {
                        "key": f"round{rounds['count']}",
                        "name": f"round {rounds['count']}",
                        "objective": "Do the next step.",
                    }
                ],
                "complete": rounds["count"] >= 3,
            }
        return "step done"

    async def scenario():
        platform = await build_platform(CallableProvider(respond))
        execution = await platform.run(
            "Figure out what is wrong and fix it, whatever it takes."
        )
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert rounds["count"] >= 2
    assert len(execution.tasks) >= 2


def test_the_evaluator_optimizer_loop_is_bounded():
    verdicts = {"count": 0}

    def respond(request):
        system = request.system or ""
        if "extract structured requirements" in system:
            return {"explicit": ["work"], "success_criteria": [{"description": "done"}]}
        if "decompose an objective" in system:
            return {
                "tasks": [
                    {"key": "gen", "name": "generate", "objective": "Produce the work."},
                    {
                        "key": "eval",
                        "name": "evaluate",
                        "objective": "Judge the work.",
                        "depends_on": ["gen"],
                    },
                ]
            }
        if "Judge the work" in request.messages[0].content:
            verdicts["count"] += 1
            return {"passed": False, "reason": "still not good enough"}
        return "a draft of the work"

    async def scenario():
        platform = await build_platform(
            CallableProvider(respond), limits={"max_optimizer_iterations": 2}
        )
        execution = await platform.start("Iterate until the result is good enough.")
        # Wire the loop the way a planner would.
        await platform.engine._understand(execution)
        await platform.engine._plan(execution)
        tasks = list(execution.tasks.values())
        generate = next(t for t in tasks if t.name == "generate")
        evaluate = next(t for t in tasks if t.name == "evaluate")
        evaluate.metadata["optimizes"] = generate.id
        evaluate.metadata["max_iterations"] = 2
        await platform.state.persist(execution)
        final = await platform.engine.run(execution.id)
        await platform.close()
        return final

    execution = run(scenario())
    assert verdicts["count"] <= 3  # bounded, never unbounded self-reflection
    assert execution.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)
