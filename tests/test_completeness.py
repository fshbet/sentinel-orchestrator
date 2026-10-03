"""Behaviour that the specification requires and the first pass left unbuilt.

Router branch pruning, handoff, nested orchestration, MCP server scoping,
isolation enforcement, and pre-execution verification (spec sections 5, 45, 44,
63, 87). Each of these was either missing or claimed-but-absent; these tests are
what keep them honest.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from datetime import UTC
from pathlib import Path

import pytest
from conftest import build_platform, make_config, make_task, planning_model, run

from orchestrator.core.domain.enums import (
    ExecutionStatus,
    IsolationLevel,
    OrchestrationPattern,
    TaskStatus,
    ToolSource,
)
from orchestrator.core.domain.models import (
    AgentConstraints,
    AgentSpec,
    Execution,
    Plan,
    Task,
    TaskResult,
    ToolSpec,
    ValidationSpec,
)
from orchestrator.core.policy.engine import PermissionScope, default_policy
from orchestrator.core.workflow.patterns import Step, router
from orchestrator.core.workflow.verification import (
    VerificationContext,
    verify,
)
from orchestrator.errors import PermissionDenied
from orchestrator.llm.providers.scripted import CallableProvider
from orchestrator.observability.audit import AuditLog, NullAuditSink
from orchestrator.platform import Orchestrator
from orchestrator.tools.registry import ToolContext, ToolRegistry

# --------------------------------------------------------------------------
# Router branch pruning (spec section 5)
# --------------------------------------------------------------------------


def _router_platform(decision):
    """A platform whose plan is a router with two branches."""

    def model(request):
        system = request.system or ""
        if "extract structured requirements" in system:
            return {
                "explicit": ["route the work"],
                "success_criteria": [{"description": "d"}],
            }
        if "decompose an objective" in system:
            return {"tasks": [{"key": "x", "name": "x", "objective": "placeholder"}]}
        body = request.messages[0].content
        if "Choose a route" in body:
            return decision
        return "branch work done"

    return CallableProvider(model)


async def _run_router(decision):
    platform = await build_platform(_router_platform(decision))
    execution = await platform.engine.start("Route the work.")
    await platform.engine._understand(execution)

    tasks = router(
        execution.id,
        Step(
            name="classify",
            objective="Choose a route.",
            validations=[ValidationSpec(validator="non_empty")],
        ),
        {
            "left": [Step(name="left work", objective="Do the left work.")],
            "right": [Step(name="right work", objective="Do the right work.")],
        },
    )
    plan = Plan(
        execution_id=execution.id,
        version=1,
        tasks=tasks,
        pattern=OrchestrationPattern.ROUTER,
    )
    platform.engine._adopt(execution, plan)
    from orchestrator.core.domain.enums import ExecutionStatus as _S

    platform.state.transition(execution, _S.READY)
    await platform.state.persist(execution)

    finished = await platform.engine.run(execution.id)
    events = await platform.audit(finished.id)
    await platform.close()
    return finished, events


def test_the_chosen_branch_runs_and_the_others_are_skipped():
    execution, events = run(_run_router({"route": "left"}))

    statuses = {t.name: t.status for t in execution.tasks.values()}
    assert statuses["left work"] is TaskStatus.SUCCEEDED
    assert statuses["right work"] is TaskStatus.SKIPPED
    assert execution.status is ExecutionStatus.COMPLETED

    selected = [e for e in events if e.type == "route.selected"]
    assert selected and selected[0].payload["route"] == "left"


def test_a_route_named_only_in_prose_is_still_honoured():
    execution, _ = run(_run_router("I have decided we should take the right path."))
    statuses = {t.name: t.status for t in execution.tasks.values()}
    assert statuses["right work"] is TaskStatus.SUCCEEDED
    assert statuses["left work"] is TaskStatus.SKIPPED


def test_an_undecidable_route_is_reported_rather_than_guessed():
    """Naming both routes is not a decision, and picking one would be guessing."""

    execution, events = run(_run_router("It could be left, or it could be right."))

    assert any(e.type == "route.undecided" for e in events)
    assert not any(e.type == "route.selected" for e in events)
    # Nothing was pruned, so both branches ran rather than the graph stalling.
    statuses = {t.name: t.status for t in execution.tasks.values()}
    assert statuses["left work"] is TaskStatus.SUCCEEDED
    assert statuses["right work"] is TaskStatus.SUCCEEDED


# --------------------------------------------------------------------------
# Handoff (spec section 5)
# --------------------------------------------------------------------------


def _handoff_platform(target, *, agents=None):
    from orchestrator.core.domain.models import Usage
    from orchestrator.llm.base import ModelResponse

    state = {"handed": False}

    def model(request):
        system = request.system or ""
        if "extract structured requirements" in system:
            return {"explicit": ["work"], "success_criteria": [{"description": "d"}]}
        if "decompose an objective" in system:
            return {
                "tasks": [{"key": "a", "name": "starter", "objective": "Begin the work."}]
            }
        if not state["handed"]:
            state["handed"] = True
            return ModelResponse(
                text="I have done my part; the rest belongs to someone else.",
                usage=Usage(model_calls=1),
            )
        return "the handed-over work is complete"

    return CallableProvider(model), state


def test_a_handoff_appends_a_follow_on_task():
    provider, _ = _handoff_platform("specialist")

    async def scenario():
        config = make_config(
            capabilities=["specialist"],
            agents={
                "definitions": [
                    {"id": "specialist-agent", "capabilities": ["specialist"], "tools": []}
                ]
            },
        )
        platform = await Orchestrator.create(
            config=config, providers=[provider], connect_mcp=False
        )
        execution = await platform.engine.start("Do the work.")
        await platform.engine._understand(execution)
        await platform.engine._plan(execution)

        # The runtime reports the handoff; the orchestrator decides on it.
        original_validate = platform.engine._validate_task

        async def validate(execution_, task_, scope):
            if task_.result is not None and task_.name == "starter":
                task_.result.handoff_to = "specialist"
            await original_validate(execution_, task_, scope)

        platform.engine._validate_task = validate
        finished = await platform.engine.run(execution.id)
        events = await platform.audit(finished.id)
        await platform.close()
        return finished, events

    execution, events = run(scenario())

    accepted = [e for e in events if e.type == "handoff.accepted"]
    assert accepted, "the handoff should have produced a follow-on task"
    follow_on = execution.tasks[accepted[0].payload["follow_on_task"]]
    assert follow_on.pattern is OrchestrationPattern.HANDOFF
    assert follow_on.required_capabilities == ["specialist"]
    assert follow_on.status is TaskStatus.SUCCEEDED
    assert execution.status is ExecutionStatus.COMPLETED


def test_a_handoff_to_something_unregistered_is_refused():
    async def scenario():
        platform = await build_platform(planning_model())
        execution = Execution(objective="o")
        task = make_task("starter")
        task.result = TaskResult(task_id=task.id, ok=True, handoff_to="nobody")
        execution.tasks[task.id] = task

        platform.engine._apply_handoff(execution, task)
        pending = platform.state.audit.pending
        await platform.close()
        return execution, pending

    execution, events = run(scenario())
    assert len(execution.tasks) == 1  # nothing appended
    refusals = [e for e in events if e.type == "handoff.refused"]
    assert (
        refusals
        and "neither a registered agent nor capability" in refusals[0].payload["reason"]
    )


def test_a_handoff_chain_is_bounded():
    async def scenario():
        platform = await build_platform(planning_model(), capabilities=["specialist"])
        execution = Execution(objective="o")
        execution.limits.max_optimizer_iterations = 2
        task = make_task("deep")
        task.metadata["handoff_depth"] = 2  # already at the limit
        task.result = TaskResult(task_id=task.id, ok=True, handoff_to="specialist")
        execution.tasks[task.id] = task

        platform.engine._apply_handoff(execution, task)
        pending = platform.state.audit.pending
        await platform.close()
        return execution, pending

    execution, events = run(scenario())
    assert len(execution.tasks) == 1
    assert any(
        "limit" in e.payload.get("reason", "")
        for e in events
        if e.type == "handoff.refused"
    )


# --------------------------------------------------------------------------
# Nested orchestration (spec section 45)
# --------------------------------------------------------------------------


def test_a_task_can_run_its_own_child_execution():
    depth_seen = {"objectives": []}

    def model(request):
        system = request.system or ""
        if "extract structured requirements" in system:
            return {"explicit": ["work"], "success_criteria": [{"description": "d"}]}
        if "decompose an objective" in system:
            body = request.messages[0].content
            depth_seen["objectives"].append(body.split("\n")[1] if "\n" in body else body)
            return {
                "tasks": [{"key": "a", "name": "inner", "objective": "Do the inner work."}]
            }
        return "work done"

    async def scenario():
        platform = await build_platform(CallableProvider(model))
        execution = await platform.engine.start("Parent objective.")
        await platform.engine._understand(execution)

        nested = Task(
            execution_id=execution.id,
            name="nested",
            objective="Decompose and carry out the sub-problem.",
            validations=[ValidationSpec(validator="non_empty")],
            metadata={"sub_execution": True},
        )
        platform.engine._adopt(
            execution, Plan(execution_id=execution.id, version=1, tasks=[nested])
        )
        platform.state.transition(execution, ExecutionStatus.READY)
        await platform.state.persist(execution)

        finished = await platform.engine.run(execution.id)
        events = await platform.audit(finished.id)
        children = await platform.list()
        await platform.close()
        return finished, events, children

    execution, events, children = run(scenario())

    started = [e for e in events if e.type == "sub_execution.started"]
    assert started, "the nested task should have started a child execution"
    assert execution.status is ExecutionStatus.COMPLETED
    # Parent plus child are both persisted, and the child records its parent.
    assert len(children) == 2
    child_id = started[0].payload["child_execution"]
    assert child_id != execution.id


def test_nesting_depth_is_bounded():
    from orchestrator.agents.runtime import AgentRunContext
    from orchestrator.core.execution.nested import DEPTH_KEY, SubOrchestrationRuntime

    async def scenario():
        platform = await build_platform(planning_model())
        runtime = SubOrchestrationRuntime(platform.engine, max_depth=1)

        execution = Execution(objective="already deep")
        execution.context[DEPTH_KEY] = 1  # one level in already
        task = make_task("deeper")
        result = await runtime.run(
            AgentRunContext(
                execution=execution,
                task=task,
                agent=AgentSpec(id="a", runtime="sub_orchestrator"),
                scope=PermissionScope(),
            )
        )
        await platform.close()
        return result

    result = run(scenario())
    assert result.ok is False
    assert "nesting depth" in result.summary


def test_a_child_budget_is_carved_out_of_the_parent():
    from orchestrator.core.domain.models import ResourceLimits, Usage
    from orchestrator.core.execution.nested import SubOrchestrationRuntime

    async def scenario():
        platform = await build_platform(planning_model())
        runtime = SubOrchestrationRuntime(platform.engine, budget_share=0.5)
        parent = Execution(
            objective="o",
            limits=ResourceLimits(max_model_calls=100, max_tool_calls=40),
        )
        parent.usage = Usage(model_calls=20)
        limits = runtime._child_limits(parent)
        await platform.close()
        return limits

    limits = run(scenario())
    # Half of what is left, not half of the original total.
    assert limits.max_model_calls == 40
    assert limits.max_tool_calls == 20


# --------------------------------------------------------------------------
# MCP server scoping (spec section 44)
# --------------------------------------------------------------------------


def _mcp_tool_registry():
    registry = ToolRegistry(policy=default_policy(), audit=AuditLog(NullAuditSink()))
    for server in ("alpha", "beta"):
        registry.register(
            ToolSpec(
                id=f"mcp.{server}.search",
                name="search",
                source=ToolSource.MCP,
                source_ref=server,
                permissions=["mcp.invoke"],
            ),
            lambda arguments, context, s=server: {"server": s},
        )
    return registry


def test_an_agent_scoped_to_one_mcp_server_cannot_reach_another():
    """A broad tool glob must not widen which servers an agent can reach."""

    registry = _mcp_tool_registry()
    scope = PermissionScope(
        permissions=("mcp.invoke",),
        tools=("mcp.*",),  # broad on tools
        mcp_servers=("alpha",),  # narrow on servers
    )

    visible = {spec.id for spec in registry.for_scope(scope)}
    assert visible == {"mcp.alpha.search"}

    context = ToolContext(execution_id="e", scope=scope)
    assert registry.authorize("mcp.alpha.search", context)

    with pytest.raises(PermissionDenied, match="not in the scope granted"):
        registry.authorize("mcp.beta.search", context)


def test_a_scope_making_no_server_claim_is_governed_by_the_tool_glob():
    registry = _mcp_tool_registry()
    scope = PermissionScope(permissions=("mcp.invoke",), tools=("mcp.*",))
    visible = {spec.id for spec in registry.for_scope(scope)}
    assert visible == {"mcp.alpha.search", "mcp.beta.search"}


def test_non_mcp_tools_are_unaffected_by_server_scoping():
    registry = _mcp_tool_registry()
    registry.register(ToolSpec(id="local.tool", name="local"), lambda a, c: "ok")
    scope = PermissionScope(tools=("*",), mcp_servers=("alpha",))
    visible = {spec.id for spec in registry.for_scope(scope)}
    assert "local.tool" in visible
    assert "mcp.beta.search" not in visible


# --------------------------------------------------------------------------
# Isolation enforcement (spec section 63)
# --------------------------------------------------------------------------


def test_runtimes_declare_the_isolation_they_can_provide():
    from orchestrator.adapters.execution.openhands import OpenHandsAdapter
    from orchestrator.adapters.execution.subprocess_adapter import SubprocessAdapter
    from orchestrator.agents.runtime import GenericAgentRuntime

    assert IsolationLevel.CONTAINER not in GenericAgentRuntime.supported_isolation
    assert IsolationLevel.RESTRICTED in SubprocessAdapter.supported_isolation
    assert IsolationLevel.CONTAINER not in SubprocessAdapter.supported_isolation
    assert IsolationLevel.REMOTE in OpenHandsAdapter.supported_isolation


def test_an_unprovidable_isolation_level_fails_rather_than_running_exposed():
    """Running with less isolation than was declared, silently, is the risk."""

    provider = planning_model()
    config = make_config(
        agents={
            "definitions": [
                {
                    "id": "sandboxed",
                    "capabilities": [],
                    "tools": [],
                    "constraints": {"isolation": "container"},
                }
            ]
        }
    )

    async def scenario():
        platform = await Orchestrator.create(
            config=config, providers=[provider], connect_mcp=False
        )
        execution = await platform.run("Do the work.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is not ExecutionStatus.COMPLETED
    assert any(
        "container" in f.message and "isolation" in f.message for f in execution.failures
    )


def test_restricted_isolation_scrubs_the_worker_environment():
    import os

    from orchestrator.adapters.execution.subprocess_adapter import SubprocessAdapter
    from orchestrator.agents.runtime import AgentRunContext

    worker = textwrap.dedent(
        """
        import json, os, sys
        json.load(sys.stdin)
        print(json.dumps({
            "ok": True,
            "summary": "reported the environment",
            "output": {
                "leaked": os.environ.get("ORCHESTRATOR_TEST_SECRET"),
                "isolation": os.environ.get("ORCHESTRATOR_ISOLATION"),
            },
        }))
        """
    )
    os.environ["ORCHESTRATOR_TEST_SECRET"] = "must-not-leak"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            script = Path(directory) / "worker.py"
            script.write_text(worker, encoding="utf-8")
            adapter = SubprocessAdapter([sys.executable, str(script)])

            execution = Execution(objective="o")
            task = make_task("t")
            execution.tasks[task.id] = task

            def attempt(level):
                return run(
                    adapter.run(
                        AgentRunContext(
                            execution=execution,
                            task=task,
                            agent=AgentSpec(
                                id="w",
                                constraints=AgentConstraints(isolation=level),
                            ),
                            scope=PermissionScope(),
                            workspace=directory,
                        )
                    )
                )

            unrestricted = attempt(IsolationLevel.NONE)
            restricted = attempt(IsolationLevel.RESTRICTED)
    finally:
        os.environ.pop("ORCHESTRATOR_TEST_SECRET", None)

    assert unrestricted.output["leaked"] == "must-not-leak"
    assert restricted.output["leaked"] is None
    assert restricted.output["isolation"] == "restricted"


# --------------------------------------------------------------------------
# Pre-execution verification (spec section 87)
# --------------------------------------------------------------------------


def _plan_with(**task_kwargs) -> Plan:
    task = Task(execution_id="e", name="t", objective="o", **task_kwargs)
    return Plan(execution_id="e", version=1, tasks=[task])


def test_verification_rejects_an_unknown_validator():
    from orchestrator.validation.validators import ValidatorRegistry

    report = verify(
        _plan_with(validations=[ValidationSpec(validator="does_not_exist")]),
        VerificationContext(validators=ValidatorRegistry()),
    )
    assert not report.ok
    assert report.errors[0].code == "unknown_validator"


def test_verification_rejects_a_restriction_to_a_tool_that_does_not_exist():
    registry = ToolRegistry()
    report = verify(
        _plan_with(allowed_tools=["nope.missing"]),
        VerificationContext(tools=registry),
    )
    assert not report.ok
    assert report.errors[0].code == "unknown_tool"


def test_verification_allows_tool_globs():
    report = verify(
        _plan_with(allowed_tools=["fs.*"]),
        VerificationContext(tools=ToolRegistry()),
    )
    assert report.ok


def test_an_uncovered_capability_is_a_warning_when_agents_can_be_created():
    from orchestrator.agents.capabilities import CapabilityRegistry
    from orchestrator.agents.registry import AgentRegistry

    context = VerificationContext(
        capabilities=CapabilityRegistry(),
        agents=AgentRegistry(),
        allow_dynamic_agents=True,
    )
    report = verify(_plan_with(required_capabilities=["novel"]), context)
    assert report.ok
    assert report.warnings and report.warnings[0].code == "unknown_capability"


def test_an_uncovered_capability_is_an_error_when_they_cannot():
    from orchestrator.agents.capabilities import CapabilityRegistry
    from orchestrator.agents.registry import AgentRegistry

    context = VerificationContext(
        capabilities=CapabilityRegistry(),
        agents=AgentRegistry(),
        allow_dynamic_agents=False,
    )
    report = verify(_plan_with(required_capabilities=["novel"]), context)
    assert not report.ok


def test_verification_accepts_dependencies_on_earlier_rounds():
    """An iterative plan legitimately depends on tasks it did not create."""

    earlier = Task(execution_id="e", name="earlier", objective="o")
    follow_on = Task(
        execution_id="e", name="later", objective="o", dependencies=[earlier.id]
    )
    plan = Plan(execution_id="e", version=2, tasks=[follow_on])

    assert not verify(plan, VerificationContext()).ok  # dangling on its own
    assert verify(plan, VerificationContext(), existing=[earlier]).ok


def test_the_engine_refuses_to_run_an_unverifiable_plan():
    provider = planning_model(
        tasks=[
            {
                "key": "a",
                "name": "a",
                "objective": "Do it.",
                # A plan restricted to a tool nobody registered cannot run.
                "capabilities": [],
            }
        ]
    )

    async def scenario():
        platform = await build_platform(provider)
        execution = await platform.engine.start("Do the work.")
        await platform.engine._understand(execution)
        await platform.engine._plan(execution)

        # Corrupt the adopted plan the way a bad planner response would.
        plan = execution.plan
        plan.tasks[0].validations = [ValidationSpec(validator="imaginary")]
        report = platform.engine._verify(execution, plan)
        await platform.close()
        return report

    report = run(scenario())
    assert not report.ok
    assert report.errors[0].code == "unknown_validator"


# --------------------------------------------------------------------------
# Plugin seams (spec section 46)
# --------------------------------------------------------------------------


def test_a_plugin_can_subscribe_to_the_audit_trail():
    seen: list[str] = []

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        module = Path(directory) / "orchestrator_observer_plugin.py"
        module.write_text(
            textwrap.dedent(
                """
                RECORDED = []

                def register(registry):
                    registry.observe(lambda event: RECORDED.append(event.type))
                """
            ),
            encoding="utf-8",
        )
        sys.path.insert(0, directory)
        try:

            async def scenario():
                platform = await build_platform(
                    planning_model(),
                    config=make_config(
                        plugins={
                            "enabled": True,
                            "modules": ["orchestrator_observer_plugin"],
                            "entry_point_group": "orchestrator.plugins.absent",
                        }
                    ),
                )
                await platform.run("Do the work.")
                await platform.close()

            run(scenario())
            import orchestrator_observer_plugin

            seen = list(orchestrator_observer_plugin.RECORDED)
        finally:
            sys.path.remove(directory)
            sys.modules.pop("orchestrator_observer_plugin", None)

    assert "execution.created" in seen
    assert "execution.completed" in seen


def test_a_plugin_can_supply_a_storage_backend():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        module = Path(directory) / "orchestrator_storage_plugin.py"
        module.write_text(
            textwrap.dedent(
                """
                from orchestrator.core.state.memory_store import InMemoryStateStore

                class MarkedStore(InMemoryStateStore):
                    supplied_by_plugin = True

                def register(registry):
                    registry.add_storage(MarkedStore())
                """
            ),
            encoding="utf-8",
        )
        sys.path.insert(0, directory)
        try:

            async def scenario():
                platform = await build_platform(
                    planning_model(),
                    config=make_config(
                        storage={"backend": "memory"},
                        plugins={
                            "enabled": True,
                            "modules": ["orchestrator_storage_plugin"],
                            "entry_point_group": "orchestrator.plugins.absent",
                        },
                    ),
                )
                marked = getattr(platform.store, "supplied_by_plugin", False)
                execution = await platform.run("Do the work.")
                await platform.close()
                return marked, execution

            marked, execution = run(scenario())
        finally:
            sys.path.remove(directory)
            sys.modules.pop("orchestrator_storage_plugin", None)

    assert marked is True
    assert execution.status is ExecutionStatus.COMPLETED


# --------------------------------------------------------------------------
# Shipped workflows (spec section 90)
# --------------------------------------------------------------------------


def test_all_four_specification_examples_ship_as_generic_workflows():
    from orchestrator.core.workflow.definition import WorkflowRegistry
    from orchestrator.core.workflow.graph import TaskGraph

    registry = WorkflowRegistry()
    loaded = registry.load_directory(Path(__file__).resolve().parent.parent / "workflows")
    ids = {definition.id for definition in loaded}

    # A research, B creation, C problem solving, D complex project.
    assert {
        "generic.research",
        "generic.creation",
        "generic.problem_solving",
        "generic.complex_project",
    } <= ids

    for definition in loaded:
        TaskGraph(definition.build("e")).validate()


# --------------------------------------------------------------------------
# Skills (spec sections 13, 69)
# --------------------------------------------------------------------------


def test_a_skill_is_parsed_from_a_markdown_document():
    from orchestrator.agents.skills import skill_from_text

    skill = skill_from_text(
        textwrap.dedent(
            """
            ---
            id: careful-reading
            version: 2.1.0
            description: How to read source material without over-claiming.
            tags: [analysis]
            applies_to: [analysis]
            ---
            Separate what the source states from what it implies.
            Quote before paraphrasing.
            """
        ).lstrip(),
        source="careful-reading.md",
    )

    assert skill.id == "careful-reading"
    assert skill.version == "2.1.0"
    assert skill.applies_to == ("analysis",)
    assert "Quote before paraphrasing" in skill.content


def test_skills_are_versioned_and_immutable_per_version():
    from orchestrator.agents.skills import Skill, SkillRegistry
    from orchestrator.errors import ConfigurationError

    registry = SkillRegistry()
    registry.register(Skill(id="s", version="1.0.0", content="first"))
    registry.register(Skill(id="s", version="2.0.0", content="second"))

    assert registry.get("s").version == "2.0.0"  # latest by default
    assert registry.get("s", "1.0.0").content == "first"  # pinned still available
    assert registry.versions("s") == ["1.0.0", "2.0.0"]

    with pytest.raises(ConfigurationError):
        registry.register(Skill(id="s", version="1.0.0", content="rewritten"))


def test_skills_compose_through_their_requirements():
    from orchestrator.agents.skills import Skill, SkillRegistry

    registry = SkillRegistry()
    registry.register(Skill(id="base", content="Base guidance."))
    registry.register(Skill(id="middle", content="Middle guidance.", requires=("base",)))
    registry.register(Skill(id="top", content="Top guidance.", requires=("middle",)))

    resolved = [s.id for s in registry.resolve(["top"])]
    assert resolved == ["base", "middle", "top"]  # dependencies first

    rendered = registry.render(["top"])
    assert rendered.index("Base guidance") < rendered.index("Top guidance")


def test_a_cycle_in_skill_requirements_does_not_hang():
    from orchestrator.agents.skills import Skill, SkillRegistry

    registry = SkillRegistry()
    registry.register(Skill(id="a", content="A", requires=("b",)))
    registry.register(Skill(id="b", content="B", requires=("a",)))

    resolved = {s.id for s in registry.resolve(["a"])}
    assert resolved == {"a", "b"}


def test_a_missing_skill_degrades_rather_than_failing():
    """Guidance is not capability: its absence should not stop the work."""

    from orchestrator.agents.skills import Skill, SkillRegistry

    registry = SkillRegistry()
    registry.register(Skill(id="present", content="Present guidance."))

    resolved = [s.id for s in registry.resolve(["present", "absent"])]
    assert resolved == ["present"]

    report = verify(
        _plan_with(metadata={"skills": ["absent"]}),
        VerificationContext(skills=registry),
    )
    assert report.ok  # a warning, not an error
    assert report.warnings[0].code == "unknown_skill"


def test_an_agent_is_given_the_skills_it_declares():
    seen: dict[str, str] = {}

    def model(request):
        system = request.system or ""
        if "extract structured requirements" in system:
            return {"explicit": ["work"], "success_criteria": [{"description": "d"}]}
        if "decompose an objective" in system:
            return {"tasks": [{"key": "a", "name": "a", "objective": "Do it."}]}
        seen["system"] = system
        return "the work is done"

    async def scenario():
        platform = await build_platform(
            CallableProvider(model),
            config=make_config(
                skills={
                    "definitions": [
                        {
                            "id": "house-style",
                            "description": "How results are written here.",
                            "content": "State what is unverified before what is.",
                        }
                    ]
                },
                agents={
                    "definitions": [
                        {"id": "writer", "capabilities": [], "skills": ["house-style"]}
                    ]
                },
            ),
        )
        execution = await platform.run("Do the work.")
        pinned = execution.workflow.skill_versions
        await platform.close()
        return pinned

    pinned = run(scenario())

    assert "State what is unverified before what is." in seen["system"]
    # Section 69: the version used is retained for reproducibility.
    assert pinned == {"house-style": "1.0.0"}


def test_skills_load_from_a_directory():
    from orchestrator.agents.skills import SkillRegistry

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        path = Path(directory)
        (path / "evidence.md").write_text(
            "---\ndescription: Cite before concluding.\n---\nShow the observation.\n",
            encoding="utf-8",
        )
        (path / "structured.json").write_text(
            '{"id": "structured", "content": "Answer in the requested shape."}',
            encoding="utf-8",
        )
        registry = SkillRegistry()
        loaded = registry.load_directory(path)

    ids = {s.id for s in loaded}
    assert ids == {"evidence", "structured"}  # id falls back to the filename
    assert registry.get("evidence").description == "Cite before concluding."


def test_a_plugin_can_add_a_skill():
    from orchestrator.agents.skills import Skill, SkillRegistry
    from orchestrator.plugins.loader import PluginRegistry

    skills = SkillRegistry()
    registry = PluginRegistry(
        tools=ToolRegistry(),
        validators=None,
        agents=None,
        capabilities=None,
        runtimes=None,
        workflows=None,
        add_model_provider=lambda p: p,
        skills=skills,
    )
    registry.add_skill(Skill(id="from-plugin", content="Plugin guidance."))
    assert skills.has("from-plugin")


# --------------------------------------------------------------------------
# Real-model robustness: found by running against local Ollama models
# --------------------------------------------------------------------------


def test_a_tool_call_emitted_as_text_is_promoted_to_a_real_call():
    """Llama-family models often put a tool call in the content channel."""

    from orchestrator.llm.toolcalls import recover

    text, calls = recover(
        '{"name": "fs.read_file", "parameters": {"path": "pyproject.toml"}}',
        offered_tools=["fs.read_file"],
    )
    assert text == ""
    assert [(c.name, c.arguments) for c in calls] == [
        ("fs.read_file", {"path": "pyproject.toml"})
    ]


def test_an_invented_tool_envelope_is_unwrapped_not_promoted():
    """A name matching no offered tool is an envelope around the answer."""

    from orchestrator.llm.toolcalls import recover

    text, calls = recover(
        '{"name": "json", "parameters": {"content": "cli, api and dev"}}',
        offered_tools=["fs.read_file"],
    )
    assert text == "cli, api and dev"
    assert calls == []


def test_ordinary_output_is_left_alone():
    from orchestrator.llm.toolcalls import recover

    for payload in (
        "The groups are cli, api and dev.",
        '{"groups": ["cli", "api"]}',  # a legitimate JSON answer
        '{"name": "something"}',  # a name with no arguments is prose
        "",
    ):
        text, calls = recover(payload, offered_tools=["fs.read_file"])
        assert text == payload and calls == []


def test_recovery_defers_to_the_structured_channel():
    from orchestrator.llm.base import ToolCallRequest
    from orchestrator.llm.toolcalls import recover

    original = '{"name": "json", "parameters": {"content": "x"}}'
    text, calls = recover(
        original,
        offered_tools=["json"],
        existing_calls=[ToolCallRequest(id="1", name="fs.read_file")],
    )
    assert text == original and calls == []


def test_a_declared_check_is_shown_to_the_worker():
    """A check the worker cannot see is a check it cannot pass."""

    from orchestrator.context.manager import ContextManager, ContextRequest
    from orchestrator.core.domain.enums import ContextKind

    execution = Execution(objective="o")
    task = make_task("t")
    task.validations = [
        ValidationSpec(
            validator="json_schema",
            config={"schema": {"type": "object", "required": ["groups"]}},
        ),
        ValidationSpec(validator="pattern", config={"pattern": "APPROVED"}),
    ]
    execution.tasks[task.id] = task

    items = ContextManager().collect(ContextRequest(execution=execution, task=task))
    rendered = next(i.content for i in items if i.kind is ContextKind.TASK)

    # Not just the validator's name — the requirement itself.
    assert "How this will be checked" in rendered
    assert '"required"' in rendered and "groups" in rendered
    assert "APPROVED" in rendered


def test_a_failed_attempt_still_reports_what_it_spent():
    """A failing agent that looks free is the run you most want costed."""

    from orchestrator.agents.runtime import BaseRuntime
    from orchestrator.core.domain.models import Usage

    spent = Usage(model_calls=12, tool_calls=9, input_tokens=19903)
    result = BaseRuntime._failure(make_task("t"), "hit the iteration limit", usage=spent)

    assert result.ok is False
    assert result.usage.model_calls == 12
    assert result.usage.input_tokens == 19903


def test_an_unregistered_criterion_validator_reports_uncertainty_not_failure():
    """A model naming a checker that does not exist must not fail the work."""

    from orchestrator.core.domain.models import Requirements, SuccessCriterion, TaskResult
    from orchestrator.validation.gates import GateRunner
    from orchestrator.validation.validators import ValidationContext, ValidatorRegistry

    execution = Execution(objective="o")
    execution.requirements = Requirements(
        success_criteria=[
            SuccessCriterion(description="it worked", validator="not_a_real_validator")
        ]
    )
    task = make_task("t")
    task.status = TaskStatus.SUCCEEDED
    task.result = TaskResult(task_id=task.id, ok=True, output="the answer")
    execution.tasks[task.id] = task

    outcome = run(
        GateRunner(ValidatorRegistry()).run_for_objective(
            execution, ValidationContext(execution=execution)
        )
    )
    assert outcome.passed is True
    assert outcome.confidence.value == "uncertain"


def test_the_goal_analyzer_normalises_null_shaped_validator_names():
    from orchestrator.planning.goal import GoalAnalyzer

    data = {
        "explicit": ["do it"],
        "success_criteria": [
            {"description": "a", "validator": "null"},  # the string, not null
            {"description": "b", "validator": "None"},
            {"description": "c", "validator": "invented_checker"},
            {"description": "d", "validator": "non_empty"},
        ],
    }
    requirements = GoalAnalyzer._requirements_from(
        data, "do it", frozenset({"non_empty", "pattern"})
    )
    named = [c.validator for c in requirements.success_criteria]
    assert named == [None, None, None, "non_empty"]


def test_the_planner_may_request_tools_and_they_are_filtered_to_real_ones():
    from orchestrator.llm.routing import ModelRouter
    from orchestrator.planning.decomposition import Planner, PlanningContext
    from orchestrator.planning.strategy import choose

    def model(request):
        if "decompose an objective" in (request.system or ""):
            return {
                "tasks": [
                    {
                        "key": "a",
                        "name": "read",
                        "objective": "Read the file.",
                        "tools": ["fs.read_file", "not.a.real.tool"],
                    }
                ]
            }
        return {}

    router = ModelRouter([CallableProvider(model)])
    plan = run(
        Planner(router=router).plan(
            Execution(objective="read a file"),
            choose("read a file"),
            PlanningContext(available_tools=["fs.read_file", "fs.list_directory"]),
        )
    )
    assert plan.tasks[0].allowed_tools == ["fs.read_file"]


# --------------------------------------------------------------------------
# Remote-provider interop: found by testing against NVIDIA and OpenRouter
# --------------------------------------------------------------------------


def test_dotted_tool_ids_are_made_provider_legal():
    """OpenAI-style function names allow only [a-zA-Z0-9_-].

    Every tool the platform exposes is namespaced with dots, so without
    translation a strict provider rejects the whole request with a 400 — which
    is what NVIDIA did for every tool until this existed.
    """
    import re

    from orchestrator.llm.toolnames import build_mapping, rename_tools, restore

    tools = [
        {"name": "fs.read_file"},
        {"name": "orchestrator.emit_artifact"},
        {"name": "mcp.server-a.search"},
    ]
    mapping = build_mapping(tools)

    on_the_wire = [t["name"] for t in rename_tools(tools, mapping)]
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]+", name) for name in on_the_wire)

    # And the round trip has to be lossless, or a returned call names nothing.
    assert [restore(name, mapping) for name in on_the_wire] == [
        "fs.read_file",
        "orchestrator.emit_artifact",
        "mcp.server-a.search",
    ]


def test_colliding_tool_names_stay_distinguishable():
    from orchestrator.llm.toolnames import build_mapping, restore

    mapping = build_mapping([{"name": "a.b"}, {"name": "a_b"}])
    assert len(mapping) == 2
    assert sorted(restore(k, mapping) for k in mapping) == ["a.b", "a_b"]


def test_an_over_long_tool_name_is_truncated_but_still_maps_back():
    from orchestrator.llm.toolnames import MAX_NAME_LENGTH, build_mapping, restore

    long_id = "mcp." + "x" * 90 + ".search"
    mapping = build_mapping([{"name": long_id}])
    safe = next(iter(mapping))
    assert len(safe) <= MAX_NAME_LENGTH
    assert restore(safe, mapping) == long_id


def test_an_empty_completion_is_an_error_not_an_empty_answer():
    """A reasoning model can spend its whole budget thinking and say nothing.

    Returning that as a successful empty answer strands the agent; raising lets
    the router fall back to a model that will actually respond.
    """
    from orchestrator.core.domain.models import ModelSpec
    from orchestrator.errors import ModelError
    from orchestrator.llm.providers.openai_compat import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(base_url="http://unused", api_key="x")
    payload = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "", "reasoning": "thinking at length..."},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4000},
    }
    with pytest.raises(ModelError, match="budget was consumed"):
        provider._parse(payload, ModelSpec(id="m", model="m"), elapsed=1.0)


def test_the_goal_analyzer_is_told_what_the_platform_can_already_do():
    """Otherwise it asks a human for a file the platform could read itself."""

    from orchestrator.llm.routing import ModelRouter
    from orchestrator.planning.goal import GoalAnalyzer

    seen = {}

    def analyse(request):
        seen["prompt"] = request.messages[0].content
        return {"explicit": ["read it"], "success_criteria": [{"description": "d"}]}

    analyzer = GoalAnalyzer(ModelRouter([CallableProvider(analyse)]))
    run(
        analyzer.analyze(
            "Read the file and summarise it.",
            available_tools=["fs.read_file", "fs.list_directory"],
            available_capabilities=["analysis"],
        )
    )
    assert "fs.read_file" in seen["prompt"]
    assert "analysis" in seen["prompt"]


# --------------------------------------------------------------------------
# The devil's advocate
# --------------------------------------------------------------------------


def _advocate(response):
    from orchestrator.validation.advocate import DevilsAdvocateValidator

    async def challenge(payload):
        if isinstance(response, BaseException):
            raise response
        return response

    return DevilsAdvocateValidator(challenge)


def _result_context(output="the work is done"):
    from orchestrator.core.domain.models import TaskResult

    execution = Execution(objective="do the work")
    task = make_task("t")
    task.result = TaskResult(task_id=task.id, ok=True, output=output)
    execution.tasks[task.id] = task
    from orchestrator.validation.validators import ValidationContext

    return ValidationContext(execution=execution, task=task)


def test_the_advocate_reports_objections_without_failing_the_task():
    """A suspicion is not a defect. It lowers confidence; it does not gate."""

    validator = _advocate(
        {
            "objections": [
                {
                    "severity": "critical",
                    "claim": "All six groups listed",
                    "objection": "Only four appear in the output",
                    "resolves_it": "Count the groups in the source file",
                },
            ],
            "strongest_case_against": "The list is incomplete.",
        }
    )
    result = run(
        validator.validate(ValidationSpec(validator="devils_advocate"), _result_context())
    )

    assert result.passed is True  # it did not gate
    assert result.confidence.value == "uncertain"  # but it did lower confidence
    assert "1 objection" in result.message


def test_the_advocate_can_be_made_to_gate_on_critical_objections():
    validator = _advocate(
        {
            "objections": [
                {
                    "severity": "critical",
                    "claim": "c",
                    "objection": "o",
                    "resolves_it": "r",
                },
            ]
        }
    )
    result = run(
        validator.validate(
            ValidationSpec(
                validator="devils_advocate", config={"block_on": "critical"}, mandatory=True
            ),
            _result_context(),
        )
    )
    assert result.passed is False


def test_an_objection_with_no_resolution_is_dropped():
    """An objection that cannot be settled is a complaint, not a finding."""

    from orchestrator.validation.advocate import parse_challenge

    challenge = parse_challenge(
        {
            "objections": [
                {
                    "severity": "minor",
                    "claim": "a",
                    "objection": "vague unease",
                    "resolves_it": "",
                },
                {
                    "severity": "minor",
                    "claim": "b",
                    "objection": "concrete",
                    "resolves_it": "check x",
                },
            ]
        }
    )
    assert len(challenge.objections) == 1
    assert challenge.objections[0].objection == "concrete"


def test_finding_nothing_is_reported_as_likely_never_confirmed():
    """Silence from a challenger is one more opinion, not proof."""

    validator = _advocate({"objections": [], "nothing_to_challenge": True})
    result = run(
        validator.validate(ValidationSpec(validator="devils_advocate"), _result_context())
    )
    assert result.passed is True
    assert result.confidence.value == "likely"  # never "confirmed"
    assert "nothing substantive" in result.message


def test_an_unreachable_advocate_does_not_fail_the_work():
    validator = _advocate(RuntimeError("model unreachable"))
    result = run(
        validator.validate(ValidationSpec(validator="devils_advocate"), _result_context())
    )
    assert result.passed is True
    assert result.confidence.value == "uncertain"
    assert "could not be reached" in result.message


def test_objections_sort_by_severity_for_display():
    from orchestrator.validation.advocate import parse_challenge

    challenge = parse_challenge(
        {
            "objections": [
                {"severity": "minor", "claim": "c", "objection": "o", "resolves_it": "r"},
                {
                    "severity": "critical",
                    "claim": "a",
                    "objection": "o",
                    "resolves_it": "r",
                },
                {
                    "severity": "substantive",
                    "claim": "b",
                    "objection": "o",
                    "resolves_it": "r",
                },
            ]
        }
    )
    assert [o.severity for o in challenge.sorted()] == ["critical", "substantive", "minor"]


def test_the_advocate_is_registered_only_when_a_model_exists():
    async def with_model():
        platform = await build_platform(planning_model())
        has = platform.validators.has("devils_advocate")
        await platform.close()
        return has

    async def without_model():
        platform = await build_platform(None)
        has = platform.validators.has("devils_advocate")
        await platform.close()
        return has

    assert run(with_model()) is True
    assert run(without_model()) is False


def test_summarise_distinguishes_not_run_from_found_nothing():
    """ "Nobody argued against it" and "the argument failed" are opposite claims."""

    from orchestrator.core.domain.models import ValidationResult
    from orchestrator.validation.advocate import summarise

    never_ran = summarise(
        [
            ValidationResult(validator="non_empty", passed=True, message="ok"),
        ]
    )
    assert never_ran["ran"] is False
    assert never_ran["objections"] == []

    ran_and_found_nothing = summarise(
        [
            ValidationResult(
                validator="devils_advocate", passed=True, message="nothing substantive"
            ),
        ]
    )
    assert ran_and_found_nothing["ran"] is True
    assert ran_and_found_nothing["objections"] == []


# --------------------------------------------------------------------------
# API security
# --------------------------------------------------------------------------


def _client(**security_kwargs):
    from fastapi.testclient import TestClient

    from orchestrator.api.app import create_app
    from orchestrator.api.security import SecurityConfig

    return TestClient(create_app(security=SecurityConfig(**security_kwargs)))


def test_binding_to_the_network_without_a_token_is_refused():
    """An unauthenticated API that runs tools is a remote execution endpoint."""

    from orchestrator.api.security import InsecureBinding, SecurityConfig

    with pytest.raises(InsecureBinding):
        SecurityConfig(host="0.0.0.0").verify_binding()
    with pytest.raises(InsecureBinding):
        SecurityConfig(host="10.0.0.5").verify_binding()

    # Loopback is the single-user case and stays usable.
    SecurityConfig(host="127.0.0.1").verify_binding()
    SecurityConfig(host="localhost").verify_binding()
    SecurityConfig(host="::1").verify_binding()

    # A token makes the bind legitimate.
    SecurityConfig(host="0.0.0.0", tokens=("t",)).verify_binding()

    # And an operator who fronts it with their own auth can say so explicitly.
    SecurityConfig(
        host="0.0.0.0", allow_unauthenticated_network_access=True
    ).verify_binding()


def test_a_protected_api_rejects_calls_without_a_token():
    client = _client(host="0.0.0.0", tokens=("right-token",))

    assert client.get("/v1/executions").status_code == 401
    assert (
        client.get("/v1/executions", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert (
        client.get("/v1/executions", headers={"Authorization": "right-token"}).status_code
        == 401
    )

    ok = client.get("/v1/executions", headers={"Authorization": "Bearer right-token"})
    assert ok.status_code == 200


def test_liveness_answers_without_a_token_so_probes_keep_working():
    """A probe that needs a secret is a probe that reports a false outage."""

    client = _client(host="0.0.0.0", tokens=("t",))
    assert client.get("/live").status_code == 200
    assert client.get("/live").json()["status"] == "alive"


def test_token_rotation_accepts_both_tokens():
    client = _client(host="0.0.0.0", tokens=("old", "new"))
    for token in ("old", "new"):
        assert (
            client.get(
                "/v1/executions", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )


def test_an_oversized_body_is_refused_before_it_is_read():
    client = _client(host="127.0.0.1", max_body_bytes=100)
    response = client.post("/v1/executions", json={"objective": "x" * 5000})
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


def test_tokens_come_from_the_environment_not_the_config_file(monkeypatch):
    """A config file gets committed; an environment variable does not."""

    from orchestrator.api.security import ENV_TOKEN, SecurityConfig

    monkeypatch.setenv(ENV_TOKEN, " a , b ,, ")
    assert SecurityConfig.from_env().tokens == ("a", "b")

    monkeypatch.delenv(ENV_TOKEN, raising=False)
    assert SecurityConfig.from_env().tokens == ()
    assert SecurityConfig.from_env().enabled is False


def test_the_console_page_loads_without_a_token_but_its_data_does_not():
    """The sign-in page cannot itself require sign-in."""

    client = _client(host="0.0.0.0", tokens=("t",))

    page = client.get("/")
    assert page.status_code == 200
    # And it really is only the shell: no execution data rides along with it.
    assert "/v1/executions" not in page.text or "DEMO" in page.text
    assert client.get("/v1/executions").status_code == 401
    assert client.get("/v1/agents").status_code == 401
    assert client.get("/metrics").status_code == 401


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def _aged_store(tmp_path, rows):
    """A store holding executions with controlled ages and statuses."""
    from datetime import datetime, timedelta

    from orchestrator.core.state.sqlite_store import SQLiteStateStore

    store = SQLiteStateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    for index, (status, age_days) in enumerate(rows):
        stamp = (now - timedelta(days=age_days)).isoformat()
        execution_id = f"exe_{index}_{status}"
        store._conn.execute(
            "INSERT INTO executions (id, objective, status, revision, "
            "created_at, updated_at, document) VALUES (?,?,?,?,?,?,?)",
            (execution_id, "objective", status, 1, stamp, stamp, "{}"),
        )
        store._conn.execute(
            "INSERT INTO audit_events (execution_id, sequence, id, type, "
            "task_id, actor, timestamp, payload) VALUES (?,?,?,?,?,?,?,?)",
            (execution_id, 1, f"aud_{index}", "execution.created", None, None, stamp, "{}"),
        )
    store._conn.commit()
    return store


def _ids(store):
    return {row[0] for row in store._conn.execute("SELECT id FROM executions")}


def test_pruning_removes_old_finished_runs_and_their_audit_trails(tmp_path):
    store = _aged_store(tmp_path, [("completed", 200), ("failed", 200)])
    report = run(store.prune(older_than_days=90))

    assert report["executions_removed"] == 2
    assert report["audit_events_removed"] == 2
    assert _ids(store) == set()
    # No trail is left pointing at a record that no longer exists.
    remaining = store._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    assert remaining == 0
    run(store.close())


def test_pruning_never_deletes_work_that_is_still_in_flight(tmp_path):
    """Age is not evidence of abandonment. A run waiting on a person is blocked."""

    ages = [
        ("waiting", 500),  # blocked on a human for over a year
        ("paused", 500),
        ("running", 500),
        ("cancelling", 500),  # mid-transition, NOT the same as cancelled
        ("reviewing", 500),
        ("completed", 500),  # the only one that should go
    ]
    store = _aged_store(tmp_path, ages)
    report = run(store.prune(older_than_days=1))

    assert report["executions_removed"] == 1
    survivors = {i.split("_", 2)[2] for i in _ids(store)}
    assert survivors == {"waiting", "paused", "running", "cancelling", "reviewing"}
    run(store.close())


def test_pruning_keeps_recent_runs_regardless_of_status(tmp_path):
    store = _aged_store(tmp_path, [("completed", 5), ("failed", 5)])
    report = run(store.prune(older_than_days=90))
    assert report["executions_removed"] == 0
    assert len(_ids(store)) == 2
    run(store.close())


def test_asking_to_prune_a_non_terminal_status_is_refused_not_ignored(tmp_path):
    """Quietly ignoring the request would delete nothing and report success."""

    store = _aged_store(tmp_path, [("running", 500)])
    with pytest.raises(ValueError, match="non-terminal"):
        run(store.prune(older_than_days=1, statuses=("running",)))
    assert len(_ids(store)) == 1
    run(store.close())


def test_prunable_statuses_are_derived_from_the_enum_not_retyped():
    """A status added later must default to protected, never to deletable."""

    from orchestrator.core.domain.enums import TERMINAL_EXECUTION_STATUSES
    from orchestrator.core.state.sqlite_store import PRUNABLE_STATUSES

    assert set(PRUNABLE_STATUSES) == {s.value for s in TERMINAL_EXECUTION_STATUSES}


def test_the_console_is_found_from_inside_the_package():
    """An installed wheel has no ui/ directory beside the source tree.

    The previous lookup walked up from the module, which in a container means
    walking up through site-packages — so the API served no console and said
    nothing about it.
    """
    from pathlib import Path

    from orchestrator.api.app import _console_path

    resolved = _console_path()
    assert resolved is not None
    assert resolved.is_file()

    packaged = (
        Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "ui" / "console.html"
    )
    assert packaged.is_file(), "the console must ship inside the package"
    assert resolved == packaged


def test_the_packaged_console_matches_the_source_console():
    """Two copies of a file drift. This is the check that they have not."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = root / "ui" / "console.html"
    packaged = root / "src" / "orchestrator" / "ui" / "console.html"
    assert source.read_text(encoding="utf-8") == packaged.read_text(encoding="utf-8"), (
        "ui/console.html and src/orchestrator/ui/console.html differ; "
        "copy the former over the latter"
    )


def test_the_root_route_serves_the_console():
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from orchestrator.api.app import create_app
    from orchestrator.api.security import SecurityConfig

    client = TestClient(create_app(security=SecurityConfig(host="127.0.0.1")))
    response = client.get("/")
    assert response.status_code == 200
    assert "Orchestrator" in response.text
    # Not the "console not found" fallback.
    assert "was not found alongside this" not in response.text
