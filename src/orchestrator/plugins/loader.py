"""Plugin loading.

A plugin extends the platform without editing it (spec section 46). It is any
module exposing ``register(registry: PluginRegistry) -> None``, discovered
either through the ``orchestrator.plugins`` entry-point group or named
explicitly in configuration.

Plugins receive a narrow registration surface rather than the whole platform,
so a plugin cannot quietly reach into execution state.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..agents.capabilities import CapabilityRegistry
from ..agents.registry import AgentRegistry
from ..agents.runtime import AgentRuntime, RuntimeRegistry
from ..agents.skills import Skill, SkillRegistry
from ..core.domain.models import AgentSpec, Capability, ToolSpec
from ..core.workflow.definition import WorkflowDefinition, WorkflowRegistry
from ..errors import ConfigurationError
from ..observability.logging import get_logger
from ..tools.registry import ToolHandler, ToolRegistry
from ..validation.validators import Validator, ValidatorRegistry

_logger = get_logger("plugins")

ENTRY_POINT_GROUP = "orchestrator.plugins"


@dataclass
class PluginRegistry:
    """What a plugin is allowed to add."""

    tools: ToolRegistry
    validators: ValidatorRegistry
    agents: AgentRegistry
    capabilities: CapabilityRegistry
    runtimes: RuntimeRegistry
    workflows: WorkflowRegistry
    # Providers, storage, and observers are registered through callbacks so the
    # router, the store, and the audit log all stay private to the platform.
    add_model_provider: Callable[[Any], Any]
    skills: SkillRegistry | None = None
    add_storage_backend: Callable[[Any], Any] | None = None
    add_observer: Callable[[Any], Any] | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def add_tool(self, spec: ToolSpec, handler: ToolHandler) -> ToolSpec:
        return self.tools.register(spec, handler)

    def add_validator(self, validator: Validator) -> Validator:
        return self.validators.register(validator)

    def add_agent(self, agent: AgentSpec) -> AgentSpec:
        return self.agents.register(agent)

    def add_capability(self, capability: Capability) -> Capability:
        return self.capabilities.register(capability)

    def add_runtime(self, runtime: AgentRuntime) -> AgentRuntime:
        return self.runtimes.register(runtime)

    def add_workflow(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        return self.workflows.register(definition)

    def add_skill(self, skill: Skill) -> Skill:
        """Register reusable knowledge an agent can be given."""
        if self.skills is None:
            raise ConfigurationError("this platform was assembled without a skill registry")
        return self.skills.register(skill)

    def add_storage(self, store: Any) -> Any:
        """Offer an alternative ``StateStore``.

        Only honoured before the platform has been assembled; a plugin cannot
        swap the store out from under a running execution.
        """
        if self.add_storage_backend is None:
            raise ConfigurationError(
                "this platform was assembled without a storage seam for plugins"
            )
        return self.add_storage_backend(store)

    def observe(self, callback: Any) -> Any:
        """Subscribe to audit events, for metrics or tracing exporters."""
        if self.add_observer is None:
            raise ConfigurationError(
                "this platform was assembled without an observability seam"
            )
        return self.add_observer(callback)


@dataclass
class LoadedPlugin:
    name: str
    source: str
    ok: bool
    error: str | None = None


def load_module(name: str, registry: PluginRegistry) -> LoadedPlugin:
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        return LoadedPlugin(name, "module", False, f"import failed: {exc}")
    register = getattr(module, "register", None)
    if not callable(register):
        return LoadedPlugin(
            name, "module", False, "module does not expose a register(registry) function"
        )
    try:
        register(registry)
    except Exception as exc:  # noqa: BLE001 - a bad plugin must not stop startup
        return LoadedPlugin(name, "module", False, f"{type(exc).__name__}: {exc}")
    return LoadedPlugin(name, "module", True)


def load_entry_points(
    registry: PluginRegistry, *, group: str = ENTRY_POINT_GROUP
) -> list[LoadedPlugin]:
    # requires-python is >=3.11, where importlib.metadata.entry_points
    # always exists and always accepts `group`. The two fallbacks that
    # used to be here could not run on any supported interpreter.
    from importlib.metadata import entry_points

    loaded: list[LoadedPlugin] = []
    points = entry_points(group=group)
    for point in points:
        try:
            target = point.load()
        except Exception as exc:  # noqa: BLE001
            loaded.append(
                LoadedPlugin(point.name, "entry_point", False, f"load failed: {exc}")
            )
            continue
        register = target if callable(target) else getattr(target, "register", None)
        if not callable(register):
            loaded.append(
                LoadedPlugin(point.name, "entry_point", False, "target is not callable")
            )
            continue
        try:
            register(registry)
        except Exception as exc:  # noqa: BLE001
            loaded.append(
                LoadedPlugin(
                    point.name, "entry_point", False, f"{type(exc).__name__}: {exc}"
                )
            )
            continue
        loaded.append(LoadedPlugin(point.name, "entry_point", True))
    return loaded


def load_all(
    registry: PluginRegistry,
    *,
    modules: Sequence[str] = (),
    use_entry_points: bool = True,
    group: str = ENTRY_POINT_GROUP,
    strict: bool = False,
) -> list[LoadedPlugin]:
    """Load every configured plugin, reporting failures rather than hiding them."""
    loaded: list[LoadedPlugin] = []
    if use_entry_points:
        loaded.extend(load_entry_points(registry, group=group))
    for module in modules:
        loaded.append(load_module(module, registry))

    failures = [p for p in loaded if not p.ok]
    for failure in failures:
        _logger.warning(
            "plugin failed to load",
            extra={"context": {"plugin": failure.name, "error": failure.error}},
        )
    if strict and failures:
        raise ConfigurationError(
            "plugins failed to load: "
            + "; ".join(f"{p.name} ({p.error})" for p in failures)
        )
    return loaded


def summarise(plugins: Iterable[LoadedPlugin]) -> list[dict[str, Any]]:
    return [
        {"name": p.name, "source": p.source, "loaded": p.ok, "error": p.error}
        for p in plugins
    ]
