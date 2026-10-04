"""Transport-level behaviour of the MCP client: how it starts servers, how it
cleans up when one will not start, and how it tells a slow server apart from a
broken one.

These are the cases that cost real debugging time, so each test names the wrong
conclusion it prevents rather than the function it calls. Every server here is
a real subprocess speaking real framing unless the test is specifically about
what happens before a process exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import run

from orchestrator.errors import MCPError, MCPProtocolError, MCPTimeout
from orchestrator.mcp import client as client_module
from orchestrator.mcp.client import MCPClient
from orchestrator.mcp.transport import StdioTransport, open_transport


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _config(script: Path, **overrides) -> dict:
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "timeout": 3.0,
    }
    config.update(overrides)
    return config


# A server that answers initialize correctly, so tests can choose what goes
# wrong *after* the handshake.
_GOOD_INITIALIZE = """
    import json, sys
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"protocolVersion": "2025-06-18",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "t", "version": "1"}},
            }) + "\\n")
            sys.stdout.flush()
    """


# --------------------------------------------------------------------------
# Finding the executable
# --------------------------------------------------------------------------


def test_a_command_on_no_path_is_refused_before_a_process_is_started():
    """The error names what was configured, not a bare OS code.

    `[WinError 2] The system cannot find the file specified` reads like a
    missing npm package. It is a missing executable, and the message should
    say which one.
    """
    transport = StdioTransport("definitely-not-a-real-binary-xyz")

    with pytest.raises(MCPError) as caught:
        run(transport.start())

    assert "definitely-not-a-real-binary-xyz" in str(caught.value)
    assert "no such executable on PATH" in str(caught.value)
    assert caught.value.details["command"] == "definitely-not-a-real-binary-xyz"
    # Nothing was spawned, so there is nothing to clean up.
    assert transport._process is None


def test_the_command_is_resolved_before_it_is_spawned(monkeypatch):
    """`npx` must reach `npx.cmd` on Windows without the config saying so.

    create_subprocess_exec reaches CreateProcess, which ignores PATHEXT, so a
    bare name fails there while the same config works on Linux. This pins that
    resolution happens and that what gets spawned is the resolved path.
    """
    import shutil as shutil_module

    from orchestrator.mcp import transport as transport_module

    resolved = str(Path(sys.executable))
    monkeypatch.setattr(transport_module.shutil, "which", lambda cmd, path=None: resolved)
    assert shutil_module.which  # the real one is untouched elsewhere

    spawned: dict = {}

    async def fake_exec(program, *args, **kwargs):
        spawned["program"] = program
        spawned["args"] = args
        raise OSError("not actually spawning anything")

    monkeypatch.setattr(transport_module.asyncio, "create_subprocess_exec", fake_exec)

    transport = StdioTransport("npx", ["-y", "whatever"])
    with pytest.raises(MCPError):
        run(transport.start())

    assert spawned["program"] == resolved, "the resolved path must be what is spawned"
    assert spawned["args"] == ("-y", "whatever")
    # The configured name survives for diagnostics.
    assert transport.command == "npx"
    assert transport.executable == resolved


def test_resolution_uses_the_path_the_child_will_actually_get(monkeypatch):
    """A server that sets its own PATH is resolved against that PATH."""
    from orchestrator.mcp import transport as transport_module

    seen: dict = {}

    def fake_which(cmd, path=None):
        seen["cmd"] = cmd
        seen["path"] = path
        return None

    monkeypatch.setattr(transport_module.shutil, "which", fake_which)

    transport = StdioTransport("tool", env={"PATH": "/opt/custom/bin"})
    with pytest.raises(MCPError):
        run(transport.start())

    assert seen["cmd"] == "tool"
    assert seen["path"] == "/opt/custom/bin"


def test_a_bare_command_found_only_through_path_starts(tmp_path):
    """End to end: the shim problem, reproduced and shown fixed.

    A wrapper is placed in a directory that is only on the child's PATH, and
    the server is launched by its bare name. On Windows the wrapper is a .cmd,
    which is exactly the case CreateProcess cannot resolve on its own.
    """
    server = _script(tmp_path, "srv.py", _GOOD_INITIALIZE)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()

    if os.name == "nt":
        shim = shim_dir / "mcpshim.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{server}" %*\r\n', encoding="utf-8"
        )
    else:
        shim = shim_dir / "mcpshim"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{server}" "$@"\n', encoding="utf-8"
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    path = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    async def scenario():
        client = await MCPClient.connect(
            "shimmed", {"command": "mcpshim", "env": {"PATH": path}, "timeout": 10.0}
        )
        try:
            return client.protocol_version
        finally:
            await client.close()

    assert run(scenario()) == "2025-06-18"


def test_an_absolute_executable_path_still_works(mcp_server_config):
    """The fixture config names sys.executable outright; resolution must not
    break that, since it is the common case in tests and in containers."""

    async def scenario():
        client = await MCPClient.connect("absolute", mcp_server_config)
        try:
            return client.protocol_version, client.transport.executable
        finally:
            await client.close()

    protocol, executable = run(scenario())
    assert protocol
    assert executable is not None


# --------------------------------------------------------------------------
# Cleanup when initialize fails
# --------------------------------------------------------------------------


class _FailingTransport:
    """A transport whose handshake fails, recording whether it was closed."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.closed = False
        self.on_notification = None

    async def request(self, method, params, *, timeout):
        raise self.error

    async def notify(self, method, params):
        return None

    async def close(self):
        self.closed = True

    def info(self):
        from orchestrator.mcp.transport import TransportInfo

        return TransportInfo(kind="fake", target="fake", detail={"stderr_tail": []})


