"""Execution adapters.

An execution adapter is anything that can take one task and come back with a
structured result: the built-in agent loop, an external agent system, a remote
worker, a human queue. The orchestrator hands it an objective, the relevant
context, the tools it is allowed to use, and the completion criteria, and it
returns a ``TaskResult`` (spec sections 47, 48).

Adapters do not get orchestration state. They cannot change the plan, mark
themselves complete, or grant themselves tools.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from ...agents.runtime import AgentRunContext, BaseRuntime
from ...core.domain.models import TaskResult


@dataclass
class AdapterBrief:
    """The self-contained description handed to an external executor."""

    execution_id: str
    task_id: str
    objective: str
    overall_objective: str
    inputs: dict[str, Any]
    expected_outputs: list[str]
    completion_criteria: list[str]
    allowed_tools: list[str]
    permissions: list[str]
    required_capabilities: list[str]
    dependency_results: list[dict[str, Any]]
    workspace: str | None = None
    constraints: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "overall_objective": self.overall_objective,
            "inputs": self.inputs,
            "expected_outputs": self.expected_outputs,
            "completion_criteria": self.completion_criteria,
            "allowed_tools": self.allowed_tools,
            "permissions": self.permissions,
            "required_capabilities": self.required_capabilities,
            "dependency_results": self.dependency_results,
            "workspace": self.workspace,
            "constraints": self.constraints or {},
        }


def build_brief(context: AgentRunContext) -> AdapterBrief:
    execution, task = context.execution, context.task
    dependency_results = []
    for dependency_id in task.dependencies:
        dependency = execution.tasks.get(dependency_id)
        if dependency is None or dependency.result is None:
            continue
        dependency_results.append(
            {
                "task": dependency.name,
                "summary": dependency.result.summary,
                "output": dependency.result.output,
            }
        )
    return AdapterBrief(
        execution_id=execution.id,
        task_id=task.id,
        objective=task.objective,
        overall_objective=execution.objective,
        inputs=dict(task.inputs),
        expected_outputs=list(task.expected_outputs),
        completion_criteria=list(task.completion_criteria)
        or [c.description for c in execution.requirements.success_criteria],
        allowed_tools=list(context.scope.tools),
        permissions=list(context.scope.permissions),
        required_capabilities=list(task.required_capabilities),
        dependency_results=dependency_results,
        workspace=context.workspace,
        constraints={
            "timeout_seconds": context.agent.constraints.timeout_seconds,
            "max_iterations": context.agent.constraints.max_iterations,
            "isolation": context.agent.constraints.isolation.value,
        },
    )


class ExecutionAdapter(BaseRuntime, abc.ABC):
    """Base class for adapters. Subclasses implement ``run``."""

    name = "adapter"

    @abc.abstractmethod
    async def run(self, context: AgentRunContext) -> TaskResult: ...

    @staticmethod
    def parse_result(payload: Any, context: AgentRunContext) -> TaskResult:
        """Turn an adapter's JSON reply into a ``TaskResult``.

        A reply that does not say it succeeded is treated as a failure. An
        adapter has to state success; silence is not consent.
        """
        from ...core.domain.enums import ArtifactType, Confidence
        from ...core.domain.models import Artifact, Evidence, Usage

        task = context.task
        if isinstance(payload, str):
            return TaskResult(
                task_id=task.id,
                ok=bool(payload.strip()),
                summary=payload.strip()[:2000],
                output=payload,
                confidence=Confidence.LIKELY if payload.strip() else Confidence.UNCERTAIN,
            )
        if not isinstance(payload, dict):
            return TaskResult(
                task_id=task.id,
                ok=False,
                summary="adapter returned an unusable payload",
                confidence=Confidence.FAILED,
                error={"payload_type": type(payload).__name__},
            )

        ok = bool(payload.get("ok", payload.get("success", False)))
        artifacts = [
            Artifact(
                type=ArtifactType(entry.get("type", "text")),
                name=str(entry.get("name", "")),
                location=str(entry.get("location", "")),
                content=entry.get("content"),
                produced_by=task.id,
            )
            for entry in payload.get("artifacts", [])
            if isinstance(entry, dict)
        ]
        evidence = [
            Evidence(
                source=str(entry.get("source", "adapter")),
                summary=str(entry.get("summary", ""))[:500],
                detail=entry.get("detail"),
                location=entry.get("location"),
            )
            for entry in payload.get("evidence", [])
            if isinstance(entry, dict)
        ]
        usage_data = payload.get("usage") or {}
        usage = Usage(
            model_calls=int(usage_data.get("model_calls", 0)),
            tool_calls=int(usage_data.get("tool_calls", 0)),
            input_tokens=int(usage_data.get("input_tokens", 0)),
            output_tokens=int(usage_data.get("output_tokens", 0)),
            cost=float(usage_data.get("cost", 0.0)),
        )
        confidence_value = str(payload.get("confidence", "")).lower()
        try:
            confidence = Confidence(confidence_value)
        except ValueError:
            confidence = Confidence.LIKELY if ok else Confidence.FAILED
        # An adapter may not certify its own work as confirmed.
        if confidence is Confidence.CONFIRMED:
            confidence = Confidence.LIKELY

        return TaskResult(
            task_id=task.id,
            ok=ok,
            summary=str(payload.get("summary", ""))[:2000],
            output=payload.get("output"),
            confidence=confidence,
            artifacts=artifacts,
            evidence=evidence,
            usage=usage,
            handoff_to=payload.get("handoff_to"),
            error=payload.get("error"),
        )
