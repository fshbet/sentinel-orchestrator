"""A task worker that is not Python-aware orchestration code.

The subprocess adapter hands a worker a JSON brief on stdin and reads a JSON
result from stdout. That is the whole contract, so a worker can be written in
any language. This one happens to be Python for convenience.

Register it:

    from orchestrator.adapters.execution.subprocess_adapter import SubprocessAdapter

    platform.engine.c.runtimes.register(
        SubprocessAdapter(["python", "examples/external_worker.py"], name="external")
    )

then point an agent at it with `runtime: external`.
"""

import json
import sys


def handle(brief: dict) -> dict:
    """Do the work described by the brief and report it structurally."""
    objective = brief["objective"]
    upstream = brief.get("dependency_results", [])

    # Real work would happen here, using only what the brief granted:
    # brief["allowed_tools"], brief["permissions"], brief["workspace"].
    finding = f"Handled: {objective}"
    if upstream:
        finding += f" (building on {len(upstream)} upstream result(s))"

    return {
        # Success must be stated. Silence is treated as failure, deliberately.
        "ok": True,
        "summary": finding,
        "output": {"objective": objective, "handled": True},
        "artifacts": [
            {"name": "worker-note", "type": "text", "content": finding}
        ],
        # Evidence is what makes the result checkable rather than merely claimed.
        "evidence": [
            {"source": "external_worker", "summary": "worker completed its brief"}
        ],
        # Anything above "likely" is capped by the platform: an adapter does not
        # get to certify its own work.
        "confidence": "likely",
    }


def main() -> None:
    brief = json.load(sys.stdin)
    try:
        result = handle(brief)
    except Exception as exc:  # noqa: BLE001 - report failure rather than crashing
        result = {"ok": False, "summary": f"{type(exc).__name__}: {exc}"}
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
