"""Token estimation and budgeting.

The platform must never knowingly exceed a model's context limit (spec section
23). Estimation is deliberately conservative and tokenizer-free: an exact count
would require a per-provider tokenizer dependency, and over-estimating is the
safe direction to be wrong in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Empirical average across common tokenizers for prose and code. Rounded down
# so the estimate errs high.
CHARS_PER_TOKEN = 3.6
# Per-message protocol overhead (role markers, delimiters).
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_SCHEMA_OVERHEAD_TOKENS = 12


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


def estimate_value_tokens(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return estimate_text_tokens(value)
    if isinstance(value, (int, float, bool)):
        return 2
    import json

    try:
        return estimate_text_tokens(json.dumps(value, default=str))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return estimate_text_tokens(str(value))


def estimate_tokens(request: Any) -> int:
    """Estimate the input tokens of a ``CompletionRequest``."""
    total = estimate_text_tokens(getattr(request, "system", "") or "")
    for message in getattr(request, "messages", []) or []:
        total += MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(
            getattr(message, "content", "") or ""
        )
    for tool in getattr(request, "tools", []) or []:
        total += TOOL_SCHEMA_OVERHEAD_TOKENS + estimate_value_tokens(tool)
    schema = getattr(request, "response_schema", None)
    if schema:
        total += estimate_value_tokens(schema)
    return total


@dataclass
class ContextBudget:
    """How many tokens each part of a prompt may occupy."""

    context_window: int
    reserved_output: int = 2048
    # Safety margin against estimation error.
    safety_margin: float = 0.10

    @property
    def total_input(self) -> int:
        usable = self.context_window - self.reserved_output
        return max(256, int(usable * (1.0 - self.safety_margin)))

    def split(
        self,
        *,
        system: float = 0.10,
        tools: float = 0.15,
        task: float = 0.30,
        history: float = 0.30,
        memory: float = 0.15,
    ) -> dict[str, int]:
        """Allocate the input budget across context categories."""
        weights = {
            "system": system,
            "tools": tools,
            "task": task,
            "history": history,
            "memory": memory,
        }
        total = sum(weights.values()) or 1.0
        available = self.total_input
        return {name: int(available * (w / total)) for name, w in weights.items()}

    def fits(self, estimated_tokens: int) -> bool:
        return estimated_tokens <= self.total_input

    def overflow(self, estimated_tokens: int) -> int:
        return max(0, estimated_tokens - self.total_input)

    @classmethod
    def for_model(cls, model: Any, *, reserved_output: int | None = None) -> "ContextBudget":
        window = int(getattr(model, "context_window", 8192) or 8192)
        reserve = reserved_output or int(getattr(model, "max_output_tokens", 2048) or 2048)
        # Never reserve more than half the window for output.
        reserve = min(reserve, window // 2)
        return cls(context_window=window, reserved_output=reserve)
