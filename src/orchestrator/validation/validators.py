"""Validators.

A validator is independent of the agent whose work it judges. It never asks the
agent whether the work is done; it checks something observable and returns
structured evidence (spec sections 33, 34).

The built-ins here are domain-neutral mechanisms - run a command, check a
schema, match a pattern, call a tool, ask a separate model - not domain checks.
Domain-specific validators arrive as plugins.
"""

from __future__ import annotations

import abc
import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.domain.enums import Confidence, EvidenceType, KnowledgeStatus
from ..core.domain.jsonio import coerce_json
from ..core.domain.models import (
    Evidence,
    Execution,
    Task,
    ToolCall,
    ValidationResult,
    ValidationSpec,
)
from ..errors import ConfigurationError, NotFound
from ..tools.registry import ToolContext, ToolRegistry


@dataclass
class ValidationContext:
    """What a validator is allowed to see."""

    execution: Execution
    task: Task | None = None
    tools: ToolRegistry | None = None
    tool_context: ToolContext | None = None
    workspace: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def target_id(self) -> str:
        return self.task.id if self.task is not None else self.execution.id

    def output(self) -> Any:
        """The thing under test.

        For a task that is its own output. For the execution as a whole it is
        the accumulated output of the tasks that succeeded, because there is no
        single "the answer" field an agent could have filled in.
        """
        if self.task is not None:
            return self.task.result.output if self.task.result is not None else None
        outputs = [
            task.result.output
            for task in self.execution.tasks.values()
            if task.result is not None and task.result.ok
        ]
        if not outputs:
            return self.execution.summary or None
        return outputs[0] if len(outputs) == 1 else outputs

    def output_text(self) -> str:
        value = self.output()
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(value)


class Validator(abc.ABC):
    """Produces a pass/fail judgement backed by evidence."""

    name: str = "validator"

    @abc.abstractmethod
    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult: ...

    def _result(
        self,
        spec: ValidationSpec,
        context: ValidationContext,
        *,
        passed: bool,
        message: str,
        evidence: list[Evidence] | None = None,
        confidence: Confidence | None = None,
    ) -> ValidationResult:
        if confidence is None:
            confidence = Confidence.CONFIRMED if passed else Confidence.FAILED
        return ValidationResult(
            spec_id=spec.id,
            validator=self.name,
            target_id=context.target_id(),
            passed=passed,
            confidence=confidence,
            message=message,
            evidence=evidence or [],
            mandatory=spec.mandatory,
        )


# --------------------------------------------------------------------------
# Deterministic validators
# --------------------------------------------------------------------------


class CommandValidator(Validator):
    """Run a command through the tool registry and check its exit code.

    Config: ``tool`` (default ``process.run``), ``command``, ``expect_exit``,
    ``expect_stdout`` (regex, optional).
    """

    name = "command"

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        if context.tools is None or context.tool_context is None:
            return self._result(
                spec,
                context,
                passed=False,
                message="no tool registry available to run the validation command",
                confidence=Confidence.BLOCKED,
            )
        tool_id = spec.config.get("tool", "process.run")
        command = spec.config.get("command")
        if not command:
            raise ConfigurationError("command validator requires a command")

        result = await context.tools.call(
            ToolCall(tool_id=tool_id, arguments={"command": command}),
            context.tool_context,
        )
        if not result.ok:
            return self._result(
                spec,
                context,
                passed=False,
                message=f"command could not be run: {(result.error or {}).get('message')}",
                evidence=[
                    Evidence(
                        type=EvidenceType.COMMAND,
                        source=tool_id,
                        summary=str(command),
                        detail=result.error,
                        confidence=Confidence.FAILED,
                    )
                ],
                confidence=Confidence.BLOCKED,
            )

        output = result.output or {}
        exit_code = output.get("exit_code")
        expected = spec.config.get("expect_exit", 0)
        passed = exit_code == expected
        pattern = spec.config.get("expect_stdout")
        if passed and pattern:
            passed = bool(re.search(str(pattern), str(output.get("stdout", ""))))

        return self._result(
            spec,
            context,
            passed=passed,
            message=(
                f"command exited {exit_code} (expected {expected})"
                if passed
                else f"command exited {exit_code}, expected {expected}"
            ),
            evidence=[
                Evidence(
                    type=EvidenceType.COMMAND,
                    source=tool_id,
                    summary=str(command),
                    detail={
                        "exit_code": exit_code,
                        "stdout": str(output.get("stdout", ""))[:4000],
                        "stderr": str(output.get("stderr", ""))[:4000],
                    },
                    confidence=Confidence.CONFIRMED,
                )
            ],
        )


