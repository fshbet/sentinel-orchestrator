"""Native tools.

These are general-purpose primitives, not domain features: notes, artifacts,
files, subprocesses, HTTP. The genuinely dangerous families are opt-in factory
functions rather than automatic registrations, so a default install grants an
agent nothing beyond the orchestrator's own bookkeeping (spec sections 2, 62).

Filesystem and process tools are confined to the workspace root handed to them.
Path traversal out of the workspace is rejected before anything is opened.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.domain.enums import ArtifactType, RiskLevel, ToolSource
from ..core.domain.models import Artifact, ToolSpec
from ..errors import PermissionDenied, ToolError
from . import permissions as perms
from .registry import ToolContext, ToolHandler

if TYPE_CHECKING:  # pragma: no cover - import cycle avoided at runtime
    from .egress import EgressPolicy
    from .execpolicy import ExecPolicy

_log = logging.getLogger("orchestrator.tools.native")

ToolEntry = tuple[ToolSpec, ToolHandler]


def _resolve_within(root: Path, candidate: str) -> Path:
    """Resolve ``candidate`` and refuse anything outside ``root``."""
    base = root.resolve()
    target = (
        (base / candidate).resolve()
        if not os.path.isabs(candidate)
        else Path(candidate).resolve()
    )
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise PermissionDenied(
            f"path {candidate} resolves outside the workspace",
            path=str(target),
            workspace=str(base),
        ) from exc
    return target


def _workspace(context: ToolContext, fallback: Path) -> Path:
    return Path(context.workspace) if context.workspace else fallback


# The model chooses the artifact's name, so it is untrusted input used as a
# filename. Directory separators, parent traversal, drive letters, and the
# Windows reserved device names are all removed rather than escaped: a name is
# a label here, never a path.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def safe_artifact_filename(name: str) -> str:
    """Turn an agent-chosen artifact name into one safe filename component."""
    cleaned = _UNSAFE_NAME.sub("_", (name or "").strip()).strip("._-")
    if not cleaned:
        cleaned = "artifact"
    stem = cleaned.split(".", 1)[0].lower()
    if stem in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:120]


class ArtifactPersistenceError(ToolError):
    """The artifact could not be stored durably.

    Raised rather than logged. A publication that did not persist must not be
    reported as a publication: in the production deployment the root
    filesystem is read-only, the fallback artifact path was unwritable, and
    swallowing the error let executions complete successfully while the file
    the user asked for existed nowhere.
    """


def _artifact_bytes(content: Any) -> bytes:
    """The exact bytes to store, for both writing and checksumming."""
    text = (
        content if isinstance(content, str) else json.dumps(content, indent=2, default=str)
    )
    return text.encode("utf-8")


def _unique_target(directory: Path, filename: str, payload: bytes) -> Path:
    """Resolve a collision without ever overwriting different content.

    Three cases, and the middle one is the important one:

    * nothing there -> use the name;
    * the same bytes are already there -> reuse it, so a retried task that
      republishes an identical file does not litter the directory with copies;
    * different bytes -> add a version suffix, because the earlier file is
      somebody's output too and silently replacing it loses it.
    """
    target = directory / filename
    if not target.exists():
        return target
    try:
        if target.read_bytes() == payload:
            return target
    except OSError:
        pass

    stem, dot, suffix = filename.partition(".")
    for version in range(2, 1000):
        candidate = directory / f"{stem}-{version}{dot}{suffix}"
        if not candidate.exists():
            return candidate
        try:
            if candidate.read_bytes() == payload:
                return candidate
        except OSError:
            continue
    raise ArtifactPersistenceError(
        "too many versions of this artifact name already exist",
        artifact_name=filename,
    )


def _persist_artifact(artifact: Any, context: ToolContext) -> Path | None:
    """Write an artifact's content to the run's artifact directory.

    Returns the path written, or None when there is nothing to write - either a
    reference-only artifact carrying no content, or an embedder that has not
    configured a store at all.

    When a store *is* configured and there *is* content, failing to write is
    fatal to the tool call. See ArtifactPersistenceError.
    """
    content = artifact.content
    if content is None or content == "":
        return None

    root = (context.metadata or {}).get("artifact_dir")
    if not root:
        return None

    payload = _artifact_bytes(content)
    directory = Path(root) / (context.execution_id or "unattached")

    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = _unique_target(directory, safe_artifact_filename(artifact.name), payload)

        # Defence in depth. safe_artifact_filename already reduces the name to
        # a single component, but the check is cheap and the consequence of a
        # gap is a write outside the artifact root.
        resolved_root = directory.resolve()
        resolved_target = target.resolve()
        if resolved_target.parent != resolved_root:
            raise ArtifactPersistenceError(
                "artifact name resolved outside its execution directory",
                artifact_name=artifact.name,
            )
        # Never follow a symlink out of the store: an attacker who can create
        # one inside the volume could otherwise redirect a write anywhere the
        # process can reach.
        if target.is_symlink():
            raise ArtifactPersistenceError(
                "artifact path is a symbolic link and was not written",
                artifact_name=artifact.name,
            )

        target.write_bytes(payload)
    except ArtifactPersistenceError:
        raise
    except OSError as exc:
        # The message names the errno and the artifact, never the absolute
        # path: this text reaches the model and the API response, and the
        # store's location is deployment layout.
        _log.warning(
            "could not persist artifact %r under %s",
            artifact.name,
            root,
            exc_info=True,
        )
        raise ArtifactPersistenceError(
            "the artifact store could not be written to, so nothing was "
            f"published ({exc.__class__.__name__}).",
            artifact_name=artifact.name,
            remedy=(
                "check that storage.artifact_dir points at a writable volume "
                "in this deployment"
            ),
        ) from exc

    artifact.size_bytes = len(payload)
    artifact.checksum = hashlib.sha256(payload).hexdigest()
    return target


# --------------------------------------------------------------------------
# Orchestrator bookkeeping. Safe enough to grant by default.
# --------------------------------------------------------------------------


def bookkeeping_tools(
    *,
    on_note: Callable[[str, str, ToolContext], None] | None = None,
    on_artifact: Callable[[Artifact, ToolContext], None] | None = None,
) -> list[ToolEntry]:
    """Tools that write into the orchestrator's own memory and artifact store."""

    def record_note(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        key = str(arguments.get("key", "")).strip()
        value = str(arguments.get("value", ""))
        if not key:
            raise ToolError("record_note requires a key")
        if on_note is not None:
            on_note(key, value, context)
        return {"recorded": key, "length": len(value)}

    def emit_artifact(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        name = str(arguments.get("name", "")).strip()
        if not name:
            raise ToolError("emit_artifact requires a name")
        artifact = Artifact(
            type=ArtifactType(arguments.get("type", "text")),
            name=name,
            location=str(arguments.get("location", "")),
            content=arguments.get("content"),
            media_type=arguments.get("media_type"),
            produced_by=context.task_id,
        )

        # "Durable" has to mean a file someone can open. Storing the content in
        # a database row and calling it durable is how a run that generated a
        # complete, working page still left nothing on disk for the person who
        # asked for it. The write is confined to the orchestrator's own artifact
        # directory, so publishing a result never needs general filesystem
        # write access.
        written = _persist_artifact(artifact, context)
        if written is not None:
            artifact.location = str(written)

        # Where the artifact record lands. The runtime puts a list here for the
        # task it is running, so what the agent publishes ends up on that task's
        # result; `on_artifact` stays as a seam for embedders who route
        # artifacts somewhere else entirely.
        delivered = False
        sink = (context.metadata or {}).get("artifact_sink")
        if sink is not None:
            sink.append(artifact)
            delivered = True
        if on_artifact is not None:
            on_artifact(artifact, context)
            delivered = True

        if written is not None:
            result_extra = {"written_to": str(written)}
        else:
            result_extra = {}

        if not delivered:
            # Previously this returned an artifact_id and called it a success.
            # Nothing was stored, so every downstream check for the artifact
            # failed while the agent had been told, thirty-three times, that
            # its work was safely published. A tool that cannot do the thing
            # must say so: silent success is the one outcome that leaves both
            # the model and the operator misinformed.
            raise ToolError(
                "artifact was not stored: this execution has no artifact sink, "
                "so emit_artifact has nowhere to publish to.",
                artifact_name=name,
                remedy=(
                    "pass on_artifact= to bookkeeping_tools, or run the agent "
                    "through AgentRuntime, which supplies one per task"
                ),
            )

        return {"artifact_id": artifact.id, "name": artifact.name, **result_extra}

    return [
        (
            ToolSpec(
                id="orchestrator.record_note",
                name="record_note",
                description=(
                    "Store a short keyed note in working memory so later tasks can "
                    "retrieve it without re-deriving it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["key", "value"],
                },
                permissions=[perms.MEMORY_WRITE],
                source=ToolSource.BUILTIN,
                risk=RiskLevel.NONE,
            ),
            record_note,
        ),
        (
            ToolSpec(
                id="orchestrator.emit_artifact",
                name="emit_artifact",
                description="Publish a result as a durable artifact of this execution.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [t.value for t in ArtifactType],
                        },
                        "content": {},
                        "location": {"type": "string"},
                        "media_type": {"type": "string"},
                    },
                    "required": ["name"],
                },
                permissions=[perms.ARTIFACT_WRITE],
                source=ToolSource.BUILTIN,
                risk=RiskLevel.NONE,
            ),
            emit_artifact,
        ),
    ]


