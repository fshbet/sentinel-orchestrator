"""What the health and metrics endpoints disclose, and to whom.

Regression tests for a real leak: `/health` was in the public path set and
returned `orchestrator.health()` verbatim — absolute filesystem paths, the
storage backend, every registered validator, every configured provider, and
the full model inventory — to any caller who could reach the port.

The rule these encode: a probe answers *whether*, an authenticated endpoint
answers *what*.
"""

from __future__ import annotations

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
from orchestrator.api.security import PUBLIC_PATHS, SecurityConfig

# Strings that must never appear in a response to an unauthenticated caller.
# Drawn from what the endpoint actually returned before the fix.
DISCLOSURE_MARKERS = (
    "config_sources",
    "config_fingerprint",
    "storage",
    "validators",
    "plugins",
    "models",
    "provider",
    "SQLiteStateStore",
    "devils_advocate",
    ".orchestrator",
    "C:\\",
    "/usr/",
)


def _client():
    registry = IdentityRegistry([
        TokenPrincipal("admin-token", Principal(
            id="admin", scopes=expand_scopes([ADMIN]), token_id="ENV_ADMIN")),
        TokenPrincipal("reader-token", Principal(
            id="reader", scopes=expand_scopes([EXECUTIONS_READ]),
            token_id="ENV_READER")),
    ])
    return TestClient(create_app(
        security=SecurityConfig(host="0.0.0.0", tokens=("unused",)),
        identity_registry=registry,
    ))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _assert_no_disclosure(body: str, where: str):
    leaked = [m for m in DISCLOSURE_MARKERS if m in body]
    assert not leaked, f"{where} disclosed {leaked}: {body[:400]}"


# --------------------------------------------------------------------------
# The public path set itself
# --------------------------------------------------------------------------


def test_health_is_not_a_public_path():
    """The regression: /health used to sit alongside /live and /ready."""
    assert "/health" not in PUBLIC_PATHS
    assert "/live" in PUBLIC_PATHS
    assert "/ready" in PUBLIC_PATHS
    assert "/" in PUBLIC_PATHS


def test_the_public_path_set_is_exactly_the_four_intended_entries():
    """A path added here becomes reachable by anyone. Pin it."""
    assert PUBLIC_PATHS == frozenset({"/", "/live", "/ready"})


# --------------------------------------------------------------------------
# /live — public, lightweight, dependency-free
# --------------------------------------------------------------------------


def test_live_answers_anonymously_with_nothing_but_status():
    client = _client()
    response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_live_does_not_touch_models_or_storage():
    """A liveness probe that checks dependencies restarts a healthy process."""
    import inspect

    from orchestrator.api import app as app_module

    source = inspect.getsource(app_module.create_app)
    body = source.split('@app.get("/live")')[1].split("@app.get")[0]
    for forbidden in ("get_orchestrator", "orc.", "await"):
        assert forbidden not in body, (
            f"/live reaches a dependency via {forbidden!r}; it must not"
        )


# --------------------------------------------------------------------------
# /ready — public, but status only
# --------------------------------------------------------------------------


def test_ready_gives_an_anonymous_caller_only_a_status():
    client = _client()
    response = client.get("/ready")
    assert response.status_code in (200, 503)
    payload = response.json()
    assert set(payload) == {"status"}
    assert payload["status"] in ("ready", "not_ready")
    _assert_no_disclosure(response.text, "/ready anonymous")


def test_ready_gives_a_read_only_caller_only_a_status():
    """Read on executions is not read on the deployment's shape."""
    client = _client()
    response = client.get("/ready", headers=_auth("reader-token"))
    assert set(response.json()) == {"status"}
    _assert_no_disclosure(response.text, "/ready reader")


def test_ready_gives_an_admin_the_detail():
    client = _client()
    response = client.get("/ready", headers=_auth("admin-token"))
    if response.status_code == 200:
        assert "detail" in response.json()


def test_a_not_ready_reason_is_never_shown_anonymously():
    """The reason names what is broken, which maps the deployment."""
    client = _client()
    response = client.get("/ready")
    assert "reason" not in response.json()


