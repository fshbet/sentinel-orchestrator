"""Recovering tool calls that arrived in the wrong channel.

Some models — Llama 3.x and several small local models especially — emit a
tool call as JSON in the *content* field instead of through the provider's
structured ``tool_calls`` channel. Left alone this surfaces as an answer that
looks like ``{"name": "json", "parameters": {"content": "..."}}``, which is
neither a usable answer nor a usable tool call.

Two distinct cases, handled differently on purpose:

* The name matches a tool the caller actually offered — the model meant to call
  it, so promote it to a real tool call.
* The name matches nothing — the model wrapped its *answer* in an invented
  envelope. Unwrap the payload rather than promoting a call to a tool that does
  not exist.

Anything that does not clearly match either shape is left exactly as it was.
Guessing at ambiguous output would be worse than passing it through.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .base import ToolCallRequest

# Keys different models use for the same two things.
_NAME_KEYS = ("name", "tool", "function", "tool_name")
_ARGUMENT_KEYS = ("parameters", "arguments", "args", "input", "params")
# Where an unwrapped answer usually lives once the envelope is removed.
_PAYLOAD_KEYS = ("content", "text", "answer", "result", "summary", "output", "response")


def _parse_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
        stripped = stripped.rsplit("```", 1)[0]
        stripped = stripped.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _envelope(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Read ``(name, arguments)`` out of a tool-call-shaped object."""
    name = next(
        (str(payload[key]) for key in _NAME_KEYS if isinstance(payload.get(key), str)),
        None,
    )
    if not name:
        return None
    arguments: dict[str, Any] = {}
    for key in _ARGUMENT_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            arguments = value
            break
        if isinstance(value, str):
            nested = _parse_object(value)
            if nested is not None:
                arguments = nested
                break
    # A bare {"name": "..."} with nothing else is prose, not a call.
    if not arguments and not any(k in payload for k in _ARGUMENT_KEYS):
        return None
    return name, arguments


def recover(
    text: str,
    *,
    offered_tools: Sequence[str] = (),
    existing_calls: Sequence[ToolCallRequest] = (),
) -> tuple[str, list[ToolCallRequest]]:
    """Return ``(text, extra_tool_calls)`` after untangling a misplaced call.

    ``offered_tools`` is what the caller actually put in front of the model; it
    is the only reliable way to tell a real call from an invented envelope.
    """
    if existing_calls or not text:
        # The provider already gave us calls through the proper channel.
        return text, []

    payload = _parse_object(text)
    if payload is None:
        return text, []

    envelope = _envelope(payload)
    if envelope is None:
        return text, []

    name, arguments = envelope

    if name in set(offered_tools):
        # The model meant to call this. Honour it.
        return "", [ToolCallRequest(id="recovered_1", name=name, arguments=arguments)]

    # An envelope around the answer. Unwrap rather than inventing a tool.
    for key in _PAYLOAD_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value, []
    if arguments:
        return json.dumps(arguments, indent=2), []
    return text, []
