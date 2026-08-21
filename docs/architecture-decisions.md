# Architecture decision records

Each ADR states the context, the decision, its consequences (including costs),
and what was rejected. The full records are in `docs/adr/`.

| ADR | Decision |
|---|---|
| [001](adr/ADR-001-core-orchestration-model.md) | Two layers: an explicit state machine for the lifecycle, a dynamic DAG for the work |
| [002](adr/ADR-002-dynamic-planning.md) | Structured plan generation with a deterministic fallback |
| [003](adr/ADR-003-mcp-native-client.md) | A native MCP client rather than an SDK dependency |
| [004](adr/ADR-004-state-persistence.md) | SQLite document store with optimistic concurrency |
| [005](adr/ADR-005-durable-execution.md) | Durability by write-ahead state; a backend interface for more |
| [006](adr/ADR-006-capability-routing.md) | Capability-based routing, with no shipped agent library |
| [007](adr/ADR-007-context-engineering.md) | Budgeted, deterministically compacted context |
| [008](adr/ADR-008-evidence-based-completion.md) | Completion requires evidence; uncertainty is reportable |
| [009](adr/ADR-009-security-model.md) | Reasoning, authorisation, execution, and validation are separate |
| [010](adr/ADR-010-adapter-architecture.md) | Adapters for execution, storage, workflow, observability |
| [011](adr/ADR-011-dependency-free-core.md) | The core depends only on the standard library |
| [012](adr/ADR-012-bounded-iteration.md) | Every loop has an enforced bound |

Background: `docs/research/ORCHESTRATION_RESEARCH.md` records what was surveyed,
what was taken from each approach, and what was rejected.

Three decisions are argued in the topic guides rather than in an ADR, because
they follow directly from ADR-006 and ADR-009 rather than standing alone:

- **Skills grant nothing** ([skills.md](skills.md)) — guidance is content, so a
  missing skill degrades the work rather than blocking it, while a missing tool
  does block it.
- **Isolation is refused rather than downgraded** ([security.md](security.md)) —
  a runtime declares what it can enforce, and a task asking for more fails.
- **Server scope and tool scope are separate checks**
  ([security.md](security.md)) — a broad tool glob must not widen which MCP
  servers an agent can reach.
