"""Scripted and callable providers.

These are test doubles and local-integration seams, not production backends.
They exist so the orchestration engine can be exercised deterministically
(planning, recovery, fallback, context compaction) without a network, and so an
embedder can plug a plain Python function in as a model.

They are never registered automatically. A run only uses them if configuration
asks for them by name, which keeps them from masquerading as a real provider
(spec section 79).
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable, Iterable, Sequence

from ...core.domain.enums import ModelCapability
from ...core.domain.models import ModelSpec, Usage
from ...errors import ModelError
from ..base import (
    CompletionRequest,
    LLMProvider,
    ModelResponse,
    ProviderHealth,
    ToolCallRequest,
)

ALL_CAPABILITIES = [
    ModelCapability.TEXT_GENERATION,
    ModelCapability.TOOL_CALLING,
    ModelCapability.STRUCTURED_OUTPUT,
    ModelCapability.LONG_CONTEXT,
    ModelCapability.STREAMING,
    ModelCapability.REASONING,
]


class ScriptedProvider(LLMProvider):
    """Replays a fixed list of responses in order.

    Anything that is not a ``ModelResponse`` is treated as the response text;
    a dict is returned as structured output. Raising is supported by putting an
    exception instance in the script, which is how model-failure and fallback
    paths are tested.
    """

    name = "scripted"

    def __init__(
        self,
        responses: Iterable[Any] = (),
        *,
        name: str = "scripted",
        model_id: str = "scripted/deterministic",
        capabilities: Sequence[ModelCapability] = tuple(ALL_CAPABILITIES),
        context_window: int = 32000,
        repeat_last: bool = True,
    ) -> None:
        self.name = name
        self._script = list(responses)
        self._index = 0
        self.calls: list[CompletionRequest] = []
        self.repeat_last = repeat_last
        self._models = [
            ModelSpec(
                id=model_id,
                provider=name,
                model=model_id.split("/", 1)[-1],
                capabilities=list(capabilities),
                context_window=context_window,
                max_output_tokens=4096,
                priority=1000,
            )
        ]

    def models(self) -> list[ModelSpec]:
        return list(self._models)

    def queue(self, *responses: Any) -> None:
        self._script.extend(responses)

    async def generate(
        self, request: CompletionRequest, model: ModelSpec
    ) -> ModelResponse:
        self.calls.append(request)
        if not self._script:
            raise ModelError("scripted provider has no responses queued")
        if self._index < len(self._script):
            entry = self._script[self._index]
            self._index += 1
        elif self.repeat_last:
            entry = self._script[-1]
        else:
            raise ModelError("scripted provider exhausted")

        if isinstance(entry, BaseException):
            raise entry
        if callable(entry):
            entry = entry(request)
            if inspect.isawaitable(entry):
                entry = await entry
        if isinstance(entry, ModelResponse):
            entry.model = entry.model or model.model
            entry.provider = entry.provider or self.name
            return entry
        if isinstance(entry, (dict, list)):
            text = json.dumps(entry)
            return ModelResponse(
                text=text,
                structured=entry,
                model=model.model,
                provider=self.name,
                usage=Usage(model_calls=1, output_tokens=len(text) // 4),
            )
        text = str(entry)
        return ModelResponse(
            text=text,
            model=model.model,
            provider=self.name,
            usage=Usage(model_calls=1, output_tokens=max(1, len(text) // 4)),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            available=bool(self._script) or self.repeat_last,
            models=[m.model for m in self._models],
            detail={"queued": max(0, len(self._script) - self._index)},
        )


class CallableProvider(LLMProvider):
    """Wraps a user-supplied function as a model backend.

    The function receives the ``CompletionRequest`` and returns a string, a
    dict, or a ``ModelResponse``. Useful for embedding the orchestrator inside
    a host application that already owns its model access.
    """

    name = "callable"

    def __init__(
        self,
        fn: Callable[[CompletionRequest], Any],
        *,
        name: str = "callable",
        model_id: str = "callable/host",
        capabilities: Sequence[ModelCapability] = tuple(ALL_CAPABILITIES),
        context_window: int = 32000,
    ) -> None:
        self.name = name
        self._fn = fn
        self._models = [
            ModelSpec(
                id=model_id,
                provider=name,
                model=model_id.split("/", 1)[-1],
                capabilities=list(capabilities),
                context_window=context_window,
            )
        ]

    def models(self) -> list[ModelSpec]:
        return list(self._models)

    async def generate(
        self, request: CompletionRequest, model: ModelSpec
    ) -> ModelResponse:
        result = self._fn(request)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ModelResponse):
            return result
        if isinstance(result, (dict, list)):
            return ModelResponse(
                text=json.dumps(result),
                structured=result,
                model=model.model,
                provider=self.name,
                usage=Usage(model_calls=1),
            )
        return ModelResponse(
            text=str(result),
            model=model.model,
            provider=self.name,
            usage=Usage(model_calls=1),
        )


def response_with_tool_call(
    name: str, arguments: dict[str, Any], *, text: str = "", call_id: str = "call_1"
) -> ModelResponse:
    """Helper for building a scripted response that requests a tool."""
    return ModelResponse(
        text=text,
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_use",
        usage=Usage(model_calls=1),
    )
