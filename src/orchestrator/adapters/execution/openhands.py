"""OpenHands execution adapter.

OpenHands is an integration target, never a dependency and never part of the
core model (spec sections 48, 103). The orchestrator hands it a self-contained
brief and reads back a structured result; it does not reimplement OpenHands'
agent loop, and nothing in the core knows this adapter exists.

Endpoint paths are configuration, not constants baked into the code, because
the OpenHands HTTP API changes between releases. The defaults below match the
conversation-oriented API; point them elsewhere if your deployment differs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from ...agents.runtime import AgentRunContext
from ...core.domain.enums import Confidence, IsolationLevel
from ...core.domain.models import TaskResult
from ...errors import ModelUnavailable, ToolError
from .base import ExecutionAdapter, build_brief


@dataclass
class OpenHandsEndpoints:
    """Configurable API surface."""

    create: str = "/api/conversations"
    status: str = "/api/conversations/{id}"
    events: str = "/api/conversations/{id}/events"
    stop: str = "/api/conversations/{id}/stop"
    # Keys read from the create response, in order of preference.
    id_keys: tuple[str, ...] = ("conversation_id", "id", "session_id")
    # Status values that mean the run is over.
    terminal_states: tuple[str, ...] = (
        "finished",
        "completed",
        "stopped",
        "error",
        "failed",
        "cancelled",
    )
    error_states: tuple[str, ...] = ("error", "failed", "cancelled")


@dataclass
class OpenHandsConfig:
    base_url: str = "http://localhost:3000"
    api_key: str | None = None
    endpoints: OpenHandsEndpoints = field(default_factory=OpenHandsEndpoints)
    poll_interval: float = 3.0
    timeout: float = 1800.0
    # Extra fields merged into the create payload (workspace, LLM config, ...).
    extra_create_fields: dict[str, Any] = field(default_factory=dict)


class OpenHandsAdapter(ExecutionAdapter):
    name = "openhands"
    # The work happens in OpenHands' own runtime on the other side of an HTTP
    # boundary, so REMOTE is accurate. Whether *that* runtime is containerised
    # is OpenHands' configuration, not something this adapter can promise.
    supported_isolation = frozenset(
        {IsolationLevel.NONE, IsolationLevel.RESTRICTED, IsolationLevel.REMOTE}
    )

    def __init__(self, config: OpenHandsConfig | None = None) -> None:
        self.config = config or OpenHandsConfig()

    # -- helpers -----------------------------------------------------------

    def _client(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ModelUnavailable(
                "the OpenHands adapter requires httpx; install"
                " universal-orchestrator[http]"
            ) from exc
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=headers,
            timeout=60.0,
        )

    @staticmethod
    def _prompt(brief) -> str:
        """Render the brief as the instruction OpenHands receives."""
        sections = [
            f"Overall objective: {brief.overall_objective}",
            f"Your task: {brief.objective}",
        ]
        if brief.expected_outputs:
            sections.append("Expected outputs:\n" + "\n".join(f"- {o}" for o in brief.expected_outputs))
        if brief.completion_criteria:
            sections.append(
                "This is complete when:\n"
                + "\n".join(f"- {c}" for c in brief.completion_criteria)
            )
        if brief.dependency_results:
            sections.append(
                "Results of earlier tasks:\n"
                + json.dumps(brief.dependency_results, default=str)[:6000]
            )
        if brief.inputs:
            sections.append("Inputs:\n" + json.dumps(brief.inputs, default=str)[:4000])
        sections.append(
            "When you are done, print a single JSON object as your final message "
            'with the shape {"ok": true, "summary": "...", "output": ..., '
            '"artifacts": [...]}. Your work will be independently validated, so '
            "do not claim a check passed unless you actually ran it."
        )
        return "\n\n".join(sections)

    # -- execution ---------------------------------------------------------

    async def run(self, context: AgentRunContext) -> TaskResult:
        brief = build_brief(context)
        deadline = time.monotonic() + min(
            self.config.timeout, context.agent.constraints.timeout_seconds
        )

        async with self._client() as client:
            payload = {
                "initial_user_msg": self._prompt(brief),
                **self.config.extra_create_fields,
            }
            if brief.workspace:
                payload.setdefault("workspace", brief.workspace)

            try:
                response = await client.post(self.config.endpoints.create, json=payload)
            except Exception as exc:  # noqa: BLE001 - connection problems are failures
                return self._failure(
                    context.task, f"could not reach OpenHands: {exc}"
                )
            if response.status_code >= 400:
                return self._failure(
                    context.task,
                    f"OpenHands refused the conversation ({response.status_code})",
                    body=response.text[:1000],
                )

            created = response.json()
            conversation_id = next(
                (
                    str(created[key])
                    for key in self.config.endpoints.id_keys
                    if isinstance(created, dict) and created.get(key)
                ),
                None,
            )
            if not conversation_id:
                return self._failure(
                    context.task,
                    "OpenHands did not return a conversation id",
                    response=str(created)[:500],
                )

            state = await self._poll(client, conversation_id, deadline)
            if state is None:
                await self._stop(client, conversation_id)
                return self._failure(
                    context.task, "OpenHands did not finish within the time budget"
                )

            status = str(state.get("status") or state.get("state") or "").lower()
            final = await self._final_message(client, conversation_id)

            if status in self.config.endpoints.error_states:
                return self._failure(
                    context.task,
                    f"OpenHands ended in state {status}",
                    detail=final[:1000] if final else None,
                )
            if final is None:
                return TaskResult(
                    task_id=context.task.id,
                    ok=False,
                    summary="OpenHands finished but produced no readable result",
                    confidence=Confidence.UNCERTAIN,
                )

            parsed: Any = final
            stripped = final.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = final
            return self.parse_result(parsed, context)

    async def _poll(self, client, conversation_id: str, deadline: float):
        path = self.config.endpoints.status.format(id=conversation_id)
        while time.monotonic() < deadline:
            try:
                response = await client.get(path)
            except Exception:  # noqa: BLE001 - a blip should not end the task
                await asyncio.sleep(self.config.poll_interval)
                continue
            if response.status_code >= 400:
                await asyncio.sleep(self.config.poll_interval)
                continue
            state = response.json()
            status = str(state.get("status") or state.get("state") or "").lower()
            if status in self.config.endpoints.terminal_states:
                return state
            await asyncio.sleep(self.config.poll_interval)
        return None

    async def _final_message(self, client, conversation_id: str) -> str | None:
        path = self.config.endpoints.events.format(id=conversation_id)
        try:
            response = await client.get(path)
        except Exception:  # noqa: BLE001
            return None
        if response.status_code >= 400:
            return None
        try:
            events = response.json()
        except ValueError:
            return None
        if isinstance(events, dict):
            events = events.get("events") or events.get("results") or []
        if not isinstance(events, list):
            return None
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            source = str(event.get("source", "")).lower()
            content = (
                event.get("message")
                or (event.get("args") or {}).get("content")
                or event.get("content")
            )
            if source in ("agent", "assistant") and content:
                return str(content)
        return None

    async def _stop(self, client, conversation_id: str) -> None:
        try:
            await client.post(self.config.endpoints.stop.format(id=conversation_id))
        except Exception:  # noqa: BLE001 - best effort
            pass


def build(config: dict[str, Any]) -> OpenHandsAdapter:
    """Construct the adapter from a configuration block."""
    endpoints = OpenHandsEndpoints(**(config.get("endpoints") or {}))
    if not config.get("base_url"):
        raise ToolError("the OpenHands adapter requires a base_url")
    return OpenHandsAdapter(
        OpenHandsConfig(
            base_url=str(config["base_url"]),
            api_key=config.get("api_key"),
            endpoints=endpoints,
            poll_interval=float(config.get("poll_interval", 3.0)),
            timeout=float(config.get("timeout", 1800.0)),
            extra_create_fields=dict(config.get("extra_create_fields", {})),
        )
    )
