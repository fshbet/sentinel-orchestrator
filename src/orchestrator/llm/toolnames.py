"""Tool-name translation for providers with stricter naming rules.

The platform namespaces tool ids with dots — ``fs.read_file``,
``orchestrator.emit_artifact``, ``mcp.<server>.<tool>`` — because a flat
namespace collides as soon as two MCP servers offer a tool of the same name.

The OpenAI function-calling schema allows only ``[a-zA-Z0-9_-]``. Providers that
enforce it (OpenAI, NVIDIA NIM, and others) reject every tool the platform
offers with a 400. Ollama happens to be lenient, which is exactly why this went
unnoticed until a strict provider was tried.

So names are translated on the way out and translated back on the way in. The
mapping is built per request and carried alongside it, which keeps it reversible
even when two different ids sanitise to the same string.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_ILLEGAL = re.compile(r"[^a-zA-Z0-9_-]")
MAX_NAME_LENGTH = 64


def sanitise(name: str) -> str:
    """Reduce a tool id to what a strict provider will accept."""
    cleaned = _ILLEGAL.sub("_", name).strip("_") or "tool"
    if len(cleaned) > MAX_NAME_LENGTH:
        # Keep the tail: the distinguishing part of a namespaced id is the end.
        cleaned = cleaned[-MAX_NAME_LENGTH:].lstrip("_")
    return cleaned


def build_mapping(tools: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Map provider-safe name -> original tool id, resolving collisions.

    Two ids can sanitise to the same string (``a.b`` and ``a_b``). A numeric
    suffix keeps them distinct so the reverse lookup stays unambiguous.
    """
    mapping: dict[str, str] = {}
    for tool in tools:
        original = str(tool.get("name", ""))
        if not original:
            continue
        candidate = sanitise(original)
        if mapping.get(candidate) not in (None, original):
            suffix = 2
            while mapping.get(f"{candidate}_{suffix}") not in (None, original):
                suffix += 1
            candidate = f"{candidate}_{suffix}"
        mapping[candidate] = original
    return mapping


def rename_tools(
    tools: Iterable[dict[str, Any]], mapping: dict[str, str]
) -> list[dict[str, Any]]:
    """Return the tool list with provider-safe names substituted in."""
    reverse = {original: safe for safe, original in mapping.items()}
    renamed = []
    for tool in tools:
        entry = dict(tool)
        original = str(entry.get("name", ""))
        entry["name"] = reverse.get(original, sanitise(original))
        renamed.append(entry)
    return renamed


def restore(name: str, mapping: dict[str, str]) -> str:
    """Translate a name the model returned back to the real tool id."""
    if name in mapping:
        return mapping[name]
    # A model occasionally echoes the original id even though it was offered the
    # sanitised one; accept that rather than failing the call.
    if name in mapping.values():
        return name
    return name
