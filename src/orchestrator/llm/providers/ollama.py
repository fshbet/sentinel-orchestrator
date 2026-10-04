"""Ollama provider (native API).

Local models matter for the cost and resource awareness the platform is meant
to have (spec section 60). This provider discovers what is actually installed
via ``/api/tags`` and reports context size and tool support from the model
metadata rather than assuming.
"""

from __future__ import annotations

import json
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


def _require_httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ModelUnavailable(
            "HTTP model providers require httpx; install universal-orchestrator[http]"
        ) from exc
    return httpx


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        models: Sequence[ModelSpec] = (),
        timeout: float = 300.0,
        default_context_window: int = 8192,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._models = list(models)
        self.timeout = timeout
        self.default_context_window = default_context_window

    def models(self) -> list[ModelSpec]:
        return list(self._models)

    def add_model(self, spec: ModelSpec) -> ModelSpec:
        spec.provider = self.name
        self._models.append(spec)
        return spec

    async def discover(self) -> list[ModelSpec]:
        """Ask the daemon which models are installed and register them."""
        httpx = _require_httpx()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            raise ModelUnavailable(
                f"could not list ollama models: {exc}", base_url=self.base_url
            ) from exc

        discovered: list[ModelSpec] = []
        for entry in payload.get("models", []):
            model_name = entry.get("model") or entry.get("name")
            if not model_name:
                continue
            details = entry.get("details") or {}
            capabilities = [
                ModelCapability.TEXT_GENERATION,
                ModelCapability.STREAMING,
                ModelCapability.STRUCTURED_OUTPUT,
            ]
            family = str(details.get("family", "")).lower()
            if any(marker in family for marker in ("llama", "qwen", "mistral", "command")):
                capabilities.append(ModelCapability.TOOL_CALLING)
            if "embed" in model_name.lower():
                capabilities = [ModelCapability.EMBEDDINGS]
            spec = ModelSpec(
                id=f"ollama/{model_name}",
                provider=self.name,
                model=model_name,
                capabilities=capabilities,
                context_window=int(
                    entry.get("context_length") or self.default_context_window
                ),
                metadata={
                    "parameter_size": details.get("parameter_size"),
                    "quantization": details.get("quantization_level"),
                    "size_bytes": entry.get("size"),
                },
            )
            discovered.append(spec)

        known = {m.model for m in self._models}
        self._models.extend(s for s in discovered if s.model not in known)
        return discovered

    async def generate(self, request: CompletionRequest, model: ModelSpec) -> ModelResponse:
        httpx = _require_httpx()
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            entry: dict[str, Any] = {
                "role": "tool" if message.role == "tool" else message.role,
                "content": message.content,
            }
            messages.append(entry)

        payload: dict[str, Any] = {
            "model": model.model,
            "messages": messages,
            "stream": False,
            "options": {},
        }
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature
        if request.max_output_tokens:
            payload["options"]["num_predict"] = request.max_output_tokens
        if request.stop:
            payload["options"]["stop"] = request.stop
        if request.tools:
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
                for tool in request.tools
            ]
        if request.response_schema is not None:
            payload["format"] = request.response_schema

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=request.timeout or self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise ModelTimeout(
                f"ollama timed out calling {model.model}", model=model.model
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                f"ollama could not be reached: {exc}", model=model.model
            ) from exc

        if response.status_code >= 500:
            raise ModelUnavailable(
                f"ollama returned {response.status_code}",
                model=model.model,
                body=response.text[:500],
            )
        if response.status_code >= 400:
            raise ModelError(
                f"ollama rejected the request with {response.status_code}",
                model=model.model,
                body=response.text[:500],
            )

        data = response.json()
        reply = data.get("message") or {}
        text = reply.get("content") or ""
        tool_calls: list[ToolCallRequest] = []
        for index, raw in enumerate(reply.get("tool_calls") or []):
            function = raw.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            tool_calls.append(
                ToolCallRequest(
                    id=str(raw.get("id") or f"call_{index}"),
                    name=function.get("name", ""),
                    arguments=arguments or {},
                )
            )

        # Some models emit a tool call as text instead of through the
        # structured channel; untangle that before anything else reads `text`.
        text, recovered = recover(
            text,
            offered_tools=[t.get("name", "") for t in request.tools],
            existing_calls=tool_calls,
        )
        tool_calls.extend(recovered)

        structured = None
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                structured = json.loads(stripped)
            except json.JSONDecodeError:
                structured = None

        if not text.strip() and not tool_calls:
            raise ModelError(
                f"ollama returned no content for {model.model} "
                f"(done_reason={data.get('done_reason')}). A reasoning model can "
                "consume its whole output budget before answering; raise "
                "max_output_tokens or choose a non-reasoning model.",
                model=model.model,
                finish_reason=data.get("done_reason"),
                output_tokens=int(data.get("eval_count", 0)),
            )

        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            structured=structured,
            finish_reason=data.get("done_reason") or "stop",
            model=model.model,
            provider=self.name,
            usage=Usage(
                model_calls=1,
                input_tokens=int(data.get("prompt_eval_count", 0)),
                output_tokens=int(data.get("eval_count", 0)),
                wall_seconds=time.monotonic() - started,
            ),
            raw=data,
        )

    async def health(self) -> ProviderHealth:
        httpx = _require_httpx()
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
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
                models=[
                    str(m.get("model") or m.get("name")) for m in payload.get("models", [])
                ],
                latency_ms=latency,
                detail={"base_url": self.base_url},
            )
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(provider=self.name, available=False, error=str(exc))

    async def embed(self, texts: Sequence[str], model: ModelSpec) -> list[list[float]]:
        httpx = _require_httpx()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/embed",
                json={"model": model.model, "input": list(texts)},
            )
        if response.status_code >= 400:
            raise ModelError(
                f"ollama embeddings failed with {response.status_code}",
                body=response.text[:500],
            )
        return response.json().get("embeddings", [])
