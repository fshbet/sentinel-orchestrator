# ADR-009: Separate reasoning, authorisation, execution, and validation

**Status:** accepted

## Context

An agent that can decide what it is allowed to do is not sandboxed. Prompt
injection, model error, and a genuinely reasonable-looking mistake all lead to
the same place if the model's request is the authorisation.

## Decision

Four separate stages, in order:

1. **Reasoning** - the model *requests* an action.
2. **Authorisation** - the policy engine decides, using deterministic risk
   scoring across independent axes: reversibility, destructiveness, external
   effect, data sensitivity, financial impact, security impact.
3. **Execution** - the tool layer runs the authorised action, with timeouts,
   retry rules, and an idempotency log.
4. **Validation** - a validator judges the result.

Supporting rules:

- Every tool reaches an agent through one registry, whatever its source, so
  enforcement is uniform.
- An agent receives the intersection of its declared privileges and its task's
  needs, and cannot *see* tools outside that scope.
- An explicit deny always beats an allow; more specific rules win.
- A missing permission is a denial, never an approval prompt.
- Anything rated HIGH or above needs a human by default.
- Declared risk may only raise the assessment, never lower it. This is what stops
  an MCP server describing its delete tool as read-only.
- Filesystem and process tools are workspace-confined and allow-listed.
- Secrets are redacted before anything reaches a log or the audit trail.

## Consequences

**Good.** A compromised or mistaken model cannot escalate. The boundary is
testable, and is tested.

**Cost.** More configuration for high-privilege work, and a run needing a
dangerous tool will stop and ask. That is the intended trade.

## Alternatives rejected

- **Model-mediated authorisation.** Not a boundary.
- **Trusting tool self-description.** It raises risk only.
- **A single "is this production" flag.** Risk is multi-dimensional.
