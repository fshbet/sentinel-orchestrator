"""The devil's advocate.

Every other validator asks "did this pass the check?". This one asks the
opposite question: **what would make this wrong?**

The two are not the same, and the gap between them is where confident-but-wrong
work lives. A schema check confirms shape, not truth. A passing test confirms
the tested path, not the untested one. An agent's summary reads as authoritative
whether or not anything behind it holds up.

So the advocate is deliberately adversarial. It is given the result and told to
argue against it: to find the claims that outran their evidence, the checks that
only look like checks, and the quiet assumptions the work is resting on.

Three rules keep it honest rather than merely negative:

* **It cannot fail a task on its own.** It is an opinion, not a gate, so it is
  registered as non-mandatory by default. A model's suspicion is not proof of a
  defect any more than an agent's confidence is proof of success.
* **It must say what would settle each objection.** An objection with no
  resolution is a complaint. One that names the observation that would answer it
  is a next action.
* **It reaches LIKELY at best.** Like every model judgement in this platform, it
  is recorded as an inference, never as a fact.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.domain.enums import Confidence, EvidenceType, KnowledgeStatus
from ..core.domain.jsonio import extract_json
from ..core.domain.models import Evidence, ValidationResult, ValidationSpec
from .validators import ValidationContext, Validator

# How much an objection should move the reader.
SEVERITIES = ("critical", "substantive", "minor")

# An objection at or above this severity is worth interrupting someone for.
BLOCKING_SEVERITY = "critical"

ADVOCATE_SCHEMA: dict[str, Any] = {
    "title": "challenge",
    "type": "object",
    "properties": {
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "claim": {
                        "type": "string",
                        "description": "The specific claim in the work being challenged.",
                    },
                    "objection": {
                        "type": "string",
                        "description": "Why it may not hold. Be concrete.",
                    },
                    "resolves_it": {
                        "type": "string",
                        "description": (
                            "The observation, check, or answer that would settle "
                            "this one way or the other."
                        ),
                    },
                },
                "required": ["severity", "claim", "objection", "resolves_it"],
            },
        },
        "strongest_case_against": {
            "type": "string",
            "description": (
                "In one or two sentences, the best argument that this work is "
                "not actually finished."
            ),
        },
        "nothing_to_challenge": {
            "type": "boolean",
            "description": (
                "True only when the work is genuinely well-evidenced. Do not "
                "invent objections to appear rigorous."
            ),
        },
    },
    "required": ["objections"],
}

SYSTEM_PROMPT = """You are the devil's advocate reviewing completed work.

Your job is not to be helpful and not to be harsh. It is to find where this work
is weaker than it appears, so a reader is not misled by confident phrasing.

Look specifically for:
- Claims stated as fact that the evidence does not actually support.
- Checks that were declared but only confirm shape, not correctness — a schema
  passing does not mean the values are right.
- Gaps quietly filled in: a value that reads like a measurement but was never
  observed, an absence reported as a finding.
- Work that answers a nearby, easier question than the one that was asked.
- Assumptions the result depends on that were never stated.

Rules:
- Challenge the work, never the worker. No commentary on effort or intent.
- Every objection must name what would settle it. An objection with no
  resolution is a complaint, and complaints are not useful.
- Rank honestly. If the only problems are cosmetic, say "minor" — inflating
  severity destroys your usefulness faster than missing something does.
- If the work is genuinely well-evidenced, set nothing_to_challenge and return
  an empty list. Manufacturing doubt to look rigorous is the one failure mode
  that makes you worse than useless.

Reply with JSON matching the requested schema and nothing else."""


@dataclass
class Objection:
    """One challenge to the work."""

    severity: str = "minor"
    claim: str = ""
    objection: str = ""
    resolves_it: str = ""

    @property
    def rank(self) -> int:
        return SEVERITIES.index(self.severity) if self.severity in SEVERITIES else 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "claim": self.claim,
            "objection": self.objection,
            "resolves_it": self.resolves_it,
        }


@dataclass
class Challenge:
    """The advocate's whole response."""

    objections: list[Objection] = field(default_factory=list)
    strongest_case_against: str = ""
    nothing_to_challenge: bool = False

    @property
    def critical(self) -> list[Objection]:
        return [o for o in self.objections if o.severity == BLOCKING_SEVERITY]

    def sorted(self) -> list[Objection]:
        return sorted(self.objections, key=lambda o: (o.rank, o.claim))

    def to_dict(self) -> dict[str, Any]:
        return {
            "objections": [o.to_dict() for o in self.sorted()],
            "strongest_case_against": self.strongest_case_against,
            "nothing_to_challenge": self.nothing_to_challenge,
            "counts": {
                severity: sum(1 for o in self.objections if o.severity == severity)
                for severity in SEVERITIES
            },
        }


def parse_challenge(payload: Any) -> Challenge:
    """Read a challenge out of whatever the model actually returned."""
    if isinstance(payload, str):
        payload = extract_json(payload)
    if not isinstance(payload, dict):
        return Challenge()

    objections = []
    for entry in payload.get("objections") or []:
        if not isinstance(entry, dict):
            continue
        severity = str(entry.get("severity", "minor")).strip().lower()
        if severity not in SEVERITIES:
            severity = "minor"
        objection = Objection(
            severity=severity,
            claim=str(entry.get("claim", "")).strip(),
            objection=str(entry.get("objection", "")).strip(),
            resolves_it=str(entry.get("resolves_it", "")).strip(),
        )
        # An objection that says nothing, or offers no way to settle it, is
        # noise. Dropping it is kinder to the reader than showing it.
        if objection.objection and objection.resolves_it:
            objections.append(objection)

    return Challenge(
        objections=objections,
        strongest_case_against=str(payload.get("strongest_case_against", "")).strip(),
        nothing_to_challenge=bool(payload.get("nothing_to_challenge", False))
        or not objections,
    )


