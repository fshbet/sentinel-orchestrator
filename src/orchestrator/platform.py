"""Platform assembly.

Builds every subsystem from configuration and exposes the one facade the CLI,
the REST API, the MCP server, and embedding applications all use.

Assembly is the only place that knows which concrete provider, store, or
adapter is in play. Everything below it depends on interfaces (spec section 47).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .agents.capabilities import CapabilityRegistry
from .agents.registry import AgentRegistry, agent_from_dict
from .agents.runtime import GenericAgentRuntime, RuntimeRegistry
from .agents.selection import AgentSelector, SelectionPolicy
from .agents.skills import Skill, SkillRegistry, skill_from_dict
from .config.loader import Config, load
from .context.manager import ContextManager
from .context.memory import MemoryStore
from .core.domain.enums import (
    ExecutionStatus,
    ModelCapability,
    OrchestrationPattern,
    PlanStrategy,
    RiskLevel,
)
from .core.domain.models import Capability, ModelSpec, ResourceLimits
from .core.execution.engine import EngineComponents, EngineConfig, ExecutionEngine
from .core.execution.nested import SubOrchestrationRuntime
from .core.policy.engine import (
    PolicyConfig,
    PolicyEngine,
    PolicyRule,
)
from .core.policy.risk import RiskEngine
from .core.scheduler.scheduler import Scheduler
from .core.state.manager import StateManager
from .core.state.memory_store import InMemoryStateStore
from .core.state.sqlite_store import SQLiteStateStore
from .core.state.store import ExecutionSummary, StateStore
from .core.workflow.definition import WorkflowRegistry
from .errors import ConfigurationError, NotFound
from .llm.base import LLMProvider
from .llm.routing import ModelRouter
from .observability.audit import AuditLog
from .observability.logging import configure_logging
from .planning.decomposition import Planner
from .planning.goal import GoalAnalyzer
from .plugins.loader import LoadedPlugin, PluginRegistry, load_all, summarise
from .recovery.engine import RecoveryEngine
from .recovery.strategies import RecoveryPolicy
from .tools import native
from .tools.registry import ToolRegistry
from .validation.gates import GateRunner
from .validation.validators import ValidatorRegistry


@dataclass
class Orchestrator:
    """The platform facade."""

    config: Config
    store: StateStore
    state: StateManager
    engine: ExecutionEngine
    router: ModelRouter
    tools: ToolRegistry
    agents: AgentRegistry
    capabilities: CapabilityRegistry
    validators: ValidatorRegistry
    skills: SkillRegistry
    workflows: WorkflowRegistry
    memory: MemoryStore
    policy: PolicyEngine
    mcp: Any = None  # MCPRegistry, imported lazily so MCP stays optional
    plugins: list[LoadedPlugin] = field(default_factory=list)
    # One collector for this orchestrator's lifetime. Never None, so no
    # call site has to guard before recording.
    metrics: Any = None

    # -- construction ------------------------------------------------------

    @classmethod
    async def create(
        cls,
        *,
        config: Config | None = None,
        config_paths: Sequence[str | Path] | None = None,
        overrides: dict[str, Any] | None = None,
        workspace: str | Path | None = None,
        providers: Sequence[LLMProvider] = (),
        connect_mcp: bool = True,
        metrics: Any = None,
        start: str | Path = ".",
    ) -> "Orchestrator":
        cfg = config or load(
            paths=config_paths, overrides=overrides, start=start
        )
        configure_logging(
            str(cfg.get("logging.level", "warning")),
            json_output=bool(cfg.get("logging.json", True)),
        )

        workspace_path = str(workspace or cfg.get("workspace", "."))

        # One collector for this orchestrator's lifetime, built before the
        # components that record into it.
        from .observability.metrics import Metrics

        collector = metrics if metrics is not None else Metrics()

        store = await _build_store_async(cfg)
        audit = AuditLog(store)
        state = StateManager(store, audit)

        policy = _build_policy(cfg)
        tools = ToolRegistry(
            policy=policy, audit=audit, operations=store, metrics=collector
        )
        validators = ValidatorRegistry()
        capabilities = CapabilityRegistry()
        agents = AgentRegistry()
        workflows = WorkflowRegistry()
        skills = SkillRegistry()
        runtimes = RuntimeRegistry()
        memory = MemoryStore(
            max_working=int(cfg.get("context.max_working_memory", 500))
        )

        # The data-flow policy lives on the router because that is the one
        # place every request to every provider passes through. Enforcing it
        # at the call sites would mean enforcing it again on the next one.
        from .llm.dataflow import DataFlowPolicy

        router = ModelRouter(
            audit=audit,
            data_policy=DataFlowPolicy.from_config(cfg),
            metrics=collector,
        )
        for provider in providers:
            router.register(provider)
        for provider in _build_providers(cfg):
            router.register(provider)

        context_manager = ContextManager(memory=memory, audit=audit)

        _register_native_tools(cfg, tools, memory, workspace_path)
        _register_capabilities(cfg, capabilities)
        _register_agents(cfg, agents)
        _register_workflows(cfg, workflows)
        _register_skills(cfg, skills)

        # A plugin may replace the store only before anything has been
        # persisted, which is why this is a slot rather than a live swap.
        store_slot: dict[str, StateStore] = {"store": store}

        def _replace_store(candidate: StateStore) -> StateStore:
            store_slot["store"] = candidate
            return candidate

        plugin_registry = PluginRegistry(
            tools=tools,
            validators=validators,
            agents=agents,
            capabilities=capabilities,
            runtimes=runtimes,
            workflows=workflows,
            skills=skills,
            add_model_provider=router.register,
            add_storage_backend=_replace_store,
            add_observer=audit.subscribe,
            config=cfg.section("plugins"),
        )
        plugins: list[LoadedPlugin] = []
        if cfg.get("plugins.enabled", True):
            plugins = load_all(
                plugin_registry,
                modules=list(cfg.get("plugins.modules", []) or []),
                group=str(cfg.get("plugins.entry_point_group", "orchestrator.plugins")),
            )

        if store_slot["store"] is not store:
            store = store_slot["store"]
            audit = AuditLog(store)
            state = StateManager(store, audit)
            tools.operations = store

        runtimes.register(
            GenericAgentRuntime(
                router=router,
                tools=tools,
                context_manager=context_manager,
                audit=audit,
                skills=skills,
            )
        )

        mcp_registry = None
        mcp_servers = cfg.section("mcp").get("servers") or {}
        if mcp_servers:
            from .mcp.registry import MCPRegistry, policies_from_config

            mcp_registry = MCPRegistry(tools, policy_engine=policy, audit=audit)
            mcp_registry.configure_many(
                mcp_servers,
                policies_from_config(cfg.section("mcp").get("policies") or []),
            )
            if connect_mcp:
                await mcp_registry.connect_all()

        selector = AgentSelector(
            agents,
            capabilities,
            policy=SelectionPolicy(
                allow_dynamic_agents=bool(cfg.get("recovery.allow_dynamic_agents", True))
            ),
        )
        # The devil's advocate needs a model to argue with, so it is
        # registered only when one is configured. It is never applied
        # automatically: a task opts in by declaring it.
        if router.all_models():
            from .validation.advocate import build as build_advocate

            validators.register(build_advocate(router))

        gates = GateRunner(validators, audit=audit)
        recovery = RecoveryEngine(
            state,
            policy=RecoveryPolicy(
                allow_replan=bool(cfg.get("recovery.allow_replan", True)),
                allow_dynamic_agents=bool(cfg.get("recovery.allow_dynamic_agents", True)),
                allow_human_escalation=bool(
                    cfg.get("recovery.allow_human_escalation", True)
                ),
                max_attempts_per_task=int(cfg.get("limits.max_task_attempts", 3)),
                max_replans=int(cfg.get("limits.max_replans", 3)),
            ),
        )
        planner = Planner(router=router, audit=audit)
        goal = GoalAnalyzer(router=router, known_validators=validators.names())

        components = EngineComponents(
            state=state,
            goal=goal,
            planner=planner,
            selector=selector,
            runtimes=runtimes,
            tools=tools,
            gates=gates,
            recovery=recovery,
            context=context_manager,
            validators=validators,
            capabilities=capabilities,
            agents=agents,
            policy=policy,
            skills=skills,
            scheduler=Scheduler(limits=_build_limits(cfg)),
            mcp_servers=sorted(mcp_servers),
        )
        engine = ExecutionEngine(
            components,
            metrics=collector,
            config=EngineConfig(
                workspace=workspace_path,
                default_limits=_build_limits(cfg),
                approval_threshold=RiskLevel(
                    str(cfg.get("policy.approval_threshold", "high"))
                ),
            ),
        )

        # Nesting needs the engine, so it is registered once the engine exists.
        runtimes.register(
            SubOrchestrationRuntime(
                engine,
                max_depth=int(cfg.get("limits.max_nesting_depth", 2)),
            )
        )

        return cls(
            config=cfg,
            store=store,
            state=state,
            engine=engine,
            router=router,
            tools=tools,
            agents=agents,
            capabilities=capabilities,
            validators=validators,
            skills=skills,
            workflows=workflows,
            memory=memory,
            policy=policy,
            mcp=mcp_registry,
            plugins=plugins,
            metrics=collector,
        )

    # -- execution ---------------------------------------------------------

    async def run(
        self,
        objective: str,
        *,
        limits: ResourceLimits | None = None,
        context: dict[str, Any] | None = None,
        pattern: OrchestrationPattern | None = None,
        plan_strategy: PlanStrategy | None = None,
    ):
        """Create and drive an execution to a terminal or waiting state."""
        execution = await self.engine.start(
            objective,
            limits=limits,
            context=context,
            pattern=pattern,
            plan_strategy=plan_strategy,
        )
        execution.workflow.config_fingerprint = self.config.fingerprint()
        await self.state.persist(execution)
        return await self.engine.run(execution.id)

    async def start(self, objective: str, **kwargs: Any):
        execution = await self.engine.start(objective, **kwargs)
        execution.workflow.config_fingerprint = self.config.fingerprint()
        return await self.state.persist(execution)

    async def resume(self, execution_id: str):
        return await self.engine.resume(execution_id)

    async def pause(self, execution_id: str, *, reason: str = ""):
        return await self.state.request_pause(execution_id, reason=reason)

    async def cancel(self, execution_id: str, *, reason: str = ""):
        return await self.state.request_cancel(execution_id, reason=reason)

    async def status(self, execution_id: str):
        return await self.state.load(execution_id)

    async def list(
        self, *, status: ExecutionStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[ExecutionSummary]:
        return await self.store.list(status=status, limit=limit, offset=offset)

    async def audit(self, execution_id: str, *, after: int = 0, limit: int = 1000):
        return await self.store.audit(execution_id, after_sequence=after, limit=limit)

    async def approve(
        self,
        execution_id: str,
        approval_id: str,
        *,
        approved: bool = True,
        response: Any = None,
        responder: str | None = None,
        resume: bool = True,
    ):
        execution = await self.state.resolve_approval(
            execution_id,
            approval_id,
            approved=approved,
            response=response,
            responder=responder,
        )
        if resume and execution.pending_approval() is None:
            return await self.engine.resume(execution_id)
        return execution

    async def artifacts(self, execution_id: str):
        execution = await self.state.load(execution_id)
        return execution.artifacts

    # -- introspection -----------------------------------------------------

    async def health(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "status": "ok",
            "config_sources": self.config.sources(),
            "config_fingerprint": self.config.fingerprint(),
            "storage": type(self.store).__name__,
            "tools": len(self.tools.list()),
            "agents": len(self.agents.list()),
            "capabilities": len(self.capabilities.list()),
            "skills": len(self.skills.list()),
            "validators": self.validators.names(),
            "plugins": summarise(self.plugins),
        }
        report["models"] = [health.to_dict() for health in await self.router.health()]
        report["model_routing"] = self.router.status()
        if self.mcp is not None:
            report["mcp"] = await self.mcp.health()
        else:
            report["mcp"] = []
        return report

    def describe(self) -> dict[str, Any]:
        """A static snapshot of what this installation can do."""
        return {
            "tools": [
                {
                    "id": spec.id,
                    "source": spec.source.value,
                    "risk": spec.risk.value,
                    "permissions": spec.permissions,
                    "description": spec.description,
                }
                for spec in self.tools.list()
            ],
            "agents": [
                {
                    "id": agent.id,
                    "capabilities": agent.capabilities,
                    "skills": agent.skills,
                    "runtime": agent.runtime,
                    "version": agent.version,
                    "ephemeral": agent.ephemeral,
                }
                for agent in self.agents.list()
            ],
            "capabilities": [
                {"id": c.id, "description": c.description} for c in self.capabilities.list()
            ],
            "models": [
                {
                    "id": spec.id,
                    "provider": provider.name,
                    "capabilities": [c.value for c in spec.capabilities],
                    "context_window": spec.context_window,
                }
                for provider, spec in self.router.all_models()
            ],
            "workflows": [
                {"id": w.id, "version": w.version, "pattern": w.pattern.value}
                for w in self.workflows.list()
            ],
            "skills": [
                {
                    "id": s.id,
                    "version": s.version,
                    "description": s.description,
                    "applies_to": list(s.applies_to),
                    "requires": list(s.requires),
                }
                for s in self.skills.list()
            ],
            "validators": self.validators.names(),
            "policy_rules": [
                {"kind": r.kind, "subject": r.subject, "effect": r.effect}
                for r in self.policy.rules()
            ],
        }

    async def close(self) -> None:
        if self.mcp is not None:
            await self.mcp.close()
        await asyncio.gather(
            *(provider.close() for provider in self.router.providers),
            return_exceptions=True,
        )
        await self.store.close()


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _build_store(config: Config) -> StateStore:
    """Construct the state backend named by ``storage.backend``.

    PostgreSQL needs an await to open its pool, so it cannot be built here.
    It is constructed in ``Orchestrator.create`` instead; this raises a
    message saying so rather than silently returning the wrong backend.
    """
    backend = str(config.get("storage.backend", "sqlite")).lower()
    if backend == "memory":
        return InMemoryStateStore()
    if backend == "sqlite":
        return SQLiteStateStore(str(config.get("storage.path", ".orchestrator/state.db")))
    if backend == "postgres":
        raise ConfigurationError(
            "the postgres backend is built asynchronously; use "
            "Orchestrator.create(), which awaits the connection pool"
        )
    raise ConfigurationError(
        f"unknown storage backend {backend!r}. Valid backends are: "
        f"sqlite (default, single-node), postgres (multi-instance), "
        f"memory (tests)."
    )


async def _build_store_async(config: Config) -> StateStore:
    """Build the store, awaiting a connection pool when one is needed."""
    backend = str(config.get("storage.backend", "sqlite")).lower()
    if backend != "postgres":
        return _build_store(config)

    from .core.state.postgres_store import PostgresStateStore, resolve_dsn

    section = config.section("storage").get("postgres", {}) or {}
    return await PostgresStateStore.connect(
        resolve_dsn(section),
        min_size=int(section.get("min_connections", 1)),
        max_size=int(section.get("max_connections", 10)),
        apply_migrations=bool(section.get("apply_migrations", True)),
        command_timeout=float(section.get("command_timeout", 30.0)),
    )


def _build_limits(config: Config) -> ResourceLimits:
    section = config.section("limits")
    return ResourceLimits(
        max_wall_seconds=float(section.get("max_wall_seconds", 3600.0)),
        max_model_calls=int(section.get("max_model_calls", 300)),
        max_tool_calls=int(section.get("max_tool_calls", 1000)),
        max_tokens=int(section.get("max_tokens", 2000000)),
        max_cost=section.get("max_cost"),
        max_parallel_tasks=int(section.get("max_parallel_tasks", 4)),
        max_task_attempts=int(section.get("max_task_attempts", 3)),
        max_replans=int(section.get("max_replans", 3)),
        max_optimizer_iterations=int(section.get("max_optimizer_iterations", 3)),
        max_external_requests=int(section.get("max_external_requests", 500)),
    )


def _build_policy(config: Config) -> PolicyEngine:
    section = config.section("policy")
    rules = []
    for entry in section.get("rules", []) or []:
        rules.append(
            PolicyRule(
                subject=str(entry.get("subject", "*")),
                kind=str(entry.get("kind", "tool")),
                effect=str(entry.get("effect", "allow")).lower(),
                max_risk=RiskLevel(entry["max_risk"]) if entry.get("max_risk") else None,
                required_permissions=tuple(entry.get("required_permissions", ())),
                reason=str(entry.get("reason", "")),
                priority=int(entry.get("priority", 0)),
            )
        )
    deny_threshold = section.get("deny_threshold")
    return PolicyEngine(
        rules=rules,
        config=PolicyConfig(
            default_effect=str(section.get("default_effect", "allow")).lower(),
            approval_threshold=RiskLevel(str(section.get("approval_threshold", "high"))),
            deny_threshold=RiskLevel(deny_threshold) if deny_threshold else None,
            require_explicit_tool_grant=bool(
                section.get("require_explicit_tool_grant", False)
            ),
            version=str(section.get("version", "1.0.0")),
        ),
        risk_engine=RiskEngine(),
    )


def _build_providers(config: Config) -> list[LLMProvider]:
    providers: list[LLMProvider] = []
    for entry in config.get("models.providers", []) or []:
        kind = str(entry.get("type", "")).lower()
        models = [_model_spec(m) for m in entry.get("models", [])]

        if kind in ("openai", "openai_compatible", "openai-compatible"):
            from .llm.providers.openai_compat import OpenAICompatibleProvider

            provider: LLMProvider = OpenAICompatibleProvider(
                base_url=str(entry.get("base_url", "https://api.openai.com/v1")),
                api_key=entry.get("api_key"),
                api_key_env=str(entry.get("api_key_env", "OPENAI_API_KEY")),
                models=models,
                name=entry.get("name"),
                default_headers=entry.get("headers"),
                timeout=float(entry.get("timeout", 120.0)),
            )
        elif kind == "anthropic":
            from .llm.providers.anthropic import AnthropicProvider

            provider = AnthropicProvider(
                base_url=str(entry.get("base_url", "https://api.anthropic.com/v1")),
                api_key=entry.get("api_key"),
                api_key_env=str(entry.get("api_key_env", "ANTHROPIC_API_KEY")),
                models=models,
                timeout=float(entry.get("timeout", 120.0)),
            )
        elif kind == "ollama":
            from .llm.providers.ollama import OllamaProvider

            provider = OllamaProvider(
                base_url=str(entry.get("base_url", "http://localhost:11434")),
                models=models,
                timeout=float(entry.get("timeout", 300.0)),
            )
        elif kind == "scripted":
            from .llm.providers.scripted import ScriptedProvider

            provider = ScriptedProvider(
                entry.get("responses", []),
                name=str(entry.get("name", "scripted")),
            )
        else:
            raise ConfigurationError(f"unknown model provider type {kind!r}")

        for spec in models:
            spec.provider = provider.name
        providers.append(provider)
    return providers


def _model_spec(data: dict[str, Any]) -> ModelSpec:
    return ModelSpec(
        id=str(data.get("id") or data.get("model", "")),
        provider=str(data.get("provider", "")),
        model=str(data.get("model") or data.get("id", "")),
        capabilities=[ModelCapability(c) for c in data.get("capabilities", [])]
        or [ModelCapability.TEXT_GENERATION],
        context_window=int(data.get("context_window", 8192)),
        max_output_tokens=int(data.get("max_output_tokens", 2048)),
        cost_per_1k_input=data.get("cost_per_1k_input"),
        cost_per_1k_output=data.get("cost_per_1k_output"),
        priority=int(data.get("priority", 100)),
        metadata=dict(data.get("metadata", {})),
    )


def _register_native_tools(
    config: Config, tools: ToolRegistry, memory: MemoryStore, workspace: str
) -> None:
    section = config.section("tools")

    if section.get("bookkeeping", {}).get("enabled", True):
        def on_note(key: str, value: str, context: Any) -> None:
            memory.remember(
                key,
                value,
                execution_id=context.execution_id,
                task_id=context.task_id,
            )

        tools.register_many(native.bookkeeping_tools(on_note=on_note))

    filesystem = section.get("filesystem", {})
    if filesystem.get("enabled"):
        tools.register_many(
            native.filesystem_tools(
                filesystem.get("root", workspace),
                allow_write=bool(filesystem.get("allow_write", True)),
            )
        )

    process = section.get("process", {})
    if process.get("enabled"):
        tools.register_many(native.process_tools(policy=_exec_policy(process, workspace)))

    http = section.get("http", {})
    if http.get("enabled"):
        tools.register_many(native.http_tools(policy=_egress_policy(http)))




def _exec_policy(process: dict[str, Any], workspace: str):
    """Build the process execution policy from the ``tools.process`` config.

    Validated here so a configuration that permits a shell, or that would pass
    a loader variable to a child, fails at startup rather than on the first
    request that happens to exercise it.
    """
    from .errors import ConfigurationError
    from .tools.execpolicy import ExecPolicy

    policy = ExecPolicy(
        allowed_commands=tuple(str(c) for c in process.get("allowed_commands", [])),
        root=str(process.get("root", workspace)),
        allow_shell_interpreters=bool(process.get("allow_shell_interpreters", False)),
        environment_allowlist=tuple(
            str(v) for v in process.get("environment_allowlist", [])
        ),
        denied_argument_patterns=tuple(
            str(p) for p in process.get("denied_argument_patterns", [])
        ),
        max_arguments=int(process.get("max_arguments", 64)),
        timeout=float(process.get("timeout", 120.0)),
        max_output_bytes=int(process.get("max_output_bytes", 100_000)),
    )
    try:
        policy.validate()
    except ValueError as exc:
        raise ConfigurationError(f"tools.process is not valid: {exc}") from exc
    return policy


def _egress_policy(http: dict[str, Any]):
    """Build the outbound network policy from the ``tools.http`` config.

    Every permissive option is opt-in and named, so reading a config file tells
    you the egress posture without also having to read the code.
    """
    from .errors import ConfigurationError
    from .tools.egress import READ_METHODS, EgressPolicy

    hosts = tuple(str(h) for h in (http.get("allowed_hosts") or []) if str(h).strip())
    if not hosts:
        raise ConfigurationError(
            "tools.http.enabled is true but tools.http.allowed_hosts is empty. "
            "HTTP tools let a model choose a destination, so the hosts it may "
            "reach must be listed explicitly. Add the hosts, or set "
            "tools.http.enabled to false.",
            remedy="set tools.http.allowed_hosts",
        )

    methods = tuple(
        str(m).upper() for m in (http.get("allowed_methods") or READ_METHODS)
    )

    try:
        return EgressPolicy(
            allowed_hosts=hosts,
            allowed_methods=methods,
            allow_http=bool(http.get("allow_http", False)),
            allow_private_networks=bool(http.get("allow_private_networks", False)),
            allow_loopback=bool(http.get("allow_loopback", False)),
            allow_link_local=bool(http.get("allow_link_local", False)),
            max_redirects=int(http.get("max_redirects", 3)),
            max_request_bytes=int(http.get("max_request_bytes", 1024 * 1024)),
            max_response_bytes=int(http.get("max_response_bytes", 5 * 1024 * 1024)),
            connect_timeout=float(http.get("connect_timeout", 10.0)),
            read_timeout=float(http.get("timeout", http.get("read_timeout", 30.0))),
            write_timeout=float(http.get("write_timeout", 30.0)),
            total_timeout=float(http.get("total_timeout", 60.0)),
        )
    except ValueError as exc:
        raise ConfigurationError(f"tools.http is not valid: {exc}") from exc


def _register_capabilities(config: Config, registry: CapabilityRegistry) -> None:
    for entry in config.get("capabilities", []) or []:
        if isinstance(entry, str):
            registry.declare(entry)
        elif isinstance(entry, dict):
            registry.register(Capability.from_dict(entry))


def _register_agents(config: Config, registry: AgentRegistry) -> None:
    section = config.section("agents")
    for directory in section.get("directories", []) or []:
        registry.load_directory(directory)
    for entry in section.get("definitions", []) or []:
        registry.register(agent_from_dict(entry))


def _register_skills(config: Config, registry: SkillRegistry) -> None:
    section = config.section("skills")
    registry.load_all(section.get("directories", []) or [])
    for entry in section.get("definitions", []) or []:
        registry.register(skill_from_dict(entry))


def _register_workflows(config: Config, registry: WorkflowRegistry) -> None:
    for directory in config.get("workflows.directories", []) or []:
        registry.load_directory(directory)
    packaged = Path(__file__).resolve().parent.parent.parent / "workflows"
    if packaged.is_dir():
        registry.load_directory(packaged)


async def get_execution(orchestrator: Orchestrator, execution_id: str):
    try:
        return await orchestrator.status(execution_id)
    except NotFound:
        return None
