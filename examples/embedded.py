"""Driving the platform from Python.

Shows three things: a plain run, a run against a host application's own model
access, and reading the result honestly (status *and* confidence).

    PYTHONPATH=src python examples/embedded.py
"""

import asyncio

from orchestrator import Orchestrator
from orchestrator.config.loader import load
from orchestrator.llm.providers.scripted import CallableProvider


async def host_model(request):
    """Stand-in for whatever model access the host application already has.

    A real implementation would call your provider here. The request carries
    `system`, `messages`, `tools`, and `response_schema`; return a string, a
    dict (treated as structured output), or a `ModelResponse`.
    """
    system = request.system or ""
    if "extract structured requirements" in system:
        return {
            "explicit": ["Produce a short written summary"],
            "success_criteria": [
                {"description": "A summary exists", "validator": "non_empty"}
            ],
            "clarification_needed": False,
        }
    if "decompose an objective" in system:
        return {
            "rationale": "one step is enough for this",
            "tasks": [
                {
                    "key": "summarise",
                    "name": "summarise",
                    "objective": "Write the summary.",
                    "validation": {"validator": "non_empty"},
                }
            ],
            "complete": True,
        }
    return "The material describes three approaches and favours the second."


async def main() -> None:
    # An in-memory store keeps the example self-contained; the default is
    # SQLite under .orchestrator/.
    config = load(
        include_discovered=False,
        overrides={"storage": {"backend": "memory"}, "logging": {"level": "critical"}},
    )

    platform = await Orchestrator.create(
        config=config,
        providers=[CallableProvider(host_model, name="host")],
        connect_mcp=False,
    )
    try:
        execution = await platform.run("Summarise the supplied material.")

        print(f"status      {execution.status.value}")
        print(f"confidence  {execution.confidence.value}")
        print(f"tasks       {len(execution.tasks)}")
        for task in execution.tasks.values():
            print(f"  [{task.status.value}] {task.name} -> {task.assigned_agent}")
        for validation in execution.validations:
            mark = "PASS" if validation.passed else "FAIL"
            print(f"  {mark} {validation.validator} ({validation.confidence.value})")

        # The audit trail is the record to reach for when the outcome is not
        # what you expected. Every decision is in it, in order.
        events = await platform.audit(execution.id)
        print(f"audit       {len(events)} events")
    finally:
        await platform.close()


if __name__ == "__main__":
    asyncio.run(main())
