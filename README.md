# Universal AI Orchestration Platform

A domain-agnostic, MCP-first orchestration engine.

**Describe the goal, not the workflow.** Give it an objective and it decides what
the work is, how to decompose it, what capabilities it needs, what can run in
parallel, how results are validated, what to do when something fails, and
whether the objective was actually met.

It contains no assumptions about your domain. No shipped agent roles, no shipped
domain workflows, no hard-coded provider, no hard-coded tooling.

```bash
pip install -e ".[all]"
orchestrator init .
orchestrator run "Accomplish this objective."
```

---

## What makes it different

**The model proposes; software disposes.** State, scheduling, permissions,
retries, persistence, validation, and resource limits are deterministic code. A
model is used for reasoning, planning, doing the work, and judging subjective
output — and for nothing else.

That division is what lets the platform make hard promises instead of
probabilistic ones:

- A failed mandatory validation **cannot** produce `COMPLETED`.
- A cancelled execution **cannot** silently continue.
- An agent **cannot** call, or even see, a tool it was not granted.
- Every loop — retries, re-plans, refinement rounds, agent iterations — has an
  enforced bound.
- A discovered MCP tool is **not** trusted; a server's annotations may only
  raise its assessed risk, never lower it.

**"Done" means something.** Completion requires evidence. Where no deterministic
check exists, the platform reports `UNCERTAIN` rather than pretending. A model
may reach `LIKELY`; it may never certify `CONFIRMED`.

**The core has zero dependencies.** Everything else — CLI, REST API, HTTP model
providers, YAML — is opt-in. MCP over stdio works in a bare install, because the
protocol client is implemented natively.

---

## Security posture

This platform runs tools with arguments a model chose. That single fact drives
the security design, and the controls are enforced in code rather than asked
of the model.

**Declare a deployment profile.** Omitting it means `production`.

```yaml
profile: production   # development | internal-pilot | production
```

| | development | internal-pilot | production |
|---|---|---|---|
| Policy default | allow | **deny** | **deny** |
| Explicit tool grant | no | **yes** | **yes** |
| API authentication | not required | **required** | **required** |
| Plain HTTP egress | allowed | refused | refused |
| Private networks | allowed | allowed | **refused** |
| Cloud metadata | **refused** | **refused** | **refused** |
| Process tools | if configured | refused | refused |

Read before deploying:

* [SECURITY.md](SECURITY.md) — threat model, what is and is not defended
* [docs/security.md](docs/security.md) — every control, with its test
* [docs/production-readiness.md](docs/production-readiness.md) — 108-item
  checklist, each marked complete / incomplete / operator responsibility
* [deployment/runbook.md](deployment/runbook.md) — three deployment shapes
* [docs/configuration.md](docs/configuration.md) — every setting

### Scope of the claim

Supports **controlled production deployment** behind a TLS/auth proxy: either
single-instance on SQLite, or **multi-instance on PostgreSQL**
(`storage.backend: postgres`).

Explicitly **not supported**: multi-instance on SQLite, enforced
container/sandbox/remote isolation, in-process OIDC JWT validation or any
proxy-forwarded identity (bearer tokens are the only identity mechanism),
process execution without an external isolation boundary, and any guarantee of
AI output correctness.

Verified across two replicas in the shipped Compose stack: shared PostgreSQL
state, distributed token revocation, distributed rate limiting, enforced
outbound egress through a proxy, and a TLS boundary that strips client-supplied
identity headers. The platform provides evidence-based validation,
confidence reporting, an adversarial reviewer, human approval gates, and a
deterministic regression harness — none of which make model output correct;
they make the basis for trusting it inspectable.

```bash
python -m pytest tests/ -q        # 699 with PostgreSQL
orchestrator validate             # effective posture and migration notices
orchestrator evaluate             # 31 deterministic regression cases
orchestrator migrate              # schema status
```


## A run, end to end

```
objective
   → goal analysis        structured requirements, provenance-tagged
   → pattern selection    deterministic complexity scoring
   → planning             validated task DAG (or a deterministic fallback)
   → scheduling           dependency-aware, parallel, resource-locked
   → per task             capability-routed agent, least-privilege scope,
                          bounded loop, independent validation gate
   → recovery             classify → strategy ladder → retry/swap/re-plan/ask
   → objective gate       success criteria, checked
   → review               structural precondition on completion
   → COMPLETED
```

