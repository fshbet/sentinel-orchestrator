# ADR-008: Completion requires evidence, and uncertainty is reportable

**Status:** accepted

## Context

The failure mode that makes agent systems untrustworthy is an agent declaring
success. It is confident, plausible, and frequently wrong.

But not everything can be checked mechanically. A system that only accepts
deterministic proof cannot handle subjective work at all.

## Decision

Validation is independent of the agent (`validation/`). A validator checks
something observable and returns structured **evidence**. Whether a task
succeeded is decided by the gate, never by the agent's claim.

Where no deterministic check exists, the platform says so rather than pretending:

- `NoopValidator` passes but reports `UNCERTAIN`, with evidence marked
  `UNVERIFIED`.
- `ModelJudgeValidator` can reach at most `LIKELY`. A model may not certify
  anything as `CONFIRMED`.
- An execution adapter's self-reported `confirmed` is downgraded to `likely`.
- Gate confidence is the weakest link across its validators.

`can_complete()` is a structural precondition on `COMPLETED`: no failed mandatory
validation, no non-terminal task, no failed task.

## Consequences

**Good.** "Done" means something. The uncertainty ladder
(`CONFIRMED / LIKELY / UNCERTAIN / BLOCKED / FAILED`) lets a caller distinguish
verified work from plausible work, which is the distinction that matters.

**Cost.** More configuration: reaching `CONFIRMED` requires declaring a real
check. Tasks with no available check report `UNCERTAIN` forever, which is correct
but can read as failure to someone expecting a green tick.

## Alternatives rejected

- **Trusting agent self-report.** The problem being solved.
- **Requiring deterministic validation everywhere.** Excludes subjective work.
- **Treating a model judgement as proof.** It is an inference, and is recorded as
  one.
