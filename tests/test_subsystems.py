"""Model routing, context management, validation, recovery, and planning."""

from __future__ import annotations

import pytest
from conftest import make_task, run

from orchestrator.context.budget import ContextBudget, estimate_text_tokens
from orchestrator.context.compaction import ContextItem, compact
from orchestrator.context.manager import ContextManager, ContextRequest
from orchestrator.context.memory import MemoryStore
from orchestrator.core.domain.enums import (
    Confidence,
    ContextKind,
    FailureCategory,
    MemoryTier,
    ModelCapability,
    OrchestrationPattern,
    PlanStrategy,
    RecoveryStrategy,
    RiskLevel,
    TaskStatus,
)
from orchestrator.core.domain.models import (
    Execution,
    Failure,
    ModelSpec,
    Requirements,
    ResourceLimits,
    SuccessCriterion,
    TaskResult,
    ValidationResult,
    ValidationSpec,
)
from orchestrator.core.execution.limits import LimitGuard
from orchestrator.core.state.manager import StateManager
from orchestrator.core.state.memory_store import InMemoryStateStore
from orchestrator.errors import ModelError, ModelUnavailable, NoCapableModel
from orchestrator.llm.base import CompletionRequest, Message
from orchestrator.llm.providers.scripted import ScriptedProvider
from orchestrator.llm.routing import ModelRouter, RoutingRequirements
from orchestrator.observability.audit import AuditLog
from orchestrator.planning.decomposition import Planner, PlanningContext
from orchestrator.planning.goal import GoalAnalyzer
from orchestrator.planning.strategy import assess_complexity, choose
from orchestrator.recovery.classification import classify, to_failure
from orchestrator.recovery.engine import RecoveryEngine
from orchestrator.recovery.strategies import RecoveryPolicy, select
from orchestrator.validation.gates import GateRunner, can_complete
from orchestrator.validation.validators import ValidationContext, ValidatorRegistry


# -- model routing ---------------------------------------------------------


def _provider(name, *, capabilities, context_window=8000, priority=100, responses=("ok",)):
    provider = ScriptedProvider(
        list(responses),
        name=name,
        model_id=f"{name}/model",
        capabilities=capabilities,
        context_window=context_window,
    )
    provider.models()[0].priority = priority
    return provider


def test_routing_selects_by_capability_not_by_name():
    router = ModelRouter(
        [
            _provider("small", capabilities=[ModelCapability.TEXT_GENERATION]),
            _provider(
                "capable",
                capabilities=[
                    ModelCapability.TEXT_GENERATION,
                    ModelCapability.TOOL_CALLING,
                ],
            ),
        ]
    )
    chosen = router.select(
        RoutingRequirements(capabilities=[ModelCapability.TOOL_CALLING])
    )
    assert chosen.provider.name == "capable"


def test_routing_respects_the_context_window_requirement():
    router = ModelRouter(
        [
            _provider("short", capabilities=[ModelCapability.TEXT_GENERATION], context_window=4000),
            _provider("long", capabilities=[ModelCapability.TEXT_GENERATION], context_window=200000),
        ]
    )
    chosen = router.select(RoutingRequirements(min_context_tokens=100000))
    assert chosen.provider.name == "long"


def test_routing_raises_when_nothing_is_capable():
    router = ModelRouter([_provider("text", capabilities=[ModelCapability.TEXT_GENERATION])])
    with pytest.raises(NoCapableModel):
        router.select(RoutingRequirements(capabilities=[ModelCapability.VISION]))


def test_routing_falls_back_after_a_model_failure():
    broken = _provider(
        "broken",
        capabilities=[ModelCapability.TEXT_GENERATION],
        priority=1,
        responses=[ModelUnavailable("down")],
    )
    working = _provider(
        "working", capabilities=[ModelCapability.TEXT_GENERATION], priority=2
    )
    router = ModelRouter([broken, working])
    response = run(router.complete(CompletionRequest(messages=[Message("user", "hi")])))
    assert response.provider == "working"
    assert router.status()["broken/model"]["consecutive_failures"] == 1


