"""Metrics — and above all, that they cannot become an exposure route.

A metrics endpoint is scraped by infrastructure that rarely appears in a threat
model. Putting an objective or a prompt into a label both exposes it there and
creates unbounded Prometheus series. These tests exist mostly to prove that
cannot happen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.observability.metrics import (
    ALLOWED_LABELS,
    Metrics,
    UnsafeLabel,
)


# --------------------------------------------------------------------------
# Cardinality and exposure
# --------------------------------------------------------------------------


def test_an_objective_can_never_become_a_label():
    """The exact mistake this module exists to prevent."""
    metrics = Metrics()
    with pytest.raises(UnsafeLabel):
        metrics.counter("x", objective="Read every customer record and summarise")


@pytest.mark.parametrize("value", [
    "an objective with spaces",
    "exe_0mszxbhum250hv9agvj but with a very long tail " * 4,
    "user@example.com",
    "a\nnewline",
    'a "quote"',
    "{injected}",
])
def test_unbounded_or_unsafe_label_values_are_refused(value):
    metrics = Metrics()
    with pytest.raises(UnsafeLabel):
        metrics.counter("orchestrator_test", freeform=value)


def test_a_value_outside_a_closed_set_is_refused():
    """A typo must fail loudly, not create a second series nobody notices."""
    metrics = Metrics()
    metrics.counter("x", status="completed")          # fine
    with pytest.raises(UnsafeLabel):
        metrics.counter("x", status="complete")        # typo
    with pytest.raises(UnsafeLabel):
        metrics.counter("x", outcome="mostly_fine")


def test_the_closed_sets_cover_every_real_status():
    from orchestrator.core.domain.enums import Confidence, ExecutionStatus

    for status in ExecutionStatus:
        assert status.value in ALLOWED_LABELS["status"], status.value
    for confidence in Confidence:
        assert confidence.value in ALLOWED_LABELS["confidence"], confidence.value


def test_series_are_capped_as_a_backstop():
    metrics = Metrics(max_series=20)
    for index in range(200):
        # tool ids are bounded in practice; this simulates the failure anyway.
        metrics.tool_called(f"tool-{index}", "ok")
    rendered = metrics.render()
    assert "orchestrator_metrics_dropped_total" in rendered


def test_nothing_recorded_leaks_free_text_into_the_output():
    metrics = Metrics()
    metrics.execution_finished("completed", "confirmed", 12.5)
    metrics.tool_called("fs.read_file", "ok", 0.2)
    metrics.model_called("ollama/llama3.1-8b", "ok", 3.1)
    metrics.policy_denied("tool", "permission")
    metrics.security_event("authentication")

    rendered = metrics.render()
    for forbidden in ("objective", "prompt", "password", "Bearer", "sk-"):
        assert forbidden not in rendered


# --------------------------------------------------------------------------
# The measurements themselves
# --------------------------------------------------------------------------


def test_execution_outcomes_are_counted_by_status_and_confidence():
    metrics = Metrics()
    metrics.execution_finished("completed", "confirmed", 10)
    metrics.execution_finished("completed", "uncertain", 20)
    metrics.execution_finished("failed", "failed", 5)

    rendered = metrics.render()
    assert 'orchestrator_executions_total{confidence="confirmed",status="completed"} 1' in rendered
    assert 'orchestrator_executions_total{confidence="uncertain",status="completed"} 1' in rendered
    assert 'orchestrator_executions_total{confidence="failed",status="failed"} 1' in rendered


def test_a_histogram_renders_cumulative_buckets_sum_and_count():
    metrics = Metrics()
    for seconds in (0.05, 0.4, 3, 45):
        metrics.observe("orchestrator_test_seconds", seconds)

    rendered = metrics.render()
    assert "orchestrator_test_seconds_count 4" in rendered
    assert "orchestrator_test_seconds_sum" in rendered
    assert 'le="+Inf"' in rendered
    # Cumulative: the +Inf bucket holds everything.
    inf_line = [l for l in rendered.splitlines() if 'le="+Inf"' in l][0]
    assert inf_line.strip().endswith(" 4")


def test_every_recording_method_the_platform_needs_exists():
    metrics = Metrics()
    metrics.execution_finished("completed", "confirmed", 1)
    metrics.tool_called("fs.read_file", "denied")
    metrics.model_called("m", "error", 0.5)
    metrics.approval_waited(3600)
    metrics.policy_denied("tool", "policy")
    metrics.security_event("authorization")
    metrics.budget_exhausted("budget")
    metrics.queue_wait(2.0)

    rendered = metrics.render()
    for name in (
        "orchestrator_executions_total",
        "orchestrator_tool_calls_total",
        "orchestrator_model_calls_total",
        "orchestrator_model_latency_seconds",
        "orchestrator_approval_wait_seconds",
        "orchestrator_policy_denials_total",
        "orchestrator_security_events_total",
        "orchestrator_budget_exhausted_total",
        "orchestrator_queue_wait_seconds",
    ):
        assert name in rendered, name


def test_the_output_is_valid_exposition_format():
    metrics = Metrics()
    metrics.execution_finished("completed", "confirmed", 1.5)
    metrics.tool_called("fs.read_file", "ok", 0.1)

    for line in metrics.render().splitlines():
        if not line or line.startswith("#"):
            continue
        # Every sample line is "name{labels} value" with a numeric value.
        name, _, value = line.rpartition(" ")
        assert name, line
        float(value)


def test_counters_accumulate_and_gauges_replace():
    metrics = Metrics()
    metrics.counter("c_total", outcome="ok")
    metrics.counter("c_total", outcome="ok")
    metrics.gauge("g", 5)
    metrics.gauge("g", 9)

    rendered = metrics.render()
    assert 'c_total{outcome="ok"} 2' in rendered
    assert "g 9" in rendered


def test_recording_is_thread_safe():
    import threading

    metrics = Metrics()

    def work():
        for _ in range(200):
            metrics.tool_called("fs.read_file", "ok", 0.001)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rendered = metrics.render()
    assert 'orchestrator_tool_calls_total{outcome="ok",tool="fs.read_file"} 800' in rendered
