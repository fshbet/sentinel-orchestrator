"""Scopes and tenant boundaries on the API.

Two properties matter here and neither is provable by inspection:

* a credential that may read must not thereby be able to write, approve, or
  read audit trails;
* a caller who knows another tenant's execution id must get nothing.
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
    ALL_SCOPES,
    APPROVALS_RESPOND,
    AUDIT_READ,
    EXECUTIONS_READ,
    EXECUTIONS_WRITE,
    IdentityRegistry,
    Principal,
    TokenPrincipal,
    Unauthorized,
    expand_scopes,
    required_scope,
)
from orchestrator.api.security import SecurityConfig
from orchestrator.errors import ConfigurationError


def _registry(*specs, multi_tenant=False):
    """specs: (token, principal_id, scopes, tenant)"""
    entries = [
        TokenPrincipal(
            token=token,
            principal=Principal(
                id=pid, scopes=expand_scopes(scopes), tenant=tenant, token_id=f"ENV_{pid}"
            ),
        )
        for token, pid, scopes, tenant in specs
    ]
    return IdentityRegistry(entries, multi_tenant=multi_tenant)


def _client(registry):
    return TestClient(
        create_app(
            security=SecurityConfig(host="0.0.0.0", tokens=("unused",)),
            identity_registry=registry,
        )
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Scope resolution
# --------------------------------------------------------------------------


def test_admin_implies_the_other_scopes():
    scopes = expand_scopes([ADMIN])
    for scope in ALL_SCOPES:
        assert scope in scopes


def test_an_unmapped_route_requires_admin():
    """A route added later must fail closed, not become accidentally public."""
    assert required_scope("GET", "/v1/some-future-endpoint") == ADMIN
    assert required_scope("DELETE", "/v1/executions") == ADMIN


def test_each_route_maps_to_the_expected_scope():
    assert required_scope("GET", "/v1/executions") == EXECUTIONS_READ
    assert required_scope("POST", "/v1/executions") == EXECUTIONS_WRITE
    assert required_scope("GET", "/v1/executions/abc/audit") == AUDIT_READ
    assert required_scope("POST", "/v1/executions/a/approvals/b") == APPROVALS_RESPOND
    assert required_scope("GET", "/metrics") == ADMIN


# --------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------


def test_a_read_only_credential_cannot_start_an_execution():
    client = _client(_registry(("ro", "reader", [EXECUTIONS_READ], "default")))

    assert client.get("/v1/executions", headers=_auth("ro")).status_code == 200

    denied = client.post("/v1/executions", headers=_auth("ro"),
                         json={"objective": "do a thing", "run": False})
    assert denied.status_code == 403
    assert denied.json()["required_scope"] == EXECUTIONS_WRITE


def test_a_read_only_credential_cannot_read_audit_trails():
    """Audit is separate from execution read: it carries far more detail."""
    client = _client(_registry(("ro", "reader", [EXECUTIONS_READ], "default")))
    response = client.get("/v1/executions/anything/audit", headers=_auth("ro"))
    assert response.status_code == 403
    assert response.json()["required_scope"] == AUDIT_READ


def test_a_read_only_credential_cannot_respond_to_approvals():
    client = _client(_registry(("ro", "reader", [EXECUTIONS_READ], "default")))
    response = client.post("/v1/executions/x/approvals/y", headers=_auth("ro"),
                           json={"approved": True})
    assert response.status_code == 403
    assert response.json()["required_scope"] == APPROVALS_RESPOND


def test_a_non_admin_credential_cannot_read_metrics():
    client = _client(_registry(("rw", "writer",
                                [EXECUTIONS_READ, EXECUTIONS_WRITE], "default")))
    assert client.get("/metrics", headers=_auth("rw")).status_code == 403


def test_an_admin_credential_can_do_everything():
    client = _client(_registry(("adm", "admin", [ADMIN], "default")))
    assert client.get("/v1/executions", headers=_auth("adm")).status_code == 200
    assert client.get("/metrics", headers=_auth("adm")).status_code == 200


def test_an_unrecognised_credential_is_rejected():
    client = _client(_registry(("good", "p", [ADMIN], "default")))
    assert client.get("/v1/executions", headers=_auth("bad")).status_code == 401
    assert client.get("/v1/executions").status_code == 401


def test_probes_stay_reachable_without_a_credential():
    client = _client(_registry(("good", "p", [ADMIN], "default")))
    assert client.get("/live").status_code == 200
    assert client.get("/").status_code == 200


def test_every_response_carries_a_request_id():
    client = _client(_registry(("adm", "admin", [ADMIN], "default")))
    ok = client.get("/v1/executions", headers=_auth("adm"))
    assert ok.headers.get("X-Request-ID")

    denied = client.get("/v1/executions")
    assert denied.headers.get("X-Request-ID")
    assert denied.json()["request_id"]


def test_a_supplied_request_id_is_preserved_for_correlation():
    client = _client(_registry(("adm", "admin", [ADMIN], "default")))
    response = client.get("/v1/executions", headers={
        **_auth("adm"), "X-Request-ID": "caller-supplied-id"
    })
    assert response.headers["X-Request-ID"] == "caller-supplied-id"


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


def _create(client, token, objective):
    response = client.post("/v1/executions", headers=_auth(token),
                           json={"objective": objective, "run": False})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def two_tenants():
    registry = _registry(
        ("acme-token", "acme-bot", [ADMIN], "acme"),
        ("globex-token", "globex-bot", [ADMIN], "globex"),
        multi_tenant=True,
    )
    return _client(registry)


def test_one_tenant_cannot_read_another_tenants_execution(two_tenants):
    execution_id = _create(two_tenants, "acme-token", "acme private work")

    assert two_tenants.get(f"/v1/executions/{execution_id}",
                           headers=_auth("acme-token")).status_code == 200

    # 404 rather than 403: a 403 would confirm the id exists.
    stolen = two_tenants.get(f"/v1/executions/{execution_id}",
                             headers=_auth("globex-token"))
    assert stolen.status_code == 404


def test_one_tenant_cannot_cancel_another_tenants_execution(two_tenants):
    execution_id = _create(two_tenants, "acme-token", "acme work")
    response = two_tenants.post(f"/v1/executions/{execution_id}/cancel",
                                headers=_auth("globex-token"))
    assert response.status_code == 404


def test_one_tenant_cannot_pause_or_resume_another_tenants_execution(two_tenants):
    execution_id = _create(two_tenants, "acme-token", "acme work")
    for action in ("pause", "resume"):
        response = two_tenants.post(f"/v1/executions/{execution_id}/{action}",
                                    headers=_auth("globex-token"))
        assert response.status_code == 404, action


def test_one_tenant_cannot_read_another_tenants_audit_trail(two_tenants):
    execution_id = _create(two_tenants, "acme-token", "acme work")
    response = two_tenants.get(f"/v1/executions/{execution_id}/audit",
                               headers=_auth("globex-token"))
    assert response.status_code == 404


def test_listing_shows_only_the_callers_own_executions(two_tenants):
    acme_id = _create(two_tenants, "acme-token", "acme work")
    globex_id = _create(two_tenants, "globex-token", "globex work")

    acme_ids = {
        row["id"] for row in
        two_tenants.get("/v1/executions", headers=_auth("acme-token")).json()["executions"]
    }
    assert acme_id in acme_ids
    assert globex_id not in acme_ids

    globex_ids = {
        row["id"] for row in
        two_tenants.get("/v1/executions", headers=_auth("globex-token")).json()["executions"]
    }
    assert globex_id in globex_ids
    assert acme_id not in globex_ids


def test_ownership_is_recorded_on_the_execution(two_tenants):
    execution_id = _create(two_tenants, "acme-token", "acme work")
    payload = two_tenants.get(f"/v1/executions/{execution_id}",
                              headers=_auth("acme-token")).json()
    assert payload["id"] == execution_id


# --------------------------------------------------------------------------
# Single-tenant mode
# --------------------------------------------------------------------------


def test_single_tenant_mode_lets_principals_share_executions():
    """Simpler internal deployments should not have to think about tenancy."""
    client = _client(_registry(
        ("a", "alice", [ADMIN], "default"),
        ("b", "bob", [ADMIN], "default"),
    ))
    execution_id = _create(client, "a", "shared work")
    assert client.get(f"/v1/executions/{execution_id}",
                      headers=_auth("b")).status_code == 200


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_a_principal_must_name_an_environment_variable_not_a_token():
    with pytest.raises(ConfigurationError) as exc:
        IdentityRegistry.from_config({
            "principals": [{"id": "ci", "token": "literal-secret"}]
        })
    assert "token_env" in str(exc.value)


def test_an_unknown_scope_is_rejected(monkeypatch):
    monkeypatch.setenv("CI_TOKEN", "x")
    with pytest.raises(ConfigurationError) as exc:
        IdentityRegistry.from_config({
            "principals": [{"id": "ci", "token_env": "CI_TOKEN",
                            "scopes": ["executions.destroy"]}]
        })
    assert "executions.destroy" in str(exc.value)


def test_a_tenant_cannot_be_set_without_enabling_multi_tenancy(monkeypatch):
    monkeypatch.setenv("CI_TOKEN", "x")
    with pytest.raises(ConfigurationError) as exc:
        IdentityRegistry.from_config({
            "tenancy": "single",
            "principals": [{"id": "ci", "token_env": "CI_TOKEN", "tenant": "acme"}],
        })
    assert "tenancy" in str(exc.value)


def test_a_principal_whose_token_is_unset_is_skipped_not_fatal(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)
    registry = IdentityRegistry.from_config({
        "principals": [{"id": "ci", "token_env": "MISSING_TOKEN"}]
    })
    assert len(registry) == 0


def test_scopes_default_to_read_only(monkeypatch):
    monkeypatch.setenv("CI_TOKEN", "x")
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)
    registry = IdentityRegistry.from_config({
        "principals": [{"id": "ci", "token_env": "CI_TOKEN"}]
    })
    principal = registry.resolve("x")
    assert principal.has(EXECUTIONS_READ)
    assert not principal.has(EXECUTIONS_WRITE)
    assert not principal.has(ADMIN)


def test_the_legacy_token_still_grants_full_access(monkeypatch):
    """Existing deployments must not break."""
    monkeypatch.setenv("ORCHESTRATOR_API_TOKEN", "legacy-value")
    registry = IdentityRegistry.from_config({})
    principal = registry.resolve("legacy-value")
    assert principal.has(ADMIN)
    assert principal.has(EXECUTIONS_WRITE)
    assert principal.id == "legacy-token"


def test_legacy_token_rotation_still_works(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_API_TOKEN", "old,new")
    registry = IdentityRegistry.from_config({})
    assert registry.resolve("old").has(ADMIN)
    assert registry.resolve("new").has(ADMIN)


def test_resolving_an_unknown_credential_raises_unauthorized(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_API_TOKEN", "known")
    registry = IdentityRegistry.from_config({})
    with pytest.raises(Unauthorized):
        registry.resolve("unknown")
    with pytest.raises(Unauthorized):
        registry.resolve(None)


def test_a_principal_never_exposes_its_token():
    """describe() feeds logs and the audit trail."""
    registry = _registry(("secret-token-value", "p", [ADMIN], "default"))
    described = registry.principals()[0].describe()
    assert "secret-token-value" not in str(described)
    assert described["token_id"] == "ENV_p"


# --------------------------------------------------------------------------
# Structural: no route may be added without an ownership check
# --------------------------------------------------------------------------
#
# Testing routes one at a time is how /audit, /artifacts and /approvals were
# missed in the first place: four endpoints were guarded, three were not, and
# every individual test passed. This enumerates them instead, so a route added
# later fails here rather than leaking.


def _execution_scoped_routes(app):
    return [
        route for route in app.routes
        if "{execution_id}" in getattr(route, "path", "")
    ]


def test_every_execution_scoped_route_takes_the_request_for_an_ownership_check():
    import inspect

    app = create_app(security=SecurityConfig(host="127.0.0.1"))
    routes = _execution_scoped_routes(app)
    assert routes, "expected execution-scoped routes to exist"

    unguarded = []
    for route in routes:
        parameters = inspect.signature(route.endpoint).parameters
        if "http_request" not in parameters:
            unguarded.append(f"{sorted(route.methods)} {route.path}")
    assert not unguarded, (
        "these routes cannot check ownership because they never see the "
        f"caller: {unguarded}"
    )


def test_every_execution_scoped_route_denies_a_foreign_tenant(two_tenants):
    """Exercised for real, not by signature inspection."""
    execution_id = _create(two_tenants, "acme-token", "acme work")
    intruder = _auth("globex-token")

    attempts = [
        ("GET", f"/v1/executions/{execution_id}", None),
        ("GET", f"/v1/executions/{execution_id}/audit", None),
        ("GET", f"/v1/executions/{execution_id}/artifacts", None),
        ("POST", f"/v1/executions/{execution_id}/pause", None),
        ("POST", f"/v1/executions/{execution_id}/resume", None),
        ("POST", f"/v1/executions/{execution_id}/cancel", None),
        ("POST", f"/v1/executions/{execution_id}/approvals/anything",
         {"approved": True}),
    ]

    leaked = []
    for method, path, body in attempts:
        response = two_tenants.request(method, path, headers=intruder, json=body)
        if response.status_code != 404:
            leaked.append(f"{method} {path} -> {response.status_code}")
    assert not leaked, f"cross-tenant access permitted on: {leaked}"


def test_the_owner_can_still_reach_all_of_those_routes(two_tenants):
    """A boundary that also blocks the legitimate owner is not a boundary."""
    execution_id = _create(two_tenants, "acme-token", "acme work")
    owner = _auth("acme-token")

    for path in (
        f"/v1/executions/{execution_id}",
        f"/v1/executions/{execution_id}/audit",
        f"/v1/executions/{execution_id}/artifacts",
    ):
        assert two_tenants.get(path, headers=owner).status_code == 200, path


# --------------------------------------------------------------------------
# Request size limits
# --------------------------------------------------------------------------


def test_a_lying_content_length_does_not_bypass_the_body_limit():
    """Content-Length is the caller's claim, not a measurement."""
    app = create_app(security=SecurityConfig(host="127.0.0.1", max_body_bytes=500))
    client = TestClient(app)

    payload = b'{"objective": "' + b"x" * 20000 + b'"}'
    response = client.post(
        "/v1/executions",
        content=payload,
        headers={"Content-Type": "application/json", "Content-Length": "50"},
    )
    assert response.status_code == 413