# --------------------------------------------------------------------------
# /health and /v1/health — authenticated only
# --------------------------------------------------------------------------


def test_health_requires_admin():
    client = _client()
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers=_auth("reader-token")).status_code == 403
    assert client.get("/health", headers=_auth("admin-token")).status_code == 200


def test_v1_health_requires_admin_and_carries_the_detail():
    client = _client()
    assert client.get("/v1/health").status_code == 401
    assert client.get("/v1/health", headers=_auth("reader-token")).status_code == 403

    ok = client.get("/v1/health", headers=_auth("admin-token"))
    assert ok.status_code == 200
    payload = ok.json()
    # The detail an operator actually needs is still there.
    assert "status" in payload
    assert "storage" in payload


def test_no_public_endpoint_discloses_deployment_shape():
    """The whole point, checked across every public path at once."""
    client = _client()
    for path in sorted(PUBLIC_PATHS):
        response = client.get(path)
        if path == "/":
            # The console shell is static HTML; it holds no deployment data.
            assert response.status_code == 200
            assert "config_fingerprint" not in response.text
            assert "SQLiteStateStore" not in response.text
            continue
        _assert_no_disclosure(response.text, path)


# --------------------------------------------------------------------------
# /metrics
# --------------------------------------------------------------------------


def test_metrics_requires_admin():
    client = _client()
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=_auth("reader-token")).status_code == 403
    assert client.get("/metrics", headers=_auth("admin-token")).status_code == 200


def test_metrics_comes_from_the_collector_not_ad_hoc_queries():
    """One source of truth, with the collector's cardinality guarantees.

    The previous endpoint built its own exposition text inline, which meant
    two exporters with two sets of label rules and only one of them having
    any. Point-in-time gauges now go *through* the collector so they inherit
    its validation.
    """
    client = _client()
    body = client.get("/metrics", headers=_auth("admin-token")).text

    assert "# TYPE" in body

    # The legacy ad-hoc series name is gone.
    assert "orchestrator_executions{" not in body
    assert "orchestrator_tools " not in body

    # Its replacement is registered through the collector.
    assert "orchestrator_executions_current" in body
    assert "orchestrator_tools_registered" in body


def test_the_metrics_endpoint_renders_the_apps_collector():
    """Not a fresh Metrics() per request, or every counter reads zero."""
    from orchestrator.observability.metrics import Metrics

    collector = Metrics()
    collector.execution_finished("completed", "confirmed", 1.0)

    registry = IdentityRegistry([
        TokenPrincipal("admin-token", Principal(
            id="admin", scopes=expand_scopes([ADMIN]), token_id="ENV_ADMIN")),
    ])
    client = TestClient(create_app(
        security=SecurityConfig(host="0.0.0.0", tokens=("unused",)),
        identity_registry=registry,
        metrics_collector=collector,
    ))
    body = client.get("/metrics", headers=_auth("admin-token")).text
    assert 'orchestrator_executions_total{confidence="confirmed",status="completed"} 1' in body


def test_a_failing_gauge_refresh_still_serves_the_recorded_counters():
    """A scrape must not fail because one point-in-time query broke."""
    from orchestrator.observability.metrics import Metrics

    collector = Metrics()
    collector.security_event("authentication")

    registry = IdentityRegistry([
        TokenPrincipal("admin-token", Principal(
            id="admin", scopes=expand_scopes([ADMIN]), token_id="ENV_ADMIN")),
    ])
    app = create_app(
        security=SecurityConfig(host="0.0.0.0", tokens=("unused",)),
        identity_registry=registry,
        metrics_collector=collector,
    )
    client = TestClient(app)
    response = client.get("/metrics", headers=_auth("admin-token"))
    assert response.status_code == 200
    assert "orchestrator_security_events_total" in response.text


def test_metrics_never_leaks_free_text():
    client = _client()
    body = client.get("/metrics", headers=_auth("admin-token")).text
    for forbidden in ("objective", "prompt", "Bearer", "sk-", "C:\\", "/usr/"):
        assert forbidden not in body, f"/metrics leaked {forbidden!r}"
