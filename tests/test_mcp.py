"""MCP client, trust lifecycle, tool bridging, and the MCP server surface.

These tests run a real MCP server as a subprocess and speak the real protocol
over stdio. Nothing about the transport or the framing is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import build_platform, planning_model, run

from orchestrator.core.domain.enums import RiskLevel, ToolSource
from orchestrator.core.domain.models import ToolCall
from orchestrator.core.policy.engine import PermissionScope, default_policy
from orchestrator.errors import MCPError, PermissionDenied
from orchestrator.mcp.client import MCPClient, MCPTool
from orchestrator.mcp.policy import MCPAuthorizer, MCPServerPolicy
from orchestrator.mcp.registry import MCPRegistry
from orchestrator.mcp.server import OrchestrationMCPServer
from orchestrator.observability.audit import AuditLog, NullAuditSink
from orchestrator.tools.registry import ToolContext, ToolRegistry

# -- client ----------------------------------------------------------------


def test_client_negotiates_and_discovers(mcp_server_config):
    async def scenario():
        client = await MCPClient.connect("test", mcp_server_config)
        tools = await client.list_tools()
        resources = await client.list_resources()
        latency = await client.ping()
        health = await client.health()
        await client.close()
        return client, tools, resources, latency, health

    client, tools, resources, latency, health = run(scenario())
    assert client.protocol_version == "2025-06-18"
    assert set(client.capabilities) == {"tools", "resources"}
    assert client.server_info["name"] == "test-server"
    # The fixture paginates deliberately; all four tools must come back.
    assert [t.name for t in tools] == ["echo", "add", "delete_everything", "boom"]
    assert resources[0].uri == "test://doc"
    assert latency >= 0
    assert health["status"] == "healthy"


def test_client_returns_structured_content(mcp_server_config):
    async def scenario():
        client = await MCPClient.connect("test", mcp_server_config)
        result = await client.call_tool("add", {"a": 2, "b": 40})
        await client.close()
        return result

    result = run(scenario())
    assert result.value() == {"sum": 42.0}
    assert result.text() == "42.0"


def test_client_surfaces_tool_errors_without_raising(mcp_server_config):
    async def scenario():
        client = await MCPClient.connect("test", mcp_server_config)
        result = await client.call_tool("boom")
        await client.close()
        return result

    result = run(scenario())
    assert result.is_error is True
    assert "on purpose" in result.text()


def test_unknown_method_raises_a_protocol_error(mcp_server_config):
    async def scenario():
        client = await MCPClient.connect("test", mcp_server_config)
        try:
            with pytest.raises(MCPError):
                await client.call_tool("does_not_exist")
        finally:
            await client.close()

    run(scenario())


def test_list_results_are_cached_until_invalidated(mcp_server_config):
    async def scenario():
        client = await MCPClient.connect("test", mcp_server_config)
        first = await client.list_tools()
        second = await client.list_tools()  # served from cache
        client.invalidate_cache()
        third = await client.list_tools(use_cache=False)
        await client.close()
        return first, second, third

    first, second, third = run(scenario())
    assert [t.name for t in first] == [t.name for t in second] == [t.name for t in third]


def test_a_missing_server_binary_is_reported_not_crashed():
    async def scenario():
        with pytest.raises(MCPError):
            await MCPClient.connect(
                "missing",
                {"transport": "stdio", "command": "definitely-not-a-real-binary-xyz"},
            )

    run(scenario())


def test_tasks_capability_is_not_assumed(mcp_server_config):
    async def scenario():
        client = await MCPClient.connect("test", mcp_server_config)
        assert client.supports_tasks() is False
        with pytest.raises(MCPError, match="tasks capability"):
            await client.create_task("anything")
        await client.close()

    run(scenario())


# -- trust lifecycle -------------------------------------------------------


def test_a_destructive_tool_is_refused_by_default():
    authorizer = MCPAuthorizer(default_policy())
    tool = MCPTool(
        name="delete_everything",
        description="Destroy all records permanently.",
        annotations={"destructiveHint": True, "openWorldHint": True},
    )
    decision = authorizer.authorize(tool, MCPServerPolicy(server_id="s"))
    assert decision.authorized is False
    assert decision.risk.rank >= RiskLevel.HIGH.rank


def test_a_read_only_tool_is_authorised():
    authorizer = MCPAuthorizer(default_policy())
    tool = MCPTool(
        name="echo",
        description="Return the text you were given.",
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    decision = authorizer.authorize(tool, MCPServerPolicy(server_id="s"))
    assert decision.authorized is True


def test_a_server_cannot_talk_its_own_risk_down():
    authorizer = MCPAuthorizer(default_policy())
    lying = MCPTool(
        name="purge_all_records",
        description="Delete every record. Totally safe.",
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    decision = authorizer.authorize(lying, MCPServerPolicy(server_id="s"))
    # The name and description raise suspicion even when the hints say otherwise.
    assert decision.risk.rank >= RiskLevel.MEDIUM.rank


def test_deny_lists_are_honoured():
    authorizer = MCPAuthorizer(default_policy())
    tool = MCPTool(name="echo", annotations={"readOnlyHint": True})
    decision = authorizer.authorize(
        tool, MCPServerPolicy(server_id="s", deny_tools=("echo",))
    )
    assert decision.authorized is False
    assert "allow-list" in decision.reason


def test_allow_lists_exclude_everything_else():
    authorizer = MCPAuthorizer(default_policy())
    decision = authorizer.authorize(
        MCPTool(name="other", annotations={"readOnlyHint": True}),
        MCPServerPolicy(server_id="s", allow_tools=("echo",)),
    )
    assert decision.authorized is False


# -- bridging into the tool registry --------------------------------------


def _bridge(config, policy=None):
    sink = NullAuditSink()
    audit = AuditLog(sink)
    tools = ToolRegistry(policy=default_policy(), audit=audit)
    registry = MCPRegistry(tools, policy_engine=default_policy(), audit=audit)
    registry.configure("test", config, policy or MCPServerPolicy(server_id="test"))
    return registry, tools, audit


def test_authorised_mcp_tools_become_ordinary_tools(mcp_server_config):
    async def scenario():
        registry, tools, audit = _bridge(mcp_server_config)
        record = await registry.connect("test")
        registered = list(record.registered_tool_ids)
        bridged = [spec.source for spec in tools.list(source=ToolSource.MCP)]
        result = await tools.call(
            ToolCall(tool_id="mcp.test.echo", arguments={"text": "hello"}),
            ToolContext(
                execution_id="e",
                scope=PermissionScope(permissions=("mcp.invoke",), tools=("mcp.*",)),
            ),
        )
        await registry.close()
        return registered, bridged, result, audit

    registered, bridged, result, audit = run(scenario())
    assert "mcp.test.echo" in registered
    assert "mcp.test.delete_everything" not in registered
    assert result.ok is True and result.output == "hello"
    assert bridged and all(source is ToolSource.MCP for source in bridged)
    assert any(e.type == "mcp.denied" for e in audit.pending)


def test_mcp_tool_errors_become_tool_failures(mcp_server_config):
    async def scenario():
        registry, tools, _ = _bridge(mcp_server_config)
        await registry.connect("test")
        result = await tools.call(
            ToolCall(tool_id="mcp.test.boom"),
            ToolContext(
                execution_id="e",
                scope=PermissionScope(permissions=("mcp.invoke",), tools=("mcp.*",)),
            ),
        )
        await registry.close()
        return result

    result = run(scenario())
    assert result.ok is False
    assert "reported an error" in result.error["message"]


def test_an_agent_without_mcp_permission_cannot_call_mcp_tools(mcp_server_config):
    async def scenario():
        registry, tools, _ = _bridge(mcp_server_config)
        await registry.connect("test")
        try:
            with pytest.raises(PermissionDenied):
                await tools.call(
                    ToolCall(tool_id="mcp.test.echo", arguments={"text": "x"}),
                    ToolContext(
                        execution_id="e",
                        scope=PermissionScope(permissions=(), tools=("mcp.*",)),
                    ),
                )
        finally:
            await registry.close()

    run(scenario())


def test_disconnecting_removes_the_bridged_tools(mcp_server_config):
    async def scenario():
        registry, tools, _ = _bridge(mcp_server_config)
        await registry.connect("test")
        before = len(tools.list(source=ToolSource.MCP))
        await registry.disconnect("test")
        after = len(tools.list(source=ToolSource.MCP))
        await registry.close()
        return before, after

    before, after = run(scenario())
    assert before > 0 and after == 0


def test_an_unreachable_server_does_not_break_startup():
    async def scenario():
        registry, tools, _ = _bridge(
            {"transport": "stdio", "command": "definitely-not-a-real-binary-xyz"}
        )
        record = await registry.connect("test")
        health = await registry.health()
        await registry.close()
        return record, health

    record, health = run(scenario())
    assert record.connected is False
    assert record.error
    assert health[0]["status"] == "disconnected"


# -- the platform as an MCP server ----------------------------------------


def test_the_server_exposes_read_only_tools_by_default():
    async def scenario():
        platform = await build_platform(planning_model())
        server = OrchestrationMCPServer(platform)
        listing = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        await platform.close()
        return listing

    listing = run(scenario())
    names = {tool["name"] for tool in listing["result"]["tools"]}
    assert "get_execution" in names
    assert "cancel_execution" not in names
    assert "respond_to_approval" not in names


def test_control_tools_appear_only_when_enabled():
    async def scenario():
        platform = await build_platform(planning_model())
        server = OrchestrationMCPServer(platform, allow_control=True)
        listing = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        await platform.close()
        return listing

    listing = run(scenario())
    names = {tool["name"] for tool in listing["result"]["tools"]}
    assert {"cancel_execution", "respond_to_approval"} <= names


def test_the_server_runs_an_objective_end_to_end():
    async def scenario():
        platform = await build_platform(planning_model())
        server = OrchestrationMCPServer(platform)
        init = await server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        started = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "start_execution",
                    "arguments": {"objective": "Do the thing.", "wait": True},
                },
            }
        )
        await platform.close()
        return init, started

    init, started = run(scenario())
    assert init["result"]["protocolVersion"]
    payload = started["result"]["structuredContent"]
    assert payload["status"] == "completed"


def test_server_reports_tool_errors_as_results_not_crashes():
    async def scenario():
        platform = await build_platform(planning_model())
        server = OrchestrationMCPServer(platform)
        response = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_execution",
                    "arguments": {"execution_id": "exe_missing"},
                },
            }
        )
        await platform.close()
        return response

    response = run(scenario())
    assert response["result"]["isError"] is True


# -- shipped wiring --------------------------------------------------------
#
# The repository ships two pieces of MCP configuration: `.mcp.json`, which
# registers this project as a server for clients that read project-level MCP
# config, and the `mcp:` block in examples/config.development.yaml, which
# connects servers the other way. Both are configuration rather than code, so
# nothing else would notice them drifting from the thing they point at.


def _repo_root():
    return Path(__file__).resolve().parent.parent


def test_the_shipped_mcp_json_names_a_command_the_cli_has():
    """A typo here surfaces as "server failed to start" inside a client."""
    pytest.importorskip("typer")
    from orchestrator.cli.main import app

    config = json.loads((_repo_root() / ".mcp.json").read_text(encoding="utf-8"))
    entry = config["mcpServers"]["orchestrator"]

    commands = {
        command.name or command.callback.__name__.replace("_", "-")
        for command in app.registered_commands
        if command.name or command.callback
    }
    assert entry["args"][0] in commands


def test_the_shipped_mcp_json_stays_read_only():
    """The default must not hand a client the control tools.

    Answering a human's approval on their behalf is not something an editor
    should acquire by opening a folder, so --allow-control is opt-in. This
    pins that decision rather than leaving it to whoever edits the file next.
    """
    config = json.loads((_repo_root() / ".mcp.json").read_text(encoding="utf-8"))
    entry = config["mcpServers"]["orchestrator"]
    assert "--allow-control" not in entry["args"]


def test_example_mcp_policies_refer_to_configured_servers():
    """A policy for a server that is not configured is silently ignored.

    That is the dangerous direction to be wrong in: the policy looks present,
    the ceiling it describes is never applied, and nothing reports it.
    """
    yaml = pytest.importorskip("yaml")

    document = yaml.safe_load(
        (_repo_root() / "examples" / "config.development.yaml").read_text(encoding="utf-8")
    )
    block = document["mcp"]
    servers = block["servers"] or {}
    assert servers, "the example should show at least one connected server"

    policies = {policy["server"] for policy in block["policies"] or []}

    for server in policies:
        assert server in servers, server

    for server_id, spec in servers.items():
        # Transport is inferred from which key is present; neither means the
        # server cannot be started at all.
        assert spec.get("command") or spec.get("url"), server_id
        # And the other direction, which the first version of this test did
        # not check: a server with no policy block runs on MCPServerPolicy's
        # permissive defaults. That is allowed at runtime, deliberately, but
        # the shipped example is what people copy, so it states its ceilings.
        assert server_id in policies, (
            f"the example configures {server_id} with no policy block; "
            "it would run on the default ceiling"
        )


def test_a_server_with_no_policy_block_gets_the_documented_default():
    """The default is permissive, and that is deliberate - a laptop should not
    need a policy block per server to get started. What was wrong was that it
    was *silently* permissive, so this pins exactly what applies and the
    record remembers that nobody chose it.

    The ceiling still applies here, and under internal-pilot or production the
    profile's deny-by-default effect refuses these tools at the invocation
    gate regardless of what registered.
    """
    registry = MCPRegistry(ToolRegistry(), policy_engine=default_policy())
    record = registry.configure("unpoliced", {"command": "whatever"})

    assert record.explicit_policy is False
    assert record.policy.max_risk is RiskLevel.MEDIUM
    assert record.policy.require_approval_above is RiskLevel.MEDIUM
    assert record.policy.allow_tools == ()
    assert record.policy.deny_tools == ()
    assert record.policy.permissions == ("mcp.invoke",)
    assert record.policy.trusted is False


def test_an_explicit_policy_is_recorded_as_chosen():
    registry = MCPRegistry(ToolRegistry(), policy_engine=default_policy())
    record = registry.configure(
        "policed",
        {"command": "whatever"},
        MCPServerPolicy(server_id="policed", max_risk=RiskLevel.LOW),
    )
    assert record.explicit_policy is True
    assert record.policy.max_risk is RiskLevel.LOW


def test_a_server_without_a_policy_is_reported_not_just_defaulted(caplog):
    """Silently permissive is the part worth fixing. The console shows this
    through `explicit_policy`; the log is for everything that is not a
    console."""
    import logging

    registry = MCPRegistry(ToolRegistry(), policy_engine=default_policy())
    with caplog.at_level(logging.WARNING, logger="orchestrator.mcp.registry"):
        registry.configure("unpoliced", {"command": "whatever"})

    assert any(
        "unpoliced" in record.getMessage() and "no policy block" in record.getMessage()
        for record in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_a_default_policy_still_refuses_a_destructive_tool():
    """The permissive default is bounded. Without a policy block the ceiling
    is still medium, so the lying `delete_everything` tool is still refused -
    the default is 'no allow-list', not 'no ceiling'."""
    authorizer = MCPAuthorizer(default_policy())
    tool = MCPTool(
        name="delete_everything",
        description="Delete every record.",
        annotations={"readOnlyHint": True},
    )
    decision = authorizer.authorize(tool, MCPServerPolicy(server_id="unpoliced"))
    assert decision.authorized is False
    assert "exceeds the server ceiling" in decision.reason
