"""Generic external-worker adapter.

Runs any executable as a task worker: the brief goes to stdin as JSON, the
result comes back on stdout as JSON. That is enough to plug in a worker written
in any language, or a shell script, without the platform knowing anything about
it (spec section 47).

This is the adapter to reach for before writing a bespoke one.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Sequence
from typing import Any

from ...agents.runtime import AgentRunContext
from ...core.domain.enums import IsolationLevel
from ...core.domain.models import TaskResult
from ...errors import ToolError
from .base import ExecutionAdapter, build_brief


class SubprocessAdapter(ExecutionAdapter):
    """Executes tasks by invoking an external program.

    Provides real RESTRICTED isolation: the work happens in a separate process
    whose environment is scrubbed and whose working directory is confined to the
    workspace. SANDBOX, CONTAINER, and REMOTE are not claimed, because this
    adapter cannot enforce them - an agent declaring one of those fails rather
    than silently running with less isolation than it asked for.
    """

    supported_isolation = frozenset({IsolationLevel.NONE, IsolationLevel.RESTRICTED})

    def __init__(
        self,
        command: str | Sequence[str],
        *,
        name: str = "subprocess",
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 900.0,
        inherit_env: bool = True,
    ) -> None:
        self.name = name
        self.argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not self.argv:
            raise ToolError("subprocess adapter requires a command")
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = timeout
        self.inherit_env = inherit_env

    async def run(self, context: AgentRunContext) -> TaskResult:
        brief = build_brief(context)
        payload = json.dumps(brief.to_dict(), default=str)

        restricted = context.agent.constraints.isolation is IsolationLevel.RESTRICTED
        # Under RESTRICTED the child inherits nothing: no credentials, no proxy
        # settings, no tokens that happen to be in the parent environment.
        inherit = self.inherit_env and not restricted
        environment = dict(os.environ) if inherit else {}
        environment.update(self.env)
        environment["ORCHESTRATOR_TASK_ID"] = context.task.id
        environment["ORCHESTRATOR_EXECUTION_ID"] = context.execution.id
        environment["ORCHESTRATOR_ISOLATION"] = context.agent.constraints.isolation.value

        workdir = self.cwd or context.workspace
        if restricted and workdir is None:
            return self._failure(
                context.task,
                "restricted isolation requires a workspace to confine the worker to",
            )

        try:
            process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=environment,
            )
        except OSError as exc:
            return self._failure(
                context.task, f"could not start worker {self.argv[0]}: {exc}"
            )

        timeout = min(self.timeout, context.agent.constraints.timeout_seconds)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload.encode("utf-8")), timeout=timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return self._failure(context.task, f"worker did not finish within {timeout}s")

        if process.returncode != 0:
            return self._failure(
                context.task,
                f"worker exited {process.returncode}",
                stderr=stderr.decode("utf-8", errors="replace")[:2000],
            )

        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return self._failure(context.task, "worker produced no output")

        parsed: Any
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # A worker that prints prose is still usable; treat it as the answer.
            parsed = text
        return self.parse_result(parsed, context)
