"""IsolationPool protocol — the common surface for ThreadPool and BoxLitePool.

Phase 3 introduces :class:`BoxLiteIsolationPool` as an alternative to
:class:`~flowjet.core.backends.isolation.pool.ThreadPool`.  Both implement
this protocol so :class:`IsolatingRuntimeBackend` can accept either.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from flowjet.core.backends.isolation.pool import PoolMetrics
from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import RuntimeEvent


@runtime_checkable
class IsolationPool(Protocol):
    """Pool that executes :class:`IsolatedRunRequest` and yields events."""

    async def start(self) -> None:
        """Start the pool (spawn workers / pull images)."""
        ...

    async def submit(self, request: IsolatedRunRequest) -> AsyncIterator[RuntimeEvent]:
        """Submit a request and yield runtime events until terminal."""
        ...

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a running request. Returns True if the run was found."""
        ...

    def cancel_session(self, session: str) -> bool:
        """Cancel all runs for a session. Returns True if any were found."""
        ...

    def metrics(self) -> PoolMetrics:
        """Return pool health metrics."""
        ...

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Stop all workers and release resources."""
        ...

    async def await_session_dispatchable(self, session: str) -> None:
        """Wait until no worker is busy with this session."""
        ...
