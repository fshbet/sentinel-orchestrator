"""Expose orchestration capabilities over MCP.

The platform is an MCP client first, but it can also be a server, so another
agent system can start work here, watch it, and read the results (spec section
17).

Control operations - cancelling an execution, answering an approval on a
human's behalf - are not exposed unless the operator explicitly enables them.
Read-only inspection is always available; nothing here can bypass a gate or
mark work complete.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from ..config.loader import load
from ..core.domain.enums import ExecutionStatus
from ..errors import OrchestratorError
from ..platform import Orchestrator

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "universal-orchestrator", "version": "0.1.0"}

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class OrchestrationMCPServer:
    """A JSON-RPC MCP server backed by an ``Orchestrator``."""

    def __init__(self, orchestrator: Orchestrator, *, allow_control: bool = False) -> None:
        self.orchestrator = orchestrator
        self.allow_control = allow_control
        self._handlers: dict[str, Handler] = {}
        self._tools: list[dict[str, Any]] = []
        self._register_tools()

    # -- tool surface ------------------------------------------------------

    def _tool(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: Handler,
        *,
        read_only: bool = True,
        destructive: bool = False,
    ) -> None:
        self._tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": schema,
                "annotations": {
                    "readOnlyHint": read_only,
                    "destructiveHint": destructive,
                    "idempotentHint": read_only,
                    "openWorldHint": False,
                },
            }
        )
        self._handlers[name] = handler

    def _register_tools(self) -> None:
        self._tool(
            "start_execution",
            "Start an orchestrated execution for an objective and return its id. "
            "The execution runs asynchronously; poll get_execution for progress.",
            {
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "context": {"type": "object"},
                    "wait": {
                        "type": "boolean",
                        "description": "Block until the execution stops or waits.",
                    },
                },
                "required": ["objective"],
            },
            self._start_execution,
            read_only=False,
        )
        self._tool(
            "get_execution",
            "Return the status, task graph, validations, and pending approvals of "
            "one execution.",
            {
                "type": "object",
                "properties": {"execution_id": {"type": "string"}},
                "required": ["execution_id"],
            },
            self._get_execution,
        )
        self._tool(
            "list_executions",
            "List recent executions, optionally filtered by status.",
            {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            self._list_executions,
        )
        self._tool(
            "get_artifacts",
            "Return the artifacts an execution produced.",
            {
                "type": "object",
                "properties": {"execution_id": {"type": "string"}},
                "required": ["execution_id"],
            },
            self._get_artifacts,
        )
        self._tool(
            "get_audit",
            "Return the structured audit trail of an execution.",
            {
                "type": "object",
                "properties": {
                    "execution_id": {"type": "string"},
                    "after": {"type": "integer"},
                },
                "required": ["execution_id"],
            },
            self._get_audit,
        )
        self._tool(
            "describe_platform",
            "Report the tools, agents, capabilities, models, and validators this "
            "installation has available.",
            {"type": "object", "properties": {}},
            self._describe,
        )

        if self.allow_control:
            self._tool(
                "respond_to_approval",
                "Answer a pending approval on behalf of an operator and resume the "
                "execution.",
                {
                    "type": "object",
                    "properties": {
                        "execution_id": {"type": "string"},
                        "approval_id": {"type": "string"},
                        "approved": {"type": "boolean"},
                        "response": {"type": "string"},
                    },
                    "required": ["execution_id", "approval_id"],
                },
                self._respond_to_approval,
                read_only=False,
            )
            self._tool(
                "cancel_execution",
                "Cancel a running execution gracefully.",
                {
                    "type": "object",
                    "properties": {
                        "execution_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["execution_id"],
                },
                self._cancel_execution,
                read_only=False,
                destructive=True,
            )

    # -- handlers ----------------------------------------------------------

    async def _start_execution(self, arguments: dict[str, Any]) -> Any:
        objective = str(arguments.get("objective", "")).strip()
        if not objective:
            raise OrchestratorError("objective is required")
        context = arguments.get("context") or {}
        if arguments.get("wait"):
            execution = await self.orchestrator.run(objective, context=context)
        else:
            execution = await self.orchestrator.start(objective, context=context)
            asyncio.ensure_future(self.orchestrator.engine.run(execution.id))
        return self._summary(execution)

    async def _get_execution(self, arguments: dict[str, Any]) -> Any:
        execution = await self.orchestrator.status(str(arguments["execution_id"]))
        payload = self._summary(execution)
        payload["tasks"] = [
            {
                "id": t.id,
                "name": t.name,
                "status": t.status.value,
                "depends_on": t.dependencies,
                "summary": t.result.summary if t.result else "",
            }
            for t in execution.tasks.values()
        ]
        payload["validations"] = [
            {
                "validator": v.validator,
                "passed": v.passed,
                "mandatory": v.mandatory,
                "confidence": v.confidence.value,
                "message": v.message,
            }
            for v in execution.validations
        ]
        return payload

    async def _list_executions(self, arguments: dict[str, Any]) -> Any:
        status = arguments.get("status")
        summaries = await self.orchestrator.list(
            status=ExecutionStatus(status) if status else None,
            limit=int(arguments.get("limit", 25)),
        )
        return [s.to_dict() for s in summaries]

    async def _get_artifacts(self, arguments: dict[str, Any]) -> Any:
        artifacts = await self.orchestrator.artifacts(str(arguments["execution_id"]))
        return [a.to_dict() for a in artifacts]

    async def _get_audit(self, arguments: dict[str, Any]) -> Any:
        events = await self.orchestrator.audit(
            str(arguments["execution_id"]), after=int(arguments.get("after", 0))
        )
        return [e.to_dict() for e in events]

    async def _describe(self, arguments: dict[str, Any]) -> Any:
        return self.orchestrator.describe()

    async def _respond_to_approval(self, arguments: dict[str, Any]) -> Any:
        execution = await self.orchestrator.approve(
            str(arguments["execution_id"]),
            str(arguments["approval_id"]),
            approved=bool(arguments.get("approved", True)),
            response=arguments.get("response"),
            responder="mcp",
        )
        return self._summary(execution)

    async def _cancel_execution(self, arguments: dict[str, Any]) -> Any:
        execution = await self.orchestrator.cancel(
            str(arguments["execution_id"]), reason=str(arguments.get("reason", ""))
        )
        return self._summary(execution)

    @staticmethod
    def _summary(execution) -> dict[str, Any]:
        return {
            "execution_id": execution.id,
            "status": execution.status.value,
            "confidence": execution.confidence.value,
            "objective": execution.objective,
            "summary": execution.summary,
            "pending_approvals": [
                {"id": a.id, "prompt": a.prompt, "reason": a.reason.value}
                for a in execution.approvals
                if a.status.value == "pending"
            ],
            "artifacts": [a.name for a in execution.artifacts],
        }

    # -- protocol ----------------------------------------------------------

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            return _result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": (
                        "Start orchestrated work with start_execution, then poll "
                        "get_execution. Completion is decided by validation gates, "
                        "not by any agent's claim."
                    ),
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": self._tools})
        if method == "tools/call":
            name = str(params.get("name", ""))
            handler = self._handlers.get(name)
            if handler is None:
                return _error(request_id, -32601, f"unknown tool {name}")
            try:
                value = await handler(params.get("arguments") or {})
            except OrchestratorError as exc:
                return _result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": exc.message}],
                        "isError": True,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - report, never crash the server
                return _result(
                    request_id,
                    {
                        "content": [
                            {"type": "text", "text": f"{type(exc).__name__}: {exc}"}
                        ],
                        "isError": True,
                    },
                )
            text = json.dumps(value, indent=2, default=str)
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": value
                    if isinstance(value, dict)
                    else {"result": value},
                },
            )
        if request_id is not None:
            return _error(request_id, -32601, f"unknown method {method}")
        return None


def _result(request_id: Any, payload: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def serve_stdio(
    *,
    config_path: str | None = None,
    allow_control: bool = False,
    orchestrator: Orchestrator | None = None,
) -> None:
    """Run the MCP server on stdin/stdout until the client disconnects."""
    owned = orchestrator is None
    if orchestrator is None:
        config = load(paths=[config_path] if config_path else None)
        orchestrator = await Orchestrator.create(config=config)
    server = OrchestrationMCPServer(orchestrator, allow_control=allow_control)

    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = await server.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, default=str) + "\n")
                sys.stdout.flush()
    finally:
        if owned:
            await orchestrator.close()
