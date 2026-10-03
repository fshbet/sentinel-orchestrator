"""Configuration, plugins, adapters, CLI, and REST API."""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest
from conftest import build_platform, make_config, planning_model, run

from orchestrator.config.loader import (
    Config,
    deep_merge,
    load,
    validate,
    write_default,
)
from orchestrator.errors import ConfigurationError

# -- configuration ---------------------------------------------------------


def test_later_layers_override_earlier_ones():
    merged = deep_merge(
        {"limits": {"max_model_calls": 300, "max_tool_calls": 1000}, "a": 1},
        {"limits": {"max_model_calls": 5}, "b": 2},
    )
    assert merged["limits"]["max_model_calls"] == 5
    assert merged["limits"]["max_tool_calls"] == 1000  # untouched
    assert merged["a"] == 1 and merged["b"] == 2


def test_lists_replace_rather_than_accumulate():
    merged = deep_merge({"rules": [1, 2, 3]}, {"rules": [9]})
    assert merged["rules"] == [9]


def test_project_configuration_is_discovered_and_applied():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        (project / ".orchestrator").mkdir()
        (project / ".orchestrator" / "config.json").write_text(
            json.dumps({"version": 1, "limits": {"max_parallel_tasks": 9}}),
            encoding="utf-8",
        )
        config = load(start=project)
    assert config.get("limits.max_parallel_tasks") == 9
    assert any(".orchestrator" in source for source in config.sources())


def test_invalid_configuration_is_rejected_before_anything_runs():
    with pytest.raises(ConfigurationError) as excinfo:
        validate(
            {
                "version": "one",
                "limits": {"max_model_calls": -5},
                "policy": {"approval_threshold": "extreme", "rules": []},
                "models": {"providers": [{}]},
                "mcp": {"servers": {"bad": {}}},
            }
        )
    message = excinfo.value.message
    assert "version must be an integer" in message
    assert "must not be negative" in message
    assert "approval_threshold" in message
    assert "requires a type" in message
    assert "requires either a command" in message


def test_process_tools_require_an_allow_list_in_configuration():
    with pytest.raises(ConfigurationError, match="allowed_commands"):
        validate(
            {
                "version": 1,
                "tools": {"process": {"enabled": True, "allowed_commands": []}},
            }
        )


def test_zero_is_a_valid_limit_for_countable_budgets():
    validate({"version": 1, "limits": {"max_replans": 0}})
    with pytest.raises(ConfigurationError):
        validate({"version": 1, "limits": {"max_parallel_tasks": 0}})


def test_fingerprint_changes_with_content_and_is_stable_otherwise():
    first = make_config()
    second = make_config()
    third = make_config(limits={"max_parallel_tasks": 7})
    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() != third.fingerprint()


def test_init_writes_a_usable_project_configuration():
    with tempfile.TemporaryDirectory() as directory:
        path = write_default(directory)
        assert path.exists()
        reloaded = load(start=directory)
    assert isinstance(reloaded, Config)
    assert reloaded.get("version") == 1


def test_unknown_provider_type_is_rejected_at_assembly():
    async def scenario():
        config = make_config(models={"providers": [{"type": "nonsense"}]})
        with pytest.raises(ConfigurationError, match="unknown model provider"):
            await build_platform(None, config=config)

    run(scenario())


# -- plugins ---------------------------------------------------------------


def test_a_plugin_can_add_a_tool_and_a_validator():
    with tempfile.TemporaryDirectory() as directory:
        module = Path(directory) / "orchestrator_test_plugin.py"
        module.write_text(
            textwrap.dedent(
                """
                from orchestrator.core.domain.models import ToolSpec
                from orchestrator.validation.validators import Validator


                class AlwaysPasses(Validator):
                    name = "always_passes"

                    async def validate(self, spec, context):
                        return self._result(
                            spec, context, passed=True, message="plugin validator"
                        )


                def register(registry):
                    registry.add_tool(
                        ToolSpec(id="plugin.ping", name="ping", description="pong"),
                        lambda arguments, context: "pong",
                    )
                    registry.add_validator(AlwaysPasses())
                """
            ),
            encoding="utf-8",
        )
        sys.path.insert(0, directory)
        try:

            async def scenario():
                platform = await build_platform(
                    planning_model(),
                    config=make_config(
                        plugins={
                            "enabled": True,
                            "modules": ["orchestrator_test_plugin"],
                            "entry_point_group": "orchestrator.plugins.absent",
                        }
                    ),
                )
                loaded = platform.plugins
                has_tool = platform.tools.has("plugin.ping")
                has_validator = platform.validators.has("always_passes")
                await platform.close()
                return loaded, has_tool, has_validator

            loaded, has_tool, has_validator = run(scenario())
        finally:
            sys.path.remove(directory)
            sys.modules.pop("orchestrator_test_plugin", None)

    assert any(p.name == "orchestrator_test_plugin" and p.ok for p in loaded)
    assert has_tool and has_validator