class SchemaValidator(Validator):
    """Check the task output against a JSON Schema.

    Uses ``jsonschema`` when installed and falls back to a structural check of
    type, required keys, and enum membership otherwise.
    """

    name = "json_schema"

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        schema = spec.config.get("schema")
        if not schema:
            raise ConfigurationError("json_schema validator requires a schema")
        # Models routinely fence JSON or precede it with a sentence. The JSON
        # is genuinely present, so locate it rather than failing work that is
        # actually correct on a formatting technicality.
        value = coerce_json(context.output())

        errors = _validate_schema(value, schema)
        passed = not errors
        return self._result(
            spec,
            context,
            passed=passed,
            message="output matches the schema" if passed else "; ".join(errors[:5]),
            evidence=[
                Evidence(
                    type=EvidenceType.SCHEMA,
                    source="json_schema",
                    summary=("schema satisfied" if passed else "schema violations"),
                    detail={"errors": errors[:20]},
                    confidence=Confidence.CONFIRMED,
                )
            ],
        )


def _validate_schema(value: Any, schema: dict[str, Any]) -> list[str]:
    try:
        import jsonschema

        validator = jsonschema.Draft202012Validator(schema)
        return [
            f"{'/'.join(str(p) for p in error.path) or '<root>'}: {error.message}"
            for error in validator.iter_errors(value)
        ]
    except ImportError:
        return _structural_check(value, schema, "<root>")


def _structural_check(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    """Dependency-free subset of JSON Schema: type, required, enum, properties."""
    errors: list[str] = []
    expected = schema.get("type")
    types: dict[str, type | tuple[type, ...]] = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
    }
    if expected in types and not isinstance(value, types[expected]):
        errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if expected == "object" and isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key}")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in value and isinstance(subschema, dict):
                errors.extend(_structural_check(value[key], subschema, f"{path}/{key}"))
    if expected == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_structural_check(item, item_schema, f"{path}/{index}"))
    return errors


class PatternValidator(Validator):
    """Require (or forbid) a regular expression in the output.

    Config: ``pattern``, ``must_match`` (default True), ``flags``.
    """

    name = "pattern"

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        pattern = spec.config.get("pattern")
        if not pattern:
            raise ConfigurationError("pattern validator requires a pattern")
        must_match = bool(spec.config.get("must_match", True))
        flags = re.IGNORECASE if spec.config.get("ignore_case") else 0
        text = spec.config.get("text") or context.output_text()
        found = re.search(str(pattern), text, flags) is not None
        passed = found is must_match
        return self._result(
            spec,
            context,
            passed=passed,
            message=(
                f"pattern {'found' if found else 'not found'}"
                f" ({'required' if must_match else 'forbidden'})"
            ),
            evidence=[
                Evidence(
                    type=EvidenceType.DETERMINISTIC_CHECK,
                    source="pattern",
                    summary=str(pattern),
                    detail={"found": found, "sample": text[:1000]},
                )
            ],
        )


