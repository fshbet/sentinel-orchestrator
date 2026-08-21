"""Risk classification.

Risk is scored along independent axes, not reduced to a single "is this
production?" flag (spec section 39). The score decides whether the policy engine
requires a human before an action proceeds.

Nothing here knows about any particular domain: an operation is described by
its declared properties, which come from tool metadata, MCP tool annotations,
or task declarations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import RiskLevel


@dataclass
class OperationDescriptor:
    """What is about to happen, described in domain-neutral terms."""

    name: str = ""
    kind: str = "tool"  # tool | mcp | model | agent | adapter | workflow
    reversible: bool = True
    destructive: bool = False
    external_effect: bool = False
    writes_data: bool = False
    reads_sensitive_data: bool = False
    financial_effect: bool = False
    security_effect: bool = False
    affects_shared_resource: bool = False
    permissions: list[str] = field(default_factory=list)
    declared_risk: RiskLevel | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Axis -> contribution to the raw score. Tuned so that any single severe axis
# reaches HIGH on its own, and two moderate axes combine into HIGH.
_WEIGHTS: dict[str, int] = {
    "irreversible": 3,
    "destructive": 3,
    "external_effect": 2,
    "writes_data": 1,
    "reads_sensitive_data": 2,
    "financial_effect": 4,
    "security_effect": 3,
    "affects_shared_resource": 1,
}


@dataclass
class RiskAssessment:
    level: RiskLevel
    score: int
    factors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level.value, "score": self.score, "factors": self.factors}


class RiskEngine:
    """Deterministic scoring of an operation's risk."""

    def __init__(self, *, thresholds: dict[RiskLevel, int] | None = None) -> None:
        self.thresholds = thresholds or {
            RiskLevel.CRITICAL: 7,
            RiskLevel.HIGH: 4,
            RiskLevel.MEDIUM: 2,
            RiskLevel.LOW: 1,
        }

    def assess(self, operation: OperationDescriptor) -> RiskAssessment:
        factors: list[str] = []
        score = 0

        axes = {
            "irreversible": not operation.reversible,
            "destructive": operation.destructive,
            "external_effect": operation.external_effect,
            "writes_data": operation.writes_data,
            "reads_sensitive_data": operation.reads_sensitive_data,
            "financial_effect": operation.financial_effect,
            "security_effect": operation.security_effect,
            "affects_shared_resource": operation.affects_shared_resource,
        }
        for axis, present in axes.items():
            if present:
                score += _WEIGHTS[axis]
                factors.append(axis)

        level = RiskLevel.NONE
        for candidate in (
            RiskLevel.CRITICAL,
            RiskLevel.HIGH,
            RiskLevel.MEDIUM,
            RiskLevel.LOW,
        ):
            if score >= self.thresholds[candidate]:
                level = candidate
                break

        # A declared risk is a floor, never a ceiling: a tool may say it is
        # dangerous, but it may not talk itself down.
        if operation.declared_risk is not None and operation.declared_risk.rank > level.rank:
            level = operation.declared_risk
            factors.append("declared_risk")

        return RiskAssessment(level=level, score=score, factors=factors)

    @staticmethod
    def from_tool(spec: Any) -> OperationDescriptor:
        """Build a descriptor from a ``ToolSpec``-shaped object."""
        permissions = list(getattr(spec, "permissions", []) or [])
        lowered = {p.lower() for p in permissions}
        return OperationDescriptor(
            name=getattr(spec, "id", "") or getattr(spec, "name", ""),
            kind=str(getattr(getattr(spec, "source", None), "value", "tool")),
            reversible=bool(getattr(spec, "idempotent", True)),
            destructive=any("delete" in p or "destroy" in p for p in lowered),
            external_effect=any(
                p.startswith(("network", "http", "external", "mcp")) for p in lowered
            ),
            writes_data=any(p.startswith(("write", "fs.write", "db.write")) for p in lowered),
            reads_sensitive_data=any("secret" in p or "credential" in p for p in lowered),
            financial_effect=any("payment" in p or "financial" in p for p in lowered),
            security_effect=any("admin" in p or "security" in p for p in lowered),
            permissions=permissions,
            declared_risk=getattr(spec, "risk", None),
        )
