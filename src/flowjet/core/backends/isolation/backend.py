"""RuntimeBackend that admits, resolves workspaces, and submits to ThreadPool."""

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
    """Pool-backed RuntimeBackend for the soothe-nano AgentAdapter."""

    def __init__(
        self,
        *,
        models: list[str] | None = None,
        adapter_factory: AdapterFactory,
        pool_settings: PoolSettings | None = None,
        home: Path | str | None = None,
        admission: SessionAdmission | None = None,
        pool: ThreadPool | None = None,
        allow_external_workspace: bool = False,
        mode_gate: InteractionModeGate | None = None,
    ) -> None:
        self._models = models or ["default"]
        home_path = Path(home).expanduser() if home else Path.home() / ".flowjet"
        self._workspaces = WorkspaceResolver(
            home_path, allow_external_workspace=allow_external_workspace
        )
        self._admission = admission or SessionAdmission()
        self._mode_gate = mode_gate or InteractionModeGate()
        self._pool = pool or ThreadPool(adapter_factory, pool_settings)
        self._started = False

    @property
    def pool(self) -> ThreadPool:
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
            async for event in self._pool.submit(isolated):
                yield event

    async def shutdown(self) -> None:
        await self._pool.shutdown()
        self._started = False
