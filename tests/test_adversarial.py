"""Adversarial tests: a model that has been turned against the system.

Every test here assumes the attacker has already won the argument. The prompt
injection succeeded, the model is now trying to expand its own permissions,
read secrets, or exfiltrate data — and the question is whether anything
downstream still stops it.

That framing matters. A test that checks "the model refuses" tests the model.
These check that the refusal does not depend on the model at all: policy
decides, tools execute, and neither asks the model whether it should.

Where a test would need a live model it constructs the malicious request
directly instead. The attack surface is the tool call, not the sentence that
produced it, so a synthetic tool call is a *stronger* test than a real
injection that may or may not have worked.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.core.policy.engine import OperationDescriptor
from orchestrator.errors import PermissionDenied, ToolError
from orchestrator.tools import permissions as perms
from orchestrator.tools.egress import EgressPolicy, resolve_and_validate, validate_redirect
from orchestrator.tools.execpolicy import ExecPolicy, build_environment, resolve_executable
from orchestrator.tools.native import filesystem_tools, http_tools, process_tools
from orchestrator.tools.registry import ToolContext


def _run(coro):
    return asyncio.run(coro)


def _call(entries, tool_id, **arguments):
    """Invoke a tool handler, sync or async."""
    import inspect

    tools = {spec.id: fn for spec, fn in entries}
    if tool_id not in tools:
        raise KeyError(f"{tool_id} was not registered: {sorted(tools)}")
    result = tools[tool_id](arguments, ToolContext(execution_id="e", task_id="t"))
    return _run(result) if inspect.isawaitable(result) else result


# ==========================================================================
# Exfiltrating files over HTTP
# ==========================================================================


def test_a_compromised_model_cannot_post_a_file_to_an_attacker_host():
    """The classic chain: read a secret, then POST it somewhere."""
    policy = EgressPolicy(allowed_hosts=("api.internal.example",))

    for destination in (
        "https://attacker.test/collect",
        "https://api.internal.example.attacker.test/collect",
        "http://attacker.test/collect",
    ):
        with pytest.raises(PermissionDenied):
            resolve_and_validate(destination, policy)


def test_exfiltration_via_a_redirect_from_an_allowed_host_is_refused():
    """An allowed host the attacker controls, bouncing to their collector."""
    policy = EgressPolicy(allowed_hosts=("example.com",), allow_http=True)
    with pytest.raises(PermissionDenied):
        validate_redirect(
            "https://example.com/start", "https://collector.attacker.test/x", policy
        )


def test_a_read_only_http_grant_cannot_send_a_body_outward():
    """Data leaves in a POST. A read tool must not be able to make one."""
    entries = http_tools(
        policy=EgressPolicy(allowed_hosts=("example.com",), allowed_methods=("GET",))
    )
    ids = {spec.id for spec, _ in entries}
    assert "http.send" not in ids, "a write tool was registered under a read-only policy"

    with pytest.raises(PermissionDenied):
        _call(
            entries,
            "http.request",
            url="https://example.com/x",
            method="POST",
            body="the contents of a private file",
        )


def test_a_stolen_credential_cannot_be_attached_to_an_outbound_request():
    """Reading a token is bad; forwarding it is worse."""
    entries = http_tools(
        policy=EgressPolicy(allowed_hosts=("example.com",), allowed_methods=("GET",))
    )
    for header in ("Authorization", "authorization", "Cookie", "X-Api-Key"):
        with pytest.raises(PermissionDenied):
            _call(
                entries,
                "http.request",
                url="https://example.com/x",
                headers={header: "sk-or-v1-stolenkeyvalue123456"},
            )


# ==========================================================================
# Reaching the cloud metadata service
# ==========================================================================


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://169.254.170.2/v2/credentials/",
        "http://100.100.100.200/latest/meta-data/",
        "http://192.0.0.192/opc/v1/instance/",
    ],
)
def test_no_configuration_permits_reaching_a_metadata_endpoint(url):
    """This is the single request that turns an SSRF into a stolen account."""
    # Maximally permissive: everything opted into, and the host allowlisted.
    permissive = EgressPolicy(
        allowed_hosts=(
            "169.254.169.254",
            "metadata.google.internal",
            "169.254.170.2",
            "100.100.100.200",
            "192.0.0.192",
        ),
        allow_http=True,
        allow_private_networks=True,
        allow_loopback=True,
        allow_link_local=True,
    )
    with pytest.raises(PermissionDenied):
        resolve_and_validate(url, permissive)


def test_the_metadata_block_has_no_configuration_escape_hatch():
    """There is no YAML key that turns this off."""
    from orchestrator.platform import _egress_policy

    policy = _egress_policy(
        {
            "enabled": True,
            "allowed_hosts": ["169.254.169.254"],
            "allow_http": True,
            "allow_link_local": True,
            "allow_private_networks": True,
            # Not a real setting. If it were honoured, this would pass.
            "allow_cloud_metadata": True,
        }
    )
    assert policy.allow_cloud_metadata is False
    with pytest.raises(PermissionDenied):
        resolve_and_validate("http://169.254.169.254/latest/", policy)


# ==========================================================================
# Escaping the workspace
# ==========================================================================


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "..\\..\\..\\Windows\\System32\\config\\SAM",
        "/etc/shadow",
        "C:\\Windows\\win.ini",
        "....//....//etc/passwd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    ],
)
def test_a_model_cannot_read_outside_the_workspace(tmp_path, path):
    (tmp_path / "allowed.txt").write_text("fine", encoding="utf-8")
    entries = filesystem_tools(str(tmp_path), allow_write=False)

    with pytest.raises((PermissionDenied, ToolError, OSError, ValueError)):
        _call(entries, "fs.read_file", path=path)


def test_the_legitimate_case_still_works(tmp_path):
    """A confinement that blocks everything proves nothing."""
    (tmp_path / "allowed.txt").write_text("fine", encoding="utf-8")
    entries = filesystem_tools(str(tmp_path), allow_write=False)
    result = _call(entries, "fs.read_file", path="allowed.txt")
    assert "fine" in str(result)


def test_a_read_only_filesystem_grant_registers_no_write_tool(tmp_path):
    entries = filesystem_tools(str(tmp_path), allow_write=False)
    ids = {spec.id for spec, _ in entries}
    assert not any("write" in i or "delete" in i for i in ids), ids


# ==========================================================================
# Escalating through process execution
# ==========================================================================


def test_a_subprocess_cannot_read_the_orchestrators_credentials(monkeypatch, tmp_path):
    """The most direct escalation: spawn something and print os.environ."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-attackerwantsthis")
    monkeypatch.setenv("ORCHESTRATOR_API_TOKEN", "attackerwantsthistoo")

    entries = process_tools(
        policy=ExecPolicy(allowed_commands=(sys.executable,), root=str(tmp_path))
    )
    result = _call(
        entries,
        "process.run",
        command=[sys.executable, "-c", "import os; print(dict(os.environ))"],
    )
    assert "attackerwantsthis" not in result["stdout"]
    assert "OPENROUTER_API_KEY" not in result["stdout"]
    assert "ORCHESTRATOR_API_TOKEN" not in result["stdout"]


