# Architecture

## The one idea

Everything else follows from a single division:

```
                  reasoning                    control
        ┌──────────────────────────┐  ┌──────────────────────────┐
        │  understand the goal     │  │  state transitions       │
        │  decompose it            │  │  scheduling              │
        │  do the work             │  │  permissions             │
        │  judge subjective output │  │  retries and recovery    │
        │                          │  │  persistence             │
        │      (a model)           │  │  validation gates        │
        │                          │  │  resource limits         │
        └──────────────────────────┘  │      (software)          │
                                      └──────────────────────────┘
```

A model may *request* anything. Whether it happens is decided by code that does
not consult a model. This is why the platform can make hard promises — a failed
mandatory gate never yields `COMPLETED`, a cancelled execution never continues,
an agent never calls a tool it was not granted — that a model-driven controller
could only make probabilistically.

## Two layers of control

A single control structure cannot express both "what is the lifecycle of a run"
and "what work does this objective imply". So there are two, and they compose.

**The lifecycle** is an explicit state machine (`core/state/machine.py`). It is
small, fixed, and total: every transition is enumerated, and anything not
enumerated raises.

```
CREATED → PLANNING → READY → RUNNING → VALIDATING → REVIEWING → COMPLETED
             ↑         ↑        │           │
             │         └────────┤           │
             └──────────────────┴───────────┘        (re-plan / recover)

  any non-terminal ──→ WAITING   (a human must decide)
  any non-terminal ──→ PAUSING → PAUSED
  any non-terminal ──→ CANCELLING → CANCELLED
  any non-terminal ──→ FAILED
```

**The work** is a dynamic directed acyclic graph of tasks
(`core/workflow/graph.py`), generated per objective. It carries dependencies,
required capabilities, declared validations, and the resources each task
mutates. It is data, not prose, and it is validated structurally before anything
runs.

## Request flow

```
  objective
     │
     ▼
┌─────────────────┐   requirements are structured: explicit / inferred /
│ goal analyser   │   assumed / unknown, each tagged with provenance so an
└────────┬────────┘   assumption is never promoted into a requirement
         ▼
┌─────────────────┐   deterministic complexity scoring picks the pattern and
│ meta-planner    │   the planning strategy. A trivial objective gets one
└────────┬────────┘   agent, not ten.
         ▼
┌─────────────────┐   full | iterative | adaptive. Output is a validated DAG;
│ planner         │   a model that returns a cycle is rejected, and a model
└────────┬────────┘   that returns nothing usable falls back to a
         │            deterministic decomposition.
         ▼
┌─────────────────┐   ready-set computation, concurrency limit, resource
│ scheduler       │   locks. Never asks a model what to run next.
└────────┬────────┘
         ▼
    ┌────┴────┬─────────┐
    ▼         ▼         ▼          per task:
  task      task      task           select agent by capability
    │         │         │            derive least-privilege scope
    ▼         ▼         ▼            run bounded agent loop
┌─────────────────┐                  gate the result
│ validation gate │
└────────┬────────┘
    pass │ fail
         │   └──→ classify → recovery ladder → retry / swap / re-plan / ask
         ▼
┌─────────────────┐   objective-level gate over the success criteria, then a
│ review          │   structural check: every task terminal, no failed
└────────┬────────┘   mandatory validation.
         ▼
     COMPLETED
```

## Subsystems

| Package | Owns | Deliberately does not |
|---|---|---|
| `core/domain` | The vocabulary: Execution, Task, Plan, Evidence, Approval | Reference any domain, provider, or protocol |
| `core/state` | Transitions, persistence, audit, optimistic concurrency | Decide *what* should happen |
| `core/workflow` | Graph mechanics, patterns, versioned definitions | Call a model |
| `core/scheduler` | Dependency-aware dispatch, resource locks | Know what a task means |
| `core/execution` | The engine: phases, gates, recovery, approvals, limits | Know a domain |
| `core/policy` | Risk scoring, allow/deny/approve decisions | Execute anything |
| `planning` | Goal analysis, pattern selection, DAG generation | Own state |
| `agents` | Capability, skill, and agent registries, selection, the agent loop | Own the workflow |
| `llm` | Provider interface, capability routing, fallback | Appear in orchestration logic |
| `tools` | One registry for every tool source, with uniform enforcement | Trust a caller |
| `mcp` | Native client, trust lifecycle, tool bridging, server surface | Be the workflow engine |
| `context` | Budgeting, compaction, memory tiers | Send everything |
| `validation` | Validators, evidence, gates | Ask the agent if it is done |
| `recovery` | Classification, strategy ladders, escalation | Loop forever |
| `adapters` | Execution, storage, workflow, observability backends | Be required |

