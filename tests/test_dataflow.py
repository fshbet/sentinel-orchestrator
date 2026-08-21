"""Redaction and model egress policy."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.errors import PolicyViolation
from orchestrator.llm.dataflow import (
    APPROVED,
    CONFIDENTIAL,
    INTERNAL,
    LOCAL,
    PROHIBITED,
    PUBLIC,
    RESTRICTED,
    UNAPPROVED,
    DataFlowPolicy,
    ProviderPolicy,
    rank,
)
from orchestrator.observability.logging import redact, redact_text


# --------------------------------------------------------------------------
# Redaction of values, not just key names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,label",
    [
        ("calling with sk-or-v1-abcdefghij1234567890 now", "openrouter"),
        ("key nvapi-QWERTYUIOPASDFGHJKLZXCVBNM12345 rejected", "nvidia"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123 expired", "github"),
        ("id AKIAIOSFODNN7EXAMPLE used", "aws_key_id"),
        ("slack xoxb-1234567890-abcdefghijkl posted", "slack"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "bearer"),
        ("git clone https://user:hunter2@example.com/r.git", "url_credentials"),
    ],
)
def test_credential_shaped_values_are_redacted(text, label):
    """Key-name redaction misses secrets carried inside messages and stdout."""
    result = redact_text(text)
    assert f"[redacted:{label}]" in result, result


def test_a_jwt_is_redacted():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert jwt not in redact_text(f"got {jwt} back")


def test_a_private_key_block_is_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxyz123\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert "MIIEowIBAAKCAQEAxyz123" not in redact_text(f"loaded {pem} ok")


@pytest.mark.parametrize(
    "text",
    [
        "commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        "input_tokens=8140 output_tokens=612",
        "model nvidia/nemotron-3-super-120b selected",
        "path /usr/local/bin/python3.12",
        "id exe_0mszxbhum250hv9agvj",
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ],
)
def test_legitimate_content_survives(text):
    """A redactor that eats real content gets switched off, protecting nothing."""
    assert redact_text(text) == text


def test_short_values_are_left_alone():
    assert redact_text("sk-abc") == "sk-abc"


def test_redaction_reaches_values_nested_in_structures():
    payload = {
        "stdout": "exported nvapi-QWERTYUIOPASDFGHJKLZXCVBNM12345",
        "usage": {"input_tokens": 100},
        "items": ["sk-or-v1-abcdefghij1234567890"],
    }
    result = redact(payload)
    assert "nvapi-QWERTY" not in str(result)
    assert "sk-or-v1-abcdefghij" not in str(result)
    assert result["usage"]["input_tokens"] == 100


def test_key_based_redaction_still_applies():
    result = redact({"api_key": "anything", "input_tokens": 42})
    assert result["api_key"] == "[redacted]"
    assert result["input_tokens"] == 42


# --------------------------------------------------------------------------
# Classification ordering
# --------------------------------------------------------------------------


def test_classifications_are_ordered_by_sensitivity():
    assert rank(PUBLIC) < rank(INTERNAL) < rank(CONFIDENTIAL) < rank(RESTRICTED)


def test_an_unknown_classification_ranks_as_most_sensitive():
    """Guessing downward is guessing in the direction that loses data."""
    assert rank("something-nobody-defined") > rank(RESTRICTED)


# --------------------------------------------------------------------------
# Provider dispositions
# --------------------------------------------------------------------------


def test_an_undeclared_provider_is_unapproved_not_public():
    """"We never decided" must not read as "cleared for public data"."""
    policy = DataFlowPolicy()
    decision = policy.evaluate("some-cloud-provider", PUBLIC)
    assert decision.allowed is False
    assert decision.disposition == UNAPPROVED
    assert "no declared data policy" in decision.reason


def test_a_local_provider_accepts_every_classification():
    policy = DataFlowPolicy({
        "ollama": ProviderPolicy("ollama", disposition=LOCAL,
                                 max_classification=RESTRICTED)
    })
    for classification in (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED):
        assert policy.evaluate("ollama", classification).allowed is True


def test_a_prohibited_provider_never_receives_data():
    policy = DataFlowPolicy({
        "banned": ProviderPolicy("banned", disposition=PROHIBITED,
                                 max_classification=RESTRICTED)
    })
    for classification in (PUBLIC, RESTRICTED):
        assert policy.evaluate("banned", classification).allowed is False


def test_an_approved_provider_is_bounded_by_its_maximum():
    policy = DataFlowPolicy({
        "openrouter": ProviderPolicy("openrouter", disposition=APPROVED,
                                     max_classification=INTERNAL,
                                     approval_reference="VENDOR-114"),
    })
    assert policy.evaluate("openrouter", PUBLIC).allowed is True
    assert policy.evaluate("openrouter", INTERNAL).allowed is True

    denied = policy.evaluate("openrouter", CONFIDENTIAL)
    assert denied.allowed is False
    assert "internal" in denied.reason

    assert policy.evaluate("openrouter", RESTRICTED).allowed is False


def test_the_approval_reference_appears_in_the_reason():
    """The audit trail should say which review cleared this."""
    policy = DataFlowPolicy({
        "x": ProviderPolicy("x", disposition=APPROVED,
                            max_classification=CONFIDENTIAL,
                            approval_reference="DPA-2026-04"),
    })
    assert "DPA-2026-04" in policy.evaluate("x", CONFIDENTIAL).reason


def test_enforce_raises_rather_than_returning_false():
    policy = DataFlowPolicy()
    with pytest.raises(PolicyViolation):
        policy.enforce("unknown-provider", CONFIDENTIAL)


def test_a_decision_records_what_happened_without_the_data():
    policy = DataFlowPolicy({
        "ollama": ProviderPolicy("ollama", disposition=LOCAL),
    })
    recorded = policy.evaluate("ollama", CONFIDENTIAL).to_dict()
    assert recorded["provider"] == "ollama"
    assert recorded["classification"] == CONFIDENTIAL
    assert recorded["disposition"] == LOCAL
    # The decision, never the payload.
    assert set(recorded) == {
        "allowed", "reason", "provider", "classification", "disposition"
    }


def test_the_policy_can_be_disabled_for_deployments_that_do_not_need_it():
    policy = DataFlowPolicy(enabled=False)
    assert policy.evaluate("anything", RESTRICTED).allowed is True


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _config(document):
    from orchestrator.config.loader import Config

    return Config(document)


def test_ollama_is_recognised_as_local_without_being_declared():
    """The one case where the platform knows more than the config does."""
    policy = DataFlowPolicy.from_config(_config({
        "profile": "development",
        "models": {"providers": [{"type": "ollama", "name": "ollama"}]},
    }))
    assert policy.evaluate("ollama", RESTRICTED).allowed is True


def test_a_remote_provider_without_a_data_policy_is_unapproved():
    policy = DataFlowPolicy.from_config(_config({
        "profile": "internal-pilot",
        "models": {"providers": [
            {"type": "openai_compatible", "name": "openrouter"}
        ]},
    }))
    decision = policy.evaluate("openrouter", PUBLIC)
    assert decision.allowed is False
    assert decision.disposition == UNAPPROVED


def test_a_declared_approval_is_honoured():
    policy = DataFlowPolicy.from_config(_config({
        "profile": "internal-pilot",
        "models": {"providers": [{
            "type": "openai_compatible",
            "name": "openrouter",
            "data_policy": {
                "disposition": "approved",
                "max_classification": "internal",
                "approval_reference": "VENDOR-114",
            },
        }]},
    }))
    assert policy.evaluate("openrouter", INTERNAL).allowed is True
    assert policy.evaluate("openrouter", CONFIDENTIAL).allowed is False


def test_an_invalid_disposition_is_rejected():
    from orchestrator.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        DataFlowPolicy.from_config(_config({
            "profile": "development",
            "models": {"providers": [{
                "type": "openai_compatible", "name": "x",
                "data_policy": {"disposition": "probably-fine"},
            }]},
        }))


def test_the_default_classification_applies_when_none_is_given():
    policy = DataFlowPolicy.from_config(_config({
        "profile": "internal-pilot",
        "data": {"default_classification": "confidential"},
        "models": {"providers": [{
            "type": "openai_compatible", "name": "x",
            "data_policy": {"disposition": "approved",
                            "max_classification": "internal"},
        }]},
    }))
    # No classification passed: the default is used, and it exceeds the max.
    assert policy.evaluate("x").allowed is False
    assert policy.evaluate("x").classification == CONFIDENTIAL


# --------------------------------------------------------------------------
# When enforcement applies
# --------------------------------------------------------------------------
#
# The risk being guarded is data reaching a third-party service nobody
# reviewed, and that risk arrives through configuration. A provider handed
# directly to Orchestrator.create(providers=[...]) is the caller's own object
# in the caller's own process; refusing it breaks every embedding use and
# every test while protecting nothing.


def test_enforcement_is_off_in_development():
    policy = DataFlowPolicy.from_config(_config({
        "profile": "development",
        "models": {"providers": [{"type": "openai_compatible", "name": "x"}]},
    }))
    assert policy.enabled is False
    assert policy.evaluate("x", RESTRICTED).allowed is True


def test_enforcement_is_on_for_internal_pilot_and_production():
    for profile in ("internal-pilot", "production"):
        policy = DataFlowPolicy.from_config(_config({
            "profile": profile,
            "models": {"providers": [{"type": "openai_compatible", "name": "x"}]},
        }))
        assert policy.enabled is True, profile
        assert policy.evaluate("x", PUBLIC).allowed is False, profile


def test_enforcement_stays_off_when_no_provider_is_declared_in_config():
    """Programmatic providers are the caller's, not a model-chosen destination."""
    policy = DataFlowPolicy.from_config(_config({"profile": "production"}))
    assert policy.enabled is False
    assert policy.evaluate("injected-by-the-caller", RESTRICTED).allowed is True


def test_enforcement_can_be_turned_on_explicitly_in_development():
    policy = DataFlowPolicy.from_config(_config({
        "profile": "development",
        "data": {"enforce_egress_policy": True},
        "models": {"providers": [{"type": "openai_compatible", "name": "x"}]},
    }))
    assert policy.enabled is True
    assert policy.evaluate("x", PUBLIC).allowed is False


def test_enforcement_can_be_turned_off_explicitly_in_production():
    """An operator who says no must be obeyed, and the setting is auditable."""
    policy = DataFlowPolicy.from_config(_config({
        "profile": "production",
        "data": {"enforce_egress_policy": False},
        "models": {"providers": [{"type": "openai_compatible", "name": "x"}]},
    }))
    assert policy.enabled is False