Any phase can stop and wait for a human, pause, or cancel — and resume from
persisted state afterwards, including after the process dies.

```bash
orchestrator inspect <id>    # the task graph, validations, failures
orchestrator audit <id>      # every decision, in order
```

---

## Orchestration patterns

Composable graph fragments, not modes. A parallel fan-out whose merge step is an
evaluator-optimizer loop is just a graph.

`single agent` · `sequential` · `parallel` · `router` · `orchestrator-worker` ·
`evaluator-optimizer` · `hierarchical` · `handoff` · `dynamic DAG`

A router materialises every branch and prunes the ones it did not choose. A task
may hand its remaining work to a different specialist, bounded so peers cannot
pass it in a circle. And a task can be an entire orchestrated run of its own,
with its budget carved out of its parent's rather than added to it.

The platform picks one from the objective's structure. A trivial objective gets
one agent and one check — not ten agents and four reviewers. Override with
`--pattern` when you want to.

---

## Capabilities, not roles

Nothing routes by name. A task declares what it *requires*; an agent advertises
what it *provides*; selection is deterministic scoring that prefers the least
excess privilege. When nothing covers a requirement, a scoped ephemeral
specialist is created — and when the shortfall is not real, selection fails
loudly instead.

The core ships no capabilities, agents, or skills. They come from configuration,
plugins, or the planner. That is what keeps it domain-neutral.

**Skills** are the third piece: reusable knowledge, as content rather than code.
A skill tells an agent how to approach work it was already authorised to do, and
grants it nothing.

---

## MCP

First-class in both directions, implemented natively over JSON-RPC 2.0 with no
SDK dependency. Protocol versions are negotiated, not assumed.

**As a client:** servers are discovered, described, policy-checked, authorised,
and only then registered — so an agent calls an MCP tool exactly the way it calls
anything else.

```bash
orchestrator mcp    # health, negotiated version, registered tools, refusals
```

**As a server:** `orchestrator mcp-serve` exposes orchestration to other agent
systems. Control operations stay behind `--allow-control`.

---

## Documentation

| | |
|---|---|
| [docs/how-it-works.html](docs/how-it-works.html) | Plain-language guide. Start here if you are not going to read the code |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The design, the invariants, the subsystem map |
| [ORCHESTRATION_GUIDE.md](ORCHESTRATION_GUIDE.md) | How to actually drive it |
| [MCP_GUIDE.md](MCP_GUIDE.md) | MCP in both directions |
| [EXTENDING.md](EXTENDING.md) | Every extension seam |
| [docs/production-readiness.md](docs/production-readiness.md) | What is proven, what is not, and how each claim was checked |
| [docs/security.md](docs/security.md) | The controls, and what each one does not cover |
| [deployment/runbook.md](deployment/runbook.md) | Running it: the three supported shapes |
| [docs/](docs/) | 29 topic guides |
| [docs/adr/](docs/adr/) | 12 architecture decision records |
| [docs/research/](docs/research/ORCHESTRATION_RESEARCH.md) | What was evaluated and rejected |

---

## Status

782 tests. 40 of them need a reachable PostgreSQL instance and skip without
one; CI supplies one and fails the build if they skip there, because a gated
suite that quietly skips proves nothing. A handful more are POSIX-only (file
modes, symlinks) and skip on Windows. Every subsystem is exercised against real
stores, a real MCP server subprocess, and deliberate failure injection.

`ruff`, `ruff format` and `mypy` are clean across the whole tree, and the
whole-tree type check gates the build rather than being reported and ignored.

The multi-replica controls — shared token revocation, shared rate limiting,
egress enforcement, and the TLS boundary — are verified against the running
production Compose stack by
[deployment/smoke-test.sh](deployment/smoke-test.sh), not only in unit tests.

Known limitations are recorded honestly in
[docs/production-readiness.md](docs/production-readiness.md) and
[docs/research/ORCHESTRATION_RESEARCH.md](docs/research/ORCHESTRATION_RESEARCH.md#7-known-limitations)
rather than left to be discovered.

Apache-2.0.
