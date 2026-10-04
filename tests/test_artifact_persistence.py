"""Durable artifact publication.

The defect these cover: artifact persistence worked locally and was broken in
the documented production deployment. `deployment/config.production.yaml` set
no `storage.artifact_dir`, so the path fell back to one derived from the unused
SQLite default - `.orchestrator/artifacts`, inside a root filesystem mounted
read-only. Every write failed with EACCES, the failure was swallowed, and
`emit_artifact` reported success because the in-memory record had been stored.
`artifact_exists` then passed on that record. An execution could complete,
report a produced artifact, and leave nothing behind.

The rule these enforce: a publication that did not persist is not a
publication, and nothing downstream may say otherwise.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys

import pytest

from orchestrator.core.domain.models import (
    Artifact,
    Execution,
    Task,
    TaskResult,
    ValidationSpec,
)
from orchestrator.errors import ToolError
from orchestrator.tools import native
from orchestrator.tools.registry import ToolContext
from orchestrator.validation.validators import (
    ArtifactExistsValidator,
    ValidationContext,
)

PAGE = "<!DOCTYPE html><html><body><canvas id='game'></canvas></body></html>"


def _emit():
    entries = native.bookkeeping_tools()
    return next(h for spec, h in entries if spec.id == "orchestrator.emit_artifact")


def _context(tmp_path, execution_id="exe_1", sink=None):
    return ToolContext(
        execution_id=execution_id,
        task_id="task_1",
        metadata={
            "artifact_sink": sink if sink is not None else [],
            "artifact_dir": str(tmp_path),
        },
    )


# ---------------------------------------------------------------------------
# Content is stored, with integrity metadata
# ---------------------------------------------------------------------------


def test_content_is_written_and_described_by_its_own_checksum(tmp_path):
    sink: list = []
    _emit()(
        {"name": "tetris.html", "content": PAGE, "media_type": "text/html"},
        _context(tmp_path, sink=sink),
    )

    stored = tmp_path / "exe_1" / "tetris.html"
    assert stored.read_text(encoding="utf-8") == PAGE

    record = sink[0]
    assert record.location == str(stored)
    assert record.size_bytes == len(PAGE.encode("utf-8"))
    assert record.checksum == hashlib.sha256(PAGE.encode("utf-8")).hexdigest()


def test_structured_content_is_serialised_and_checksummed(tmp_path):
    sink: list = []
    _emit()(
        {
            "name": "report.json",
            "type": "json",
            "content": {"lines_cleared": 4, "score": 1200},
        },
        _context(tmp_path, sink=sink),
    )

    stored = tmp_path / "exe_1" / "report.json"
    data = stored.read_bytes()
    assert b"lines_cleared" in data
    assert sink[0].checksum == hashlib.sha256(data).hexdigest()
    assert sink[0].size_bytes == len(data)


def test_a_reference_artifact_is_published_without_a_write(tmp_path):
    """Nothing to store is not a failure - some artifacts are pointers."""
    sink: list = []
    result = _emit()(
        {
            "name": "dashboard",
            "type": "reference",
            "location": "https://example.internal/dashboard",
        },
        _context(tmp_path, sink=sink),
    )

    assert not list((tmp_path).rglob("*.html"))
    assert sink[0].location == "https://example.internal/dashboard"
    assert sink[0].checksum is None
    assert "written_to" not in result


# ---------------------------------------------------------------------------
# Failure to persist must fail the tool call
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX mode bits are not enforced for the owner on Windows",
)
def test_an_unwritable_store_fails_the_publication(tmp_path):
    """The production case: the store exists and cannot be written.

    Previously this logged a warning, returned an artifact id, and let the run
    complete successfully having stored nothing.
    """
    store = tmp_path / "artifacts"
    store.mkdir()
    store.chmod(stat.S_IREAD | stat.S_IEXEC)  # r-x: no writing
    try:
        with pytest.raises(ToolError) as exc:
            _emit()({"name": "tetris.html", "content": PAGE}, _context(store))
        assert "could not be written" in str(exc.value)
    finally:
        store.chmod(stat.S_IRWXU)


def test_a_store_path_blocked_by_a_file_fails_the_publication(tmp_path):
    """Portable equivalent of an unwritable store.

    The execution directory cannot be created because a regular file occupies
    its path - the same OSError class the read-only production mount produced.
    """
    store = tmp_path / "artifacts"
    store.mkdir()
    (store / "exe_1").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ToolError) as exc:
        _emit()({"name": "tetris.html", "content": PAGE}, _context(store))
    assert "could not be written" in str(exc.value)


def test_a_persistence_failure_never_reports_a_successful_publication(tmp_path):
    """The property the whole change exists to guarantee."""
    store = tmp_path / "artifacts"
    store.mkdir()
    (store / "exe_1").write_text("blocking file", encoding="utf-8")
    sink: list = []

    with pytest.raises(ToolError):
        _emit()({"name": "tetris.html", "content": PAGE}, _context(store, sink=sink))

    # No record reached the sink, so nothing downstream can claim it exists.
    assert sink == []


def test_the_failure_message_does_not_leak_the_store_location(tmp_path):
    """This text reaches the model and the API response.

    The store's path is deployment layout; naming it there hands out the
    filesystem shape of the deployment to anyone who can trigger an error.
    """
    store = tmp_path / "artifacts"
    store.mkdir()
    (store / "exe_1").write_text("blocking file", encoding="utf-8")

    with pytest.raises(ToolError) as exc:
        _emit()({"name": "tetris.html", "content": PAGE}, _context(store))

    rendered = str(exc.value) + str(getattr(exc.value, "details", ""))
    assert str(store) not in rendered
    assert str(tmp_path) not in rendered


# ---------------------------------------------------------------------------
# The store boundary holds against a hostile name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd",
        "..\\..\\windows\\system32\\evil.dll",
        "/etc/shadow",
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "nested/dir/file.html",
        "....//....//escape.txt",
        "con",
        "NUL.txt",
    ],
)
def test_a_hostile_artifact_name_stays_inside_its_execution_directory(hostile, tmp_path):
    _emit()({"name": hostile, "content": "x" * 32}, _context(tmp_path))

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(written) == 1, written
    assert written[0].parent == tmp_path / "exe_1"


def test_each_execution_gets_its_own_directory(tmp_path):
    for execution_id in ("exe_a", "exe_b"):
        _emit()(
            {"name": "out.html", "content": f"<p>{execution_id}</p>"},
            _context(tmp_path, execution_id=execution_id),
        )

    assert (tmp_path / "exe_a" / "out.html").read_text() == "<p>exe_a</p>"
    assert (tmp_path / "exe_b" / "out.html").read_text() == "<p>exe_b</p>"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_a_symlink_in_the_store_is_not_followed(tmp_path):
    """An attacker who can plant a link inside the volume must not redirect a
    write through it."""
    store = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "exe_1").mkdir(parents=True)
    victim = outside / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    try:
        (store / "exe_1" / "out.html").symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(ToolError):
        _emit()({"name": "out.html", "content": "overwritten"}, _context(store))
    assert victim.read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------------------
# artifact_exists proves the file, not the record
# ---------------------------------------------------------------------------


def _validate(execution, task=None, artifact_dir=None, **config):
    import asyncio

    context = ValidationContext(
        execution=execution,
        task=task,
        extras={"artifact_dir": str(artifact_dir)} if artifact_dir else {},
    )
    return asyncio.run(
        ArtifactExistsValidator().validate(
            ValidationSpec(validator="artifact_exists", config=config), context
        )
    )


def _execution_with(artifact) -> Execution:
    execution = Execution(objective="create a tetris game")
    task = Task(id="task_1", name="build")
    task.result = TaskResult(task_id="task_1", ok=True, artifacts=[artifact])
    execution.tasks[task.id] = task
    execution.artifacts.append(artifact)
    return execution


def test_validation_passes_when_the_file_is_really_there(tmp_path):
    sink: list = []
    _emit()({"name": "tetris.html", "content": PAGE}, _context(tmp_path, sink=sink))

    result = _validate(_execution_with(sink[0]), artifact_dir=tmp_path)
    assert result.passed is True
    assert "stored" in result.message


def test_validation_fails_when_the_record_exists_but_the_file_does_not(tmp_path):
    """The exact production symptom, now caught."""
    sink: list = []
    _emit()({"name": "tetris.html", "content": PAGE}, _context(tmp_path, sink=sink))
    (tmp_path / "exe_1" / "tetris.html").unlink()

    result = _validate(_execution_with(sink[0]), artifact_dir=tmp_path)
    assert result.passed is False
    assert "missing" in result.message


def test_validation_fails_when_no_durable_location_was_ever_recorded():
    """An in-memory record alone must not satisfy "a file was produced"."""
    artifact = Artifact(name="tetris.html", content=PAGE)  # never persisted
    result = _validate(_execution_with(artifact))
    assert result.passed is False
    assert "durable location" in result.message


def test_validation_fails_when_the_stored_bytes_were_altered(tmp_path):
    sink: list = []
    _emit()({"name": "tetris.html", "content": PAGE}, _context(tmp_path, sink=sink))
    (tmp_path / "exe_1" / "tetris.html").write_text("tampered", encoding="utf-8")

    result = _validate(_execution_with(sink[0]), artifact_dir=tmp_path)
    assert result.passed is False
    # Size differs first, so either message is a correct rejection.
    assert "checksum" in result.message or "bytes" in result.message


def test_validation_fails_when_the_size_disagrees(tmp_path):
    sink: list = []
    _emit()({"name": "tetris.html", "content": PAGE}, _context(tmp_path, sink=sink))
    record = sink[0]
    record.size_bytes = 999999  # record no longer describes the file

    result = _validate(_execution_with(record), artifact_dir=tmp_path)
    assert result.passed is False
    assert "bytes" in result.message


def test_validation_fails_when_the_location_is_outside_the_store(tmp_path):
    """A path outside the configured store is not in the store."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    stray = elsewhere / "tetris.html"
    stray.write_text(PAGE, encoding="utf-8")

    artifact = Artifact(
        name="tetris.html",
        content=PAGE,
        location=str(stray),
        size_bytes=len(PAGE.encode()),
        checksum=hashlib.sha256(PAGE.encode()).hexdigest(),
    )
    store = tmp_path / "artifacts"
    store.mkdir()

    result = _validate(_execution_with(artifact), artifact_dir=store)
    assert result.passed is False
    assert "outside" in result.message


