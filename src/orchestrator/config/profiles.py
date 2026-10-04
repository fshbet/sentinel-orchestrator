"""Deployment profiles.

A single set of defaults cannot serve both a developer on a laptop and a
service handling other people's data. Trying to produces the worst outcome of
the two: defaults loose enough to be convenient, shipped to production because
nobody realised they were the development ones.

So the posture is named. A config declares which world it is in:

.. code-block:: yaml

    profile: production

and the defaults follow from that declaration. Three profiles exist, and the
gap between them is deliberate rather than a sliding scale:

``development``
    A laptop. Permissive defaults, plain HTTP allowed, loopback reachable.
    Fast to iterate in. Not for anyone else's data.

``internal-pilot``
    Real users, bounded blast radius, an audience who can be told when it
    breaks. Deny-by-default policy and explicit tool grants, but private
    network access is permitted because that is usually the point.

``production``
    Everything denied unless granted. No private networks, no plain HTTP, no
    process execution, API authentication required.

Two rules keep this honest:

* **``production`` is the default when nothing is declared.** An operator who
  has not thought about it gets the strict posture, not the loose one. Being
  told to turn something on is a good afternoon; discovering something was
  never off is a bad quarter.
* **A profile sets defaults, never a ceiling.** Explicit config always wins,
  in either direction. The profile answers "what if this is unspecified", and
  ``explain()`` reports which values came from where so the effective posture
  is inspectable rather than inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ConfigurationError

DEVELOPMENT = "development"
INTERNAL_PILOT = "internal-pilot"
PRODUCTION = "production"

PROFILES = (DEVELOPMENT, INTERNAL_PILOT, PRODUCTION)

# Unspecified means production. See the module docstring.
DEFAULT_PROFILE = PRODUCTION


@dataclass(frozen=True)
class Profile:
    """The defaults a named posture implies."""

    name: str
    summary: str
    # policy
    default_effect: str = "deny"
    require_explicit_tool_grant: bool = True
    approval_threshold: str = "high"
    # api
    require_api_authentication: bool = True
    expose_readiness_detail: bool = False
    # egress
    allow_http: bool = False
    allow_private_networks: bool = False
    allow_loopback: bool = False
    # tools
    allow_process_tools: bool = False
    # data
    default_data_classification: str = "internal"
    allow_external_model_providers: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "policy": {
                "default_effect": self.default_effect,
                "require_explicit_tool_grant": self.require_explicit_tool_grant,
                "approval_threshold": self.approval_threshold,
            },
            "api": {
                "require_authentication": self.require_api_authentication,
                "expose_readiness_detail": self.expose_readiness_detail,
            },
            "egress": {
                "allow_http": self.allow_http,
                "allow_private_networks": self.allow_private_networks,
                "allow_loopback": self.allow_loopback,
            },
            "tools": {"allow_process_tools": self.allow_process_tools},
            "data": {
                "default_classification": self.default_data_classification,
                "allow_external_model_providers": self.allow_external_model_providers,
            },
        }


_PROFILES: dict[str, Profile] = {
    DEVELOPMENT: Profile(
        name=DEVELOPMENT,
        summary=(
            "A single developer on one machine. Permissive, convenient, and "
            "not suitable for anyone else's data."
        ),
        default_effect="allow",
        require_explicit_tool_grant=False,
        approval_threshold="high",
        require_api_authentication=False,
        expose_readiness_detail=True,
        allow_http=True,
        allow_private_networks=True,
        allow_loopback=True,
        allow_process_tools=True,
        default_data_classification="public",
    ),
    INTERNAL_PILOT: Profile(
        name=INTERNAL_PILOT,
        summary=(
            "Real users inside one organisation, with a bounded blast radius "
            "and an audience who can be told when something breaks."
        ),
        default_effect="deny",
        require_explicit_tool_grant=True,
        approval_threshold="high",
        require_api_authentication=True,
        expose_readiness_detail=False,
        allow_http=False,
        # Reaching internal services is usually the whole point of a pilot.
        allow_private_networks=True,
        allow_loopback=False,
        allow_process_tools=False,
        default_data_classification="internal",
    ),
    PRODUCTION: Profile(
        name=PRODUCTION,
        summary=(
            "Deny by default. Every tool, permission, host, and method is "
            "granted explicitly or not at all."
        ),
        default_effect="deny",
        require_explicit_tool_grant=True,
        approval_threshold="medium",
        require_api_authentication=True,
        expose_readiness_detail=False,
        allow_http=False,
        allow_private_networks=False,
        allow_loopback=False,
        allow_process_tools=False,
        default_data_classification="confidential",
    ),
}


def get(name: str | None) -> Profile:
    """Look up a profile, or fail with the list of real ones."""
    resolved = (name or DEFAULT_PROFILE).strip().lower()
    # A common near-miss worth accepting rather than lecturing about.
    resolved = {
        "internal_pilot": INTERNAL_PILOT,
        "prod": PRODUCTION,
        "dev": DEVELOPMENT,
    }.get(resolved, resolved)
    if resolved not in _PROFILES:
        raise ConfigurationError(
            f"unknown profile {name!r}. Valid profiles are: {', '.join(PROFILES)}",
            profile=name,
            valid=list(PROFILES),
        )
    return _PROFILES[resolved]


def all_profiles() -> dict[str, Profile]:
    return dict(_PROFILES)


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

# Settings whose old default was permissive and whose new default is not.
# Listed explicitly so the migration message can name what changed for *this*
# config rather than describing the release in general.
_TIGHTENED: tuple[tuple[tuple[str, ...], Any, Any, str], ...] = (
    (
        ("policy", "default_effect"),
        "allow",
        "deny",
        "tools are now refused unless a policy rule permits them",
    ),
    (
        ("policy", "require_explicit_tool_grant"),
        False,
        True,
        "a task must now be granted each tool it uses",
    ),
    (
        ("tools", "http", "allowed_hosts"),
        [],
        None,
        "an empty HTTP allowlist used to mean 'any host' and now means 'none'",
    ),
)


@dataclass
class MigrationNotice:
    """One setting whose default changed under this config's profile."""

    path: str
    old_default: Any
    new_default: Any
    consequence: str
    currently_explicit: bool

    def describe(self) -> str:
        if self.currently_explicit:
            return f"{self.path}: set explicitly, unchanged"
        return (
            f"{self.path}: default changed from {self.old_default!r} to "
            f"{self.new_default!r} — {self.consequence}"
        )


