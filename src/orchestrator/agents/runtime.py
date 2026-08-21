"""Agent runtime.

The agent loop is OBSERVE -> REASON -> SELECT ACTION -> EXECUTE -> OBSERVE, but
the agent does not own orchestration state (spec section 8). It receives one
task, a scoped set of tools, and a context window budget, and it returns a
structured result. Whether that result is acceptable is decided elsewhere.

Every bound - iterations, tool calls, model calls, wall time - is enforced by
this loop rather than requested of the model.
"""

from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from ..context.manager import ContextManager, ContextRequest
from ..core.domain.enums import Confidence, IsolationLevel, ModelCapability
from ..core.domain.models import (
    AgentSpec,
    Artifact,
    Evidence,
    Execution,
    Task,
    TaskResult,
    ToolCall,
    Usage,
)
from ..core.policy.engine import PermissionScope
from ..errors import (
    ApprovalRequired,
    ModelError,
    NoCapableModel,
    PermissionDenied,
    ResourceLimitExceeded,
)
from ..llm.base import Message, ModelResponse
from ..llm.routing import ModelRouter, RoutingRequirements
from ..observability.audit import AuditLog
from ..tools.registry import ToolContext, ToolRegistry
from .skills import SkillRegistry

SYSTEM_PROMPT = """You are a worker executing one task inside a larger orchestrated run.

How to work:
- Do the task you were given. Do not expand it, and do not do other tasks.
- Use the tools you were granted. If you need something you were not granted,
  say so plainly in your answer rather than pretending or working around it.
- Base conclusions on what tools and inputs actually returned. If you could not
  establish something, say which part is unverified.
- When you are finished, reply with your result as plain prose. Do not claim a
  check passed unless a tool actually reported that.

Your work will be independently validated. Claiming completion does not make a
task complete."""


@dataclass
class AgentRunContext:
    """Everything the runtime needs for one task attempt."""

    execution: Execution
    task: Task
    agent: AgentSpec
    scope: PermissionScope
    workspace: str | None = None
    instructions: str = ""
    extra_context: dict[str, Any] = field(default_factory=dict)


class AgentRuntime(Protocol):
    """A way of executing a task. Adapters implement this."""

    name: str
    # Isolation levels this runtime can genuinely provide. A runtime that
    # claims a level it does not enforce is worse than one that claims none.
    supported_isolation: frozenset[IsolationLevel]

    async def run(self, context: AgentRunContext) -> TaskResult: ...


class BaseRuntime(abc.ABC):
    name = "base"

    # In-process execution offers no isolation beyond the scoping the platform
    # applies to every task. Adapters that can do better say so.
    supported_isolation: frozenset[IsolationLevel] = frozenset({IsolationLevel.NONE})

    @abc.abstractmethod
    async def run(self, context: AgentRunContext) -> TaskResult: ...

    @classmethod
    def provides(cls, level: IsolationLevel) -> bool:
        return level in cls.supported_isolation

    @staticmethod
    def _failure(
        task: Task,
        message: str,
        *,
        usage: Usage | None = None,
        **details: Any,
    ) -> TaskResult:
        """A failed attempt still spent budget, and that has to stay visible.

        Dropping the accrued usage here would make a failing agent look free,
        which is exactly the run you most want to see the cost of.
        """
        return TaskResult(
            task_id=task.id,
            ok=False,
            summary=message,
            confidence=Confidence.FAILED,
            usage=usage or Usage(),
            error={"message": message, **details},
        )


