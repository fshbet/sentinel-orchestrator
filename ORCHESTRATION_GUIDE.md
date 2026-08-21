# Orchestration guide

How to actually drive the platform, and how to read what it tells you.

---

## 1. The mental model

You give it an **objective**. It does not need a workflow, a task list, an agent
roster, or a tool selection — it derives all of that. What you *do* need to give
it, if you want a strong result, is:

- **a model provider** (otherwise the work cannot be done, only planned), and
- **success criteria it can actually check** (otherwise the answer will be
  honest but uncertain).

Everything else is optional.

---

## 2. Writing an objective

The platform reads **structure**, not subject matter, to decide how much
machinery the objective warrants. That has practical consequences.

**A single question gets a single agent:**

```
What does the configuration in ./deploy actually enable?
```

**Enumeration produces a graph:**

```
Compare the three approaches in ./notes against our constraints:
- what each one assumes
- what each one costs to operate
- what each one rules out later
Then recommend one, and say what evidence would change the recommendation.
```

**Expressed uncertainty selects iterative planning** — plan a step, look at what
came back, plan the next:

```
Figure out why the nightly job is unreliable, and fix it.
```

**Iteration language selects a refinement loop:**

```
Draft the summary, then keep improving it until someone with no background
could follow it.
```

If the platform under-plans, the objective was under-specified. Enumerate the
parts, or force the shape:

```bash
orchestrator run "..." --pattern dynamic_dag --strategy adaptive
```

An override is honoured exactly. The platform does not second-guess you.

---

## 3. Reading the result

Two fields, and you need both.

```python
execution.status       # where the run ended
execution.confidence   # how much to believe it
```

| Status | Meaning |
|---|---|
| `completed` | Every mandatory gate passed and every task is terminal |
| `waiting` | A human must decide something |
| `paused` | You asked it to stop; resume when ready |
| `cancelled` | Stopped deliberately; will not continue |
| `failed` | Did not achieve the objective, and recovery is exhausted |

| Confidence | Meaning |
|---|---|
| `confirmed` | Checked deterministically |
| `likely` | Passed, but judgement was involved |
| `uncertain` | Passed, but something could not be verified |
| `blocked` | Stopped on something external |
| `failed` | Did not achieve it |

**`completed` + `uncertain` is a real signal, not a bug.** The work was done and
the platform could not prove it. If you want `confirmed`, give the success
criteria a validator that can actually run.

---

## 4. Making completion mean something

This is the highest-leverage configuration in the system.

```yaml
# A criterion with no validator can only ever reach "uncertain".
success_criteria:
  - description: "The report exists"
    validator: artifact_exists
    validator_config: {name: report}

  - description: "The checks pass"
    validator: command
    validator_config:
      command: ["make", "verify"]
      expect_exit: 0
```

Built-in, domain-neutral validators: `non_empty`, `pattern`, `json_schema`,
`command`, `artifact_exists`, `tool` (including any MCP tool), `noop`.

For subjective work, use a **separate** evaluator model rather than asking the
producer to grade itself. Either `model_judge`, or the evaluator-optimizer
pattern. Both are recorded as inference, never as fact — which is the point.

Write your own for anything domain-specific; see [EXTENDING.md](EXTENDING.md).

---

## 5. When it stops and asks

By default anything the risk engine rates HIGH or above waits for a human.

```bash
orchestrator status <id>                        # shows the pending approval
orchestrator approve <id> <approval-id>         # proceed
orchestrator approve <id> <approval-id> --reject  # skip the task
orchestrator approve <id> <approval-id> --response "use the staging endpoint"
```

A free-text `--response` is passed to the task as guidance, which is the way to
unblock a run that stalled for want of a decision rather than a permission.

From code:

```python
if execution.status is ExecutionStatus.WAITING:
    approval = execution.pending_approval()
    print(approval.prompt, approval.risk.value)
    execution = await platform.approve(execution.id, approval.id, approved=True)
```