class DevilsAdvocateValidator(Validator):
    """Argues against the work and reports what it found.

    Registered non-mandatory by default: it lowers confidence and surfaces
    objections, but a suspicion is not a defect, so it does not fail a task on
    its own. Set ``mandatory: true`` and ``block_on: critical`` in the spec
    config when you do want a critical objection to stop the run.
    """

    name = "devils_advocate"

    def __init__(
        self,
        challenger: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> None:
        self._challenge = challenger

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        payload = {
            "objective": context.execution.objective,
            "task": context.task.objective if context.task else "",
            "result": context.output_text()[:20000],
            "checks_that_ran": [
                {
                    "validator": v.validator,
                    "passed": v.passed,
                    "message": v.message[:300],
                }
                for v in (context.task.validation_results if context.task else [])
            ],
            "focus": spec.config.get("focus", ""),
        }

        try:
            raw = await self._challenge(payload)
        except Exception as exc:  # noqa: BLE001 - a silent advocate is the failure
            return self._result(
                spec,
                context,
                passed=True,
                message=f"the devil's advocate could not be reached: {exc}",
                confidence=Confidence.UNCERTAIN,
                evidence=[
                    Evidence(
                        type=EvidenceType.MODEL_JUDGEMENT,
                        source="devils_advocate",
                        summary="no challenge was performed",
                        confidence=Confidence.UNCERTAIN,
                        knowledge_status=KnowledgeStatus.UNVERIFIED,
                    )
                ],
            )

        challenge = parse_challenge(raw)
        blocking = str(spec.config.get("block_on", "")).strip().lower()
        should_block = bool(blocking) and any(
            o.severity == blocking or o.rank < SEVERITIES.index(blocking)
            for o in challenge.objections
            if blocking in SEVERITIES
        )

        if challenge.nothing_to_challenge:
            message = "the advocate found nothing substantive to challenge"
            # Silence from a challenger is not proof; it is one more opinion.
            confidence = Confidence.LIKELY
        else:
            counts = ", ".join(
                f"{n} {severity}"
                for severity in SEVERITIES
                if (n := sum(1 for o in challenge.objections if o.severity == severity))
            )
            message = f"{len(challenge.objections)} objection(s): {counts}"
            confidence = Confidence.UNCERTAIN if challenge.critical else Confidence.LIKELY

        return self._result(
            spec,
            context,
            passed=not should_block,
            message=message,
            confidence=confidence,
            evidence=[
                Evidence(
                    type=EvidenceType.MODEL_JUDGEMENT,
                    source="devils_advocate",
                    summary=(challenge.strongest_case_against or message)[:500],
                    detail=challenge.to_dict(),
                    confidence=confidence,
                    # An argument is an inference, however well made.
                    knowledge_status=KnowledgeStatus.INFERRED,
                )
            ],
        )


def build(router: Any, *, model_preference: str | None = None) -> DevilsAdvocateValidator:
    """Wire the advocate to a model router.

    A separate call from the one that produced the work, deliberately: asking
    the same conversation to critique itself gets agreement, not scrutiny.
    """
    from ..core.domain.enums import ModelCapability
    from ..llm.base import CompletionRequest, Message
    from ..llm.routing import RoutingRequirements

    async def challenge(payload: dict[str, Any]) -> Any:
        response = await router.complete(
            CompletionRequest(
                system=SYSTEM_PROMPT,
                messages=[
                    Message(
                        role="user",
                        content=json.dumps(payload, indent=2, default=str),
                    )
                ],
                response_schema=ADVOCATE_SCHEMA,
                temperature=0.0,
                max_output_tokens=4000,
            ),
            requirements=RoutingRequirements(
                capabilities=[
                    ModelCapability.TEXT_GENERATION,
                    ModelCapability.STRUCTURED_OUTPUT,
                ],
                prefer_model=model_preference,
            ),
        )
        return response.structured if response.structured is not None else response.text

    return DevilsAdvocateValidator(challenge)


def summarise(results: Sequence[ValidationResult]) -> dict[str, Any]:
    """Pull the advocate's findings out of a set of validation results.

    Used by the interfaces to render objections without re-running anything.
    """
    objections: list[dict[str, Any]] = []
    strongest = ""
    ran = False
    for result in results:
        if result.validator != DevilsAdvocateValidator.name:
            continue
        # It ran. Whether it found anything is a separate question, and the
        # two must not be reported the same way: "found nothing" and "was
        # never asked" are opposite claims about how much scrutiny happened.
        ran = True
        for evidence in result.evidence:
            detail = evidence.detail if isinstance(evidence.detail, dict) else {}
            objections.extend(detail.get("objections") or [])
            strongest = strongest or str(detail.get("strongest_case_against", ""))
    return {
        "ran": ran,
        "objections": objections,
        "strongest_case_against": strongest,
        "counts": {
            severity: sum(1 for o in objections if o.get("severity") == severity)
            for severity in SEVERITIES
        },
    }