class GenericAgentRuntime(BaseRuntime):
    """The default runtime: a bounded model-and-tools loop."""

    name = "generic"
    # RESTRICTED is honest here: the agent runs in-process, but it can only see
    # and call the tools in its scope, and filesystem and process tools are
    # confined to the workspace. Stronger levels need a separate process or
    # machine, which is an adapter's job.
    supported_isolation = frozenset({IsolationLevel.NONE, IsolationLevel.RESTRICTED})

    def __init__(
        self,
        *,
        router: ModelRouter,
        tools: ToolRegistry,
        context_manager: ContextManager,
        audit: AuditLog | None = None,
        skills: "SkillRegistry | None" = None,
    ) -> None:
        self.router = router
        self.tools = tools
        self.context = context_manager
        self.audit = audit
        self.skills = skills

    def _skill_guidance(self, run_context: AgentRunContext) -> str:
        """Compose the skills this agent and task declare.

        Skills are knowledge, not capability: they can tell an agent how to
        approach the work, and cannot grant it anything.
        """
        if self.skills is None:
            return ""
        declared = list(run_context.agent.skills)
        declared += [
            s for s in run_context.task.metadata.get("skills", []) if s not in declared
        ]
        if not declared:
            return ""
        return self.skills.render(
            declared, pinned=run_context.execution.workflow.skill_versions
        )

    async def run(self, run_context: AgentRunContext) -> TaskResult:
        task = run_context.task
        agent = run_context.agent
        constraints = agent.constraints
        started = time.monotonic()
        usage = Usage()
        artifacts: list[Artifact] = []
        evidence: list[Evidence] = []
        history: list[Message] = []
        transcript: list[dict[str, Any]] = []

        tool_context = ToolContext(
            execution_id=run_context.execution.id,
            task_id=task.id,
            agent_id=agent.id,
            scope=run_context.scope,
            workspace=run_context.workspace,
        )
        skill_guidance = self._skill_guidance(run_context)
        visible_tools = self.tools.for_scope(run_context.scope)
        tool_schemas = [
            {
                "name": spec.id,
                "description": spec.description,
                "input_schema": spec.input_schema or {"type": "object", "properties": {}},
            }
            for spec in visible_tools
        ]

        capabilities = list(task.model_requirements) or list(agent.model_requirements)
        if tool_schemas and ModelCapability.TOOL_CALLING not in capabilities:
            capabilities.append(ModelCapability.TOOL_CALLING)
        if not capabilities:
            capabilities = [ModelCapability.TEXT_GENERATION]

        routing = RoutingRequirements(
            capabilities=capabilities,
            prefer_model=task.assigned_model or agent.model,
            exclude_models=tuple(task.metadata.get("excluded_models", [])),
        )
        try:
            selection = self.router.select(routing)
        except NoCapableModel:
            if not routing.exclude_models:
                raise
            # The exclusion came from a recovery attempt, not from a hard
            # requirement. A single-model deployment must still be able to
            # retry rather than stalling on an empty candidate list.
            routing = replace(routing, exclude_models=())
            selection = self.router.select(routing)
        task.assigned_model = selection.spec.id

        for iteration in range(1, constraints.max_iterations + 1):
            if time.monotonic() - started > constraints.timeout_seconds:
                return self._failure(
                    task,
                    f"agent exceeded its {constraints.timeout_seconds}s time budget",
                    usage=usage,
                    iterations=iteration - 1,
                )
            if usage.model_calls >= constraints.max_model_calls:
                raise ResourceLimitExceeded(
                    "agent exceeded its model-call budget",
                    task_id=task.id,
                    limit=constraints.max_model_calls,
                )

            # Tell the agent where it is in its budget. Without this a model
            # can spend every iteration calling tools and never conclude, which
            # is the most common way a local model wastes a whole task.
            remaining = constraints.max_iterations - iteration
            if remaining == 0:
                budget_note = (
                    "This is your final turn. Do not request any more tools. "
                    "Reply now with your result, stating plainly anything you "
                    "were unable to establish."
                )
            elif remaining <= 2:
                budget_note = (
                    f"You have {remaining} turn(s) left after this one. Start "
                    "drawing your conclusion; request a tool only if you truly "
                    "cannot answer without it."
                )
            else:
                budget_note = (
                    f"You have {remaining} turn(s) left. When you have what you "
                    "need, answer instead of calling another tool."
                )

            built = await self.context.build(
                ContextRequest(
                    execution=run_context.execution,
                    task=task,
                    system=SYSTEM_PROMPT,
                    instructions="\n\n".join(
                        part
                        for part in (
                            agent.instructions,
                            run_context.instructions,
                            skill_guidance,
                            budget_note,
                        )
                        if part
                    ),
                    history=history,
                    # On the last turn the tools are withheld, not just
                    # discouraged: a model that can call one usually will.
                    tools=[] if remaining == 0 else tool_schemas,
                ),
                selection.spec,
            )
            request = built.request
            request.required_capabilities = capabilities

            try:
                response: ModelResponse = await self.router.complete(
                    request,
                    requirements=routing,
                    execution_id=run_context.execution.id,
                    task_id=task.id,
                )
            except ModelError:
                raise
            usage = usage.add(response.usage)

            if not response.wants_tools:
                summary = response.text.strip()
                transcript.append({"role": "assistant", "content": summary[:4000]})
                return TaskResult(
                    task_id=task.id,
                    ok=bool(summary),
                    summary=summary[:2000],
                    output=response.structured if response.structured is not None else summary,
                    confidence=Confidence.LIKELY if summary else Confidence.UNCERTAIN,
                    artifacts=artifacts,
                    evidence=evidence,
                    usage=usage.add(Usage(wall_seconds=time.monotonic() - started)),
                    messages=transcript,
                )

            history.append(
                Message(
                    role="assistant",
                    content=response.text
                    or f"(requesting {len(response.tool_calls)} tool call(s))",
                )
            )
            transcript.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                }
            )

            for call in response.tool_calls:
                if usage.tool_calls >= constraints.max_tool_calls:
                    raise ResourceLimitExceeded(
                        "agent exceeded its tool-call budget",
                        task_id=task.id,
                        limit=constraints.max_tool_calls,
                    )
                observation = await self._invoke_tool(call, tool_context, task)
                usage.tool_calls += 1
                if observation.get("_evidence"):
                    evidence.append(observation.pop("_evidence"))
                history.append(
                    Message(
                        role="user",
                        content=(
                            f"Result of {call.name}:\n"
                            + json.dumps(observation, default=str)[:8000]
                        ),
                    )
                )
                transcript.append(
                    {"role": "tool", "name": call.name, "result": observation}
                )

        return self._failure(
            task,
            f"agent reached its iteration limit of {constraints.max_iterations} "
            "without producing a final answer",
            usage=usage,
            iterations=constraints.max_iterations,
        )

    async def _invoke_tool(
        self, call: Any, tool_context: ToolContext, task: Task
    ) -> dict[str, Any]:
        """Execute one requested tool call, turning refusals into observations."""
        if not self.tools.has(call.name):
            return {
                "ok": False,
                "error": f"tool {call.name} does not exist or is not available to you",
            }
        try:
            result = await self.tools.call(
                ToolCall(tool_id=call.name, arguments=call.arguments or {}),
                tool_context,
            )
        except PermissionDenied as exc:
            if exc.details.get("requires_approval"):
                # Approval is an orchestration decision, not something the agent
                # may route around, so it interrupts the loop.
                raise ApprovalRequired(
                    exc.message, approval_id="", tool_id=call.name, task_id=task.id
                ) from exc
            return {"ok": False, "error": exc.message, "denied": True}

        if not result.ok:
            return {"ok": False, "error": (result.error or {}).get("message", "failed")}

        return {
            "ok": True,
            "output": result.output,
            "_evidence": Evidence(
                source=call.name,
                summary=f"{call.name} returned successfully",
                detail=_truncate(result.output),
            ),
        }


def _truncate(value: Any, limit: int = 2000) -> Any:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        text = str(value)
    if len(text) <= limit:
        return value
    return text[:limit] + f"... [{len(text) - limit} more characters]"


class RuntimeRegistry:
    """Maps an agent's declared runtime to an implementation."""

    def __init__(self) -> None:
        self._runtimes: dict[str, AgentRuntime] = {}

    def register(self, runtime: AgentRuntime) -> AgentRuntime:
        self._runtimes[runtime.name] = runtime
        return runtime

    def get(self, name: str) -> AgentRuntime:
        runtime = self._runtimes.get(name)
        if runtime is None:
            runtime = self._runtimes.get("generic")
        if runtime is None:
            from ..errors import NotFound

            raise NotFound(f"no runtime registered for {name}", name=name)
        return runtime

    def has(self, name: str) -> bool:
        return name in self._runtimes

    def names(self) -> list[str]:
        return sorted(self._runtimes)
