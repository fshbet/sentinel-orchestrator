"""MCP transports.

Two transports are implemented natively over JSON-RPC 2.0: stdio (newline
delimited) and streamable HTTP (POST with a JSON or SSE response, carrying a
session id and the negotiated protocol version).

No vendor SDK is required. That keeps MCP support installable with the core and
avoids the platform inheriting an SDK's protocol assumptions (ADR-003).
"""

from __future__ import annotations

import abc
import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import MCPError, MCPProtocolError, MCPTimeout

JSONRPC_VERSION = "2.0"

# Notifications the server may send unsolicited.
NotificationHandler = Callable[[str, dict[str, Any]], None]


@dataclass
class TransportInfo:
    kind: str
    target: str
    session_id: str | None = None
    protocol_version: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class Transport(abc.ABC):
    """A bidirectional JSON-RPC channel to one MCP server."""

    def __init__(self) -> None:
        self.on_notification: NotificationHandler | None = None

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def request(
        self, method: str, params: dict[str, Any] | None, *, timeout: float
    ) -> Any: ...

    @abc.abstractmethod
    async def notify(self, method: str, params: dict[str, Any] | None) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    def info(self) -> TransportInfo: ...

    def _dispatch_notification(self, message: dict[str, Any]) -> None:
        if self.on_notification is None:
            return
        try:
            self.on_notification(message.get("method", ""), message.get("params") or {})
        except Exception:  # noqa: BLE001, S110 - a handler must not break the transport
            pass


