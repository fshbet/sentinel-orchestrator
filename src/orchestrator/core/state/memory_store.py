"""In-memory state store.

Used by tests and by ephemeral embedded usage. It serialises through the same
JSON representation as the durable store so a test cannot accidentally pass by
sharing mutable objects with the engine.
"""

from __future__ import annotations

import asyncio

# `builtins` is imported because the registries below expose a public
# `list()` method, which shadows the builtin inside their own class body.
# `-> builtins.list[X]` is the annotation that keeps the method name.
import builtins
import json
from collections.abc import Sequence
from typing import Any

from ...errors import NotFound
from ..domain.enums import ExecutionStatus
from ..domain.models import AuditEvent, Execution
from ..domain.serde import utcnow
from .store import ConcurrentModification, ExecutionSummary, StateStore


class InMemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._documents: dict[str, str] = {}
        self._audit: dict[str, list[AuditEvent]] = {}
        self._operations: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def create(self, execution: Execution) -> Execution:
        async with self._lock:
            if execution.id in self._documents:
                raise ConcurrentModification(execution.id, 0, execution.revision)
            self._documents[execution.id] = json.dumps(execution.to_dict())
            return execution

    async def get(self, execution_id: str) -> Execution:
        async with self._lock:
            document = self._documents.get(execution_id)
            if document is None:
                raise NotFound(f"execution {execution_id} not found", id=execution_id)
            return Execution.from_dict(json.loads(document))

    async def save(
        self, execution: Execution, *, expected_revision: int | None = None
    ) -> Execution:
        async with self._lock:
            document = self._documents.get(execution.id)
            if document is None:
                raise NotFound(f"execution {execution.id} not found", id=execution.id)
            stored = int(json.loads(document)["revision"])
            if expected_revision is not None and stored != expected_revision:
                raise ConcurrentModification(execution.id, expected_revision, stored)
            execution.revision = stored + 1
            execution.updated_at = utcnow()
            self._documents[execution.id] = json.dumps(execution.to_dict())
            return execution

    async def list(
        self,
        *,
        status: ExecutionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionSummary]:
        async with self._lock:
            executions = [
                Execution.from_dict(json.loads(d)) for d in self._documents.values()
            ]
        executions.sort(key=lambda e: (e.created_at, e.id), reverse=True)
        if status is not None:
            executions = [e for e in executions if e.status is status]
        window = executions[offset : offset + limit]
        return [
            ExecutionSummary(
                id=e.id,
                objective=e.objective,
                status=e.status,
                created_at=e.created_at,
                updated_at=e.updated_at,
                revision=e.revision,
            )
            for e in window
        ]

    async def delete(self, execution_id: str) -> None:
        async with self._lock:
            self._documents.pop(execution_id, None)
            self._audit.pop(execution_id, None)

    async def append_audit(self, events: Sequence[AuditEvent]) -> None:
        async with self._lock:
            for event in events:
                bucket = self._audit.setdefault(event.execution_id, [])
                event.sequence = len(bucket) + 1
                bucket.append(event)

    async def audit(
        self, execution_id: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> builtins.list[AuditEvent]:
        async with self._lock:
            bucket = self._audit.get(execution_id, [])
            return [e for e in bucket if e.sequence > after_sequence][:limit]

    async def record_operation(self, key: str, result: Any) -> tuple[bool, Any]:
        async with self._lock:
            if key in self._operations:
                return False, self._operations[key]
            self._operations[key] = result
            return True, result

    async def lookup_operation(self, key: str) -> Any | None:
        async with self._lock:
            return self._operations.get(key)