@pytest.mark.parametrize(
    "error",
    [
        MCPTimeout("initialize timed out", method="initialize"),
        MCPProtocolError("initialize returned nonsense"),
        MCPError("MCP server closed the connection"),
        asyncio.CancelledError(),
    ],
    ids=["timeout", "protocol-error", "early-exit", "cancelled"],
)
def test_a_failed_handshake_closes_the_transport(monkeypatch, error):
    """Otherwise the child process and its two pipes outlive the attempt.

    In a CLI run the leak ends when the process does. Under `orchestrator
    serve` it lasts as long as the API, which is the shape that matters.
    """
    fake = _FailingTransport(error)

    async def fake_open(config):
        return fake

    monkeypatch.setattr(client_module, "open_transport", fake_open)

    async def scenario():
        await MCPClient.connect("doomed", {"command": "irrelevant"})

    with pytest.raises(type(error)):
        run(scenario())

    assert fake.closed, "the transport must be closed when initialize fails"


def test_the_original_failure_is_not_replaced_by_a_cleanup_failure(monkeypatch):
    """A close() that itself fails must not hide why the server would not start."""
    fake = _FailingTransport(MCPTimeout("initialize timed out", method="initialize"))

    async def exploding_close():
        raise RuntimeError("close failed too")

    fake.close = exploding_close

    async def fake_open(config):
        return fake

    monkeypatch.setattr(client_module, "open_transport", fake_open)

    async def scenario():
        await MCPClient.connect("doomed", {"command": "irrelevant"})

    with pytest.raises(MCPTimeout):
        run(scenario())


def test_a_real_child_is_reaped_when_the_handshake_times_out(tmp_path, monkeypatch):
    """The failure-injection version of the test above, with a real process."""
    server = _script(
        tmp_path,
        "silent.py",
        """
        import sys, time
        for line in sys.stdin:
            time.sleep(60)
        """,
    )

    captured: dict = {}
    real_open = open_transport

    async def capture(config):
        transport = await real_open(config)
        captured["transport"] = transport
        return transport

    monkeypatch.setattr(client_module, "open_transport", capture)

    async def scenario():
        await MCPClient.connect("silent", _config(server, timeout=1.0))

    with pytest.raises(MCPTimeout):
        run(scenario())

    process = captured["transport"]._process
    assert process is not None
    assert process.returncode is not None, "the child process must have been reaped"


# --------------------------------------------------------------------------
# A broken server is not a slow server
# --------------------------------------------------------------------------


def test_undecodable_frames_are_reported_as_a_protocol_error(tmp_path):
    """Previously this surfaced as "initialize timed out", which sends whoever
    is debugging it after a slow server when the server is not speaking the
    protocol at all."""
    server = _script(
        tmp_path,
        "garbage.py",
        """
        import sys
        sys.stdout.write("this is not json at all\\n")
        sys.stdout.write("<html>certainly not json</html>\\n")
        sys.stdout.flush()
        for line in sys.stdin:
            pass
        """,
    )

    async def scenario():
        await MCPClient.connect("garbage", _config(server, timeout=2.0))

    with pytest.raises(MCPProtocolError) as caught:
        run(scenario())

    message = str(caught.value)
    assert "could not decode" in message
    assert caught.value.details["undecodable_frames"] >= 1
    # A short sample is kept, because "it sent something unparseable" without
    # saying what is only half a diagnostic.
    assert "not json" in caught.value.details["sample"]
    assert len(caught.value.details["sample"]) <= 120


