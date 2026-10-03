"""Console settings: what may be read, what may be written, and by whom.

The risk this surface carries is disclosure and privilege escalation, not
correctness, so most of these tests assert a refusal rather than a result.
"""

from __future__ import annotations

import os
import stat
import types

import pytest

from orchestrator.api import settings as S
from orchestrator.errors import ConfigurationError, PermissionDenied


class FakeConfig:
    """Enough Config for the settings module, with a settable profile."""

    def __init__(self, profile="development", providers=(), tmp_path=None):
        self.profile = types.SimpleNamespace(name=profile)
        self._providers = list(providers)
        self._tmp = tmp_path
        self._sources: list[str] = []

    def get(self, path, default=None):
        if path == "models.providers":
            return self._providers
        if path == "storage.path" and self._tmp is not None:
            return str(self._tmp / "state.db")
        if path == "workspace":
            return str(self._tmp) if self._tmp else "."
        return default

    def section(self, path):
        return {}

    def sources(self):
        return self._sources


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------


def test_a_key_stored_where_a_variable_name_belongs_is_never_echoed(tmp_path):
    """The most common misconfiguration must not become a disclosure.

    Putting the key itself in ``api_key_env`` fails silently — the lookup finds
    nothing and every call 401s. A settings screen that echoed that field back
    would hand the credential to anyone who could read it.
    """
    leaked = "sk-or-v1-" + "a" * 56
    config = FakeConfig(
        providers=[{"name": "openrouter", "type": "openai", "api_key_env": leaked}],
        tmp_path=tmp_path,
    )

    payload = S.describe(config)
    assert leaked not in repr(payload)
    assert leaked[:20] not in repr(payload)

    key = payload["providers"][0]["key"]
    assert key["set"] is False
    assert key["env"] is None
    assert "environment variable" in key["problem"]


def test_a_configured_key_reports_presence_but_not_value(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_PROVIDER_API_KEY", "abcdefghijklmnop-TAILEND")
    config = FakeConfig(
        providers=[{"name": "demo", "api_key_env": "DEMO_PROVIDER_API_KEY"}],
        tmp_path=tmp_path,
    )
    key = S.describe(config)["providers"][0]["key"]

    assert key["set"] is True
    assert key["hint"] == "...LEND"  # enough to tell two keys apart
    assert "abcdefghijklmnop" not in repr(key)  # and no more than that


def test_a_local_provider_reports_that_it_needs_no_key(tmp_path):
    config = FakeConfig(providers=[{"name": "ollama"}], tmp_path=tmp_path)
    provider = S.describe(config)["providers"][0]
    assert provider["needs_key"] is False
    assert provider["key"] is None


# ---------------------------------------------------------------------------
# The development-only write rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["internal-pilot", "production"])
def test_settings_are_read_only_outside_development(profile, tmp_path):
    """A deployment serving other people cannot be reconfigured over HTTP."""
    config = FakeConfig(profile=profile, tmp_path=tmp_path)

    writable, reason = S.writable(config)
    assert writable is False
    assert profile in reason

    with pytest.raises(PermissionDenied):
        S.set_provider_key(config, "SOME_API_KEY", "abcdefghijkl")
    with pytest.raises(PermissionDenied):
        S.set_profile(config, "development")


def test_development_may_promote_itself(tmp_path):
    """Tightening is allowed; it is the direction that is safe."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("profile: development\nworkspace: .\n", encoding="utf-8")
    config = FakeConfig(tmp_path=tmp_path)
    config._sources = [str(cfg_file)]

    S.set_profile(config, "production")
    assert "profile: production" in cfg_file.read_text(encoding="utf-8")


def test_a_profile_is_written_even_when_the_file_never_declared_one(tmp_path):
    """Omitting the line means production; making it explicit must still work."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("workspace: .\n", encoding="utf-8")
    config = FakeConfig(tmp_path=tmp_path)
    config._sources = [str(cfg_file)]

    S.set_profile(config, "internal-pilot")
    assert cfg_file.read_text(encoding="utf-8").startswith("profile: internal-pilot")


def test_an_unknown_profile_is_refused(tmp_path):
    config = FakeConfig(tmp_path=tmp_path)
    with pytest.raises(ConfigurationError):
        S.set_profile(config, "staging")


# ---------------------------------------------------------------------------
# Which variables may be written at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "ORCHESTRATOR_API_TOKEN",
        "PATH",
        "PYTHONPATH",
        "LD_PRELOAD",
        "ORCHESTRATOR_POSTGRES_DSN",
    ],
)
def test_variables_that_grant_access_or_locate_code_are_refused(name, tmp_path):
    """Otherwise this endpoint is 'set any environment variable in the server'."""
    config = FakeConfig(tmp_path=tmp_path)
    with pytest.raises(PermissionDenied):
        S.set_provider_key(config, name, "abcdefghijklmnop")


@pytest.mark.parametrize("name", ["SOMETHING_ELSE", "HOME", "EDITOR"])
def test_names_that_are_not_credential_shaped_are_refused(name, tmp_path):
    config = FakeConfig(tmp_path=tmp_path)
    with pytest.raises(ConfigurationError):
        S.set_provider_key(config, name, "abcdefghijklmnop")