# --------------------------------------------------------------------------
# Filesystem. Opt-in.
# --------------------------------------------------------------------------


def filesystem_tools(
    root: str | Path = ".", *, allow_write: bool = True
) -> list[ToolEntry]:
    base = Path(root)

    def read_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        path = _resolve_within(_workspace(context, base), str(arguments.get("path", "")))
        limit = int(arguments.get("max_bytes", 200_000))
        data = path.read_bytes()[:limit]
        return {
            "path": str(path),
            "size": path.stat().st_size,
            "truncated": path.stat().st_size > limit,
            "content": data.decode("utf-8", errors="replace"),
        }

    def list_directory(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        path = _resolve_within(_workspace(context, base), str(arguments.get("path", ".")))
        entries = sorted(
            (
                {"name": p.name, "type": "directory" if p.is_dir() else "file"}
                for p in path.iterdir()
            ),
            key=lambda e: (e["type"], e["name"]),
        )
        return {"path": str(path), "entries": entries[: int(arguments.get("limit", 500))]}

    def write_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        path = _resolve_within(_workspace(context, base), str(arguments.get("path", "")))
        content = str(arguments.get("content", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "bytes_written": len(content.encode("utf-8"))}

    entries: list[ToolEntry] = [
        (
            ToolSpec(
                id="fs.read_file",
                name="read_file",
                description="Read a UTF-8 text file from the workspace.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_bytes": {"type": "integer"},
                    },
                    "required": ["path"],
                },
                permissions=[perms.FS_READ],
                source=ToolSource.NATIVE,
                risk=RiskLevel.LOW,
            ),
            read_file,
        ),
        (
            ToolSpec(
                id="fs.list_directory",
                name="list_directory",
                description="List the entries of a workspace directory.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
                permissions=[perms.FS_READ],
                source=ToolSource.NATIVE,
                risk=RiskLevel.LOW,
            ),
            list_directory,
        ),
    ]
    if allow_write:
        entries.append(
            (
                ToolSpec(
                    id="fs.write_file",
                    name="write_file",
                    description="Write a UTF-8 text file inside the workspace.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                    permissions=[perms.FS_WRITE],
                    source=ToolSource.NATIVE,
                    risk=RiskLevel.MEDIUM,
                    idempotent=False,
                ),
                write_file,
            )
        )
    return entries