def test_a_chunked_body_is_still_limited():
    """No Content-Length at all must not mean no limit."""
    app = create_app(security=SecurityConfig(host="127.0.0.1", max_body_bytes=500))
    client = TestClient(app)

    def chunks():
        for _ in range(40):
            yield b"x" * 1000

    response = client.post(
        "/v1/executions",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_a_body_within_the_limit_is_accepted():
    app = create_app(security=SecurityConfig(host="127.0.0.1", max_body_bytes=100_000))
    client = TestClient(app)
    response = client.post("/v1/executions",
                           json={"objective": "small enough", "run": False})
    assert response.status_code == 201


# --------------------------------------------------------------------------
# Information disclosure
# --------------------------------------------------------------------------


def test_readiness_detail_is_not_given_to_unauthenticated_callers():
    """A probe needs ready/not-ready; it does not need the deployment map."""
    client = _client(_registry(("adm", "admin", [ADMIN], "default")))

    anonymous = client.get("/ready")
    assert anonymous.status_code in (200, 503)
    assert "detail" not in anonymous.json()
    assert "reason" not in anonymous.json()

    authenticated = client.get("/ready", headers=_auth("adm"))
    if authenticated.status_code == 200:
        assert "detail" in authenticated.json()


def test_errors_do_not_leak_filesystem_paths_or_credentials():
    from orchestrator.api.app import _safe_error
    from orchestrator.errors import ToolError

    error = ToolError(
        "could not read the file",
        path="/home/deploy/secrets/config.yaml",
        root="/srv/orchestrator",
        api_key="sk-or-v1-realkey",
        tool="fs.read_file",
    )
    payload = str(_safe_error(error))
    assert "/home/deploy" not in payload
    assert "/srv/orchestrator" not in payload
    assert "sk-or-v1-realkey" not in payload
    # Something useful must survive, or the sanitiser has made errors useless.
    assert "fs.read_file" in payload


def test_an_unexpected_error_returns_an_id_not_a_traceback():
    from orchestrator.api.app import create_app as _create

    app = _create(security=SecurityConfig(host="127.0.0.1"))

    @app.get("/v1/deliberate-boom")
    async def boom():
        raise RuntimeError("secret internal detail at /srv/app/main.py line 42")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/v1/deliberate-boom")
    assert response.status_code == 500
    body = response.text
    assert "/srv/app/main.py" not in body
    assert "secret internal detail" not in body
    assert response.json()["request_id"]


def test_a_wildcard_cors_origin_is_refused():
    from orchestrator.api.security import InsecureBinding

    with pytest.raises(InsecureBinding):
        create_app(security=SecurityConfig(
            host="127.0.0.1", allowed_origins=("*",)
        ))


def test_forwarded_headers_are_believed_only_from_a_trusted_proxy():
    from types import SimpleNamespace

    from orchestrator.api.security import client_address

    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.9"),
        headers={"x-forwarded-for": "203.0.113.5, 10.0.0.9"},
    )
    # Untrusted peer: the header is a claim, so it is ignored.
    assert client_address(request, ()) == "10.0.0.9"
    assert client_address(request, ("192.168.1.1",)) == "10.0.0.9"
    # Trusted peer: believed.
    assert client_address(request, ("10.0.0.9",)) == "203.0.113.5"


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_the_in_process_limiter_allows_then_blocks():
    from orchestrator.api.ratelimit import InMemoryRateLimiter, RateLimit

    clock = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: clock[0])
    limit = RateLimit(requests=3, window_seconds=60)

    for expected_remaining in (2, 1, 0):
        decision = limiter.check("k", limit)
        assert decision.allowed is True
        assert decision.remaining == expected_remaining

    blocked = limiter.check("k", limit)
    assert blocked.allowed is False
    assert blocked.retry_after > 0