class NonEmptyValidator(Validator):
    """The weakest useful check: the task actually produced something.

    Passing establishes that output exists, and nothing whatsoever about
    whether it is correct - so it reports LIKELY, not CONFIRMED. Reporting
    CONFIRMED here meant a run whose only evidence was "the model said
    something" was presented with the platform's highest certainty, which is
    the exact claim this project exists not to make. A run that needs
    CONFIRMED needs a check that can actually fail on wrong work.
    """

    name = "non_empty"

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        text = context.output_text().strip()
        minimum = int(spec.config.get("min_length", 1))
        passed = len(text) >= minimum
        return self._result(
            spec,
            context,
            passed=passed,
            confidence=Confidence.LIKELY if passed else Confidence.FAILED,
            message=(
                f"output is {len(text)} characters (minimum {minimum})"
                if passed
                else f"output is empty or shorter than {minimum} characters"
            ),
            evidence=[
                Evidence(
                    type=EvidenceType.DETERMINISTIC_CHECK,
                    source="non_empty",
                    summary=f"{len(text)} characters",
                )
            ],
        )


class ArtifactExistsValidator(Validator):
    """Check that a named artifact was produced *and* durably stored.

    An in-memory record is not a deliverable. In the production deployment the
    root filesystem is read-only and the fallback artifact path was unwritable,
    so runs completed successfully while the file the user asked for existed
    nowhere - the record said otherwise and this check believed it. It now
    verifies the bytes.

    Config:
      ``name``            optional; which artifact to look for.
      ``verify_content``  default true; check the stored file. Set false only
                          where the store is not reachable from the validating
                          process, and understand that this weakens the check
                          back to a record lookup.
    """

    name = "artifact_exists"

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        import hashlib

        name = str(spec.config.get("name", ""))
        verify = bool(spec.config.get("verify_content", True))

        artifacts = context.execution.artifacts
        if context.task is not None and context.task.result is not None:
            artifacts = artifacts + context.task.result.artifacts
        match = next((a for a in artifacts if not name or a.name == name), None)

        if match is None:
            return self._result(
                spec,
                context,
                passed=False,
                message=f"no artifact named {name or '<any>'} was produced",
                evidence=[
                    Evidence(
                        type=EvidenceType.FILE,
                        source="artifact_registry",
                        summary=f"missing artifact {name or '<any>'}",
                    )
                ],
            )

        # A reference-only artifact carries no content, so there is nothing on
        # disk to check and its record is the whole of it.
        has_content = match.content is not None and match.content != ""
        if not verify or not has_content:
            return self._result(
                spec,
                context,
                passed=True,
                message=f"artifact {match.name} was produced",
                evidence=[
                    Evidence(
                        type=EvidenceType.FILE,
                        source="artifact_registry",
                        location=match.location or None,
                        summary=match.name,
                    )
                ],
            )

        problem = self._durability_problem(match, context, hashlib)
        return self._result(
            spec,
            context,
            passed=problem is None,
            message=(
                f"artifact {match.name} was produced and stored ({match.size_bytes} bytes)"
                if problem is None
                else f"artifact {match.name} was recorded but {problem}"
            ),
            evidence=[
                Evidence(
                    type=EvidenceType.FILE,
                    source="artifact_store",
                    location=match.location or None,
                    summary=(
                        f"{match.name}: {match.size_bytes} bytes, "
                        f"sha256 {(match.checksum or '')[:12]}"
                        if problem is None
                        else f"{match.name}: {problem}"
                    ),
                )
            ],
        )

    @staticmethod
    def _durability_problem(artifact, context, hashlib) -> str | None:
        """Why this artifact is not durably stored, or None if it is.

        Returns a sentence, never a path: this text reaches API responses and
        the store's location is deployment layout.
        """
        if not artifact.location:
            return "was never given a durable location"

        stored = Path(artifact.location)

        # The store boundary, when the deployment declared one. An artifact
        # whose location sits outside it is not in the store, whatever the
        # record claims.
        root = context.extras.get("artifact_dir") if context.extras else None
        if root:
            try:
                stored.resolve().relative_to(Path(root).resolve())
            except (ValueError, OSError):
                return "was stored outside the configured artifact store"

        try:
            if not stored.is_file():
                return "the stored file is missing"
            data = stored.read_bytes()
        except OSError:
            return "the stored file could not be read"

        if artifact.size_bytes is not None and len(data) != artifact.size_bytes:
            return (
                f"the stored file is {len(data)} bytes, not the recorded "
                f"{artifact.size_bytes}"
            )
        if artifact.checksum:
            actual = hashlib.sha256(data).hexdigest()
            if actual != artifact.checksum:
                return "the stored file does not match its recorded checksum"
        return None