def test_a_genuinely_slow_server_is_still_a_timeout(tmp_path):
    """The counterpart: well-formed framing and no answer is a timeout, and
    must not be relabelled a protocol error."""
    server = _script(
        tmp_path,
        "slow.py",
        """
        import sys, time
        for line in sys.stdin:
            time.sleep(30)
        """,
    )

    async def scenario():
        await MCPClient.connect("slow", _config(server, timeout=1.0))

    with pytest.raises(MCPTimeout) as caught:
        run(scenario())
    assert "timed out" in str(caught.value)
    assert not isinstance(caught.value, MCPProtocolError)


def test_a_server_that_exits_immediately_says_so(tmp_path):
    server = _script(
        tmp_path,
        "crash.py",
        """
        import sys
        sys.stderr.write("boom: config file missing\\n")
        sys.stderr.flush()
        sys.exit(3)
        """,
    )

    async def scenario():
        await MCPClient.connect("crash", _config(server))

    with pytest.raises(MCPError) as caught:
        run(scenario())
    assert "closed the connection" in str(caught.value)


def test_a_clean_disconnect_after_a_good_handshake_is_not_an_error(tmp_path):
    """Closing a healthy client is ordinary, and must not raise."""
    server = _script(tmp_path, "srv.py", _GOOD_INITIALIZE)

    async def scenario():
        client = await MCPClient.connect("clean", _config(server, timeout=5.0))
        assert client.initialized
        await client.close()
        return client.transport._process.returncode

    assert run(scenario()) is not None


# --------------------------------------------------------------------------
# Surfacing what the server said
# --------------------------------------------------------------------------


def test_the_stderr_tail_is_carried_on_the_error(tmp_path):
    """The transport that captured it is closed and dropped on failure, so if
    the error does not carry it the one useful fact is gone."""
    server = _script(
        tmp_path,
        "noisy.py",
        """
        import sys
        sys.stderr.write("fatal: MCP_TOKEN is not set\\n")
        sys.stderr.flush()
        sys.exit(1)
        """,
    )

    async def scenario():
        await MCPClient.connect("noisy", _config(server))

    with pytest.raises(MCPError) as caught:
        run(scenario())

    tail = caught.value.details.get("stderr_tail") or []
    assert any("MCP_TOKEN is not set" in line for line in tail), tail


def test_the_stderr_tail_is_redacted(tmp_path):
    """A server crashing on startup may print its own configuration on the way
    down, and this tail reaches logs and the console.

    The fixture spells its marker `should-never-be-visible`, the same way every
    other credential-shaped fixture in this suite does. CI greps tests/ for
    credential shapes and fails unless each one carries a marker naming it as
    fake, and that gate is the reason a real key cannot quietly become a
    fixture - so the fixture conforms to it rather than the reverse.
    """
    server = _script(
        tmp_path,
        "leaky.py",
        """
        import sys
        sys.stderr.write("using key sk-ant-api03-should-never-be-visible-0123456789\\n")
        sys.stderr.flush()
        sys.exit(1)
        """,
    )

    async def scenario():
        await MCPClient.connect("leaky", _config(server))

    with pytest.raises(MCPError) as caught:
        run(scenario())

    tail = " ".join(caught.value.details.get("stderr_tail") or [])
    assert "should-never-be-visible" not in tail
    assert "redacted" in tail


def test_the_registry_keeps_the_stderr_tail_for_the_console(tmp_path):
    """`orchestrator mcp` reads this, and a failed connect retains no client,
    so the record is the only place it can live."""
    from orchestrator.core.policy.engine import default_policy
    from orchestrator.mcp.registry import MCPRegistry
    from orchestrator.tools.registry import ToolRegistry

    server = _script(
        tmp_path,
        "noisy.py",
        """
        import sys
        sys.stderr.write("fatal: port already in use\\n")
        sys.stderr.flush()
        sys.exit(1)
        """,
    )

    async def scenario():
        registry = MCPRegistry(ToolRegistry(), policy_engine=default_policy())
        registry.configure("noisy", _config(server))
        await registry.connect_all()
        reports = await registry.health()
        await registry.close()
        return reports

    report = run(scenario())[0]
    assert report["status"] == "disconnected"
    assert any("port already in use" in line for line in report["stderr_tail"])
    # The JSON surface the console renders from carries it too.
    assert json.dumps(report)
