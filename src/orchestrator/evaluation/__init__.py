"""Regression evaluation for orchestration behaviour.

**What this harness proves, and what it does not.**

It proves that *the platform's deterministic machinery behaves as specified*:
that a given plan shape is produced from a given model response, that a tool
outside a task's grant is refused, that an egress decision comes out the way
the policy says, that a validator reaches the confidence it should, that a
recovery ladder terminates. Every case is a fixed input and a fixed expected
outcome, run against a scripted provider, so a failure means the platform
changed — not that a model had an off day.

It does **not** prove that AI output is correct. It cannot. A scripted
provider tells you nothing about what a real model will say, and no fixed
suite could: the space of things a model might produce is not enumerable, and
"correct" for most objectives is a judgement rather than a comparison. What
this harness gives you is the layer underneath — the guarantee that whatever
the model says, the *handling* of it is unchanged since the last release.

That distinction is the whole point. Security and policy cases are the ones
worth gating a release on, because they are the ones where the platform's
behaviour is fully determined by the platform. Quality cases are reported and
baselined, never gated, because a moving score there is information rather
than a verdict.

Real-model evaluation is possible and deliberately opt-in: it needs a network,
costs money, and is not reproducible, so it must never be a required CI step.
"""

from .cases import CASES, EvaluationCase, Severity, Suite, load_cases
from .runner import (
    SUITE_GROUPS,
    EvaluationReport,
    EvaluationResult,
    resolve_suites,
    run_suite,
)

__all__ = [
    "CASES",
    "EvaluationCase",
    "EvaluationReport",
    "EvaluationResult",
    "Severity",
    "SUITE_GROUPS",
    "Suite",
    "resolve_suites",
    "load_cases",
    "run_suite",
]