class ToolValidator(Validator):
    """Delegate the judgement to a tool, including any MCP tool.

    Config: ``tool``, ``arguments``, ``expect`` (optional literal), ``path``
    (dotted path into a structured result).
    """

    name = "tool"

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        if context.tools is None or context.tool_context is None:
            return self._result(
                spec,
                context,
                passed=False,
                message="no tool registry available for tool validation",
                confidence=Confidence.BLOCKED,
            )
        tool_id = spec.config.get("tool")
        if not tool_id:
            raise ConfigurationError("tool validator requires a tool id")
        result = await context.tools.call(
            ToolCall(
                tool_id=str(tool_id), arguments=dict(spec.config.get("arguments", {}))
            ),
            context.tool_context,
        )
        if not result.ok:
            return self._result(
                spec,
                context,
                passed=False,
                message=f"validation tool failed: {(result.error or {}).get('message')}",
                confidence=Confidence.BLOCKED,
                evidence=[
                    Evidence(
                        type=EvidenceType.MCP_TOOL,
                        source=str(tool_id),
                        summary="tool call failed",
                        detail=result.error,
                        confidence=Confidence.FAILED,
                    )
                ],
            )

        value = result.output
        for key in str(spec.config.get("path", "")).split("."):
            if key and isinstance(value, dict):
                value = value.get(key)
        expected = spec.config.get("expect", True)
        passed = value == expected if "expect" in spec.config else bool(value)
        return self._result(
            spec,
            context,
            passed=passed,
            message=f"tool returned {value!r} (expected {expected!r})",
            evidence=[
                Evidence(
                    type=EvidenceType.MCP_TOOL,
                    source=str(tool_id),
                    summary=f"returned {value!r}",
                    detail=result.output,
                )
            ],
        )


class CallableValidator(Validator):
    """Wrap a Python predicate. Used by plugins and embedded deployments."""

    name = "callable"

    def __init__(
        self,
        fn: Callable[[ValidationSpec, ValidationContext], Any],
        *,
        name: str = "callable",
    ) -> None:
        self._fn = fn
        self.name = name

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        outcome = self._fn(spec, context)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if isinstance(outcome, ValidationResult):
            return outcome
        passed = bool(outcome)
        return self._result(
            spec,
            context,
            passed=passed,
            message=f"{self.name} returned {passed}",
            evidence=[
                Evidence(
                    type=EvidenceType.DETERMINISTIC_CHECK,
                    source=self.name,
                    summary=str(outcome)[:200],
                )
            ],
        )


class ModelJudgeValidator(Validator):
    """Ask a separate model to judge subjective work.

    This is the fallback for tasks with no deterministic check. Its verdict is
    recorded with ``LIKELY``/``UNCERTAIN`` confidence and evidence marked as an
    inference, never as a confirmed fact (spec sections 34, 96, 97).
    """

    name = "model_judge"

    def __init__(
        self, judge: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
    ) -> None:
        self._judge = judge

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        criteria = spec.config.get("criteria") or spec.description or "the task objective"
        payload = {
            "objective": context.execution.objective,
            "task": context.task.objective if context.task else "",
            "output": context.output_text()[:20000],
            "criteria": criteria,
        }
        try:
            verdict = await self._judge(str(criteria), payload)
        except Exception as exc:  # noqa: BLE001 - a broken judge is not a pass
            return self._result(
                spec,
                context,
                passed=False,
                message=f"evaluator could not reach a verdict: {exc}",
                confidence=Confidence.BLOCKED,
            )

        passed = bool(verdict.get("passed"))
        confidence_value = str(verdict.get("confidence", "")).lower()
        confidence = {
            "confirmed": Confidence.LIKELY,  # a model may not confirm
            "likely": Confidence.LIKELY,
            "uncertain": Confidence.UNCERTAIN,
        }.get(confidence_value, Confidence.LIKELY if passed else Confidence.UNCERTAIN)

        return self._result(
            spec,
            context,
            passed=passed,
            message=str(verdict.get("reason", ""))[:1000],
            confidence=confidence,
            evidence=[
                Evidence(
                    type=EvidenceType.MODEL_JUDGEMENT,
                    source="evaluator_model",
                    summary=str(verdict.get("reason", ""))[:500],
                    detail=verdict,
                    confidence=confidence,
                    knowledge_status=KnowledgeStatus.INFERRED,
                )
            ],
        )


