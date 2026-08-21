"""Anthropic Messages API provider.

Kept deliberately thin: it speaks the HTTP API directly so the platform does
not take a hard dependency on a vendor SDK, and so the same code path works
against any Anthropic-compatible gateway.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Sequence

from ...core.domain.enums import ModelCapability
from ...core.domain.models import ModelSpec, Usage
from ...errors import ModelError, ModelTimeout, ModelUnavailable
from ..toolnames import build_mapping, rename_tools, restore
from ..base import (
    CompletionRequest,
    LLMProvider,
    ModelResponse,
    ProviderHealth,
    ToolCallRequest,
)

API_VERSION = "2023-06-01"


def _require_httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ModelUnavailable(
            "HTTP model providers require httpx; install universal-orchestrator[http]"
        ) from exc
    return httpx


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        base_url: str = "https://api.anthropic.com/v1",
        api_key: str | None = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
        models: Sequence[ModelSpec] = (),
        api_version: str = API_VERSION,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get(api_key_env)
        self._models = list(models)
        self.api_version = api_version
        self.timeout = timeout

    def models(self) -> list[ModelSpec]:
        return list(self._models)

    def add_model(self, spec: ModelSpec) -> ModelSpec:
        spec.provider = self.name
        self._models.append(spec)
        return spec

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": self.api_version,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _payload(self, request: CompletionRequest, model: ModelSpec) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id or "",
                                "content": message.content,
                            }
                        ],
                    }
                )
            else:
                messages.append({"role": message.role, "content": message.content})

        payload: dict[str, Any] = {
            "model": model.model,
            "messages": messages or [{"role": "user", "content": ""}],
            "max_tokens": request.max_output_tokens or model.max_output_tokens or 1024,
        }
        system_parts = [request.system] if request.system else []
        system_parts += [m.content for m in request.messages if m.role == "system"]
        if system_parts:
            payload["system"] = "\n\n".join(p for p in system_parts if p)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop:
            payload["stop_sequences"] = request.stop
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema")
                    or {"type": "object", "properties": {}},
                }
                for tool in rename_tools(request.tools, build_mapping(request.tools))
            ]
        return payload

    async def generate(
        self, request: CompletionRequest, model: ModelSpec
    ) -> ModelResponse:
        httpx = _require_httpx()
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=request.timeout or self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/messages",
                    headers=self._headers(),
                    json=self._payload(request, model),
                )
        except httpx.TimeoutException as exc:
            raise ModelTimeout(
                f"anthropic timed out calling {model.model}", model=model.model
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                f"anthropic could not be reached: {exc}", model=model.model
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise ModelUnavailable(
                f"anthropic returned {response.status_code}",
                model=model.model,
                status=response.status_code,
                body=response.text[:500],
            )
        if response.status_code >= 400:
            raise ModelError(
                f"anthropic rejected the request with {response.status_code}",
                model=model.model,
                status=response.status_code,
                body=response.text[:500],
            )

        data = response.json()
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCallRequest(
                        id=str(block.get("id", f"call_{len(tool_calls)}")),
                        name=restore(
                            block.get("name", ""), build_mapping(request.tools)
                        ),
                        arguments=block.get("input") or {},
                    )
                )

        text = "".join(text_parts)
        structured = None
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                structured = json.loads(stripped)
            except json.JSONDecodeError:
                structured = None

        usage_data = data.get("usage") or {}
        input_tokens = int(usage_data.get("input_tokens", 0))
        output_tokens = int(usage_data.get("output_tokens", 0))
        cost = 0.0
        if model.cost_per_1k_input:
            cost += input_tokens / 1000 * model.cost_per_1k_input
        if model.cost_per_1k_output:
            cost += output_tokens / 1000 * model.cost_per_1k_output

        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            structured=structured,
            finish_reason=data.get("stop_reason") or "stop",
            model=model.model,
            provider=self.name,
            usage=Usage(
                model_calls=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
                wall_seconds=time.monotonic() - started,
            ),
            raw=data,
        )

    async def health(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                provider=self.name,
                available=False,
                error="no API key configured",
            )
        httpx = _require_httpx()
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/models", headers=self._headers()
                )
            latency = (time.monotonic() - started) * 1000
            if response.status_code >= 400:
                return ProviderHealth(
                    provider=self.name,
                    available=False,
                    error=f"HTTP {response.status_code}",
                    latency_ms=latency,
                )
            payload = response.json()
            return ProviderHealth(
                provider=self.name,
                available=True,
                models=[str(m.get("id")) for m in payload.get("data", []) if m.get("id")]
                or [m.model for m in self._models],
                latency_ms=latency,
            )
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(provider=self.name, available=False, error=str(exc))


DEFAULT_CAPABILITIES = [
    ModelCapability.TEXT_GENERATION,
    ModelCapability.TOOL_CALLING,
    ModelCapability.STRUCTURED_OUTPUT,
    ModelCapability.LONG_CONTEXT,
    ModelCapability.REASONING,
    ModelCapability.VISION,
    ModelCapability.STREAMING,
]
