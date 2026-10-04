"""OpenAI-compatible chat-completions provider.

One implementation covers OpenAI itself and every server that speaks the same
endpoint: vLLM, LM Studio, llama.cpp server, Ollama's compatibility layer, and
most hosted gateways. The base URL and model list come from configuration, so
no model name is hard-coded into orchestration logic (spec section 61).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from typing import Any

from ...core.domain.enums import ModelCapability
from ...core.domain.models import ModelSpec, Usage
from ...errors import ModelError, ModelTimeout, ModelUnavailable
from ..base import (
    CompletionRequest,
    LLMProvider,
    ModelResponse,
    ProviderHealth,
    ToolCallRequest,
)
from ..toolcalls import recover
from ..toolnames import build_mapping, rename_tools, restore


def _require_httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ModelUnavailable(
            "HTTP model providers require httpx; install universal-orchestrator[http]"
        ) from exc
    return httpx


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        models: Sequence[ModelSpec] = (),
        name: str | None = None,
        default_headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get(api_key_env)
        self._models = list(models)
        self._default_headers = dict(default_headers or {})
        self.timeout = timeout
        if name:
            self.name = name

    def models(self) -> list[ModelSpec]:
        return list(self._models)

    def add_model(self, spec: ModelSpec) -> ModelSpec:
        spec.provider = self.name
        self._models.append(spec)
        return spec

    # -- request construction ---------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._default_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _payload(
        self,
        request: CompletionRequest,
        model: ModelSpec,
        mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            if message.role == "tool":
                messages.append(
                    {
                        "role": "tool",
                        "content": message.content,
                        "tool_call_id": message.tool_call_id or "",
                    }
                )
            else:
                messages.append(message.to_dict())

        payload: dict[str, Any] = {
            "model": model.model,
            "messages": messages,
        }
        max_tokens = request.max_output_tokens or model.max_output_tokens
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop:
            payload["stop"] = request.stop
        if request.tools:
            # Provider-safe names on the wire; the mapping restores the real
            # ids when a call comes back.
            offered = rename_tools(request.tools, mapping or {})
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema")
                        or {"type": "object", "properties": {}},
                    },
                }
                for tool in offered
            ]
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_schema.get("title", "response"),
                    "schema": request.response_schema,
                    "strict": False,
                },
            }
        return payload

    # -- generation --------------------------------------------------------

    async def generate(self, request: CompletionRequest, model: ModelSpec) -> ModelResponse:
        httpx = _require_httpx()
        mapping = build_mapping(request.tools)
        payload = self._payload(request, model, mapping)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=request.timeout or self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ModelTimeout(
                f"{self.name} timed out calling {model.model}", model=model.model
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                f"{self.name} could not be reached: {exc}", model=model.model
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise ModelUnavailable(
                f"{self.name} returned {response.status_code}",
                model=model.model,
                status=response.status_code,
                body=response.text[:500],
            )
        if response.status_code >= 400:
            raise ModelError(
                f"{self.name} rejected the request with {response.status_code}",
                model=model.model,
                status=response.status_code,
                body=response.text[:500],
            )

        data = response.json()
        return self._parse(
            data,
            model,
            elapsed=time.monotonic() - started,
            offered_tools=[t.get("name", "") for t in request.tools],
            tool_names=mapping,
        )

    def _parse(
        self,
        data: dict[str, Any],
        model: ModelSpec,
        *,
        elapsed: float,
        offered_tools: Sequence[str] = (),
        tool_names: dict[str, str] | None = None,
    ) -> ModelResponse:
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"{self.name} returned no choices", model=model.model)
        message = choices[0].get("message") or {}
        text = message.get("content") or ""

        tool_calls: list[ToolCallRequest] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            tool_calls.append(
                ToolCallRequest(
                    id=str(raw.get("id") or f"call_{len(tool_calls)}"),
                    name=restore(function.get("name", ""), tool_names or {}),
                    arguments=arguments or {},
                )
            )

        text, recovered = recover(
            text,
            offered_tools=offered_tools,
            existing_calls=tool_calls,
        )
        tool_calls.extend(recovered)

        structured = None
        if text:
            stripped = text.strip()
            if stripped.startswith(("{", "[")):
                try:
                    structured = json.loads(stripped)
                except json.JSONDecodeError:
                    structured = None

        usage_data = data.get("usage") or {}
        input_tokens = int(usage_data.get("prompt_tokens", 0))
        output_tokens = int(usage_data.get("completion_tokens", 0))

        finish_reason = choices[0].get("finish_reason") or "stop"

        # An empty completion is never a usable answer, and returning it as one
        # pushes a confusing "the model said nothing" failure downstream. The
        # common cause is a reasoning model spending its entire output budget on
        # reasoning tokens before emitting any content, so say that plainly and
        # let the router try something else.
        if not text.strip() and not tool_calls:
            reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
            if finish_reason == "length" or reasoning:
                raise ModelError(
                    f"{self.name} returned no content for {model.model}: the output "
                    f"budget was consumed before an answer was produced "
                    f"(finish_reason={finish_reason}, {output_tokens} output tokens"
                    + (", reasoning tokens included" if reasoning else "")
                    + "). Raise max_output_tokens for this model.",
                    model=model.model,
                    finish_reason=finish_reason,
                    output_tokens=output_tokens,
                    reasoning_model=bool(reasoning),
                )
            raise ModelError(
                f"{self.name} returned an empty response for {model.model}",
                model=model.model,
                finish_reason=finish_reason,
            )
        cost = 0.0
        if model.cost_per_1k_input:
            cost += input_tokens / 1000 * model.cost_per_1k_input
        if model.cost_per_1k_output:
            cost += output_tokens / 1000 * model.cost_per_1k_output

        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            structured=structured,
            finish_reason=finish_reason,
            model=model.model,
            provider=self.name,
            usage=Usage(
                model_calls=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
                wall_seconds=elapsed,
            ),
            raw=data,
        )

    # -- health ------------------------------------------------------------

    async def health(self) -> ProviderHealth:
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
            served = [
                str(entry.get("id")) for entry in payload.get("data", []) if entry.get("id")
            ]
            return ProviderHealth(
                provider=self.name,
                available=True,
                models=served or [m.model for m in self._models],
                latency_ms=latency,
            )
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(provider=self.name, available=False, error=str(exc))

    async def embed(self, texts: Sequence[str], model: ModelSpec) -> list[list[float]]:
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers=self._headers(),
                json={"model": model.model, "input": list(texts)},
            )
        if response.status_code >= 400:
            raise ModelError(
                f"{self.name} embeddings failed with {response.status_code}",
                body=response.text[:500],
            )
        return [item["embedding"] for item in response.json().get("data", [])]


DEFAULT_CAPABILITIES = [
    ModelCapability.TEXT_GENERATION,
    ModelCapability.TOOL_CALLING,
    ModelCapability.STRUCTURED_OUTPUT,
    ModelCapability.STREAMING,
]
