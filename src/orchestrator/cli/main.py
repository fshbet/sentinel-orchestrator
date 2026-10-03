"""Command-line interface.

Human-readable by default, machine-readable with ``--json`` (spec section 85).
The CLI is a thin shell over ``Orchestrator``; it contains no orchestration
logic of its own.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from typing import Any

try:
    import typer
except ImportError:  # pragma: no cover - environment dependent
    typer = None  # type: ignore[assignment]

from datetime import UTC

from ..config.loader import load, write_default
from ..core.domain.enums import OrchestrationPattern, PlanStrategy
from ..errors import OrchestratorError
from ..platform import Orchestrator

if typer is not None:
    app = typer.Typer(
        name="orchestrator",
        help="Domain-agnostic AI orchestration platform.",
        no_args_is_help=True,
        add_completion=False,
    )
else:  # pragma: no cover - allows import without typer installed
    app = None


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def _echo(message: str) -> None:
    print(message)


def _emit(payload: Any, as_json: bool, renderer=None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif renderer is not None:
        renderer(payload)
    else:
        print(json.dumps(payload, indent=2, default=str))


def _fail(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


async def _build(
    config_path: str | None = None,
    *,
    workspace: str | None = None,
    connect_mcp: bool = True,
) -> Orchestrator:
    config = load(paths=[config_path] if config_path else None)
    return await Orchestrator.create(
        config=config, workspace=workspace, connect_mcp=connect_mcp
    )


def _run(coro):
    try:
        return asyncio.run(coro)
    except OrchestratorError as exc:
        _fail(exc.message)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        _fail("interrupted", code=130)


def _execution_summary(execution) -> dict[str, Any]:
    return {
        "id": execution.id,
        "status": execution.status.value,
        "confidence": execution.confidence.value,
        "objective": execution.objective,
        "tasks": {
            "total": len(execution.tasks),
            "succeeded": sum(
                1 for t in execution.tasks.values() if t.status.value == "succeeded"
            ),
            "failed": sum(
                1 for t in execution.tasks.values() if t.status.value == "failed"
            ),
        },
        "artifacts": [a.name for a in execution.artifacts],
        "pending_approvals": [
            {"id": a.id, "prompt": a.prompt, "reason": a.reason.value}
            for a in execution.approvals
            if a.status.value == "pending"
        ],
        "summary": execution.summary,
        "usage": execution.usage.to_dict(),
    }


def _print_execution(payload: dict[str, Any]) -> None:
    _echo(f"execution {payload['id']}")
    _echo(f"  status     {payload['status']} ({payload['confidence']})")
    _echo(f"  objective  {payload['objective'][:100]}")
    tasks = payload["tasks"]
    _echo(
        f"  tasks      {tasks['succeeded']}/{tasks['total']} succeeded"
        + (f", {tasks['failed']} failed" if tasks["failed"] else "")
    )
    if payload["artifacts"]:
        _echo(f"  artifacts  {', '.join(payload['artifacts'])}")
    for approval in payload["pending_approvals"]:
        _echo(f"  APPROVAL NEEDED [{approval['id']}] {approval['prompt'][:200]}")
    if payload["summary"]:
        _echo("  summary:")
        for line in payload["summary"].splitlines():
            _echo(f"    {line}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

if typer is not None:

    @app.command()
    def init(
        directory: str = typer.Argument(".", help="Project directory."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Create a project configuration."""
        path = write_default(directory)
        _emit(
            {"created": str(path)},
            as_json,
            lambda p: _echo(f"wrote {p['created']}"),
        )

    @app.command()
    def run(
        objective: str = typer.Argument(..., help="What to accomplish."),
        config: str | None = typer.Option(None, "--config", "-c"),
        workspace: str | None = typer.Option(None, "--workspace", "-w"),
        pattern: str | None = typer.Option(
            None, "--pattern", help="Force an orchestration pattern."
        ),
        strategy: str | None = typer.Option(
            None, "--strategy", help="Force a planning strategy."
        ),
        detach: bool = typer.Option(
            False, "--detach", help="Create the execution without running it."
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Run an objective."""

        async def main():
            orchestrator = await _build(config, workspace=workspace)
            try:
                kwargs: dict[str, Any] = {}
                if pattern:
                    kwargs["pattern"] = OrchestrationPattern(pattern)
                if strategy:
                    kwargs["plan_strategy"] = PlanStrategy(strategy)
                if detach:
                    execution = await orchestrator.start(objective, **kwargs)
                else:
                    execution = await orchestrator.run(objective, **kwargs)
                return _execution_summary(execution)
            finally:
                await orchestrator.close()

        payload = _run(main())
        _emit(payload, as_json, _print_execution)
        if payload["status"] in ("failed",):
            raise SystemExit(2)

    @app.command()
    def status(
        execution_id: str | None = typer.Argument(None),
        config: str | None = typer.Option(None, "--config", "-c"),
        limit: int = typer.Option(20, "--limit"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show one execution, or list recent ones."""

        async def main():
            orchestrator = await _build(config, connect_mcp=False)
            try:
                if execution_id:
                    return _execution_summary(await orchestrator.status(execution_id))
                return [s.to_dict() for s in await orchestrator.list(limit=limit)]
            finally:
                await orchestrator.close()

        payload = _run(main())

        def render(data):
            if isinstance(data, dict):
                _print_execution(data)
                return
            if not data:
                _echo("no executions")
                return
            for row in data:
                _echo(
                    f"{row['id']}  {row['status']:<10} {row['updated_at'][:19]}  "
                    f"{row['objective'][:60]}"
                )

        _emit(payload, as_json, render)

    @app.command()
    def inspect(
        execution_id: str = typer.Argument(...),
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show the full task graph and validation record."""

        async def main():
            orchestrator = await _build(config, connect_mcp=False)
            try:
                execution = await orchestrator.status(execution_id)
                return {
                    **_execution_summary(execution),
                    "plan": {
                        "version": execution.plan_version,
                        "pattern": execution.plan.pattern.value if execution.plan else None,
                        "strategy": execution.plan.strategy.value
                        if execution.plan
                        else None,
                        "rationale": execution.plan.rationale if execution.plan else "",
                    },
                    "requirements": execution.requirements.to_dict(),
                    "graph": [
                        {
                            "id": t.id,
                            "name": t.name,
                            "status": t.status.value,
                            "depends_on": t.dependencies,
                            "agent": t.assigned_agent,
                            "model": t.assigned_model,
                            "attempts": t.attempts,
                            "summary": t.result.summary if t.result else "",
                        }
                        for t in execution.tasks.values()
                    ],
                    "validations": [
                        {
                            "validator": v.validator,
                            "target": v.target_id,
                            "passed": v.passed,
                            "mandatory": v.mandatory,
                            "confidence": v.confidence.value,
                            "message": v.message,
                        }
                        for v in execution.validations
                    ],
                    "failures": [
                        {
                            "category": f.category.value,
                            "code": f.code,
                            "message": f.message,
                            "recovery": f.recovery.value if f.recovery else None,
                            "recovered": f.recovered,
                        }
                        for f in execution.failures
                    ],
                }
            finally:
                await orchestrator.close()

        def render(data):
            _print_execution(data)
            _echo("  graph:")
            for node in data["graph"]:
                deps = f" <- {', '.join(node['depends_on'])}" if node["depends_on"] else ""
                _echo(
                    f"    [{node['status']:<9}] {node['name']}{deps}"
                    + (f"  ({node['agent']})" if node["agent"] else "")
                )
            if data["validations"]:
                _echo("  validations:")
                for v in data["validations"]:
                    mark = "PASS" if v["passed"] else "FAIL"
                    _echo(
                        f"    {mark} {v['validator']} ({v['confidence']}) {v['message'][:80]}"
                    )
            if data["failures"]:
                _echo("  failures:")
                for f in data["failures"]:
                    _echo(f"    [{f['category']}] {f['message'][:80]} -> {f['recovery']}")

        _emit(_run(main()), as_json, render)

    @app.command()
    def audit(
        execution_id: str = typer.Argument(...),
        config: str | None = typer.Option(None, "--config", "-c"),
        after: int = typer.Option(0, "--after", help="Only events after this sequence."),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Print the structured audit trail."""

        async def main():
            orchestrator = await _build(config, connect_mcp=False)
            try:
                events = await orchestrator.audit(execution_id, after=after)
                return [e.to_dict() for e in events]
            finally:
                await orchestrator.close()

        def render(events):
            for event in events:
                _echo(
                    f"{event['sequence']:>4} {event['timestamp'][11:19]} "
                    f"{event['type']:<26} "
                    + json.dumps(event["payload"], default=str)[:120]
                )

        _emit(_run(main()), as_json, render)

    @app.command()
    def pause(
        execution_id: str = typer.Argument(...),
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Request a graceful pause."""

        async def main():
            orchestrator = await _build(config, connect_mcp=False)
            try:
                return _execution_summary(await orchestrator.pause(execution_id))
            finally:
                await orchestrator.close()

        _emit(_run(main()), as_json, _print_execution)

    @app.command()
    def resume(
        execution_id: str = typer.Argument(...),
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Resume a paused or waiting execution."""

        async def main():
            orchestrator = await _build(config)
            try:
                return _execution_summary(await orchestrator.resume(execution_id))
            finally:
                await orchestrator.close()

        _emit(_run(main()), as_json, _print_execution)

    @app.command()
    def cancel(
        execution_id: str = typer.Argument(...),
        reason: str = typer.Option("", "--reason"),
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Cancel an execution gracefully."""

        async def main():
            orchestrator = await _build(config, connect_mcp=False)
            try:
                return _execution_summary(
                    await orchestrator.cancel(execution_id, reason=reason)
                )
            finally:
                await orchestrator.close()

        _emit(_run(main()), as_json, _print_execution)

    @app.command()
    def approve(
        execution_id: str = typer.Argument(...),
        approval_id: str = typer.Argument(...),
        reject: bool = typer.Option(False, "--reject"),
        response: str | None = typer.Option(None, "--response"),
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Answer a pending approval and continue."""

        async def main():
            orchestrator = await _build(config)
            try:
                return _execution_summary(
                    await orchestrator.approve(
                        execution_id,
                        approval_id,
                        approved=not reject,
                        response=response,
                        responder="cli",
                    )
                )
            finally:
                await orchestrator.close()

        _emit(_run(main()), as_json, _print_execution)

    @app.command()
    def agents(
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List registered agents."""
        _list_section(config, "agents", as_json)

    @app.command()
    def capabilities(
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List registered capabilities."""
        _list_section(config, "capabilities", as_json)

    @app.command()
    def tools(
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List available tools and their permissions."""
        _list_section(config, "tools", as_json)

    @app.command()
    def models(
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List models and their advertised capabilities."""
        _list_section(config, "models", as_json, connect_mcp=False)

    @app.command()
    def skills(
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List registered skills and what they apply to."""
        _list_section(config, "skills", as_json, connect_mcp=False)

    @app.command()
    def workflows(
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """List registered workflow definitions."""
        _list_section(config, "workflows", as_json, connect_mcp=False)

    @app.command()
    def mcp(
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Show MCP server health, capabilities, and authorisation state."""

        async def main():
            orchestrator = await _build(config)
            try:
                if orchestrator.mcp is None:
                    return []
                return await orchestrator.mcp.health()
            finally:
                await orchestrator.close()

        def render(servers):
            if not servers:
                _echo("no MCP servers configured")
                return
            for server in servers:
                _echo(f"{server['server']}  {server.get('status', 'unknown')}")
                _echo(f"  transport  {server.get('transport')} {server.get('target', '')}")
                _echo(f"  protocol   {server.get('protocol_version')}")
                _echo(
                    f"  registered {', '.join(server.get('tools_registered', [])) or '-'}"
                )
                for denied in server.get("tools_denied", []):
                    _echo(f"  DENIED     {denied['tool']}: {denied['reason'][:80]}")
                if server.get("error"):
                    _echo(f"  error      {server['error']}")

        _emit(_run(main()), as_json, render)

    @app.command()
    def health(
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Report platform, model, and MCP health."""

        async def main():
            orchestrator = await _build(config)
            try:
                return await orchestrator.health()
            finally:
                await orchestrator.close()

        def render(report):
            _echo(f"status      {report['status']}")
            _echo(f"config      {', '.join(report['config_sources'])}")
            _echo(f"storage     {report['storage']}")
            _echo(
                f"registry    {report['tools']} tools, {report['agents']} agents, "
                f"{report['capabilities']} capabilities"
            )
            for model in report["models"]:
                mark = "up" if model["available"] else "down"
                _echo(
                    f"model       {model['provider']:<20} {mark} {model.get('error') or ''}"
                )
            for server in report["mcp"]:
                _echo(f"mcp         {server['server']:<20} {server.get('status')}")
            for plugin in report["plugins"]:
                if not plugin["loaded"]:
                    _echo(f"plugin      {plugin['name']} FAILED {plugin['error']}")

        _emit(_run(main()), as_json, render)

    @app.command()
    def validate(
        config: str | None = typer.Option(None, "--config", "-c"),
        isolated: bool = typer.Option(
            False,
            "--isolated",
            help="Check only the named file, ignoring discovered configs. "
            "Use this to check a deployment config from a developer "
            "machine, where an ambient .orchestrator/config.yaml would "
            "otherwise be merged in and change the answer.",
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Validate configuration, agents, tools, and workflows without running."""

        async def main():
            problems: list[str] = []
            cfg = load(
                paths=[config] if config else None,
                include_discovered=not isolated,
            )

            # Validation answers "is this configuration valid", not "is the
            # database up". Building the real store would make `validate`
            # useless for checking a production config from a laptop, or
            # before the database exists — which is exactly when it is most
            # wanted. The configured backend is reported separately, along
            # with whether its DSN variable is set.
            backend = str(cfg.get("storage.backend", "sqlite")).lower()
            storage_note = {"backend": backend}
            if backend == "postgres":
                import os as _os

                section = cfg.section("storage").get("postgres", {}) or {}
                variable = str(section.get("dsn_env", "ORCHESTRATOR_POSTGRES_DSN"))
                storage_note["dsn_env"] = variable
                storage_note["dsn_env_set"] = bool(_os.environ.get(variable, "").strip())
                if not storage_note["dsn_env_set"]:
                    problems.append(
                        f"storage.backend is postgres but {variable} is not set "
                        f"in this environment (the configuration itself is valid)"
                    )

            checking = cfg
            if backend != "memory":
                # Everything below inspects registries, not state, so an
                # in-memory store answers the same questions without a
                # connection.
                from ..config.loader import Config

                document = cfg.raw if hasattr(cfg, "raw") else cfg.to_dict()
                document = copy.deepcopy(document)
                document.setdefault("storage", {})["backend"] = "memory"
                checking = Config(document)

            orchestrator = await Orchestrator.create(config=checking, connect_mcp=False)
            try:
                described = orchestrator.describe()
                known_capabilities = {c["id"] for c in described["capabilities"]}
                known_capabilities |= {
                    c for a in described["agents"] for c in a["capabilities"]
                }
                for agent in described["agents"]:
                    for capability in agent["capabilities"]:
                        if capability not in known_capabilities:
                            problems.append(
                                f"agent {agent['id']} advertises unknown capability"
                                f" {capability}"
                            )
                tool_ids = {t["id"] for t in described["tools"]}
                for definition in orchestrator.workflows.list():
                    for step in definition.steps:
                        for tool in step.tools:
                            if tool not in tool_ids:
                                problems.append(
                                    f"workflow {definition.id} step '{step.name}'"
                                    f" references unknown tool {tool}"
                                )
                        for spec in step.validations:
                            if not orchestrator.validators.has(spec.validator):
                                problems.append(
                                    f"workflow {definition.id} step '{step.name}'"
                                    f" references unknown validator {spec.validator}"
                                )
                    try:
                        definition.build("validate")
                    except OrchestratorError as exc:
                        problems.append(f"workflow {definition.id}: {exc.message}")
                for agent in described["agents"]:
                    for skill_id in agent.get("skills", []):
                        if not orchestrator.skills.has(skill_id):
                            problems.append(
                                f"agent {agent['id']} declares unknown skill {skill_id}"
                            )
                if not described["models"]:
                    problems.append(
                        "no model providers are configured; planning and agent work"
                        " will fall back to deterministic behaviour only"
                    )
                for plugin in orchestrator.plugins:
                    if not plugin.ok:
                        problems.append(f"plugin {plugin.name}: {plugin.error}")
                # Surfaced separately from problems: a config that relies on
                # a default which has since tightened is not invalid, but it
                # will behave differently, and finding that out from a failed
                # run is the worst way to find it out.
                notices = [n.describe() for n in cfg.migration_notices()]
                return {
                    "valid": not problems,
                    "problems": problems,
                    "profile": cfg.profile.name,
                    "posture": cfg.posture(),
                    "storage": storage_note,
                    "migration_notices": notices,
                    "config_sources": cfg.sources(),
                    "fingerprint": cfg.fingerprint(),
                }
            finally:
                await orchestrator.close()

        payload = _run(main())

        def render(data):
            if data["valid"]:
                _echo("configuration is valid")
            else:
                _echo("problems found:")
                for problem in data["problems"]:
                    _echo(f"  - {problem}")

            posture = data["posture"]
            _echo("")
            _echo(f"profile: {data['profile']} - {posture['summary']}")
            _echo(
                f"  tools denied unless granted: "
                f"{posture['policy']['require_explicit_tool_grant']}"
            )
            http = posture["http_tools"]
            if http["enabled"]:
                _echo(
                    f"  http egress: {len(http['allowed_hosts'])} host(s), "
                    f"{'https only' if http['https_only'] else 'HTTP ALLOWED'}, "
                    f"methods {', '.join(http['allowed_methods'])}"
                )
            else:
                _echo("  http egress: disabled")
            if posture["process_tools_enabled"]:
                _echo("  process execution: ENABLED (privileged)")

            storage = data["storage"]
            line = f"  storage: {storage['backend']}"
            if storage["backend"] == "postgres":
                state = "set" if storage["dsn_env_set"] else "NOT SET"
                line += f" (dsn from {storage['dsn_env']}: {state})"
            _echo(line)

            if data["migration_notices"]:
                _echo("")
                _echo("defaults that changed and this config does not set:")
                for notice in data["migration_notices"]:
                    _echo(f"  - {notice}")
                _echo(
                    "  Declare `profile: development` to keep the previous "
                    "permissive behaviour, or set these explicitly."
                )

            _echo("")
            _echo(f"sources: {', '.join(data['config_sources'])}")

        _emit(payload, as_json, render)
        if not payload["valid"]:
            raise SystemExit(2)

    @app.command()
    def evaluate(
        suite: str | None = typer.Option(
            None,
            "--suite",
            help="One of: planning, tool_selection, policy, injection, egress, "
            "validation, recovery. Omit to run everything.",
        ),
        report: str | None = typer.Option(
            None, "--report", help="Write JSON and Markdown reports to this path."
        ),
        min_quality: float | None = typer.Option(
            None,
            "--min-quality",
            help="Fail if the non-gating pass rate falls below this (0-1).",
        ),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Run the deterministic evaluation suite.

        Exits non-zero when a security or policy case fails. Quality cases are
        reported, and only gate when --min-quality is given: a moving quality
        score is information, not a verdict.

        This proves the platform's handling is unchanged. It does not prove AI
        output is correct — see docs/evaluation.md.
        """
        from ..evaluation import run_suite
        from ..evaluation.runner import write_reports

        try:
            result = run_suite(suite)
        except ValueError as exc:
            _fail(str(exc))

        if report:
            written = write_reports(result, report)
            if not as_json:
                _echo(f"wrote {written['json']} and {written['markdown']}")

        def render(payload):
            totals = payload["totals"]
            _echo(
                f"{totals['passed']}/{totals['cases']} passed"
                + (f", {totals['failed']} failed" if totals["failed"] else "")
                + (f", {totals['errors']} errored" if totals["errors"] else "")
            )
            for name, counts in payload["by_suite"].items():
                mark = "ok" if counts["passed"] == counts["total"] else "FAILED"
                _echo(f"  {name:<16} {counts['passed']}/{counts['total']}  {mark}")
            if payload["blocking_failures"]:
                _echo("")
                _echo("BLOCKING failures (security or policy):")
                for entry in payload["results"]:
                    if entry["id"] in payload["blocking_failures"]:
                        _echo(f"  {entry['id']}: {entry['detail']}")
            _echo("")
            _echo(f"quality score: {payload['quality_score']:.1%} (non-gating cases)")

        _emit(result.to_dict(), as_json, render)

        if not result.ok:
            raise SystemExit(2)
        if min_quality is not None and result.quality_score < min_quality:
            _fail(
                f"quality score {result.quality_score:.1%} is below the "
                f"required {min_quality:.1%}",
                code=3,
            )

    @app.command()
    def migrate(
        apply: bool = typer.Option(
            False, "--apply", help="Apply pending migrations. Default is status only."
        ),
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Inspect or apply schema migrations.

        Status by default. Applying is idempotent, so running it twice, or on
        two instances at once, converges rather than conflicting.
        """

        async def _run_migrate():
            from ..core.state import migrations as m

            orchestrator = await _build(config, connect_mcp=False)
            try:
                store = orchestrator.store
                if not hasattr(store, "applied_migrations"):
                    return {
                        "backend": type(store).__name__,
                        "supported": False,
                        "message": (
                            "this backend has no schema to migrate (in-memory "
                            "state is rebuilt on every start)"
                        ),
                    }
                applied = await store.applied_migrations()
                payload = {
                    "backend": type(store).__name__,
                    "supported": True,
                    **m.describe(applied),
                }
                if apply and payload["pending"]:
                    result = await store.migrate()
                    payload["applied_now"] = result["applied"]
                    payload.update(result["state"])
                return payload
            finally:
                await orchestrator.close()

        result = _run(_run_migrate())

        def render(payload):
            if not payload.get("supported"):
                _echo(f"{payload['backend']}: {payload['message']}")
                return
            _echo(f"backend: {payload['backend']}")
            _echo(
                f"schema:  version {payload['current_version']} "
                f"of {payload['target_version']}"
            )
            for entry in payload["migrations"]:
                mark = "applied" if entry["applied"] else "PENDING"
                _echo(f"  {entry['version']:>3}  {entry['name']:<28} {mark}")
            if payload.get("applied_now"):
                _echo("")
                _echo(f"applied: {', '.join(payload['applied_now'])}")
            elif payload["pending"]:
                _echo("")
                _echo("Re-run with --apply to apply the pending migrations.")
            else:
                _echo("")
                _echo("up to date")

        _emit(result, as_json, render)

    @app.command()
    def prune(
        older_than_days: int = typer.Option(
            90,
            "--older-than-days",
            help="Delete finished executions last updated before this many days ago.",
        ),
        dry_run: bool = typer.Option(
            True,
            "--dry-run/--apply",
            help="Report what would be deleted. Defaults to a dry run.",
        ),
        vacuum: bool = typer.Option(
            False, "--vacuum", help="Return freed space to the filesystem afterwards."
        ),
        config: str | None = typer.Option(None, "--config", "-c"),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Delete old finished executions and their audit trails.

        Only completed, failed, and cancelled runs are eligible. Anything
        still running, paused, or waiting on a person is kept regardless of
        age. Defaults to a dry run: deleting audit history is not something
        to do by mistyping a flag.
        """

        async def _run_prune():
            orchestrator = await _build(config, connect_mcp=False)
            try:
                store = orchestrator.store
                if not hasattr(store, "prune"):
                    _fail("this state store does not support retention")
                if dry_run:
                    # Count without deleting by pruning a copy of nothing:
                    # list eligible rows through the store's own rules.
                    from datetime import datetime, timedelta

                    from ..core.state.sqlite_store import PRUNABLE_STATUSES

                    cutoff = (
                        datetime.now(UTC) - timedelta(days=older_than_days)
                    ).isoformat()
                    # Only "?" characters are interpolated; the statuses
                    # themselves are bound parameters below.
                    placeholders = ",".join("?" for _ in PRUNABLE_STATUSES)
                    rows = store._conn.execute(
                        f"SELECT status, COUNT(*) FROM executions "  # noqa: S608  # nosec B608
                        f"WHERE updated_at < ? AND status IN ({placeholders}) "
                        f"GROUP BY status",
                        (cutoff, *PRUNABLE_STATUSES),
                    ).fetchall()
                    return {
                        "dry_run": True,
                        "cutoff": cutoff,
                        "would_remove": {row[0]: row[1] for row in rows},
                        "total": sum(row[1] for row in rows),
                    }
                report = await store.prune(older_than_days=older_than_days)
                if vacuum:
                    await store.vacuum()
                report.pop("ids", None)
                report["dry_run"] = False
                return report
            finally:
                await orchestrator.close()

        result = _run(_run_prune())

        def render(payload):
            if payload["dry_run"]:
                total = payload["total"]
                if not total:
                    typer.echo("Nothing is old enough to remove.")
                    return
                typer.echo(f"Would remove {total} execution(s) and their audit trails:")
                for status, count in sorted(payload["would_remove"].items()):
                    typer.echo(f"  {count:>6}  {status}")
                typer.echo("")
                typer.echo("Re-run with --apply to delete them.")
            else:
                typer.echo(
                    f"Removed {payload['executions_removed']} execution(s), "
                    f"{payload['audit_events_removed']} audit event(s), "
                    f"{payload['idempotency_keys_removed']} idempotency key(s)."
                )

        _emit(result, as_json, render)

    @app.command()
    def serve(
        host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(8080, "--port"),
        config: str | None = typer.Option(None, "--config", "-c"),
        cors_origin: list[str] = typer.Option(
            [],
            "--cors-origin",
            help="Allow browser calls from this origin. Repeatable. "
            "Unnecessary when using the console served at /.",
        ),
        insecure_no_auth: bool = typer.Option(
            False,
            "--i-have-my-own-authentication",
            help="Bind a non-loopback address without a token. Only when "
            "another layer in front of this process authenticates callers.",
        ),
    ) -> None:
        """Run the REST API and web console.

        Authentication comes from ORCHESTRATOR_API_TOKEN (comma-separated to
        rotate). Binding anything but loopback without one is refused: this
        API starts executions that run tools.
        """
        try:
            import uvicorn
        except ImportError:
            _fail(
                "the API requires fastapi and uvicorn: pip install universal-orchestrator[api]"
            )
        from ..api.app import create_app
        from ..api.security import InsecureBinding, SecurityConfig

        security = SecurityConfig.from_env(
            host=host,
            allowed_origins=tuple(cors_origin),
            allow_unauthenticated_network_access=insecure_no_auth,
        )
        # Built here rather than inside create_app so the startup banner can
        # report the posture that will actually be enforced. Reporting only
        # `security.tokens` printed "auth: none" for a deployment authenticated
        # by api.principals — the message said unauthenticated while the API
        # was in fact requiring credentials.
        # Deliberately does NOT pre-build the registry. create_app chooses
        # between the in-process store and the shared PostgreSQL one based on
        # storage.backend; passing one in here overrode that choice and made
        # the container use per-process tokens while the config asked for
        # shared state. The banner reads back what create_app built.
        try:
            application = create_app(config_path=config, security=security)
        except InsecureBinding as exc:
            _fail(str(exc))

        identities = getattr(application.state, "identities", None)

        principal_count = len(identities) if identities is not None else 0
        shared = bool(getattr(getattr(identities, "tokens", None), "distributed", False))
        if principal_count and security.enabled:
            summary = f"{principal_count} principal(s) + legacy ORCHESTRATOR_API_TOKEN"
        elif principal_count:
            summary = f"bearer token, {principal_count} principal(s)"
        elif security.enabled:
            summary = "bearer token (legacy ORCHESTRATOR_API_TOKEN)"
        else:
            summary = "none"

        typer.echo(
            f"orchestrator serving on http://{host}:{port}  (auth: {summary}"
            + (", shared token state" if shared else "")
            + ")"
        )
        if summary == "none":
            typer.echo(
                "  loopback only, no token required — set ORCHESTRATOR_API_TOKEN "
                "or configure api.principals before exposing this to a network."
            )
        elif identities is not None:
            for principal in identities.principals():
                typer.echo(f"    {principal.id:<16} {', '.join(sorted(principal.scopes))}")
        typer.echo(f"  console: http://{host}:{port}/")

        uvicorn.run(application, host=host, port=port)

    @app.command("mcp-serve")
    def mcp_serve(
        config: str | None = typer.Option(None, "--config", "-c"),
        allow_control: bool = typer.Option(
            False,
            "--allow-control",
            help="Expose cancel and approval-response tools as well as read-only ones.",
        ),
    ) -> None:
        """Expose orchestration capabilities to MCP clients over stdio."""
        from ..mcp.server import serve_stdio

        _run(serve_stdio(config_path=config, allow_control=allow_control))

    def _list_section(
        config: str | None, section: str, as_json: bool, *, connect_mcp: bool = True
    ) -> None:
        async def main():
            orchestrator = await _build(config, connect_mcp=connect_mcp)
            try:
                return orchestrator.describe()[section]
            finally:
                await orchestrator.close()

        def render(rows):
            if not rows:
                _echo(f"no {section} registered")
                return
            for row in rows:
                primary = row.get("id", "")
                extra = {k: v for k, v in row.items() if k != "id"}
                _echo(f"{primary}")
                for key, value in extra.items():
                    if value in (None, "", [], {}):
                        continue
                    rendered = (
                        ", ".join(map(str, value)) if isinstance(value, list) else value
                    )
                    _echo(f"    {key}: {rendered}")

        _emit(_run(main()), as_json, render)


def main() -> None:
    if typer is None:  # pragma: no cover - environment dependent
        _fail("the CLI requires typer: pip install universal-orchestrator[cli]")
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
