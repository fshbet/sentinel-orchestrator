"""Storage abstraction.

The orchestration engine talks only to this interface, so the backing store can
move from an embedded database to PostgreSQL or a distributed store without the
engine changing (spec section 82).
"""

from __future__ import annotations

import abc

# `builtins` is imported because StateStore exposes a public `list()`
# method, which shadows the builtin inside its own class body.
# `-> builtins.list[X]` is the annotation that keeps the method name.
import builtins
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..domain.enums import ExecutionStatus
from ..domain.models import AuditEvent, Execution


class ConcurrentModification(Exception):
    """Raised when an optimistic-concurrency check fails on save."""

    def __init__(self, execution_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"execution {execution_id} was modified concurrently "
            f"(expected revision {expected}, found {actual})"
        )
        self.execution_id = execution_id
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class ExecutionSummary:
    id: str
    objective: str
    status: ExecutionStatus
    created_at: datetime
    updated_at: datetime
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "revision": self.revision,
        }


class StateStore(abc.ABC):
    """Authoritative persistence for executions plus their audit trail."""

    @abc.abstractmethod
    async def create(self, execution: Execution) -> Execution:
        """Persist a new execution. Fails if the id already exists."""

    @abc.abstractmethod
    async def get(self, execution_id: str) -> Execution:
        """Load an execution, raising ``NotFound`` when absent."""

    @abc.abstractmethod
    async def save(
        self, execution: Execution, *, expected_revision: int | None = None
    ) -> Execution:
        """Persist changes, bumping ``revision``.

        When ``expected_revision`` is given and does not match the stored
        revision, ``ConcurrentModification`` is raised instead of clobbering.
        """

    @abc.abstractmethod
    async def list(
        self,
        *,
        status: ExecutionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionSummary]:
        """List executions, newest first."""

    @abc.abstractmethod
    async def delete(self, execution_id: str) -> None:
        """Remove an execution and its audit trail."""

    @abc.abstractmethod
    async def append_audit(self, events: Sequence[AuditEvent]) -> None:
        """Append audit events; sequence numbers are assigned by the store."""

    @abc.abstractmethod
    async def audit(
        self, execution_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> builtins.list[AuditEvent]:
        """Read audit events in sequence order."""

    @abc.abstractmethod
    async def record_operation(self, key: str, result: Any) -> tuple[bool, Any]:
        """Claim an idempotency key.

        Returns ``(stored, result)``: ``stored`` is False when the key was
        already present, and ``result`` is whatever was recorded first.

        This is part of the interface rather than an extra the concrete
        stores happen to provide, because ToolRegistry requires it for
        tool idempotency. A store without it loses exactly-once tool
        execution, and nothing else would say so.
        """

    @abc.abstractmethod
    async def lookup_operation(self, key: str) -> Any | None:
        """The result recorded for an idempotency key, or None."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None
