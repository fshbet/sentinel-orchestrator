"""What the console may read, and what it may change.

A web form that edits credentials and security posture is a liability if it is
reachable by anyone who can reach the port, so this module is built around one
rule:

    **Settings are writable only while the deployment is in the
    ``development`` profile.**

That gives a direction of travel rather than a switch. Someone setting a
machine up locally can paste in provider keys and then promote the deployment
to ``internal-pilot`` or ``production`` from the console. Once promoted, the
same console can still *show* the settings but can no longer change them: a
production deployment is configured by whoever deploys it, and an HTTP request
must not be able to hand itself a looser posture or a different key. Going back
means editing ``.orchestrator/config.yaml`` on the box, which requires access
this API deliberately does not grant.

Keys are written to ``.orchestrator/secrets.env`` rather than into
``config.yaml``. Three reasons: the configuration file is the thing people
paste into issues and commit by accident; ``.orchestrator/`` is already
excluded from version control wholesale; and keeping secrets in one file means
one place to lock down and one place to delete. The file is created 0600 where
the platform honours it.

Values are never returned. A caller learns that a key is *set*, its length, and
its last four characters — enough to tell two keys apart when you are checking
which one is loaded, not enough to use.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..config import profiles as _profiles
from ..errors import ConfigurationError, PermissionDenied

SECRETS_FILENAME = "secrets.env"

# Only variables shaped like a provider credential may be written. Without this
# the endpoint is a general-purpose "set any environment variable in the server
# process" primitive, which is a much larger thing to expose than it looks:
# PATH, PYTHONPATH, and LD_PRELOAD are all environment variables.
_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_KEY_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_API_TOKEN", "_SECRET")

# Never settable through the API, whatever they are named. These decide who may
# call this API at all; letting a caller rewrite them is privilege escalation.
_FORBIDDEN_KEYS = frozenset(
    {
        "ORCHESTRATOR_API_TOKEN",
        "ORCHESTRATOR_POSTGRES_DSN",
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    }
)


def state_dir(config) -> Path:
    """The directory holding this deployment's local state."""
    storage = str(config.get("storage.path", ".orchestrator/state.db"))
    return Path(storage).expanduser().resolve().parent


def secrets_file(config) -> Path:
    """Where provider keys are kept, beside the rest of the local state."""
    return state_dir(config) / SECRETS_FILENAME


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


# A configuration that puts the key itself where the *name* of the variable
# belongs is the single most common way this is set up wrong, and it fails
# silently: os.environ.get("sk-or-v1-...") is empty, so the provider is built
# with no credential and every call comes back 401. Worse, echoing that field
# back to a caller would disclose the key. Detected, reported, never returned.
_LOOKS_LIKE_A_SECRET = re.compile(
    r"^(sk-|nvapi-|ghp_|gho_|github_pat_|xox[baprs]-|AIza|ASIA|AKIA)"
    r"|^[A-Za-z0-9_\-]{40,}$"
)


def looks_like_a_secret(value: str) -> bool:
    """Is this a credential sitting where a variable name should be?"""
    value = (value or "").strip()
    return bool(value) and bool(_LOOKS_LIKE_A_SECRET.match(value))


def describe_key(name: str) -> dict[str, Any]:
    """Report a credential's presence without disclosing it."""
    if looks_like_a_secret(name):
        # Return nothing derived from the value - not a prefix, not a length.
        return {
            "env": None,
            "set": False,
            "problem": (
                "The configuration has a credential where the *name* of an "
                "environment variable should be, so nothing is ever read and "
                "every call to this provider fails authentication. Set "
                "api_key_env to a name such as OPENROUTER_API_KEY, then save "
                "the key here."
            ),
        }
    raw = os.environ.get(name, "")
    value = raw.strip()
    if not value:
        return {"env": name, "set": False}
    return {
        "env": name,
        "set": True,
        "length": len(value),
        # Enough to distinguish two keys at a glance; useless on its own.
        "hint": f"...{value[-4:]}" if len(value) > 8 else "...",
    }


def writable(config) -> tuple[bool, str]:
    """May settings be changed through the API right now?

    Returns the answer and the sentence to show the operator, because "no" is
    only useful alongside why and what to do instead.
    """
    profile = getattr(config.profile, "name", None) or _profiles.DEFAULT_PROFILE
    if profile == _profiles.DEVELOPMENT:
        return True, "Editable: this deployment is in the development profile."
    return False, (
        f"Read-only: this deployment is in the {profile!r} profile. Settings "
        "are editable only in 'development', so that a deployment serving "
        "other people cannot be given new credentials or a looser posture "
        "over HTTP. Edit .orchestrator/config.yaml on the host and restart."
    )


