"""Token lifecycle enforced through HTTP, not through the class.

`test_token_lifecycle.py` proves `TokenStore` behaves correctly when called.
That is a different claim from "the API calls it": the store existed, was
fully tested, and the middleware resolved credentials through a separate raw
string comparison in `IdentityRegistry` that knew nothing about expiry or
revocation. A token could be expired according to the store and valid
according to the thing actually guarding the endpoint.

Every test here goes through `TestClient`, so it exercises the middleware.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("fastapi", reason="the API requires fastapi")

from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.identity import ADMIN, EXECUTIONS_READ, IdentityRegistry
from orchestrator.api.security import SecurityConfig
from orchestrator.api.tokens import CLOCK_SKEW, TokenStore


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _client(store, **kwargs):
    registry = IdentityRegistry(store=store, **kwargs)
    return TestClient(create_app(
        security=SecurityConfig(host="0.0.0.0", tokens=("unused",)),
        identity_registry=registry,
    )), registry


class _Recorder:
    def __init__(self):
        self.events = []

    def record(self, event, **payload):
        self.events.append((event, payload))

    def types(self):
        return [e for e, _ in self.events]


# --------------------------------------------------------------------------
# The middleware actually uses the store
# --------------------------------------------------------------------------


def test_a_valid_token_is_accepted_over_http():
    store = TokenStore()
    secret, _ = store.issue("ops", scopes=[ADMIN])
    client, _ = _client(store)
    assert client.get("/v1/executions", headers=_auth(secret)).status_code == 200


def test_an_expired_token_is_rejected_over_http():
    """The regression: expiry existed in the store and not on the request path."""
    store = TokenStore()
    secret, _ = store.issue("ops", scopes=[ADMIN], lifetime=-(CLOCK_SKEW * 10))

    client, _ = _client(store)
    response = client.get("/v1/executions", headers=_auth(secret))
    assert response.status_code == 401
    assert "expired" in response.json()["message"].lower()


def test_a_not_yet_valid_token_is_rejected_over_http():
    store = TokenStore()
    secret, _ = store.issue(
        "ops", scopes=[ADMIN],
        not_before=datetime.now(UTC) + timedelta(hours=2),
    )
    client, _ = _client(store)
    response = client.get("/v1/executions", headers=_auth(secret))
    assert response.status_code == 401
    assert "not valid yet" in response.json()["message"].lower()


def test_a_revoked_token_is_rejected_over_http():
    store = TokenStore()
    secret, record = store.issue("ops", scopes=[ADMIN])
    client, _ = _client(store)

    assert client.get("/v1/executions", headers=_auth(secret)).status_code == 200
    store.revoke(record.token_id, reason="test")
    response = client.get("/v1/executions", headers=_auth(secret))
    assert response.status_code == 401
    assert "revoked" in response.json()["message"].lower()


def test_revocation_takes_effect_without_restarting_the_service():
    """Same client, same process — only the revocation happened in between."""
    store = TokenStore()
    secret, record = store.issue("ops", scopes=[ADMIN])
    client, registry = _client(store)

    assert client.get("/v1/executions", headers=_auth(secret)).status_code == 200
    assert registry.revoke(record.token_id, reason="leaked") is True
    assert client.get("/v1/executions", headers=_auth(secret)).status_code == 401
    # And the service is still serving other traffic.
    assert client.get("/live").status_code == 200


def test_the_rejection_reason_distinguishes_expiry_from_an_unknown_token():
    """"Expired" says reissue; "not recognised" sends you hunting for a typo."""
    store = TokenStore()
    expired, _ = store.issue("a", scopes=[ADMIN], lifetime=-(CLOCK_SKEW * 10))
    client, _ = _client(store)

    a = client.get("/v1/executions", headers=_auth(expired)).json()["message"]
    b = client.get("/v1/executions", headers=_auth("never-issued")).json()["message"]
    assert a != b
    assert "expired" in a.lower()


def test_scopes_are_enforced_on_the_request_path():
    store = TokenStore()
    reader, _ = store.issue("reader", scopes=[EXECUTIONS_READ])
    client, _ = _client(store)

    assert client.get("/v1/executions", headers=_auth(reader)).status_code == 200
    denied = client.post("/v1/executions", headers=_auth(reader),
                         json={"objective": "x", "run": False})
    assert denied.status_code == 403


def test_tenant_assignment_survives_the_request_path():
    store = TokenStore()
    acme, _ = store.issue("acme-bot", scopes=[ADMIN], tenant="acme")
    globex, _ = store.issue("globex-bot", scopes=[ADMIN], tenant="globex")
    client, _ = _client(store, multi_tenant=True)

    created = client.post("/v1/executions", headers=_auth(acme),
                          json={"objective": "acme work", "run": False})
    assert created.status_code == 201
    execution_id = created.json()["id"]

    assert client.get(f"/v1/executions/{execution_id}",
                      headers=_auth(acme)).status_code == 200
    # 404, not 403: a 403 would confirm the id exists.
    assert client.get(f"/v1/executions/{execution_id}",
                      headers=_auth(globex)).status_code == 404


# --------------------------------------------------------------------------
# The admin API
# --------------------------------------------------------------------------


def test_listing_tokens_returns_metadata_and_never_a_secret():
    store = TokenStore()
    admin_secret, _ = store.issue("admin", scopes=[ADMIN], description="ops")
    other_secret, _ = store.issue("ci", scopes=[EXECUTIONS_READ],
                                  lifetime=timedelta(days=30))
    client, _ = _client(store)

    response = client.get("/v1/tokens", headers=_auth(admin_secret))
    assert response.status_code == 200
    body = response.text
    assert admin_secret not in body
    assert other_secret not in body

    tokens = response.json()["tokens"]
    assert len(tokens) == 2
    for entry in tokens:
        assert entry["token_id"].startswith("tok_")
        assert {"principal", "scopes", "expires_at", "revoked"} <= set(entry)
        assert "digest" not in entry and "salt" not in entry


def test_listing_tokens_requires_admin():
    store = TokenStore()
    reader, _ = store.issue("reader", scopes=[EXECUTIONS_READ])
    client, _ = _client(store)
    assert client.get("/v1/tokens").status_code == 401
    assert client.get("/v1/tokens", headers=_auth(reader)).status_code == 403


def test_revoking_through_the_api_takes_effect_immediately():
    store = TokenStore()
    admin_secret, _ = store.issue("admin", scopes=[ADMIN])
    victim_secret, victim = store.issue("ci", scopes=[EXECUTIONS_READ])
    client, _ = _client(store)

    assert client.get("/v1/executions", headers=_auth(victim_secret)).status_code == 200

    revoked = client.post(f"/v1/tokens/{victim.token_id}/revoke",
                          headers=_auth(admin_secret), params={"reason": "leaked"})
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    # No restart between these two lines.
    assert client.get("/v1/executions", headers=_auth(victim_secret)).status_code == 401


def test_revoking_requires_admin():
    store = TokenStore()
    reader, record = store.issue("reader", scopes=[EXECUTIONS_READ])
    client, _ = _client(store)
    assert client.post(f"/v1/tokens/{record.token_id}/revoke",
                       headers=_auth(reader)).status_code == 403


def test_revoking_an_unknown_token_is_a_404():
    store = TokenStore()
    admin_secret, _ = store.issue("admin", scopes=[ADMIN])
    client, _ = _client(store)
    assert client.post("/v1/tokens/tok_nonexistent/revoke",
                       headers=_auth(admin_secret)).status_code == 404


def test_an_admin_cannot_revoke_their_way_into_a_secret():
    """No endpoint returns or issues a token value over HTTP."""
    import inspect

    from orchestrator.api import app as module

    source = inspect.getsource(module.create_app)
    tokens_section = source.split("# -- tokens")[1].split("# -- registries")[0]
    # Strip comments and docstrings: the prose here discusses secrets, which
    # is not the same as returning one.
    code = " ".join(
        line for line in tokens_section.splitlines()
        if not line.strip().startswith("#")
    )
    for forbidden in ("generate_token", ".issue(", '"secret"', "token_value"):
        assert forbidden not in code, (
            f"the token API exposes {forbidden!r}; a credential that can be "
            f"fetched from an API is one the API can leak"
        )


def test_revocation_is_audited_with_the_actor():
    audit = _Recorder()
    store = TokenStore(audit=audit)
    admin_secret, _ = store.issue("admin", scopes=[ADMIN])
    _victim_secret, victim = store.issue("ci", scopes=[EXECUTIONS_READ])
    client, _ = _client(store)

    client.post(f"/v1/tokens/{victim.token_id}/revoke",
                headers=_auth(admin_secret), params={"reason": "rotated"})
    assert "token.revoked" in audit.types()
    payloads = [p for e, p in audit.events if e == "token.revoked"]
    assert any(p.get("token_id") == victim.token_id for p in payloads)


# --------------------------------------------------------------------------
# There is one resolution path
# --------------------------------------------------------------------------


def test_the_registry_holds_no_plaintext_after_construction():
    """A memory dump must not yield working credentials."""
    from orchestrator.api.identity import Principal, TokenPrincipal, expand_scopes

    registry = IdentityRegistry([
        TokenPrincipal("a-very-distinctive-secret-value",
                       Principal(id="p", scopes=expand_scopes([ADMIN])))
    ])
    assert "a-very-distinctive-secret-value" not in str(registry.__dict__)
    assert "a-very-distinctive-secret-value" not in str(registry.list_tokens())
    # And it still resolves.
    assert registry.resolve("a-very-distinctive-secret-value").id == "p"


def test_the_registry_resolves_through_the_token_store():
    """One path: if these diverged, the middleware would use the weaker one."""
    from orchestrator.api.identity import Principal, TokenPrincipal, expand_scopes

    registry = IdentityRegistry([
        TokenPrincipal("s", Principal(id="p", scopes=expand_scopes([ADMIN])))
    ])
    assert isinstance(registry.tokens, TokenStore)

    token_id = registry.list_tokens()[0]["token_id"]
    registry.tokens.revoke(token_id)
    # Revoking in the store is visible through the registry's resolve().
    from orchestrator.api.tokens import TokenRevoked

    with pytest.raises(TokenRevoked):
        registry.resolve("s")


def test_configured_principals_satisfy_the_insecure_binding_check(monkeypatch):
    """The Compose regression: correct config could not start on 0.0.0.0.

    `verify_binding` only knew about ORCHESTRATOR_API_TOKEN, so a deployment
    that configured api.principals properly and set no legacy token was
    refused at startup — the correct configuration was the unusable one.
    """
    from orchestrator.api.security import InsecureBinding, SecurityConfig

    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)
    monkeypatch.setenv("OPS_TOKEN", "a-configured-token")

    registry = IdentityRegistry.from_config({
        "principals": [{"id": "ops", "token_env": "OPS_TOKEN", "scopes": ["admin"]}]
    })
    assert registry.enabled is True

    config = SecurityConfig(host="0.0.0.0")
    # Without the registry it is refused...
    with pytest.raises(InsecureBinding):
        config.verify_binding()
    # ...and with it, the deployment starts.
    config.verify_binding(identities=registry)


def test_a_principal_yields_exactly_one_token(monkeypatch):
    """The facade double-registered: metadata-only entries were re-issued.

    `from_config` puts the real credentials in the store and hands back
    entries carrying no token. Issuing those called generate_token(), minting
    a second, phantom credential per principal — visible in the admin listing,
    counted by len(), and not revoked when the real one was.
    """
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)
    monkeypatch.setenv("OPS_TOKEN", "ops-value")
    monkeypatch.setenv("CI_TOKEN", "ci-value")

    registry = IdentityRegistry.from_config({
        "principals": [
            {"id": "ops", "token_env": "OPS_TOKEN", "scopes": ["admin"]},
            {"id": "ci", "token_env": "CI_TOKEN", "scopes": ["executions.read"]},
        ]
    })

    tokens = registry.list_tokens()
    assert len(tokens) == 2, [t["principal"] for t in tokens]
    assert {t["principal"] for t in tokens} == {"ops", "ci"}
    assert len(registry) == 2


def test_revoking_a_principal_leaves_no_second_credential(monkeypatch):
    """Otherwise revocation appears to work and the phantom still resolves."""
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)
    monkeypatch.setenv("CI_TOKEN", "ci-value")

    registry = IdentityRegistry.from_config({
        "principals": [{"id": "ci", "token_env": "CI_TOKEN", "scopes": ["admin"]}]
    })
    entries = [t for t in registry.list_tokens() if t["principal"] == "ci"]
    assert len(entries) == 1

    assert registry.revoke(entries[0]["token_id"]) is True
    from orchestrator.api.tokens import TokenRevoked

    with pytest.raises(TokenRevoked):
        registry.resolve("ci-value")


# --------------------------------------------------------------------------
# Proxy identity is not integrated, and says so
# --------------------------------------------------------------------------


def test_enabling_proxy_identity_is_refused_by_configuration(tmp_path):
    """A setting that looks like authentication and does nothing is worse
    than one that is absent."""
    from orchestrator.config.loader import load
    from orchestrator.errors import ConfigurationError

    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\nprofile: development\n"
        "api:\n  proxy_identity:\n    enabled: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as exc:
        load(paths=[str(path)], include_discovered=False)
    message = str(exc.value)
    assert "not integrated" in message
    assert "api.principals" in message, "the error must name the supported mode"


def test_proxy_identity_disabled_is_accepted(tmp_path):
    from orchestrator.config.loader import load

    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\nprofile: development\n"
        "api:\n  proxy_identity:\n    enabled: false\n",
        encoding="utf-8",
    )
    assert load(paths=[str(path)], include_discovered=False) is not None


def test_the_middleware_does_not_call_proxy_identity():
    """Option B: the claim is removed, so the code must match the claim."""
    import inspect

    from orchestrator.api import security

    source = inspect.getsource(security)
    assert "proxy_identity(" not in source, (
        "middleware calls proxy_identity but the deployment documents it as "
        "unintegrated; one of the two is wrong"
    )


def test_the_shipped_deployment_does_not_enable_proxy_identity():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load(
        (root / "deployment" / "config.production.yaml").read_text(encoding="utf-8")
    )
    assert config["api"]["proxy_identity"]["enabled"] is False


def test_the_caddyfile_does_not_claim_to_authenticate():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "deployment" / "Caddyfile").read_text(encoding="utf-8")
    assert "does NOT authenticate" in text