def test_a_broken_plugin_is_reported_not_fatal():
    async def scenario():
        platform = await build_platform(
            None,
            config=make_config(
                plugins={
                    "enabled": True,
                    "modules": ["definitely_not_a_module_xyz"],
                    "entry_point_group": "orchestrator.plugins.absent",
                }
            ),
        )
        plugins = platform.plugins
        health = await platform.health()
        await platform.close()
        return plugins, health

    plugins, health = run(scenario())
    assert plugins and plugins[0].ok is False
    assert any(not p["loaded"] for p in health["plugins"])


# -- adapters --------------------------------------------------------------


def test_the_subprocess_adapter_runs_an_external_worker():
    from conftest import make_task

    from orchestrator.adapters.execution.subprocess_adapter import SubprocessAdapter
    from orchestrator.agents.runtime import AgentRunContext
    from orchestrator.core.domain.models import AgentSpec, Execution
    from orchestrator.core.policy.engine import PermissionScope

    worker_source = textwrap.dedent(
        """
        import json, sys
        brief = json.load(sys.stdin)
        print(json.dumps({
            "ok": True,
            "summary": "handled " + brief["objective"],
            "output": {"objective": brief["objective"]},
            "artifacts": [{"name": "note", "type": "text", "content": "hi"}],
            "confidence": "confirmed",
        }))
        """
    )
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "worker.py"
        script.write_text(worker_source, encoding="utf-8")

        execution = Execution(objective="overall")
        task = make_task("t")
        task.objective = "do the external thing"
        execution.tasks[task.id] = task
        adapter = SubprocessAdapter([sys.executable, str(script)])

        result = run(
            adapter.run(
                AgentRunContext(
                    execution=execution,
                    task=task,
                    agent=AgentSpec(id="external", runtime="subprocess"),
                    scope=PermissionScope(),
                    workspace=directory,
                )
            )
        )

    assert result.ok is True
    assert "do the external thing" in result.summary
    assert result.artifacts[0].name == "note"
    # An adapter may not certify its own work as confirmed.
    assert result.confidence.value == "likely"


def test_an_adapter_that_does_not_claim_success_is_treated_as_failed():
    from conftest import make_task

    from orchestrator.adapters.execution.base import ExecutionAdapter
    from orchestrator.agents.runtime import AgentRunContext
    from orchestrator.core.domain.models import AgentSpec, Execution
    from orchestrator.core.policy.engine import PermissionScope

    execution = Execution(objective="o")
    task = make_task("t")
    context = AgentRunContext(
        execution=execution,
        task=task,
        agent=AgentSpec(id="a"),
        scope=PermissionScope(),
    )
    result = ExecutionAdapter.parse_result({"summary": "I did stuff"}, context)
    assert result.ok is False


def test_a_failing_external_worker_is_reported_not_raised():
    from conftest import make_task

    from orchestrator.adapters.execution.subprocess_adapter import SubprocessAdapter
    from orchestrator.agents.runtime import AgentRunContext
    from orchestrator.core.domain.models import AgentSpec, Execution
    from orchestrator.core.policy.engine import PermissionScope

    adapter = SubprocessAdapter(["definitely-not-a-real-binary-xyz"])
    execution = Execution(objective="o")
    task = make_task("t")
    result = run(
        adapter.run(
            AgentRunContext(
                execution=execution,
                task=task,
                agent=AgentSpec(id="a"),
                scope=PermissionScope(),
            )
        )
    )
    assert result.ok is False
    assert "could not start" in result.summary