def test_the_window_slides():
    from orchestrator.api.ratelimit import InMemoryRateLimiter, RateLimit

    clock = [1000.0]
    limiter = InMemoryRateLimiter(clock=lambda: clock[0])
    limit = RateLimit(requests=1, window_seconds=10)

    assert limiter.check("k", limit).allowed is True
    assert limiter.check("k", limit).allowed is False
    clock[0] += 11
    assert limiter.check("k", limit).allowed is True


def test_limits_are_counted_per_key():
    from orchestrator.api.ratelimit import InMemoryRateLimiter, RateLimit

    limiter = InMemoryRateLimiter()
    limit = RateLimit(requests=1, window_seconds=60)
    assert limiter.check("a", limit).allowed is True
    assert limiter.check("b", limit).allowed is True
    assert limiter.check("a", limit).allowed is False


def test_the_key_table_is_bounded():
    """The key is caller-influenced, so an unbounded map is a memory DoS."""
    from orchestrator.api.ratelimit import InMemoryRateLimiter, RateLimit

    limiter = InMemoryRateLimiter(max_keys=50)
    limit = RateLimit(requests=5, window_seconds=60)
    for index in range(500):
        limiter.check(f"key-{index}", limit)
    assert len(limiter._hits) <= 50


def test_the_in_process_limiter_declares_that_it_is_not_distributed():
    """Two replicas allow twice the rate. That must not be a surprise."""
    from orchestrator.api.ratelimit import InMemoryRateLimiter, NullRateLimiter

    assert InMemoryRateLimiter().distributed is False
    assert NullRateLimiter().distributed is True


def test_rate_limiting_is_off_unless_configured():
    from orchestrator.api.ratelimit import NullRateLimiter, build

    limiter, limit = build({})
    assert isinstance(limiter, NullRateLimiter)
    assert limit is None


def test_an_unknown_backend_is_rejected_with_guidance():
    from orchestrator.api.ratelimit import build

    with pytest.raises(ConfigurationError) as exc:
        build({"enabled": True, "backend": "redis"})
    assert "RateLimiter" in str(exc.value)


def test_a_rate_limited_caller_gets_429_with_retry_after():
    from orchestrator.api.ratelimit import InMemoryRateLimiter, RateLimit
    from orchestrator.api.security import SecurityConfig, install

    from fastapi import FastAPI

    app = FastAPI()
    install(
        app,
        SecurityConfig(host="127.0.0.1"),
        rate_limiter=InMemoryRateLimiter(),
        rate_limit=RateLimit(requests=2, window_seconds=60),
    )

    @app.get("/v1/ping")
    async def ping():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/v1/ping").status_code == 200
    assert client.get("/v1/ping").status_code == 200

    limited = client.get("/v1/ping")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"]
    assert limited.json()["error"] == "rate_limited"
