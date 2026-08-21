# ADR-006: Capability-based routing, with no shipped agent library

**Status:** accepted

## Context

The fastest way to make an orchestration framework domain-specific is to ship
agent roles. `developer`, `tester`, `researcher`, `database-agent` - each encodes
an assumption about what work looks like, and every user inherits the author's
assumptions.

But something has to decide which worker handles which task.

## Decision

Route by **capability**, an opaque string. A task declares what it requires; an
agent advertises what it provides; the orchestrator scores and selects
deterministically (`agents/selection.py`).

The core ships **no capabilities and no agents**. They arrive from configuration,
plugins, or the planner.

Selection prefers the agent covering the requirement with the least excess
privilege. When nothing covers it, a scoped ephemeral specialist is created - but
only when the shortfall is real. If agents advertise the capability and were
rejected on privilege grounds, a near-duplicate would not help, and selection
fails loudly instead.

## Consequences

**Good.** The core is genuinely domain-neutral. New domains are configuration.
Least privilege falls out of the scoring function rather than being bolted on.

**Cost.** A fresh install has no agents, so the first run creates an ephemeral
one. This appears in the audit trail as `agent.created` and is the honest
representation of "nothing was registered".

## Alternatives rejected

- **A shipped role library.** Overfits to one worldview.
- **Model-chosen agent selection.** Non-deterministic, and there is a correct
  deterministic answer.
- **Name-based routing.** The thing capabilities exist to avoid.
