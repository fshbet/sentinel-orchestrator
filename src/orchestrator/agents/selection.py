"""Capability-based agent selection.

Selection is deterministic scoring, not a model call: given the capabilities a
task requires, pick the registered agent that covers them with the least extra
privilege. Only when nothing covers the requirement does the platform consider
creating a specialist, and that decision carries an explicit cost/benefit test
(spec sections 10, 12, 44).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..core.domain.models import AgentSpec, Task
from ..errors import NoCapableAgent
from .capabilities import CapabilityRegistry
from .registry import AgentRegistry


@dataclass
class Selection:
    agent: AgentSpec
    score: float
    rationale: str
    created: bool = False
    missing_capabilities: list[str] = field(default_factory=list)


@dataclass
class SelectionPolicy:
    """Knobs that govern how eagerly specialists are created."""

    allow_dynamic_agents: bool = True
    # A specialist is only worth creating when the shortfall is real: at least
    # this many required capabilities must be unmet by every existing agent.
    min_missing_for_creation: int = 1
    # Reject a candidate that would grant this many unrelated permissions.
    max_excess_permissions: int = 8
    prefer_specialists: bool = True


class AgentSelector:
    def __init__(
        self,
        agents: AgentRegistry,
        capabilities: CapabilityRegistry,
        *,
        policy: SelectionPolicy | None = None,
    ) -> None:
        self.agents = agents
        self.capabilities = capabilities
        self.policy = policy or SelectionPolicy()

    # -- scoring -----------------------------------------------------------

    def score(self, agent: AgentSpec, required: Sequence[str]) -> tuple[float, str]:
        """Higher is better. Returns ``(score, rationale)``."""
        required_set = set(required)
        provided = set(agent.capabilities)
        covered = required_set & provided
        if required_set and not required_set.issubset(provided):
            missing = sorted(required_set - provided)
            return -1.0, f"missing capabilities: {', '.join(missing)}"

        coverage = 1.0 if not required_set else len(covered) / len(required_set)
        # Least privilege: penalise capability and permission surface the task
        # did not ask for.
        excess_capabilities = len(provided - required_set)
        excess_permissions = len(set(agent.permissions))
        if excess_permissions > self.policy.max_excess_permissions:
            return -1.0, (
                f"agent carries {excess_permissions} permissions, over the"
                f" {self.policy.max_excess_permissions} allowed for this selection"
            )

        specialisation = 1.0 / (1.0 + excess_capabilities)
        privilege = 1.0 / (1.0 + excess_permissions)
        weight = 0.25 if self.policy.prefer_specialists else 0.1
        score = coverage * 10.0 + specialisation * weight * 10.0 + privilege * 2.0
        rationale = (
            f"covers {len(covered)}/{len(required_set) or 0} required capabilities,"
            f" {excess_capabilities} extra capabilities,"
            f" {excess_permissions} permissions"
        )
        return score, rationale

    # -- selection ---------------------------------------------------------

    def candidates(self, required: Sequence[str]) -> list[tuple[AgentSpec, float, str]]:
        scored = []
        for agent in self.agents.list():
            score, rationale = self.score(agent, required)
            if score >= 0:
                scored.append((agent, score, rationale))
        scored.sort(key=lambda item: (-item[1], item[0].id))
        return scored

    def select(self, task: Task) -> Selection:
        required = list(task.required_capabilities)
        scored = self.candidates(required)
        if scored:
            agent, score, rationale = scored[0]
            return Selection(agent=agent, score=score, rationale=rationale)

        unmet = self._unmet_capabilities(required)
        if not self.policy.allow_dynamic_agents:
            raise NoCapableAgent(
                "no registered agent provides the required capabilities and dynamic"
                " agent creation is disabled",
                task_id=task.id,
                required=required,
                missing=unmet,
            )
        if required and len(unmet) < self.policy.min_missing_for_creation:
            # Every required capability exists somewhere, so an agent was
            # rejected on privilege grounds rather than on ability. Creating a
            # near-duplicate specialist would not fix that.
            raise NoCapableAgent(
                "agents advertise the required capabilities but none passed the"
                " selection policy",
                task_id=task.id,
                required=required,
                missing=unmet,
            )
        agent = self.create_agent(task, unmet)
        rationale = (
            "no registered agent covered "
            + ", ".join(unmet)
            + "; created a scoped specialist"
            if unmet
            else "no agents are registered; created a worker scoped to this task"
        )
        return Selection(
            agent=agent,
            score=0.0,
            rationale=rationale,
            created=True,
            missing_capabilities=unmet,
        )

    def _unmet_capabilities(self, required: Sequence[str]) -> list[str]:
        provided = self.agents.capabilities()
        return sorted(set(required) - provided)

    # -- dynamic creation --------------------------------------------------

    def create_agent(self, task: Task, missing: Sequence[str]) -> AgentSpec:
        """Create an ephemeral specialist scoped to exactly what the task needs.

        The new agent gets the union of the requirements its capabilities
        declare, plus whatever the task already restricts itself to. It does not
        inherit the orchestrator's privileges (spec section 44).
        """
        required = list(task.required_capabilities)
        requirements = self.capabilities.requirements_for(required)
        tools = sorted(set(requirements.tools) | set(task.allowed_tools))
        agent = AgentSpec(
            id=f"dynamic:{task.name or task.id}",
            description=(f"Ephemeral specialist created for task {task.name or task.id}."),
            capabilities=required,
            tools=tools,
            skills=list(requirements.skills),
            permissions=list(requirements.permissions),
            model_requirements=list(task.model_requirements)
            or list(requirements.model_capabilities),
            ephemeral=True,
            instructions=(
                "You have been created for a single task. Work only within the "
                "tools and permissions you were granted, and report structured "
                "results with evidence."
            ),
            metadata={"created_for_task": task.id, "unmet_capabilities": list(missing)},
        )
        for capability_id in required:
            if not self.capabilities.has(capability_id):
                self.capabilities.declare(capability_id)
        return self.agents.register(agent)
