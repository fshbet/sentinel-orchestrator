# Evaluation harness

```bash
orchestrator evaluate                          # every suite
orchestrator evaluate --suite security         # policy + injection + egress
orchestrator evaluate --report ./eval          # writes eval.json and eval.md
orchestrator evaluate --min-quality 0.90       # fail below a quality floor
```

## What it proves

That the platform's **deterministic machinery** behaves as specified. Every
case is a fixed input and a fixed expected outcome, run against scripted model
behaviour, so a failure means the platform changed — not that a model had an
off day:

* plans keep their shape, and dangling dependencies are pruned;
* a tool outside a task's grant is refused;
* an egress decision comes out the way the policy says;
* a validator reaches the confidence it should;
* a recovery ladder terminates.

## What it does not prove

**That AI output is correct.** It cannot, and no fixed suite could. A scripted
provider tells you nothing about what a real model will say, and "correct" for
most objectives is a judgement rather than a comparison.

What this gives you is the layer underneath: the guarantee that *whatever* the
model says, the handling of it is unchanged since the last release. If a
release quietly stopped refusing an ungranted tool, this catches it. If a
model starts producing worse plans, it will not.

## Suites

| Suite | Cases | Gates a release |
|---|---|---|
| `planning` | 4 | only if a case is rated critical |
| `tool_selection` | 3 | only if critical |
| `policy` | 5 | **yes** |
| `injection` | 5 | **yes** |
| `egress` | 7 | **yes** |
| `validation` | 4 | only if critical |
| `recovery` | 3 | only if critical |

Groups: `security` (policy + injection + egress), `quality` (the rest), `all`.

## Gating

Two verdicts, kept apart on purpose:

* **Blocking failures** — any security or policy case, or anything rated
  critical. `orchestrator evaluate` exits 2. These should stop a release.
* **Quality score** — the pass rate over non-gating cases. Reported always;
  gates only against an explicit `--min-quality`.

A single score mixing the two would let a security regression hide behind an
improvement somewhere else.

**Errors count as failures.** A case that could not run did not pass — a
harness reporting 100% because it skipped everything is worse than one
reporting 60%.

## Case schema

```python
EvaluationCase(
    id="egress-004-plain-http-refused",
    version=1,                       # bump when an expectation legitimately changes
    suite=Suite.EGRESS,
    severity=Severity.HIGH,
    description="HTTPS by default; HTTP needs a named development opt-in.",
    egress_url="http://api.example.com/data",
    egress_policy={"allowed_hosts": ["api.example.com"]},
    expect_egress_allowed=False,
)
```

Versioning is per case. When an expectation changes, the version goes up and
the diff shows *which* expectation moved rather than a score sliding by an
unexplained amount.

External cases load from JSON with `load_cases(directory)`.

## Real-model evaluation

Not built, deliberately. It needs a network, costs money, and is not
reproducible, so it must never be a required CI step. The seam is
`run_suite(cases=...)`: supply cases whose `model_script` is empty and a real
provider, and run it outside CI.

## CI

Three steps, in `.github/workflows/ci.yml`:

1. `--suite security` must pass entirely.
2. The full suite with `--min-quality 0.90`, reports uploaded as artifacts.
3. A **canary**: an inverted case is run and the build fails if the harness
   reports it as passing. A suite that cannot fail is indistinguishable from
   one that runs nothing.
