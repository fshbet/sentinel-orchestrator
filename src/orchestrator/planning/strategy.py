"""Meta-orchestration: choosing how to orchestrate.

The system selects its own orchestration pattern and planning strategy from the
shape of the objective, the capabilities available, and the risk involved (spec
sections 93, 94). A trivial objective gets one agent and one validation; it does
not get ten agents and four reviewers (spec section 58).

Complexity assessment is deterministic and explainable. A model may refine it,
but the floor and ceiling are set here so a confident model cannot talk the
platform into an elaborate plan for a one-line task.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.domain.enums import OrchestrationPattern, PlanStrategy, RiskLevel
from ..core.domain.models import Requirements

# Signals that an objective involves several separable pieces of work.
_CONJUNCTIONS = re.compile(
    r"\b(and then|then|after that|followed by|as well as|also|additionally)\b",
    re.IGNORECASE,
)
_ENUMERATION = re.compile(r"(^|\n)\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_COMPARISON = re.compile(
    r"\b(compare|contrast|evaluate|assess|versus|vs\.?|trade-?offs?|options)\b",
    re.IGNORECASE,
)
_RESEARCH = re.compile(
    r"\b(research|investigate|find out|survey|review the literature|gather|explore)\b",
    re.IGNORECASE,
)
_ITERATIVE = re.compile(
    r"\b(iterate|refine|improve|optimi[sz]e|polish|until|repeatedly)\b", re.IGNORECASE
)
_UNCERTAINTY = re.compile(
    r"\b(somehow|figure out|unclear|not sure|maybe|possibly|unknown|diagnose|debug|why)\b",
    re.IGNORECASE,
)
_TRIVIAL = re.compile(
    r"^\s*(what|who|when|where|list|show|print|echo|define|convert|translate)\b",
    re.IGNORECASE,
)


@dataclass
class ComplexityAssessment:
    score: float
    band: str  # trivial | simple | moderate | complex
    signals: list[str] = field(default_factory=list)
    estimated_tasks: int = 1
    parallelisable: bool = False
    iterative: bool = False
    uncertain: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 3),
            "band": self.band,
            "signals": self.signals,
            "estimated_tasks": self.estimated_tasks,
            "parallelisable": self.parallelisable,
            "iterative": self.iterative,
            "uncertain": self.uncertain,
        }


@dataclass
class OrchestrationChoice:
    pattern: OrchestrationPattern
    plan_strategy: PlanStrategy
    complexity: ComplexityAssessment
    rationale: str
    max_parallel: int = 1
    use_evaluator: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern.value,
            "plan_strategy": self.plan_strategy.value,
            "rationale": self.rationale,
            "max_parallel": self.max_parallel,
            "use_evaluator": self.use_evaluator,
            "complexity": self.complexity.to_dict(),
        }


def assess_complexity(
    objective: str, requirements: Requirements | None = None
) -> ComplexityAssessment:
    """Score an objective from its structure, not its subject matter."""
    text = objective.strip()
    signals: list[str] = []
    score = 0.0

    words = len(text.split())
    if words > 120:
        score += 0.25
        signals.append("long objective")
    elif words > 40:
        score += 0.12
        signals.append("multi-sentence objective")

    enumerated = len(_ENUMERATION.findall(text))
    if enumerated >= 2:
        score += min(0.35, 0.08 * enumerated)
        signals.append(f"{enumerated} enumerated items")

    conjunctions = len(_CONJUNCTIONS.findall(text))
    if conjunctions:
        score += min(0.25, 0.08 * conjunctions)
        signals.append(f"{conjunctions} sequencing conjunctions")

    parallelisable = bool(_COMPARISON.search(text)) or enumerated >= 2
    if _COMPARISON.search(text):
        score += 0.15
        signals.append("comparison or evaluation requested")

    if _RESEARCH.search(text):
        score += 0.15
        signals.append("external information gathering")

    iterative = bool(_ITERATIVE.search(text))
    if iterative:
        score += 0.12
        signals.append("iterative refinement requested")

    uncertain = bool(_UNCERTAINTY.search(text))
    if uncertain:
        score += 0.15
        signals.append("objective contains uncertainty")

    if requirements is not None:
        if len(requirements.explicit) > 3:
            score += 0.1
            signals.append(f"{len(requirements.explicit)} explicit requirements")
        if requirements.unknowns:
            score += 0.1
            signals.append(f"{len(requirements.unknowns)} unknowns")
            uncertain = True
        if len(requirements.success_criteria) > 2:
            score += 0.08
            signals.append("multiple success criteria")

    if _TRIVIAL.match(text) and words <= 25 and not signals:
        score = 0.0
        signals.append("short, single-answer question")

    score = max(0.0, min(1.0, score))
    if score < 0.12:
        band, tasks = "trivial", 1
    elif score < 0.35:
        band, tasks = "simple", 2
    elif score < 0.65:
        band, tasks = "moderate", 4
    else:
        band, tasks = "complex", 7

    return ComplexityAssessment(
        score=score,
        band=band,
        signals=signals,
        estimated_tasks=tasks,
        parallelisable=parallelisable and band in ("moderate", "complex"),
        iterative=iterative,
        uncertain=uncertain,
    )


def choose(
    objective: str,
    *,
    requirements: Requirements | None = None,
    available_capabilities: Sequence[str] = (),
    risk: RiskLevel = RiskLevel.LOW,
    max_parallel: int = 4,
    force_pattern: OrchestrationPattern | None = None,
    force_strategy: PlanStrategy | None = None,
) -> OrchestrationChoice:
    """Select an orchestration pattern and planning strategy."""
    complexity = assess_complexity(objective, requirements)
    reasons: list[str] = []

    if force_pattern is not None:
        return OrchestrationChoice(
            pattern=force_pattern,
            plan_strategy=force_strategy or PlanStrategy.ADAPTIVE,
            complexity=complexity,
            rationale="pattern was specified explicitly by the caller",
            max_parallel=max_parallel if complexity.parallelisable else 1,
        )

    if complexity.band == "trivial":
        pattern = OrchestrationPattern.SINGLE_AGENT
        strategy = PlanStrategy.FULL
        reasons.append("trivial objective: one agent, one validation")
    elif complexity.band == "simple":
        pattern = OrchestrationPattern.SEQUENTIAL
        strategy = PlanStrategy.FULL
        reasons.append("a short sequence covers this objective")
    elif complexity.band == "moderate":
        if complexity.parallelisable:
            pattern = OrchestrationPattern.PARALLEL
            reasons.append("independent branches can run concurrently")
        else:
            pattern = OrchestrationPattern.SEQUENTIAL
            reasons.append("steps depend on each other")
        strategy = PlanStrategy.ADAPTIVE
    else:
        pattern = OrchestrationPattern.DYNAMIC_DAG
        strategy = PlanStrategy.ADAPTIVE
        reasons.append("complex objective: build a dependency graph")

    if complexity.uncertain and complexity.band != "trivial":
        strategy = PlanStrategy.ITERATIVE
        reasons.append("uncertainty means later steps depend on what earlier ones find")

    use_evaluator = complexity.iterative or complexity.band == "complex"
    if complexity.iterative:
        reasons.append("evaluator-optimizer loop requested by the objective")

    if risk.rank >= RiskLevel.HIGH.rank:
        use_evaluator = True
        reasons.append(f"risk is {risk.value}: adding an independent evaluation step")

    if available_capabilities and complexity.band in ("moderate", "complex"):
        if len(set(available_capabilities)) >= 3:
            reasons.append(
                f"{len(set(available_capabilities))} capabilities available for routing"
            )

    return OrchestrationChoice(
        pattern=pattern,
        plan_strategy=force_strategy or strategy,
        complexity=complexity,
        rationale="; ".join(reasons),
        max_parallel=max_parallel if complexity.parallelisable else 1,
        use_evaluator=use_evaluator,
    )
