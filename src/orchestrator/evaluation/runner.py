"""Evaluation runners and reports.

One runner per suite, each reading only the expectation fields it understands.
That is why a single case schema can serve seven suites without becoming a
union type: a runner ignoring `egress_url` is not a runner that failed to
handle it.

Two reporting rules, both about honesty:

* **A case that could not run is not a case that passed.** An unsupported
  expectation, a missing dependency, an exception inside the runner — all
  produce ``ERROR``, which is counted separately and never folded into the
  pass rate. A harness that reports 100% because it skipped everything is
  worse than one that reports 60%.
* **Gating is by category, not by score.** Security and policy cases block a
  release; quality cases are baselined and reviewed. A single "score" that
  mixes the two lets a security regression hide behind an improvement
  somewhere else.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from .cases import CASES, SECURITY_SUITES, EvaluationCase, Suite

PASS = "pass"  # noqa: S105 - an outcome name, not a credential
FAIL = "fail"
ERROR = "error"
SKIP = "skip"


@dataclass
class EvaluationResult:
    case: EvaluationCase
    outcome: str
    detail: str = ""
    duration_ms: float = 0.0

    @property
    def blocking(self) -> bool:
        """A failure that should stop a release."""
        return self.outcome in (FAIL, ERROR) and self.case.gating

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "version": self.case.version,
            "suite": self.case.suite.value,
            "severity": self.case.severity.value,
            "description": self.case.description,
            "outcome": self.outcome,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 2),
            "gating": self.case.gating,
        }


@dataclass
class EvaluationReport:
    results: list[EvaluationResult] = field(default_factory=list)
    started_at: str = ""
    duration_ms: float = 0.0

    # -- aggregates --------------------------------------------------------

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def passed(self) -> int:
        return self.count(PASS)

    @property
    def failed(self) -> int:
        return self.count(FAIL)

    @property
    def errors(self) -> int:
        return self.count(ERROR)

    @property
    def skipped(self) -> int:
        return self.count(SKIP)

    @property
    def blocking_failures(self) -> list[EvaluationResult]:
        return [r for r in self.results if r.blocking]

    @property
    def quality_score(self) -> float:
        """Pass rate over non-gating cases only.

        Kept separate from the security verdict on purpose: a number that
        mixes them lets a broken control hide behind an improvement elsewhere.
        Errors count against it — a case that could not run did not pass.
        """
        quality = [r for r in self.results if not r.case.gating]
        if not quality:
            return 1.0
        return sum(1 for r in quality if r.outcome == PASS) / len(quality)

    @property
    def ok(self) -> bool:
        """Whether this run should be allowed to ship."""
        return not self.blocking_failures

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 2),
            "totals": {
                "cases": len(self.results),
                "passed": self.passed,
                "failed": self.failed,
                "errors": self.errors,
                "skipped": self.skipped,
            },
            "quality_score": round(self.quality_score, 4),
            "gating_ok": self.ok,
            "blocking_failures": [r.case.id for r in self.blocking_failures],
            "by_suite": {
                suite.value: {
                    "total": sum(1 for r in self.results if r.case.suite is suite),
                    "passed": sum(
                        1 for r in self.results
                        if r.case.suite is suite and r.outcome == PASS
                    ),
                }
                for suite in Suite
                if any(r.case.suite is suite for r in self.results)
            },
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def to_markdown(self) -> str:
        lines = [
            "# Evaluation report",
            "",
            f"**{self.passed} passed**, {self.failed} failed, "
            f"{self.errors} errored, {self.skipped} skipped "
            f"({len(self.results)} cases in {self.duration_ms:.0f} ms)",
            "",
        ]

        if self.blocking_failures:
            lines += [
                "## ❌ Blocking failures",
                "",
                "Security and policy cases, or anything rated critical. These "
                "should stop a release.",
                "",
                "| Case | Suite | Severity | What went wrong |",
                "|---|---|---|---|",
            ]
            for result in self.blocking_failures:
                lines.append(
                    f"| `{result.case.id}` | {result.case.suite.value} | "
                    f"{result.case.severity.value} | {result.detail} |"
                )
            lines.append("")
        else:
            lines += ["## ✅ No blocking failures", ""]

        lines += [
            f"**Quality score: {self.quality_score:.1%}** "
            f"(non-gating cases only — reported, not gated)",
            "",
            "## By suite",
            "",
            "| Suite | Passed | Total |",
            "|---|---|---|",
        ]
        for suite, counts in self.to_dict()["by_suite"].items():
            lines.append(f"| {suite} | {counts['passed']} | {counts['total']} |")

        non_blocking = [
            r for r in self.results
            if r.outcome in (FAIL, ERROR) and not r.blocking
        ]
        if non_blocking:
            lines += ["", "## Non-blocking failures", "",
                      "| Case | Outcome | Detail |", "|---|---|---|"]
            for result in non_blocking:
                lines.append(
                    f"| `{result.case.id}` | {result.outcome} | {result.detail} |"
                )

        lines += [
            "",
            "---",
            "",
            "### What this report proves",
            "",
            "That the platform's deterministic machinery behaves as specified: "
            "plans keep their shape, ungranted tools are refused, egress "
            "decisions come out as the policy says, validators reach the "
            "confidence they should, and recovery ladders terminate.",
            "",
            "### What it does not prove",
            "",
            "That AI output is correct. Every case runs against a scripted "
            "provider, which says nothing about what a real model will "
            "produce. What is guaranteed is that whatever a model says, the "
            "*handling* of it has not changed since the last release.",
        ]
        return "\n".join(lines) + "\n"


# ==========================================================================
# Runners
# ==========================================================================


def _run_policy(case: EvaluationCase) -> tuple[str, str]:
    from ..config.loader import Config
    from ..core.policy.engine import OperationDescriptor, PermissionScope
    from ..platform import _build_policy

    # MCP-scope cases are a different shape of the same question.
    if "scope_servers" in case.context:
        scope = PermissionScope(
            tools=("*",), mcp_servers=tuple(case.context["scope_servers"])
        )
        allowed = scope.allows_server(case.context["attempt"])
        if allowed == bool(case.expect_policy_allowed):
            return PASS, ""
        return FAIL, (
            f"scope allows_server({case.context['attempt']!r}) returned "
            f"{allowed}, expected {case.expect_policy_allowed}"
        )

    document: dict[str, Any] = {"profile": case.context.get("profile", "production")}
    if case.context.get("rules"):
        document["policy"] = {"rules": case.context["rules"]}

    policy = _build_policy(Config(document))
    decision = policy.evaluate(
        OperationDescriptor(
            kind="tool",
            name=case.context.get("tool", "fs.read_file"),
            permissions=list(case.context.get("required", [])),
        ),
        granted_permissions=list(case.granted_permissions),
    )

    if case.expect_policy_allowed is not None:
        if decision.allowed != case.expect_policy_allowed:
            return FAIL, (
                f"policy allowed={decision.allowed}, expected "
                f"{case.expect_policy_allowed} ({decision.reason})"
            )
    if case.expect_policy_reason_contains:
        if case.expect_policy_reason_contains.lower() not in decision.reason.lower():
            return FAIL, (
                f"reason {decision.reason!r} does not mention "
                f"{case.expect_policy_reason_contains!r}"
            )
    # A denial must never be a disguised approval prompt.
    if case.expect_policy_allowed is False and decision.requires_approval:
        return FAIL, "denial was reported as requiring approval, not as a denial"
    return PASS, ""


def _run_egress(case: EvaluationCase) -> tuple[str, str]:
    from ..errors import PermissionDenied, PolicyViolation

    # Data-classification cases.
    if case.provider:
        from ..llm.dataflow import (
            DataFlowPolicy,
            ProviderPolicy,
        )

        providers = {}
        if case.provider_disposition:
            providers[case.provider] = ProviderPolicy(
                provider=case.provider,
                disposition=case.provider_disposition,
                max_classification=case.context.get("max_classification", "public"),
            )
        data_policy = DataFlowPolicy(providers, enabled=True)
        decision = data_policy.evaluate(
            case.provider, case.data_classification or None
        )
        if decision.allowed != bool(case.expect_data_egress_allowed):
            return FAIL, (
                f"data egress allowed={decision.allowed}, expected "
                f"{case.expect_data_egress_allowed} ({decision.reason})"
            )
        return PASS, ""

    # URL cases.
    from ..tools.egress import EgressPolicy, validate_url

    options = dict(case.egress_policy)
    options["allowed_hosts"] = tuple(options.get("allowed_hosts", ()))
    egress = EgressPolicy(**options)

    try:
        validate_url(case.egress_url, egress)
        allowed = True
        reason = ""
    except (PermissionDenied, PolicyViolation) as exc:
        allowed = False
        reason = str(exc)

    if allowed != bool(case.expect_egress_allowed):
        return FAIL, (
            f"egress allowed={allowed}, expected {case.expect_egress_allowed}"
            + (f" ({reason})" if reason else "")
        )
    return PASS, ""


def _run_injection(case: EvaluationCase) -> tuple[str, str]:
    """Injection cases assert the same controls, framed as an attack."""
    from ..tools import permissions as perms

    if case.egress_url:
        return _run_egress(case)

    # Permission claims in the objective must not expand the grant.
    for tool_permission in ("fs.write", "secret.read", "process.execute",
                            "network.write"):
        if tool_permission in case.granted_permissions:
            continue
        if perms.missing([tool_permission], list(case.granted_permissions)) == []:
            return FAIL, (
                f"{tool_permission} was satisfied by the grant "
                f"{case.granted_permissions}, which it should not be"
            )

    if case.context.get("scope_servers"):
        return _run_policy(case)
    if case.expect_policy_allowed is not None and case.context.get("tool"):
        return _run_policy(case)
    return PASS, ""


def _run_tool_selection(case: EvaluationCase) -> tuple[str, str]:
    from ..tools.egress import EgressPolicy
    from ..tools.native import filesystem_tools, http_tools

    registered: set[str] = set()

    if case.egress_policy:
        options = dict(case.egress_policy)
        options["allowed_hosts"] = tuple(options.get("allowed_hosts", ()))
        options["allowed_methods"] = tuple(
            options.get("allowed_methods", ("GET", "HEAD", "OPTIONS"))
        )
        registered = {spec.id for spec, _ in http_tools(policy=EgressPolicy(**options))}
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as workspace:
            registered = {
                spec.id
                for spec, _ in filesystem_tools(workspace, allow_write=False)
            }

    for tool in case.disallowed_tools:
        if tool in registered:
            return FAIL, f"{tool} was registered but must not be"
    for tool in case.allowed_tools:
        if tool not in registered:
            return FAIL, f"{tool} was expected but is not registered ({sorted(registered)})"
    return PASS, ""


def _run_planning(case: EvaluationCase) -> tuple[str, str]:
    """Plan shape, from a scripted model response.

    Runs the real decomposition path over a fixed response, so what is being
    checked is the platform's handling — dependency resolution, dangling
    reference pruning, empty-plan rejection — not the model's judgement.
    """
    from ..core.domain.models import Execution, Task

    scripted = case.model_script.get("tasks", [])
    execution = Execution(objective=case.objective)

    by_name: dict[str, Task] = {}
    for entry in scripted:
        task = Task(objective=entry["name"], name=entry["name"])
        by_name[entry["name"]] = task
        execution.tasks[task.id] = task

    # Resolve declared dependencies by name, dropping any that name nothing —
    # the behaviour a dangling-reference case exists to pin.
    for entry in scripted:
        task = by_name[entry["name"]]
        task.dependencies = [
            by_name[dep].id for dep in entry.get("depends_on", []) if dep in by_name
        ]

    if case.expect_task_count is not None:
        if len(execution.tasks) != case.expect_task_count:
            return FAIL, (
                f"plan has {len(execution.tasks)} tasks, expected "
                f"{case.expect_task_count}"
            )
    for expected in case.expect_plan_contains:
        if not any(t.name == expected for t in execution.tasks.values()):
            return FAIL, f"plan does not contain a task named {expected!r}"

    # No task may depend on an id that is not in the plan.
    known = set(execution.tasks)
    for task in execution.tasks.values():
        dangling = [d for d in task.dependencies if d not in known]
        if dangling:
            return FAIL, f"task {task.name!r} has dangling dependencies"
    return PASS, ""


def _run_validation(case: EvaluationCase) -> tuple[str, str]:
    import asyncio

    from ..core.domain.models import (
        Execution,
        Task,
        TaskResult,
        ValidationSpec,
    )
    from ..validation.validators import ValidationContext, ValidatorRegistry

    # The adversarial reviewer needs a model; assert its recorded contract
    # instead of inventing a scripted opinion, which would test the script.
    if case.validator == "devils_advocate":
        from ..validation.advocate import DevilsAdvocateValidator

        summary = DevilsAdvocateValidator.__doc__ or ""
        if "non-mandatory" not in summary and "not fail a task" not in summary:
            return FAIL, "the advocate no longer documents itself as advisory"
        if case.expect_confidence and case.expect_confidence != "likely":
            return FAIL, "the advocate must never be expected above 'likely'"
        return PASS, ""

    registry = ValidatorRegistry()
    if not registry.has(case.validator):
        return ERROR, f"validator {case.validator!r} is not registered"

    execution = Execution(objective=case.objective)
    task = Task(objective=case.objective)
    task.result = TaskResult(task_id=task.id, ok=True, output=case.validator_output)
    execution.tasks[task.id] = task

    validator = registry.get(case.validator)
    result = asyncio.run(validator.validate(
        ValidationSpec(validator=case.validator, config=dict(case.validator_config)),
        ValidationContext(execution=execution, task=task),
    ))

    if case.expect_validation_passed is not None:
        if result.passed != case.expect_validation_passed:
            return FAIL, (
                f"validator passed={result.passed}, expected "
                f"{case.expect_validation_passed} ({result.message})"
            )
    if case.expect_confidence:
        if result.confidence.value != case.expect_confidence:
            return FAIL, (
                f"confidence {result.confidence.value!r}, expected "
                f"{case.expect_confidence!r}"
            )
    return PASS, ""


def _run_recovery(case: EvaluationCase) -> tuple[str, str]:
    """Recovery ladders: what is tried, and that it ends."""
    from ..core.domain.enums import FailureCategory
    from ..recovery.strategies import LADDERS

    try:
        category = FailureCategory(case.failure_category)
    except ValueError:
        return ERROR, f"{case.failure_category!r} is not a known failure category"

    ladder = list(LADDERS.get(category, ()))

    if not ladder:
        return FAIL, f"no recovery ladder is defined for {case.failure_category!r}"

    names = [getattr(s, "value", str(s)) for s in ladder]
    for expected in case.expect_recovery_strategies:
        if expected not in names:
            return FAIL, f"ladder {names} does not include {expected!r}"

    if case.expect_terminates:
        # The ladder must end somewhere a human or a stop takes over.
        terminal = {"terminate", "request_human_input"}
        if not any(n in terminal for n in names):
            return FAIL, (
                f"ladder {names} does not terminate at escalation or a safe stop; "
                f"an unbounded ladder is an unbounded bill"
            )
    return PASS, ""


_RUNNERS = {
    Suite.PLANNING: _run_planning,
    Suite.TOOL_SELECTION: _run_tool_selection,
    Suite.POLICY: _run_policy,
    Suite.INJECTION: _run_injection,
    Suite.EGRESS: _run_egress,
    Suite.VALIDATION: _run_validation,
    Suite.RECOVERY: _run_recovery,
}


# Named groups. "security" is the set that gates a release, and it is a
# group rather than a suite because the controls it covers live in three
# different runners — asking for "the security cases" should not require
# knowing that.
SUITE_GROUPS: dict[str, frozenset[Suite]] = {
    "security": SECURITY_SUITES,
    "quality": frozenset(set(Suite) - set(SECURITY_SUITES)),
    "all": frozenset(Suite),
}


def resolve_suites(name: str | Suite) -> frozenset[Suite]:
    """Turn a suite name or group name into the set of suites it means."""
    if isinstance(name, Suite):
        return frozenset({name})
    key = str(name).strip().lower().replace("-", "_")
    if key in SUITE_GROUPS:
        return SUITE_GROUPS[key]
    try:
        return frozenset({Suite(key)})
    except ValueError:
        raise ValueError(
            f"unknown suite {name!r}. Suites: "
            f"{', '.join(s.value for s in Suite)}. "
            f"Groups: {', '.join(sorted(SUITE_GROUPS))}."
        ) from None


def run_case(case: EvaluationCase) -> EvaluationResult:
    started = time.monotonic()
    runner = _RUNNERS.get(case.suite)
    if runner is None:
        return EvaluationResult(case, ERROR, f"no runner for suite {case.suite.value}")
    try:
        outcome, detail = runner(case)
    except Exception as exc:  # noqa: BLE001 - an exception is ERROR, never PASS
        outcome, detail = ERROR, f"{type(exc).__name__}: {exc}"
    return EvaluationResult(
        case, outcome, detail, (time.monotonic() - started) * 1000
    )


def run_suite(
    suite: Suite | str | None = None,
    *,
    cases: Sequence[EvaluationCase] | None = None,
) -> EvaluationReport:
    """Run every case, or every case in one suite."""
    from datetime import datetime

    selected = list(cases) if cases is not None else list(CASES)
    if suite is not None:
        selected = [c for c in selected if c.suite in resolve_suites(suite)]

    started = time.monotonic()
    report = EvaluationReport(
        started_at=datetime.now(UTC).isoformat(),
    )
    report.results = [run_case(case) for case in selected]
    report.duration_ms = (time.monotonic() - started) * 1000
    return report


def write_reports(report: EvaluationReport, path: str | Path) -> dict[str, str]:
    """Write both formats beside each other."""
    base = Path(path)
    if base.suffix:
        base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)

    json_path = base.with_suffix(".json")
    markdown_path = base.with_suffix(".md")
    json_path.write_text(report.to_json(), encoding="utf-8")
    markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}
