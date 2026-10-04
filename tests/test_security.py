"""Policy, risk, permissions, and tool authorisation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from conftest import run

from orchestrator.core.domain.enums import RiskLevel, ToolSource
from orchestrator.core.domain.models import ToolCall, ToolSpec
from orchestrator.core.policy.engine import (
    ALLOW,
    DENY,
    REQUIRE_APPROVAL,
    PermissionScope,
    PolicyConfig,
    PolicyEngine,
    PolicyRule,
    default_policy,
)
from orchestrator.core.policy.risk import OperationDescriptor, RiskEngine
from orchestrator.errors import PermissionDenied
from orchestrator.observability.audit import AuditLog, NullAuditSink
from orchestrator.observability.logging import REDACTED, redact
from orchestrator.tools import permissions as perms
from orchestrator.tools.native import bookkeeping_tools, filesystem_tools, process_tools
from orchestrator.tools.registry import ToolContext, ToolRegistry

# -- risk ------------------------------------------------------------------


def test_risk_rises_with_independent_factors():
    risk = RiskEngine()
    benign = risk.assess(OperationDescriptor(name="read"))
    dangerous = risk.assess(
        OperationDescriptor(
            name="wipe", reversible=False, destructive=True, external_effect=True
        )
    )
    assert benign.level is RiskLevel.NONE
    assert dangerous.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert "irreversible" in dangerous.factors


def test_a_declared_risk_can_raise_but_never_lower():
    risk = RiskEngine()
    raised = risk.assess(
        OperationDescriptor(name="quiet", declared_risk=RiskLevel.CRITICAL)
    )
    assert raised.level is RiskLevel.CRITICAL

    lowered = risk.assess(
        OperationDescriptor(
            name="loud",
            reversible=False,
            destructive=True,
            financial_effect=True,
            declared_risk=RiskLevel.LOW,
        )
    )
    assert lowered.level is RiskLevel.CRITICAL


# -- policy ----------------------------------------------------------------


def test_high_risk_operations_require_approval_by_default():
    decision = default_policy().evaluate(
        OperationDescriptor(name="deploy", reversible=False, destructive=True)
    )
    assert decision.allowed is False
    assert decision.requires_approval is True
    assert decision.effect == REQUIRE_APPROVAL


def test_explicit_deny_beats_everything():
    engine = PolicyEngine(
        rules=[PolicyRule(kind="tool", subject="danger.*", effect=DENY, reason="no")]
    )
    decision = engine.evaluate(OperationDescriptor(name="danger.run"), kind="tool")
    assert decision.allowed is False and decision.requires_approval is False
    with pytest.raises(PermissionDenied):
        decision.raise_if_denied(subject="danger.run")


def test_more_specific_rules_win():
    engine = PolicyEngine(
        rules=[
            PolicyRule(kind="tool", subject="*", effect=ALLOW),
            PolicyRule(kind="tool", subject="fs.write_file", effect=DENY),
        ]
    )
    assert engine.evaluate(OperationDescriptor(name="fs.read_file"), kind="tool").allowed
    assert not engine.evaluate(
        OperationDescriptor(name="fs.write_file"), kind="tool"
    ).allowed


def test_missing_permissions_are_denied_not_escalated():
    engine = PolicyEngine()
    decision = engine.evaluate(
        OperationDescriptor(name="tool", permissions=["fs.write"]),
        kind="tool",
        granted_permissions=["fs.read"],
    )
    assert decision.allowed is False
    assert decision.requires_approval is False
    assert "fs.write" in decision.reason


def test_wildcard_permission_grants_are_honoured():
    engine = PolicyEngine()
    decision = engine.evaluate(
        OperationDescriptor(name="tool", permissions=["fs.write"]),
        kind="tool",
        granted_permissions=["fs.*"],
    )
    assert decision.allowed is True


def test_explicit_grant_mode_denies_ungranted_tools():
    engine = PolicyEngine(config=PolicyConfig(require_explicit_tool_grant=True))
    assert not engine.evaluate(OperationDescriptor(name="anything"), kind="tool").allowed


def test_permission_scope_narrows_to_what_was_granted():
    scope = PermissionScope(permissions=("fs.read",), tools=("fs.*",))
    assert scope.allows_tool("fs.read_file")
    assert not scope.allows_tool("process.run")
    narrowed = scope.narrowed_to(["fs.read_file", "process.run"])
    assert narrowed.tools == ("fs.read_file",)


def test_permission_expansion_and_missing_report():
    assert "fs.read" in perms.expand(["fs"])
    assert perms.missing(["fs.write"], ["fs.read"]) == ["fs.write"]
    assert perms.missing(["fs.write"], ["fs.*"]) == []


# -- tool registry ---------------------------------------------------------


def _registry():
    sink = NullAuditSink()
    return ToolRegistry(policy=default_policy(), audit=AuditLog(sink)), sink


def test_tool_outside_scope_is_refused():
    registry, _ = _registry()
    with tempfile.TemporaryDirectory() as directory:
        registry.register_many(filesystem_tools(directory))
        context = ToolContext(
            execution_id="e",
            scope=PermissionScope(permissions=("fs.read",), tools=("orchestrator.*",)),
            workspace=directory,
        )
        with pytest.raises(PermissionDenied, match="not in the scope"):
            registry.authorize("fs.read_file", context)


def test_tool_without_permission_is_refused():
    registry, _ = _registry()
    with tempfile.TemporaryDirectory() as directory:
        registry.register_many(filesystem_tools(directory))
        context = ToolContext(
            execution_id="e",
            scope=PermissionScope(permissions=("fs.read",), tools=("fs.*",)),
            workspace=directory,
        )
        with pytest.raises(PermissionDenied, match="fs.write"):
            registry.authorize("fs.write_file", context)


def test_path_traversal_out_of_the_workspace_is_refused():
    registry, _ = _registry()
    with tempfile.TemporaryDirectory() as directory:
        registry.register_many(filesystem_tools(directory))
        context = ToolContext(
            execution_id="e",
            scope=PermissionScope(permissions=("fs.read",), tools=("fs.*",)),
            workspace=directory,
        )
        result = run(
            registry.call(
                ToolCall(tool_id="fs.read_file", arguments={"path": "../../secret"}),
                context,
            )
        )
    assert result.ok is False
    assert result.error["code"] == "permission_denied"


def test_process_tools_require_an_allow_list():
    from orchestrator.errors import ToolError

    with pytest.raises(ToolError):
        process_tools(".", allowed_commands=[])


def test_disallowed_command_is_refused():
    registry, _ = _registry()
    with tempfile.TemporaryDirectory() as directory:
        registry.register_many(process_tools(directory, allowed_commands=["python"]))
        # process.run is HIGH risk, so it needs approval before it can even run.
        context = ToolContext(
            execution_id="e",
            scope=PermissionScope(permissions=("process.execute",), tools=("process.*",)),
            workspace=directory,
        )
        with pytest.raises(PermissionDenied) as excinfo:
            run(
                registry.call(
                    ToolCall(tool_id="process.run", arguments={"command": "rm -rf /"}),
                    context,
                )
            )
    assert excinfo.value.details.get("requires_approval") is True


def test_tool_failures_are_returned_as_data_not_raised():
    registry, _ = _registry()
    spec = ToolSpec(id="boom", name="boom", source=ToolSource.NATIVE, max_retries=0)

    def handler(arguments, context):
        raise ValueError("handler exploded")

    registry.register(spec, handler)
    result = run(
        registry.call(
            ToolCall(tool_id="boom"),
            ToolContext(execution_id="e", scope=PermissionScope(tools=("boom",))),
        )
    )
    assert result.ok is False
    assert "handler exploded" in result.error["message"]


def test_idempotent_tools_are_retried_and_can_recover():
    registry, _ = _registry()
    attempts = {"count": 0}

    def handler(arguments, context):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ValueError("flaky")
        return {"ok": True}

    registry.register(
        ToolSpec(id="flaky", name="flaky", max_retries=3, idempotent=True), handler
    )
    result = run(
        registry.call(
            ToolCall(tool_id="flaky"),
            ToolContext(execution_id="e", scope=PermissionScope(tools=("flaky",))),
        )
    )
    assert result.ok is True and attempts["count"] == 3


def test_non_idempotent_tools_are_not_retried():
    registry, _ = _registry()
    attempts = {"count": 0}

    def handler(arguments, context):
        attempts["count"] += 1
        raise ValueError("side effect already happened")

    registry.register(
        ToolSpec(id="once", name="once", max_retries=3, idempotent=False), handler
    )
    run(
        registry.call(
            ToolCall(tool_id="once"),
            ToolContext(execution_id="e", scope=PermissionScope(tools=("once",))),
        )
    )
    assert attempts["count"] == 1


def test_tool_timeouts_are_enforced():
    import asyncio

    registry, _ = _registry()

    async def handler(arguments, context):
        await asyncio.sleep(5)

    registry.register(
        ToolSpec(id="slow", name="slow", timeout_seconds=0.05, max_retries=0), handler
    )
    result = run(
        registry.call(
            ToolCall(tool_id="slow"),
            ToolContext(execution_id="e", scope=PermissionScope(tools=("slow",))),
        )
    )
    assert result.ok is False and result.error["code"] == "tool_timeout"


def test_scope_hides_tools_the_agent_may_not_use():
    registry, _ = _registry()
    with tempfile.TemporaryDirectory() as directory:
        registry.register_many(filesystem_tools(directory) + bookkeeping_tools())
        visible = registry.for_scope(
            PermissionScope(permissions=("fs.read",), tools=("fs.*",))
        )
    assert {spec.id for spec in visible} == {"fs.read_file", "fs.list_directory"}


def test_denials_are_audited():
    registry, sink = _registry()
    with tempfile.TemporaryDirectory() as directory:
        registry.register_many(filesystem_tools(directory))
        with pytest.raises(PermissionDenied):
            registry.authorize(
                "fs.write_file",
                ToolContext(
                    execution_id="e",
                    scope=PermissionScope(permissions=("fs.read",), tools=("fs.*",)),
                ),
            )
    assert any(e.type == "tool.denied" for e in registry.audit.pending)


# -- redaction -------------------------------------------------------------


def test_redaction_does_not_destroy_usage_observability():
    """Substring matching would blank every token *count* along with the secrets."""

    cleaned = redact(
        {
            "input_tokens": 120,
            "output_tokens": 45,
            "max_tokens": 2000,
            "cost": 0.02,
            "access_token": "should-not-survive",
            "refresh_token": "nor-this",
        }
    )
    assert cleaned["input_tokens"] == 120
    assert cleaned["output_tokens"] == 45
    assert cleaned["max_tokens"] == 2000
    assert cleaned["cost"] == 0.02
    assert cleaned["access_token"] == REDACTED
    assert cleaned["refresh_token"] == REDACTED


def test_secrets_never_reach_the_audit_trail():
    payload = {
        "api_key": "sk-secret",
        "nested": {"authorization": "Bearer abc", "safe": "value"},
        "list": [{"password": "hunter2"}],
    }
    cleaned = redact(payload)
    blob = str(cleaned)
    assert "sk-secret" not in blob
    assert "hunter2" not in blob
    assert "Bearer abc" not in blob
    assert cleaned["nested"]["safe"] == "value"


def test_workspace_confinement_allows_legitimate_paths():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "notes.txt"
        target.write_text("content", encoding="utf-8")
        registry, _ = _registry()
        registry.register_many(filesystem_tools(directory))
        result = run(
            registry.call(
                ToolCall(tool_id="fs.read_file", arguments={"path": "notes.txt"}),
                ToolContext(
                    execution_id="e",
                    scope=PermissionScope(permissions=("fs.read",), tools=("fs.*",)),
                    workspace=directory,
                ),
            )
        )
    assert result.ok is True and result.output["content"] == "content"
