"""Process execution policy.

Running a subprocess is the most privileged thing this platform does. Every
other tool is bounded by what the tool itself implements; a subprocess is
bounded only by what the operating system will let the orchestrator's own user
do. The controls here exist because the model chooses the argv.

What each one is actually stopping:

* **The environment is not inherited.** This is the sharpest edge. A child
  process started with the default environment can read ``OPENROUTER_API_KEY``,
  ``NVIDIA_API_KEY`` and ``ORCHESTRATOR_API_TOKEN`` — no exploit needed, just
  ``os.environ``. So the child gets a constructed environment containing a
  minimal PATH and nothing else unless a variable is named in
  ``environment_allowlist``.
* **The allowlist is an identity, not a name.** Matching on basename means
  permitting ``python`` also permits ``/tmp/attacker/python``. Entries are
  resolved to real paths and compared as such.
* **Shell interpreters are refused.** An allowlist containing ``bash`` is an
  allowlist containing everything, since the shell will run whatever it is
  handed. Permitting one requires saying so by name.
* **No command is run through a shell.** ``create_subprocess_exec`` takes argv
  directly, so metacharacters are inert data rather than syntax.
* **The bounds are not negotiable downward by the caller.** The old code took
  ``timeout`` from the arguments the model supplied, which made the configured
  limit advisory. A caller may shorten a limit, never extend it.

What this is not: a sandbox. It confines a cooperative process, not a hostile
one. A permitted executable can still open sockets, read anything the
orchestrator's user can read, and exhaust CPU. Real isolation requires a
container or VM boundary the operating system enforces, which is what
``IsolationLevel`` distinguishes — see ``docs/security.md``.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..errors import PermissionDenied

# Interpreters that execute arbitrary text. Permitting one of these makes
# every other entry in an allowlist decorative.
SHELL_INTERPRETERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "fish",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "ash",
        "cmd",
        "cmd.exe",
        "command.com",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "busybox",
        "env",
        "xargs",
        "nohup",
        "timeout",
        "nice",
        "setsid",
    }
)

# Variables that turn a permitted executable into arbitrary code execution by
# changing what it loads before its own main() runs. Never passable, whatever
# an allowlist says.
FORBIDDEN_ENVIRONMENT = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "PERL5OPT",
        "RUBYOPT",
        "BASH_ENV",
        "ENV",
        "IFS",
    }
)

# Enough PATH for a permitted executable to find its own helpers. Not the
# orchestrator's PATH, which may point at developer tooling.
_MINIMAL_PATH = (
    "C:\\Windows\\system32;C:\\Windows"
    if os.name == "nt"
    else "/usr/local/bin:/usr/bin:/bin"
)


@dataclass(frozen=True)
class ExecPolicy:
    """What a task may execute, and under what constraints."""

    allowed_commands: tuple[str, ...] = ()
    root: str = "."

    # Named opt-in. See SHELL_INTERPRETERS.
    allow_shell_interpreters: bool = False

    # Empty means the child gets PATH and nothing else.
    environment_allowlist: tuple[str, ...] = ()

    denied_argument_patterns: tuple[str, ...] = ()
    max_arguments: int = 64

    timeout: float = 120.0
    max_output_bytes: int = 100_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_commands", tuple(self.allowed_commands))
        object.__setattr__(self, "environment_allowlist", tuple(self.environment_allowlist))

    def validate(self) -> None:
        """Check the configuration itself. Called before any process starts.

        Config-time rather than call-time: a policy that permits a shell is
        wrong the moment it is written, and finding that out on first traffic
        is finding out too late.
        """
        if not self.allowed_commands:
            raise ValueError(
                "process tools require a non-empty allowed_commands list; "
                "there is no 'run anything' mode"
            )

        if not self.allow_shell_interpreters:
            offenders = [
                command
                for command in self.allowed_commands
                if Path(command).name.lower() in SHELL_INTERPRETERS
                or command.lower() in SHELL_INTERPRETERS
            ]
            if offenders:
                raise ValueError(
                    f"allowed_commands contains shell interpreter(s): "
                    f"{', '.join(offenders)}. A shell will run whatever it is "
                    f"given, so allowing one allows everything. Set "
                    f"allow_shell_interpreters: true only if that is the intent."
                )

        forbidden = sorted(
            set(v.upper() for v in self.environment_allowlist) & FORBIDDEN_ENVIRONMENT
        )
        if forbidden:
            raise ValueError(
                f"environment_allowlist contains {', '.join(forbidden)}, which "
                f"change what a process loads before it runs and would make any "
                f"permitted executable arbitrary code execution"
            )

        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        if self.max_arguments <= 0:
            raise ValueError("max_arguments must be positive")

        for pattern in self.denied_argument_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"denied_argument_patterns contains an invalid regex {pattern!r}: {exc}"
                ) from exc

    def resolved_allowlist(self) -> dict[str, str]:
        """Configured entries mapped to the real paths they name.

        An entry that does not resolve is kept rather than dropped, so a typo
        surfaces as "not in the allowed list" at call time instead of silently
        shrinking the allowlist to nothing.
        """
        resolved: dict[str, str] = {}
        for command in self.allowed_commands:
            found = self._locate(command)
            if found is not None:
                try:
                    resolved[str(Path(found).resolve())] = command
                except OSError:  # pragma: no cover - unusual filesystems
                    resolved[found] = command
        return resolved

    def allowed_access_paths(self) -> set[str]:
        """The paths the allowlist names, before any link is followed.

        ``resolved_allowlist`` answers "which file is this", by real path, and
        that is what stops a copy or a link planted under a permitted name
        from being run. It does not stop the inverse. Allowing a symlink would
        otherwise also allow its target under the target's own name, because
        the two share a real path - and an operator who permitted
        ``/usr/local/bin/safe-wrapper`` has not permitted the interpreter it
        happens to point at.

        So an executable has to satisfy both: the file must be the permitted
        file, and the path used to reach it must be a permitted path. Absolute
        but deliberately *not* resolved, since resolving is the thing being
        guarded against here.
        """
        paths: set[str] = set()
        for command in self.allowed_commands:
            found = self._locate(command)
            if found is not None:
                paths.add(os.path.abspath(found))
        return paths

    @staticmethod
    def _locate(command: str) -> str | None:
        """Where a configured entry is found on disk, or None.

        An entry that does not resolve is reported as missing rather than
        dropped, so a typo surfaces as "not in the allowed list" at call time
        instead of silently shrinking the allowlist to nothing.
        """
        found = shutil.which(command)
        if found is None and Path(command).exists():
            found = command
        return found

    def check_arguments(self, argv: Sequence[str]) -> None:
        if len(argv) > self.max_arguments:
            raise PermissionDenied(
                f"command has {len(argv)} arguments, more than the "
                f"{self.max_arguments} permitted",
                count=len(argv),
                limit=self.max_arguments,
            )
        for pattern in self.denied_argument_patterns:
            compiled = re.compile(pattern)
            for argument in argv:
                if compiled.search(argument):
                    raise PermissionDenied(
                        f"argument {argument!r} matches the denied pattern {pattern!r}",
                        argument=argument,
                        pattern=pattern,
                    )

    def effective_timeout(self, requested: float | None) -> float:
        """The caller may shorten a bound, never extend it."""
        if requested is None:
            return self.timeout
        try:
            value = float(requested)
        except (TypeError, ValueError):
            return self.timeout
        if value <= 0:
            return self.timeout
        return min(value, self.timeout)

    def describe(self) -> dict[str, object]:
        return {
            "allowed_commands": list(self.allowed_commands),
            "shell_interpreters_permitted": self.allow_shell_interpreters,
            "environment_allowlist": list(self.environment_allowlist),
            "environment_inherited": False,
            "timeout": self.timeout,
            "max_output_bytes": self.max_output_bytes,
            "root": self.root,
        }


def resolve_executable(command: str, policy: ExecPolicy) -> str:
    """Resolve argv[0] and confirm it is the executable that was permitted.

    Comparison is on the resolved path, so a copy or a symlink sitting under a
    permitted *name* is not a permitted *executable*.
    """
    found = shutil.which(command)
    if found is None and Path(command).exists():
        found = command
    if found is None:
        raise PermissionDenied(
            f"executable {command!r} was not found",
            command=command,
            allowed=list(policy.allowed_commands),
        )

    try:
        real = str(Path(found).resolve())
    except OSError as exc:  # pragma: no cover - unusual filesystems
        raise PermissionDenied(f"could not resolve {command!r}: {exc}") from exc

    allowed = policy.resolved_allowlist()
    if real not in allowed:
        raise PermissionDenied(
            f"executable {command!r} resolves to {real}, which is not in the allowed list",
            command=command,
            resolved=real,
            allowed=list(policy.allowed_commands),
        )

    # The file is permitted. The path used to reach it has to be permitted too,
    # or allowing a symlink would silently allow its target under the target's
    # own name: both share a real path, so the check above cannot tell them
    # apart. See ExecPolicy.allowed_access_paths.
    if os.path.abspath(found) not in policy.allowed_access_paths():
        raise PermissionDenied(
            f"executable {command!r} is the permitted file {real} reached by a "
            f"path that is not in the allowed list",
            command=command,
            resolved=real,
            accessed=os.path.abspath(found),
            allowed=list(policy.allowed_commands),
        )

    if not policy.allow_shell_interpreters:
        if Path(real).name.lower() in SHELL_INTERPRETERS:
            raise PermissionDenied(
                f"{Path(real).name} is a shell interpreter and would run "
                f"arbitrary commands",
                command=command,
            )

    return real


def build_environment(
    policy: ExecPolicy, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Construct the child's environment from nothing.

    Built up rather than filtered down: a denylist has to anticipate every
    secret-shaped variable name, and it only takes one it did not anticipate.
    """
    env: dict[str, str] = {"PATH": _MINIMAL_PATH}

    # Windows processes fail in obscure ways without these, and none of them
    # carry credentials.
    if os.name == "nt":
        for required in (
            "SYSTEMROOT",
            "COMSPEC",
            "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE",
            "TEMP",
            "TMP",
        ):
            value = os.environ.get(required)
            if value:
                env[required] = value

    for name in policy.environment_allowlist:
        upper = name.upper()
        if upper in FORBIDDEN_ENVIRONMENT:
            continue
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    for name, value in (extra or {}).items():
        upper = name.upper()
        if upper in FORBIDDEN_ENVIRONMENT:
            raise PermissionDenied(
                f"environment variable {name} may never be passed to a "
                f"subprocess: it changes what the process loads before it runs",
                variable=name,
            )
        if name not in policy.environment_allowlist:
            raise PermissionDenied(
                f"environment variable {name} is not in the allowed list; "
                f"add it to tools.process.environment_allowlist to pass it",
                variable=name,
                allowed=list(policy.environment_allowlist),
            )
        env[name] = str(value)

    return env
