"""PostgreSQL configuration through the real `load()` path.

Regression tests for a gap that unit tests could not see: `PostgresStateStore`
was implemented and its own tests passed, but `config.loader._ENUM_FIELDS`
listed only `sqlite` and `memory`. Every documented PostgreSQL configuration
was rejected before it reached the store, so `orchestrator serve` with
`storage.backend: postgres` refused to start.

Everything here goes through `load()` — the same function the CLI and the API
use — rather than constructing a `Config` directly. That is the whole point:
a config the application will never accept is not a supported config.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.config.loader import load
from orchestrator.errors import ConfigurationError


def _load(tmp_path, document: str):
    """Load a config the way the application does."""
    path = tmp_path / "config.yaml"
    path.write_text(document, encoding="utf-8")
    return load(paths=[str(path)], include_discovered=False)


VALID = """profile: production
storage:
  backend: postgres
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN
    min_connections: 2
    max_connections: 20
    command_timeout: 30
"""


# --------------------------------------------------------------------------
# The backend is reachable at all
# --------------------------------------------------------------------------


def test_postgres_is_an_accepted_backend(tmp_path):
    """The regression: documented and rejected."""
    config = _load(tmp_path, VALID)
    assert config.get("storage.backend") == "postgres"


def test_every_documented_backend_loads(tmp_path):
    for backend in ("sqlite", "memory", "postgres"):
        extra = (
            "  postgres:\n    dsn_env: ORCHESTRATOR_POSTGRES_DSN\n"
            if backend == "postgres"
            else ""
        )
        config = _load(
            tmp_path,
            f"profile: development\nstorage:\n  backend: {backend}\n{extra}",
        )
        assert config.get("storage.backend") == backend


def test_an_unknown_backend_is_rejected_and_names_the_real_ones(tmp_path):
    with pytest.raises(ConfigurationError) as exc:
        _load(tmp_path, "profile: development\nstorage:\n  backend: mysql\n")
    message = str(exc.value)
    assert "mysql" in message
    for backend in ("sqlite", "memory", "postgres"):
        assert backend in message


# --------------------------------------------------------------------------
# Credentials must not be in the file
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["dsn", "url", "password", "user", "credentials"])
def test_a_plaintext_credential_field_is_rejected(tmp_path, field):
    """A DSN carries a password and a config file gets committed."""
    with pytest.raises(ConfigurationError) as exc:
        _load(
            tmp_path,
            f"""profile: production
storage:
  backend: postgres
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN
    {field}: postgresql://user:hunter2@host/db
""",
        )
    message = str(exc.value)
    assert f"storage.postgres.{field}" in message
    assert "dsn_env" in message, "the error must say what to use instead"
    # The rejected value must not be echoed back into the error.
    assert "hunter2" not in message


def test_an_unknown_key_in_the_postgres_block_is_rejected(tmp_path):
    """An allowlist, not a denylist: a typo'd secret field would slip a denylist."""
    with pytest.raises(ConfigurationError) as exc:
        _load(
            tmp_path,
            """profile: production
storage:
  backend: postgres
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN
    connection_string: postgresql://user:hunter2@host/db
""",
        )
    message = str(exc.value)
    assert "connection_string" in message
    assert "hunter2" not in message


def test_dsn_env_is_required(tmp_path):
    with pytest.raises(ConfigurationError) as exc:
        _load(tmp_path, "profile: production\nstorage:\n  backend: postgres\n")
    assert "dsn_env" in str(exc.value)


def test_dsn_env_must_be_a_valid_variable_name(tmp_path):
    for bad in ("has spaces", "lower-case-dashes", "1STARTS_WITH_DIGIT", "has$dollar", ""):
        with pytest.raises(ConfigurationError) as exc:
            _load(
                tmp_path,
                f"""profile: production
storage:
  backend: postgres
  postgres:
    dsn_env: "{bad}"
""",
            )
        assert "dsn_env" in str(exc.value), bad