Tune the threshold in `policy.approval_threshold`, or target specific tools with
a policy rule. Raising it is a deliberate act; the assessment is usually right.

---

## 6. Parallelism and safety

Independent tasks run concurrently, bounded by `limits.max_parallel_tasks`.

When two tasks touch the same thing, say so and the scheduler serialises them:

```yaml
resources: ["shared-database"]
```

Locks are acquired in sorted order, which makes deadlock between two tasks
holding half of each other's resources structurally impossible.

---

## 7. Control

```bash
orchestrator pause <id>       # stop at the next safe point, persist
orchestrator resume <id>      # continue from persisted state
orchestrator cancel <id>      # stop gracefully; cannot silently continue
```

All three survive process death. After a crash, in-flight tasks are rewound and
finished work is not repeated:

```python
from orchestrator.adapters.workflow.base import LocalWorkflowBackend

for execution in await LocalWorkflowBackend(platform.store).recover_interrupted():
    await platform.engine.run(execution.id)
```

---

## 8. Budgets

Enforced by counting, before the spend, never by asking a model to be careful:

```yaml
limits:
  max_wall_seconds: 3600
  max_model_calls: 300
  max_tool_calls: 1000
  max_tokens: 2000000
  max_cost: 5.00          # needs cost declared on your models
  max_parallel_tasks: 4
  max_task_attempts: 3
  max_replans: 3
  max_optimizer_iterations: 3
```

Hitting a limit fails the execution with `limit.exceeded` in the audit trail
naming which budget. That is deliberate: a run that quietly costs ten times what
you expected is worse than one that stops.

---

## 9. When something goes wrong

```bash
orchestrator inspect <id>    # failures, categories, chosen recovery
orchestrator audit <id>      # every decision in order
```

Recovery classifies the failure, then climbs a ladder specific to its category —
and every ladder ends at a human or a safe stop:

```
transient   retry → retry → alternate model → ask
tool        modify parameters → alternate tool → re-plan → ask
model       alternate model → modify parameters → alternate agent → ask
context     reduce scope → alternate model → re-plan → ask
permission  ask → reduce scope → terminate
validation  modify parameters → alternate agent → re-plan → ask
```

Two of these are deliberate and worth knowing:

- **Permission failures escalate first.** Silently working around a refusal is
  exactly what a policy engine exists to prevent.
- **Context overflow reduces scope rather than retrying.** Re-sending the same
  oversized prompt cannot work.

To make a run fail rather than wait, set
`recovery.allow_human_escalation: false`.

---

## 10. Common situations

**"It planned one task for something complicated."** Complexity is read from
structure. Enumerate the parts, or pass `--pattern dynamic_dag`.

**"The plan says (deterministic decomposition)."** The model was not used for
planning — no provider, or an unusable response. Check
`orchestrator health --json`.

**"An agent could not use the tool it needed."** Check
`orchestrator audit <id> --json | jq '.[] | select(.type=="tool.denied")'`. The
reason distinguishes the three causes: outside the agent's scope, missing a
permission, or refused by policy.

**"An MCP tool did not appear."** `orchestrator mcp` lists refusals with reasons.
Usually the tool's assessed risk exceeded the server policy's `max_risk`.

**"Everything failed after one model outage."** Expected while a cool-off holds.
Cool-offs clear automatically when nothing else can run, and always within five
minutes.

More in [docs/troubleshooting.md](docs/troubleshooting.md).

---

## 11. Where to go next

- [docs/orchestration.md](docs/orchestration.md) — phases in detail
- [docs/planning.md](docs/planning.md) — goal analysis and decomposition
- [docs/validation.md](docs/validation.md) — evidence and gates
- [docs/recovery.md](docs/recovery.md) — the full strategy ladders
- [docs/security.md](docs/security.md) — trust boundaries in one place
- [MCP_GUIDE.md](MCP_GUIDE.md) — connecting and exposing MCP
- [EXTENDING.md](EXTENDING.md) — adding tools, validators, adapters, providers
