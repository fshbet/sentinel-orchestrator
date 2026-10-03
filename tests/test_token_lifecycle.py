"""Token expiry, revocation, hashed storage, and proxy identity."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.api.identity import ADMIN, EXECUTIONS_READ, Unauthorized
from orchestrator.api.tokens import (
    CLOCK_SKEW,
    ProxyIdentityConfig,
    TokenExpired,
    TokenNotYetValid,
    TokenRevoked,
    TokenStore,
    generate_token,
    hash_token,
    proxy_identity,
)
from orchestrator.errors import ConfigurationError


class _Recorder:
    def __init__(self):
        self.events = []

    def record(self, event, **payload):
        self.events.append((event, payload))

    def types(self):
        return [e for e, _ in self.events]


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_a_token_is_never_stored_in_plaintext():
    store = TokenStore()
    secret, record = store.issue("ci")

    assert secret not in record.digest
    assert secret not in str(record.describe())
    assert secret not in repr(record)
    # And it still verifies.
    assert record.matches(secret)
    assert not record.matches(secret + "x")


def test_the_same_token_hashes_differently_under_different_salts():
    """A digest lifted from one deployment says nothing about another."""
    assert hash_token("same-token", "salt-a") != hash_token("same-token", "salt-b")


def test_generated_tokens_are_high_entropy_and_unique():
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 40 for t in tokens)


def test_describe_never_carries_the_secret():
    store = TokenStore()
    secret, record = store.issue("ci", description="CI runner")
    described = record.describe()
    assert secret not in str(described)
    assert "digest" not in described
    assert "salt" not in described
    assert described["token_id"].startswith("tok_")


# --------------------------------------------------------------------------
# Expiry and not-before
# --------------------------------------------------------------------------


def test_an_expired_token_is_rejected_as_expired():
    """A different message from "not recognised": one says reissue."""
    store = TokenStore()
    secret, _ = store.issue("ci", lifetime=timedelta(seconds=-3600))
    with pytest.raises(TokenExpired):
        store.resolve(secret)


def test_a_token_within_its_lifetime_resolves():
    store = TokenStore()
    secret, _ = store.issue("ci", scopes=[EXECUTIONS_READ], lifetime=timedelta(hours=1))
    principal = store.resolve(secret)
    assert principal.id == "ci"
    assert principal.has(EXECUTIONS_READ)


def test_a_token_with_no_expiry_never_expires():
    store = TokenStore()
    secret, _ = store.issue("forever")
    assert store.resolve(secret).id == "forever"


def test_a_token_is_not_valid_before_its_start_time():
    store = TokenStore()
    future = datetime.now(UTC) + timedelta(hours=2)
    secret, _ = store.issue("scheduled", not_before=future)
    with pytest.raises(TokenNotYetValid):
        store.resolve(secret)


def test_clock_skew_is_tolerated_in_both_directions():
    """Rejecting a one-second clock difference is a self-inflicted outage."""
    store = TokenStore()
    now = datetime.now(UTC)

    barely_future, _ = store.issue("a", not_before=now + (CLOCK_SKEW / 2))
    assert store.resolve(barely_future).id == "a"

    barely_past, _ = store.issue("b", lifetime=-(CLOCK_SKEW / 2))
    assert store.resolve(barely_past).id == "b"


def test_expiry_is_evaluated_at_use_not_at_issue():
    store = TokenStore()
    secret, record = store.issue("ci", lifetime=timedelta(hours=1))

    assert store.resolve(secret).id == "ci"
    later = datetime.now(UTC) + timedelta(hours=2)
    with pytest.raises(TokenExpired):
        store.resolve(secret, now=later)


# --------------------------------------------------------------------------
# Revocation
# --------------------------------------------------------------------------


def test_revoking_a_token_takes_effect_without_a_restart():
    """The whole point: a leaked credential must not need a redeploy."""
    store = TokenStore()
    secret, record = store.issue("ci")
    assert store.resolve(secret).id == "ci"

    assert store.revoke(record.token_id, reason="leaked in a build log") is True
    with pytest.raises(TokenRevoked):
        store.resolve(secret)


def test_revoking_one_token_leaves_the_others_working():
    store = TokenStore()
    first_secret, first = store.issue("a")
    second_secret, _ = store.issue("b")

    store.revoke(first.token_id)
    with pytest.raises(TokenRevoked):
        store.resolve(first_secret)
    assert store.resolve(second_secret).id == "b"


def test_revoking_an_unknown_token_reports_that_it_was_unknown():
    assert TokenStore().revoke("tok_nonexistent") is False


def test_revocation_is_idempotent():
    store = TokenStore()
    secret, record = store.issue("ci")
    assert store.revoke(record.token_id) is True
    assert store.revoke(record.token_id) is True
    with pytest.raises(TokenRevoked):
        store.resolve(secret)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def test_issue_revoke_and_rejection_are_all_audited():
    audit = _Recorder()
    store = TokenStore(audit=audit)

    secret, record = store.issue("ci")
    assert "token.issued" in audit.types()

    store.revoke(record.token_id, reason="rotated")
    assert "token.revoked" in audit.types()

    with pytest.raises(TokenRevoked):
        store.resolve(secret)
    with pytest.raises(Unauthorized):
        store.resolve("a-token-nobody-issued")
    assert audit.types().count("token.rejected") == 2


def test_an_expired_use_is_audited_with_its_reason():
    audit = _Recorder()
    store = TokenStore(audit=audit)
    # Well outside the skew allowance, or this asserts nothing.
    secret, _ = store.issue("ci", lifetime=-(CLOCK_SKEW * 10))
    with pytest.raises(TokenExpired):
        store.resolve(secret)
    reasons = [p.get("reason") for e, p in audit.events if e == "token.rejected"]
    assert "expired" in reasons


def test_no_audit_event_ever_carries_the_secret():
    audit = _Recorder()
    store = TokenStore(audit=audit)
    secret, record = store.issue("ci")
    store.revoke(record.token_id)
    try:
        store.resolve(secret)
    except TokenRevoked:
        pass
    assert secret not in str(audit.events)


def test_a_broken_audit_log_does_not_break_authentication():
    class Exploding:
        def record(self, *a, **k):
            raise RuntimeError("audit sink is down")

    store = TokenStore(audit=Exploding())
    secret, _ = store.issue("ci")
    assert store.resolve(secret).id == "ci"


# --------------------------------------------------------------------------
# Backwards compatibility
# --------------------------------------------------------------------------


def test_the_legacy_environment_token_still_grants_full_access(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_API_TOKEN", "legacy-value")
    store = TokenStore.from_config({})
    principal = store.resolve("legacy-value")
    assert principal.has(ADMIN)
    assert principal.id == "legacy-token"


def test_legacy_rotation_still_accepts_both(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_API_TOKEN", "old,new")
    store = TokenStore.from_config({})
    assert store.resolve("old").has(ADMIN)
    assert store.resolve("new").has(ADMIN)


def test_configured_principals_get_their_lifetime(monkeypatch):
    monkeypatch.setenv("CI_TOKEN", "ci-secret-value")
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)
    store = TokenStore.from_config(
        {
            "principals": [
                {
                    "id": "ci",
                    "token_env": "CI_TOKEN",
                    "scopes": ["executions.read"],
                    "lifetime_days": 30,
                }
            ]
        }
    )
    listed = store.list_tokens()
    assert len(listed) == 1
    assert listed[0]["expires_at"] is not None
    assert store.resolve("ci-secret-value").id == "ci"


def test_tokens_loaded_from_the_environment_are_hashed(monkeypatch):
    monkeypatch.setenv("CI_TOKEN", "ci-secret-value")
    monkeypatch.delenv("ORCHESTRATOR_API_TOKEN", raising=False)
    store = TokenStore.from_config({"principals": [{"id": "ci", "token_env": "CI_TOKEN"}]})
    assert "ci-secret-value" not in str(store.list_tokens())


# --------------------------------------------------------------------------
# Trusted reverse-proxy identity
# --------------------------------------------------------------------------


def _proxy_config(**overrides):
    options = dict(
        enabled=True,
        trusted_proxies=("10.0.0.9",),
        group_scopes={"platform-admins": (ADMIN,), "viewers": (EXECUTIONS_READ,)},
    )
    options.update(overrides)
    return ProxyIdentityConfig(**options)


def test_an_identity_header_from_the_trusted_proxy_is_believed():
    principal = proxy_identity(
        "10.0.0.9",
        {"X-Forwarded-User": "alice@example.com", "X-Forwarded-Groups": "viewers"},
        _proxy_config(),
    )
    assert principal is not None
    assert principal.id == "alice@example.com"
    assert principal.source == "proxy"
    assert principal.has(EXECUTIONS_READ)
    assert not principal.has(ADMIN)


def test_the_same_header_from_anyone_else_is_ignored():
    """The mistake this mode exists to avoid: header spoofing."""
    spoofed = proxy_identity(
        "203.0.113.7",
        {"X-Forwarded-User": "alice@example.com", "X-Forwarded-Groups": "platform-admins"},
        _proxy_config(),
    )
    assert spoofed is None


def test_enabling_proxy_identity_without_trusted_proxies_is_refused():
    """Otherwise any caller can become any user by setting a header."""
    with pytest.raises(ConfigurationError) as exc:
        ProxyIdentityConfig(enabled=True).validate()
    assert "trusted_proxies" in str(exc.value)

    ProxyIdentityConfig(enabled=True, trusted_proxies=("10.0.0.9",)).validate()
    ProxyIdentityConfig(enabled=False).validate()


def test_groups_map_to_scopes_and_unknown_groups_grant_nothing():
    principal = proxy_identity(
        "10.0.0.9",
        {"X-Forwarded-User": "bob", "X-Forwarded-Groups": "some-other-group"},
        _proxy_config(),
    )
    assert principal is not None
    assert principal.scopes == frozenset()


def test_multiple_groups_union_their_scopes():
    principal = proxy_identity(
        "10.0.0.9",
        {"X-Forwarded-User": "carol", "X-Forwarded-Groups": "viewers, platform-admins"},
        _proxy_config(),
    )
    assert principal.has(ADMIN)
    assert principal.has(EXECUTIONS_READ)


def test_the_tenant_header_is_honoured_from_a_trusted_proxy():
    principal = proxy_identity(
        "10.0.0.9",
        {"X-Forwarded-User": "dave", "X-Forwarded-Tenant": "acme"},
        _proxy_config(),
    )
    assert principal.tenant == "acme"


def test_a_missing_user_header_yields_no_identity():
    assert (
        proxy_identity("10.0.0.9", {"X-Forwarded-Groups": "viewers"}, _proxy_config())
        is None
    )


def test_proxy_identity_is_off_unless_enabled():
    assert (
        proxy_identity(
            "10.0.0.9",
            {"X-Forwarded-User": "alice"},
            ProxyIdentityConfig(enabled=False, trusted_proxies=("10.0.0.9",)),
        )
        is None
    )


def test_header_matching_is_case_insensitive():
    """HTTP header names are case-insensitive; a proxy may send any casing."""
    for name in ("X-Forwarded-User", "x-forwarded-user", "X-FORWARDED-USER"):
        principal = proxy_identity("10.0.0.9", {name: "eve"}, _proxy_config())
        assert principal is not None and principal.id == "eve", name


def test_no_password_handling_exists_anywhere_in_this_module():
    """A deliberate boundary, asserted so it stays one."""
    import orchestrator.api.tokens as module

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    for forbidden in (
        "def verify_password",
        "password_hash",
        "bcrypt",
        "argon2",
        "def login(",
    ):
        assert forbidden not in source, f"password handling crept in: {forbidden}"
