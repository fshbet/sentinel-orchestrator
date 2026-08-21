# Validation

## Independent by construction

A validator never asks the agent whether the work is done. It checks something
observable and returns structured **evidence**.

```yaml
validations:
  - validator: command
    config:
      command: ["pytest", "-q"]
      expect_exit: 0
    mandatory: true
```

## Built-in validators

Domain-neutral mechanisms, not domain checks:

| Validator | Checks |
|---|---|
| `non_empty` | Output exists and meets a minimum length |
| `pattern` | A regex is present (or absent, with `must_match: false`) |
| `json_schema` | Output satisfies a JSON Schema |
| `command` | A command exits as expected, optionally matching stdout |
| `artifact_exists` | A named artifact was produced |
| `tool` | Any tool, including an MCP tool, returns an expected value |
| `noop` | Explicitly records that no check was possible |

Domain-specific validators arrive as plugins.

## Evidence

```yaml
evidence:
  type: command
  source: process.run
  location: null
  summary: "pytest -q"
  detail: {exit_code: 0, stdout: "..."}
  confidence: confirmed
  knowledge_status: known
  timestamp: "..."
```

## Confidence

Gate confidence is the **weakest link** across its validators. One `uncertain`
check makes the whole gate uncertain, and passing with known optional failures
never yields `CONFIRMED`.

Deliberate ceilings:

- `noop` passes at `UNCERTAIN`, with evidence marked `unverified`.
- `model_judge` reaches at most `LIKELY`. A model may not certify anything.
- An adapter's self-reported `confirmed` is downgraded to `likely`.

## Gates

Mandatory failures block. Optional failures are recorded and lower confidence. A
validator that raises **fails closed** - a broken check is not a pass.

`can_complete()` is a structural precondition on `COMPLETED`: no failed mandatory
validation, no non-terminal task, no failed task. This is the invariant the
platform exists to protect, and there is a test that has an agent claim success
while a validator disagrees.

## Subjective work

Use `model_judge` with an evaluator model separate from the producer, or the
evaluator-optimizer pattern. Both report as inference, never as fact.