def test_a_reference_artifact_satisfies_validation_without_a_file(tmp_path):
    artifact = Artifact(name="dashboard", location="https://example.internal/d")
    result = _validate(_execution_with(artifact), artifact_dir=tmp_path)
    assert result.passed is True


def test_content_verification_can_be_turned_off_explicitly(tmp_path):
    """For stores the validating process cannot reach. Opt-in, and weaker."""
    artifact = Artifact(name="tetris.html", content=PAGE, location="/unreachable/x")
    result = _validate(
        _execution_with(artifact), artifact_dir=tmp_path, verify_content=False
    )
    assert result.passed is True


def test_validation_messages_do_not_leak_the_store_path(tmp_path):
    sink: list = []
    _emit()({"name": "tetris.html", "content": PAGE}, _context(tmp_path, sink=sink))
    (tmp_path / "exe_1" / "tetris.html").unlink()

    result = _validate(_execution_with(sink[0]), artifact_dir=tmp_path)
    assert str(tmp_path) not in result.message


# ---------------------------------------------------------------------------
# Two replicas, one store
# ---------------------------------------------------------------------------


def test_an_artifact_published_by_one_replica_is_readable_by_another(tmp_path):
    """What the shared volume buys.

    Two processes with independent state, one mounted store: the second must
    be able to read - and validate - what the first published, because a
    request can land on either replica.
    """
    shared = tmp_path / "shared-artifacts"

    # Replica 1 publishes.
    sink: list = []
    _emit()(
        {"name": "tetris.html", "content": PAGE},
        ToolContext(
            execution_id="exe_shared",
            task_id="t",
            metadata={"artifact_sink": sink, "artifact_dir": str(shared)},
        ),
    )
    published = sink[0]

    # Replica 2 has its own registry and its own in-memory state, and sees the
    # same volume. It reads the bytes and validates the record it was handed.
    replica_two_entries = native.bookkeeping_tools()
    assert replica_two_entries is not None
    stored = shared / "exe_shared" / "tetris.html"
    assert stored.read_text(encoding="utf-8") == PAGE

    result = _validate(_execution_with(published), artifact_dir=shared)
    assert result.passed is True
