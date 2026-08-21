"""Metrics, in Prometheus text format.

The whole design of this module is one constraint: **labels are bounded, and
nothing that varies per request is ever a label.**

That is not a style preference. A Prometheus series is created per unique
label combination and kept in memory, so a label carrying an objective, a
prompt, an execution id, or a user-supplied string creates unbounded series —
which takes down the metrics backend, not the orchestrator, which is why it is
easy to do by accident and hard to notice until it is somebody else's outage.
It is also a data-exposure route: objectives contain the very information the
data-classification policy exists to control, and a metrics endpoint is
typically scraped by infrastructure that never appears in a threat model.

So every label here comes from a closed set — a status, an outcome, a
severity, a tool id — and ``_check_label`` refuses anything else. High
cardinality belongs in the audit trail, which is access-controlled, redacted,
and queried per execution.

No third-party dependency: this emits the text exposition format directly, in
keeping with the core's standard-library-only rule (ADR-011). A deployment
that prefers OpenTelemetry can scrape this endpoint or wrap ``Metrics`` — the
recording surface is six methods.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

# A label value must look like this. Anything else is rejected rather than
# sanitised: silently rewriting a value produces series nobody can correlate
# back to what happened.
_SAFE_LABEL = re.compile(r"^[a-zA-Z0-9_.:/-]{0,64}$")

# Values that may appear in a label, by label name. Closed sets, so a new
# status has to be added here deliberately.
ALLOWED_LABELS: dict[str, frozenset[str]] = {
    "status": frozenset({
        "created", "planning", "ready", "running", "waiting", "validating",
        "reviewing", "recovering", "pausing", "paused", "cancelling",
        "cancelled", "failed", "completed",
    }),
    "confidence": frozenset({
        "confirmed", "likely", "uncertain", "blocked", "failed", "unknown",
    }),
    "outcome": frozenset({"ok", "error", "denied", "timeout"}),
    "reason": frozenset({
        "policy", "permission", "budget", "validation", "egress",
        "authentication", "authorization", "rate_limit", "payload_too_large",
        "unknown",
    }),
    "severity": frozenset({"critical", "substantive", "minor"}),
    "kind": frozenset({"tool", "mcp_tool", "model", "agent", "workflow"}),
}

# Buckets in seconds. Chosen for the shape of this workload: model calls are
# seconds, approvals are hours, and a linear scale would put everything in one
# bucket at one end or the other.
DURATION_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900, 3600)


class UnsafeLabel(ValueError):
    """A label value that would create unbounded series."""


def _check_label(name: str, value: str) -> str:
    """Reject a label value that is unbounded or unrecognised."""
    text = str(value)
    if not _SAFE_LABEL.match(text):
        raise UnsafeLabel(
            f"label {name}={text!r} is not a safe label value. Labels must come "
            f"from a small closed set; anything per-request belongs in the "
            f"audit trail, not in metrics."
        )
    allowed = ALLOWED_LABELS.get(name)
    if allowed is not None and text not in allowed:
        raise UnsafeLabel(
            f"label {name}={text!r} is not one of the permitted values: "
            f"{', '.join(sorted(allowed))}"
        )
    return text


@dataclass
class _Histogram:
    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    count: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets) + 1)

    def observe(self, value: float) -> None:
        self.total += value
        self.count += 1
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[index] += 1
                return
        self.counts[-1] += 1


class Metrics:
    """Counters, gauges, and histograms with bounded labels.

    Thread-safe: an orchestrator serves concurrent executions and a metrics
    scrape can land mid-update.
    """

    def __init__(self, *, max_series: int = 5000) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple, float] = {}
        self._gauges: dict[tuple, float] = {}
        self._histograms: dict[tuple, _Histogram] = {}
        self._help: dict[str, tuple[str, str]] = {}
        # A backstop. The label allowlist should make this unreachable; if it
        # is ever reached, something is wrong and dropping is better than
        # growing without limit.
        self._max_series = max_series
        self._dropped = 0

    # -- recording ---------------------------------------------------------

    def _key(self, name: str, labels: dict[str, str] | None) -> tuple:
        pairs = tuple(
            sorted((k, _check_label(k, v)) for k, v in (labels or {}).items())
        )
        return (name, pairs)

    def _room(self, store: dict, key: tuple) -> bool:
        if key in store:
            return True
        if len(self._counters) + len(self._gauges) + len(self._histograms) >= self._max_series:
            self._dropped += 1
            return False
        return True

    def counter(self, name: str, *, help: str = "", **labels: str) -> None:
        self.add(name, 1, help=help, **labels)

    def add(self, name: str, value: float, *, help: str = "", **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._help.setdefault(name, (help, "counter"))
            if self._room(self._counters, key):
                self._counters[key] = self._counters.get(key, 0.0) + value

    def gauge(self, name: str, value: float, *, help: str = "", **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._help.setdefault(name, (help, "gauge"))
            if self._room(self._gauges, key):
                self._gauges[key] = value

    def observe(
        self,
        name: str,
        seconds: float,
        *,
        help: str = "",
        buckets: tuple[float, ...] = DURATION_BUCKETS,
        **labels: str,
    ) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._help.setdefault(name, (help, "histogram"))
            if key not in self._histograms:
                if not self._room(self._histograms, key):
                    return
                self._histograms[key] = _Histogram(buckets=buckets)
            self._histograms[key].observe(seconds)

    # -- the recording surface the platform uses ---------------------------

    def execution_finished(self, status: str, confidence: str, seconds: float) -> None:
        self.counter(
            "orchestrator_executions_total",
            help="Executions by final status and confidence.",
            status=status,
            confidence=confidence,
        )
        self.observe(
            "orchestrator_execution_duration_seconds",
            seconds,
            help="Wall time from start to terminal state.",
            status=status,
        )

    def tool_called(self, tool_id: str, outcome: str, seconds: float = 0.0) -> None:
        # A tool id is bounded by the registry, so it is a safe label.
        self.counter(
            "orchestrator_tool_calls_total",
            help="Tool calls by tool and outcome.",
            tool=tool_id,
            outcome=outcome,
        )
        if seconds:
            self.observe(
                "orchestrator_tool_duration_seconds", seconds,
                help="Tool call latency.", tool=tool_id,
            )

    def model_called(self, model_id: str, outcome: str, seconds: float) -> None:
        self.counter(
            "orchestrator_model_calls_total",
            help="Model calls by model and outcome.",
            model=model_id,
            outcome=outcome,
        )
        self.observe(
            "orchestrator_model_latency_seconds", seconds,
            help="Model call latency.", model=model_id,
        )

    def approval_waited(self, seconds: float) -> None:
        self.observe(
            "orchestrator_approval_wait_seconds", seconds,
            help="How long executions waited on a human.",
        )

    def policy_denied(self, kind: str, reason: str) -> None:
        self.counter(
            "orchestrator_policy_denials_total",
            help="Operations refused by policy.",
            kind=kind, reason=reason,
        )

    def security_event(self, reason: str) -> None:
        self.counter(
            "orchestrator_security_events_total",
            help="Authentication, authorization, and egress refusals.",
            reason=reason,
        )

    def budget_exhausted(self, reason: str) -> None:
        self.counter(
            "orchestrator_budget_exhausted_total",
            help="Executions stopped by a resource limit.",
            reason=reason,
        )

    def queue_wait(self, seconds: float) -> None:
        self.observe(
            "orchestrator_queue_wait_seconds", seconds,
            help="Time between an execution being created and starting.",
        )

    # -- exposition --------------------------------------------------------

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            emitted: set[str] = set()

            def header(name: str, kind: str) -> None:
                if name in emitted:
                    return
                emitted.add(name)
                text, declared = self._help.get(name, ("", kind))
                if text:
                    lines.append(f"# HELP {name} {text}")
                lines.append(f"# TYPE {name} {declared or kind}")

            for (name, pairs), value in sorted(self._counters.items()):
                header(name, "counter")
                lines.append(f"{name}{_format_labels(pairs)} {_number(value)}")

            for (name, pairs), value in sorted(self._gauges.items()):
                header(name, "gauge")
                lines.append(f"{name}{_format_labels(pairs)} {_number(value)}")

            for (name, pairs), histogram in sorted(self._histograms.items()):
                header(name, "histogram")
                cumulative = 0
                for index, edge in enumerate(histogram.buckets):
                    cumulative += histogram.counts[index]
                    lines.append(
                        f"{name}_bucket{_format_labels(pairs, le=str(edge))} {cumulative}"
                    )
                cumulative += histogram.counts[-1]
                lines.append(
                    f"{name}_bucket{_format_labels(pairs, le='+Inf')} {cumulative}"
                )
                lines.append(f"{name}_sum{_format_labels(pairs)} {_number(histogram.total)}")
                lines.append(f"{name}_count{_format_labels(pairs)} {histogram.count}")

            if self._dropped:
                lines.append("# HELP orchestrator_metrics_dropped_total Series dropped at the cardinality ceiling.")
                lines.append("# TYPE orchestrator_metrics_dropped_total counter")
                lines.append(f"orchestrator_metrics_dropped_total {self._dropped}")

        return "\n".join(lines) + "\n"


def _format_labels(pairs: Iterable[tuple[str, str]], **extra: str) -> str:
    items = list(pairs) + sorted(extra.items())
    if not items:
        return ""
    rendered = ",".join(f'{key}="{_escape(value)}"' for key, value in items)
    return "{" + rendered + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))
