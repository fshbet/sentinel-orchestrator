# ADR-002: Structured dynamic planning with a deterministic fallback

**Status:** accepted

## Context

The platform receives an objective with no workflow. It has to decide what tasks
exist, how they depend on each other, and what would count as done.

A natural-language plan cannot be scheduled, checked for cycles, or resumed. A
fixed plan template cannot be domain-agnostic.

## Decision

The planner emits **structured data**: tasks with keys, dependencies, required
capabilities, and declared validations, produced through a JSON schema
(`planning/decomposition.py`).

Three strategies are supported and selected automatically:

- `FULL` - generate the whole graph up front (simple, well-understood objectives)
- `ITERATIVE` - plan the next step, execute, observe, plan again (uncertainty)
- `ADAPTIVE` - generate a graph and revise it as observations arrive

Every generated plan is validated. Cycles are rejected outright. Dangling
dependency references and unknown capabilities are repaired by dropping them,
because those are transcription errors rather than logical ones.

When no model is configured, or the model returns something unusable, planning
falls back to a deterministic decomposition derived from the extracted
requirements and the chosen pattern.

## Consequences

**Good.** Plans are inspectable, versioned, resumable, and testable. A model that
hallucinates a cycle gets an error rather than a deadlock. The platform still
functions with no provider configured, which makes it testable and usable in
constrained environments.

**Cost.** Schema-constrained generation is more brittle with weak models; the
fallback path exists precisely because of this, and is exercised by the tests.

## Alternatives rejected

- **Natural-language plans** parsed later. Unschedulable and unverifiable.
- **Fixed templates per domain.** Not domain-agnostic.
- **Trusting the model's dependency list.** It is validated instead.
