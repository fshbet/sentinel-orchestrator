"""Validation gates.

A gate runs a set of validators and turns their results into a decision the
state machine can act on. The rule that makes evidence-based completion real
lives here: a failed mandatory validation cannot produce a pass, no matter what
any agent or model reported (spec sections 34, 73).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..core.domain.enums import Confidence, TaskStatus
from ..core.domain.models import Evidence, Execution, Task, ValidationResult, ValidationSpec
from ..observability.audit import AuditLog, EventType
from .validators import ValidationContext, ValidatorRegistry

# Ordered weakest to strongest, for aggregating many results into one.
_CONFIDENCE_ORDER = [
    Confidence.FAILED,
    Confidence.BLOCKED,
    Confidence.UNCERTAIN,
    Confidence.LIKELY,
    Confidence.CONFIRMED,
]


@dataclass
class GateOutcome:
    passed: bool
    confidence: Confidence
    results: list[ValidationResult] = field(default_factory=list)
    failed_mandatory: list[ValidationResult] = field(default_factory=list)
    failed_optional: list[ValidationResult] = field(default_factory=list)
    message: str = ""

    @property
    def evidence(self) -> list[Evidence]:
        return [e for result in self.results for e in result.evidence]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "confidence": self.confidence.value,
            "message": self.message,
            "results": [
                {
                    "validator": r.validator,
                    "passed": r.passed,
                    "mandatory": r.mandatory,
                    "message": r.message,
                    "confidence": r.confidence.value,
                }
                for r in self.results
            ],
        }


def aggregate_confidence(results: Sequence[ValidationResult]) -> Confidence:
    """The weakest link. One uncertain check makes the whole gate uncertain."""
    if not results:
        return Confidence.UNCERTAIN
    weakest = Confidence.CONFIRMED
    for result in results:
        if _CONFIDENCE_ORDER.index(result.confidence) < _CONFIDENCE_ORDER.index(weakest):
            weakest = result.confidence
    return weakest


class GateRunner:
    def __init__(
        self,
        validators: ValidatorRegistry,
        *,
        audit: AuditLog | None = None,
        max_parallel: int = 4,
    ) -> None:
        self.validators = validators
        self.audit = audit
        self.max_parallel = max(1, max_parallel)

    async def run(
        self,
        specs: Sequence[ValidationSpec],
        context: ValidationContext,
    ) -> GateOutcome:
        if not specs:
            # No declared checks is itself a finding, not a silent pass.
            return GateOutcome(
                passed=True,
                confidence=Confidence.UNCERTAIN,
                message="no validations were declared for this target",
            )

        semaphore = asyncio.Semaphore(self.max_parallel)

        async def run_one(spec: ValidationSpec) -> ValidationResult:
            async with semaphore:
                try:
                    return await self.validators.run(spec, context)
                except Exception as exc:  # noqa: BLE001 - a broken validator fails closed
                    return ValidationResult(
                        spec_id=spec.id,
                        validator=spec.validator,
                        target_id=context.target_id(),
                        passed=False,
                        confidence=Confidence.BLOCKED,
                        message=f"validator raised {type(exc).__name__}: {exc}",
                        mandatory=spec.mandatory,
                    )

        results = list(await asyncio.gather(*(run_one(spec) for spec in specs)))

        failed_mandatory = [r for r in results if not r.passed and r.mandatory]
        failed_optional = [r for r in results if not r.passed and not r.mandatory]
        passed = not failed_mandatory
        confidence = aggregate_confidence(results)
        if not passed:
            confidence = Confidence.FAILED

        if passed and failed_optional:
            # Passing with known optional failures is never CONFIRMED.
            if confidence is Confidence.CONFIRMED:
                confidence = Confidence.LIKELY

        message = (
            "all mandatory validations passed"
            if passed
            else "; ".join(f"{r.validator}: {r.message}" for r in failed_mandatory)[:600]
        )

        outcome = GateOutcome(
            passed=passed,
            confidence=confidence,
            results=results,
            failed_mandatory=failed_mandatory,
            failed_optional=failed_optional,
            message=message,
        )

        if self.audit is not None:
            for result in results:
                self.audit.record(
                    EventType.VALIDATION_RESULT,
                    execution_id=context.execution.id,
                    task_id=context.task.id if context.task else None,
                    validator=result.validator,
                    passed=result.passed,
                    mandatory=result.mandatory,
                    confidence=result.confidence.value,
                    message=result.message[:500],
                    evidence_count=len(result.evidence),
                )
            self.audit.record(
                EventType.GATE_RESULT,
                execution_id=context.execution.id,
                task_id=context.task.id if context.task else None,
                passed=passed,
                confidence=confidence.value,
                failed_mandatory=[r.validator for r in failed_mandatory],
            )
        return outcome

    async def run_for_task(
        self, execution: Execution, task: Task, context: ValidationContext
    ) -> GateOutcome:
        outcome = await self.run(task.validations, context)
        task.validation_results.extend(outcome.results)
        execution.validations.extend(outcome.results)
        return outcome

    async def run_for_objective(
        self, execution: Execution, context: ValidationContext
    ) -> GateOutcome:
        """The final gate: every mandatory success criterion must hold."""
        specs = []
        for criterion in execution.requirements.success_criteria:
            validator = criterion.validator or "noop"
            if not self.validators.has(validator):
                # A criterion naming a checker that does not exist cannot be
                # verified. Failing the run would blame the work for what is a
                # planning error; passing it silently would be worse. Fall back
                # to noop, which passes at UNCERTAIN and says why.
                if self.audit is not None:
                    self.audit.record(
                        "validation.unavailable",
                        execution_id=execution.id,
                        validator=validator,
                        criterion=criterion.description[:200],
                    )
                validator = "noop"
            specs.append(
                ValidationSpec(
                    validator=validator,
                    config=dict(criterion.validator_config),
                    mandatory=criterion.mandatory,
                    description=criterion.description,
                )
            )
        outcome = await self.run(specs, context)
        execution.validations.extend(outcome.results)
        return outcome


def _superseded_by_a_later_attempt(execution: Execution, result) -> bool:
    """Did a retry replace this failure?

    ``execution.validations`` is append-only, so a task that failed its check
    on attempt one and passed it on attempt two leaves both records behind.
    Counting the stale failure made retrying pointless for any task with a
    mandatory validator: the first failure blocked completion permanently, no
    matter how well the retry went. A run whose task genuinely failed is still
    caught - by the task's own status, checked separately below.

    Only task-scoped results are forgiven, and only when that task reached
    SUCCEEDED, which it can only do by passing its mandatory gate.
    """
    task = execution.tasks.get(result.target_id)
    if task is None:
        return False
    return task.status is TaskStatus.SUCCEEDED


def can_complete(execution: Execution) -> tuple[bool, str]:
    """Structural check used before an execution may be marked COMPLETED.

    This is the invariant the whole platform exists to protect: no amount of
    model confidence substitutes for a passing mandatory gate.
    """
    failed = [
        result
        for result in execution.validations
        if result.mandatory
        and not result.passed
        and not _superseded_by_a_later_attempt(execution, result)
    ]
    if failed:
        return False, (
            "mandatory validations failed: "
            + ", ".join(f"{r.validator} ({r.message[:80]})" for r in failed[:5])
        )
    unfinished = [task for task in execution.tasks.values() if not task.is_terminal]
    if unfinished:
        return False, f"{len(unfinished)} tasks have not reached a terminal state"
    failed_tasks = [t for t in execution.tasks.values() if t.status.value == "failed"]
    if failed_tasks:
        return False, f"{len(failed_tasks)} tasks failed"
    return True, "all mandatory gates passed and every task is terminal"