class NoopValidator(Validator):
    """Explicitly records that no check was possible.

    It passes, but with UNCERTAIN confidence and evidence saying so, which is
    the honest representation of an unverifiable task.
    """

    name = "noop"

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        return self._result(
            spec,
            context,
            passed=True,
            message="no deterministic validation was available for this task",
            confidence=Confidence.UNCERTAIN,
            evidence=[
                Evidence(
                    type=EvidenceType.DETERMINISTIC_CHECK,
                    source="noop",
                    summary="no check performed",
                    confidence=Confidence.UNCERTAIN,
                    knowledge_status=KnowledgeStatus.UNVERIFIED,
                )
            ],
        )


def describe_expectation(spec: ValidationSpec) -> str:
    """What a worker must do to satisfy this check, in its own terms.

    A declared check the worker cannot see is a trap: it will be judged against
    a requirement it was never told about. Every validation a task carries is
    rendered into the worker's context through this function, so the check and
    the instruction can never drift apart.
    """
    config = spec.config or {}

    if spec.validator == "json_schema":
        schema = config.get("schema")
        rendered = json.dumps(schema, indent=2)[:1500] if schema else "(unspecified)"
        return (
            "Your final answer must be JSON matching this schema exactly, with "
            "no surrounding prose:\n" + rendered
        )

    if spec.validator == "pattern":
        pattern = config.get("pattern", "")
        if config.get("must_match", True):
            return f"Your answer must contain text matching: {pattern}"
        return f"Your answer must NOT contain text matching: {pattern}"

    if spec.validator == "non_empty":
        minimum = int(config.get("min_length", 1))
        if minimum > 1:
            return f"Your answer must be at least {minimum} characters long."
        return "Your answer must not be empty."

    if spec.validator == "artifact_exists":
        name = config.get("name")
        return (
            f"You must produce an artifact named {name!r}."
            if name
            else "You must produce at least one artifact."
        )

    if spec.validator == "command":
        return (
            "Your work will be checked by running: "
            f"{config.get('command')!r} (expecting exit "
            f"{config.get('expect_exit', 0)})."
        )

    if spec.validator == "tool":
        return f"Your work will be checked by the tool {config.get('tool')!r}."

    if spec.validator == "noop":
        return ""

    description = spec.description or spec.validator
    return f"Your work will be checked by: {description}"


class ValidatorRegistry:
    def __init__(self) -> None:
        self._validators: dict[str, Validator] = {}
        for validator in (
            NoopValidator(),
            NonEmptyValidator(),
            PatternValidator(),
            SchemaValidator(),
            CommandValidator(),
            ArtifactExistsValidator(),
            ToolValidator(),
        ):
            self.register(validator)

    def register(self, validator: Validator) -> Validator:
        self._validators[validator.name] = validator
        return validator

    def register_callable(
        self, name: str, fn: Callable[[ValidationSpec, ValidationContext], Any]
    ) -> Validator:
        return self.register(CallableValidator(fn, name=name))

    def get(self, name: str) -> Validator:
        try:
            return self._validators[name]
        except KeyError as exc:
            raise NotFound(f"validator {name} is not registered", name=name) from exc

    def has(self, name: str) -> bool:
        return name in self._validators

    def names(self) -> list[str]:
        return sorted(self._validators)

    async def run(
        self, spec: ValidationSpec, context: ValidationContext
    ) -> ValidationResult:
        validator = self.get(spec.validator)
        return await validator.validate(spec, context)