# --------------------------------------------------------------------------
# Process execution. Opt-in, and the allow-list is mandatory.
# --------------------------------------------------------------------------


def process_tools(
    root: str | Path = ".",
    *,
    allowed_commands: Sequence[str] = (),
    timeout: float = 120.0,
    policy: ExecPolicy | None = None,
) -> list[ToolEntry]:
    """Run a subprocess under an explicit execution policy.

    There is no "run anything" mode. The policy is validated when these tools
    are built, not when one is first called, so a configuration that permits a
    shell or leaks an environment variable fails at startup rather than on the
    first request that exercises it.

    See ``execpolicy.py`` for what each control stops and — importantly — what
    this is not: it confines a cooperative process, not a hostile one.
    """
    from .execpolicy import ExecPolicy, build_environment, resolve_executable

    if policy is None:
        policy = ExecPolicy(
            allowed_commands=tuple(allowed_commands),
            root=str(root),
            timeout=timeout,
        )
    try:
        policy.validate()
    except ValueError as exc:
        # Kept as ToolError: this is the established contract for a tool that
        # cannot be built, and callers already handle it.
        raise ToolError(str(exc)) from exc

    base = Path(policy.root)

    async def run_command(
        arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        command = arguments.get("command")
        if isinstance(command, str):
            # Split for convenience, never handed to a shell.
            argv = shlex.split(command)
        elif isinstance(command, list):
            argv = [str(part) for part in command]
        else:
            raise ToolError("run_command requires a command string or argv list")
        if not argv:
            raise ToolError("run_command requires a non-empty command")

        policy.check_arguments(argv)
        executable = resolve_executable(argv[0], policy)

        workdir = _workspace(context, base)
        if arguments.get("cwd"):
            workdir = _resolve_within(workdir, str(arguments["cwd"]))
        # Resolve again after joining: a symlink inside the root can point
        # outside it, and the check has to be on where the path actually lands.
        try:
            real_workdir = Path(workdir).resolve()
            real_base = Path(base).resolve()
        except OSError as exc:
            raise ToolError(f"could not resolve the working directory: {exc}") from exc
        if real_workdir != real_base and real_base not in real_workdir.parents:
            raise PermissionDenied(
                f"working directory {real_workdir} is outside the permitted "
                f"root {real_base}",
                cwd=str(real_workdir),
                root=str(real_base),
            )

        environment = build_environment(policy, arguments.get("env"))
        limit = policy.effective_timeout(arguments.get("timeout"))

        process = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            cwd=str(real_workdir),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # No shell, and no inherited stdin: a process waiting on input it
            # will never get is a process that holds a slot until the timeout.
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=limit)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ToolError(
                f"command timed out after {limit} seconds",
                command=argv[0],
                timeout=limit,
            ) from None

        cap = policy.max_output_bytes
        out = stdout or b""
        err = stderr or b""
        return {
            "exit_code": process.returncode,
            "stdout": out[:cap].decode("utf-8", errors="replace"),
            "stderr": err[:cap].decode("utf-8", errors="replace"),
            "stdout_truncated": len(out) > cap,
            "stderr_truncated": len(err) > cap,
            "command": argv,
            "executable": executable,
        }

    return [
        (
            ToolSpec(
                id="process.run",
                name="run_command",
                description=(
                    "Run one of the permitted executables: "
                    + ", ".join(policy.allowed_commands)
                    + ". Not run through a shell; the environment is not inherited."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                        "cwd": {"type": "string"},
                        "env": {"type": "object"},
                        "timeout": {"type": "number"},
                    },
                    "required": ["command"],
                },
                permissions=[perms.PROCESS_EXECUTE],
                source=ToolSource.NATIVE,
                risk=RiskLevel.HIGH,
                timeout_seconds=policy.timeout,
            ),
            run_command,
        )
    ]


