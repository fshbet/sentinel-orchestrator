"""Resource governance.

Budgets are enforced by counting, in software, before an action happens - never
by asking a model to be careful (spec sections 64, 95, 106).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ...errors import ResourceLimitExceeded
from ..domain.models import ResourceLimits, Usage


@dataclass
class LimitStatus:
    exceeded: bool
    limit: str = ""
    used: float = 0.0
    allowed: float = 0.0

    @property
    def message(self) -> str:
        if not self.exceeded:
            return "within limits"
        return f"{self.limit} limit reached: {self.used} of {self.allowed}"


class LimitGuard:
    """Tracks usage for one execution and refuses work that would exceed it."""

    def __init__(self, limits: ResourceLimits, *, usage: Usage | None = None) -> None:
        self.limits = limits
        self.usage = usage or Usage()
        self._started = time.monotonic()

    # -- accounting --------------------------------------------------------

    def add(self, usage: Usage) -> Usage:
        self.usage = self.usage.add(usage)
        return self.usage

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def snapshot(self) -> Usage:
        snapshot = Usage(
            model_calls=self.usage.model_calls,
            tool_calls=self.usage.tool_calls,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
            cost=self.usage.cost,
            external_requests=self.usage.external_requests,
            wall_seconds=self.elapsed,
        )
        return snapshot

    # -- checks ------------------------------------------------------------

    def check(self) -> LimitStatus:
        limits = self.limits
        usage = self.usage

        if self.elapsed > limits.max_wall_seconds:
            return LimitStatus(
                True, "wall clock", round(self.elapsed, 1), limits.max_wall_seconds
            )
        if usage.model_calls >= limits.max_model_calls:
            return LimitStatus(
                True, "model calls", usage.model_calls, limits.max_model_calls
            )
        if usage.tool_calls >= limits.max_tool_calls:
            return LimitStatus(True, "tool calls", usage.tool_calls, limits.max_tool_calls)
        if usage.input_tokens + usage.output_tokens >= limits.max_tokens:
            return LimitStatus(
                True,
                "tokens",
                usage.input_tokens + usage.output_tokens,
                limits.max_tokens,
            )
        if limits.max_cost is not None and usage.cost >= limits.max_cost:
            return LimitStatus(True, "cost", round(usage.cost, 4), limits.max_cost)
        if usage.external_requests >= limits.max_external_requests:
            return LimitStatus(
                True,
                "external requests",
                usage.external_requests,
                limits.max_external_requests,
            )
        return LimitStatus(False)

    def within_limits(self) -> bool:
        return not self.check().exceeded

    def raise_if_exceeded(self, *, execution_id: str = "") -> None:
        status = self.check()
        if status.exceeded:
            raise ResourceLimitExceeded(
                status.message,
                execution_id=execution_id,
                limit=status.limit,
                used=status.used,
                allowed=status.allowed,
            )

    def remaining(self) -> dict[str, float]:
        limits = self.limits
        usage = self.usage
        remaining = {
            "wall_seconds": max(0.0, limits.max_wall_seconds - self.elapsed),
            "model_calls": max(0, limits.max_model_calls - usage.model_calls),
            "tool_calls": max(0, limits.max_tool_calls - usage.tool_calls),
            "tokens": max(0, limits.max_tokens - usage.input_tokens - usage.output_tokens),
            "external_requests": max(
                0, limits.max_external_requests - usage.external_requests
            ),
        }
        if limits.max_cost is not None:
            remaining["cost"] = max(0.0, limits.max_cost - usage.cost)
        return remaining
