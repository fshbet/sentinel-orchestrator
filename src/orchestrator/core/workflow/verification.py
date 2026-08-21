"""Pre-execution workflow verification.

Before a plan runs, everything it references is checked to exist: agents,
capabilities, tools, validators, dependencies, and a model able to satisfy the
declared requirements (spec section 87).

The point is to fail at the start with a precise reason rather than three tasks
in with a confusing one. Problems are separated into *errors*, which make the
plan unrunnable, and *warnings*, which are recorded and proceeded past — because
a plan referencing a capability that a dynamically created agent will supply is
a normal, working situation, not a defect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ...errors import InvalidWorkflow
from ..domain.models import Plan, Task
from .graph import TaskGraph


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str
    task_id: str | None = None
    fatal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "task_id": self.task_id,
            "fatal": self.fatal,
        }


@dataclass
class VerificationReport:
    issues: list[VerificationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[VerificationIssue]:
        return [i for i in self.issues if i.fatal]

    @property
    def warnings(self) -> list[VerificationIssue]:
        return [i for i in self.issues if not i.fatal]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
        }

    def raise_if_invalid(self, *, execution_id: str = "") -> None:
        if self.ok:
            return
        raise InvalidWorkflow(
            "plan cannot be executed: "
            + "; ".join(i.message for i in self.errors),
            execution_id=execution_id,
            issues=[i.to_dict() for i in self.issues],
        )


class _Registry(Protocol):
    def has(self, key: str) -> bool: ...


@dataclass
class VerificationContext:
    """What the plan is verified against."""

    capabilities: _Registry | None = None
    agents: Any = None
    tools: _Registry | None = None
    validators: _Registry | None = None
    skills: _Registry | None = None
    router: Any = None
    # Whether an unmet capability can be covered by creating a specialist.
    allow_dynamic_agents: bool = True
    granted_permissions: Sequence[str] = ()


def verify(
    plan: Plan,
    context: VerificationContext,
    *,
    existing: Sequence[Task] = (),
) -> VerificationReport:
    """Check a plan against the registries that will have to satisfy it.

    ``existing`` is the execution's current tasks. An iterative or revised plan
    legitimately depends on tasks from an earlier round, so structure is checked
    against the combined graph; the per-task checks apply only to what is new.
    """
    report = VerificationReport()

    known = {task.id: task for task in existing}
    combined = list(known.values()) + [t for t in plan.tasks if t.id not in known]

    # 1. Structure. A cycle or a dangling reference makes the plan unrunnable.
    graph = TaskGraph(combined)
    for issue in graph.issues():
        report.issues.append(
            VerificationIssue(issue.code, issue.message, issue.task_id, fatal=True)
        )
    if report.errors:
        # Later checks assume a walkable graph.
        return report

    for task in plan.tasks:
        _verify_task(task, context, report)

    return report


def _verify_task(
    task: Task, context: VerificationContext, report: VerificationReport
) -> None:
    label = task.name or task.id

    # 2. Capabilities. Unknown ones are a warning when a specialist can be
    # created for them, and an error when it cannot.
    for capability in task.required_capabilities:
        known = context.capabilities is not None and context.capabilities.has(capability)
        provided = context.agents is not None and bool(
            context.agents.providing(capability)
        )
        if known or provided:
            continue
        report.issues.append(
            VerificationIssue(
                "unknown_capability",
                f"task '{label}' requires capability '{capability}', which no"
                " registered agent provides",
                task.id,
                fatal=not context.allow_dynamic_agents,
            )
        )

    # 3. Tools. A task restricted to a tool that does not exist would leave the
    # agent with nothing, so this is fatal unless the entry is a glob.
    if context.tools is not None:
        for tool_id in task.allowed_tools:
            if "*" in tool_id or "?" in tool_id:
                continue
            if not context.tools.has(tool_id):
                report.issues.append(
                    VerificationIssue(
                        "unknown_tool",
                        f"task '{label}' is restricted to tool '{tool_id}',"
                        " which is not registered",
                        task.id,
                    )
                )

    # 4. Validators. A missing one means the gate cannot run, which would let
    # unverified work through.
    if context.validators is not None:
        for spec in task.validations:
            if not context.validators.has(spec.validator):
                report.issues.append(
                    VerificationIssue(
                        "unknown_validator",
                        f"task '{label}' declares validator '{spec.validator}',"
                        " which is not registered",
                        task.id,
                    )
                )

    # 5. Skills. Guidance is not capability, so a missing skill degrades the
    # work rather than preventing it. Say so and continue.
    if context.skills is not None:
        for skill_id in task.metadata.get("skills", []) or []:
            if not context.skills.has(str(skill_id)):
                report.issues.append(
                    VerificationIssue(
                        "unknown_skill",
                        f"task '{label}' asks for skill '{skill_id}', which is not"
                        " registered; it will run without that guidance",
                        task.id,
                        fatal=False,
                    )
                )

    # 6. Models. Requirements nothing can satisfy would fail at dispatch.
    if context.router is not None and task.model_requirements:
        from ...llm.routing import RoutingRequirements

        try:
            satisfiable = bool(
                context.router.candidates(
                    RoutingRequirements(capabilities=list(task.model_requirements))
                )
            )
        except Exception:  # noqa: BLE001 - a router problem is not a plan problem
            satisfiable = True
        if not satisfiable:
            report.issues.append(
                VerificationIssue(
                    "unsatisfiable_model_requirements",
                    f"task '{label}' requires model capabilities "
                    + ", ".join(c.value for c in task.model_requirements)
                    + ", which no registered model provides",
                    task.id,
                )
            )

    # 7. Attempts. A task that may never run is a planning mistake worth naming.
    if task.max_attempts < 1:
        report.issues.append(
            VerificationIssue(
                "unrunnable_task",
                f"task '{label}' allows {task.max_attempts} attempts",
                task.id,
            )
        )