def describe(config, orchestrator=None) -> dict[str, Any]:
    """Everything the settings screen needs, and nothing secret.

    Deliberately answers "where does my work end up?" as well as "what is
    configured?" - those are the same question to anyone running this, and the
    answer was previously only discoverable by reading the source.
    """
    editable, reason = writable(config)
    profile_name = getattr(config.profile, "name", None) or _profiles.DEFAULT_PROFILE

    providers: list[dict[str, Any]] = []
    for entry in config.get("models.providers", []) or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type", "")).strip()
        env_name = str(entry.get("api_key_env", "") or "").strip()
        record: dict[str, Any] = {
            "name": str(entry.get("name", "") or kind or "provider"),
            "type": kind,
            "base_url": str(entry.get("base_url", "") or ""),
            "models": [
                str(m.get("id", m)) if isinstance(m, dict) else str(m)
                for m in (entry.get("models") or [])
            ],
        }
        # A local provider has no key to set; saying so is clearer than showing
        # an empty field that looks broken.
        record["needs_key"] = bool(env_name)
        record["key"] = describe_key(env_name) if env_name else None
        providers.append(record)

    storage_path = str(config.get("storage.path", ".orchestrator/state.db"))
    workspace = str(config.get("workspace", "."))

    return {
        "profile": {
            "current": profile_name,
            "available": list(_profiles.PROFILES),
            "editable": editable,
            "reason": reason,
        },
        "providers": providers,
        "storage": {
            "backend": str(config.get("storage.backend", "sqlite")),
            # Absolute, because "where is it?" is the whole question and a
            # relative path just moves it.
            "path": str(Path(storage_path).expanduser().resolve()),
            "secrets_file": str(secrets_file(config)),
        },
        "workspace": {
            "path": str(Path(workspace).expanduser().resolve()),
            "note": (
                "Files a run creates are written here. Results, plans, and the "
                "audit trail are recorded in the state database above, not as "
                "loose files."
            ),
        },
        "config_sources": config.sources(),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _require_writable(config) -> None:
    ok, reason = writable(config)
    if not ok:
        raise PermissionDenied(reason)


def _validate_key_name(name: str) -> str:
    name = (name or "").strip().upper()
    if not _KEY_NAME.match(name):
        raise ConfigurationError(
            "a credential's variable name must be uppercase letters, digits, "
            "and underscores.",
            remedy="use a name like OPENROUTER_API_KEY",
        )
    if name in _FORBIDDEN_KEYS:
        raise PermissionDenied(
            f"{name} cannot be set through the API: it controls access to this "
            "API or how this process finds code to run. Set it in the "
            "environment before starting the server."
        )
    if not name.endswith(_KEY_SUFFIXES):
        raise ConfigurationError(
            f"{name} does not look like a provider credential, so it is "
            "refused rather than written into the environment of a running "
            "process.",
            remedy=f"names must end with one of {', '.join(_KEY_SUFFIXES)}",
        )
    return name


def _read_secrets(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        found[key.strip()] = value.strip()
    return found


def _write_secrets(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [
        "# Provider credentials, written by the console.",
        "# This file is loaded into the environment when the server starts.",
        "# It is inside .orchestrator/, which is excluded from version control.",
        "# Delete a line to remove a credential.",
        "",
    ]
    body.extend(f"{k}={v}" for k, v in sorted(values.items()))

    # Created 0600, rather than created and then narrowed. A plain write
    # followed by a chmod leaves the file at the umask default for as long as
    # the two calls take, and a reader only needs that window once.
    def _private(file: str, flags: int) -> int:
        return os.open(file, flags, 0o600)

    with open(path, "w", encoding="utf-8", opener=_private) as handle:
        handle.write("\n".join(body) + "\n")
    try:
        # The mode above applies on creation only, so this is what tightens a
        # file that already existed with looser permissions. Windows does not
        # honour POSIX modes here; the file still lands inside .orchestrator/,
        # which is not published, so say nothing rather than fail the write
        # over a permission bit that was never going to apply.
        path.chmod(0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pass


def load_secrets(config) -> list[str]:
    """Load stored credentials into the environment. Returns names loaded.

    The real environment wins. A key exported by whoever started the process is
    a deliberate act by someone with shell access; one in this file was typed
    into a web form. When they disagree, the stronger claim should not be
    silently replaced by the weaker one.
    """
    loaded: list[str] = []
    for name, value in _read_secrets(secrets_file(config)).items():
        if not value or os.environ.get(name, "").strip():
            continue
        os.environ[name] = value
        loaded.append(name)
    return loaded


def set_provider_key(config, env_name: str, value: str) -> dict[str, Any]:
    """Store a provider credential and make it live for this process."""
    _require_writable(config)
    name = _validate_key_name(env_name)
    value = (value or "").strip()

    path = secrets_file(config)
    values = _read_secrets(path)

    if not value:
        values.pop(name, None)
        os.environ.pop(name, None)
        _write_secrets(path, values)
        return {"env": name, "set": False, "removed": True}

    if len(value) < 8:
        raise ConfigurationError(
            "that is too short to be a provider key; nothing was saved.",
            remedy="paste the whole key",
        )

    values[name] = value
    _write_secrets(path, values)
    os.environ[name] = value
    return describe_key(name)


_PROFILE_LINE = re.compile(r"^\s*profile\s*:.*$", re.MULTILINE)


def set_profile(config, profile: str, *, config_path: str | None = None) -> str:
    """Record a new profile in the configuration file.

    Writes the file and reports that a restart is needed rather than mutating
    the running process. A profile decides tool permissions, egress rules, and
    whether authentication is required; swapping those under live requests
    would apply a half-old, half-new posture to whatever is in flight.
    """
    _require_writable(config)

    profile = (profile or "").strip().lower()
    if profile not in _profiles.PROFILES:
        raise ConfigurationError(
            f"unknown profile {profile!r}.",
            remedy=f"one of: {', '.join(_profiles.PROFILES)}",
        )

    target = Path(config_path) if config_path else None
    if target is None:
        for source in config.sources():
            candidate = Path(source)
            if candidate.suffix in {".yaml", ".yml"} and candidate.is_file():
                target = candidate
                break
    if target is None or not target.is_file():
        raise ConfigurationError(
            "no configuration file to write to.",
            remedy="run `orchestrator init .` first",
        )

    text = target.read_text(encoding="utf-8")
    line = f"profile: {profile}"
    if _PROFILE_LINE.search(text):
        text = _PROFILE_LINE.sub(line, text, count=1)
    else:
        text = f"{line}\n{text}"
    target.write_text(text, encoding="utf-8")
    return str(target)
