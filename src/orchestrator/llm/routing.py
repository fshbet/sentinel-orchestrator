"""Capability-based model routing with fallback.

Callers ask for what a task needs (tool calling, structured output, a context
window of at least N tokens, an acceptable cost) and the router picks a model.
Model names never appear in orchestration logic (spec sections 20, 21, 61).

Failures move the router down an ordered candidate list rather than retrying the
same model forever, and a model that has just failed is put in a short cool-off
so a broken backend does not get re-selected on the next task.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from ..core.domain.enums import ModelCapability
from ..core.domain.models import ModelSpec, Usage
from ..errors import ModelError, NoCapableModel
from ..observability.audit import AuditLog, EventType
from .base import CompletionRequest, LLMProvider, ModelResponse, ProviderHealth


@dataclass
class RoutingRequirements:
    capabilities: list[ModelCapability] = field(default_factory=list)
    min_context_tokens: int = 0
    max_cost_per_1k: float | None = None
    prefer_local: bool = False
    prefer_model: str | None = None
    exclude_models: tuple[str, ...] = ()


@dataclass
class Candidate:
    provider: LLMProvider
    spec: ModelSpec
    score: float
    reason: str


@dataclass
class _Health:
    consecutive_failures: int = 0
    cool_off_until: float = 0.0
    last_error: str = ""


class ModelRouter:
    def __init__(
        self,
        providers: Sequence[LLMProvider] = (),
        *,
        audit: AuditLog | None = None,
        cool_off_seconds: float = 30.0,
        max_fallbacks: int = 3,
        data_policy=None,
        metrics=None,
    ) -> None:
        self._providers: list[LLMProvider] = list(providers)
        self.audit = audit
        self.cool_off_seconds = cool_off_seconds
        self.max_fallbacks = max_fallbacks
        self._health: dict[str, _Health] = {}
        # Checked here rather than at the call sites, because this is the one
        # place every request to every provider passes through. A check at the
        # call sites is a check somebody will forget to add to the next one.
        self.data_policy = data_policy
        # Recording is best-effort by construction: see _record.
        self.metrics = metrics

    def _record(self, method: str, *args, **kwargs) -> None:
        """Record a metric, never letting it break the run.

        Instrumentation is not the work. A collector that raises — a bad
        label, a full series table — must not turn a successful model call
        into a failed one, so every recording goes through here.
        """
        if self.metrics is None:
            return
        try:
            getattr(self.metrics, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001 - deliberate: metrics never fail work
            pass

    # -- registration ------------------------------------------------------

    def register(self, provider: LLMProvider) -> LLMProvider:
        self._providers.append(provider)
        return provider

    @property
    def providers(self) -> list[LLMProvider]:
        return list(self._providers)

    def all_models(self) -> list[tuple[LLMProvider, ModelSpec]]:
        return [(p, spec) for p in self._providers for spec in p.models()]

    def find(self, model_id: str) -> tuple[LLMProvider, ModelSpec] | None:
        for provider, spec in self.all_models():
            if spec.id == model_id or spec.model == model_id:
                return provider, spec
        return None

    # -- selection ---------------------------------------------------------

    def candidates(self, requirements: RoutingRequirements) -> list[Candidate]:
        now = time.monotonic()
        out: list[Candidate] = []
        for provider, spec in self.all_models():
            if spec.id in requirements.exclude_models:
                continue
            if not spec.supports(requirements.capabilities):
                continue
            if (
                requirements.min_context_tokens
                and spec.context_window < requirements.min_context_tokens
            ):
                continue
            cost = max(spec.cost_per_1k_input or 0.0, spec.cost_per_1k_output or 0.0)
            if (
                requirements.max_cost_per_1k is not None
                and cost > requirements.max_cost_per_1k
            ):
                continue

            health = self._health.get(spec.id)
            if health is not None and health.cool_off_until > now:
                continue

            # Lower is better on priority and cost; larger context breaks ties.
            score = float(spec.priority) + cost * 100.0
            reason_parts = [f"priority {spec.priority}"]
            if cost:
                reason_parts.append(f"cost {cost}/1k")
            if requirements.prefer_model and spec.id == requirements.prefer_model:
                score -= 10_000
                reason_parts.append("explicitly preferred")
            if requirements.prefer_local and (spec.cost_per_1k_input or 0.0) == 0.0:
                score -= 100
                reason_parts.append("local")
            score -= min(spec.context_window, 1_000_000) / 1_000_000
            out.append(
                Candidate(
                    provider=provider,
                    spec=spec,
                    score=score,
                    reason=", ".join(reason_parts),
                )
            )
        out.sort(key=lambda c: (c.score, c.spec.id))
        return out

    def _candidates_or_retry(self, requirements: RoutingRequirements) -> list[Candidate]:
        """Candidates, clearing cool-offs rather than reporting nothing to run.

        A cool-off is a hint to prefer something else, not a reason to strand a
        task when it is the only model that could do the work.
        """
        candidates = self.candidates(requirements)
        if not candidates and self._health:
            self._health.clear()
            candidates = self.candidates(requirements)
        return candidates

    def select(self, requirements: RoutingRequirements) -> Candidate:
        candidates = self._candidates_or_retry(requirements)
        if not candidates:
            raise NoCapableModel(
                "no registered model satisfies the requirements",
                capabilities=[c.value for c in requirements.capabilities],
                min_context_tokens=requirements.min_context_tokens,
                registered=[spec.id for _, spec in self.all_models()],
            )
        return candidates[0]

    # -- completion with fallback -----------------------------------------

    async def complete(
        self,
        request: CompletionRequest,
        *,
        requirements: RoutingRequirements | None = None,
        execution_id: str = "",
        task_id: str | None = None,
    ) -> ModelResponse:
        requirements = requirements or RoutingRequirements(
            capabilities=list(request.required_capabilities)
        )
        classification = getattr(request, "data_classification", None)
        if request.required_capabilities and not requirements.capabilities:
            requirements.capabilities = list(request.required_capabilities)

        candidates = self._candidates_or_retry(requirements)
        if not candidates:
            raise NoCapableModel(
                "no registered model satisfies the requirements",
                capabilities=[c.value for c in requirements.capabilities],
                min_context_tokens=requirements.min_context_tokens,
            )

        attempts = candidates[: max(1, self.max_fallbacks)]
        last_error: Exception | None = None
        for index, candidate in enumerate(attempts):
            if self.audit is not None:
                self.audit.record(
                    EventType.MODEL_SELECTED,
                    execution_id=execution_id,
                    task_id=task_id,
                    model=candidate.spec.id,
                    provider=candidate.provider.name,
                    reason=candidate.reason,
                    attempt=index + 1,
                )
            # Before the request is built, not after: the point is that the
            # data never leaves.
            if self.data_policy is not None and self.data_policy.enabled:
                decision = self.data_policy.evaluate(
                    candidate.provider.name, classification
                )
                if self.audit is not None:
                    # What was decided and why. Never the payload — recording
                    # that would recreate the exposure inside the log.
                    self.audit.record(
                        EventType.POLICY_DECISION,
                        execution_id=execution_id,
                        task_id=task_id,
                        subject=f"model:{candidate.spec.id}",
                        **decision.to_dict(),
                    )
                if not decision.allowed:
                    self._record("security_event", "egress")
                    self._record("model_called", candidate.spec.id, "denied", 0.0)
                    last_error = last_error or ModelError(decision.reason)
                    continue

            started = time.monotonic()
            try:
                response = await candidate.provider.generate(request, candidate.spec)
                self._record(
                    "model_called", candidate.spec.id, "ok",
                    time.monotonic() - started,
                )
            except ModelError as exc:
                self._record(
                    "model_called", candidate.spec.id, "error",
                    time.monotonic() - started,
                )
                last_error = exc
                self._record_failure(candidate.spec.id, str(exc))
                if self.audit is not None:
                    self.audit.record(
                        EventType.MODEL_FALLBACK,
                        execution_id=execution_id,
                        task_id=task_id,
                        model=candidate.spec.id,
                        error=exc.code,
                        message=exc.message,
                        remaining=len(attempts) - index - 1,
                    )
                continue
            self._record_success(candidate.spec.id)
            if self.audit is not None:
                self.audit.record(
                    EventType.MODEL_CALL,
                    execution_id=execution_id,
                    task_id=task_id,
                    model=candidate.spec.id,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    finish_reason=response.finish_reason,
                )
            return response

        assert last_error is not None
        if len(attempts) == 1:
            # Preserve the original error type. Wrapping a timeout in a generic
            # ModelError would lose the classification the recovery engine
            # needs to tell "retry this" from "try something else".
            raise last_error
        raise ModelError(
            "every candidate model failed",
            attempted=[c.spec.id for c in attempts],
            last_error_code=getattr(last_error, "code", type(last_error).__name__),
            last_error=str(last_error),
        ) from last_error

    # -- health ------------------------------------------------------------

    def _record_failure(self, model_id: str, error: str) -> None:
        health = self._health.setdefault(model_id, _Health())
        health.consecutive_failures += 1
        health.last_error = error
        # Exponential cool-off, capped, so a flapping backend backs off.
        backoff = min(
            self.cool_off_seconds * (2 ** (health.consecutive_failures - 1)), 300.0
        )
        health.cool_off_until = time.monotonic() + backoff

    def _record_success(self, model_id: str) -> None:
        self._health.pop(model_id, None)

    async def health(self) -> list[ProviderHealth]:
        results = []
        for provider in self._providers:
            try:
                results.append(await provider.health())
            except Exception as exc:  # noqa: BLE001 - health must never raise
                results.append(
                    ProviderHealth(
                        provider=provider.name, available=False, error=str(exc)
                    )
                )
        return results

    def status(self) -> dict[str, dict[str, object]]:
        now = time.monotonic()
        return {
            model_id: {
                "consecutive_failures": h.consecutive_failures,
                "cooling_off": h.cool_off_until > now,
                "last_error": h.last_error,
            }
            for model_id, h in self._health.items()
        }

    @staticmethod
    def accumulate(total: Usage, response: ModelResponse) -> Usage:
        return total.add(response.usage)
