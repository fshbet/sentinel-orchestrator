"""Shared test fixtures.

Tests run the real engine against real stores, a real MCP server subprocess, and
a scripted model. Nothing about the orchestration path is mocked: the model is a
test double because a model is not deterministic, and that is the only seam.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.config.loader import Config, load  # noqa: E402
from orchestrator.core.domain.models import Task  # noqa: E402
from orchestrator.core.state.manager import StateManager  # noqa: E402
from orchestrator.core.state.memory_store import InMemoryStateStore  # noqa: E402
from orchestrator.llm.providers.scripted import (  # noqa: E402
    CallableProvider,
    ScriptedProvider,
)
from orchestrator.observability.audit import AuditLog, NullAuditSink  # noqa: E402
from orchestrator.platform import Orchestrator  # noqa: E402

MCP_TEST_SERVER = str(Path(__file__).resolve().parent / "fixtures" / "mcp_test_server.py")


def run(coro):
    """Run a coroutine in a fresh event loop.

    Used instead of pytest-asyncio so the suite has no plugin dependency and
    each test gets a clean loop.
    """
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Model doubles
# --------------------------------------------------------------------------


def planning_model(
    *,
    tasks: list[dict[str, Any]] | None = None,
    requirements: dict[str, Any] | None = None,
    worker: Callable[[Any], Any] | str = "done: the work is complete",
    complete: bool = True,
) -> CallableProvider:
    """A model that answers goal analysis, planning, and worker turns.

    Which phase a request belongs to is detected from the system prompt, which
    is exactly how a real provider would see it.
    """
    default_tasks = tasks if tasks is not None else [
        {
            "key": "only",
            "name": "do the work",
            "objective": "Accomplish the objective.",
            "validation": {"validator": "non_empty"},
        }
    ]
    default_requirements = requirements or {
        "explicit": ["Accomplish the objective"],
        "success_criteria": [
            {"description": "Work was produced", "validator": "non_empty"}
        ],
        "clarification_needed": False,
    }

    def respond(request):
        system = request.system or ""
        if "extract structured requirements" in system:
            return default_requirements
        if "decompose an objective" in system:
            return {
                "rationale": "test plan",
                "tasks": default_tasks,
                "complete": complete,
            }
        if callable(worker):
            return worker(request)
        return worker

    return CallableProvider(respond, name="test-model")


def failing_worker_model(failures: int = 99, *, message: str = "") -> CallableProvider:
    """A model whose worker turns return empty output, failing validation."""
    state = {"count": 0}

    def worker(request):
        state["count"] += 1
        if state["count"] <= failures:
            return message
        return "recovered: the work is now complete"

    return planning_model(worker=worker)


# --------------------------------------------------------------------------
# Platform fixtures
# --------------------------------------------------------------------------


def make_config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "storage": {"backend": "memory"},
        "logging": {"level": "critical"},
        "plugins": {"enabled": False},
        "workflows": {"directories": []},
    }
    base.update(overrides)
    return load(include_discovered=False, overrides=base)


async def build_platform(
    provider=None, *, config: Config | None = None, **overrides: Any
) -> Orchestrator:
    return await Orchestrator.create(
        config=config or make_config(**overrides),
        providers=[provider] if provider is not None else [],
        connect_mcp=False,
    )


@pytest.fixture
def state_manager() -> StateManager:
    store = InMemoryStateStore()
    return StateManager(store, AuditLog(store))


@pytest.fixture
def audit_sink() -> NullAuditSink:
    return NullAuditSink()


@pytest.fixture
def mcp_server_config() -> dict[str, Any]:
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [MCP_TEST_SERVER],
        "timeout": 20.0,
    }


def make_task(name: str, **kwargs: Any) -> Task:
    return Task(execution_id="exe_test", name=name, objective=name, **kwargs)


__all__ = [
    "run",
    "planning_model",
    "failing_worker_model",
    "build_platform",
    "make_config",
    "make_task",
    "ScriptedProvider",
    "CallableProvider",
    "MCP_TEST_SERVER",
]
