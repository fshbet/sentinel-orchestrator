"""Controlled failure injection.

Every failure mode the platform claims to handle is triggered deliberately here
and the recovery behaviour is asserted (spec section 74). These are the tests
that would catch "it works until something goes wrong".
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import build_platform, make_config, planning_model, run

from orchestrator.core.domain.enums import (
    Confidence,
    ExecutionStatus,
    FailureCategory,
    TaskStatus,
)
from orchestrator.core.domain.models import ToolCall, ToolSpec
from orchestrator.core.policy.engine import PermissionScope, default_policy
from orchestrator.errors import (
    ContextOverflow,
    ModelTimeout,
    ModelUnavailable,
)
from orchestrator.llm.providers.scripted import CallableProvider, ScriptedProvider
from orchestrator.observability.audit import AuditLog, NullAuditSink
from orchestrator.platform import Orchestrator
from orchestrator.tools.registry import ToolContext, ToolRegistry


def _categories(execution):
    return {failure.category for failure in execution.failures}


# -- model failures --------------------------------------------------------


def test_a_model_timeout_is_classified_as_transient_and_retried():
    calls = {"count": 0}

    def flaky(request):
        if "extract structured requirements" in (request.system or ""):
            return {"explicit": ["work"], "success_criteria": [{"description": "d"}]}
        if "decompose an objective" in (request.system or ""):
            return {"tasks": [{"key": "a", "name": "a", "objective": "Do it."}]}
        calls["count"] += 1
        if calls["count"] == 1:
            raise ModelTimeout("the model timed out")
        return "the work is done"

    async def scenario():
        platform = await build_platform(CallableProvider(flaky))
        execution = await platform.run("Do it.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert FailureCategory.TRANSIENT in _categories(execution)
    assert calls["count"] >= 2


def test_a_malformed_model_response_falls_back_to_deterministic_planning():
    def malformed(request):
        if "decompose an objective" in (request.system or ""):
            return "this is prose, not a plan"
        if "extract structured requirements" in (request.system or ""):
            return "also prose"
        return "the work is done"

    async def scenario():
        platform = await build_platform(CallableProvider(malformed))
        execution = await platform.run("Do the thing.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.COMPLETED
    assert execution.plan is not None
    assert "deterministic" in execution.plan.rationale


def test_every_model_being_unavailable_escalates_rather_than_inventing_output():
    async def scenario():
        provider = ScriptedProvider(
            [ModelUnavailable("everything is down")],
            name="down",
            repeat_last=True,
        )
        platform = await build_platform(
            provider, recovery={"allow_human_escalation": False}
        )
        execution = await platform.run("Do the thing.")
        await platform.close()
        return execution

    execution = run(scenario())
    assert execution.status is ExecutionStatus.FAILED
    assert execution.confidence is Confidence.FAILED
    assert not any(t.status is TaskStatus.SUCCEEDED for t in execution.tasks.values())


# -- tool and MCP failures -------------------------------------------------


def test_a_tool_that_hangs_is_timed_out_and_reported():
    registry = ToolRegistry(policy=default_policy(), audit=AuditLog(NullAuditSink()))

    async def hang(arguments, context):
        await asyncio.sleep(30)

    registry.register(
        ToolSpec(id="hang", name="hang", timeout_seconds=0.05, max_retries=0), hang
    )
    result = run(
        registry.call(
            ToolCall(tool_id="hang"),
            ToolContext(execution_id="e", scope=PermissionScope(tools=("hang",))),
        )
    )
    assert result.ok is False and result.error["code"] == "tool_timeout"


def test_a_tool_returning_corrupt_data_does_not_crash_the_run():
    class Unserialisable:
        def __repr__(self):
            raise RuntimeError("even repr is broken")

    registry = ToolRegistry(policy=default_policy(), audit=AuditLog(NullAuditSink()))
    registry.register(
        ToolSpec(id="corrupt", name="corrupt", max_retries=0),
        lambda arguments, context: {"value": Unserialisable()},
    )
    result = run(
        registry.call(
            ToolCall(tool_id="corrupt"),
            ToolContext(execution_id="e", scope=PermissionScope(tools=("corrupt",))),
        )
    )
    # The tool call itself succeeded; the platform did not crash trying to
    # log or serialise the odd value.
    assert result.ok is True
    with pytest.raises(TypeError):
        json.dumps(result.output)


def test_an_mcp_server_that_dies_mid_session_is_reported():
    import sys
    import tempfile
    import textwrap
    from pathlib import Path

    from orchestrator.errors import MCPError
    from orchestrator.mcp.client import MCPClient

    server = textwrap.dedent(
        """
        import json, sys
        line = sys.stdin.readline()
        message = json.loads(line)
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": message["id"],
            "result": {"protocolVersion": "2025-06-18",
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "dying", "version": "1"}},
        }) + "\\n")
        sys.stdout.flush()
        raise SystemExit(1)
        """
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        script = Path(directory) / "dying_server.py"
        script.write_text(server, encoding="utf-8")

        async def scenario():
            client = await MCPClient.connect(
                "dying",
                {"transport": "stdio", "command": sys.executable, "args": [str(script)]},
            )
            with pytest.raises(MCPError):
                await client.list_tools()
            health = await client.health()
            await client.close()
            return health

        health = run(scenario())

    assert health["status"] == "unreachable"


def test_an_mcp_timeout_cancels_the_request():
    import sys
    import tempfile
    import textwrap
    from pathlib import Path

    from orchestrator.errors import MCPTimeout as MCPTimeoutError
    from orchestrator.mcp.client import MCPClient

    server = textwrap.dedent(
        """
        import json, sys, time
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("method") == "initialize":
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": message["id"],
                    "result": {"protocolVersion": "2025-06-18",
                               "capabilities": {"tools": {}},
                               "serverInfo": {"name": "slow", "version": "1"}},
                }) + "\\n")
                sys.stdout.flush()
            elif message.get("method") == "tools/list":
                time.sleep(10)
        """
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        script = Path(directory) / "slow_server.py"
        script.write_text(server, encoding="utf-8")

        async def scenario():
            client = await MCPClient.connect(
                "slow",
                {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(script)],
                    "timeout": 0.3,
                },
            )
            with pytest.raises(MCPTimeoutError):
                await client.list_tools()
            await client.close()

        run(scenario())


# -- context failures ------------------------------------------------------


def test_context_overflow_selects_scope_reduction():
    from conftest import make_task

    from orchestrator.core.domain.models import Failure
    from orchestrator.recovery.classification import classify
    from orchestrator.recovery.strategies import select

    assert classify(ContextOverflow("too big")) is FailureCategory.CONTEXT
    choice = select(Failure(category=FailureCategory.CONTEXT), task=make_task("t"))
    assert choice.strategy.value == "reduce_scope"


def test_a_tiny_context_window_still_produces_a_valid_request():
    from conftest import make_task

    from orchestrator.context.manager import ContextManager, ContextRequest
    from orchestrator.core.domain.models import Execution, ModelSpec

    execution = Execution(objective="o" * 4000)
    task = make_task("t")
    task.objective = "t" * 4000
    execution.tasks[task.id] = task
    built = run(
        ContextManager().build(
            ContextRequest(execution=execution, task=task),
            ModelSpec(id="tiny", context_window=800, max_output_tokens=200),
        )
    )
    assert built.estimated_tokens <= built.budget.total_input
    assert built.request.messages[0].content  # still says something


# -- state corruption ------------------------------------------------------


def test_a_corrupt_stored_document_is_reported_not_silently_accepted():
    import sqlite3
    import tempfile
    from pathlib import Path

    from orchestrator.core.state.sqlite_store import SQLiteStateStore

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        path = Path(directory) / "state.db"

        async def scenario():
            store = SQLiteStateStore(path)
            from orchestrator.core.domain.models import Execution

            execution = Execution(objective="will be corrupted")
            await store.create(execution)
            await store.close()
            return execution.id

        execution_id = run(scenario())

        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE executions SET document = ? WHERE id = ?",
            ("{not valid json", execution_id),
        )
        connection.commit()
        connection.close()

        async def read():
            store = SQLiteStateStore(path)
            try:
                with pytest.raises(json_error()):
                    await store.get(execution_id)
            finally:
                await store.close()

        run(read())


def json_error():
    import json as _json

    return _json.JSONDecodeError


# -- interruption ----------------------------------------------------------


def test_an_interrupted_run_does_not_repeat_completed_work():
    completed: list[str] = []

    def worker(request):
        body = request.messages[0].content
        # Match the task objective, not the overall objective, which mentions both.
        for name in ("alpha", "beta"):
            if f"Handle {name}." in body:
                completed.append(name)
        return "done"

    provider = planning_model(
        tasks=[
            {"key": "a", "name": "a", "objective": "Handle alpha."},
            {"key": "b", "name": "b", "objective": "Handle beta.", "depends_on": ["a"]},
        ],
        worker=worker,
    )

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        database = str(Path(directory) / "state.db")
        config = make_config(storage={"backend": "sqlite", "path": database})

        async def first():
            platform = await Orchestrator.create(
                config=config, providers=[provider], connect_mcp=False
            )
            execution = await platform.start("Alpha then beta.")
            await platform.engine._understand(execution)
            await platform.engine._plan(execution)
            # Run only the first task, then simulate the process dying.
            from orchestrator.core.execution.limits import LimitGuard

            guard = LimitGuard(execution.limits)
            first_task = next(t for t in execution.tasks.values() if t.name == "a")
            await platform.engine._run_task(execution, first_task, guard)
            await platform.state.persist(execution)
            await platform.close()
            return execution.id

        execution_id = run(first())
        assert completed == ["alpha"]

        async def second():
            platform = await Orchestrator.create(
                config=config, providers=[provider], connect_mcp=False
            )
            resumed = await platform.engine.run(execution_id)
            await platform.close()
            return resumed

        resumed = run(second())

    assert resumed.status is ExecutionStatus.COMPLETED
    assert completed == ["alpha", "beta"]  # alpha was not redone
