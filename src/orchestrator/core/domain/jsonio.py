"""Finding JSON inside model output.

Models wrap JSON in preambles ("Here is the result:"), fence it in code blocks,
or trail it with commentary. The JSON is genuinely there; requiring byte-perfect
formatting would fail work that is actually correct.

This lives in the domain layer rather than under ``llm`` so that validators can
use it without the validation subsystem depending on the model subsystem.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any | None:
    """Return the first JSON value in ``text``, or None if there is none.

    Tries, in order: the whole string, any fenced block, then the first
    balanced object or array. Returns None rather than guessing when nothing
    parses.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    stripped = text.strip()

    # 1. The whole thing.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Fenced blocks, in order of appearance.
    for block in _FENCE.findall(stripped):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue

    # 3. The first balanced object or array, respecting strings and escapes so
    #    a brace inside a string value does not end the scan early.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(stripped)):
            char = stripped[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stripped[start : index + 1])
                    except json.JSONDecodeError:
                        break
    return None


def coerce_json(value: Any) -> Any:
    """Return ``value`` as parsed JSON when it is a string that contains some."""
    if isinstance(value, str):
        extracted = extract_json(value)
        if extracted is not None:
            return extracted
    return value
