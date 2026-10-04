"""Artifacts an agent publishes must actually be stored.

These cover a defect where `orchestrator.emit_artifact` built an Artifact,
dropped it on the floor, and returned an `artifact_id` for it anyway. Every
call reported success, no artifact was ever produced, and `artifact_exists`
failed a run whose agent had done exactly what it was asked. The whole point of
the platform is that software decides whether work happened; a tool that
reports success for work it did not do defeats that at the root.
"""

from __future__ import annotations

import pytest
from conftest import build_platform, planning_model, run

from orchestrator.errors import ToolError
from orchestrator.llm.base import ModelResponse, ToolCallRequest
from orchestrator.tools import native
from orchestrator.tools.registry import ToolContext

PAGE = "<!DOCTYPE html><html><body><h1>Tic Tac Toe</h1></body></html>"


def _emit_handler(**kwargs):
    entries = native.bookkeeping_tools(**kwargs)
    return next(h for spec, h in entries if spec.id == "orchestrator.emit_artifact")


# ---------------------------------------------------------------------------
# The tool itself
# ---------------------------------------------------------------------------


def test_emitting_with_nowhere_to_store_fails_instead_of_reporting_success():
    """The original defect. Silent success is the one unacceptable outcome.

    It leaves the model believing it published, the operator believing the run
    produced nothing, and no record anywhere of which is true.
    """
    emit = _emit_handler()

    with pytest.raises(ToolError) as exc:
        emit(
            {"name": "tic-tac-toe.html", "content": PAGE}, ToolContext(execution_id="exe_1")
        )

    assert "not stored" in str(exc.value)


def test_the_sink_supplied_by_the_runtime_receives_the_artifact():
    emit = _emit_handler()
    sink: list = []
    context = ToolContext(
        execution_id="exe_1",
        task_id="task_1",
        metadata={"artifact_sink": sink},
    )

    result = emit(
        {
            "name": "tic-tac-toe.html",
            "type": "text",
            "content": PAGE,
            "media_type": "text/html",
        },
        context,
    )

    assert len(sink) == 1
    stored = sink[0]
    assert stored.name == "tic-tac-toe.html"
    assert stored.content == PAGE  # the content, not a summary of it
    assert stored.produced_by == "task_1"  # attributed to the task that made it
    assert result["artifact_id"] == stored.id


def test_an_explicit_callback_still_works_for_embedders():
    """`on_artifact` remains the seam for hosts routing artifacts elsewhere."""
    seen: list = []
    emit = _emit_handler(on_artifact=lambda a, c: seen.append(a))

    emit({"name": "report.md", "content": "# hello"}, ToolContext(execution_id="e"))

    assert [a.name for a in seen] == ["report.md"]


def test_an_artifact_still_needs_a_name():
    emit = _emit_handler()
    with pytest.raises(ToolError):
        emit(
            {"content": PAGE}, ToolContext(execution_id="e", metadata={"artifact_sink": []})
        )


# ---------------------------------------------------------------------------
# Through the real runtime, which is where the wiring was missing
# ---------------------------------------------------------------------------


# The default test config inherits the production profile, which denies every
# tool that is not explicitly granted. That is the right default; it just means
# a test about publishing artifacts has to grant the publishing tool.
ALLOW_EMIT = {
    "policy": {
        "rules": [
            {
                "kind": "tool",
                "subject": "orchestrator.emit_artifact",
                "effect": "allow",
                "reason": "this test is about publishing artifacts",
            }
        ]
    }
}