def test_a_failed_model_is_cooled_off_and_not_reselected():
    broken = _provider(
        "broken",
        capabilities=[ModelCapability.TEXT_GENERATION],
        priority=1,
        responses=[ModelUnavailable("down"), ModelUnavailable("still down")],
    )
    working = _provider("working", capabilities=[ModelCapability.TEXT_GENERATION], priority=2)
    router = ModelRouter([broken, working])

    async def scenario():
        first = await router.complete(CompletionRequest(messages=[Message("user", "a")]))
        second = await router.complete(CompletionRequest(messages=[Message("user", "b")]))
        return first, second

    first, second = run(scenario())
    assert first.provider == "working"
    assert second.provider == "working"
    assert len(broken.calls) == 1  # not tried again while cooling off


def test_a_single_failing_model_reraises_the_original_error():
    """The error type carries the classification the recovery engine needs."""

    router = ModelRouter(
        [
            _provider(
                "a",
                capabilities=[ModelCapability.TEXT_GENERATION],
                responses=[ModelUnavailable("nope")],
            )
        ]
    )
    with pytest.raises(ModelUnavailable):
        run(router.complete(CompletionRequest(messages=[Message("user", "x")])))


def test_every_candidate_failing_reports_what_was_attempted():
    router = ModelRouter(
        [
            _provider(
                "a",
                capabilities=[ModelCapability.TEXT_GENERATION],
                priority=1,
                responses=[ModelError("nope")],
            ),
            _provider(
                "b",
                capabilities=[ModelCapability.TEXT_GENERATION],
                priority=2,
                responses=[ModelError("also nope")],
            ),
        ]
    )
    with pytest.raises(ModelError) as excinfo:
        run(router.complete(CompletionRequest(messages=[Message("user", "x")])))
    assert set(excinfo.value.details["attempted"]) == {"a/model", "b/model"}


# -- context ---------------------------------------------------------------


def test_budget_reserves_output_and_a_safety_margin():
    budget = ContextBudget.for_model(
        ModelSpec(id="m", context_window=10000, max_output_tokens=2000)
    )
    assert budget.reserved_output == 2000
    assert budget.total_input < 8000
    assert budget.fits(1000) and not budget.fits(budget.total_input + 1)


def test_compaction_drops_the_least_relevant_first_and_keeps_pinned():
    items = [
        ContextItem(kind=ContextKind.OBJECTIVE, label="objective", content="o" * 400, relevance=1.0, pinned=True),
        ContextItem(kind=ContextKind.HISTORY, label="old", content="x" * 4000, relevance=0.1),
        ContextItem(kind=ContextKind.RESULT, label="result", content="r" * 400, relevance=0.9),
    ]
    kept, report = compact(items, 300)
    labels = {item.label for item in kept}
    assert "objective" in labels
    assert "old" in report.dropped
    assert report.after_tokens <= 300


def test_truncation_marks_the_gap_rather_than_hiding_it():
    item = ContextItem(label="big", content="a" * 10000, relevance=1.0, pinned=True)
    kept, report = compact([item], 200)
    assert "omitted" in kept[0].content
    assert report.truncated == ["big"]


def test_memory_recall_is_selective():
    memory = MemoryStore()
    memory.remember("deployment window", "Fridays are frozen", tier=MemoryTier.PROJECT)
    memory.remember("unrelated", "the sky is blue", tier=MemoryTier.PROJECT)
    hits = memory.recall("when is the deployment window", limit=1)
    assert len(hits) == 1
    assert hits[0].key == "deployment window"


def test_memory_updates_rather_than_duplicating_a_key():
    memory = MemoryStore()
    memory.remember("k", "first")
    memory.remember("k", "second")
    assert len(memory.all()) == 1
    assert memory.get("k").value == "second"


