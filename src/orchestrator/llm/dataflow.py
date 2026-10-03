"""Data classification and model egress policy.

Sending a prompt to a hosted model is an export of data to a third party. That
is true whether or not anyone framed it that way when the provider was
configured, and it is the reason this module exists: the decision about which
data may leave should be a declared policy, checked before the request, rather
than an emergent property of which model happened to sort first.

Four classifications, ordered:

``public``        already published, or intended to be
``internal``      ordinary business data
``confidential``  customer data, commercial terms, anything under NDA
``restricted``    regulated data — health, payment, credentials, personal data
                  under a regime with its own rules

And three provider dispositions, declared per provider:

``local``         runs on infrastructure you control; no third-party egress
``approved``      an external provider cleared for a stated maximum
                  classification through your own review
``prohibited``    never receives data, whatever the classification

Three decisions in the design are worth stating, because the obvious
alternative is wrong in each case:

* **A provider with no declared disposition is treated as unapproved.** Not as
  approved-for-public. An operator who has not classified a provider has not
  reviewed it, and "we never decided" must not read as "cleared for public
  data".
* **The classification travels with the execution, not the task.** A task that
  reads a confidential file does not become confidential retroactively; the
  whole execution was already handling that data, and the summary of it is
  every bit as sensitive as the source.
* **What is recorded is the decision, never the payload.** The audit trail
  says which provider received data of what classification and under which
  approval. Logging the data would recreate the exposure this module exists to
  prevent, inside the log aggregator.

This is a technical control that enforces a decision. It is not the decision.
Approving an external provider for confidential data is a legal, privacy, and
vendor-review question; this module records and enforces the answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ConfigurationError, PolicyViolation

PUBLIC = "public"
INTERNAL = "internal"
CONFIDENTIAL = "confidential"
RESTRICTED = "restricted"

# Ordered least to most sensitive. The index is the comparison.
CLASSIFICATIONS = (PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED)

LOCAL = "local"
APPROVED = "approved"
PROHIBITED = "prohibited"
UNAPPROVED = "unapproved"

DISPOSITIONS = (LOCAL, APPROVED, PROHIBITED, UNAPPROVED)


def rank(classification: str) -> int:
    """How sensitive, as a number. Unknown values rank highest."""
    try:
        return CLASSIFICATIONS.index(classification)
    except ValueError:
        # An unrecognised label is treated as the most sensitive thing it
        # could be. Guessing downward would be guessing in the direction that
        # loses data.
        return len(CLASSIFICATIONS)


def normalise(classification: Any, *, default: str = INTERNAL) -> str:
    value = str(classification or "").strip().lower()
    return value if value in CLASSIFICATIONS else default


@dataclass(frozen=True)
class ProviderPolicy:
    """What one provider is cleared to receive."""

    provider: str
    disposition: str = UNAPPROVED
    # The most sensitive classification this provider may receive. Ignored for
    # `prohibited`; for `local` it defaults to everything.
    max_classification: str = PUBLIC
    # Free text: the ticket, the DPA, the review that cleared this.
    approval_reference: str = ""

    def accepts(self, classification: str) -> tuple[bool, str]:
        """Whether this provider may receive data at ``classification``."""
        if self.disposition == PROHIBITED:
            return False, f"provider {self.provider} is prohibited from receiving data"
        if self.disposition == UNAPPROVED:
            return False, (
                f"provider {self.provider} has no declared data policy. Declare "
                f"models.providers[].data_policy with a disposition of local, "
                f"approved, or prohibited. An undeclared provider is treated as "
                f"unapproved rather than as cleared for public data."
            )
        if self.disposition == LOCAL:
            return True, f"{self.provider} runs on infrastructure you control"
        if rank(classification) > rank(self.max_classification):
            return False, (
                f"provider {self.provider} is approved for {self.max_classification} "
                f"data at most, and this execution is classified {classification}"
            )
        return True, (
            f"{self.provider} is approved for {self.max_classification} data"
            + (f" ({self.approval_reference})" if self.approval_reference else "")
        )


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    provider: str
    classification: str
    disposition: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "provider": self.provider,
            "classification": self.classification,
            "disposition": self.disposition,
        }


class DataFlowPolicy:
    """Decides whether an execution's data may reach a given provider."""

    def __init__(
        self,
        providers: dict[str, ProviderPolicy] | None = None,
        *,
        default_classification: str = INTERNAL,
        enabled: bool = True,
    ) -> None:
        self._providers = dict(providers or {})
        self.default_classification = normalise(default_classification)
        self.enabled = enabled

    def policy_for(self, provider: str) -> ProviderPolicy:
        return self._providers.get(
            provider, ProviderPolicy(provider=provider, disposition=UNAPPROVED)
        )

    def evaluate(self, provider: str, classification: str | None = None) -> Decision:
        resolved = normalise(classification, default=self.default_classification)
        policy = self.policy_for(provider)

        if not self.enabled:
            return Decision(
                allowed=True,
                reason="data flow policy is disabled for this deployment",
                provider=provider,
                classification=resolved,
                disposition=policy.disposition,
            )

        allowed, reason = policy.accepts(resolved)
        return Decision(
            allowed=allowed,
            reason=reason,
            provider=provider,
            classification=resolved,
            disposition=policy.disposition,
        )

    def enforce(self, provider: str, classification: str | None = None) -> Decision:
        decision = self.evaluate(provider, classification)
        if not decision.allowed:
            raise PolicyViolation(decision.reason, **decision.to_dict())
        return decision

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "default_classification": self.default_classification,
            "providers": {
                name: {
                    "disposition": policy.disposition,
                    "max_classification": policy.max_classification,
                    "approval_reference": policy.approval_reference,
                }
                for name, policy in self._providers.items()
            },
        }

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, config: Any) -> DataFlowPolicy:
        """Build from ``data`` plus each provider's ``data_policy`` block."""
        section = config.section("data") if hasattr(config, "section") else (config or {})
        providers: dict[str, ProviderPolicy] = {}

        raw_providers = (
            config.get("models.providers", []) if hasattr(config, "get") else []
        ) or []
        for entry in raw_providers:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("type") or "").strip()
            if not name:
                continue
            block = entry.get("data_policy") or {}

            if block:
                disposition = str(block.get("disposition", UNAPPROVED)).lower()
            elif str(entry.get("type", "")).lower() == "ollama":
                # Ollama is a local runtime by definition. Inferring this is
                # the one case where the platform knows more than the config.
                disposition = LOCAL
            else:
                disposition = UNAPPROVED

            if disposition not in DISPOSITIONS:
                raise ConfigurationError(
                    f"provider {name}: data_policy.disposition must be one of "
                    f"{', '.join(DISPOSITIONS)}, not {disposition!r}"
                )

            max_classification = normalise(
                block.get("max_classification"),
                default=RESTRICTED if disposition == LOCAL else PUBLIC,
            )
            providers[name] = ProviderPolicy(
                provider=name,
                disposition=disposition,
                max_classification=max_classification,
                approval_reference=str(block.get("approval_reference", "")),
            )

        # When to enforce.
        #
        # The risk this guards is data reaching a third-party service nobody
        # reviewed, and that risk arrives through *configuration*: a provider
        # block naming a hosted endpoint. A provider handed to
        # ``Orchestrator.create(providers=[...])`` is the caller's own object
        # in the caller's own process — it is not a model choosing a
        # destination, and refusing it would break every embedding use and
        # every test without protecting anything.
        #
        # So enforcement needs both: a profile that asks for it, and at least
        # one provider actually declared in config. An explicit
        # ``data.enforce_egress_policy`` overrides in either direction.
        profile = getattr(config, "profile", None)
        profile_wants = bool(getattr(profile, "name", "development") != "development")
        declared = bool(providers)

        explicit = section.get("enforce_egress_policy")
        enabled = bool(explicit) if explicit is not None else (profile_wants and declared)

        return cls(
            providers,
            default_classification=section.get(
                "default_classification",
                getattr(profile, "default_data_classification", INTERNAL),
            ),
            enabled=enabled,
        )
