"""The dynamic task graph.

A plan is a directed graph of tasks, not a natural-language description. This
module is pure, deterministic graph mechanics: structural validation, cycle
detection, topological layering, and the ready-set computation the scheduler
polls. No LLM is involved at this level (spec sections 6, 30, 106).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping

from ...errors import InvalidWorkflow
from ..domain.enums import TaskStatus
from ..domain.models import Task


@dataclass(frozen=True)
class GraphIssue:
    code: str
    message: str
    task_id: str | None = None


class TaskGraph:
    """A validated view over a set of tasks and their dependencies."""

    def __init__(self, tasks: Iterable[Task]) -> None:
        self._tasks: dict[str, Task] = {}
        for task in tasks:
            if task.id in self._tasks:
                raise InvalidWorkflow(f"duplicate task id {task.id}", task_id=task.id)
            self._tasks[task.id] = task
        self._dependents: dict[str, set[str]] = {tid: set() for tid in self._tasks}
        for task in self._tasks.values():
            for dep in task.dependencies:
                if dep in self._dependents:
                    self._dependents[dep].add(task.id)

    # -- basic access ------------------------------------------------------

    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self) -> Iterator[Task]:
        return iter(self._tasks.values())

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._tasks

    @property
    def tasks(self) -> Mapping[str, Task]:
        return self._tasks

    def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise InvalidWorkflow(f"unknown task {task_id}", task_id=task_id) from exc

    def dependents(self, task_id: str) -> set[str]:
        return set(self._dependents.get(task_id, set()))

    def roots(self) -> list[Task]:
        return [t for t in self._tasks.values() if not t.dependencies]

    def leaves(self) -> list[Task]:
        return [t for t in self._tasks.values() if not self._dependents[t.id]]

    # -- structural validation --------------------------------------------

    def issues(self) -> list[GraphIssue]:
        """Return every structural problem rather than only the first."""
        found: list[GraphIssue] = []
        for task in self._tasks.values():
            for dep in task.dependencies:
                if dep not in self._tasks:
                    found.append(
                        GraphIssue(
                            "dangling_dependency",
                            f"task {task.id} depends on unknown task {dep}",
                            task.id,
                        )
                    )
                elif dep == task.id:
                    found.append(
                        GraphIssue(
                            "self_dependency",
                            f"task {task.id} depends on itself",
                            task.id,
                        )
                    )
            if task.parent_id and task.parent_id not in self._tasks:
                found.append(
                    GraphIssue(
                        "dangling_parent",
                        f"task {task.id} has unknown parent {task.parent_id}",
                        task.id,
                    )
                )
        for cycle in self.cycles():
            found.append(
                GraphIssue(
                    "cycle",
                    "dependency cycle: " + " -> ".join(cycle),
                    cycle[0],
                )
            )
        return found

    def validate(self) -> None:
        """Raise ``InvalidWorkflow`` if the graph cannot be executed."""
        issues = self.issues()
        if issues:
            raise InvalidWorkflow(
                "; ".join(issue.message for issue in issues),
                issues=[
                    {"code": i.code, "message": i.message, "task_id": i.task_id}
                    for i in issues
                ],
            )

    def cycles(self) -> list[list[str]]:
        """Find dependency cycles using an iterative depth-first search.

        Iterative rather than recursive so a pathological generated plan cannot
        blow the Python stack.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        colour = {tid: WHITE for tid in self._tasks}
        found: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()

        for start in self._tasks:
            if colour[start] != WHITE:
                continue
            stack: list[tuple[str, Iterator[str]]] = [
                (start, iter(self._tasks[start].dependencies))
            ]
            path = [start]
            colour[start] = GREY
            while stack:
                node, deps = stack[-1]
                advanced = False
                for dep in deps:
                    if dep not in self._tasks:
                        continue
                    if colour[dep] == GREY:
                        index = path.index(dep)
                        cycle = path[index:] + [dep]
                        key = tuple(sorted(set(cycle)))
                        if key not in seen:
                            seen.add(key)
                            found.append(cycle)
                        continue
                    if colour[dep] == WHITE:
                        colour[dep] = GREY
                        path.append(dep)
                        stack.append((dep, iter(self._tasks[dep].dependencies)))
                        advanced = True
                        break
                if not advanced:
                    colour[node] = BLACK
                    stack.pop()
                    path.pop()
        return found

    # -- ordering ----------------------------------------------------------

    def layers(self) -> list[list[str]]:
        """Topological layers; every task in a layer may run concurrently."""
        self.validate()
        remaining = {tid: set(t.dependencies) for tid, t in self._tasks.items()}
        result: list[list[str]] = []
        while remaining:
            layer = sorted(tid for tid, deps in remaining.items() if not deps)
            if not layer:  # pragma: no cover - validate() rules this out
                raise InvalidWorkflow("graph is not acyclic")
            result.append(layer)
            for tid in layer:
                del remaining[tid]
            for deps in remaining.values():
                deps.difference_update(layer)
        return result

    def topological_order(self) -> list[str]:
        return [tid for layer in self.layers() for tid in layer]

    # -- runtime queries ---------------------------------------------------

    def ready_tasks(self) -> list[Task]:
        """Tasks whose dependencies have all succeeded and are not yet started."""
        ready: list[Task] = []
        for task in self._tasks.values():
            if task.status not in (TaskStatus.PENDING, TaskStatus.READY):
                continue
            if all(
                self._tasks[dep].status is TaskStatus.SUCCEEDED
                for dep in task.dependencies
                if dep in self._tasks
            ):
                ready.append(task)
        return ready

    def blocked_tasks(self) -> list[Task]:
        """Tasks that can never run because a dependency failed or was skipped."""
        dead = {
            tid
            for tid, task in self._tasks.items()
            if task.status
            in (TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED)
        }
        if not dead:
            return []
        blocked: list[Task] = []
        changed = True
        while changed:
            changed = False
            for task in self._tasks.values():
                if task.id in dead or task.is_terminal:
                    continue
                if any(dep in dead for dep in task.dependencies):
                    blocked.append(task)
                    dead.add(task.id)
                    changed = True
        return blocked

    def is_complete(self) -> bool:
        return all(task.is_terminal for task in self._tasks.values())

    def succeeded(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.status is TaskStatus.SUCCEEDED]

    def failed(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.status is TaskStatus.FAILED]

    def progress(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self._tasks.values():
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        counts["total"] = len(self._tasks)
        return counts

    def ancestors(self, task_id: str) -> list[str]:
        """All transitive dependencies of a task, in topological order."""
        seen: set[str] = set()
        stack = list(self.get(task_id).dependencies)
        while stack:
            current = stack.pop()
            if current in seen or current not in self._tasks:
                continue
            seen.add(current)
            stack.extend(self._tasks[current].dependencies)
        order = self.topological_order()
        return [tid for tid in order if tid in seen]
