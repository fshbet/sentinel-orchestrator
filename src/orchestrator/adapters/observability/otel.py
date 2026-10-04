"""Optional OpenTelemetry bridge.

Observability is never a hard dependency (spec section 83). If
``opentelemetry-api`` is installed and a tracer is configured, audit events
become spans and counters; if it is not, this module degrades to nothing at all
and the platform behaves identically.
"""

from __future__ import annotations

from typing import Any

from ...core.domain.models import AuditEvent
from ...observability.audit import AuditLog
from ...observability.logging import get_logger

_logger = get_logger("otel")


def available() -> bool:
    try:
        import opentelemetry.trace  # noqa: F401
    except ImportError:
        return False
    return True


class OTelBridge:
    """Turns audit events into spans and counters."""

    def __init__(self, service_name: str = "universal-orchestrator") -> None:
        self.service_name = service_name
        self._tracer = None
        self._counters: dict[str, Any] = {}
        self._enabled = False

        if not available():
            return
        try:
            from opentelemetry import metrics, trace

            self._tracer = trace.get_tracer(service_name)
            self._meter = metrics.get_meter(service_name)
            self._enabled = True
        except Exception as exc:  # noqa: BLE001 - never break on telemetry
            _logger.warning(
                "OpenTelemetry present but unusable",
                extra={"context": {"error": str(exc)}},
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def attach(self, audit: AuditLog) -> None:
        """Subscribe to the audit log so every recorded event is exported."""
        if not self._enabled:
            return
        audit.subscribe(self.on_event)

    def on_event(self, event: AuditEvent) -> None:
        if not self._enabled or self._tracer is None:
            return
        try:
            attributes: dict[str, str | int | float | bool] = {
                "execution.id": event.execution_id,
                "event.type": event.type,
            }
            if event.task_id:
                attributes["task.id"] = event.task_id
            if event.actor:
                attributes["actor"] = event.actor
            for key, value in event.payload.items():
                if isinstance(value, (str, int, float, bool)):
                    attributes[f"payload.{key}"] = value
            with self._tracer.start_as_current_span(event.type, attributes=attributes):
                pass
            self._count(event.type)
        except Exception:  # noqa: BLE001, S110 - telemetry must never raise
            pass

    def _count(self, name: str) -> None:
        counter = self._counters.get(name)
        if counter is None:
            try:
                counter = self._meter.create_counter(
                    f"orchestrator.{name.replace('.', '_')}"
                )
            except Exception:  # noqa: BLE001
                return
            self._counters[name] = counter
        counter.add(1)


def attach_if_available(
    audit: AuditLog, *, service_name: str = "universal-orchestrator"
) -> OTelBridge | None:
    bridge = OTelBridge(service_name)
    if not bridge.enabled:
        return None
    bridge.attach(audit)
    return bridge