def test_a_malformed_name_is_refused(tmp_path):
    config = FakeConfig(tmp_path=tmp_path)
    for bad in ("A B_API_KEY", "", "X", "WITH-DASH_API_KEY", "1LEADING_API_KEY"):
        with pytest.raises(ConfigurationError):
            S.set_provider_key(config, bad, "abcdefghijklmnop")


def test_a_lowercase_name_is_normalised_rather_than_refused(tmp_path):
    """Environment variables are uppercase by convention, not by rule.

    Someone typing the name of their key in lower case means the same variable;
    refusing them over a shift key would be pedantry, so it is normalised and
    stored under the canonical name.
    """
    config = FakeConfig(tmp_path=tmp_path)
    result = S.set_provider_key(config, "openrouter_api_key", "abcdefghijklmnop")
    assert result["env"] == "OPENROUTER_API_KEY"
    assert "OPENROUTER_API_KEY=" in S.secrets_file(config).read_text()


def test_a_key_too_short_to_be_real_is_refused_and_nothing_is_written(tmp_path):
    config = FakeConfig(tmp_path=tmp_path)
    with pytest.raises(ConfigurationError):
        S.set_provider_key(config, "DEMO_API_KEY", "short")
    assert not S.secrets_file(config).exists()


# ---------------------------------------------------------------------------
# Storing and removing
# ---------------------------------------------------------------------------


def test_a_key_is_stored_live_and_removable(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_API_KEY", raising=False)
    config = FakeConfig(tmp_path=tmp_path)

    result = S.set_provider_key(config, "DEMO_API_KEY", "abcdefghijklmnop")
    assert result["set"] is True
    # Live in this process, so the next run picks it up without a restart.
    import os

    assert os.environ["DEMO_API_KEY"] == "abcdefghijklmnop"
    assert "DEMO_API_KEY=abcdefghijklmnop" in S.secrets_file(config).read_text()

    removed = S.set_provider_key(config, "DEMO_API_KEY", "")
    assert removed["set"] is False
    assert "DEMO_API_KEY" not in os.environ
    assert "abcdefghijklmnop" not in S.secrets_file(config).read_text()


def test_keys_are_never_written_into_the_configuration_file(tmp_path):
    """The file people paste into issues must stay free of credentials."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("profile: development\n", encoding="utf-8")
    config = FakeConfig(tmp_path=tmp_path)
    config._sources = [str(cfg_file)]

    S.set_provider_key(config, "DEMO_API_KEY", "abcdefghijklmnop")
    assert "abcdefghijklmnop" not in cfg_file.read_text(encoding="utf-8")


def test_the_real_environment_beats_the_stored_file(tmp_path, monkeypatch):
    """An exported key was a deliberate act by someone with shell access.

    One in the file was typed into a web form. When they disagree the stronger
    claim wins, or a stale saved key would silently shadow a rotated one.
    """
    config = FakeConfig(tmp_path=tmp_path)
    S.set_provider_key(config, "DEMO_API_KEY", "from-the-file-value")

    monkeypatch.setenv("DEMO_API_KEY", "from-the-environment")
    loaded = S.load_secrets(config)

    import os

    assert os.environ["DEMO_API_KEY"] == "from-the-environment"
    assert "DEMO_API_KEY" not in loaded


def test_stored_keys_load_when_the_environment_is_empty(tmp_path, monkeypatch):
    config = FakeConfig(tmp_path=tmp_path)
    S.set_provider_key(config, "DEMO_API_KEY", "stored-value-here")
    monkeypatch.delenv("DEMO_API_KEY", raising=False)

    assert "DEMO_API_KEY" in S.load_secrets(config)

    import os

    assert os.environ["DEMO_API_KEY"] == "stored-value-here"


# ---------------------------------------------------------------------------
# Where things are saved
# ---------------------------------------------------------------------------


def test_paths_are_reported_absolute(tmp_path):
    """'Where is my data?' is not answered by a relative path."""
    config = FakeConfig(tmp_path=tmp_path)
    payload = S.describe(config)

    from pathlib import Path

    assert Path(payload["storage"]["path"]).is_absolute()
    assert Path(payload["workspace"]["path"]).is_absolute()
    assert Path(payload["storage"]["secrets_file"]).is_absolute()
    # Credentials sit beside the database, not in the workspace, so a run that
    # is allowed to read its own working directory cannot read them.
    assert payload["storage"]["secrets_file"].endswith(S.SECRETS_FILENAME)


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes; Windows does not honour them"
)
def test_the_secrets_file_is_never_group_or_world_readable(tmp_path):
    """0600 at creation, not 0600 shortly afterwards.

    A write followed by a chmod leaves the file at the umask default in
    between, and a reader only needs that window once. This asserts the mode
    on a file the module created from scratch, which is the case the opener
    exists for.
    """
    config = FakeConfig(tmp_path=tmp_path)
    path = S.secrets_file(config)
    assert not path.exists()

    # A permissive umask, so a mode of 0600 can only come from the opener.
    previous = os.umask(0o000)
    try:
        S.set_provider_key(config, "DEMO_API_KEY", "abcdefghijklmnop")
    finally:
        os.umask(previous)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)
