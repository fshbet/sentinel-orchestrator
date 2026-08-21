"""Context compaction.

When the assembled context does not fit, something has to go. The order is
fixed and deterministic: drop the least relevant unpinned items, then compress
history, then truncate individual items. Only if all of that fails does the
platform ask a model to summarise, because a summarisation call is itself a
model call with its own cost and failure mode (spec section 22).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from ..core.domain.enums import ContextKind
from .budget import estimate_text_tokens

Summariser = Callable[[str, int], Awaitable[str]]


@dataclass
class ContextItem:
    """One addressable piece of context."""

    kind: ContextKind = ContextKind.HISTORY
    content: str = ""
    # Higher survives longer. Objectives and constraints sit near 1.0.
    relevance: float = 0.5
    # Pinned items are never dropped; they may still be truncated as a last
    # resort, and the truncation is always visible in the text.
    pinned: bool = False
    ref: str | None = None
    label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return estimate_text_tokens(self.content) + estimate_text_tokens(self.label)

    def render(self) -> str:
        return f"{self.label}\n{self.content}" if self.label else self.content


@dataclass
class CompactionReport:
    dropped: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    summarised: list[str] = field(default_factory=list)
    before_tokens: int = 0
    after_tokens: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.dropped or self.truncated or self.summarised)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dropped": self.dropped,
            "truncated": self.truncated,
            "summarised": self.summarised,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
        }


def total_tokens(items: Sequence[ContextItem]) -> int:
    return sum(item.tokens for item in items)


def truncate_text(text: str, max_tokens: int) -> str:
    """Keep the head and tail, mark the gap. Never silently lose the ending."""
    if max_tokens <= 0:
        return ""
    if estimate_text_tokens(text) <= max_tokens:
        return text
    from .budget import CHARS_PER_TOKEN

    budget_chars = int(max_tokens * CHARS_PER_TOKEN)
    if budget_chars <= 40:
        return text[:budget_chars]
    head = int(budget_chars * 0.6)
    tail = budget_chars - head - 30
    return f"{text[:head]}\n[... {len(text) - head - tail} characters omitted ...]\n{text[-tail:]}"


def compact(
    items: Sequence[ContextItem],
    max_tokens: int,
    *,
    min_item_tokens: int = 64,
) -> tuple[list[ContextItem], CompactionReport]:
    """Fit ``items`` into ``max_tokens`` deterministically."""
    working = list(items)
    report = CompactionReport(before_tokens=total_tokens(working))

    if report.before_tokens <= max_tokens:
        report.after_tokens = report.before_tokens
        return working, report

    # 1. Drop the least relevant unpinned items, lowest relevance first.
    droppable = sorted(
        (item for item in working if not item.pinned),
        key=lambda item: (item.relevance, -item.tokens),
    )
    for item in droppable:
        if total_tokens(working) <= max_tokens:
            break
        working.remove(item)
        report.dropped.append(item.label or item.kind.value)

    # 2. Fold remaining history into a single compressed note.
    if total_tokens(working) > max_tokens:
        history = [i for i in working if i.kind is ContextKind.HISTORY and not i.pinned]
        if len(history) > 1:
            merged_text = "\n".join(item.render() for item in history)
            for item in history:
                working.remove(item)
            remaining = max_tokens - total_tokens(working)
            working.append(
                ContextItem(
                    kind=ContextKind.SUMMARY,
                    label="Compressed history",
                    content=truncate_text(merged_text, max(min_item_tokens, remaining)),
                    relevance=0.4,
                )
            )
            report.summarised.append("history")

    # 3. Truncate the largest items until it fits, pinned ones last.
    guard = 0
    while total_tokens(working) > max_tokens and guard < 200:
        guard += 1
        candidates = sorted(
            working, key=lambda item: (item.pinned, -item.tokens)
        )
        target = candidates[0]
        overflow = total_tokens(working) - max_tokens
        allowed = max(min_item_tokens, target.tokens - overflow)
        if allowed >= target.tokens:
            allowed = max(min_item_tokens, int(target.tokens * 0.6))
        target.content = truncate_text(target.content, allowed)
        label = target.label or target.kind.value
        if label not in report.truncated:
            report.truncated.append(label)
        if target.tokens <= min_item_tokens and len(working) > 1 and not target.pinned:
            working.remove(target)
            report.dropped.append(label)

    report.after_tokens = total_tokens(working)
    return working, report


async def compact_with_summariser(
    items: Sequence[ContextItem],
    max_tokens: int,
    summariser: Summariser,
    *,
    min_item_tokens: int = 64,
) -> tuple[list[ContextItem], CompactionReport]:
    """Deterministic compaction first; a model summary only if still oversized."""
    working, report = compact(items, max_tokens, min_item_tokens=min_item_tokens)
    if report.after_tokens <= max_tokens:
        return working, report

    bulky = [i for i in working if not i.pinned and i.tokens > min_item_tokens * 2]
    for item in sorted(bulky, key=lambda i: -i.tokens):
        if total_tokens(working) <= max_tokens:
            break
        target_tokens = max(min_item_tokens, item.tokens // 3)
        try:
            item.content = await summariser(item.content, target_tokens)
        except Exception:  # noqa: BLE001 - fall back to truncation, never fail
            item.content = truncate_text(item.content, target_tokens)
        report.summarised.append(item.label or item.kind.value)

    report.after_tokens = total_tokens(working)
    if report.after_tokens > max_tokens:
        working, second = compact(working, max_tokens, min_item_tokens=min_item_tokens)
        report.dropped.extend(second.dropped)
        report.truncated.extend(second.truncated)
        report.after_tokens = second.after_tokens
    return working, report
