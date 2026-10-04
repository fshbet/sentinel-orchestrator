"""Structured logging.

JSON lines by default so machine consumption is the norm and human reading is
the fallback (spec section 42). Observability is never a hard dependency: with
no configuration the platform logs nothing beyond warnings.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

LOGGER_NAME = "orchestrator"

# Keys whose values are replaced before anything reaches a log sink.
# Matched as whole words within a key, so "access_token" is redacted but
# "input_tokens" (a usage count) is not. Naive substring matching destroys
# observability: every token *count* would disappear along with the secrets.
REDACT_WORDS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "bearer",
        "cookie",
    }
)

# Matched as substrings, for compound names that survive word splitting.
REDACT_PATTERNS = frozenset(
    {
        "api_key",
        "private_key",
        "access_token",
        "refresh_token",
        "client_secret",
        "secret_key",
        "session_key",
        "auth_token",
    }
)

REDACTED = "[redacted]"

# Credential shapes recognised inside string *values*, not just key names.
#
# Key-based redaction misses the common case: a secret that arrives inside an
# error message, a tool's stdout, or a model's echoed prompt, where the key is
# "message" or "stdout" and tells you nothing.
#
# Every pattern here is anchored on a vendor prefix or a structural marker
# rather than on entropy. An entropy heuristic flags git SHAs, base64 payloads
# and UUIDs, and a redactor that eats legitimate content gets switched off —
# at which point it protects nothing. Missing an unrecognised format is the
# better failure: key-based redaction still covers the fields where secrets
# are normally carried.
VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Specific vendor prefixes first: they share the sk- stem, and the
    # first match wins. The value is redacted either way, but the label is
    # what tells an operator which credential to rotate.
    ("openrouter", re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{16,}")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("nvidia", re.compile(r"\bnvapi-[A-Za-z0-9_-]{16,}")),
    ("github", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    # Structural rather than vendor-specific.
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
            r"[\s\S]*?-----END (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
        ),
    ),
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
)

# Below this length a "secret" is almost always a placeholder or a test
# fixture, and redacting it makes failures harder to read.
_MIN_VALUE_LENGTH = 12


def redact_text(text: str) -> str:
    """Replace credential-shaped substrings inside a string value."""
    if not text or len(text) < _MIN_VALUE_LENGTH:
        return text
    for label, pattern in VALUE_PATTERNS:
        text = pattern.sub(f"[redacted:{label}]", text)
    return text


_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def is_sensitive_key(key: Any) -> bool:
    """Whether a key names something that must never be recorded."""
    lowered = str(key).lower()
    if any(pattern in lowered for pattern in REDACT_PATTERNS):
        return True
    return bool(REDACT_WORDS & set(_WORD_SPLIT.split(lowered)))


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively blank out anything that looks like a secret."""
    if _depth > 12:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                out[str(key)] = REDACTED
            else:
                out[str(key)] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        payload["message"] = redact_text(payload["message"])
        return json.dumps(payload, default=str)


def configure_logging(level: str = "warning", *, json_output: bool = True) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.WARNING))
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JSONFormatter() if json_output else logging.Formatter("%(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def log(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    logger.log(level, message, extra={"context": context})
