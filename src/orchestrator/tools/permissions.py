"""Permission vocabulary.

Permissions are plain dotted strings so they can be granted with globs
(``fs.*``) and extended by plugins without touching the core. The constants
below are the ones the built-in tooling uses; nothing prevents a plugin from
inventing its own namespace.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# Filesystem
FS_READ = "fs.read"
FS_WRITE = "fs.write"
FS_DELETE = "fs.delete"

# Process execution
PROCESS_EXECUTE = "process.execute"

# Network / external systems
NETWORK_READ = "network.read"
NETWORK_WRITE = "network.write"

# Orchestration internals
STATE_READ = "state.read"
STATE_WRITE = "state.write"
MEMORY_READ = "memory.read"
MEMORY_WRITE = "memory.write"
ARTIFACT_WRITE = "artifact.write"

# MCP
MCP_INVOKE = "mcp.invoke"

# Secrets. Never granted by default, and never logged.
SECRET_READ = "secret.read"  # noqa: S105 - a permission name, not a credential

ALL = (
    FS_READ,
    FS_WRITE,
    FS_DELETE,
    PROCESS_EXECUTE,
    NETWORK_READ,
    NETWORK_WRITE,
    STATE_READ,
    STATE_WRITE,
    MEMORY_READ,
    MEMORY_WRITE,
    ARTIFACT_WRITE,
    MCP_INVOKE,
    SECRET_READ,
)

# Granted to an agent that declares no permissions at all: enough to record
# results, not enough to touch anything outside the orchestrator.
SAFE_DEFAULTS = (STATE_READ, MEMORY_READ, MEMORY_WRITE, ARTIFACT_WRITE)


def expand(permissions: Iterable[str]) -> set[str]:
    """Expand namespace grants such as ``fs`` into their concrete members."""
    out: set[str] = set()
    for permission in permissions:
        if permission in ALL or "." in permission or "*" in permission:
            out.add(permission)
        else:
            out.update(p for p in ALL if p.split(".", 1)[0] == permission)
            out.add(permission)
    return out


def missing(required: Sequence[str], granted: Iterable[str]) -> list[str]:
    import fnmatch

    granted_set = expand(granted)
    return [
        permission
        for permission in required
        if permission not in granted_set
        and not any(fnmatch.fnmatch(permission, g) for g in granted_set)
    ]