def test_context_never_exceeds_the_model_window():
    execution = Execution(objective="o" * 200)
    execution.requirements = Requirements(constraints=["c" * 5000])
    task = make_task("t")
    task.objective = "t" * 5000
    execution.tasks[task.id] = task

    manager = ContextManager()
    model = ModelSpec(id="tiny", context_window=2000, max_output_tokens=500)
    built = run(
        manager.build(
            ContextRequest(
                execution=execution,
                task=task,
                history=[Message("user", "h" * 8000)],
            ),
            model,
        )
    )
    assert built.estimated_tokens <= built.budget.total_input
    assert built.report.changed


def test_large_dependency_output_is_referenced_not_inlined():
    execution = Execution(objective="o")
    upstream = make_task("upstream")
    upstream.status = TaskStatus.SUCCEEDED
    upstream.result = TaskResult(task_id=upstream.id, output="x" * 50000, summary="big")
    downstream = make_task("downstream")
    downstream.dependencies = [upstream.id]
    execution.tasks[upstream.id] = upstream
    execution.tasks[downstream.id] = downstream

    items = ContextManager().collect(
        ContextRequest(execution=execution, task=downstream)
    )
    result_items = [i for i in items if i.kind is ContextKind.RESULT]
    assert result_items
    assert "more characters" in result_items[0].content


def test_token_estimation_is_conservative():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("a" * 360) >= 100


# -- validation ------------------------------------------------------------


def _execution_with_result(output):
    execution = Execution(objective="o")
    task = make_task("t")
    task.result = TaskResult(task_id=task.id, output=output, summary=str(output)[:50])
    execution.tasks[task.id] = task
    return execution, task


def test_non_empty_validator_rejects_empty_output():
    execution, task = _execution_with_result("")
    registry = ValidatorRegistry()
    result = run(
        registry.run(
            ValidationSpec(validator="non_empty"),
            ValidationContext(execution=execution, task=task),
        )
    )
    assert result.passed is False and result.confidence is Confidence.FAILED


def test_schema_validator_reports_specific_violations():
    execution, task = _execution_with_result({"name": 5})
    registry = ValidatorRegistry()
    result = run(
        registry.run(
            ValidationSpec(
                validator="json_schema",
                config={
                    "schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name", "age"],
                    }
                },
            ),
            ValidationContext(execution=execution, task=task),
        )
    )
    assert result.passed is False
    assert result.evidence[0].detail["errors"]


def test_pattern_validator_supports_forbidden_patterns():
    execution, task = _execution_with_result("everything is FINE")
    registry = ValidatorRegistry()
    result = run(
        registry.run(
            ValidationSpec(
                validator="pattern",
                config={"pattern": "ERROR", "must_match": False},
            ),
            ValidationContext(execution=execution, task=task),
        )
    )
    assert result.passed is True


def test_noop_validator_passes_but_reports_uncertainty():
    execution, task = _execution_with_result("anything")
    result = run(
        ValidatorRegistry().run(
            ValidationSpec(validator="noop"),
            ValidationContext(execution=execution, task=task),
        )
    )
    assert result.passed is True
    assert result.confidence is Confidence.UNCERTAIN


def test_a_broken_validator_fails_closed():
    registry = ValidatorRegistry()

    class Exploding:
        name = "exploding"

        async def validate(self, spec, context):
            raise RuntimeError("validator defect")

    registry.register(Exploding())
    execution, task = _execution_with_result("x")
    outcome = run(
        GateRunner(registry).run(
            [ValidationSpec(validator="exploding")],
            ValidationContext(execution=execution, task=task),
        )
    )
    assert outcome.passed is False
    assert outcome.results[0].confidence is Confidence.BLOCKED