def test_a_model_cannot_reach_a_shell_through_the_allowlist(tmp_path):
    """A shell in the allowlist makes every other entry decorative."""
    with pytest.raises(ValueError):
        ExecPolicy(allowed_commands=("bash", "git")).validate()
    with pytest.raises(ValueError):
        ExecPolicy(allowed_commands=("cmd.exe",)).validate()
    # Wrappers that reach a shell indirectly.
    for wrapper in ("env", "xargs", "nohup"):
        with pytest.raises(ValueError):
            ExecPolicy(allowed_commands=(wrapper,)).validate()


def test_shell_metacharacters_are_data_not_syntax(tmp_path):
    marker = tmp_path / "pwned"
    entries = process_tools(
        policy=ExecPolicy(allowed_commands=(sys.executable,), root=str(tmp_path))
    )
    _call(
        entries,
        "process.run",
        command=[
            sys.executable,
            "-c",
            "print('ok')",
            f"&& touch {marker}",
            f"; echo x > {marker}",
            f"| tee {marker}",
        ],
    )
    assert not marker.exists()


def test_a_planted_binary_under_an_allowed_name_is_refused(tmp_path):
    """Allowing 'python' must not allow a python the attacker wrote."""
    planted_dir = tmp_path / "planted"
    planted_dir.mkdir()
    planted = planted_dir / Path(sys.executable).name
    planted.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")

    policy = ExecPolicy(allowed_commands=(sys.executable,))
    with pytest.raises(PermissionDenied):
        resolve_executable(str(planted), policy)


def test_loader_variables_cannot_be_injected_into_a_child(tmp_path):
    """LD_PRELOAD turns any permitted binary into arbitrary code."""
    policy = ExecPolicy(
        allowed_commands=(sys.executable,), environment_allowlist=("BUILD_ID",)
    )
    for variable in (
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONSTARTUP",
        "NODE_OPTIONS",
        "BASH_ENV",
    ):
        with pytest.raises(PermissionDenied):
            build_environment(policy, {variable: "/tmp/evil.so"})


