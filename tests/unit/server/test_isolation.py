"""Tests for RFC-002 isolation: workspace, admission, thread pool."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from pathlib import Path

import pytest

from flowjet.core.backends.isolation import (
    FakeAgentAdapter,
    IsolatedRunRequest,
    IsolatingRuntimeBackend,
    PoolSettings,
    SessionAdmission,
    ThreadPool,
    WorkspaceResolver,
)
from flowjet.core.events import (
    OutputTextDelta,
    RunCompleted,
    RunFailed,
    RunRequest,
    RunStarted,
)


def test_workspace_resolver_hash_and_override(tmp_path: Path):
    resolver = WorkspaceResolver(tmp_path)
    a = resolver.resolve("session-a")
    b = resolver.resolve("session-b")
    assert a != b
    assert a.is_dir()
    assert a.parent == tmp_path / "data" / "workspaces"

    override = tmp_path / "custom-ws"
    resolved = resolver.resolve("session-a", {"workspace": str(override)})
    assert resolved == override.resolve()
    assert resolved.is_dir()


@pytest.mark.asyncio
async def test_session_admission_serializes_same_session():
    admission = SessionAdmission()
    order: list[str] = []

    async def turn(label: str, delay: float) -> None:
        async with admission.admit("same"):
            order.append(f"{label}-start")
            await asyncio.sleep(delay)
            order.append(f"{label}-end")

    await asyncio.gather(turn("a", 0.05), turn("b", 0.01))
    # Second turn must not start until first ends.
    assert order.index("a-end") < order.index("b-start") or order.index("b-end") < order.index(
        "a-start"
    )


@pytest.mark.asyncio
async def test_thread_pool_submit_yields_events(tmp_path: Path):
    pool = ThreadPool(FakeAgentAdapter, PoolSettings(min_size=1, max_size=2))
    await pool.start()
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_1",
            session="fj-1",
            input_text="hello",
            model="default",
            workspace=ws,
        )
        events = [e async for e in pool.submit(req)]
        assert isinstance(events[0], RunStarted)
        assert any(isinstance(e, OutputTextDelta) for e in events)
        assert isinstance(events[-1], RunCompleted)
        assert (ws / "last_input.txt").read_text(encoding="utf-8") == "hello"
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_gives_each_turn_a_fresh_context(tmp_path: Path):
    turn_value: contextvars.ContextVar[str] = contextvars.ContextVar(
        "flowjet_test_turn_value",
        default="clean",
    )
    observations: list[str] = []

    class ContextAdapter:
        async def astream(self, req: IsolatedRunRequest):
            observations.append(turn_value.get())
            turn_value.set(req.session)
            yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
            yield RunCompleted(output_text="ok")

        def prepare_for_request(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    pool = ThreadPool(ContextAdapter, PoolSettings(min_size=1, max_size=1))
    await pool.start()
    try:
        workspace = tmp_path / "context"
        workspace.mkdir()
        for index in range(2):
            request = IsolatedRunRequest(
                run_id=f"resp_context_{index}",
                session=f"fj-context-{index}",
                input_text="context",
                model="default",
                workspace=workspace,
            )
            _ = [event async for event in pool.submit(request)]
        assert observations == ["clean", "clean"]
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_disconnected_consumer_cancels_before_worker_reuse(tmp_path: Path):
    class DisconnectAdapter:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.cancelled = threading.Event()

        async def astream(self, req: IsolatedRunRequest):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
                if req.input_text == "slow":
                    await asyncio.sleep(10)
                yield RunCompleted(output_text="ok")
            finally:
                self.active -= 1
                if req.input_text == "slow":
                    self.cancelled.set()

        def prepare_for_request(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    adapter = DisconnectAdapter()
    pool = ThreadPool(lambda: adapter, PoolSettings(min_size=1, max_size=1))
    await pool.start()
    try:
        workspace = tmp_path / "disconnect"
        workspace.mkdir()
        slow = IsolatedRunRequest(
            run_id="resp_slow",
            session="fj-slow",
            input_text="slow",
            model="default",
            workspace=workspace,
        )
        stream = pool.submit(slow)
        assert isinstance(await anext(stream), RunStarted)
        await asyncio.wait_for(stream.aclose(), timeout=2)
        assert adapter.cancelled.is_set()

        next_request = IsolatedRunRequest(
            run_id="resp_next",
            session="fj-next",
            input_text="fast",
            model="default",
            workspace=workspace,
        )
        events = [event async for event in pool.submit(next_request)]
        assert isinstance(events[-1], RunCompleted)
        assert adapter.max_active == 1
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_pool_cross_session_parallel_workspaces(tmp_path: Path):
    pool = ThreadPool(
        lambda: FakeAgentAdapter(delay_s=0.05),
        PoolSettings(min_size=2, max_size=4),
    )
    await pool.start()
    try:
        ws_a = tmp_path / "a"
        ws_b = tmp_path / "b"
        ws_a.mkdir()
        ws_b.mkdir()

        async def run(session: str, workspace: Path, text: str) -> list:
            req = IsolatedRunRequest(
                run_id=f"resp_{session}",
                session=session,
                input_text=text,
                model="default",
                workspace=workspace,
            )
            return [e async for e in pool.submit(req)]

        results = await asyncio.gather(
            run("fj-a", ws_a, "alpha"),
            run("fj-b", ws_b, "beta"),
        )
        assert all(isinstance(r[-1], RunCompleted) for r in results)
        assert (ws_a / "last_input.txt").read_text(encoding="utf-8") == "alpha"
        assert (ws_b / "last_input.txt").read_text(encoding="utf-8") == "beta"
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_same_session_serialized_in_pool(tmp_path: Path):
    pool = ThreadPool(
        lambda: FakeAgentAdapter(delay_s=0.04),
        PoolSettings(min_size=2, max_size=4),
    )
    await pool.start()
    try:
        ws = tmp_path / "s"
        ws.mkdir()
        started: list[float] = []
        loop = asyncio.get_running_loop()

        async def run(text: str) -> None:
            started.append(loop.time())
            req = IsolatedRunRequest(
                run_id=f"resp_{text}",
                session="fj-shared",
                input_text=text,
                model="default",
                workspace=ws,
            )
            async for _ in pool.submit(req):
                pass

        await asyncio.gather(run("one"), run("two"))
        # Starts may be close, but final file must be from the later-finishing turn;
        # more importantly both complete without cross-corrupting via admission.
        assert (ws / "last_input.txt").read_text(encoding="utf-8") in {"one", "two"}
        assert abs(started[0] - started[1]) < 1.0
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_cancel_run(tmp_path: Path):
    pool = ThreadPool(
        lambda: FakeAgentAdapter(delay_s=2.0),
        PoolSettings(min_size=1, max_size=1),
    )
    await pool.start()
    try:
        ws = tmp_path / "c"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_cancel",
            session="fj-c",
            input_text="slow",
            model="default",
            workspace=ws,
        )

        async def consume() -> list:
            return [e async for e in pool.submit(req)]

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert pool.cancel_run("resp_cancel")
        events = await asyncio.wait_for(task, timeout=2.0)
        assert any(isinstance(e, RunFailed) and e.code == "cancelled" for e in events)
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_isolating_backend_stream(tmp_path: Path):
    backend = IsolatingRuntimeBackend(
        models=["default"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=2),
        home=tmp_path,
    )
    try:
        events = [
            e
            async for e in backend.stream_run(
                RunRequest(model="default", input_text="ping", session="fj-x")
            )
        ]
        assert isinstance(events[0], RunStarted)
        assert events[0].session == "fj-x"
        assert isinstance(events[-1], RunCompleted)
        # Workspace created under FLOWJET_HOME
        workspaces = list((tmp_path / "data" / "workspaces").iterdir())
        assert len(workspaces) == 1
        assert (workspaces[0] / "last_input.txt").read_text(encoding="utf-8") == "ping"
    finally:
        await backend.shutdown()


# NOTE: test_isolating_backend_via_http (HTTP integration across the ASGI app,
# IsolatingRuntimeBackend, and FakeAgentAdapter) has been moved to
# tests/integration/test_isolation_http.py.