def test_gate_confidence_is_the_weakest_link():
    execution, task = _execution_with_result("content")
    outcome = run(
        GateRunner(ValidatorRegistry()).run(
            [
                ValidationSpec(validator="non_empty"),
                ValidationSpec(validator="noop"),
            ],
            ValidationContext(execution=execution, task=task),
        )
    )
    assert outcome.passed is True
    assert outcome.confidence is Confidence.UNCERTAIN


def test_optional_failures_do_not_block_but_lower_confidence():
    execution, task = _execution_with_result("content")
    outcome = run(
        GateRunner(ValidatorRegistry()).run(
            [
                ValidationSpec(validator="non_empty"),
                ValidationSpec(
                    validator="pattern",
                    config={"pattern": "impossible"},
                    mandatory=False,
                ),
            ],
            ValidationContext(execution=execution, task=task),
        )
    )
    assert outcome.passed is True
    assert outcome.failed_optional
    assert outcome.confidence is not Confidence.CONFIRMED


def test_a_failed_mandatory_gate_blocks_completion():
    execution = Execution(objective="o")
    task = make_task("t")
    task.status = TaskStatus.SUCCEEDED
    execution.tasks[task.id] = task
    execution.validations.append(
        ValidationResult(validator="check", passed=False, mandatory=True, message="no")
    )
    allowed, reason = can_complete(execution)
    assert allowed is False and "mandatory validations failed" in reason


def test_unfinished_tasks_block_completion():
    execution = Execution(objective="o")
    task = make_task("t")
    execution.tasks[task.id] = task
    allowed, reason = can_complete(execution)
    assert allowed is False and "terminal state" in reason


# -- recovery --------------------------------------------------------------


def test_failures_are_classified_from_structured_codes():
    from orchestrator.errors import (
        ContextOverflow,
        MCPTimeout,
        PermissionDenied,
        ToolError,
    )

    assert classify(MCPTimeout("slow")) is FailureCategory.TRANSIENT
    assert classify(ToolError("broken")) is FailureCategory.TOOL
    assert classify(PermissionDenied("no")) is FailureCategory.PERMISSION
    assert classify(ContextOverflow("too big")) is FailureCategory.CONTEXT
    assert classify(ValueError("unknown")) is FailureCategory.UNKNOWN


def test_permission_failures_escalate_rather_than_working_around():
    failure = Failure(category=FailureCategory.PERMISSION, message="denied")
    choice = select(failure, task=make_task("t"))
    assert choice.strategy is RecoveryStrategy.REQUEST_HUMAN_INPUT


def test_context_overflow_reduces_scope_before_anything_else():
    failure = Failure(category=FailureCategory.CONTEXT, message="too big")
    assert select(failure, task=make_task("t")).strategy is RecoveryStrategy.REDUCE_SCOPE


def test_the_ladder_climbs_and_then_escalates():
    task = make_task("t")
    task.id = "tsk_fixed"
    history: list[Failure] = []
    strategies = []
    for _ in range(5):
        failure = Failure(category=FailureCategory.TRANSIENT, task_id=task.id)
        choice = select(failure, task=task, history=history)
        failure.recovery = choice.strategy
        history.append(failure)
        strategies.append(choice.strategy)
    assert strategies[0] is RecoveryStrategy.RETRY
    assert strategies[-1] is RecoveryStrategy.REQUEST_HUMAN_INPUT


def test_escalation_can_be_disabled_in_favour_of_stopping():
    task = make_task("t")
    task.id = "tsk_fixed"
    policy = RecoveryPolicy(allow_human_escalation=False)
    history: list[Failure] = []
    strategies = []
    for _ in range(4):
        failure = Failure(category=FailureCategory.PERMISSION, task_id=task.id)
        choice = select(failure, task=task, history=history, policy=policy)
        failure.recovery = choice.strategy
        history.append(failure)
        strategies.append(choice.strategy)

    assert RecoveryStrategy.REQUEST_HUMAN_INPUT not in strategies
    assert strategies[-1] is RecoveryStrategy.TERMINATE


