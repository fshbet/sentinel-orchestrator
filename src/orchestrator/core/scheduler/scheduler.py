"""Dependency-aware parallel scheduler.

The scheduler is deliberately dumb about *what* work is: it only knows the
graph, concurrency limits, resource locks, and a cancellation signal. It never
asks a model what to run next (spec sections 31, 106).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from ..domain.models import ResourceLimits, Task
from ..workflow.graph import TaskGraph
from .locks import ResourceLockManager

TaskRunner = Callable[[Task], Awaitable[None]]


@dataclass
class SchedulerReport:
    dispatched: list[str] = field(default_factory=list)
    stopped_early: bool = False
    stop_reason: str = ""
    idle_rounds: int = 0

    @property
    def count(self) -> int:
        return len(self.dispatched)


class Scheduler:
    """Drives a task graph to quiescence, honouring dependencies and limits."""

    def __init__(
        self,
        *,
        locks: ResourceLockManager | None = None,
        limits: ResourceLimits | None = None,
    ) -> None:
        self.locks = locks or ResourceLockManager()
        self.limits = limits or ResourceLimits()

    # -- dispatch decisions ------------------------------------------------

    def dispatchable(
        self, graph: TaskGraph, *, in_flight: Sequence[str] = ()
    ) -> list[Task]:
        """Ready tasks whose declared resources are currently free.

        Ordering is stable (graph order) so repeated runs of the same plan
        dispatch in the same sequence, which keeps failures reproducible.
        """
        running = set(in_flight)
        claimed: set[str] = set()
        for task_id in running:
            task = graph.tasks.get(task_id)
            if task is not None:
                claimed.update(task.resources)

        order = {tid: index for index, tid in enumerate(graph.topological_order())}
        candidates = sorted(graph.ready_tasks(), key=lambda t: order.get(t.id, 0))

        out: list[Task] = []
        for task in candidates:
            if task.id in running:
                continue
            resources = set(task.resources)
            if resources & claimed:
                continue
            if self.locks.busy(sorted(resources)):
                continue
            claimed.update(resources)
            out.append(task)
        return out

    # -- run loop ----------------------------------------------------------

    async def run(
        self,
        graph: TaskGraph,
        runner: TaskRunner,
        *,
        should_continue: Callable[[], bool] | None = None,
        max_parallel: int | None = None,
        on_round: Callable[[], Awaitable[None]] | None = None,
    ) -> SchedulerReport:
        """Execute the graph until it is complete or told to stop.

        ``runner`` owns the outcome of a task: it must move the task to a
        terminal status (or back to a runnable one) so the loop makes progress.
        """
        report = SchedulerReport()
        limit = max_parallel or self.limits.max_parallel_tasks
        limit = max(1, limit)
        pending: dict[asyncio.Task[None], str] = {}

        def keep_going() -> bool:
            return should_continue() if should_continue is not None else True

        try:
            while True:
                if not keep_going():
                    report.stopped_early = True
                    report.stop_reason = report.stop_reason or "stop requested"
                    break

                in_flight = list(pending.values())
                if len(pending) < limit:
                    for task in self.dispatchable(graph, in_flight=in_flight):
                        if len(pending) >= limit:
                            break
                        handle = asyncio.ensure_future(self._guarded(runner, task))
                        pending[handle] = task.id
                        in_flight.append(task.id)
                        report.dispatched.append(task.id)

                if not pending:
                    if graph.is_complete() or not graph.ready_tasks():
                        break
                    # Ready work exists but every candidate is resource-blocked;
                    # yield and try again rather than spinning hot.
                    report.idle_rounds += 1
                    if report.idle_rounds > 1000:
                        report.stopped_early = True
                        report.stop_reason = "scheduler made no progress"
                        break
                    await asyncio.sleep(0.005)
                    continue

                done, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for handle in done:
                    pending.pop(handle, None)
                    exc = handle.exception()
                    if exc is not None:
                        # The runner is expected to convert failures into task
                        # state; anything escaping it is a real defect.
                        raise exc
                if on_round is not None:
                    await on_round()
        finally:
            for handle in pending:
                handle.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return report

    async def _guarded(self, runner: TaskRunner, task: Task) -> None:
        if not task.resources:
            await runner(task)
            return
        async with self.locks.acquire(sorted(set(task.resources)), holder=task.id):
            await runner(task)