def http_tools(
    *,
    allowed_hosts: Sequence[str] = (),
    timeout: float = 30.0,
    policy: EgressPolicy | None = None,
) -> list[ToolEntry]:
    """HTTP tools bound to an explicit egress policy.

    Two tools are registered rather than one, because a tool that can POST is a
    different privilege from one that can only fetch. Splitting them lets a task
    be granted ``http.request`` without also being granted the ability to send
    data outward — a distinction a single tool guarded by a runtime check cannot
    express, since by the time that check runs the tool is already in the
    model's list of what it may call.
    """
    from .egress import (
        EgressPolicy,
        filter_headers,
        resolve_and_validate,
        validate_redirect,
    )

    if policy is None:
        policy = EgressPolicy(
            allowed_hosts=tuple(allowed_hosts),
            read_timeout=timeout,
        )

    async def _perform(
        arguments: dict[str, Any], context: ToolContext, *, writing: bool
    ) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ToolError(
                "http tools require httpx; install universal-orchestrator[http]"
            ) from exc

        method = str(arguments.get("method", "POST" if writing else "GET")).upper()
        required = policy.check_method(method)
        # The tool that was granted must match the privilege the method needs,
        # or http.request becomes a way to POST.
        if not writing and required != "network.read":
            raise PermissionDenied(
                f"{method} modifies state and needs the http.send tool, which "
                f"requires the network.write permission",
                method=method,
                required_permission=required,
            )
        if writing and required != "network.write":
            raise PermissionDenied(
                f"{method} is a read method; use the http.request tool",
                method=method,
            )

        url = str(arguments.get("url", ""))
        if not url:
            raise ToolError("a url is required")

        headers = filter_headers(arguments.get("headers"), policy)

        body = arguments.get("body")
        json_body = arguments.get("json")
        if body is not None:
            policy.check_request_size(len(str(body).encode("utf-8")))
        if json_body is not None:
            import json as _json

            policy.check_request_size(len(_json.dumps(json_body).encode("utf-8")))

        target = resolve_and_validate(url, policy)

        timeouts = httpx.Timeout(
            connect=policy.connect_timeout,
            read=policy.read_timeout,
            write=policy.write_timeout,
            pool=policy.connect_timeout,
        )

        hops: list[str] = []
        current = target

        # Redirects are followed by hand. httpx's follow_redirects resolves and
        # connects to each hop without consulting the policy, which is exactly
        # the bypass this loop exists to prevent.
        async with httpx.AsyncClient(timeout=timeouts, follow_redirects=False) as client:
            for hop in range(policy.max_redirects + 1):
                request = client.build_request(
                    method,
                    current.url,
                    headers=headers or None,
                    json=json_body,
                    content=body,
                )
                response = await client.send(request, stream=True)
                try:
                    location = response.headers.get("location")
                    if response.is_redirect and location:
                        if hop >= policy.max_redirects:
                            raise ToolError(
                                f"exceeded {policy.max_redirects} redirects",
                                redirects=hops,
                            )
                        hops.append(location)
                        current = validate_redirect(current.url, location, policy)
                        # A redirect after a write is followed as a read: the
                        # body is not replayed to a second destination.
                        if method not in ("GET", "HEAD"):
                            method = "GET"
                        body = json_body = None
                        continue

                    # Read with a hard ceiling, stopping at it rather than
                    # buffering everything and slicing afterwards.
                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > policy.max_response_bytes:
                            keep = policy.max_response_bytes - (total - len(chunk))
                            if keep > 0:
                                chunks.append(chunk[:keep])
                            truncated = True
                            break
                        chunks.append(chunk)

                    raw = b"".join(chunks)
                    try:
                        text = raw.decode(response.encoding or "utf-8", errors="replace")
                    except LookupError:
                        text = raw.decode("utf-8", errors="replace")

                    return {
                        "status": response.status_code,
                        "headers": dict(response.headers),
                        "body": text,
                        "url": str(response.url),
                        "bytes": len(raw),
                        "truncated": truncated,
                        "redirects": hops,
                        "resolved_addresses": list(current.addresses),
                    }
                finally:
                    await response.aclose()

        raise ToolError("request did not complete")  # pragma: no cover

    async def http_request(arguments, context):
        return await _perform(arguments, context, writing=False)

    async def http_send(arguments, context):
        return await _perform(arguments, context, writing=True)

    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string"},
            "headers": {"type": "object"},
            "json": {},
            "body": {"type": "string"},
        },
        "required": ["url"],
    }

    read_methods = [m for m in policy.allowed_methods if m in ("GET", "HEAD", "OPTIONS")]
    write_methods = [
        m for m in policy.allowed_methods if m in ("POST", "PUT", "PATCH", "DELETE")
    ]

    entries: list[ToolEntry] = []

    if read_methods:
        entries.append(
            (
                ToolSpec(
                    id="http.request",
                    name="http_request",
                    description=(
                        "Fetch a URL from an allowed host. Read-only: "
                        + ", ".join(read_methods)
                    ),
                    input_schema=schema,
                    permissions=[perms.NETWORK_READ],
                    source=ToolSource.NATIVE,
                    risk=RiskLevel.MEDIUM,
                    timeout_seconds=policy.total_timeout,
                ),
                http_request,
            )
        )

    # Registered only when the policy actually permits a write. An unusable
    # tool sitting in the model's list is an invitation to try it.
    if write_methods:
        entries.append(
            (
                ToolSpec(
                    id="http.send",
                    name="http_send",
                    description=(
                        "Send data to an allowed host: " + ", ".join(write_methods)
                    ),
                    input_schema=schema,
                    permissions=[perms.NETWORK_WRITE],
                    source=ToolSource.NATIVE,
                    risk=RiskLevel.HIGH,
                    timeout_seconds=policy.total_timeout,
                ),
                http_send,
            )
        )

    return entries
