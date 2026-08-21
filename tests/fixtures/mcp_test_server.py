"""A real, minimal MCP server over stdio, used to exercise the client.

It is a genuine protocol implementation (initialize, tools/list with
pagination, tools/call, ping, resources) rather than a stub, so the client
tests exercise real framing, not a mock.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "echo",
        "description": "Return the text you were given.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "delete_everything",
        "description": "Destroy all records permanently.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"destructiveHint": True, "openWorldHint": True},
    },
    {
        "name": "boom",
        "description": "Always fails, for error-path testing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(request_id, payload):
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def error(request_id, code, message):
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message):
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": True}, "resources": {}},
                "serverInfo": {"name": "test-server", "version": "1.0.0"},
                "instructions": "Test server.",
            },
        )
    elif method == "notifications/initialized":
        pass
    elif method == "ping":
        result(request_id, {})
    elif method == "tools/list":
        # Deliberately paginate to exercise cursor handling.
        cursor = params.get("cursor")
        if cursor is None:
            result(request_id, {"tools": TOOLS[:2], "nextCursor": "page2"})
        else:
            result(request_id, {"tools": TOOLS[2:]})
    elif method == "resources/list":
        result(request_id, {"resources": [{"uri": "test://doc", "name": "doc"}]})
    elif method == "resources/read":
        result(
            request_id,
            {"contents": [{"uri": params.get("uri"), "text": "resource body"}]},
        )
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            result(request_id, {"content": [{"type": "text", "text": arguments.get("text", "")}]})
        elif name == "add":
            total = float(arguments.get("a", 0)) + float(arguments.get("b", 0))
            result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(total)}],
                    "structuredContent": {"sum": total},
                },
            )
        elif name == "boom":
            result(
                request_id,
                {"content": [{"type": "text", "text": "tool failed on purpose"}], "isError": True},
            )
        elif name == "delete_everything":
            result(request_id, {"content": [{"type": "text", "text": "gone"}]})
        else:
            error(request_id, -32601, f"unknown tool {name}")
    elif method == "notifications/cancelled":
        pass
    elif request_id is not None:
        error(request_id, -32601, f"unknown method {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            print(f"server error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
