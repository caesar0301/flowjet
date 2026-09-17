"""Per-session interaction_mode pin (RFC-003)."""

from __future__ import annotations

import asyncio
from typing import Any

from flowjet.core.backends.isolation.errors import IsolationError


def _requested_mode(metadata: dict[str, Any] | None) -> str:
    raw = (metadata or {}).get("interaction_mode", "agent")
    return raw if raw in ("agent", "ask") else "agent"


class InteractionModeGate:
    """First turn for a session pins agent|ask; later flips are rejected."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pinned: dict[str, str] = {}

    async def resolve(self, session: str, metadata: dict[str, Any] | None) -> str:
        requested = _requested_mode(metadata)
        async with self._lock:
            pinned = self._pinned.get(session)
            if pinned is None:
                self._pinned[session] = requested
                return requested
            if requested != pinned:
                raise IsolationError(
                    f"session '{session}' is pinned to interaction_mode={pinned!r}; "
                    f"got {requested!r}",
                    code="interaction_mode_conflict",
                )
            return pinned
