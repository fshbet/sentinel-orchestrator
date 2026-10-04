"""Metrics observed through /metrics after real flows.

`test_metrics.py` proves the collector works when called. That is not the same
claim as "the platform calls it" — the collector previously existed, was fully
tested, and was wired to nothing, so every counter read zero in production
while the unit tests passed.

Every test here drives a real path (an HTTP request, a tool call, an
execution) and then scrapes `/metrics` or reads the orchestrator's own
collector. Nothing calls a `Metrics` method directly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("fastapi", reason="the API requires fastapi")

from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.identity import (
    ADMIN,
    EXECUTIONS_READ,
    IdentityRegistry,
    Principal,
    TokenPrincipal,
    expand_scopes,
)
from orchestrator.api.ratelimit import InMemoryRateLimiter, RateLimit
from orchestrator.api.security import SecurityConfig, install
from orchestrator.core.policy.engine import PermissionScope
from orchestrator.observability.metrics import Metrics


def _full_scope():
    """A scope wide enough to run a bookkeeping tool."""
    return PermissionScope(
        permissions=("memory.write", "memory.read", "state.read", "artifact.write"),
        tools=("*",),
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _build_orchestrator(collector):
    """An orchestrator with in-memory state and no live model providers.

    Injected rather than letting create_app() load the ambient config: that
    config points at real Ollama endpoints, so the tests would depend on a
    running model server and take minutes.
    """
    from conftest import make_config

    from orchestrator.platform import Orchestrator

    # A development profile: make_config() declares none, so it would
    # otherwise resolve to production and deny every tool. Correct platform
    # behaviour, wrong fixture for exercising tool metrics.
    return asyncio.run(
        Orchestrator.create(
            config=make_config(profile="development"),
            connect_mcp=False,
            metrics=collector,
        )
    )


def _app(collector, orchestrator=None, **kwargs):
    registry = IdentityRegistry(
        [
            TokenPrincipal(
                "admin-token",
                Principal(id="admin", scopes=expand_scopes([ADMIN]), token_id="ENV_ADMIN"),
            ),
            TokenPrincipal(
                "reader-token",
                Principal(
                    id="reader",
                    scopes=expand_scopes([EXECUTIONS_READ]),
                    token_id="ENV_READER",
                ),
            ),
        ]
    )
    return create_app(
        security=SecurityConfig(host="0.0.0.0", tokens=("unused",)),
        identity_registry=registry,
        metrics_collector=collector,
        orchestrator=orchestrator
        if orchestrator is not None
        else _build_orchestrator(collector),
        **kwargs,
    )


def _scrape(client) -> str:
    response = client.get("/metrics", headers=_auth("admin-token"))
    assert response.status_code == 200, response.text
    return response.text


def _series(body: str, name: str) -> float:
    """Sum every sample of a metric family in an exposition body."""
    total = 0.0
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        metric, _, value = line.rpartition(" ")
        if metric == name or metric.startswith(name + "{"):
            total += float(value)
    return total


# --------------------------------------------------------------------------
# API-level signals, observed through the endpoint
# --------------------------------------------------------------------------


def test_an_authentication_failure_shows_up_in_metrics():
    collector = Metrics()
    client = TestClient(_app(collector))

    before = _series(_scrape(client), "orchestrator_security_events_total")
    client.get("/v1/executions")  # no credential
    client.get("/v1/executions", headers=_auth("wrong"))
    after = _series(_scrape(client), "orchestrator_security_events_total")

    assert after >= before + 2
    assert 'reason="authentication"' in _scrape(client)


def test_an_authorization_failure_shows_up_in_metrics():
    collector = Metrics()
    client = TestClient(_app(collector))

    client.post(
        "/v1/executions",
        headers=_auth("reader-token"),
        json={"objective": "x", "run": False},
    )
    body = _scrape(client)
    assert 'orchestrator_security_events_total{reason="authorization"}' in body


def test_a_rate_limit_event_shows_up_in_metrics():
    from fastapi import FastAPI

    collector = Metrics()
    app = FastAPI()
    install(
        app,
        SecurityConfig(host="127.0.0.1"),
        rate_limiter=InMemoryRateLimiter(),
        rate_limit=RateLimit(requests=1, window_seconds=60),
        metrics=collector,
    )

    @app.get("/v1/ping")
    async def ping():
        return {"ok": True}

    client = TestClient(app)
    client.get("/v1/ping")
    limited = client.get("/v1/ping")
    assert limited.status_code == 429
    assert 'reason="rate_limit"' in collector.render()


def test_an_oversized_body_is_refused_and_counted():
    """With a limit small enough that the request genuinely exceeds it."""
    collector = Metrics()
    registry = IdentityRegistry(
        [
            TokenPrincipal(
                "admin-token",
                Principal(id="admin", scopes=expand_scopes([ADMIN]), token_id="ENV_ADMIN"),
            ),
        ]
    )
    client = TestClient(
        create_app(
            security=SecurityConfig(host="0.0.0.0", tokens=("unused",), max_body_bytes=500),
            identity_registry=registry,
            metrics_collector=collector,
            orchestrator=_build_orchestrator(collector),
        )
    )
    refused = client.post(
        "/v1/executions",
        headers=_auth("admin-token"),
        json={"objective": "x" * 50_000, "run": False},
    )
    assert refused.status_code == 413
    assert "orchestrator_security_events_total" in _scrape(client)


def test_a_delete_with_a_body_is_also_bounded():
    """DELETE can carry a body and was previously unbounded."""
    from fastapi import FastAPI

    app = FastAPI()
    install(app, SecurityConfig(host="127.0.0.1", max_body_bytes=200))

    @app.delete("/v1/thing")
    async def remove():
        return {"ok": True}

    client = TestClient(app)
    assert (
        client.request(
            "DELETE",
            "/v1/thing",
            content=b"x" * 5000,
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    # A small body still works.
    assert client.request("DELETE", "/v1/thing", content=b"ok").status_code == 200


# --------------------------------------------------------------------------
# Engine-level signals, observed through the endpoint
# --------------------------------------------------------------------------


def _completing_orchestrator(collector):
    """An orchestrator with a stub model, so executions actually finish.

    Without a model the platform stops at `waiting` and asks a human — correct
    behaviour, but not a terminal state, so it exercises nothing here.
    """
    from conftest import make_config, planning_model

    from orchestrator.platform import Orchestrator

    return asyncio.run(
        Orchestrator.create(
            config=make_config(profile="development"),
            connect_mcp=False,
            providers=[planning_model()],
            metrics=collector,
        )
    )


def test_running_an_execution_records_a_terminal_outcome():
    """The signal that was entirely absent: nothing recorded outcomes."""
    collector = Metrics()
    orc = _completing_orchestrator(collector)
    client = TestClient(_app(collector, orchestrator=orc))
    try:
        before = _series(_scrape(client), "orchestrator_executions_total")
        response = client.post(
            "/v1/executions",
            headers=_auth("admin-token"),
            json={"objective": "record a terminal outcome", "run": True},
        )
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "completed", response.text

        after_body = _scrape(client)
        assert _series(after_body, "orchestrator_executions_total") > before
        assert "orchestrator_execution_duration_seconds_count" in after_body
        # Labelled by real status and confidence, from the closed sets.
        assert "orchestrator_executions_total{confidence=" in after_body
    finally:
        asyncio.run(orc.close())


def test_a_run_that_stops_to_ask_a_human_is_not_counted_as_terminal():
    """`waiting` is blocked, not finished. Counting it would inflate the rate."""
    collector = Metrics()
    client = TestClient(_app(collector))
    response = client.post(
        "/v1/executions",
        headers=_auth("admin-token"),
        json={"objective": "needs a human", "run": True},
    )
    assert response.json()["status"] == "waiting"
    assert "orchestrator_executions_total" not in _scrape(client)


def test_a_terminal_outcome_is_recorded_exactly_once():
    """The wrapper must not double-count a path the loop also reached."""
    collector = Metrics()
    orc = _completing_orchestrator(collector)
    client = TestClient(_app(collector, orchestrator=orc))
    try:
        client.post(
            "/v1/executions",
            headers=_auth("admin-token"),
            json={"objective": "count me once", "run": True},
        )
        assert _series(_scrape(client), "orchestrator_executions_total") == 1
    finally:
        asyncio.run(orc.close())


def test_the_point_in_time_gauges_reflect_real_state():
    collector = Metrics()
    client = TestClient(_app(collector))
    client.post(
        "/v1/executions",
        headers=_auth("admin-token"),
        json={"objective": "populate the gauges", "run": True},
    )

    body = _scrape(client)
    assert "orchestrator_executions_current" in body
    assert _series(body, "orchestrator_tools_registered") >= 1


def test_queue_wait_is_recorded_when_an_execution_starts():
    collector = Metrics()
    client = TestClient(_app(collector))
    client.post(
        "/v1/executions",
        headers=_auth("admin-token"),
        json={"objective": "measure queue wait", "run": True},
    )
    assert "orchestrator_queue_wait_seconds_count" in _scrape(client)


# --------------------------------------------------------------------------
# Tool and policy signals, driven through a real orchestrator
# --------------------------------------------------------------------------


def _orchestrator(collector, **overrides):
    from conftest import make_config

    from orchestrator.platform import Orchestrator

    overrides.setdefault("profile", "development")
    config = make_config(**overrides)
    return asyncio.run(
        Orchestrator.create(config=config, connect_mcp=False, metrics=collector)
    )


# The broken-collector API tests build their own orchestrator so the failure
# surface includes construction, not only request handling.


def test_one_collector_reaches_every_recording_site():
    """Separate collectors per component would silently split the counters."""
    collector = Metrics()
    orc = _orchestrator(collector)
    try:
        assert orc.metrics is collector
        assert orc.router.metrics is collector
        assert orc.tools.metrics is collector
        assert orc.engine.metrics is collector
    finally:
        asyncio.run(orc.close())


def test_a_successful_tool_call_is_counted():
    from orchestrator.core.domain.models import ToolCall
    from orchestrator.tools.registry import ToolContext

    collector = Metrics()
    orc = _orchestrator(collector)
    try:
        tool_ids = [t.id for t in orc.tools.list()]
        assert "orchestrator.record_note" in tool_ids, tool_ids
        target = "orchestrator.record_note"

        asyncio.run(
            orc.tools.call(
                ToolCall(tool_id=target, arguments={"key": "k", "value": "v"}),
                ToolContext(execution_id="e", task_id="t", scope=_full_scope()),
            )
        )
        body = collector.render()
        assert "orchestrator_tool_calls_total" in body
        assert 'outcome="ok"' in body
    finally:
        asyncio.run(orc.close())


def test_a_denied_tool_call_is_counted_as_denied_and_as_a_policy_denial():
    from orchestrator.core.domain.models import ToolCall
    from orchestrator.errors import PermissionDenied
    from orchestrator.tools.registry import ToolContext

    collector = Metrics()
    orc = _orchestrator(collector)
    try:
        target = "orchestrator.record_note"
        with pytest.raises(PermissionDenied):
            asyncio.run(
                orc.tools.call(
                    ToolCall(tool_id=target, arguments={}),
                    # An empty scope grants nothing.
                    ToolContext(execution_id="e", task_id="t", scope=PermissionScope()),
                )
            )
        body = collector.render()
        assert 'outcome="denied"' in body
        assert "orchestrator_policy_denials_total" in body
    finally:
        asyncio.run(orc.close())


def test_an_unknown_plugin_tool_name_collapses_to_one_series():
    """An unbounded tool label is how a metrics backend falls over."""
    collector = Metrics()
    orc = _orchestrator(collector)
    try:
        assert orc.tools._metric_tool_id("this-tool-is-not-registered") == "other"
        assert orc.tools._metric_tool_id("x" * 200) == "other"
    finally:
        asyncio.run(orc.close())


# --------------------------------------------------------------------------
# Failure safety
# --------------------------------------------------------------------------


class _BrokenMetrics:
    """A collector where every recording raises."""

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise RuntimeError("metrics backend is on fire")

        return explode


def test_a_broken_collector_never_fails_an_api_request():
    client = TestClient(_app(_BrokenMetrics()))
    # Authentication failure records a metric; it must still be a clean 401.
    assert client.get("/v1/executions").status_code == 401
    # And an authorization failure a clean 403.
    assert (
        client.post(
            "/v1/executions",
            headers=_auth("reader-token"),
            json={"objective": "x", "run": False},
        ).status_code
        == 403
    )


def test_a_broken_collector_never_fails_an_execution():
    client = TestClient(_app(_BrokenMetrics()))
    response = client.post(
        "/v1/executions",
        headers=_auth("admin-token"),
        json={"objective": "survive broken metrics", "run": True},
    )
    assert response.status_code == 201, response.text


def test_a_broken_collector_never_fails_a_tool_call():
    from orchestrator.core.domain.models import ToolCall
    from orchestrator.tools.registry import ToolContext

    orc = _orchestrator(_BrokenMetrics())
    try:
        target = "orchestrator.record_note"
        result = asyncio.run(
            orc.tools.call(
                ToolCall(tool_id=target, arguments={"key": "k", "value": "v"}),
                ToolContext(execution_id="e", task_id="t", scope=_full_scope()),
            )
        )
        assert result.ok is True
    finally:
        asyncio.run(orc.close())


# --------------------------------------------------------------------------
# Cardinality, on real output
# --------------------------------------------------------------------------


def test_no_identifier_or_free_text_appears_in_real_metrics_output():
    collector = Metrics()
    client = TestClient(_app(collector))
    objective = "a distinctive objective string nobody should ever see in metrics"
    created = client.post(
        "/v1/executions",
        headers=_auth("admin-token"),
        json={"objective": objective, "run": True},
    )
    execution_id = created.json()["id"]

    body = _scrape(client)
    assert objective not in body
    assert execution_id not in body
    for forbidden in ("admin", "reader", "tenant", "default", "http://", "Bearer"):
        assert f'"{forbidden}"' not in body, f"{forbidden!r} became a label value"