def test_objective_level_failures_never_pick_task_scoped_strategies():
    failure = Failure(category=FailureCategory.VALIDATION, task_id=None)
    choice = select(failure, task=None)
    assert choice.strategy in (
        RecoveryStrategy.REPLAN,
        RecoveryStrategy.REQUEST_HUMAN_INPUT,
        RecoveryStrategy.TERMINATE,
    )


def test_recovery_engine_makes_a_failed_task_runnable_again():
    store = InMemoryStateStore()
    manager = StateManager(store, AuditLog(store))
    execution = run(manager.create_execution("o"))
    task = make_task("t")
    task.execution_id = execution.id
    task.status = TaskStatus.RUNNING
    execution.tasks[task.id] = task

    engine = RecoveryEngine(manager)
    outcome = engine.handle_exception(
        execution, task, to_failure(TimeoutError("slow")).message and TimeoutError("slow")
    )
    assert outcome.retry is True
    assert task.status is TaskStatus.READY
    assert execution.failures[0].category is FailureCategory.TRANSIENT


def test_recovery_escalation_creates_a_pending_approval():
    store = InMemoryStateStore()
    manager = StateManager(store, AuditLog(store))
    execution = run(manager.create_execution("o"))
    task = make_task("t")
    task.status = TaskStatus.RUNNING
    execution.tasks[task.id] = task

    from orchestrator.errors import PermissionDenied

    outcome = RecoveryEngine(manager).handle_exception(
        execution, task, PermissionDenied("not allowed")
    )
    assert outcome.escalate is True
    assert execution.pending_approval() is not None
    assert task.status is TaskStatus.WAITING


# -- limits ----------------------------------------------------------------


def test_limits_are_enforced_by_counting_not_by_asking():
    from orchestrator.core.domain.models import Usage
    from orchestrator.errors import ResourceLimitExceeded

    guard = LimitGuard(ResourceLimits(max_model_calls=2))
    guard.add(Usage(model_calls=1))
    assert guard.within_limits()
    guard.add(Usage(model_calls=1))
    assert not guard.within_limits()
    with pytest.raises(ResourceLimitExceeded):
        guard.raise_if_exceeded()
    assert guard.remaining()["model_calls"] == 0


# -- planning --------------------------------------------------------------


def test_a_trivial_objective_gets_a_single_agent():
    decision = choose("What is the capital of France?")
    assert decision.pattern is OrchestrationPattern.SINGLE_AGENT
    assert decision.complexity.band == "trivial"


def test_a_multi_part_objective_becomes_a_graph():
    objective = (
        "Research three candidate approaches, compare them against our constraints, "
        "then produce a recommendation and a migration outline:\n"
        "- gather evidence\n- compare\n- recommend\n- outline"
    )
    decision = choose(objective)
    assert decision.complexity.band in ("moderate", "complex")
    assert decision.pattern in (
        OrchestrationPattern.PARALLEL,
        OrchestrationPattern.DYNAMIC_DAG,
    )


def test_uncertainty_selects_iterative_planning():
    decision = choose("Figure out why the nightly job is unreliable and fix it.")
    assert decision.plan_strategy is PlanStrategy.ITERATIVE


def test_high_risk_adds_an_evaluation_step():
    decision = choose("Do the thing.", risk=RiskLevel.CRITICAL)
    assert decision.use_evaluator is True


def test_forcing_a_pattern_overrides_the_heuristic():
    decision = choose(
        "What is 2 + 2?", force_pattern=OrchestrationPattern.ORCHESTRATOR_WORKER
    )
    assert decision.pattern is OrchestrationPattern.ORCHESTRATOR_WORKER


def test_complexity_assessment_explains_itself():
    assessment = assess_complexity(
        "Compare the options and then iterate until the result is good enough."
    )
    assert assessment.signals
    assert assessment.iterative is True


