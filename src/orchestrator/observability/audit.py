"""Audit trail.

Every decision the orchestrator makes lands here as a structured event, which
is what makes an execution reconstructable after the fact (spec sections 41,
70). Payloads pass through the redactor, so a secret in a tool argument does
not become a permanent record.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from ..core.domain.models import AuditEvent
from .logging import get_logger, redact

_logger = get_logger("audit")


class AuditSink(Protocol):
    async def append_audit(self, events: Sequence[AuditEvent]) -> None: ...


class EventType:
    """Well-known audit event types. Free-form types are also permitted."""

    EXECUTION_CREATED = "execution.created"
    EXECUTION_TRANSITION = "execution.transition"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_FAILED = "execution.failed"
    EXECUTION_CANCELLED = "execution.cancelled"
    EXECUTION_PAUSED = "execution.paused"
    EXECUTION_RESUMED = "execution.resumed"

    PLAN_CREATED = "plan.created"
    PLAN_REVISED = "plan.revised"
    PATTERN_SELECTED = "pattern.selected"

    TASK_TRANSITION = "task.transition"
    TASK_STARTED = "task.started"
    TASK_FINISHED = "task.finished"
    TASK_SKIPPED = "task.skipped"

    AGENT_SELECTED = "agent.selected"
    AGENT_CREATED = "agent.created"
    MODEL_SELECTED = "model.selected"
    MODEL_CALL = "model.call"
    MODEL_FALLBACK = "model.fallback"

    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_DENIED = "tool.denied"

    MCP_DISCOVERED = "mcp.discovered"
    MCP_REGISTERED = "mcp.registered"
    MCP_DENIED = "mcp.denied"
    MCP_CALL = "mcp.call"
    MCP_ERROR = "mcp.error"

    VALIDATION_RESULT = "validation.result"
    GATE_RESULT = "gate.result"

    FAILURE = "failure"
    RECOVERY_SELECTED = "recovery.selected"
    RECOVERY_RESULT = "recovery.result"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"

    POLICY_DECISION = "policy.decision"
    LIMIT_EXCEEDED = "limit.exceeded"
    CONTEXT_COMPACTED = "context.compacted"


class AuditLog:
    """Buffers events and flushes them to the state store."""

    def __init__(self, sink: AuditSink, *, execution_id: str = "") -> None:
        self._sink = sink
        self._execution_id = execution_id
        self._buffer: list[AuditEvent] = []
        self._subscribers: list[Any] = []

    def bind(self, execution_id: str) -> AuditLog:
        clone = AuditLog(self._sink, execution_id=execution_id)
        clone._subscribers = self._subscribers
        return clone

    def subscribe(self, callback: Any) -> None:
        """Register ``callback(event)`` for live observation (CLI, API stream)."""
        self._subscribers.append(callback)

    def record(
        self,
        type: str,
        *,
        task_id: str | None = None,
        actor: str | None = None,
        execution_id: str | None = None,
        **payload: Any,
    ) -> AuditEvent:
        event = AuditEvent(
            execution_id=execution_id or self._execution_id,
            type=type,
            task_id=task_id,
            actor=actor,
            payload=redact(payload),
        )
        self._buffer.append(event)
        _logger.info(type, extra={"context": {"event": type, **event.payload}})
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except Exception:  # noqa: BLE001 - a bad subscriber must not break work
                _logger.warning("audit subscriber raised", exc_info=True)
        return event

    def extend(self, events: Iterable[AuditEvent]) -> None:
        self._buffer.extend(events)

    @property
    def pending(self) -> list[AuditEvent]:
        return list(self._buffer)

    async def flush(self) -> None:
        if not self._buffer:
            return
        events, self._buffer = self._buffer, []
        await self._sink.append_audit(events)


class NullAuditSink:
    """Sink used when persistence is not wanted (dry runs, unit tests)."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append_audit(self, events: Sequence[AuditEvent]) -> None:
        for index, event in enumerate(events, start=len(self.events) + 1):
            event.sequence = index
        self.events.extend(events)


def set_audit_level(level: int) -> None:  # pragma: no cover - convenience
    _logger.setLevel(level)