def migration_notices(raw: dict[str, Any], profile: Profile) -> list[MigrationNotice]:
    """What behaves differently for this config under the new defaults.

    A config that relied on an old permissive default is not broken, but it is
    about to behave differently, and silently changing what it does is the one
    outcome to avoid. ``orchestrator validate`` prints these.
    """
    if profile.name == DEVELOPMENT:
        return []

    notices: list[MigrationNotice] = []
    for path, old, new, consequence in _TIGHTENED:
        cursor: Any = raw
        explicit = True
        for key in path:
            if not isinstance(cursor, dict) or key not in cursor:
                explicit = False
                break
            cursor = cursor[key]
        # An empty allowlist is present-but-meaningless: treat it as unset,
        # because that is the case whose meaning inverted.
        if explicit and path[-1] == "allowed_hosts" and not cursor:
            explicit = False
        notices.append(
            MigrationNotice(
                path=".".join(path),
                old_default=old,
                new_default=new if new is not None else "no hosts permitted",
                consequence=consequence,
                currently_explicit=explicit,
            )
        )
    return [n for n in notices if not n.currently_explicit]


def apply_defaults(raw: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """Fill unset values from the profile. Explicit config always wins.

    Returns a new mapping; the input is not modified, so the difference
    between what was written and what took effect stays inspectable.
    """
    import copy

    merged = copy.deepcopy(raw)

    def setdefault(section: str, key: str, value: Any) -> None:
        merged.setdefault(section, {})
        if isinstance(merged[section], dict):
            merged[section].setdefault(key, value)

    setdefault("policy", "default_effect", profile.default_effect)
    setdefault("policy", "require_explicit_tool_grant", profile.require_explicit_tool_grant)
    setdefault("policy", "approval_threshold", profile.approval_threshold)

    tools = merged.setdefault("tools", {})
    if isinstance(tools, dict):
        http = tools.setdefault("http", {})
        if isinstance(http, dict):
            http.setdefault("allow_http", profile.allow_http)
            http.setdefault("allow_private_networks", profile.allow_private_networks)
            http.setdefault("allow_loopback", profile.allow_loopback)

    return merged


def explain(raw: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """The effective posture and where each value came from."""
    merged = apply_defaults(raw, profile)
    policy = merged.get("policy", {}) or {}
    tools = merged.get("tools", {}) or {}
    http = (tools.get("http") or {}) if isinstance(tools, dict) else {}

    def source(section: str, key: str) -> str:
        block = raw.get(section)
        if isinstance(block, dict) and key in block:
            return "config"
        return f"profile:{profile.name}"

    return {
        "profile": profile.name,
        "summary": profile.summary,
        "policy": {
            "default_effect": policy.get("default_effect"),
            "default_effect_from": source("policy", "default_effect"),
            "require_explicit_tool_grant": policy.get("require_explicit_tool_grant"),
        },
        "http_tools": {
            "enabled": bool(http.get("enabled")),
            "allowed_hosts": list(http.get("allowed_hosts") or []),
            "allowed_methods": list(
                http.get("allowed_methods") or ["GET", "HEAD", "OPTIONS"]
            ),
            "https_only": not http.get("allow_http", profile.allow_http),
            "private_networks": http.get(
                "allow_private_networks", profile.allow_private_networks
            ),
        },
        "process_tools_enabled": bool(
            (tools.get("process") or {}).get("enabled")
            if isinstance(tools, dict)
            else False
        ),
        "api_authentication_required": profile.require_api_authentication,
    }
