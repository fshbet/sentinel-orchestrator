"""Configuration loading and validation.

Configuration is layered, later layers overriding earlier ones (spec section
51): built-in defaults, then global, then user, then project, then explicit
overrides. Every layer is validated before anything is constructed, so a typo
fails at startup rather than three tasks into a run (spec section 86).

YAML is supported when PyYAML is installed; JSON always works.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError

PROJECT_DIR = ".orchestrator"
CONFIG_NAMES = ("config.yaml", "config.yml", "config.json")

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "workspace": ".",
    "logging": {"level": "warning", "json": True},
    "storage": {"backend": "sqlite", "path": f"{PROJECT_DIR}/state.db"},
    "limits": {
        "max_wall_seconds": 3600.0,
        "max_model_calls": 300,
        "max_tool_calls": 1000,
        "max_tokens": 2000000,
        "max_parallel_tasks": 4,
        "max_task_attempts": 3,
        "max_replans": 3,
        "max_optimizer_iterations": 3,
        "max_external_requests": 500,
        "max_nesting_depth": 2,
    },
    # Deliberately does NOT set default_effect or require_explicit_tool_grant.
    #
    # A value seeded here is indistinguishable, later, from one an operator
    # wrote down — and profiles only fill what is unset. Seeding "allow" here
    # meant the production profile could never take effect: the posture said
    # deny-by-default and the engine ran allow-by-default. Leave these unset
    # so the declared profile is what decides.
    "policy": {
        "deny_threshold": None,
        "version": "1.0.0",
        "rules": [],
    },
    "models": {"providers": []},
    "tools": {
        "bookkeeping": {"enabled": True},
        "filesystem": {"enabled": False, "root": ".", "allow_write": True},
        "process": {"enabled": False, "allowed_commands": []},
        "http": {"enabled": False, "allowed_hosts": [], "timeout": 30.0},
    },
    "mcp": {"servers": {}, "policies": []},
    "agents": {"directories": [], "definitions": []},
    "skills": {"directories": [], "definitions": []},
    "capabilities": [],
    "workflows": {"directories": []},
    "recovery": {
        "allow_replan": True,
        "allow_dynamic_agents": True,
        "allow_human_escalation": True,
    },
    "context": {"max_working_memory": 500},
    "plugins": {
        "enabled": True,
        "entry_point_group": "orchestrator.plugins",
        "modules": [],
    },
    "api": {"host": "127.0.0.1", "port": 8080},
}

# Where a value must be one of a fixed set.
_ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "policy.default_effect": ("allow", "deny", "require_approval"),
    "policy.approval_threshold": ("none", "low", "medium", "high", "critical"),
    "logging.level": ("debug", "info", "warning", "error", "critical"),
    # postgres is the multi-instance backend. It was documented and
    # implemented before it was listed here, so every documented config
    # was rejected at load time — see tests/test_storage_config.py.
    "storage.backend": ("sqlite", "memory", "postgres"),
}


@dataclass
class ConfigLayer:
    source: str
    data: dict[str, Any] = field(default_factory=dict)


class Config:
    """A validated, merged configuration."""

    def __init__(self, data: dict[str, Any], layers: Sequence[ConfigLayer] = ()) -> None:
        from . import profiles as _profiles

        # What was written stays available beside what took effect, so
        # "where did this value come from" is answerable rather than inferred.
        self.raw = copy.deepcopy(data)
        self.profile = _profiles.get(data.get("profile"))
        self.data = _profiles.apply_defaults(data, self.profile)
        self.layers = list(layers)

    def migration_notices(self):
        """Settings whose defaults tightened and that this config leaves unset."""
        from . import profiles as _profiles

        return _profiles.migration_notices(self.raw, self.profile)

    def posture(self) -> dict[str, Any]:
        """The effective security posture, with the source of each value."""
        from . import profiles as _profiles

        return _profiles.explain(self.raw, self.profile)

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, path: str) -> dict[str, Any]:
        value = self.get(path, {})
        return value if isinstance(value, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def sources(self) -> list[str]:
        return [layer.source for layer in self.layers]

    def fingerprint(self) -> str:
        """Stable hash of the effective configuration, pinned onto executions."""
        import hashlib

        blob = json.dumps(self.data, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` onto ``base``. Lists replace; dicts merge."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_document(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        raise ConfigurationError(f"configuration file {file} does not exist")
    text = file.read_text(encoding="utf-8")
    if file.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ConfigurationError(
                f"reading {file} requires PyYAML; install"
                " universal-orchestrator[yaml] or use JSON"
            ) from exc
        data = yaml.safe_load(text) or {}
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"{file} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{file} must contain a mapping at the top level")
    return data


def discover(start: str | Path = ".") -> list[Path]:
    """Find configuration files in precedence order (weakest first)."""
    found: list[Path] = []

    global_dir = os.environ.get("ORCHESTRATOR_GLOBAL_CONFIG")
    if global_dir:
        candidate = Path(global_dir)
        if candidate.is_file():
            found.append(candidate)

    home = Path.home() / PROJECT_DIR
    for name in CONFIG_NAMES:
        candidate = home / name
        if candidate.is_file():
            found.append(candidate)
            break

    # Walk up from the working directory so a nested run finds its project.
    current = Path(start).resolve()
    project_candidates: list[Path] = []
    for directory in [current, *current.parents]:
        for name in CONFIG_NAMES:
            candidate = directory / PROJECT_DIR / name
            if candidate.is_file():
                project_candidates.append(candidate)
                break
        if project_candidates:
            break
    found.extend(project_candidates)

    explicit = os.environ.get("ORCHESTRATOR_CONFIG")
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_file():
            raise ConfigurationError(
                f"ORCHESTRATOR_CONFIG points at {candidate}, which does not exist"
            )
        found.append(candidate)

    return found


def load(
    *,
    paths: Iterable[str | Path] | None = None,
    overrides: dict[str, Any] | None = None,
    start: str | Path = ".",
    include_discovered: bool = True,
) -> Config:
    """Build the effective configuration."""
    layers = [ConfigLayer("built-in defaults", copy.deepcopy(DEFAULTS))]
    merged = copy.deepcopy(DEFAULTS)

    candidates: list[Path] = []
    if include_discovered:
        candidates.extend(discover(start))
    if paths:
        candidates.extend(Path(p) for p in paths)

    for path in candidates:
        document = load_document(path)
        layers.append(ConfigLayer(str(path), document))
        merged = deep_merge(merged, document)

    env_overlay = _from_environment()
    if env_overlay:
        layers.append(ConfigLayer("environment", env_overlay))
        merged = deep_merge(merged, env_overlay)

    if overrides:
        layers.append(ConfigLayer("explicit overrides", overrides))
        merged = deep_merge(merged, overrides)

    validate(merged)
    return Config(merged, layers)


def _from_environment() -> dict[str, Any]:
    """Read the small set of settings that make sense as environment variables."""
    overlay: dict[str, Any] = {}
    mapping = {
        "ORCHESTRATOR_WORKSPACE": ("workspace",),
        "ORCHESTRATOR_LOG_LEVEL": ("logging", "level"),
        "ORCHESTRATOR_STATE_PATH": ("storage", "path"),
        "ORCHESTRATOR_MAX_PARALLEL": ("limits", "max_parallel_tasks"),
    }
    for variable, path in mapping.items():
        value: Any = os.environ.get(variable)
        if value is None:
            continue
        if path[-1] == "max_parallel_tasks":
            try:
                value = int(value)
            except ValueError as exc:
                raise ConfigurationError(
                    f"{variable} must be an integer, got {value!r}"
                ) from exc
        node = overlay
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return overlay


# What a `storage.postgres` block may contain.
#
# An allowlist rather than a denylist of credential-shaped names. A denylist
# has to anticipate every spelling somebody might use for "password", and it
# only takes one it did not anticipate — `connection_string` would sail
# through a list that blocks `dsn` and `url`.
_POSTGRES_KEYS = frozenset(
    {
        "dsn_env",
        "min_connections",
        "max_connections",
        "command_timeout",
        "apply_migrations",
    }
)

# Ranges. Each bound exists because the value outside it is either useless or
# a foot-gun: a pool of zero never connects, a pool of a thousand exhausts
# PostgreSQL's own connection limit, and a timeout of an hour is indefinite
# in practice.
_POSTGRES_RANGES: dict[str, tuple[float, float]] = {
    "min_connections": (1, 100),
    "max_connections": (1, 1000),
    "command_timeout": (1, 3600),
}

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_postgres(section: Any, errors: list[str]) -> None:
    """Check a `storage.postgres` block.

    Never echoes a rejected value back into the error message: the commonest
    mistake here is pasting a DSN where a variable name belongs, and repeating
    it would copy the password into logs, CI output, and issue trackers.
    """
    if section is None:
        errors.append(
            "storage.backend is 'postgres' but storage.postgres.dsn_env is not "
            "set. It must name an environment variable holding the connection "
            "string; a DSN carries a password and must not be written into "
            "configuration."
        )
        return

    if not isinstance(section, dict):
        errors.append("storage.postgres must be a mapping")
        return

    unknown = sorted(set(section) - _POSTGRES_KEYS)
    for key in unknown:
        errors.append(
            f"storage.postgres.{key} is not a recognised setting. Permitted: "
            f"{', '.join(sorted(_POSTGRES_KEYS))}. A connection string must be "
            f"supplied through storage.postgres.dsn_env, which names an "
            f"environment variable — never written into configuration."
        )

    dsn_env = section.get("dsn_env")
    if dsn_env is None:
        errors.append(
            "storage.postgres.dsn_env is required: it names the environment "
            "variable holding the connection string."
        )
    elif not isinstance(dsn_env, str) or not _ENV_NAME.match(dsn_env.strip()):
        # The value is deliberately not repeated — it is often the DSN itself.
        errors.append(
            "storage.postgres.dsn_env must be the NAME of an environment "
            "variable (letters, digits and underscores, not starting with a "
            "digit), not a connection string."
        )

    for key, (low, high) in _POSTGRES_RANGES.items():
        if key not in section:
            continue
        value = section[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"storage.postgres.{key} must be a number, got {value!r}")
            continue
        if not (low <= value <= high):
            errors.append(
                f"storage.postgres.{key} must be between {low:g} and {high:g}, "
                f"got {value!r}"
            )

    minimum = section.get("min_connections")
    maximum = section.get("max_connections")
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
        if minimum > maximum:
            errors.append(
                f"storage.postgres.min_connections ({minimum:g}) may not exceed "
                f"max_connections ({maximum:g})"
            )

    apply_migrations = section.get("apply_migrations")
    if apply_migrations is not None and not isinstance(apply_migrations, bool):
        errors.append(
            f"storage.postgres.apply_migrations must be true or false, "
            f"got {apply_migrations!r}"
        )


def validate(data: dict[str, Any]) -> None:
    """Structural validation of the merged configuration."""
    errors: list[str] = []

    if not isinstance(data.get("version"), int):
        errors.append("version must be an integer")

    for path, allowed in _ENUM_FIELDS.items():
        node: Any = data
        for part in path.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if node is None:
            continue
        if str(node).lower() not in allowed:
            errors.append(f"{path} must be one of {', '.join(allowed)}, got {node!r}")

    api = data.get("api")
    if isinstance(api, dict):
        proxy = api.get("proxy_identity")
        if isinstance(proxy, dict) and proxy.get("enabled"):
            errors.append(
                "api.proxy_identity.enabled is true, but proxy identity is not "
                "integrated: the API middleware does not consult it and no "
                "shipped deployment provides an authenticating proxy. Enabling "
                "it would look like authentication while doing nothing. Use "
                "api.principals with bearer tokens, which is the supported "
                "identity mode. See docs/security.md."
            )

    storage = data.get("storage")
    if isinstance(storage, dict) and str(storage.get("backend", "")).lower() == "postgres":
        _validate_postgres(storage.get("postgres"), errors)

    limits = data.get("limits", {})
    if not isinstance(limits, dict):
        errors.append("limits must be a mapping")
    else:
        # Zero is meaningful for the "how many times may this happen" limits:
        # it means "never". Only the budgets that gate any work at all must be
        # positive.
        must_be_positive = {"max_wall_seconds", "max_model_calls", "max_parallel_tasks"}
        for key, value in limits.items():
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                errors.append(f"limits.{key} must be a number, got {value!r}")
            elif value < 0:
                errors.append(f"limits.{key} must not be negative, got {value!r}")
            elif value == 0 and key in must_be_positive:
                errors.append(f"limits.{key} must be greater than zero")

    providers = data.get("models", {}).get("providers", [])
    if not isinstance(providers, list):
        errors.append("models.providers must be a list")
    else:
        for index, provider in enumerate(providers):
            if not isinstance(provider, dict):
                errors.append(f"models.providers[{index}] must be a mapping")
                continue
            if not provider.get("type"):
                errors.append(f"models.providers[{index}] requires a type")
            models = provider.get("models", [])
            if models and not isinstance(models, list):
                errors.append(f"models.providers[{index}].models must be a list")

    servers = data.get("mcp", {}).get("servers", {})
    if not isinstance(servers, dict):
        errors.append("mcp.servers must be a mapping of name to server config")
    else:
        for name, server in servers.items():
            if not isinstance(server, dict):
                errors.append(f"mcp.servers.{name} must be a mapping")
                continue
            if not server.get("command") and not server.get("url"):
                errors.append(
                    f"mcp.servers.{name} requires either a command (stdio) or a url (http)"
                )

    process = data.get("tools", {}).get("process", {})
    if isinstance(process, dict) and process.get("enabled"):
        if not process.get("allowed_commands"):
            errors.append(
                "tools.process.enabled requires a non-empty allowed_commands list"
            )

    rules = data.get("policy", {}).get("rules", [])
    if not isinstance(rules, list):
        errors.append("policy.rules must be a list")
    else:
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f"policy.rules[{index}] must be a mapping")
                continue
            effect = str(rule.get("effect", "allow")).lower()
            if effect not in ("allow", "deny", "require_approval"):
                errors.append(
                    f"policy.rules[{index}].effect must be allow, deny, or require_approval"
                )

    if errors:
        raise ConfigurationError(
            "configuration is invalid:\n- " + "\n- ".join(errors), errors=errors
        )


def write_default(directory: str | Path = ".") -> Path:
    """Write a starter project configuration. Used by ``orchestrator init``."""
    target = Path(directory) / PROJECT_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / "config.yaml"
    if path.exists():
        return path

    body = _DEFAULT_PROJECT_CONFIG
    try:
        import yaml  # noqa: F401  (presence check only)
    except ImportError:
        path = target / "config.json"
        body = json.dumps(
            {
                "version": 1,
                "workspace": ".",
                "limits": DEFAULTS["limits"],
                "models": {"providers": []},
                "tools": DEFAULTS["tools"],
                "mcp": {"servers": {}},
            },
            indent=2,
        )
    path.write_text(body, encoding="utf-8")
    return path


_DEFAULT_PROJECT_CONFIG = """# Universal AI Orchestration Platform - project configuration
#
# Every section is optional. Values here override the built-in defaults, and a
# global config at ~/.orchestrator/config.yaml is merged in underneath.
version: 1

