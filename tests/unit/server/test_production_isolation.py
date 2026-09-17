"""RFC-003 production isolation hardening — unit tests.

Integration tests that exercise the full HTTP / service path (workspace policy
over HTTP, mode-conflict over HTTP, /health pool metrics, pool snapshot shape)
live in ``tests/integration/test_production_isolation_http.py``.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from pathlib import Path

import pytest

from flowjet.core.backends.isolation import (
    FakeAgentAdapter,
    IsolatedRunRequest,
    IsolationError,
    PoolSettings,
    ThreadPool,
    WorkspaceResolver,
)
from flowjet.core.backends.isolation.mode_gate import InteractionModeGate
from flowjet.core.backends.isolation.pool import _POISON
from flowjet.core.events import (
    OutputTextDelta,
    RunCompleted,
    RunFailed,
    RunStarted,
)
from flowjet.server.config import Settings


def test_workspace_rejects_external_by_default(tmp_path: Path):
    resolver = WorkspaceResolver(tmp_path, allow_external_workspace=False)
    outside = tmp_path.parent / "outside-ws"
    with pytest.raises(IsolationError) as exc:
        resolver.resolve("s1", {"workspace": str(outside)})
    assert exc.value.code == "invalid_workspace"


def test_workspace_allows_external_when_enabled(tmp_path: Path):
    resolver = WorkspaceResolver(tmp_path, allow_external_workspace=True)
    outside = tmp_path.parent / "outside-ws-ok"
    path = resolver.resolve("s1", {"workspace": str(outside)})
    assert path == outside.resolve()
    assert path.is_dir()


def test_workspace_allows_override_under_home(tmp_path: Path):
    resolver = WorkspaceResolver(tmp_path, allow_external_workspace=False)
    nested = tmp_path / "custom"
    path = resolver.resolve("s1", {"workspace": str(nested)})
    assert path == nested.resolve()


def test_workspace_default_hash_path(tmp_path: Path):
    resolver = WorkspaceResolver(tmp_path)
    a = resolver.resolve("session-a")
    b = resolver.resolve("session-b")
    assert a != b
    assert a.parent == tmp_path / "data" / "workspaces"


@pytest.mark.asyncio
async def test_interaction_mode_gate_pins_session():
    gate = InteractionModeGate()
    assert await gate.resolve("fj-a", {"interaction_mode": "ask"}) == "ask"
    assert await gate.resolve("fj-a", {"interaction_mode": "ask"}) == "ask"
    with pytest.raises(IsolationError) as exc:
        await gate.resolve("fj-a", {"interaction_mode": "agent"})
    assert exc.value.code == "interaction_mode_conflict"


@pytest.mark.asyncio
async def test_interaction_mode_gate_default_agent_and_independent_sessions():
    gate = InteractionModeGate()
    assert await gate.resolve("fj-1", None) == "agent"
    assert await gate.resolve("fj-2", {"interaction_mode": "ask"}) == "ask"
    assert await gate.resolve("fj-1", {"interaction_mode": "agent"}) == "agent"


@pytest.mark.asyncio
async def test_interaction_mode_omitted_after_ask_pin_conflicts():
    gate = InteractionModeGate()
    await gate.resolve("fj-x", {"interaction_mode": "ask"})
    with pytest.raises(IsolationError) as exc:
        await gate.resolve("fj-x", {})
    assert exc.value.code == "interaction_mode_conflict"


def test_settings_pool_settings_include_rfc003_knobs():
    s = Settings(
        thread_pool_min=1,
        thread_pool_max=3,
        thread_pool_idle_timeout=12,
        max_requests_per_worker=7,
        ready_timeout=9,
        allow_external_workspace=True,
        request_timeout=60,
    )
    p = s.pool_settings()
    assert p.min_size == 1
    assert p.max_size == 3
    assert p.idle_timeout_seconds == 12
    assert p.max_requests_per_worker == 7
    assert p.ready_timeout_seconds == 9
    assert p.request_timeout_seconds == 60
    assert s.allow_external_workspace is True


@pytest.mark.asyncio
async def test_control_frames_deliver_when_queue_saturated(tmp_path: Path):
    class BurstAdapter:
        async def astream(self, req: IsolatedRunRequest):
            yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
            for i in range(50):
                yield OutputTextDelta(delta=f"{i}")
            yield RunCompleted(output_text="ok")

        def prepare_for_request(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    pool = ThreadPool(
        BurstAdapter,
        PoolSettings(
            min_size=1,
            max_size=1,
            response_queue_maxsize=2,
            event_enqueue_timeout_seconds=0.05,
            control_enqueue_timeout_seconds=5.0,
            max_requests_per_worker=0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "burst"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_burst",
            session="fj-burst",
            input_text="x",
            model="default",
            workspace=ws,
        )
        stream = pool.submit(req)
        first = await anext(stream)
        assert isinstance(first, RunStarted)
        await asyncio.sleep(0.3)
        rest = [e async for e in stream]
        assert any(isinstance(e, RunCompleted) for e in rest)
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_backpressure_increments_events_dropped(tmp_path: Path):
    class FloodAdapter:
        async def astream(self, req: IsolatedRunRequest):
            yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
            for i in range(80):
                yield OutputTextDelta(delta=str(i))
            yield RunCompleted(output_text="done")

        def prepare_for_request(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    pool = ThreadPool(
        FloodAdapter,
        PoolSettings(
            min_size=1,
            max_size=1,
            response_queue_maxsize=1,
            event_enqueue_timeout_seconds=0.02,
            control_enqueue_timeout_seconds=5.0,
            max_requests_per_worker=0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "flood"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_flood",
            session="fj-flood",
            input_text="x",
            model="default",
            workspace=ws,
        )
        stream = pool.submit(req)
        await anext(stream)
        await asyncio.sleep(0.5)
        _ = [e async for e in stream]
        assert pool.metrics().events_dropped > 0
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_scaled_worker_idles_out(tmp_path: Path):
    pool = ThreadPool(
        FakeAgentAdapter,
        PoolSettings(
            min_size=1,
            max_size=2,
            idle_timeout_seconds=0.4,
            max_requests_per_worker=0,
        ),
    )
    await pool.start()
    try:
        ws_a = tmp_path / "a"
        ws_b = tmp_path / "b"
        ws_a.mkdir()
        ws_b.mkdir()

        async def run(session: str, workspace: Path) -> None:
            req = IsolatedRunRequest(
                run_id=f"resp_{session}",
                session=session,
                input_text="hi",
                model="default",
                workspace=workspace,
            )
            async for _ in pool.submit(req):
                pass

        await asyncio.gather(run("fj-a", ws_a), run("fj-b", ws_b))
        await asyncio.sleep(1.5)
        assert pool._live_count() >= 1
        assert pool.metrics().total_workers <= 2
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_max_requests_recycles_baseline(tmp_path: Path):
    pool = ThreadPool(
        FakeAgentAdapter,
        PoolSettings(
            min_size=1,
            max_size=1,
            max_requests_per_worker=1,
            idle_timeout_seconds=0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "recycle"
        ws.mkdir()
        first_id = next(iter(pool._workers.values())).worker_id

        async def one(i: int) -> None:
            req = IsolatedRunRequest(
                run_id=f"resp_{i}",
                session=f"fj-{i}",
                input_text=str(i),
                model="default",
                workspace=ws,
            )
            async for _ in pool.submit(req):
                pass

        await one(0)
        await asyncio.sleep(1.5)
        assert pool._live_count() >= 1
        second_id = next(iter(pool._workers.values())).worker_id
        await one(1)
        assert second_id != first_id
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_ready_timeout_marks_worker_dead(tmp_path: Path):
    class HangPrepareAdapter:
        async def astream(self, req: IsolatedRunRequest):
            yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
            yield RunCompleted(output_text="ok")

        def prepare_for_request(self) -> None:
            threading.Event().wait(timeout=10)

        async def cleanup(self) -> None:
            return None

    pool = ThreadPool(
        HangPrepareAdapter,
        PoolSettings(
            min_size=1,
            max_size=1,
            ready_timeout_seconds=0.3,
            max_requests_per_worker=0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "hang"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_hang",
            session="fj-hang",
            input_text="x",
            model="default",
            workspace=ws,
        )
        events = [e async for e in pool.submit(req)]
        assert any(isinstance(e, RunCompleted) for e in events)
        assert pool.metrics().ready_timeouts >= 1
        await asyncio.sleep(1.5)
        assert pool._live_count() >= 1
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_request_timeout_yields_worker_error(tmp_path: Path):
    class SlowAdapter:
        async def astream(self, req: IsolatedRunRequest):
            yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
            await asyncio.sleep(5)
            yield RunCompleted(output_text="late")

        def prepare_for_request(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    pool = ThreadPool(
        SlowAdapter,
        PoolSettings(
            min_size=1,
            max_size=1,
            request_timeout_seconds=0.2,
            max_requests_per_worker=0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "timeout"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_to",
            session="fj-to",
            input_text="x",
            model="default",
            workspace=ws,
        )
        events = [e async for e in pool.submit(req)]
        assert any(isinstance(e, RunFailed) and e.code == "worker_error" for e in events)
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_cancel_session_cancels_in_flight(tmp_path: Path):
    pool = ThreadPool(
        lambda: FakeAgentAdapter(delay_s=2.0),
        PoolSettings(min_size=1, max_size=1, max_requests_per_worker=0),
    )
    await pool.start()
    try:
        ws = tmp_path / "cs"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_cs",
            session="fj-cs",
            input_text="slow",
            model="default",
            workspace=ws,
        )

        async def consume() -> list:
            return [e async for e in pool.submit(req)]

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert pool.cancel_session("fj-cs")
        events = await asyncio.wait_for(task, timeout=2.0)
        assert any(isinstance(e, RunFailed) and e.code == "cancelled" for e in events)
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_dead_worker_respawned_by_watchdog(tmp_path: Path):
    pool = ThreadPool(
        FakeAgentAdapter,
        PoolSettings(min_size=1, max_size=1, max_requests_per_worker=0),
    )
    await pool.start()
    try:
        worker = next(iter(pool._workers.values()))
        old_id = worker.worker_id
        worker.stop_event.set()
        worker.request_queue.put(_POISON)
        worker.thread.join(timeout=2.0)
        assert not worker.is_alive()
        await asyncio.sleep(1.5)
        assert pool._live_count() >= 1
        assert old_id not in pool._workers or pool._workers[old_id].is_alive()
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_orphan_tasks_cancelled_between_turns(tmp_path: Path):
    leak_var: contextvars.ContextVar[str] = contextvars.ContextVar("leak", default="clean")
    saw_orphan = threading.Event()
    observations: list[str] = []

    class OrphanAdapter:
        async def astream(self, req: IsolatedRunRequest):
            async def _orphan() -> None:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    saw_orphan.set()
                    raise

            asyncio.create_task(_orphan())
            observations.append(leak_var.get())
            leak_var.set(req.session)
            yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
            yield RunCompleted(output_text="ok")

        def prepare_for_request(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    pool = ThreadPool(
        OrphanAdapter,
        PoolSettings(min_size=1, max_size=1, max_requests_per_worker=0),
    )
    await pool.start()
    try:
        ws = tmp_path / "orphan"
        ws.mkdir()
        for i in range(2):
            req = IsolatedRunRequest(
                run_id=f"resp_o{i}",
                session=f"fj-o{i}",
                input_text="x",
                model="default",
                workspace=ws,
            )
            _ = [e async for e in pool.submit(req)]
        assert saw_orphan.wait(timeout=2.0)
        assert observations == ["clean", "clean"]
    finally:
        await pool.shutdown()


# NOTE: The following integration tests (which exercise the full HTTP / service
# path across IsolatingRuntimeBackend, ResponseService, and the ASGI app) have
# been moved to tests/integration/test_production_isolation_http.py:
#   - test_backend_mode_conflict_via_service
#   - test_http_invalid_workspace_returns_400
#   - test_http_mode_conflict_returns_400
#   - test_health_includes_pool_metrics
#   - test_pool_metrics_snapshot_shape
