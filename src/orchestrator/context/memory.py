"""Memory tiers.

Execution state, working memory, long-term memory, project knowledge, and
artifacts are kept apart on purpose (spec section 24). Conversation history is
the *least* durable of these, not the store of record: putting everything into
a transcript is what makes long executions fall over.

Retrieval is selective. The default matcher is lexical overlap, which is
dependency-free and predictable; an embedding-backed matcher can be supplied
without changing any caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

from ..core.domain.enums import MemoryTier
from ..core.domain.ids import new_id
from ..core.domain.serde import utcnow

_WORD = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


@dataclass
class MemoryEntry:
    id: str = field(default_factory=lambda: new_id("mem"))
    tier: MemoryTier = MemoryTier.WORKING
    key: str = ""
    value: Any = None
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    execution_id: str | None = None
    task_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    hits: int = 0

    def text(self) -> str:
        return f"{self.key} {self.summary} {self.value}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tier": self.tier.value,
            "key": self.key,
            "value": self.value,
            "summary": self.summary,
            "tags": list(self.tags),
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "created_at": self.created_at.isoformat(),
        }


Scorer = Callable[[str, MemoryEntry], float]


def lexical_score(query: str, entry: MemoryEntry) -> float:
    """Jaccard-style overlap, biased towards the key."""
    query_terms = _tokenize(query)
    if not query_terms:
        return 0.0
    key_terms = _tokenize(entry.key)
    body_terms = _tokenize(entry.summary or str(entry.value))
    key_overlap = len(query_terms & key_terms) / len(query_terms)
    body_overlap = (
        len(query_terms & body_terms) / len(query_terms) if body_terms else 0.0
    )
    tag_overlap = len(query_terms & _tokenize(" ".join(entry.tags))) / len(query_terms)
    return key_overlap * 2.0 + body_overlap + tag_overlap * 0.5


class MemoryStore:
    """Tiered, selectively retrievable memory."""

    def __init__(self, *, scorer: Scorer | None = None, max_working: int = 500) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._scorer = scorer or lexical_score
        self.max_working = max_working

    # -- writing -----------------------------------------------------------

    def remember(
        self,
        key: str,
        value: Any,
        *,
        tier: MemoryTier = MemoryTier.WORKING,
        summary: str = "",
        tags: Sequence[str] = (),
        execution_id: str | None = None,
        task_id: str | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            tier=tier,
            key=key,
            value=value,
            summary=summary or (str(value)[:200] if value is not None else ""),
            tags=list(tags),
            execution_id=execution_id,
            task_id=task_id,
        )
        # A repeated key in the same tier updates rather than accumulating.
        for existing in list(self._entries.values()):
            if existing.key == key and existing.tier is tier and existing.execution_id == execution_id:
                del self._entries[existing.id]
        self._entries[entry.id] = entry
        self._evict()
        return entry

    def forget(self, entry_id: str) -> None:
        self._entries.pop(entry_id, None)

    def clear(self, *, tier: MemoryTier | None = None, execution_id: str | None = None) -> int:
        doomed = [
            entry_id
            for entry_id, entry in self._entries.items()
            if (tier is None or entry.tier is tier)
            and (execution_id is None or entry.execution_id == execution_id)
        ]
        for entry_id in doomed:
            del self._entries[entry_id]
        return len(doomed)

    def _evict(self) -> None:
        working = [e for e in self._entries.values() if e.tier is MemoryTier.WORKING]
        if len(working) <= self.max_working:
            return
        # Drop the least useful: fewest hits, then oldest.
        working.sort(key=lambda e: (e.hits, e.created_at))
        for entry in working[: len(working) - self.max_working]:
            self._entries.pop(entry.id, None)

    # -- reading -----------------------------------------------------------

    def get(self, key: str, *, execution_id: str | None = None) -> MemoryEntry | None:
        for entry in self._entries.values():
            if entry.key == key and (
                execution_id is None or entry.execution_id == execution_id
            ):
                entry.hits += 1
                return entry
        return None

    def all(
        self,
        *,
        tier: MemoryTier | None = None,
        execution_id: str | None = None,
    ) -> list[MemoryEntry]:
        return [
            entry
            for entry in self._entries.values()
            if (tier is None or entry.tier is tier)
            and (execution_id is None or entry.execution_id in (None, execution_id))
        ]

    def recall(
        self,
        query: str,
        *,
        limit: int = 8,
        tiers: Iterable[MemoryTier] = (
            MemoryTier.WORKING,
            MemoryTier.PROJECT,
            MemoryTier.LONG_TERM,
        ),
        execution_id: str | None = None,
        min_score: float = 0.05,
    ) -> list[MemoryEntry]:
        """Return the most relevant entries, not everything that exists."""
        tier_set = set(tiers)
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in self._entries.values():
            if entry.tier not in tier_set:
                continue
            if execution_id is not None and entry.execution_id not in (None, execution_id):
                continue
            score = self._scorer(query, entry)
            if score >= min_score:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1].created_at))
        selected = [entry for _, entry in scored[:limit]]
        for entry in selected:
            entry.hits += 1
        return selected

    def snapshot(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self._entries.values()]

    def load(self, entries: Iterable[dict[str, Any]]) -> int:
        count = 0
        for data in entries:
            entry = MemoryEntry(
                id=data.get("id") or new_id("mem"),
                tier=MemoryTier(data.get("tier", "working")),
                key=data.get("key", ""),
                value=data.get("value"),
                summary=data.get("summary", ""),
                tags=list(data.get("tags", [])),
                execution_id=data.get("execution_id"),
                task_id=data.get("task_id"),
            )
            self._entries[entry.id] = entry
            count += 1
        return count
