"""In-process Response run store (Phase 1).

Thread-safe: the isolating runtime pushes events from worker threads through
an asyncio bridge, while retrieve/delete may run on the event loop. A re-entrant
lock keeps ``put``/``get``/``delete`` atomic without forcing callers to hold it.
"""

from __future__ import annotations

import threading
from typing import Any


class InMemoryRunStore:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def put(self, response_id: str, body: dict[str, Any]) -> None:
        with self._lock:
            self._items[response_id] = body

    def get(self, response_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._items.get(response_id)

    def delete(self, response_id: str) -> bool:
        with self._lock:
            return self._items.pop(response_id, None) is not None
