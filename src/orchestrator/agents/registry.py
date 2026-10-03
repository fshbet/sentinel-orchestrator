"""Agent registry.

Agents are data, not classes. Anything that can advertise capabilities and be
executed by a runtime can register: a definition loaded from a file, a plugin,
a dynamically created specialist, or an external system behind an adapter.

The registry keeps every version of a definition it has seen so an execution
can be replayed against the exact definition it ran with (spec section 69).
"""

from __future__ import annotations

# `builtins` is imported because this module's registry exposes a public
# `list()` method, which shadows the builtin inside its own class body.
# `-> builtins.list[X]` is the annotation that keeps the method name.
import builtins
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..core.domain.enums import ModelCapability
from ..core.domain.models import AgentConstraints, AgentSpec
from ..errors import ConfigurationError, NotFound


def _load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ConfigurationError(
                f"reading {path} requires PyYAML; install universal-orchestrator[yaml]"
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def agent_from_dict(data: dict[str, Any]) -> AgentSpec:
    if not data.get("id"):
        raise ConfigurationError("agent definition requires an id")
    spec = AgentSpec.from_dict(data)
    if isinstance(data.get("constraints"), dict):
        spec.constraints = AgentConstraints.from_dict(data["constraints"])
    spec.model_requirements = [
        ModelCapability(v) for v in data.get("model_requirements", [])
    ]
    return spec


class AgentRegistry:
    def __init__(self) -> None:
        self._current: dict[str, AgentSpec] = {}
        self._history: dict[tuple[str, str], AgentSpec] = {}

    # -- registration ------------------------------------------------------

    def register(self, agent: AgentSpec) -> AgentSpec:
        if not agent.id:
            raise ConfigurationError("agent requires an id")
        self._current[agent.id] = agent
        self._history[(agent.id, agent.version)] = agent
        return agent

    def register_many(self, agents: Iterable[AgentSpec]) -> list[AgentSpec]:
        return [self.register(a) for a in agents]

    def unregister(self, agent_id: str) -> None:
        self._current.pop(agent_id, None)

    def load_directory(self, directory: str | Path) -> list[AgentSpec]:
        path = Path(directory)
        if not path.is_dir():
            return []
        loaded: list[AgentSpec] = []
        for file in sorted(path.iterdir()):
            if file.suffix not in (".yaml", ".yml", ".json"):
                continue
            document = _load_document(file)
            entries = document if isinstance(document, list) else [document]
            for entry in entries:
                loaded.append(self.register(agent_from_dict(entry)))
        return loaded

    # -- lookup ------------------------------------------------------------

    def get(self, agent_id: str, version: str | None = None) -> AgentSpec:
        if version is not None:
            try:
                return self._history[(agent_id, version)]
            except KeyError as exc:
                raise NotFound(
                    f"agent {agent_id} version {version} is not registered",
                    id=agent_id,
                    version=version,
                ) from exc
        try:
            return self._current[agent_id]
        except KeyError as exc:
            raise NotFound(f"agent {agent_id} is not registered", id=agent_id) from exc

    def has(self, agent_id: str) -> bool:
        return agent_id in self._current

    def list(self, *, include_ephemeral: bool = True) -> list[AgentSpec]:
        agents = [a for a in self._current.values() if include_ephemeral or not a.ephemeral]
        return sorted(agents, key=lambda a: a.id)

    def providing(self, capability_id: str) -> builtins.list[AgentSpec]:
        """Every registered agent advertising a capability."""
        return [a for a in self.list() if capability_id in a.capabilities]

    def capabilities(self) -> set[str]:
        return {c for agent in self.list() for c in agent.capabilities}

    def versions(self, agent_id: str) -> dict[str, AgentSpec]:
        return {v: spec for (aid, v), spec in self._history.items() if aid == agent_id}

    def snapshot_versions(self) -> dict[str, str]:
        """Agent id -> version, pinned onto an execution at planning time."""
        return {agent.id: agent.version for agent in self.list()}