def test_planner_falls_back_to_a_deterministic_plan_without_a_model():
    execution = Execution(objective="Do A. Do B. Do C.")
    execution.requirements = Requirements(explicit=["Do A", "Do B", "Do C"])
    decision = choose(execution.objective, requirements=execution.requirements)
    plan = run(Planner().plan(execution, decision, PlanningContext()))
    assert plan.tasks
    assert "deterministic" in plan.rationale


def test_planner_rejects_a_plan_containing_a_cycle():
    from orchestrator.errors import InvalidWorkflow
    from orchestrator.llm.providers.scripted import CallableProvider

    def cyclic(request):
        if "decompose an objective" in (request.system or ""):
            return {
                "tasks": [
                    {"key": "a", "name": "a", "objective": "a", "depends_on": ["b"]},
                    {"key": "b", "name": "b", "objective": "b", "depends_on": ["a"]},
                ]
            }
        return {}

    router = ModelRouter([CallableProvider(cyclic)])
    execution = Execution(objective="anything")
    decision = choose(execution.objective)
    with pytest.raises(InvalidWorkflow):
        run(Planner(router=router).plan(execution, decision, PlanningContext()))


def test_planner_drops_dependencies_it_cannot_resolve():
    from orchestrator.llm.providers.scripted import CallableProvider

    def dangling(request):
        if "decompose an objective" in (request.system or ""):
            return {
                "tasks": [
                    {"key": "a", "name": "a", "objective": "a", "depends_on": ["ghost"]}
                ]
            }
        return {}

    router = ModelRouter([CallableProvider(dangling)])
    execution = Execution(objective="anything")
    plan = run(
        Planner(router=router).plan(execution, choose(execution.objective), PlanningContext())
    )
    assert plan.tasks[0].dependencies == []


def test_planner_ignores_capabilities_that_do_not_exist():
    from orchestrator.llm.providers.scripted import CallableProvider

    def invented(request):
        if "decompose an objective" in (request.system or ""):
            return {
                "tasks": [
                    {
                        "key": "a",
                        "name": "a",
                        "objective": "a",
                        "capabilities": ["real", "invented"],
                    }
                ]
            }
        return {}

    router = ModelRouter([CallableProvider(invented)])
    plan = run(
        Planner(router=router).plan(
            Execution(objective="x"),
            choose("x"),
            PlanningContext(available_capabilities=["real"]),
        )
    )
    assert plan.tasks[0].required_capabilities == ["real"]


def test_goal_analysis_separates_assumptions_from_requirements():
    from orchestrator.llm.providers.scripted import CallableProvider

    def analyse(request):
        return {
            "explicit": ["Ship the report"],
            "inferred": ["It should be readable"],
            "assumptions": ["The data is already collected"],
            "success_criteria": [{"description": "A report exists"}],
        }

    analysis = run(GoalAnalyzer(ModelRouter([CallableProvider(analyse)])).analyze("Ship it"))
    requirements = analysis.requirements
    assert requirements.explicit == ["Ship the report"]
    assert "The data is already collected" in requirements.assumptions
    assert "The data is already collected" not in requirements.explicit


def test_goal_analysis_works_without_a_model():
    analysis = run(GoalAnalyzer().analyze("Do a thing. It must finish within an hour."))
    assert analysis.source == "heuristic"
    assert analysis.requirements.explicit
    assert analysis.requirements.success_criteria


def test_success_criteria_survive_into_the_objective_gate():
    execution = Execution(objective="o")
    execution.requirements = Requirements(
        success_criteria=[
            SuccessCriterion(description="must exist", validator="non_empty")
        ]
    )
    task = make_task("t")
    task.status = TaskStatus.SUCCEEDED
    task.result = TaskResult(task_id=task.id, output="content", ok=True)
    execution.tasks[task.id] = task

    outcome = run(
        GateRunner(ValidatorRegistry()).run_for_objective(
            execution, ValidationContext(execution=execution)
        )
    )
    assert outcome.passed is True
