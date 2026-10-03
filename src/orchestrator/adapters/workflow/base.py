"""Durable-execution backends.

The engine already survives process death, because every transition is written
to the state store before the next one is attempted and ``run`` is re-entrant
(spec sections 27, 28, 66). This abstraction exists so that guarantee can be
upgraded - to a durable workflow engine such as Temporal, or to a distributed
queue - without the engine changing.

Deliberately no heavyweight backend is required for local use, and none is
shipped pretending to be one. ``LocalWorkflowBackend`` is the real default; a
third-party backend arrives as a plugin implementing this interface.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ...core.domain.enums import ExecutionStatus, TaskStatus
from ...core.domain.models import Execution
from ...core.state.store import StateStore
from ...observability.logging import get_logger

_logger = get_logger("workflow")

RunFn = Callable[[str], Awaitable[Execution]]


@dataclass
class RunHandle:
    execution_id: str
    backend: str
    started_at: float
    detail: dict[str, Any] = field(default_factory=dict)


class WorkflowBackend(abc.ABC):
    """How an execution is driven to completion."""

    name = "backend"

    @abc.abstractmethod
    async def submit(self, execution_id: str, run: RunFn) -> RunHandle:
        """Begin (or resume) driving an execution."""

    @abc.abstractmethod
    async def wait(self, handle: RunHandle, *, timeout: float | None = None) -> Execution:
        """Block until the execution reaches a terminal or waiting state."""

    @abc.abstractmethod
    async def cancel(self, handle: RunHandle) -> None: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class LocalWorkflowBackend(WorkflowBackend):
    """Runs executions in this process, with the state store as the journal.

    Durability comes from the store rather than from an external engine: if the
    process dies mid-run, ``recover_interrupted`` finds executions that were
    left mid-flight and hands them back for a clean resume.
    """

    name = "local"

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._tasks: dict[str, asyncio.Task[Execution]] = {}

    async def submit(self, execution_id: str, run: RunFn) -> RunHandle:
        import time

        existing = self._tasks.get(execution_id)
        if existing is not None and not existing.done():
            return RunHandle(execution_id, self.name, time.monotonic())
        task = asyncio.ensure_future(run(execution_id))
        self._tasks[execution_id] = task
        return RunHandle(execution_id, self.name, time.monotonic())

    async def wait(self, handle: RunHandle, *, timeout: float | None = None) -> Execution:
        task = self._tasks.get(handle.execution_id)
        if task is None:
            return await self.store.get(handle.execution_id)
        if timeout is None:
            return await task
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def cancel(self, handle: RunHandle) -> None:
        task = self._tasks.pop(handle.execution_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def recover_interrupted(self) -> list[Execution]:
        """Find executions that a crash left mid-flight.

        A stored RUNNING or VALIDATING execution with no in-process task behind
        it means the previous process died. The tasks that were in flight are
        moved back to a runnable state so a resume does not double-execute the
        finished ones (spec section 28).
        """
        stranded: list[Execution] = []
        for status in (
            ExecutionStatus.RUNNING,
            ExecutionStatus.VALIDATING,
            ExecutionStatus.RECOVERING,
        ):
            for summary in await self.store.list(status=status, limit=200):
                task = self._tasks.get(summary.id)
                if task is not None and not task.done():
                    continue
                execution = await self.store.get(summary.id)
                changed = False
                for job in execution.tasks.values():
                    if job.status in (TaskStatus.RUNNING, TaskStatus.VALIDATING):
                        # Completed work stays completed; only in-flight work
                        # is rewound.
                        job.status = TaskStatus.READY
                        job.started_at = None
                        changed = True
                if changed:
                    execution.status = ExecutionStatus.READY
                    await self.store.save(execution)
                    _logger.warning(
                        "recovered interrupted execution",
                        extra={"context": {"execution": execution.id}},
                    )
                stranded.append(execution)
        return stranded


class SequentialWorkflowBackend(WorkflowBackend):
    """Runs one execution at a time. Useful for constrained environments."""

    name = "sequential"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._results: dict[str, Execution] = {}

    async def submit(self, execution_id: str, run: RunFn) -> RunHandle:
        import time

        async with self._lock:
            self._results[execution_id] = await run(execution_id)
        return RunHandle(execution_id, self.name, time.monotonic())

    async def wait(self, handle: RunHandle, *, timeout: float | None = None) -> Execution:
        return self._results[handle.execution_id]

    async def cancel(self, handle: RunHandle) -> None:
        return None