## The invariants

These are properties of the code, each covered by a test:

1. An illegal state transition raises rather than being coerced.
2. Terminal states have no outgoing edges. Completed work is never redone.
3. A failed mandatory validation cannot produce `COMPLETED`.
4. A cancelled execution cannot silently continue.
5. An agent cannot call, or even see, a tool outside its scope.
6. A discovered MCP tool is not trusted; annotations may only raise risk.
7. Every loop is bounded — retries, replans, optimizer rounds, agent iterations.
8. Execution state survives process death; in-flight work is rewound, finished
   work is not repeated.
9. Secrets never reach a log or the audit trail.
10. A run with no deterministic check available reports `UNCERTAIN`, not
    `CONFIRMED`.

## Dependency posture

The core imports nothing outside the standard library. Everything else is
opt-in: `typer` for the CLI, `fastapi` for the API, `httpx` for HTTP model
providers and HTTP MCP, `PyYAML` for YAML configuration. MCP works without any
of them, because the client is implemented natively.

This is not minimalism for its own sake. A dependency in the core is a
dependency in every deployment, including the constrained ones the platform is
supposed to work in.

## Where to read next

- `docs/orchestration.md` — how a run actually proceeds
- `docs/adr/` — why each major decision went the way it did
- `docs/research/ORCHESTRATION_RESEARCH.md` — what was surveyed and rejected
- `docs/security.md` — the trust boundaries in one place


## Security architecture

Five enforcement points, each in one place so a new call site cannot forget
one:

| Boundary | Module | Enforces |
|---|---|---|
| Inbound HTTP | `api/security.py` | Authentication, body size, rate limit, request id |
| Authorization | `api/identity.py` | Scopes per route; tenant ownership per object |
| Tool authorization | `core/policy/engine.py` | Deny-by-default, explicit grants, risk-based approval |
| Outbound HTTP | `tools/egress.py` | Allowlist, address class, redirect revalidation, method split |
| Process execution | `tools/execpolicy.py` | Executable identity, environment construction, cwd confinement |
| Model egress | `llm/dataflow.py` | Data classification vs provider disposition |

Three structural decisions run through all of them:

**Central, not per-call-site.** Authentication is middleware; the route→scope
map is one table; the data-flow check is on the router, which every provider
request passes through. The alternative — a check at each call site — is a
check somebody forgets on the next one. This is not hypothetical: while
building tenant isolation, four execution-scoped routes were guarded and three
were missed, and every individual test still passed. The fix was a structural
test that enumerates routes rather than listing them.

**Fail closed on the unknown.** An unmapped API route requires `admin`. An
undeclared model provider is unapproved, not approved-for-public. An empty
egress allowlist denies everything. An unrecognised data classification ranks
as most sensitive. A status not in the terminal set is never pruned.

**Config-time over call-time.** A policy permitting a shell interpreter, or
passing `LD_PRELOAD`, is rejected when the orchestrator starts. Finding that
out on first traffic is finding out too late.

### Profiles as declared posture

`config/profiles.py` turns "how careful should this be" into a declaration.
Omitting it yields `production`. A profile fills unset values only; explicit
config always wins, and `orchestrator validate` reports which values came from
where.

One subtlety worth recording: the built-in defaults layer must **not** seed
security-relevant keys. A default written into the raw document is
indistinguishable later from an operator's explicit choice, which made the
production profile unreachable — the posture said deny-by-default while the
engine ran allow-by-default. The regression test goes through the real
`load()` path for exactly that reason.
