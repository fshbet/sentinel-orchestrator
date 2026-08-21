"""Resource locking.

Parallel agents that touch the same file, repository, database, or external
system must not race. Tasks declare the resources they mutate and the scheduler
acquires them before starting work (spec section 32).

Locks are always acquired in sorted order, which makes deadlock between two
tasks holding half of each other's resources structurally impossible.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Sequence


class ResourceLockManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, str] = {}
        self._guard = asyncio.Lock()

    def _lock_for(self, resource: str) -> asyncio.Lock:
        lock = self._locks.get(resource)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[resource] = lock
        return lock

    def held_by(self, resource: str) -> str | None:
        return self._holders.get(resource)

    def busy(self, resources: Sequence[str]) -> list[str]:
        return [r for r in resources if self._locks.get(r, None) and self._locks[r].locked()]

    def available(self, resources: Sequence[str]) -> bool:
        return not self.busy(resources)

    @asynccontextmanager
    async def acquire(
        self, resources: Sequence[str], *, holder: str = ""
    ) -> AsyncIterator[None]:
        ordered = sorted(set(resources))
        acquired: list[str] = []
        try:
            for resource in ordered:
                async with self._guard:
                    lock = self._lock_for(resource)
                await lock.acquire()
                self._holders[resource] = holder
                acquired.append(resource)
            yield
        finally:
            for resource in reversed(acquired):
                self._holders.pop(resource, None)
                self._locks[resource].release()

    def snapshot(self) -> dict[str, str]:
        """Resource -> current holder, for observability."""
        return {
            resource: self._holders.get(resource, "")
            for resource, lock in self._locks.items()
            if lock.locked()
        }
