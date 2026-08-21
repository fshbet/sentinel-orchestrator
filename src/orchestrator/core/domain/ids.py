"""Identifier generation.

Identifiers are prefixed so a bare id in a log line is self-describing, and
sortable-by-time so listings have a natural order without a join.
"""

from __future__ import annotations

import os
import time
import uuid

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(number: int, width: int) -> str:
    out = []
    while number:
        number, rem = divmod(number, 36)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out)).rjust(width, "0")


def new_id(prefix: str) -> str:
    """Return a time-ordered, collision-resistant id such as ``exe_l3k2..``."""
    stamp = _b36(int(time.time() * 1000), 9)
    rand = _b36(int.from_bytes(os.urandom(6), "big"), 10)
    return f"{prefix}_{stamp}{rand}"


def deterministic_id(prefix: str, *parts: str) -> str:
    """Return a stable id derived from ``parts`` (used for idempotency keys)."""
    name = "\x1f".join(parts)
    digest = uuid.uuid5(uuid.NAMESPACE_URL, name).hex[:20]
    return f"{prefix}_{digest}"
