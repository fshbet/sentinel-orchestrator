"""The evaluation harness.

The thing worth testing about a regression suite is not that it passes — it is
that it would *fail* if the platform regressed. A suite that always reports
green is indistinguishable from one that runs nothing, and the second is what
you get by accident.

So most of these deliberately invert an expectation and assert the harness
catches it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.evaluation import CASES, run_suite
from orchestrator.evaluation.cases import (
    SECURITY_SUITES,
    EvaluationCase,
    Severity,
    Suite,
)
from orchestrator.evaluation.runner import (
    ERROR,
    FAIL,
    PASS,
    resolve_suites,
    run_case,
    write_reports,
)


# --------------------------------------------------------------------------
# The shipped library
# --------------------------------------------------------------------------


def test_the_suite_has_at_least_twenty_cases():
    assert len(CASES) >= 20, f"only {len(CASES)} cases"


def test_every_suite_is_represented():
    covered = {case.suite for case in CASES}
    missing = set(Suite) - covered
    assert not missing, f"no cases for {[s.value for s in missing]}"


def test_case_ids_are_unique():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_every_case_is_described_and_versioned():
    for case in CASES:
        assert case.description, case.id
        assert case.version >= 1, case.id


def test_normal_adversarial_high_risk_and_recovery_are_all_covered():
    """The four scenario kinds the suite is supposed to span."""
    suites = {case.suite for case in CASES}
    assert Suite.PLANNING in suites            # normal
    assert Suite.INJECTION in suites           # adversarial
    assert Suite.POLICY in suites              # high-risk
    assert Suite.RECOVERY in suites            # recovery

    severities = {case.severity for case in CASES}
    assert Severity.CRITICAL in severities
    assert len([c for c in CASES if c.severity is Severity.CRITICAL]) >= 8


def test_the_whole_shipped_suite_passes():
    report = run_suite()
    failures = [(r.case.id, r.outcome, r.detail) for r in report.results
                if r.outcome != PASS]
    assert not failures, failures
    assert report.ok is True


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_security_suites_are_gating_and_quality_suites_are_not():
    for case in CASES:
        if case.suite in SECURITY_SUITES:
            assert case.gating is True, case.id

    non_gating = [c for c in CASES
                  if c.suite not in SECURITY_SUITES
                  and c.severity is not Severity.CRITICAL]
    assert non_gating, "every case is gating; the quality score would be vacuous"
    for case in non_gating:
        assert case.gating is False, case.id


def test_a_critical_case_gates_even_outside_a_security_suite():
    case = EvaluationCase(
        id="c", version=1, suite=Suite.PLANNING, severity=Severity.CRITICAL,
        description="critical planning case",
    )
    assert case.gating is True


def test_suite_groups_resolve():
    assert resolve_suites("security") == SECURITY_SUITES
    assert resolve_suites("policy") == frozenset({Suite.POLICY})
    assert resolve_suites("all") == frozenset(Suite)
    assert resolve_suites(Suite.EGRESS) == frozenset({Suite.EGRESS})
    assert SECURITY_SUITES & resolve_suites("quality") == frozenset()


def test_an_unknown_suite_lists_the_real_options():
    with pytest.raises(ValueError) as exc:
        resolve_suites("nonsense")
    assert "planning" in str(exc.value)
    assert "security" in str(exc.value)


# --------------------------------------------------------------------------
# The harness detects regressions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", [
    EvaluationCase(
        id="inverted-metadata", version=1, suite=Suite.EGRESS,
        severity=Severity.CRITICAL,
        description="asserts the metadata endpoint is reachable",
        egress_url="http://169.254.169.254/latest/",
        egress_policy={"allowed_hosts": ["169.254.169.254"], "allow_http": True,
                       "allow_link_local": True},
        expect_egress_allowed=True,
    ),
    EvaluationCase(
        id="inverted-policy", version=1, suite=Suite.POLICY,
        severity=Severity.CRITICAL,
        description="asserts an ungranted tool is permitted",
        context={"profile": "production"},
        granted_permissions=("fs.read",),
        expect_policy_allowed=True,
    ),
    EvaluationCase(
        id="inverted-validation", version=1, suite=Suite.VALIDATION,
        severity=Severity.HIGH,
        description="asserts empty output passes a non-empty check",
        validator="non_empty", validator_output="",
        expect_validation_passed=True,
    ),
    EvaluationCase(
        id="inverted-tool-surface", version=1, suite=Suite.TOOL_SELECTION,
        severity=Severity.CRITICAL,
        description="asserts a write tool exists under a read-only policy",
        egress_policy={"allowed_hosts": ["example.com"], "allowed_methods": ["GET"]},
        allowed_tools=("http.send",),
    ),
    EvaluationCase(
        id="inverted-plan", version=1, suite=Suite.PLANNING,
        severity=Severity.HIGH,
        description="asserts the wrong task count",
        model_script={"tasks": [{"name": "one", "depends_on": []}]},
        expect_task_count=5,
    ),
])
def test_an_inverted_expectation_fails(case):
    """If these passed, the harness would be checking nothing."""
    result = run_case(case)
    assert result.outcome == FAIL, (case.id, result.outcome, result.detail)
    assert result.detail, "a failure must say what went wrong"


def test_an_inverted_security_case_blocks_the_release():
    case = EvaluationCase(
        id="inverted", version=1, suite=Suite.EGRESS, severity=Severity.CRITICAL,
        description="asserts an empty allowlist permits everything",
        egress_url="https://anything.test/", egress_policy={"allowed_hosts": []},
        expect_egress_allowed=True,
    )
    report = run_suite(cases=[case])
    assert report.ok is False
    assert report.blocking_failures


def test_a_runner_exception_is_an_error_not_a_pass():
    """A case that could not run did not pass."""
    case = EvaluationCase(
        id="broken", version=1, suite=Suite.VALIDATION, severity=Severity.LOW,
        description="names a validator that does not exist",
        validator="no_such_validator_exists", validator_output="x",
        expect_validation_passed=True,
    )
    assert run_case(case).outcome == ERROR


def test_errors_count_against_the_quality_score():
    """Otherwise a harness that breaks reports a perfect score."""
    broken = EvaluationCase(
        id="broken", version=1, suite=Suite.VALIDATION, severity=Severity.LOW,
        description="unrunnable", validator="nope", validator_output="x",
        expect_validation_passed=True,
    )
    report = run_suite(cases=[broken])
    assert report.errors == 1
    assert report.quality_score == 0.0


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


def test_the_json_report_is_machine_readable():
    report = run_suite("security")
    payload = json.loads(report.to_json())
    assert payload["totals"]["cases"] > 0
    assert payload["gating_ok"] is True
    assert "by_suite" in payload
    for entry in payload["results"]:
        assert {"id", "suite", "severity", "outcome", "gating"} <= set(entry)


def test_the_markdown_report_states_what_it_does_not_prove():
    """The claim this harness must never be read as making."""
    markdown = run_suite().to_markdown()
    assert "does not prove" in markdown
    assert "AI output is correct" in markdown
    assert "scripted provider" in markdown


def test_both_reports_are_written(tmp_path):
    written = write_reports(run_suite("security"), tmp_path / "report")
    assert Path(written["json"]).is_file()
    assert Path(written["markdown"]).is_file()
    assert json.loads(Path(written["json"]).read_text(encoding="utf-8"))
    assert "# Evaluation report" in Path(written["markdown"]).read_text(encoding="utf-8")


def test_a_blocking_failure_is_named_in_the_markdown():
    case = EvaluationCase(
        id="will-fail", version=1, suite=Suite.POLICY, severity=Severity.CRITICAL,
        description="inverted", context={"profile": "production"},
        granted_permissions=("fs.read",), expect_policy_allowed=True,
    )
    markdown = run_suite(cases=[case]).to_markdown()
    assert "Blocking failures" in markdown
    assert "will-fail" in markdown


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_running_the_suite_twice_gives_the_same_outcomes():
    """A flaky regression suite is one that will eventually be ignored."""
    first = {r.case.id: r.outcome for r in run_suite().results}
    second = {r.case.id: r.outcome for r in run_suite().results}
    assert first == second


def test_no_case_reaches_the_network():
    """Every case must be runnable offline, or CI depends on the internet.

    Loopback is allowed through: asyncio's event loop builds a self-pipe from
    a local socket pair on Windows, so refusing every connect() would flag the
    interpreter rather than the suite. What matters is that nothing leaves the
    machine.
    """
    import ipaddress
    import socket

    original = socket.socket.connect
    attempted: list[str] = []

    def guard(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host in ("localhost", "")
        if not local:
            attempted.append(str(address))
            raise AssertionError(f"a case tried to reach {address}")
        return original(self, address)

    socket.socket.connect = guard
    try:
        report = run_suite()
    finally:
        socket.socket.connect = original

    assert not attempted, f"cases reached the network: {attempted}"
    assert report.errors == 0, [
        (r.case.id, r.detail) for r in report.results if r.outcome == ERROR
    ]
    assert report.ok is True
