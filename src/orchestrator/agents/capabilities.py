"""Capability registry.

Capabilities are the routing currency of the platform: a task declares what it
needs, an agent advertises what it provides, and the orchestrator matches them.
Nothing is selected by hard-coded role name (spec sections 10, 91).

Capability ids are opaque strings. The core ships none; they arrive from
configuration, plugins, or the planner. That is what keeps the engine
domain-neutral.
"""

from __future__ import annotations

# `builtins` is imported because this module's registry exposes a public
# `list()` method, which shadows the builtin inside its own class body.
# `-> builtins.list[X]` is the annotation that keeps the method name.
import builtins
from collections.abc import Iterable

from ..core.domain.models import Capability, CapabilityRequirements
from ..errors import ConfigurationError, NotFound


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability) -> Capability:
        if not capability.id:
            raise ConfigurationError("capability requires an id")
        existing = self._capabilities.get(capability.id)
        if existing is not None and existing.version != capability.version:
            # Re-registering under a new version is an update, not a conflict.
            self._capabilities[capability.id] = capability
            return capability
        self._capabilities[capability.id] = capability
        return capability

    def register_many(self, capabilities: Iterable[Capability]) -> list[Capability]:
        return [self.register(c) for c in capabilities]

    def declare(
        self,
        capability_id: str,
        description: str = "",
        *,
        tools: Iterable[str] = (),
        skills: Iterable[str] = (),
        permissions: Iterable[str] = (),
    ) -> Capability:
        """Register a capability discovered at runtime (e.g. by the planner)."""
        return self.register(
            Capability(
                id=capability_id,
                description=description or capability_id.replace("_", " "),
                requirements=CapabilityRequirements(
                    tools=list(tools),
                    skills=list(skills),
                    permissions=list(permissions),
                ),
            )
        )

    def get(self, capability_id: str) -> Capability:
        try:
            return self._capabilities[capability_id]
        except KeyError as exc:
            raise NotFound(
                f"capability {capability_id} is not registered", id=capability_id
            ) from exc

    def has(self, capability_id: str) -> bool:
        return capability_id in self._capabilities

    def list(self) -> list[Capability]:
        return sorted(self._capabilities.values(), key=lambda c: c.id)

    def ids(self) -> set[str]:
        return set(self._capabilities)

    def missing(self, required: Iterable[str]) -> builtins.list[str]:
        return sorted(c for c in required if c not in self._capabilities)

    def requirements_for(self, required: Iterable[str]) -> CapabilityRequirements:
        """Union of the concrete requirements implied by a set of capabilities."""
        tools: list[str] = []
        skills: list[str] = []
        permissions: list[str] = []
        model_capabilities: list = []
        for capability_id in required:
            if not self.has(capability_id):
                continue
            requirements = self.get(capability_id).requirements
            tools.extend(t for t in requirements.tools if t not in tools)
            skills.extend(s for s in requirements.skills if s not in skills)
            permissions.extend(p for p in requirements.permissions if p not in permissions)
            model_capabilities.extend(
                m for m in requirements.model_capabilities if m not in model_capabilities
            )
        return CapabilityRequirements(
            tools=tools,
            skills=skills,
            permissions=permissions,
            model_capabilities=model_capabilities,
        )