def test_a_caller_cannot_raise_its_own_resource_ceiling(tmp_path):
    """Bounds the model can edit are not bounds."""
    policy = ExecPolicy(allowed_commands=(sys.executable,), root=str(tmp_path), timeout=1.0)
    assert policy.effective_timeout(3600) == 1.0
    assert policy.effective_timeout(-1) == 1.0
    assert policy.effective_timeout(None) == 1.0
    assert policy.effective_timeout(0.5) == 0.5


# ==========================================================================
# Expanding permissions
# ==========================================================================


def test_a_task_cannot_grant_itself_a_permission_it_was_not_given():
    """Content read by a tool must not be able to widen that tool's grant."""
    granted = ["fs.read"]
    for wanted in (
        "fs.write",
        "fs.delete",
        "process.execute",
        "network.write",
        "secret.read",
        "mcp.invoke",
    ):
        assert perms.missing([wanted], granted) == [wanted], wanted


def test_a_wildcard_in_untrusted_content_does_not_expand_a_grant():
    """ "fs.*" is a grant an operator writes, not one a model can claim."""
    # Granted a wildcard: it works.
    assert perms.missing(["fs.write"], ["fs.*"]) == []
    # Not granted it: naming it in a task does not conjure it.
    assert perms.missing(["fs.write"], ["fs.read"]) == ["fs.write"]
    assert perms.missing(["secret.read"], ["fs.*"]) == ["secret.read"]


def test_secret_read_is_never_in_the_safe_defaults():
    assert perms.SECRET_READ not in perms.SAFE_DEFAULTS
    assert perms.PROCESS_EXECUTE not in perms.SAFE_DEFAULTS
    assert perms.FS_WRITE not in perms.SAFE_DEFAULTS
    assert perms.NETWORK_WRITE not in perms.SAFE_DEFAULTS


def test_a_missing_permission_is_a_denial_not_an_approval_prompt():
    """Otherwise the escalation path is: ask, and hope a human clicks yes."""
    from orchestrator.config.loader import Config
    from orchestrator.platform import _build_policy

    policy = _build_policy(Config({"profile": "production"}))
    decision = policy.evaluate(
        OperationDescriptor(kind="tool", name="fs.write_file", permissions=["fs.write"]),
        granted_permissions=["fs.read"],
    )
    assert decision.allowed is False
    assert decision.requires_approval is False


def test_an_explicit_deny_rule_cannot_be_overridden_by_risk_or_approval():
    from orchestrator.core.policy.engine import PolicyEngine, PolicyRule

    engine = PolicyEngine(
        rules=[
            PolicyRule(
                kind="tool",
                subject="process.run",
                effect="deny",
                reason="process execution is not permitted here",
            ),
        ]
    )
    decision = engine.evaluate(
        OperationDescriptor(kind="tool", name="process.run"),
        granted_permissions=["process.execute"],
    )
    assert decision.allowed is False
    assert decision.requires_approval is False


# ==========================================================================
# Unauthorized MCP tools
# ==========================================================================


def test_a_scope_does_not_permit_a_server_it_does_not_name():
    from orchestrator.core.policy.engine import PermissionScope

    scope = PermissionScope(tools=("fs.*",), mcp_servers=("approved-server",))
    assert scope.allows_server("approved-server") is True
    assert scope.allows_server("attacker-server") is False
    # An empty server list permits nothing, rather than everything.
    assert PermissionScope(tools=("fs.*",)).allows_server("anything") is False


def test_an_empty_tool_scope_permits_nothing():
    from orchestrator.core.policy.engine import PermissionScope

    assert PermissionScope().allows_tool("fs.read_file") is False


def test_narrowing_a_scope_cannot_widen_it():
    """A re-scope must be a subset, whatever it is handed."""
    from orchestrator.core.policy.engine import PermissionScope

    scope = PermissionScope(tools=("fs.read_file",))
    widened = scope.narrowed_to(["fs.read_file", "process.run", "http.send"])
    assert set(widened.tools) == {"fs.read_file"}


# ==========================================================================
# The API surface
# ==========================================================================


def test_an_unauthenticated_caller_cannot_start_work_that_runs_tools():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from orchestrator.api.app import create_app
    from orchestrator.api.security import SecurityConfig

    client = TestClient(
        create_app(security=SecurityConfig(host="0.0.0.0", tokens=("real-token",)))
    )
    assert (
        client.post("/v1/executions", json={"objective": "read every file"}).status_code
        == 401
    )


def test_the_service_refuses_to_start_unauthenticated_on_a_network_address():
    from orchestrator.api.security import InsecureBinding, SecurityConfig

    for host in ("0.0.0.0", "::", "10.0.0.5", "203.0.113.9", "orch.internal"):
        with pytest.raises(InsecureBinding):
            SecurityConfig(host=host).verify_binding()
