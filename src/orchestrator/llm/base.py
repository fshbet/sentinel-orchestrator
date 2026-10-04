"""Provider-independent model interface.

The orchestration engine never imports a provider SDK. It builds a
``CompletionRequest``, hands it to whatever provider the router selected, and
receives a ``ModelResponse``. Adding a provider means implementing one class
(spec section 19).
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.domain.enums import ModelCapability
from ..core.domain.models import ModelSpec, Usage


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            out["name"] = self.name
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        return out


@dataclass
class ToolCallRequest:
    """A tool invocation the model asked for. It is a request, not permission."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionRequest:
    messages: list[Message] = field(default_factory=list)
    system: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    # A JSON Schema the response must satisfy, for structured output.
    response_schema: dict[str, Any] | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    stop: list[str] = field(default_factory=list)
    required_capabilities: list[ModelCapability] = field(default_factory=list)
    timeout: float = 120.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def estimated_input_tokens(self) -> int:
        from ..context.budget import estimate_tokens

        return estimate_tokens(self)


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    structured: Any = None
    finish_reason: str = "stop"
    model: str = ""
    provider: str = ""
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ProviderHealth:
    provider: str
    available: bool
    models: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "models": self.models,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "detail": self.detail,
        }


class LLMProvider(abc.ABC):
    """One model backend."""

    name: str = "provider"

    @abc.abstractmethod
    async def generate(self, request: CompletionRequest, model: ModelSpec) -> ModelResponse:
        """Produce a completion. Raise ``ModelError`` subclasses on failure."""

    @abc.abstractmethod
    def models(self) -> list[ModelSpec]:
        """Models this provider can currently serve."""

    async def health(self) -> ProviderHealth:
        """Report availability. Providers that cannot check return available."""
        return ProviderHealth(
            provider=self.name, available=True, models=[m.model for m in self.models()]
        )

    async def stream(
        self, request: CompletionRequest, model: ModelSpec
    ) -> AsyncIterator[str]:
        """Default streaming: yield the finished text once.

        Providers with real streaming override this; callers do not need to
        know which kind they have.
        """
        response = await self.generate(request, model)
        yield response.text

    async def embed(
        self, texts: Sequence[str], model: ModelSpec
    ) -> list[list[float]]:  # pragma: no cover - optional capability
        from ..errors import ModelError

        raise ModelError(f"provider {self.name} does not support embeddings")

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None
