"""Handling a run that stops to ask a human.

High-risk work waits by default. This shows the shape of the interaction:
run, notice the execution is WAITING, read the prompt, answer it, and the
platform continues from where it stopped.

    PYTHONPATH=src python examples/approval_flow.py
"""

import asyncio

from orchestrator import ExecutionStatus, Orchestrator
from orchestrator.config.loader import load
from orchestrator.llm.providers.scripted import CallableProvider


async def model(request):
    system = request.system or ""
    if "extract structured requirements" in system:
        return {
            "explicit": ["Perform the irreversible operation"],
            "success_criteria": [{"description": "It was done", "validator": "non_empty"}],
        }
    if "decompose an objective" in system:
        return {
            "tasks": [
                {
                    "key": "act",
                    "name": "irreversible step",
                    "objective": "Perform the irreversible operation.",
                    # A task the planner rates critical needs a human first.
                    "risk": "critical",
                }
            ]
        }
    return "The operation was performed and confirmed."


async def main() -> None:
    config = load(
        include_discovered=False,
        overrides={"storage": {"backend": "memory"}, "logging": {"level": "critical"}},
    )
    platform = await Orchestrator.create(
        config=config, providers=[CallableProvider(model)], connect_mcp=False
    )
    try:
        execution = await platform.run("Perform the irreversible operation.")

        if execution.status is not ExecutionStatus.WAITING:
            print(f"unexpected status: {execution.status.value}")
            return

        approval = execution.pending_approval()
        print("the run stopped and is asking:")
        print(f"  risk    {approval.risk.value}")
        print(f"  prompt  {approval.prompt.splitlines()[0]}")

        # In a real deployment this is a person, the CLI, the API, or an MCP
        # client with --allow-control. Rejecting instead would skip the task.
        execution = await platform.approve(
            execution.id, approval.id, approved=True, responder="example"
        )

        print(f"after approval: {execution.status.value} ({execution.confidence.value})")
    finally:
        await platform.close()


if __name__ == "__main__":
    asyncio.run(main())
