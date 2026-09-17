"""One-in-flight-per-session admission gate."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class SessionAdmission:
    """Serialize concurrent turns that share the same session id."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._conditions: dict[str, asyncio.Condition] = {}
        self._active: set[str] = set()

    def _condition(self, session: str) -> asyncio.Condition:
        cond = self._conditions.get(session)
        if cond is None:
            cond = asyncio.Condition(self._lock)
            self._conditions[session] = cond
        return cond

    @asynccontextmanager
    async def admit(self, session: str) -> AsyncIterator[None]:
        cond = self._condition(session)
        async with cond:
            while session in self._active:
                await cond.wait()
            self._active.add(session)
        try:
            yield
        finally:
            async with cond:
                self._active.discard(session)
                cond.notify_all()
