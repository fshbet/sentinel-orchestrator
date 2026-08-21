"""Unified tool registry and executor.

Every callable capability reaches an agent through this one path, whatever its
origin: builtin, native, plugin, MCP, or adapter (spec section 14). That is what
makes permissions, timeouts, retries, idempotency, and audit uniform instead of
per-integration.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol, Sequence

from ..core.domain.enums import ToolSource
from ..core.domain.ids import deterministic_id
from ..core.domain.models import ToolCall, ToolResult, ToolSpec
from ..core.policy.engine import PermissionScope, PolicyEngine
from ..core.policy.risk import RiskEngine
from ..errors import (
    OrchestratorError,
    PermissionDenied,
    ToolError,
    ToolNotFound,
    ToolTimeout,
)
from ..observability.audit import AuditLog, EventType
from . import permissions as perms


@dataclass
class ToolContext:
    """Everything a handler may need, and nothing it may not."""

    execution_id: str = ""
    task_id: str | None = None
    agent_id: str | None = None
    scope: PermissionScope = field(default_factory=PermissionScope)
    workspace: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[dict[str, Any], ToolContext], Any]


class OperationLog(Protocol):
    """Minimal slice of the state store used for idempotency."""

    async def record_operation(self, key: str, result: Any) -> tuple[bool, Any]: ...
    async def lookup_operation(self, key: str) -> Any | None: ...


@dataclass
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    def __init__(
        self,
        *,
        policy: PolicyEngine | None = None,
        audit: AuditLog | None = None,
        operations: OperationLog | None = None,
        metrics=None,
    ) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self.policy = policy
        self.audit = audit
        self.operations = operations
        self.metrics = metrics
        self._risk = RiskEngine()

    def _record(self, method: str, *args, **kwargs) -> None:
        """Record a metric without ever failing the tool call."""
        if self.metrics is None:
            return
        try:
            getattr(self.metrics, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001 - deliberate: metrics never fail work
            pass

    def _metric_tool_id(self, tool_id: str) -> str:
        """A label-safe tool id.

        Tool ids from the native registry are bounded and safe. A plugin or an
        MCP server can register anything, and an unbounded label is how a
        metrics backend falls over — so anything unrecognised collapses to a
        single "other" series rather than creating its own.
        """
        if tool_id in self._tools and self._tools[tool_id].spec.source.value == "native":
            return tool_id
        return tool_id if tool_id in self._tools and len(tool_id) <= 64             and all(c.isalnum() or c in "._-:/" for c in tool_id) else "other"

    # -- registration ------------------------------------------------------

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        replace: bool = True,
    ) -> ToolSpec:
        if not spec.id:
            raise ToolError("tool requires an id")
        if spec.id in self._tools and not replace:
            raise ToolError(f"tool {spec.id} is already registered", tool_id=spec.id)
        self._tools[spec.id] = RegisteredTool(spec=spec, handler=handler)
        return spec

    def register_many(
        self, entries: Iterable[tuple[ToolSpec, ToolHandler]]
    ) -> list[ToolSpec]:
        return [self.register(spec, handler) for spec, handler in entries]

    def unregister(self, tool_id: str) -> None:
        self._tools.pop(tool_id, None)

    def unregister_source(self, source: ToolSource, source_ref: str | None = None) -> int:
        """Drop every tool from a source, e.g. when an MCP server disconnects."""
        doomed = [
            tool_id
            for tool_id, entry in self._tools.items()
            if entry.spec.source is source
            and (source_ref is None or entry.spec.source_ref == source_ref)
        ]
        for tool_id in doomed:
            del self._tools[tool_id]
        return len(doomed)

    # -- lookup ------------------------------------------------------------

    def get(self, tool_id: str) -> ToolSpec:
        entry = self._tools.get(tool_id)
        if entry is None:
            raise ToolNotFound(f"tool {tool_id} is not registered", tool_id=tool_id)
        return entry.spec

    def has(self, tool_id: str) -> bool:
        return tool_id in self._tools

    def list(self, *, source: ToolSource | None = None) -> list[ToolSpec]:
        specs = [e.spec for e in self._tools.values()]
        if source is not None:
            specs = [s for s in specs if s.source is source]
        return sorted(specs, key=lambda s: s.id)

    def for_scope(self, scope: PermissionScope) -> list[ToolSpec]:
        """Tools an agent is actually allowed to see (spec section 44)."""
        visible = []
        for spec in self.list():
            if not scope.allows_tool(spec.id):
                continue
            if self._outside_server_scope(spec, scope):
                continue
            if perms.missing(spec.permissions, scope.permissions):
                continue
            visible.append(spec)
        return visible

    @staticmethod
    def _outside_server_scope(spec: ToolSpec, scope: PermissionScope) -> bool:
        """Whether an MCP tool belongs to a server this scope was not granted.

        A tool glob such as ``mcp.*`` says which *tools* an agent may use; the
        server allow-list says which *servers* it may reach. Both have to hold,
        otherwise an agent scoped to one server could reach another's tools
        through a broad glob.
        """
        if spec.source is not ToolSource.MCP or not spec.source_ref:
            return False
        if not scope.mcp_servers:
            # No server list means the scope makes no claim about servers, so
            # the tool glob alone governs.
            return False
        return not scope.allows_server(spec.source_ref)

    def schemas(self, tool_ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Tool descriptions in the shape model providers expect."""
        specs = (
            [self.get(tid) for tid in tool_ids] if tool_ids is not None else self.list()
        )
        return [
            {
                "name": spec.id,
                "description": spec.description,
                "input_schema": spec.input_schema
                or {"type": "object", "properties": {}},
            }
            for spec in specs
        ]

    # -- authorisation -----------------------------------------------------

    def authorize(
        self, tool_id: str, context: ToolContext
    ) -> tuple[ToolSpec, Any]:
        """Check scope then policy. Raises ``PermissionDenied`` when refused."""
        spec = self.get(tool_id)

        if context.scope.tools and not context.scope.allows_tool(tool_id):
            self._audit_denial(tool_id, context, "tool is outside the agent scope")
            raise PermissionDenied(
                f"tool {tool_id} is not in the scope granted to this task",
                tool_id=tool_id,
                agent_id=context.agent_id,
            )

        if self._outside_server_scope(spec, context.scope):
            self._audit_denial(
                tool_id,
                context,
                f"MCP server {spec.source_ref} is outside the agent scope",
            )
            raise PermissionDenied(
                f"tool {tool_id} belongs to MCP server {spec.source_ref}, which is"
                " not in the scope granted to this task",
                tool_id=tool_id,
                server=spec.source_ref,
                agent_id=context.agent_id,
            )

        lacking = perms.missing(spec.permissions, context.scope.permissions)
        if lacking:
            self._audit_denial(
                tool_id, context, f"missing permissions: {', '.join(lacking)}"
            )
            raise PermissionDenied(
                f"tool {tool_id} requires permissions not granted: {', '.join(lacking)}",
                tool_id=tool_id,
                missing=lacking,
            )

        decision = None
        if self.policy is not None:
            descriptor = RiskEngine.from_tool(spec)
            decision = self.policy.evaluate(
                descriptor,
                kind="mcp_tool" if spec.source is ToolSource.MCP else "tool",
                subject=tool_id,
                granted_permissions=context.scope.permissions,
            )
            if not decision.allowed:
                self._audit_denial(tool_id, context, decision.reason, decision=decision)
                if decision.requires_approval:
                    raise PermissionDenied(
                        decision.reason,
                        tool_id=tool_id,
                        requires_approval=True,
                        risk=decision.risk.level.value,
                    )
                raise PermissionDenied(decision.reason, tool_id=tool_id)
        return spec, decision

    def _audit_denial(
        self, tool_id: str, context: ToolContext, reason: str, **extra: Any
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            EventType.TOOL_DENIED,
            execution_id=context.execution_id,
            task_id=context.task_id,
            actor=context.agent_id,
            tool_id=tool_id,
            reason=reason,
            **{k: (v.to_dict() if hasattr(v, "to_dict") else v) for k, v in extra.items()},
        )

    # -- execution ---------------------------------------------------------

    async def call(
        self,
        call: ToolCall,
        context: ToolContext,
        *,
        timeout: float | None = None,
    ) -> ToolResult:
        """Authorise, execute with timeout and retries, and record the outcome."""
        started = time.monotonic()
        try:
            spec, _decision = self.authorize(call.tool_id, context)
        except PermissionDenied:
            # A refusal is an outcome worth counting: a spike in denials is
            # either a misconfiguration or an attack, and both need seeing.
            self._record(
                "tool_called", self._metric_tool_id(call.tool_id), "denied", 0.0
            )
            self._record("policy_denied", "tool", "permission")
            raise
        entry = self._tools[call.tool_id]

        idempotency_key = call.idempotency_key
        if idempotency_key is None and spec.idempotent is False:
            idempotency_key = deterministic_id(
                "op",
                context.execution_id,
                context.task_id or "",
                call.tool_id,
                repr(sorted(call.arguments.items())),
            )
        if idempotency_key and self.operations is not None:
            previous = await self.operations.lookup_operation(idempotency_key)
            if previous is not None:
                return ToolResult(
                    call_id=call.id,
                    tool_id=call.tool_id,
                    ok=True,
                    output=previous,
                    duration_ms=0.0,
                )

        if self.audit is not None:
            self.audit.record(
                EventType.TOOL_CALL,
                execution_id=context.execution_id,
                task_id=context.task_id,
                actor=context.agent_id,
                tool_id=call.tool_id,
                call_id=call.id,
                arguments=call.arguments,
            )

        effective_timeout = timeout if timeout is not None else spec.timeout_seconds
        attempts = max(1, spec.max_retries + 1) if spec.idempotent else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                output = await self._invoke(
                    entry.handler, call.arguments, context, effective_timeout
                )
            except asyncio.TimeoutError as exc:
                last_error = ToolTimeout(
                    f"tool {call.tool_id} timed out after {effective_timeout}s",
                    tool_id=call.tool_id,
                    attempt=attempt + 1,
                )
                last_error.__cause__ = exc
            except OrchestratorError as exc:
                last_error = exc
                if isinstance(exc, PermissionDenied):
                    break
            except Exception as exc:  # noqa: BLE001 - handlers are third-party code
                last_error = ToolError(
                    f"tool {call.tool_id} raised {type(exc).__name__}: {exc}",
                    tool_id=call.tool_id,
                    attempt=attempt + 1,
                )
                last_error.__cause__ = exc
            else:
                if idempotency_key and self.operations is not None:
                    await self.operations.record_operation(idempotency_key, output)
                result = ToolResult(
                    call_id=call.id,
                    tool_id=call.tool_id,
                    ok=True,
                    output=output,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                self._audit_result(context, result)
                self._record(
                    "tool_called", self._metric_tool_id(call.tool_id), "ok",
                    result.duration_ms / 1000.0,
                )
                return result

            if attempt + 1 < attempts:
                await asyncio.sleep(min(0.1 * (2**attempt), 2.0))

        assert last_error is not None
        error = (
            last_error.to_dict()
            if isinstance(last_error, OrchestratorError)
            else {"code": "tool_error", "message": str(last_error)}
        )
        result = ToolResult(
            call_id=call.id,
            tool_id=call.tool_id,
            ok=False,
            error=error,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        self._audit_result(context, result)
        outcome = "timeout" if isinstance(last_error, ToolTimeout) else "error"
        self._record(
            "tool_called", self._metric_tool_id(call.tool_id), outcome,
            result.duration_ms / 1000.0,
        )
        return result

    def _audit_result(self, context: ToolContext, result: ToolResult) -> None:
        if self.audit is None:
            return
        self.audit.record(
            EventType.TOOL_RESULT,
            execution_id=context.execution_id,
            task_id=context.task_id,
            actor=context.agent_id,
            tool_id=result.tool_id,
            call_id=result.call_id,
            ok=result.ok,
            duration_ms=round(result.duration_ms, 2),
            error=result.error,
        )

    @staticmethod
    async def _invoke(
        handler: ToolHandler,
        arguments: dict[str, Any],
        context: ToolContext,
        timeout: float,
    ) -> Any:
        result = handler(arguments, context)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout=timeout)
        return result


def tool(
    tool_id: str,
    description: str = "",
    *,
    input_schema: dict[str, Any] | None = None,
    permissions: Sequence[str] = (),
    source: ToolSource = ToolSource.NATIVE,
    **kwargs: Any,
) -> Callable[[ToolHandler], tuple[ToolSpec, ToolHandler]]:
    """Decorator producing a ``(spec, handler)`` pair for registration."""

    def wrap(handler: ToolHandler) -> tuple[ToolSpec, ToolHandler]:
        spec = ToolSpec(
            id=tool_id,
            name=kwargs.pop("name", tool_id),
            description=description or (handler.__doc__ or "").strip(),
            input_schema=input_schema or {"type": "object", "properties": {}},
            permissions=list(permissions),
            source=source,
            **kwargs,
        )
        return spec, handler

    return wrap

