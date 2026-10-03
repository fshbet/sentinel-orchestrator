"""Domain model, state machine, and state store."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from conftest import run

from orchestrator.core.domain.enums import (
    Confidence,
    ExecutionStatus,
    RiskLevel,
    TaskStatus,
)
from orchestrator.core.domain.ids import deterministic_id, new_id
from orchestrator.core.domain.models import (
    Approval,
    Evidence,
    Execution,
    Requirements,
    SuccessCriterion,
    Task,
    TaskResult,
    Usage,
)
from orchestrator.core.state.machine import (
    assert_execution_transition,
    assert_task_transition,
    can_transition_execution,
    can_transition_task,
)
from orchestrator.core.state.manager import StateManager
from orchestrator.core.state.memory_store import InMemoryStateStore
from orchestrator.core.state.sqlite_store import SQLiteStateStore
from orchestrator.core.state.store import ConcurrentModification
from orchestrator.errors import InvalidStateTransition, NotFound
from orchestrator.observability.audit import AuditLog

# -- domain model ----------------------------------------------------------


def test_execution_round_trips_through_json():
    execution = Execution(objective="do the thing")
    task = Task(execution_id=execution.id, name="step", risk=RiskLevel.MEDIUM)
    task.result = TaskResult(
        task_id=task.id,
        summary="done",
        confidence=Confidence.LIKELY,
        evidence=[Evidence(summary="observed")],
        usage=Usage(model_calls=2, input_tokens=100),
    )
    execution.tasks[task.id] = task
    execution.requirements = Requirements(
        explicit=["a"], success_criteria=[SuccessCriterion(description="c")]
    )
    execution.approvals.append(Approval(execution_id=execution.id, prompt="ok?"))

    restored = Execution.from_dict(json.loads(json.dumps(execution.to_dict())))

    assert restored.id == execution.id
    assert isinstance(restored.tasks[task.id], Task)
    assert restored.tasks[task.id].risk is RiskLevel.MEDIUM
    assert restored.tasks[task.id].result.confidence is Confidence.LIKELY
    assert restored.tasks[task.id].result.usage.model_calls == 2
    assert restored.requirements.success_criteria[0].description == "c"
    assert restored.approvals[0].prompt == "ok?"
    assert restored.created_at.tzinfo is not None


def test_ids_are_prefixed_and_time_ordered():
    first, second = new_id("exe"), new_id("exe")
    assert first.startswith("exe_") and second.startswith("exe_")
    assert first != second
    assert deterministic_id("op", "a", "b") == deterministic_id("op", "a", "b")
    assert deterministic_id("op", "a", "b") != deterministic_id("op", "a", "c")


def test_usage_addition_is_pure():
    a, b = Usage(model_calls=1, cost=0.5), Usage(model_calls=2, cost=0.25)
    total = a.add(b)
    assert total.model_calls == 3 and total.cost == 0.75
    assert a.model_calls == 1  # unchanged


# -- state machine ---------------------------------------------------------


def test_invalid_execution_transitions_are_rejected():
    # The invariant that matters: nothing reaches COMPLETED except via REVIEWING.
    assert not can_transition_execution(ExecutionStatus.RUNNING, ExecutionStatus.COMPLETED)
    assert not can_transition_execution(ExecutionStatus.READY, ExecutionStatus.COMPLETED)
    assert can_transition_execution(ExecutionStatus.REVIEWING, ExecutionStatus.COMPLETED)
    with pytest.raises(InvalidStateTransition):
        assert_execution_transition(ExecutionStatus.CREATED, ExecutionStatus.COMPLETED)


def test_terminal_execution_states_have_no_exits():
    for terminal in (
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    ):
        for target in ExecutionStatus:
            if target is terminal:
                continue
            assert not can_transition_execution(terminal, target)


def test_terminal_task_states_have_no_exits():
    for terminal in (
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
        TaskStatus.CANCELLED,
    ):
        assert not can_transition_task(terminal, TaskStatus.READY)
    with pytest.raises(InvalidStateTransition):
        assert_task_transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)


def test_state_manager_records_every_transition(state_manager: StateManager):
    async def scenario():
        execution = await state_manager.create_execution("objective")
        state_manager.transition(execution, ExecutionStatus.PLANNING, reason="plan")
        state_manager.transition(execution, ExecutionStatus.READY)
        await state_manager.persist(execution)
        events = await state_manager.store.audit(execution.id)
        return execution, events

    execution, events = run(scenario())
    assert execution.status is ExecutionStatus.READY
    types = [e.type for e in events]
    assert types[0] == "execution.created"
    assert types.count("execution.transition") == 2
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))


def test_transition_to_same_state_is_a_no_op(state_manager: StateManager):
    async def scenario():
        execution = await state_manager.create_execution("objective")
        state_manager.transition(execution, ExecutionStatus.CREATED)
        await state_manager.persist(execution)
        return await state_manager.store.audit(execution.id)

    events = run(scenario())
    assert [e.type for e in events] == ["execution.created"]


# -- persistence -----------------------------------------------------------


def test_sqlite_store_persists_across_connections():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.db"

        async def write():
            store = SQLiteStateStore(path)
            manager = StateManager(store, AuditLog(store))
            execution = await manager.create_execution("survive a restart")
            manager.transition(execution, ExecutionStatus.PLANNING)
            await manager.persist(execution)
            await store.close()
            return execution.id

        execution_id = run(write())

        async def read():
            store = SQLiteStateStore(path)
            execution = await store.get(execution_id)
            events = await store.audit(execution_id)
            await store.close()
            return execution, events

        execution, events = run(read())

    assert execution.status is ExecutionStatus.PLANNING
    assert execution.revision == 1
    assert len(events) == 2


def test_optimistic_concurrency_rejects_a_stale_write():
    async def scenario():
        store = InMemoryStateStore()
        manager = StateManager(store, AuditLog(store))
        execution = await manager.create_execution("contended")
        first = await store.get(execution.id)
        second = await store.get(execution.id)

        first.summary = "writer one"
        await store.save(first, expected_revision=first.revision)

        second.summary = "writer two"
        with pytest.raises(ConcurrentModification):
            await store.save(second, expected_revision=second.revision)

        return await store.get(execution.id)

    final = run(scenario())
    assert final.summary == "writer one"


def test_missing_execution_raises_not_found():
    async def scenario():
        store = InMemoryStateStore()
        with pytest.raises(NotFound):
            await store.get("exe_missing")

    run(scenario())


def test_idempotency_log_returns_the_first_result():
    with tempfile.TemporaryDirectory() as directory:

        async def scenario():
            store = SQLiteStateStore(Path(directory) / "state.db")
            is_new, value = await store.record_operation("key", {"id": 1})
            replayed_new, replayed = await store.record_operation("key", {"id": 2})
            looked_up = await store.lookup_operation("key")
            await store.close()
            return is_new, value, replayed_new, replayed, looked_up

        is_new, value, replayed_new, replayed, looked_up = run(scenario())

    assert is_new is True and value == {"id": 1}
    assert replayed_new is False and replayed == {"id": 1}
    assert looked_up == {"id": 1}


def test_listing_filters_by_status():
    async def scenario():
        store = InMemoryStateStore()
        manager = StateManager(store, AuditLog(store))
        first = await manager.create_execution("one")
        await manager.create_execution("two")
        manager.transition(first, ExecutionStatus.PLANNING)
        await manager.persist(first)
        planning = await store.list(status=ExecutionStatus.PLANNING)
        created = await store.list(status=ExecutionStatus.CREATED)
        return planning, created

    planning, created = run(scenario())
    assert [s.objective for s in planning] == ["one"]
    assert [s.objective for s in created] == ["two"]
