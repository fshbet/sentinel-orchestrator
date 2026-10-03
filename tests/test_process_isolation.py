"""Process execution controls.

Written against what ``process_tools`` did before ``execpolicy.py``:

* inherited the full environment, so a subprocess could read
  OPENROUTER_API_KEY, NVIDIA_API_KEY and ORCHESTRATOR_API_TOKEN;
* matched the allowlist on basename only, so allowing ``python`` allowed
  ``/tmp/anything/python``;
* had no opinion about shell interpreters;
* let the caller pass ``timeout``, overriding the configured bound.

Process execution is privileged. These tests treat it that way.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.errors import PermissionDenied, ToolError
from orchestrator.tools.execpolicy import (
    SHELL_INTERPRETERS,
    ExecPolicy,
    build_environment,
    resolve_executable,
)
from orchestrator.tools.native import process_tools
from orchestrator.tools.registry import ToolContext


def _policy(tmp_path, **overrides):
    options = dict(root=str(tmp_path), allowed_commands=(sys.executable,))
    options.update(overrides)
    return ExecPolicy(**options)


def _call(policy, **arguments):
    tools = {spec.id: fn for spec, fn in process_tools(policy=policy)}
    return asyncio.run(
        tools["process.run"](arguments, ToolContext(execution_id="e", task_id="t"))
    )


# --------------------------------------------------------------------------
# Environment isolation — the leak that mattered
# --------------------------------------------------------------------------


def test_the_environment_is_not_inherited_by_default(monkeypatch, tmp_path):
    """A subprocess must not be able to read the orchestrator's credentials."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-should-never-be-visible")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-should-never-be-visible")
    monkeypatch.setenv("ORCHESTRATOR_API_TOKEN", "token-should-never-be-visible")

    result = _call(
        _policy(tmp_path),
        command=[
            sys.executable,
            "-c",
            "import os,json; print(json.dumps(dict(os.environ)))",
        ],
    )
    child_env = result["stdout"]
    assert "should-never-be-visible" not in child_env
    assert "OPENROUTER_API_KEY" not in child_env
    assert "NVIDIA_API_KEY" not in child_env
    assert "ORCHESTRATOR_API_TOKEN" not in child_env


def test_only_allowlisted_variables_are_passed_through(monkeypatch):
    monkeypatch.setenv("SECRET_THING", "nope")
    monkeypatch.setenv("BUILD_ID", "1234")

    env = build_environment(
        ExecPolicy(allowed_commands=("x",), environment_allowlist=("BUILD_ID",))
    )
    assert env.get("BUILD_ID") == "1234"
    assert "SECRET_THING" not in env


def test_a_minimal_path_is_always_provided():
    """An empty environment breaks most executables for no security gain."""
    env = build_environment(ExecPolicy(allowed_commands=("x",)))
    assert "PATH" in env
    assert env.get("PATH")


def test_explicitly_passed_variables_cannot_smuggle_secrets(tmp_path):
    """A caller-supplied env must be filtered by the same allowlist."""
    policy = _policy(tmp_path, environment_allowlist=("BUILD_ID",))
    with pytest.raises(PermissionDenied) as exc:
        _call(
            policy,
            command=[sys.executable, "-c", "print(1)"],
            env={"AWS_SECRET_ACCESS_KEY": "leak"},
        )
    assert "AWS_SECRET_ACCESS_KEY" in str(exc.value)


def test_dangerous_loader_variables_are_never_passable():
    """LD_PRELOAD and friends turn any allowed binary into arbitrary code."""
    policy = ExecPolicy(
        allowed_commands=("x",),
        environment_allowlist=("LD_PRELOAD", "PYTHONPATH", "BUILD_ID"),
    )
    with pytest.raises(ValueError) as exc:
        policy.validate()
    assert "LD_PRELOAD" in str(exc.value)


# --------------------------------------------------------------------------
# Executable identity, not just its name
# --------------------------------------------------------------------------


def test_the_allowlist_matches_a_resolved_path_not_a_basename(tmp_path):
    """Allowing 'python' must not allow /tmp/attacker/python."""
    fake_dir = tmp_path / "attacker"
    fake_dir.mkdir()
    fake = fake_dir / Path(sys.executable).name
    fake.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
    fake.chmod(0o755)

    policy = ExecPolicy(allowed_commands=(sys.executable,))
    with pytest.raises(PermissionDenied) as exc:
        resolve_executable(str(fake), policy)
    assert "not in the allowed list" in str(exc.value).lower()


def test_the_configured_executable_itself_resolves(tmp_path):
    policy = ExecPolicy(allowed_commands=(sys.executable,))
    assert resolve_executable(sys.executable, policy) == str(Path(sys.executable).resolve())