class StdioTransport(Transport):
    """Runs an MCP server as a child process and speaks newline-delimited JSON."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        inherit_env: bool = True,
    ) -> None:
        super().__init__()
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.inherit_env = inherit_env
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._closed = False
        self._stderr_tail: list[str] = []

    async def start(self) -> None:
        environment = dict(os.environ) if self.inherit_env else {}
        environment.update(self.env)
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=environment,
            )
        except (OSError, FileNotFoundError) as exc:
            raise MCPError(
                f"could not start MCP server {self.command}: {exc}",
                command=self.command,
            ) from exc
        self._reader_task = asyncio.ensure_future(self._read_loop())
        asyncio.ensure_future(self._drain_stderr())

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        while not self._closed:
            try:
                line = await stream.readline()
            except (asyncio.CancelledError, GeneratorExit):  # pragma: no cover
                raise
            except Exception:  # noqa: BLE001 - broken pipe ends the loop
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            self._handle_message(message)
        self._fail_pending(MCPError("MCP server closed the connection"))

    async def _drain_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        while not self._closed:
            line = await self._process.stderr.readline()
            if not line:
                break
            self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
            del self._stderr_tail[:-50]

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.pop(int(message["id"]), None)
            if future is None or future.done():
                return
            if "error" in message:
                error = message["error"] or {}
                future.set_exception(
                    MCPError(
                        error.get("message", "MCP error"),
                        code=error.get("code"),
                        data=error.get("data"),
                    )
                )
            else:
                future.set_result(message.get("result"))
        elif "method" in message and "id" not in message:
            self._dispatch_notification(message)

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _write(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPError("MCP transport is not started")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def request(
        self, method: str, params: dict[str, Any] | None, *, timeout: float
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write(
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            # Tell the server to stop working on it (spec: cancellation).
            try:
                await self.notify(
                    "notifications/cancelled",
                    {"requestId": request_id, "reason": "client timeout"},
                )
            except Exception:  # noqa: BLE001, S110 - best effort
                pass
            raise MCPTimeout(
                f"MCP request {method} timed out after {timeout}s", method=method
            ) from exc

    async def notify(self, method: str, params: dict[str, Any] | None) -> None:
        await self._write(
            {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}}
        )

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._process is not None:
            if self._process.stdin is not None and not self._process.stdin.is_closing():
                self._process.stdin.close()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:  # pragma: no cover - stubborn child
                self._process.kill()
                await self._process.wait()
        self._fail_pending(MCPError("transport closed"))

    def info(self) -> TransportInfo:
        return TransportInfo(
            kind="stdio",
            target=" ".join([self.command, *self.args]),
            detail={
                "pid": self._process.pid if self._process else None,
                "stderr_tail": self._stderr_tail[-5:],
            },
        )


class StreamableHTTPTransport(Transport):
    """POST-based transport with JSON or SSE responses."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__()
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._next_id = 0
        self._client: Any = None

    def _require_httpx(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise MCPError(
                "HTTP MCP transport requires httpx; install universal-orchestrator[http]"
            ) from exc
        return httpx

    async def start(self) -> None:
        httpx = self._require_httpx()
        self._client = httpx.AsyncClient(timeout=self.timeout)

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def _post(self, payload: dict[str, Any], timeout: float) -> Any:
        if self._client is None:
            await self.start()
        httpx = self._require_httpx()
        try:
            response = await self._client.post(
                self.url, json=payload, headers=self._request_headers(), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise MCPTimeout(
                f"MCP request {payload.get('method')} timed out after {timeout}s",
                method=payload.get("method"),
            ) from exc
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP server unreachable: {exc}", url=self.url) from exc

        session = response.headers.get("Mcp-Session-Id")
        if session:
            self.session_id = session
        if response.status_code == 202:
            return None
        if response.status_code >= 400:
            raise MCPError(
                f"MCP server returned HTTP {response.status_code}",
                status=response.status_code,
                body=response.text[:500],
            )

        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            return self._parse_sse(response.text, payload.get("id"))
        if not response.content:
            return None
        return self._unwrap(response.json(), payload.get("id"))

    def _parse_sse(self, body: str, request_id: Any) -> Any:
        """Read an SSE body, returning the result for our request id."""
        result: Any = None
        for block in body.split("\n\n"):
            data_lines = [
                line[5:].strip() for line in block.splitlines() if line.startswith("data:")
            ]
            if not data_lines:
                continue
            try:
                message = json.loads("".join(data_lines))
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and "method" in message and "id" not in message:
                self._dispatch_notification(message)
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                result = self._unwrap(message, request_id)
        return result

    @staticmethod
    def _unwrap(message: Any, request_id: Any) -> Any:
        if isinstance(message, list):
            for entry in message:
                if isinstance(entry, dict) and entry.get("id") == request_id:
                    message = entry
                    break
            else:  # pragma: no cover - server returned an unrelated batch
                raise MCPProtocolError("no response matched the request id")
        if not isinstance(message, dict):
            raise MCPProtocolError("MCP response was not a JSON-RPC object")
        if "error" in message:
            error = message["error"] or {}
            raise MCPError(
                error.get("message", "MCP error"),
                code=error.get("code"),
                data=error.get("data"),
            )
        return message.get("result")

    async def request(
        self, method: str, params: dict[str, Any] | None, *, timeout: float
    ) -> Any:
        self._next_id += 1
        payload = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self._next_id,
            "method": method,
            "params": params or {},
        }
        return await self._post(payload, timeout)

    async def notify(self, method: str, params: dict[str, Any] | None) -> None:
        await self._post(
            {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}},
            self.timeout,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def info(self) -> TransportInfo:
        return TransportInfo(
            kind="http",
            target=self.url,
            session_id=self.session_id,
            protocol_version=self.protocol_version,
        )


async def open_transport(config: dict[str, Any]) -> Transport:
    """Build a transport from a server configuration block."""
    kind = str(config.get("transport") or ("http" if config.get("url") else "stdio"))
    if kind in ("stdio", "process"):
        command = config.get("command")
        if not command:
            raise MCPError("stdio MCP server configuration requires a command")
        transport: Transport = StdioTransport(
            str(command),
            [str(a) for a in config.get("args", [])],
            env={str(k): str(v) for k, v in (config.get("env") or {}).items()},
            cwd=config.get("cwd"),
        )
    elif kind in ("http", "streamable_http", "sse"):
        url = config.get("url")
        if not url:
            raise MCPError("http MCP server configuration requires a url")
        transport = StreamableHTTPTransport(
            str(url),
            headers={str(k): str(v) for k, v in (config.get("headers") or {}).items()},
            timeout=float(config.get("timeout", 60.0)),
        )
    else:
        raise MCPError(f"unsupported MCP transport {kind}", transport=kind)
    await transport.start()
    return transport