workspace: "."

logging:
  level: warning
  json: true

storage:
  backend: sqlite
  path: .orchestrator/state.db

limits:
  max_wall_seconds: 3600
  max_model_calls: 300
  max_tool_calls: 1000
  max_parallel_tasks: 4
  max_task_attempts: 3

policy:
  # Anything the risk engine rates at or above this level needs a human.
  approval_threshold: high
  rules: []
    # - kind: tool
    #   subject: "process.*"
    #   effect: require_approval
    #   reason: subprocesses touch the host

models:
  providers: []
    # - type: ollama
    #   base_url: http://localhost:11434
    #   discover: true
    #
    # - type: openai_compatible
    #   name: openai
    #   base_url: https://api.openai.com/v1
    #   api_key_env: OPENAI_API_KEY
    #   models:
    #     - id: openai/gpt-4o-mini
    #       model: gpt-4o-mini
    #       capabilities: [text_generation, tool_calling, structured_output]
    #       context_window: 128000

tools:
  bookkeeping:
    enabled: true
  filesystem:
    enabled: false
    root: "."
    allow_write: true
  process:
    enabled: false
    allowed_commands: []
  http:
    enabled: false
    allowed_hosts: []

mcp:
  servers: {}
    # filesystem:
    #   command: npx
    #   args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
  policies: []
    # - server: filesystem
    #   max_risk: medium
    #   deny_tools: ["*delete*"]

agents:
  directories: []
  definitions: []

capabilities: []

workflows:
  directories: []
"""
