"""Context assembly.

Nothing in the platform hands a model "everything we know". A caller declares
what is relevant, the manager ranks it, budgets it against the chosen model's
window, compacts what does not fit, and only then builds the request (spec
sections 22, 23).

Large values are replaced by artifact and state *references* rather than
inlined, so a 40MB result does not have to pass through a prompt to be usable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.domain.enums import ContextKind, MemoryTier
from ..core.domain.models import Artifact, Execution, ModelSpec, Task
from ..llm.base import CompletionRequest, Message
from ..observability.audit import AuditLog, EventType
from ..validation.validators import describe_expectation
from .budget import ContextBudget, estimate_tokens
from .compaction import (
    CompactionReport,
    ContextItem,
    Summariser,
    compact,
    compact_with_summariser,
)
from .memory import MemoryStore

# Values longer than this are referenced instead of inlined.
INLINE_LIMIT_CHARS = 4000


@dataclass
class ContextRequest:
    """What a caller wants in front of the model for one call."""

    execution: Execution
    task: Task | None = None
    system: str = ""
    instructions: str = ""
    history: list[Message] = field(default_factory=list)
    extra_items: list[ContextItem] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    response_schema: dict[str, Any] | None = None
    include_memory: bool = True
    include_dependency_results: bool = True
    memory_query: str = ""


@dataclass
class BuiltContext:
    request: CompletionRequest
    items: list[ContextItem]
    report: CompactionReport
    estimated_tokens: int
    budget: ContextBudget


class ContextManager:
    def __init__(
        self,
        *,
        memory: MemoryStore | None = None,
        audit: AuditLog | None = None,
        summariser: Summariser | None = None,
    ) -> None:
        self.memory = memory or MemoryStore()
        self.audit = audit
        self.summariser = summariser

    # -- assembly ----------------------------------------------------------

    def collect(self, request: ContextRequest) -> list[ContextItem]:
        execution = request.execution
        task = request.task
        items: list[ContextItem] = []

        items.append(
            ContextItem(
                kind=ContextKind.OBJECTIVE,
                label="Overall objective",
                content=execution.objective,
                relevance=1.0,
                pinned=True,
            )
        )

        requirements = execution.requirements
        if requirements.constraints:
            items.append(
                ContextItem(
                    kind=ContextKind.CONSTRAINT,
                    label="Constraints",
                    content="\n".join(f"- {c}" for c in requirements.constraints),
                    relevance=0.95,
                    pinned=True,
                )
            )
        if requirements.assumptions:
            items.append(
                ContextItem(
                    kind=ContextKind.CONSTRAINT,
                    label="Assumptions (not confirmed requirements)",
                    content="\n".join(f"- {a}" for a in requirements.assumptions),
                    relevance=0.6,
                )
            )
        if requirements.success_criteria:
            items.append(
                ContextItem(
                    kind=ContextKind.CONSTRAINT,
                    label="Success criteria",
                    content="\n".join(
                        f"- {c.description}" for c in requirements.success_criteria
                    ),
                    relevance=0.9,
                    pinned=True,
                )
            )

        if task is not None:
            items.append(
                ContextItem(
                    kind=ContextKind.TASK,
                    label="Your task",
                    content=self._render_task(task),
                    relevance=1.0,
                    pinned=True,
                )
            )
            if request.include_dependency_results:
                items.extend(self._dependency_items(execution, task))

        if execution.artifacts:
            items.append(
                ContextItem(
                    kind=ContextKind.ARTIFACT_REF,
                    label="Artifacts produced so far",
                    content="\n".join(
                        f"- {a.name} ({a.type.value}) -> {a.location or a.id}"
                        for a in execution.artifacts[-20:]
                    ),
                    relevance=0.55,
                )
            )

        if request.include_memory:
            query = request.memory_query or (
                task.objective if task is not None else execution.objective
            )
            recalled = self.memory.recall(query, execution_id=execution.id, limit=6)
            if recalled:
                items.append(
                    ContextItem(
                        kind=ContextKind.MEMORY,
                        label="Relevant memory",
                        content="\n".join(
                            f"- {e.key}: {self._inline(e.value)}" for e in recalled
                        ),
                        relevance=0.5,
                    )
                )

        for index, message in enumerate(request.history):
            items.append(
                ContextItem(
                    kind=ContextKind.HISTORY,
                    label=f"{message.role} turn {index + 1}",
                    content=message.content,
                    # Recent turns matter more than old ones.
                    relevance=0.3 + 0.3 * (index + 1) / max(1, len(request.history)),
                    metadata={"role": message.role, "index": index},
                )
            )

        items.extend(request.extra_items)
        return items

    def _render_task(self, task: Task) -> str:
        lines = [f"Objective: {task.objective}"]
        if task.expected_outputs:
            lines.append("Expected outputs: " + ", ".join(task.expected_outputs))
        if task.completion_criteria:
            lines.append(
                "Completion criteria:\n"
                + "\n".join(f"- {c}" for c in task.completion_criteria)
            )
        if task.inputs:
            lines.append("Inputs: " + self._inline(task.inputs))
        if task.validations:
            # The name *and* the requirement. Telling a worker only that a
            # schema check exists, without showing it the schema, guarantees it
            # cannot pass — the check and the instruction have to travel
            # together or they drift apart.
            expectations = [
                text for text in (describe_expectation(v) for v in task.validations) if text
            ]
            if expectations:
                lines.append(
                    "How this will be checked:\n"
                    + "\n".join(f"- {text}" for text in expectations)
                )
        return "\n".join(lines)

    def _dependency_items(self, execution: Execution, task: Task) -> list[ContextItem]:
        items: list[ContextItem] = []
        for dependency_id in task.dependencies:
            dependency = execution.tasks.get(dependency_id)
            if dependency is None or dependency.result is None:
                continue
            items.append(
                ContextItem(
                    kind=ContextKind.RESULT,
                    label=f"Result of upstream task '{dependency.name}'",
                    content=(
                        f"{dependency.result.summary}\n"
                        f"{self._inline(dependency.result.output)}"
                    ),
                    relevance=0.85,
                    ref=dependency.id,
                )
            )
        return items

    @staticmethod
    def _inline(value: Any) -> str:
        """Inline small values; reference large ones."""
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            import json

            try:
                text = json.dumps(value, default=str)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                text = str(value)
        if len(text) <= INLINE_LIMIT_CHARS:
            return text
        return (
            text[:INLINE_LIMIT_CHARS]
            + f"\n[... {len(text) - INLINE_LIMIT_CHARS} more characters; "
            "retrieve the full value from the referenced artifact or task result ...]"
        )

    # -- budgeting ---------------------------------------------------------

    async def build(
        self,
        request: ContextRequest,
        model: ModelSpec,
        *,
        reserved_output: int | None = None,
    ) -> BuiltContext:
        """Assemble, compact to fit the model, and return a ready request."""
        budget = ContextBudget.for_model(model, reserved_output=reserved_output)
        items = self.collect(request)

        allocation = budget.split()
        tool_tokens = estimate_tokens(_ToolsOnly(request.tools))
        available = budget.total_input - tool_tokens - allocation["system"] // 2
        available = max(256, available)

        if self.summariser is not None:
            items, report = await compact_with_summariser(items, available, self.summariser)
        else:
            items, report = compact(items, available)

        if report.changed and self.audit is not None:
            self.audit.record(
                EventType.CONTEXT_COMPACTED,
                execution_id=request.execution.id,
                task_id=request.task.id if request.task else None,
                model=model.id,
                **report.to_dict(),
            )

        system_parts = [request.system or "", request.instructions or ""]
        system = "\n\n".join(part for part in system_parts if part).strip()

        body = "\n\n".join(item.render() for item in items if item.content)
        completion = CompletionRequest(
            messages=[Message(role="user", content=body)],
            system=system,
            tools=list(request.tools),
            response_schema=request.response_schema,
            max_output_tokens=budget.reserved_output,
        )
        estimated = estimate_tokens(completion)
        return BuiltContext(
            request=completion,
            items=items,
            report=report,
            estimated_tokens=estimated,
            budget=budget,
        )

    # -- memory helpers ----------------------------------------------------

    def remember_result(self, execution: Execution, task: Task) -> None:
        """Persist a compact trace of a finished task into working memory."""
        if task.result is None:
            return
        self.memory.remember(
            key=f"task:{task.name or task.id}",
            value=task.result.summary or self._inline(task.result.output),
            summary=task.result.summary,
            tags=[task.name, *task.required_capabilities],
            execution_id=execution.id,
            task_id=task.id,
            tier=MemoryTier.WORKING,
        )

    def remember_artifact(self, execution: Execution, artifact: Artifact) -> None:
        self.memory.remember(
            key=f"artifact:{artifact.name}",
            value=artifact.location or artifact.id,
            summary=f"{artifact.type.value} artifact {artifact.name}",
            tags=["artifact", artifact.type.value],
            execution_id=execution.id,
            tier=MemoryTier.WORKING,
        )


@dataclass
class _ToolsOnly:
    """Shim so tool schemas can be measured with the shared estimator."""

    tools: Sequence[dict[str, Any]]
    messages: tuple = ()
    system: str = ""
    response_schema: None = None
