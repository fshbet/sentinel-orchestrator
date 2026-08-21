"""Goal understanding and requirement extraction.

Turns a natural-language objective into structured requirements: what was
actually asked, what is being inferred, what is being assumed, what is unknown,
and what would count as success (spec sections 54, 55).

The distinction between an explicit requirement and an assumption is preserved
in the data model, so an assumption can never be silently promoted into a
requirement later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from ..core.domain.enums import KnowledgeStatus, ModelCapability
from ..core.domain.jsonio import extract_json as _extract_json
from ..core.domain.models import Requirements, SuccessCriterion
from ..llm.base import CompletionRequest, Message
from ..llm.routing import ModelRouter, RoutingRequirements

REQUIREMENTS_SCHEMA: dict[str, Any] = {
    "title": "requirements",
    "type": "object",
    "properties": {
        "explicit": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirements stated directly in the objective.",
        },
        "inferred": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirements strongly implied but not stated.",
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things being assumed that could turn out wrong.",
        },
        "constraints": {"type": "array", "items": {"type": "string"}},
        "unknowns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Information genuinely needed and genuinely missing.",
        },
        "success_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "validator": {
                        "type": ["string", "null"],
                        "description": (
                            "Name of a check that could verify this, or null if "
                            "no deterministic check exists."
                        ),
                    },
                    "mandatory": {"type": "boolean"},
                },
                "required": ["description"],
            },
        },
        "clarification_needed": {
            "type": "boolean",
            "description": (
                "True only if the objective cannot be attempted at all without "
                "an answer. Do not ask for information that is merely nice to have."
            ),
        },
        "clarifying_question": {"type": ["string", "null"]},
    },
    "required": ["explicit", "success_criteria"],
}

SYSTEM_PROMPT = """You extract structured requirements from an objective.

Rules:
- Separate what was stated from what you are inferring or assuming. Never move
  an assumption into the explicit list.
- Success criteria must be checkable statements, not restatements of the goal.
- Name a validator only when a mechanical check genuinely applies. Prefer null
  over inventing one.
- Only set clarification_needed when the work cannot begin at all without an
  answer. Missing detail that a sensible default covers is not a blocker.
- You will be told which tools are available. Never ask a human for anything a
  tool could obtain: if a file can be read, a query can be run, or a page can be
  fetched, that is work to be planned, not a question to ask. Asking for
  something the platform can fetch itself wastes the person's time.
- Do not assume any particular domain, technology, or tooling.