def test_the_openhands_adapter_is_optional_and_configurable():
    from orchestrator.adapters.execution.openhands import build

    adapter = build({"base_url": "http://localhost:3000", "poll_interval": 0.1})
    assert adapter.name == "openhands"
    assert adapter.config.endpoints.create.startswith("/api/")
    # Nothing in the core imports it.
    import orchestrator.core.execution.engine as engine_module

    assert "openhands" not in engine_module.__file__.lower()
    assert not any(
        "openhands" in str(value).lower()
        for value in vars(engine_module)
        if isinstance(value, str)
    )


# -- REST API --------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    from orchestrator.api.app import create_app

    async def build():
        return await build_platform(planning_model())

    platform = run(build())
    return TestClient(create_app(orchestrator=platform)), platform


def test_api_runs_an_objective_and_exposes_the_record():
    client, platform = _client()
    try:
        created = client.post(
            "/v1/executions", json={"objective": "Do the thing.", "run": True}
        )
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "completed"
        assert body["tasks"] and body["validations"]

        fetched = client.get(f"/v1/executions/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == body["id"]

        listed = client.get("/v1/executions")
        assert listed.status_code == 200
        assert listed.json()["executions"]

        audit = client.get(f"/v1/executions/{body['id']}/audit")
        assert audit.json()["events"]
    finally:
        run(platform.close())


def test_api_reports_unknown_executions_as_404():
    client, platform = _client()
    try:
        response = client.get("/v1/executions/exe_missing")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    finally:
        run(platform.close())


def test_api_rejects_an_empty_objective():
    client, platform = _client()
    try:
        response = client.post("/v1/executions", json={"objective": ""})
        assert response.status_code == 422
    finally:
        run(platform.close())


def test_api_exposes_registries_health_and_metrics():
    client, platform = _client()
    try:
        for path in (
            "/v1/tools",
            "/v1/agents",
            "/v1/capabilities",
            "/v1/models",
            "/v1/mcp",
        ):
            assert client.get(path).status_code == 200
        health = client.get("/health")
        assert health.status_code == 200 and health.json()["status"] == "ok"
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "orchestrator_tools" in metrics.text
    finally:
        run(platform.close())


# -- CLI -------------------------------------------------------------------


def _cli_env(directory: Path) -> dict[str, str]:
    config = directory / "config.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "storage": {"backend": "sqlite", "path": str(directory / "state.db")},
                "logging": {"level": "critical"},
                "plugins": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return {"ORCHESTRATOR_CONFIG": str(config)}


def _invoke(args, env=None, cwd=None):
    """Run the CLI as a real subprocess.

    ``cwd`` matters: configuration discovery walks up from the working
    directory, so a test run from inside the repository would silently pick up
    the project's own .orchestrator/config.yaml. Passing an isolated directory
    is what keeps these tests hermetic.

    The typer guard lives here rather than on each test: every CLI test routes
    through this helper, so one check covers the ones below and the ones nobody
    has written yet. Without it an install that skipped the ``cli`` extra fails
    these tests instead of skipping them, which says nothing about the code.
    """
    import importlib.util
    import os
    import subprocess

    if importlib.util.find_spec("typer") is None:
        pytest.skip("the CLI needs typer: pip install universal-orchestrator[cli]")

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "orchestrator.cli.main", *args],
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
        timeout=120,
    )


def test_cli_lists_tools_as_json():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        result = _invoke(["tools", "--json"], _cli_env(Path(directory)), cwd=directory)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert any(tool["id"].startswith("orchestrator.") for tool in payload)


def test_cli_validate_reports_a_missing_model_provider():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        result = _invoke(["validate", "--json"], _cli_env(Path(directory)), cwd=directory)
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert any("model providers" in problem for problem in payload["problems"])
    assert result.returncode == 2


def test_cli_init_creates_a_project_configuration():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        result = _invoke(["init", directory, "--json"], cwd=directory)
        assert result.returncode == 0, result.stderr
        created = Path(json.loads(result.stdout)["created"])
        assert created.exists()


def test_cli_health_is_machine_readable():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        result = _invoke(["health", "--json"], _cli_env(Path(directory)), cwd=directory)
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert "config_fingerprint" in payload


def test_cli_reports_an_unknown_execution_cleanly():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        result = _invoke(
            ["status", "exe_missing"], _cli_env(Path(directory)), cwd=directory
        )
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_cli_status_lists_executions():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        env = _cli_env(Path(directory))
        result = _invoke(["status", "--json"], env, cwd=directory)
    assert result.returncode == 0
    assert json.loads(result.stdout) == []
