"""Versioned workflow definitions.

Definitions are optional. The normal path is a dynamically generated graph; a
definition exists for the case where a user wants to pin a repeatable shape.
Only generic patterns ship with the platform, never domain workflows (spec
section 92).

An execution records the definition id and version it started with, and the
registry never mutates a definition in place, so a running execution cannot
silently adopt a newer shape (spec section 68).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ...errors import ConfigurationError, NotFound
from ..domain.enums import ModelCapability, OrchestrationPattern, RiskLevel
from ..domain.models import Task, ValidationSpec
from .patterns import (
    Step,
    evaluator_optimizer,
    hierarchical,
    orchestrator_worker,
    parallel,
    router,
    sequential,
    single,
)


def _load_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional dependency
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ConfigurationError(
                f"reading {path} requires PyYAML; install universal-orchestrator[yaml]"
                " or use JSON"
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def _step_from_dict(data: dict[str, Any]) -> Step:
    return Step(
        name=data.get("name", ""),
        objective=data.get("objective", ""),
        capabilities=list(data.get("capabilities", [])),
        tools=list(data.get("tools", [])),
        model_requirements=[
            ModelCapability(value) for value in data.get("model_requirements", [])
        ],
        inputs=dict(data.get("inputs", {})),
        expected_outputs=list(data.get("expected_outputs", [])),
        validations=[ValidationSpec.from_dict(v) for v in data.get("validations", [])],
        completion_criteria=list(data.get("completion_criteria", [])),
        resources=list(data.get("resources", [])),
        risk=RiskLevel(data.get("risk", "low")),
        requires_approval=bool(data.get("requires_approval", False)),
        max_attempts=int(data.get("max_attempts", 3)),
        metadata=dict(data.get("metadata", {})),
    )


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    version: str = "1.0.0"
    description: str = ""
    pattern: OrchestrationPattern = OrchestrationPattern.SEQUENTIAL
    steps: tuple[Step, ...] = ()
    # Pattern-specific extras: routes, merge, evaluate, children, max_iterations.
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowDefinition":
        if "id" not in data:
            raise ConfigurationError("workflow definition requires an id")
        return cls(
            id=str(data["id"]),
            version=str(data.get("version", "1.0.0")),
            description=str(data.get("description", "")),
            pattern=OrchestrationPattern(data.get("pattern", "sequential")),
            steps=tuple(_step_from_dict(s) for s in data.get("steps", [])),
            options=dict(data.get("options", {})),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "WorkflowDefinition":
        return cls.from_dict(_load_document(Path(path)))

    def build(self, execution_id: str) -> list[Task]:
        """Materialise the definition into concrete tasks."""
        pattern = self.pattern
        steps = list(self.steps)
        options = self.options

        if pattern is OrchestrationPattern.SINGLE_AGENT:
            if len(steps) != 1:
                raise ConfigurationError(
                    f"workflow {self.id}: single_agent requires exactly one step"
                )
            return single(execution_id, steps[0])

        if pattern is OrchestrationPattern.SEQUENTIAL:
            return sequential(execution_id, steps)

        if pattern is OrchestrationPattern.PARALLEL:
            merge = options.get("merge")
            return parallel(
                execution_id,
                steps,
                merge=_step_from_dict(merge) if merge else None,
            )

        if pattern is OrchestrationPattern.ROUTER:
            if not steps:
                raise ConfigurationError(
                    f"workflow {self.id}: router requires a classification step"
                )
            routes = {
                name: [_step_from_dict(s) for s in route_steps]
                for name, route_steps in (options.get("routes") or {}).items()
            }
            if not routes:
                raise ConfigurationError(f"workflow {self.id}: router requires routes")
            return router(execution_id, steps[0], routes)

        if pattern is OrchestrationPattern.ORCHESTRATOR_WORKER:
            if len(steps) < 2:
                raise ConfigurationError(
                    f"workflow {self.id}: orchestrator_worker requires a coordinator"
                    " step and at least one worker step"
                )
            synthesis = options.get("synthesis")
            return orchestrator_worker(
                execution_id,
                steps[0],
                steps[1:],
                _step_from_dict(synthesis)
                if synthesis
                else Step(name="synthesise", objective="Combine the worker results."),
            )

        if pattern is OrchestrationPattern.EVALUATOR_OPTIMIZER:
            if len(steps) != 2:
                raise ConfigurationError(
                    f"workflow {self.id}: evaluator_optimizer requires exactly a"
                    " generate step and an evaluate step"
                )
            return evaluator_optimizer(
                execution_id,
                steps[0],
                steps[1],
                max_iterations=int(options.get("max_iterations", 3)),
            )

        if pattern is OrchestrationPattern.HIERARCHICAL:
            if not steps:
                raise ConfigurationError(
                    f"workflow {self.id}: hierarchical requires a parent step"
                )
            return hierarchical(execution_id, steps[0], steps[1:])

        raise ConfigurationError(
            f"workflow {self.id}: pattern {pattern.value} cannot be built from a"
            " static definition; use dynamic planning"
        )


class WorkflowRegistry:
    """Immutable, version-keyed store of workflow definitions."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], WorkflowDefinition] = {}
        self._latest: dict[str, str] = {}

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        key = (definition.id, definition.version)
        existing = self._by_key.get(key)
        if existing is not None and existing != definition:
            raise ConfigurationError(
                f"workflow {definition.id} version {definition.version} is already"
                " registered with different content; publish a new version instead"
            )
        self._by_key[key] = definition
        current = self._latest.get(definition.id)
        if current is None or _version_key(definition.version) >= _version_key(current):
            self._latest[definition.id] = definition.version
        return definition

    def get(self, workflow_id: str, version: str | None = None) -> WorkflowDefinition:
        resolved = version or self._latest.get(workflow_id)
        if resolved is None:
            raise NotFound(f"workflow {workflow_id} is not registered", id=workflow_id)
        try:
            return self._by_key[(workflow_id, resolved)]
        except KeyError as exc:
            raise NotFound(
                f"workflow {workflow_id} version {resolved} is not registered",
                id=workflow_id,
                version=resolved,
            ) from exc

    def list(self) -> list[WorkflowDefinition]:
        return sorted(self._by_key.values(), key=lambda d: (d.id, d.version))

    def versions(self, workflow_id: str) -> list[str]:
        return sorted(
            (v for (wid, v) in self._by_key if wid == workflow_id), key=_version_key
        )

    def load_directory(self, directory: str | Path) -> list[WorkflowDefinition]:
        path = Path(directory)
        if not path.is_dir():
            return []
        loaded = []
        for file in sorted(path.iterdir()):
            if file.suffix in (".yaml", ".yml", ".json"):
                loaded.append(self.register(WorkflowDefinition.from_file(file)))
        return loaded

    def load_all(self, directories: Iterable[str | Path]) -> list[WorkflowDefinition]:
        loaded: list[WorkflowDefinition] = []
        for directory in directories:
            loaded.extend(self.load_directory(directory))
        return loaded


def _version_key(version: str) -> tuple[int, ...]:
    parts = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)
