"""Policy engine.

The separation the whole security model rests on: the model may *request* an
action, the policy engine decides whether it is *allowed*, the tool layer
executes it, and a validator judges the result (spec section 62).

Rules are data. They are matched most-specific-first, and an action with no
matching rule falls back to the configured default, which is deny for anything
the risk engine rates HIGH or above.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ...errors import PermissionDenied
from ..domain.enums import RiskLevel
from .risk import OperationDescriptor, RiskAssessment, RiskEngine

# Decision outcomes.
ALLOW = "allow"
DENY = "deny"
REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyRule:
    """One rule. ``subject`` is a glob matched against the resource id."""

    subject: str = "*"
    # tool | mcp_server | mcp_tool | model | agent | adapter | filesystem | network
    kind: str = "tool"
    effect: str = ALLOW
    max_risk: RiskLevel | None = None
    required_permissions: tuple[str, ...] = ()
    reason: str = ""
    priority: int = 0

    def matches(self, kind: str, subject: str) -> bool:
        return self.kind == kind and fnmatch.fnmatch(subject, self.subject)

    @property
    def specificity(self) -> int:
        # Longer, less wildcarded patterns win.
        return (
            self.priority * 1000
            + len(self.subject)
            - self.subject.count("*") * 10
        )


@dataclass
class PolicyDecision:
    allowed: bool
    effect: str
    reason: str
    risk: RiskAssessment
    rule: PolicyRule | None = None
    requires_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "effect": self.effect,
            "reason": self.reason,
            "risk": self.risk.to_dict(),
            "requires_approval": self.requires_approval,
            "rule": None
            if self.rule is None
            else {"kind": self.rule.kind, "subject": self.rule.subject},
        }

    def raise_if_denied(self, *, subject: str = "") -> None:
        if not self.allowed and not self.requires_approval:
            raise PermissionDenied(
                self.reason or f"policy denied {subject}",
                subject=subject,
                risk=self.risk.level.value,
            )


@dataclass
class PolicyConfig:
    """Defaults applied when no rule matches."""

    default_effect: str = ALLOW
    # Anything at or above this level needs a human unless a rule says otherwise.
    approval_threshold: RiskLevel = RiskLevel.HIGH
    # Anything above this is refused outright.
    deny_threshold: RiskLevel | None = None
    # When True, a tool must be explicitly granted before it can run.
    require_explicit_tool_grant: bool = False
    version: str = "1.0.0"


class PolicyEngine:
    def __init__(
        self,
        rules: Iterable[PolicyRule] = (),
        *,
        config: PolicyConfig | None = None,
        risk_engine: RiskEngine | None = None,
    ) -> None:
        self.config = config or PolicyConfig()
        self.risk = risk_engine or RiskEngine()
        self._rules: list[PolicyRule] = sorted(
            rules, key=lambda r: -r.specificity
        )

    # -- rule management ---------------------------------------------------

    def add_rule(self, rule: PolicyRule) -> None:
        self._rules.append(rule)
        self._rules.sort(key=lambda r: -r.specificity)

    def rules(self) -> list[PolicyRule]:
        return list(self._rules)

    def find_rule(self, kind: str, subject: str) -> PolicyRule | None:
        for rule in self._rules:
            if rule.matches(kind, subject):
                return rule
        return None

    # -- evaluation --------------------------------------------------------

    def evaluate(
        self,
        operation: OperationDescriptor,
        *,
        kind: str | None = None,
        subject: str | None = None,
        granted_permissions: Sequence[str] = (),
    ) -> PolicyDecision:
        resolved_kind = kind or operation.kind
        resolved_subject = subject or operation.name
        assessment = self.risk.assess(operation)
        rule = self.find_rule(resolved_kind, resolved_subject)

        # 1. An explicit deny rule always wins.
        if rule is not None and rule.effect == DENY:
            return PolicyDecision(
                allowed=False,
                effect=DENY,
                reason=rule.reason or f"policy denies {resolved_subject}",
                risk=assessment,
                rule=rule,
            )

        # 2. Missing permissions are a denial, not an approval prompt.
        missing = self._missing_permissions(
            operation, rule, granted_permissions
        )
        if missing:
            return PolicyDecision(
                allowed=False,
                effect=DENY,
                reason=(
                    f"{resolved_subject} requires permissions not granted to this"
                    f" scope: {', '.join(missing)}"
                ),
                risk=assessment,
                rule=rule,
            )

        # 3. Hard risk ceiling.
        ceiling = (
            rule.max_risk
            if rule is not None and rule.max_risk is not None
            else self.config.deny_threshold
        )
        if ceiling is not None and assessment.level.rank > ceiling.rank:
            return PolicyDecision(
                allowed=False,
                effect=DENY,
                reason=(
                    f"{resolved_subject} is rated {assessment.level.value}, above the"
                    f" permitted ceiling {ceiling.value}"
                ),
                risk=assessment,
                rule=rule,
            )

        # 4. Explicit approval requirement, or risk over the approval threshold.
        if rule is not None and rule.effect == REQUIRE_APPROVAL:
            return PolicyDecision(
                allowed=False,
                effect=REQUIRE_APPROVAL,
                reason=rule.reason or f"{resolved_subject} requires human approval",
                risk=assessment,
                rule=rule,
                requires_approval=True,
            )
        if assessment.level.rank >= self.config.approval_threshold.rank:
            return PolicyDecision(
                allowed=False,
                effect=REQUIRE_APPROVAL,
                reason=(
                    f"{resolved_subject} is rated {assessment.level.value}"
                    f" ({', '.join(assessment.factors) or 'no factors'})"
                ),
                risk=assessment,
                rule=rule,
                requires_approval=True,
            )

        # 5. Explicit allow, or the default.
        if rule is not None and rule.effect == ALLOW:
            return PolicyDecision(
                allowed=True,
                effect=ALLOW,
                reason=rule.reason or "allowed by policy",
                risk=assessment,
                rule=rule,
            )
        if (
            self.config.require_explicit_tool_grant
            and resolved_kind in ("tool", "mcp_tool")
            and rule is None
        ):
            return PolicyDecision(
                allowed=False,
                effect=DENY,
                reason=(
                    f"{resolved_subject} is not explicitly granted and the policy"
                    " requires explicit grants"
                ),
                risk=assessment,
            )
        allowed = self.config.default_effect == ALLOW
        return PolicyDecision(
            allowed=allowed,
            effect=self.config.default_effect,
            reason="policy default",
            risk=assessment,
        )

    def _missing_permissions(
        self,
        operation: OperationDescriptor,
        rule: PolicyRule | None,
        granted: Sequence[str],
    ) -> list[str]:
        required = set(operation.permissions)
        if rule is not None:
            required |= set(rule.required_permissions)
        if not required:
            return []
        granted_set = set(granted)
        missing = []
        for permission in sorted(required):
            if permission in granted_set:
                continue
            # Support wildcard grants such as "fs.*".
            if any(fnmatch.fnmatch(permission, g) for g in granted_set):
                continue
            missing.append(permission)
        return missing


def default_policy() -> PolicyEngine:
    """A conservative starting policy that ships with the platform.

    It allows ordinary read-shaped work, sends anything HIGH or above to a
    human, and refuses nothing outright. Deployments are expected to tighten
    this through configuration.
    """
    return PolicyEngine(
        rules=[
            PolicyRule(
                kind="tool",
                subject="*",
                effect=ALLOW,
                reason="tools are permitted subject to risk assessment",
                priority=0,
            ),
        ],
        config=PolicyConfig(
            default_effect=ALLOW,
            approval_threshold=RiskLevel.HIGH,
            deny_threshold=None,
        ),
    )


@dataclass
class PermissionScope:
    """The privileges handed to one agent for one task (least privilege)."""

    permissions: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def allows_tool(self, tool_id: str) -> bool:
        if not self.tools:
            return False
        return any(fnmatch.fnmatch(tool_id, pattern) for pattern in self.tools)

    def allows_server(self, server_id: str) -> bool:
        if not self.mcp_servers:
            return False
        return any(fnmatch.fnmatch(server_id, pattern) for pattern in self.mcp_servers)

    def narrowed_to(self, tools: Iterable[str]) -> "PermissionScope":
        allowed = tuple(t for t in tools if self.allows_tool(t))
        return PermissionScope(
            permissions=self.permissions,
            tools=allowed,
            mcp_servers=self.mcp_servers,
            resources=self.resources,
            extra=dict(self.extra),
        )