def test_a_symlink_to_a_forbidden_executable_is_refused(tmp_path):
    """Otherwise the allowlist is a name check, not an identity check."""
    target = tmp_path / "real_tool"
    target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    link = tmp_path / "allowed_name"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/account")

    policy = ExecPolicy(allowed_commands=(str(link),))
    # The link resolves to something that is not itself allowed.
    with pytest.raises(PermissionDenied):
        resolve_executable(str(target), policy)


def test_an_unknown_executable_is_refused(tmp_path):
    policy = ExecPolicy(allowed_commands=(sys.executable,))
    with pytest.raises(PermissionDenied):
        resolve_executable("definitely-not-a-real-binary-xyz", policy)


# --------------------------------------------------------------------------
# Shells
# --------------------------------------------------------------------------


def test_shell_interpreters_are_refused_by_default():
    """An allowed shell is an allowlist with one entry: everything."""
    for shell in ("sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "pwsh"):
        policy = ExecPolicy(allowed_commands=(shell,))
        with pytest.raises(ValueError) as exc:
            policy.validate()
        assert "shell" in str(exc.value).lower()


def test_the_shell_list_covers_the_common_interpreters():
    for expected in ("sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"):
        assert expected in SHELL_INTERPRETERS


def test_a_shell_can_be_permitted_only_by_an_explicit_named_opt_in():
    policy = ExecPolicy(allowed_commands=("bash",), allow_shell_interpreters=True)
    policy.validate()  # does not raise


def test_no_command_is_ever_run_through_a_shell(tmp_path):
    """Shell metacharacters must be inert, not interpreted."""
    marker = tmp_path / "should_not_exist"
    result = _call(
        _policy(tmp_path),
        command=[sys.executable, "-c", "print('ok')", f"; touch {marker}"],
    )
    assert result["exit_code"] == 0
    assert not marker.exists()


# --------------------------------------------------------------------------
# Argument policy
# --------------------------------------------------------------------------


def test_arguments_can_be_restricted_by_pattern(tmp_path):
    policy = _policy(tmp_path, denied_argument_patterns=(r"--unsafe",))
    with pytest.raises(PermissionDenied) as exc:
        _call(policy, command=[sys.executable, "-c", "print(1)", "--unsafe"])
    assert "--unsafe" in str(exc.value)


def test_the_argument_count_is_bounded(tmp_path):
    policy = _policy(tmp_path, max_arguments=3)
    with pytest.raises(PermissionDenied):
        _call(policy, command=[sys.executable, "-c", "print(1)", "a", "b", "c"])


# --------------------------------------------------------------------------
# Working directory confinement
# --------------------------------------------------------------------------


def test_the_working_directory_cannot_escape_the_root(tmp_path):
    policy = _policy(tmp_path)
    for attempt in ("..", "../..", "/etc", "../../../../"):
        with pytest.raises((PermissionDenied, ToolError)):
            _call(policy, command=[sys.executable, "-c", "print(1)"], cwd=attempt)


def test_a_symlinked_working_directory_cannot_escape_the_root(tmp_path):
    outside = tmp_path.parent / "outside_root"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/account")

    policy = _policy(tmp_path, root=str(root))
    with pytest.raises((PermissionDenied, ToolError)):
        _call(policy, command=[sys.executable, "-c", "print(1)"], cwd="escape")


# --------------------------------------------------------------------------
# Bounds the caller cannot raise
# --------------------------------------------------------------------------


def test_a_caller_cannot_extend_the_timeout(tmp_path):
    """The old code took the caller's timeout, so the bound was advisory."""
    policy = _policy(tmp_path, timeout=1.0)
    with pytest.raises(ToolError) as exc:
        _call(
            policy,
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=600,
        )
    assert "timed out" in str(exc.value).lower()


def test_a_caller_may_shorten_the_timeout(tmp_path):
    policy = _policy(tmp_path, timeout=60.0)
    with pytest.raises(ToolError):
        _call(
            policy,
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1,
        )


def test_output_is_capped(tmp_path):
    policy = _policy(tmp_path, max_output_bytes=500)
    result = _call(
        policy,
        command=[sys.executable, "-c", "print('x' * 100000)"],
    )
    assert len(result["stdout"]) <= 600
    assert result["stdout_truncated"] is True


# --------------------------------------------------------------------------
# Configuration must be validated up front
# --------------------------------------------------------------------------


def test_an_empty_allowlist_is_rejected():
    with pytest.raises(ValueError):
        ExecPolicy(allowed_commands=()).validate()

    # Through the tool factory it surfaces as ToolError, which is the
    # established contract callers already handle.
    with pytest.raises(ToolError):
        process_tools(".", allowed_commands=[])


def test_the_normal_path_still_works(tmp_path):
    result = _call(_policy(tmp_path), command=[sys.executable, "-c", "print('hello')"])
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    assert result["stdout_truncated"] is False
