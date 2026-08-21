# ADR-010: Adapters for execution, storage, workflow, and observability

**Status:** accepted

## Context

The platform must integrate with external agent systems, alternative stores,
future durable engines, and telemetry - without any of them becoming required,
and without their concepts leaking into the core domain model.

## Decision

Four adapter seams, each an interface the core depends on:

- **Execution** (`AgentRuntime`) - the built-in loop, a subprocess worker, an
  OpenHands conversation, anything else. An adapter receives a self-contained
  brief and returns a structured result.
- **Storage** (`StateStore`) - SQLite, in-memory, or anything else.
- **Workflow** (`WorkflowBackend`) - local today, durable engine later.
- **Observability** - audit subscription, plus an OpenTelemetry bridge that
  no-ops entirely when the package is absent.

An adapter never receives orchestration state. It cannot change the plan, mark
itself complete, or grant itself tools. Its self-reported confidence is capped
below `CONFIRMED`.

Plugins register through a narrow `PluginRegistry` rather than the platform
object, and a plugin that fails to load is reported, not fatal.

## Consequences

**Good.** Integration without contamination. There is a test asserting the core
does not reference OpenHands. Users install only what they use.

**Cost.** The lowest-common-denominator brief means an adapter cannot exploit a
host system's richer features without extending the interface.

## Alternatives rejected

- **Direct integration in the core.** Makes the core a client of one system's
  worldview.
- **Adapters receiving the full execution.** Removes the boundary that makes them
  safe.
