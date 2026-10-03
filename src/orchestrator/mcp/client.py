"""MCP client.

Implements the client half of the Model Context Protocol over the transports in
``transport.py``: version negotiation, capability discovery, tools, resources,
prompts, long-running tasks, pagination, list caching, progress, cancellation,
and health.

Protocol versions are negotiated, not assumed. The client offers the revisions
in ``SUPPORTED_PROTOCOL_VERSIONS`` (newest first) and adopts whatever the server
answers with, so a server on an older or newer revision still works and no
deprecated behaviour is hard-coded (spec sections 15, 16).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import MCPError, MCPProtocolError
from .transport import Transport, TransportInfo, open_transport

# Newest first. The first entry is what the client proposes.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2026-07-28",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

CLIENT_INFO = {"name": "universal-orchestrator", "version": "0.1.0"}


@dataclass
class MCPTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    # Server-declared hints: readOnlyHint, destructiveHint, idempotentHint,
    # openWorldHint. Treated as hints for risk scoring, never as authorisation.
    annotations: dict[str, Any] = field(default_factory=dict)
    title: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPTool:
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            input_schema=data.get("inputSchema") or {},
            output_schema=data.get("outputSchema"),
            annotations=data.get("annotations") or {},
            title=str(data.get("title", "")),
        )


@dataclass
class MCPResource:
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPResource:
        return cls(
            uri=str(data.get("uri", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            mime_type=data.get("mimeType"),
        )


@dataclass
class MCPPrompt:
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPPrompt:
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            arguments=list(data.get("arguments") or []),
        )


@dataclass
class MCPToolResult:
    content: list[dict[str, Any]] = field(default_factory=list)
    structured: Any = None
    is_error: bool = False

    def text(self) -> str:
        parts = []
        for block in self.content:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "resource":
                resource = block.get("resource") or {}
                parts.append(str(resource.get("text") or resource.get("uri") or ""))
        return "\n".join(p for p in parts if p)

    def value(self) -> Any:
        return self.structured if self.structured is not None else self.text()


@dataclass
class _Cached:
    value: Any
    fetched_at: float
    revision: str | None = None


class MCPClient:
    """One connection to one MCP server."""

    def __init__(
        self,
        server_id: str,
        transport: Transport,
        *,
        timeout: float = 60.0,
        list_cache_ttl: float = 300.0,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.server_id = server_id
        self.transport = transport
        self.timeout = timeout
        self.list_cache_ttl = list_cache_ttl
        self.on_progress = on_progress

        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str = ""
        self.initialized = False
        self._cache: dict[str, _Cached] = {}
        self._progress_token = 0
        self.transport.on_notification = self._on_notification

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def connect(
        cls, server_id: str, config: dict[str, Any], **kwargs: Any
    ) -> MCPClient:
        transport = await open_transport(config)
        client = cls(
            server_id, transport, timeout=float(config.get("timeout", 60.0)), **kwargs
        )
        await client.initialize()
        return client

    async def initialize(self) -> dict[str, Any]:
        result = await self.transport.request(
            "initialize",
            {
                "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
                "capabilities": {
                    "roots": {"listChanged": False},
                    "sampling": {},
                    "elicitation": {},
                },
                "clientInfo": CLIENT_INFO,
            },
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise MCPProtocolError(
                "MCP initialize did not return a result object", server=self.server_id
            )
        negotiated = str(result.get("protocolVersion") or SUPPORTED_PROTOCOL_VERSIONS[0])
        self.protocol_version = negotiated
        if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
            # Unknown revision: proceed, but say so rather than pretending.
            self.instructions = (
                f"[client note] server negotiated unrecognised protocol revision"
                f" {negotiated}; using best-effort compatibility.\n"
            )
        self.capabilities = result.get("capabilities") or {}
        self.server_info = result.get("serverInfo") or {}
        self.instructions += str(result.get("instructions") or "")
        if hasattr(self.transport, "protocol_version"):
            self.transport.protocol_version = negotiated  # type: ignore[attr-defined]

        await self.transport.notify("notifications/initialized", {})
        self.initialized = True
        return result

    async def close(self) -> None:
        await self.transport.close()
        self.initialized = False

    # -- capability queries ------------------------------------------------

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def supports_tasks(self) -> bool:
        return "tasks" in self.capabilities

    def info(self) -> TransportInfo:
        return self.transport.info()

    # -- notifications -----------------------------------------------------

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/progress" and self.on_progress is not None:
            self.on_progress(params)
        elif method in (
            "notifications/tools/list_changed",
            "notifications/resources/list_changed",
            "notifications/prompts/list_changed",
        ):
            # Cacheable list responses are invalidated by the change notification.
            key = method.split("/")[1]
            self._cache.pop(key, None)

    # -- paginated listing -------------------------------------------------

    async def _list_all(
        self, method: str, key: str, *, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        cached = self._cache.get(key)
        if (
            use_cache
            and cached is not None
            and (time.monotonic() - cached.fetched_at) < self.list_cache_ttl
        ):
            return list(cached.value)

        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):  # hard bound against a server paginating forever
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = await self.transport.request(method, params, timeout=self.timeout)
            if not isinstance(result, dict):
                break
            items.extend(result.get(key) or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self._cache[key] = _Cached(value=list(items), fetched_at=time.monotonic())
        return items

    async def list_tools(self, *, use_cache: bool = True) -> list[MCPTool]:
        if not self.supports("tools"):
            return []
        raw = await self._list_all("tools/list", "tools", use_cache=use_cache)
        return [MCPTool.from_dict(entry) for entry in raw]

    async def list_resources(self, *, use_cache: bool = True) -> list[MCPResource]:
        if not self.supports("resources"):
            return []
        raw = await self._list_all("resources/list", "resources", use_cache=use_cache)
        return [MCPResource.from_dict(entry) for entry in raw]

    async def list_prompts(self, *, use_cache: bool = True) -> list[MCPPrompt]:
        if not self.supports("prompts"):
            return []
        raw = await self._list_all("prompts/list", "prompts", use_cache=use_cache)
        return [MCPPrompt.from_dict(entry) for entry in raw]

    def invalidate_cache(self) -> None:
        self._cache.clear()

    # -- invocation --------------------------------------------------------

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        track_progress: bool = False,
    ) -> MCPToolResult:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if track_progress:
            self._progress_token += 1
            params["_meta"] = {"progressToken": f"{self.server_id}:{self._progress_token}"}
        result = await self.transport.request(
            "tools/call", params, timeout=timeout or self.timeout
        )
        if not isinstance(result, dict):
            raise MCPProtocolError(
                f"tool {name} returned a non-object result", server=self.server_id
            )
        return MCPToolResult(
            content=list(result.get("content") or []),
            structured=result.get("structuredContent"),
            is_error=bool(result.get("isError", False)),
        )

    async def read_resource(self, uri: str) -> list[dict[str, Any]]:
        result = await self.transport.request(
            "resources/read", {"uri": uri}, timeout=self.timeout
        )
        if not isinstance(result, dict):
            raise MCPProtocolError("resources/read returned a non-object result")
        return list(result.get("contents") or [])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = await self.transport.request(
            "prompts/get",
            {"name": name, "arguments": arguments or {}},
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise MCPProtocolError("prompts/get returned a non-object result")
        return result

    # -- long-running tasks ------------------------------------------------

    async def create_task(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Start a server-side task, for servers advertising the capability."""
        if not self.supports_tasks():
            raise MCPError(
                f"server {self.server_id} does not advertise the tasks capability",
                server=self.server_id,
            )
        result = await self.transport.request(
            "tasks/create",
            {"name": name, "arguments": arguments or {}},
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise MCPProtocolError("tasks/create returned a non-object result")
        return result

    async def get_task(self, task_id: str) -> dict[str, Any]:
        result = await self.transport.request(
            "tasks/get", {"taskId": task_id}, timeout=self.timeout
        )
        if not isinstance(result, dict):
            raise MCPProtocolError("tasks/get returned a non-object result")
        return result

    async def cancel_task(self, task_id: str, *, reason: str = "") -> None:
        await self.transport.notify(
            "notifications/cancelled", {"taskId": task_id, "reason": reason}
        )

    async def await_task(
        self,
        task_id: str,
        *,
        poll_interval: float = 1.0,
        timeout: float = 900.0,
    ) -> dict[str, Any]:
        """Poll a server-side task to completion, honouring an overall timeout."""
        deadline = time.monotonic() + timeout
        while True:
            status = await self.get_task(task_id)
            state = str(status.get("status") or status.get("state") or "").lower()
            if state in ("completed", "succeeded", "failed", "cancelled", "error"):
                return status
            if time.monotonic() > deadline:
                await self.cancel_task(task_id, reason="client timeout")
                raise MCPError(
                    f"MCP task {task_id} did not finish within {timeout}s",
                    server=self.server_id,
                    task_id=task_id,
                )
            await asyncio.sleep(poll_interval)

    # -- health ------------------------------------------------------------

    async def ping(self, *, timeout: float = 10.0) -> float:
        started = time.monotonic()
        await self.transport.request("ping", {}, timeout=timeout)
        return (time.monotonic() - started) * 1000

    async def health(self) -> dict[str, Any]:
        info = self.info()
        report: dict[str, Any] = {
            "server": self.server_id,
            "transport": info.kind,
            "target": info.target,
            "protocol_version": self.protocol_version,
            "initialized": self.initialized,
            "capabilities": sorted(self.capabilities),
            "server_info": self.server_info,
        }
        try:
            report["latency_ms"] = round(await self.ping(), 2)
            report["status"] = "healthy"
        except Exception as exc:  # noqa: BLE001 - health must never raise
            report["status"] = "unreachable"
            report["error"] = str(exc)
        try:
            report["tools"] = [tool.name for tool in await self.list_tools()]
        except Exception as exc:  # noqa: BLE001
            report["tools"] = []
            report.setdefault("error", str(exc))
        return report


async def close_all(clients: Sequence[MCPClient]) -> None:
    await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
