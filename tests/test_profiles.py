"""Deployment profiles and secure-by-default policy.

The point of these tests is that the strict defaults actually deny something.
A `default_effect: deny` that nothing consults is decorative, and decorative
security is worse than none because it is believed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.config import profiles
from orchestrator.config.loader import Config
from orchestrator.core.domain.enums import RiskLevel
from orchestrator.core.policy.engine import OperationDescriptor
from orchestrator.errors import ConfigurationError
from orchestrator.platform import _build_policy


def _operation(name="fs.read_file", kind="tool", **kwargs):
    return OperationDescriptor(kind=kind, name=name, **kwargs)


# --------------------------------------------------------------------------
# Which profile applies
# --------------------------------------------------------------------------


def test_an_undeclared_profile_is_production_not_development():
    """The operator who has not thought about it gets the strict posture."""
    assert Config({}).profile.name == profiles.PRODUCTION
    assert profiles.DEFAULT_PROFILE == profiles.PRODUCTION


def test_each_profile_is_selectable_by_name():
    for name in profiles.PROFILES:
        assert Config({"profile": name}).profile.name == name


def test_common_near_misses_are_accepted():
    assert Config({"profile": "prod"}).profile.name == profiles.PRODUCTION
    assert Config({"profile": "dev"}).profile.name == profiles.DEVELOPMENT
    assert Config({"profile": "internal_pilot"}).profile.name == profiles.INTERNAL_PILOT


def test_an_unknown_profile_is_rejected_with_the_real_options():
    with pytest.raises(ConfigurationError) as exc:
        Config({"profile": "staging"})
    assert "production" in str(exc.value)


# --------------------------------------------------------------------------
# The defaults each profile implies
# --------------------------------------------------------------------------


def test_production_denies_by_default_and_requires_explicit_grants():
    config = Config({"profile": "production"})
    assert config.get("policy.default_effect") == "deny"
    assert config.get("policy.require_explicit_tool_grant") is True
    assert config.get("tools.http.allow_http") is False
    assert config.get("tools.http.allow_private_networks") is False


def test_development_stays_permissive_so_it_remains_usable():
    config = Config({"profile": "development"})
    assert config.get("policy.default_effect") == "allow"
    assert config.get("policy.require_explicit_tool_grant") is False
    assert config.get("tools.http.allow_http") is True
    assert config.get("tools.http.allow_loopback") is True


def test_internal_pilot_is_strict_but_reaches_internal_services():
    """A pilot usually exists to talk to internal systems; it still denies by default."""
    config = Config({"profile": "internal-pilot"})
    assert config.get("policy.default_effect") == "deny"
    assert config.get("policy.require_explicit_tool_grant") is True
    assert config.get("tools.http.allow_private_networks") is True
    assert config.get("tools.http.allow_http") is False


def test_no_profile_permits_plain_http_except_development():
    for name in (profiles.INTERNAL_PILOT, profiles.PRODUCTION):
        assert Config({"profile": name}).get("tools.http.allow_http") is False


def test_no_profile_enables_process_tools_by_default():
    """Process execution is privileged everywhere; development merely allows it."""
    for name in profiles.PROFILES:
        assert Config({"profile": name}).get("tools.process.enabled") in (None, False)


# --------------------------------------------------------------------------
# Explicit config always wins
# --------------------------------------------------------------------------


def test_explicit_config_overrides_a_profile_in_both_directions():
    loosened = Config({"profile": "production", "policy": {"default_effect": "allow"}})
    assert loosened.get("policy.default_effect") == "allow"

    tightened = Config({"profile": "development", "policy": {"default_effect": "deny"}})
    assert tightened.get("policy.default_effect") == "deny"


def test_the_written_config_is_preserved_beside_the_effective_one():
    config = Config({"profile": "production"})
    assert "default_effect" not in (config.raw.get("policy") or {})
    assert config.get("policy.default_effect") == "deny"


def test_posture_reports_where_each_value_came_from():
    config = Config({"profile": "production", "policy": {"default_effect": "allow"}})
    posture = config.posture()
    assert posture["policy"]["default_effect_from"] == "config"

    inherited = Config({"profile": "production"}).posture()
    assert inherited["policy"]["default_effect_from"] == "profile:production"


# --------------------------------------------------------------------------
# The defaults actually deny — the part that matters
# --------------------------------------------------------------------------


def test_production_policy_denies_an_ungranted_tool():
    policy = _build_policy(Config({"profile": "production"}))
    decision = policy.evaluate(
        _operation("fs.read_file"), granted_permissions=["fs.read"]
    )
    assert decision.allowed is False
    assert "explicit" in decision.reason.lower() or "default" in decision.reason.lower()


def test_production_policy_allows_a_tool_that_was_explicitly_granted():
    policy = _build_policy(
        Config({
            "profile": "production",
            "policy": {
                "rules": [
                    {"kind": "tool", "subject": "fs.read_file", "effect": "allow",
                     "reason": "explicitly granted for this deployment"}
                ]
            },
        })
    )
    decision = policy.evaluate(
        _operation("fs.read_file"), granted_permissions=["fs.read"]
    )
    assert decision.allowed is True


def test_development_policy_allows_the_same_ungranted_tool():
    """The difference between the profiles has to be observable, or it is fiction."""
    policy = _build_policy(Config({"profile": "development"}))
    decision = policy.evaluate(
        _operation("fs.read_file"), granted_permissions=["fs.read"]
    )
    assert decision.allowed is True


def test_missing_permissions_are_denied_under_every_profile():
    """Least privilege is not a profile setting; it applies everywhere."""
    for name in profiles.PROFILES:
        policy = _build_policy(Config({"profile": name}))
        decision = policy.evaluate(
            _operation("fs.write_file", permissions=["fs.write"]),
            granted_permissions=["fs.read"],
        )
        assert decision.allowed is False, name


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def test_a_legacy_config_is_told_exactly_what_changed():
    """Silently changing what an existing config does is the outcome to avoid."""
    notices = Config({}).migration_notices()
    paths = {n.path for n in notices}
    assert "policy.default_effect" in paths
    assert "policy.require_explicit_tool_grant" in paths
    assert "tools.http.allowed_hosts" in paths
    for notice in notices:
        assert notice.consequence
        assert notice.describe()


def test_a_config_that_sets_a_value_explicitly_gets_no_notice_for_it():
    notices = Config({"policy": {"default_effect": "allow"}}).migration_notices()
    assert "policy.default_effect" not in {n.path for n in notices}


def test_an_empty_http_allowlist_is_reported_because_its_meaning_inverted():
    """It used to mean 'any host'. It now means 'none'. That is worth saying."""
    notices = Config({"tools": {"http": {"allowed_hosts": []}}}).migration_notices()
    assert "tools.http.allowed_hosts" in {n.path for n in notices}


def test_development_configs_get_no_migration_noise():
    assert Config({"profile": "development"}).migration_notices() == []


# --------------------------------------------------------------------------
# The real loader path
# --------------------------------------------------------------------------
#
# Regression: every test above constructed Config({...}) directly and passed,
# while the actual `load()` path ran allow-by-default under a production
# profile. The built-in defaults layer seeded policy.default_effect="allow"
# into the raw document, which made it indistinguishable from an operator's
# explicit choice, so the profile never applied.
#
# These go through load() for that reason.


def _loaded(tmp_path, document: str):
    from orchestrator.config.loader import load

    path = tmp_path / "config.yaml"
    path.write_text(document, encoding="utf-8")
    return load(paths=[str(path)], include_discovered=False)


def test_the_builtin_defaults_do_not_preempt_the_profile(tmp_path):
    """The bug: seeded defaults made the production profile unreachable."""
    from orchestrator.config.loader import DEFAULTS

    assert "default_effect" not in DEFAULTS["policy"]
    assert "require_explicit_tool_grant" not in DEFAULTS["policy"]


def test_a_loaded_production_config_really_denies_by_default(tmp_path):
    config = _loaded(tmp_path, "profile: production\nstorage:\n  backend: memory\n")
    assert config.get("policy.default_effect") == "deny"
    assert config.get("policy.require_explicit_tool_grant") is True

    policy = _build_policy(config)
    assert policy.config.default_effect == "deny"
    decision = policy.evaluate(_operation("fs.read_file"),
                               granted_permissions=["fs.read"])
    assert decision.allowed is False


def test_a_loaded_development_config_really_allows(tmp_path):
    config = _loaded(tmp_path, "profile: development\nstorage:\n  backend: memory\n")
    policy = _build_policy(config)
    assert policy.config.default_effect == "allow"
    assert policy.evaluate(_operation("fs.read_file"),
                           granted_permissions=["fs.read"]).allowed is True


def test_a_loaded_config_with_no_profile_denies(tmp_path):
    """The whole point of the default: silence must not mean permissive."""
    config = _loaded(tmp_path, "storage:\n  backend: memory\n")
    assert config.profile.name == profiles.PRODUCTION
    assert _build_policy(config).config.default_effect == "deny"


def test_an_explicit_choice_in_a_loaded_file_still_wins(tmp_path):
    config = _loaded(
        tmp_path,
        "profile: production\npolicy:\n  default_effect: allow\nstorage:\n  backend: memory\n",
    )
    assert _build_policy(config).config.default_effect == "allow"