def test_a_dsn_env_that_looks_like_a_dsn_is_rejected(tmp_path):
    """The commonest mistake: pasting the DSN where the variable name goes."""
    with pytest.raises(ConfigurationError) as exc:
        _load(
            tmp_path,
            """profile: production
storage:
  backend: postgres
  postgres:
    dsn_env: postgresql://user:hunter2@host:5432/db
""",
        )
    message = str(exc.value)
    assert "dsn_env" in message
    assert "hunter2" not in message


# --------------------------------------------------------------------------
# Pool sizing and timeouts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("min_connections", 0),
        ("min_connections", -1),
        ("max_connections", 0),
        ("max_connections", -5),
        ("max_connections", 1001),
        ("command_timeout", 0),
        ("command_timeout", -1),
        ("command_timeout", 4000),
    ],
)
def test_an_out_of_range_pool_or_timeout_value_is_rejected(tmp_path, key, value):
    with pytest.raises(ConfigurationError) as exc:
        _load(
            tmp_path,
            f"""profile: production
storage:
  backend: postgres
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN
    {key}: {value}
""",
        )
    assert f"storage.postgres.{key}" in str(exc.value)


def test_min_connections_may_not_exceed_max(tmp_path):
    """asyncpg raises at connect time; catching it at config time is cheaper."""
    with pytest.raises(ConfigurationError) as exc:
        _load(
            tmp_path,
            """profile: production
storage:
  backend: postgres
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN
    min_connections: 10
    max_connections: 5
""",
        )
    assert "min_connections" in str(exc.value)


def test_a_non_numeric_pool_value_is_rejected(tmp_path):
    with pytest.raises(ConfigurationError) as exc:
        _load(
            tmp_path,
            """profile: production
storage:
  backend: postgres
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN
    max_connections: "lots"
""",
        )
    assert "max_connections" in str(exc.value)


def test_sensible_values_are_accepted(tmp_path):
    config = _load(tmp_path, VALID)
    section = config.section("storage").get("postgres")
    assert section["dsn_env"] == "ORCHESTRATOR_POSTGRES_DSN"
    assert section["min_connections"] == 2
    assert section["max_connections"] == 20


def test_the_postgres_block_is_ignored_for_other_backends(tmp_path):
    """A leftover block must not fail a config that does not use it."""
    config = _load(
        tmp_path,
        """profile: development
storage:
  backend: sqlite
  postgres:
    dsn_env: SOMETHING_UNSET
""",
    )
    assert config.get("storage.backend") == "sqlite"


# --------------------------------------------------------------------------
# Startup reaches the store
# --------------------------------------------------------------------------


def test_startup_selects_the_postgres_store_after_real_validation(tmp_path, monkeypatch):
    """Validated config must actually reach `PostgresStateStore.connect`.

    The connection itself is not attempted here — that needs a server, and
    those tests live in test_postgres_store.py. What is asserted is that the
    startup path gets that far, which is what was broken.
    """
    import asyncio

    from orchestrator.core.state import postgres_store
    from orchestrator.platform import _build_store_async

    monkeypatch.setenv("ORCHESTRATOR_POSTGRES_DSN", "postgresql://localhost/x")

    captured: dict = {}

    class _Sentinel:
        pass

    async def fake_connect(dsn, **kwargs):
        captured["dsn"] = dsn
        captured.update(kwargs)
        return _Sentinel()

    monkeypatch.setattr(postgres_store.PostgresStateStore, "connect", fake_connect)

    config = _load(tmp_path, VALID)
    store = asyncio.run(_build_store_async(config))

    assert isinstance(store, _Sentinel)
    assert captured["dsn"] == "postgresql://localhost/x"
    # The validated pool settings are the ones that reach the driver.
    assert captured["min_size"] == 2
    assert captured["max_size"] == 20
    assert captured["command_timeout"] == 30


def test_a_missing_dsn_variable_fails_startup_with_a_useful_message(tmp_path, monkeypatch):
    import asyncio

    from orchestrator.platform import _build_store_async

    monkeypatch.delenv("ORCHESTRATOR_POSTGRES_DSN", raising=False)
    config = _load(tmp_path, VALID)

    with pytest.raises(ConfigurationError) as exc:
        asyncio.run(_build_store_async(config))
    assert "ORCHESTRATOR_POSTGRES_DSN" in str(exc.value)