def _worker_that_emits_then_answers():
    """A worker that publishes a file on its first turn, then reports."""
    turns = {"n": 0}

    def worker(request):
        turns["n"] += 1
        if turns["n"] == 1:
            return ModelResponse(
                text="publishing the page",
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="orchestrator.emit_artifact",
                        arguments={
                            "name": "tic-tac-toe.html",
                            "type": "text",
                            "content": PAGE,
                            "media_type": "text/html",
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        return "The page has been published as tic-tac-toe.html."

    return planning_model(worker=worker)


def test_an_emitted_artifact_reaches_the_finished_execution():
    """End to end: what the agent publishes is on the run when it finishes.

    No test previously exercised this path, which is how a tool that stored
    nothing survived in the codebase.
    """

    async def scenario():
        platform = await build_platform(_worker_that_emits_then_answers(), **ALLOW_EMIT)
        execution = await platform.run("Create a tic tac toe app")
        await platform.close()
        return execution

    execution = run(scenario())

    names = [a.name for a in execution.artifacts]
    assert "tic-tac-toe.html" in names, (
        f"artifacts were lost between the tool and the execution: {names}"
    )
    published = next(a for a in execution.artifacts if a.name == "tic-tac-toe.html")
    assert published.content == PAGE


def test_artifact_exists_passes_once_something_was_actually_produced():
    """The validator that failed the real run must now be satisfiable."""

    async def scenario():
        turns = {"n": 0}

        def worker(request):
            turns["n"] += 1
            if turns["n"] == 1:
                return ModelResponse(
                    text="publishing the page",
                    tool_calls=[
                        ToolCallRequest(
                            id="call_1",
                            name="orchestrator.emit_artifact",
                            arguments={"name": "tic-tac-toe.html", "content": PAGE},
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return "The page has been published as tic-tac-toe.html."

        platform = await build_platform(
            planning_model(
                worker=worker,
                tasks=[
                    {
                        "key": "only",
                        "name": "Create Tic Tac Toe App",
                        "objective": "Produce the page.",
                        "validation": {"validator": "artifact_exists"},
                    }
                ],
            ),
            **ALLOW_EMIT,
        )
        execution = await platform.run("Create a tic tac toe app")
        await platform.close()
        return execution

    execution = run(scenario())

    checks = [v for v in execution.validations if v.validator == "artifact_exists"]
    assert checks, "the artifact_exists validator did not run"
    assert all(v.passed for v in checks), [v.message for v in checks]


def test_work_published_before_a_failure_is_not_thrown_away():
    """An agent that emits a good file and then runs out of turns still made it.

    Discarding it on the way out loses the only part of the attempt worth
    keeping - and it is the part the person asked for.
    """
    from orchestrator.agents.runtime import BaseRuntime
    from orchestrator.core.domain.models import Artifact, Task

    kept = Artifact(name="tic-tac-toe.html", content=PAGE)
    result = BaseRuntime._failure(
        Task(id="task_1", name="t"),
        "agent reached its iteration limit",
        artifacts=[kept],
    )

    assert result.ok is False
    assert [a.name for a in result.artifacts] == ["tic-tac-toe.html"]


# ---------------------------------------------------------------------------
# "Durable" has to mean a file
# ---------------------------------------------------------------------------


def test_a_published_artifact_is_written_to_disk(tmp_path):
    """A database row is not a deliverable.

    A run that generated a complete, working page still left nothing the person
    who asked for it could open. Publishing now writes the content out and
    records where it went.
    """
    emit = _emit_handler()
    sink: list = []
    emit(
        {"name": "tetris.html", "content": PAGE, "media_type": "text/html"},
        ToolContext(
            execution_id="exe_9",
            task_id="task_1",
            metadata={"artifact_sink": sink, "artifact_dir": str(tmp_path)},
        ),
    )

    written = tmp_path / "exe_9" / "tetris.html"
    assert written.is_file(), f"nothing written under {tmp_path}"
    assert written.read_text(encoding="utf-8") == PAGE
    # And the record says where it went, so a caller does not have to guess.
    assert sink[0].location == str(written)


def test_runs_do_not_overwrite_each_others_artifacts(tmp_path):
    emit = _emit_handler()
    for execution_id in ("exe_a", "exe_b"):
        emit(
            {"name": "index.html", "content": f"<p>{execution_id}</p>"},
            ToolContext(
                execution_id=execution_id,
                task_id="t",
                metadata={"artifact_sink": [], "artifact_dir": str(tmp_path)},
            ),
        )

    assert (tmp_path / "exe_a" / "index.html").read_text() == "<p>exe_a</p>"
    assert (tmp_path / "exe_b" / "index.html").read_text() == "<p>exe_b</p>"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd",
        "..\\..\\windows\\system32\\evil.dll",
        "/etc/shadow",
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "nested/dir/file.html",
    ],
)
def test_an_agent_chosen_name_cannot_escape_the_artifact_directory(hostile, tmp_path):
    """The filename comes from the model, so it is untrusted input.

    A name is a label here, never a path: separators and traversal are stripped
    rather than resolved, so there is no arithmetic to get wrong.
    """
    emit = _emit_handler()
    emit(
        {"name": hostile, "content": "x" * 20},
        ToolContext(
            execution_id="exe_1",
            task_id="t",
            metadata={"artifact_sink": [], "artifact_dir": str(tmp_path)},
        ),
    )

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(written) == 1, written
    # Exactly one level below the run's directory, and nowhere else.
    assert written[0].parent == tmp_path / "exe_1"


def test_a_reference_artifact_with_no_content_writes_nothing(tmp_path):
    """Nothing to write is not a failure - some artifacts are just pointers."""
    emit = _emit_handler()
    sink: list = []
    emit(
        {
            "name": "dashboard",
            "type": "reference",
            "location": "https://example.internal/dashboard",
        },
        ToolContext(
            execution_id="exe_1",
            task_id="t",
            metadata={"artifact_sink": sink, "artifact_dir": str(tmp_path)},
        ),
    )

    assert not list(tmp_path.rglob("*.html"))
    assert sink[0].location == "https://example.internal/dashboard"


def test_publishing_still_works_when_no_directory_is_configured():
    """Embedders that only want records in memory must not break."""
    emit = _emit_handler()
    sink: list = []
    result = emit(
        {"name": "report.md", "content": "# hi"},
        ToolContext(execution_id="e", metadata={"artifact_sink": sink}),
    )

    assert len(sink) == 1
    assert "written_to" not in result


def test_the_agent_is_told_to_publish_rather_than_paste():
    """The prompt asked for prose and never mentioned publishing.

    A model that pasted a file into its answer and told the reader to save it
    was following instructions exactly; the instructions were the defect.
    """
    from orchestrator.agents.runtime import SYSTEM_PROMPT

    assert "emit_artifact" in SYSTEM_PROMPT
    assert "paste" in SYSTEM_PROMPT.lower()


def test_non_empty_reports_likely_not_confirmed():
    """The weakest check must not carry the platform's strongest claim."""
    import asyncio

    from orchestrator.core.domain.enums import Confidence
    from orchestrator.core.domain.models import (
        Execution,
        Task,
        TaskResult,
        ValidationSpec,
    )
    from orchestrator.validation.validators import NonEmptyValidator, ValidationContext

    task = Task(id="t", name="t")
    task.result = TaskResult(
        task_id="t", ok=True, summary="some output", output="some output"
    )
    execution = Execution(objective="o")
    execution.tasks[task.id] = task

    context = ValidationContext(execution=execution, task=task)
    result = asyncio.run(
        NonEmptyValidator().validate(ValidationSpec(validator="non_empty"), context)
    )

    assert result.passed is True
    assert result.confidence is Confidence.LIKELY


# ---------------------------------------------------------------------------
# A retry that succeeds is the outcome that counts
# ---------------------------------------------------------------------------


def test_a_failure_from_a_superseded_attempt_does_not_block_completion():
    """`execution.validations` is append-only, so attempt one's failure stays.

    Counting it made retrying pointless for any task with a mandatory
    validator: the first failure blocked the run permanently, however well the
    retry went. A real run hit exactly this - task succeeded, artifact
    produced, run reported "no artifact named <any> was produced".
    """
    from orchestrator.core.domain.enums import TaskStatus
    from orchestrator.core.domain.models import Execution, Task, ValidationResult
    from orchestrator.validation.gates import can_complete

    execution = Execution(objective="create a tetris game")
    task = Task(id="tsk_1", name="build it")
    task.status = TaskStatus.SUCCEEDED
    execution.tasks[task.id] = task

    execution.validations.append(
        ValidationResult(
            validator="artifact_exists",
            target_id=task.id,
            passed=False,
            mandatory=True,
            message="no artifact named <any> was produced",
        )
    )
    execution.validations.append(
        ValidationResult(
            validator="artifact_exists",
            target_id=task.id,
            passed=True,
            mandatory=True,
            message="artifact tetris.html was produced",
        )
    )

    ok, reason = can_complete(execution)
    assert ok is True, reason


def test_a_failure_on_a_task_that_never_recovered_still_blocks():
    """The forgiveness is for superseded attempts, not for failure itself."""
    from orchestrator.core.domain.enums import TaskStatus
    from orchestrator.core.domain.models import Execution, Task, ValidationResult
    from orchestrator.validation.gates import can_complete

    execution = Execution(objective="create a tetris game")
    task = Task(id="tsk_1", name="build it")
    task.status = TaskStatus.FAILED
    execution.tasks[task.id] = task
    execution.validations.append(
        ValidationResult(
            validator="artifact_exists",
            target_id=task.id,
            passed=False,
            mandatory=True,
            message="no artifact was produced",
        )
    )

    ok, reason = can_complete(execution)
    assert ok is False
    assert "artifact_exists" in reason


def test_an_objective_level_failure_is_never_forgiven():
    """Only task-scoped results are superseded by retries.

    An objective-level check has no attempt to be replaced by, so a failure
    there must still stop the run.
    """
    from orchestrator.core.domain.models import Execution, ValidationResult
    from orchestrator.validation.gates import can_complete

    execution = Execution(objective="create a tetris game")
    execution.validations.append(
        ValidationResult(
            validator="pattern",
            target_id=execution.id,
            passed=False,
            mandatory=True,
            message="required pattern not found",
        )
    )

    ok, reason = can_complete(execution)
    assert ok is False
    assert "pattern" in reason


def test_a_name_collision_with_new_content_is_versioned_not_overwritten(tmp_path):
    """The earlier file is somebody's output too.

    Silently replacing it loses work that a previous task or attempt
    published, so a second artifact with the same name but different content
    is stored alongside the first.
    """
    emit = _emit_handler()
    sink: list = []
    context = ToolContext(
        execution_id="exe_1",
        task_id="t",
        metadata={"artifact_sink": sink, "artifact_dir": str(tmp_path)},
    )
    emit({"name": "tetris.html", "content": "<p>first</p>"}, context)
    emit({"name": "tetris.html", "content": "<p>second</p>"}, context)

    directory = tmp_path / "exe_1"
    assert (directory / "tetris.html").read_text(encoding="utf-8") == "<p>first</p>"
    assert (directory / "tetris-2.html").read_text(encoding="utf-8") == "<p>second</p>"
    assert len({a.location for a in sink}) == 2


def test_republishing_identical_content_reuses_the_same_file(tmp_path):
    """A retried task that produces the same file must not litter the store."""
    emit = _emit_handler()
    sink: list = []
    context = ToolContext(
        execution_id="exe_1",
        task_id="t",
        metadata={"artifact_sink": sink, "artifact_dir": str(tmp_path)},
    )
    emit({"name": "tetris.html", "content": "<p>same</p>"}, context)
    emit({"name": "tetris.html", "content": "<p>same</p>"}, context)

    files = [f for f in (tmp_path / "exe_1").iterdir() if f.is_file()]
    assert [f.name for f in files] == ["tetris.html"]
    assert len({a.location for a in sink}) == 1
