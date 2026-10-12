"""RuntimeBackend that admits, resolves workspaces, and submits to an isolation pool.

Phase 1–2 use :class:`~flowjet.core.backends.isolation.pool.ThreadPool`.
Phase 3 adds :class:`~flowjet.core.backends.isolation.boxlite_pool.BoxLiteIsolationPool`
for Docker-container-based session isolation.  Both implement the
:class:`~flowjet.core.backends.isolation.isolation_pool.IsolationPool` protocol.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from flowjet.core.backends.isolation.adapter import AdapterFactory
from flowjet.core.backends.isolation.admission import SessionAdmission
from flowjet.core.backends.isolation.mode_gate import InteractionModeGate
from flowjet.core.backends.isolation.pool import PoolMetrics, PoolSettings, ThreadPool
from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.backends.isolation.workspace import WorkspaceResolver
from flowjet.core.events import ModelInfo, RunRequest, RuntimeEvent


class IsolatingRuntimeBackend:
    """Pool-backed RuntimeBackend for the soothe/nano AgentAdapter.

    The workspace resolver is injectable (Phase 2): callers can pass a
    :class:`SyncAwareWorkspaceResolver` to enable fsspec workspace sync.
    The default is the local-only :class:`WorkspaceResolver` (Phase 1).

    The isolation pool is injectable (Phase 3): callers can pass a
    :class:`~flowjet.core.backends.isolation.boxlite_pool.BoxLiteIsolationPool`
    to enable Docker-container-based session isolation.  The default is the
    in-process :class:`ThreadPool` (Phase 1/2).
    """

    def __init__(
        self,
        *,
        models: list[str] | None = None,
        adapter_factory: AdapterFactory,
        pool_settings: PoolSettings | None = None,
        home: Path | str | None = None,
        admission: SessionAdmission | None = None,
        pool: Any | None = None,
        allow_external_workspace: bool = False,
        mode_gate: InteractionModeGate | None = None,
        workspace_resolver: WorkspaceResolver | None = None,
    ) -> None:
        self._models = models or ["default"]
        home_path = Path(home).expanduser() if home else Path.home() / ".flowjet"
        self._workspaces = workspace_resolver or WorkspaceResolver(
            home_path, allow_external_workspace=allow_external_workspace
        )
        self._admission = admission or SessionAdmission()
        self._mode_gate = mode_gate or InteractionModeGate()
        self._pool = pool or ThreadPool(adapter_factory, pool_settings)
        self._started = False

    @property
    def pool(self) -> Any:
        return self._pool

    @property
    def workspaces(self) -> WorkspaceResolver:
        return self._workspaces

    def pool_metrics(self) -> dict[str, Any]:
        m: PoolMetrics = self._pool.metrics()
        return {
            "total_workers": m.total_workers,
            "idle_workers": m.idle_workers,
            "busy_workers": m.busy_workers,
            "dead_workers": m.dead_workers,
            "requests_completed": m.requests_completed,
            "ready_timeouts": m.ready_timeouts,
            "events_dropped": m.events_dropped,
        }

    async def _ensure_started(self) -> None:
        if not self._started:
            await self._pool.start()
            self._started = True

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=m) for m in self._models]

    async def delete_run(self, run_id: str) -> None:
        self._pool.cancel_run(run_id)

    async def stream_run(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        await self._ensure_started()
        session = request.session or f"fj-{uuid4()}"
        run_id = request.run_id or f"resp_{uuid4().hex}"
        metadata = dict(request.metadata or {})
        mode = await self._mode_gate.resolve(session, metadata)
        metadata["interaction_mode"] = mode
        workspace = self._workspaces.resolve(session, metadata)
        isolated = IsolatedRunRequest(
            run_id=run_id,
            session=session,
            input_text=request.input_text,
            model=request.model,
            workspace=workspace,
            thread_id=session,
            metadata=metadata,
        )
        async with self._admission.admit(session):
            await self._workspaces.open_session(session, workspace)
            try:
                async for event in self._pool.submit(isolated):
                    yield event
            finally:
                await self._workspaces.close_session(session, workspace)

    async def shutdown(self) -> None:
        await self._pool.shutdown()
        shutdown_ws = getattr(self._workspaces, "shutdown", None)
        if shutdown_ws is not None:
            result = shutdown_ws()
            if hasattr(result, "__await__"):
                await result
        self._started = False