Reply with JSON matching the requested schema and nothing else."""


@dataclass
class GoalAnalysis:
    requirements: Requirements
    clarification_needed: bool = False
    clarifying_question: str | None = None
    source: str = "heuristic"  # heuristic | model

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirements": self.requirements.to_dict(),
            "clarification_needed": self.clarification_needed,
            "clarifying_question": self.clarifying_question,
            "source": self.source,
        }


# Models routinely write these as the *string* value of an optional field
# rather than emitting JSON null. Taking them literally produces a validator
# named "null", which then fails at gate time far from the cause.
_NULL_SENTINELS = frozenset(
    {"", "null", "none", "nil", "n/a", "na", "undefined", "-"}
)


def _validator_name(raw: Any, known: frozenset[str] | None) -> str | None:
    """Normalise a model-supplied validator name, or None if unusable."""
    if raw is None:
        return None
    name = str(raw).strip()
    if name.lower() in _NULL_SENTINELS:
        return None
    if known is not None and name not in known:
        # The model named a checker that does not exist. Claiming it would make
        # the criterion look verifiable when nothing can verify it.
        return None
    return name


class GoalAnalyzer:
    """Extracts requirements, using a model when one is available."""

    def __init__(
        self,
        router: ModelRouter | None = None,
        *,
        known_validators: Sequence[str] | None = None,
    ) -> None:
        self.router = router
        self.known_validators = (
            frozenset(known_validators) if known_validators is not None else None
        )

    async def analyze(
        self,
        objective: str,
        *,
        context: dict[str, Any] | None = None,
        execution_id: str = "",
        available_tools: Sequence[str] = (),
        available_capabilities: Sequence[str] = (),
    ) -> GoalAnalysis:
        if self.router is not None and self.router.all_models():
            try:
                return await self._analyze_with_model(
                    objective,
                    context or {},
                    execution_id,
                    available_tools=available_tools,
                    available_capabilities=available_capabilities,
                )
            except Exception:  # noqa: BLE001 - fall back rather than fail the run
                pass
        return self.heuristic(objective, context or {})

    # -- model-backed extraction ------------------------------------------

    async def _analyze_with_model(
        self,
        objective: str,
        context: dict[str, Any],
        execution_id: str,
        *,
        available_tools: Sequence[str] = (),
        available_capabilities: Sequence[str] = (),
    ) -> GoalAnalysis:
        prompt = [f"Objective:\n{objective}"]
        # Without this the analyser cannot tell a genuine blocker from something
        # the platform can simply go and fetch, and a careful model will stop to
        # ask a human for a file it could have read itself.
        prompt.append(
            "Tools available to the workers who will do this: "
            + (", ".join(available_tools) or "(none beyond internal bookkeeping)")
        )
        if available_capabilities:
            prompt.append("Capabilities available: " + ", ".join(available_capabilities))
        if context:
            prompt.append(
                "Known context (do not treat as requirements):\n"
                + json.dumps(context, default=str)[:4000]
            )
        response = await self.router.complete(
            CompletionRequest(
                system=SYSTEM_PROMPT,
                messages=[Message(role="user", content="\n\n".join(prompt))],
                response_schema=REQUIREMENTS_SCHEMA,
                temperature=0.0,
                max_output_tokens=1500,
            ),
            requirements=RoutingRequirements(
                capabilities=[
                    ModelCapability.TEXT_GENERATION,
                    ModelCapability.STRUCTURED_OUTPUT,
                ]
            ),
            execution_id=execution_id,
        )
        data = response.structured
        if not isinstance(data, dict):
            data = _extract_json(response.text)
        if not isinstance(data, dict):
            raise ValueError("model did not return structured requirements")

        requirements = self._requirements_from(data, objective, self.known_validators)
        return GoalAnalysis(
            requirements=requirements,
            clarification_needed=bool(data.get("clarification_needed", False)),
            clarifying_question=data.get("clarifying_question") or None,
            source="model",
        )

    @staticmethod
    def _requirements_from(
        data: dict[str, Any],
        objective: str,
        known_validators: frozenset[str] | None = None,
    ) -> Requirements:
        explicit = [str(x) for x in data.get("explicit", []) if str(x).strip()]
        inferred = [str(x) for x in data.get("inferred", []) if str(x).strip()]
        assumptions = [str(x) for x in data.get("assumptions", []) if str(x).strip()]
        constraints = [str(x) for x in data.get("constraints", []) if str(x).strip()]
        unknowns = [str(x) for x in data.get("unknowns", []) if str(x).strip()]

        criteria = []
        for entry in data.get("success_criteria", []):
            if isinstance(entry, str):
                criteria.append(SuccessCriterion(description=entry))
            elif isinstance(entry, dict):
                criteria.append(
                    SuccessCriterion(
                        description=str(entry.get("description", "")),
                        validator=_validator_name(
                            entry.get("validator"), known_validators
                        ),
                        validator_config=dict(entry.get("validator_config", {})),
                        mandatory=bool(entry.get("mandatory", True)),
                    )
                )
        if not criteria:
            criteria = [_default_criterion(objective)]

        provenance: dict[str, KnowledgeStatus] = {}
        for item in explicit:
            provenance[item] = KnowledgeStatus.KNOWN
        for item in inferred:
            provenance[item] = KnowledgeStatus.INFERRED
        for item in assumptions:
            provenance[item] = KnowledgeStatus.ASSUMED

        return Requirements(
            explicit=explicit or [objective.strip()],
            inferred=inferred,
            assumptions=assumptions,
            constraints=constraints,
            unknowns=unknowns,
            success_criteria=criteria,
            provenance=provenance,
        )

    # -- deterministic fallback -------------------------------------------

    def heuristic(self, objective: str, context: dict[str, Any]) -> GoalAnalysis:
        """Structure the objective without a model.

        This keeps the platform usable with no provider configured, and gives
        the model-backed path something to be compared against.
        """
        text = objective.strip()
        explicit = _split_requirements(text)
        constraints = _extract_constraints(text)
        unknowns = _extract_unknowns(text)

        requirements = Requirements(
            explicit=explicit or [text],
            inferred=[],
            assumptions=(
                ["No additional context was supplied beyond the objective."]
                if not context
                else []
            ),
            constraints=constraints,
            unknowns=unknowns,
            success_criteria=[_default_criterion(text)],
            provenance={item: KnowledgeStatus.KNOWN for item in (explicit or [text])},
        )
        return GoalAnalysis(
            requirements=requirements,
            clarification_needed=False,
            source="heuristic",
        )


def _default_criterion(objective: str) -> SuccessCriterion:
    return SuccessCriterion(
        description=f"The objective is addressed: {objective.strip()[:200]}",
        validator="non_empty",
        validator_config={"min_length": 1},
        mandatory=True,
    )


_BULLET = re.compile(r"(?:^|\n)\s*(?:[-*•]|\d+[.)])\s+(.+)")
_CONSTRAINT = re.compile(
    r"([^.\n]*\b(?:must not|must|cannot|should not|only|without|within|no more than|"
    r"at most|at least|by [A-Z0-9])\b[^.\n]*)",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"([^.\n?]*\?)")


def _split_requirements(text: str) -> list[str]:
    bullets = [m.strip() for m in _BULLET.findall(text) if m.strip()]
    if bullets:
        return bullets
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return sentences[:8]


def _extract_constraints(text: str) -> list[str]:
    found = [m.strip() for m in _CONSTRAINT.findall(text)]
    unique: list[str] = []
    for item in found:
        if item and item not in unique:
            unique.append(item)
    return unique[:8]


def _extract_unknowns(text: str) -> list[str]:
    return [m.strip() for m in _QUESTION.findall(text) if len(m.strip()) > 8][:5]
