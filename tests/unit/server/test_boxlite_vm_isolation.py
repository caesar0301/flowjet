"""Tests for the BoxLite VM isolation pool (boxlite-ai/boxlite backend).

The external ``boxlite`` SDK is faked via ``sys.modules`` injection: the pool
imports it lazily inside functions, so a fake module is enough to exercise
the full lifecycle — Box creation options (per-workspace volume), the
Execution↔JSON-lines bridge (single stdout pump + line queue), the ready
handshake, workspace-keyed dispatch, frame workspace rewriting, recycling,
and shutdown.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from flowjet.core.backends.isolation.boxlite_protocol import (
    encode_event,
    parse_frame,
)
from flowjet.core.backends.isolation.boxlite_vm_pool import (
    BoxLiteVMIsolationPool,
    BoxLiteVMSettings,
    _import_boxlite,
)
from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import Narration

# ---------------------------------------------------------------------------
# Fake boxlite SDK
# ---------------------------------------------------------------------------


class FakeStdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def send_input(self, data: bytes) -> None:
        self.written.append(data)


class FakeExecution:
    def __init__(self) -> None:
        self.stdin_obj = FakeStdin()
        self.stdout_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.killed = False

    def stdin(self) -> FakeStdin:
        return self.stdin_obj

    def stdout(self):
        return self._stdout_iter()

    async def _stdout_iter(self):
        while True:
            item = await self.stdout_queue.get()
            if item is None:
                return
            yield item

    def stderr(self):
        return self._empty_iter()

    async def _empty_iter(self):
        return
        yield  # pragma: no cover - makes this an async generator

    async def wait(self) -> None:  # pragma: no cover - unused in tests
        return None

    def kill(self, signal: int = 9) -> None:
        self.killed = True


class FakeBox:
    def __init__(self) -> None:
        self.execution = FakeExecution()
        self.stopped = False
        self.exec_calls: list[tuple] = []

    async def exec(self, cmd, args=None, tty=False):
        self.exec_calls.append((cmd, args, tty))
        return self.execution

    async def stop(self) -> None:
        self.stopped = True


class FakeRuntime:
    """Each create() returns a fresh FakeBox; the first frame the worker
    would emit ({"type": "ready"}) is queued on its stdout."""

    def __init__(self) -> None:
        self.created: list[tuple[object, object, str | None]] = []

    async def create(self, options, name=None):
        box = FakeBox()
        await box.execution.stdout_queue.put(b'{"type": "ready"}\n')
        self.created.append((box, options, name))
        return box


def _install_fake_boxlite(monkeypatch) -> FakeRuntime:
    """Inject a fake `boxlite` module; returns the fake runtime instance."""
    fake_module = types.ModuleType("boxlite")
    runtime = FakeRuntime()

    class FakeBoxOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeNetworkSpec:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeImageRegistry:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeBoxlite:
        def __init__(self, options=None):
            self.options = options

        @classmethod
        def default(cls):
            return cls()

        async def create(self, options, name=None):
            return await runtime.create(options, name=name)

    fake_module.BoxOptions = FakeBoxOptions
    fake_module.NetworkSpec = FakeNetworkSpec
    fake_module.ImageRegistry = FakeImageRegistry
    fake_module.Options = FakeOptions
    fake_module.Boxlite = FakeBoxlite
    monkeypatch.setitem(sys.modules, "boxlite", fake_module)
    return runtime


def _settings(**overrides) -> BoxLiteVMSettings:
    defaults = {
        "image": "flowjet-server:test",
        "max_size": 4,
        "ready_timeout_seconds": 5.0,
    }
    defaults.update(overrides)
    return BoxLiteVMSettings(**defaults)


def _request(workspace: str, session: str = "s1", run_id: str = "r1") -> IsolatedRunRequest:
    return IsolatedRunRequest(
        run_id=run_id,
        session=session,
        input_text="hi",
        model="default",
        workspace=Path(workspace),
    )


@pytest.fixture
def fake_runtime(monkeypatch) -> FakeRuntime:
    return _install_fake_boxlite(monkeypatch)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_validates():
    with pytest.raises(ValueError, match="max_size"):
        BoxLiteVMSettings(image="img", max_size=0)
    with pytest.raises(ValueError, match="vcpus"):
        BoxLiteVMSettings(image="img", vcpus=0)
    with pytest.raises(ValueError, match="mem_mib"):
        BoxLiteVMSettings(image="img", mem_mib=16)


def test_import_boxlite_missing_sdk_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "boxlite", None)  # None → ImportError
    with pytest.raises(RuntimeError, match="pip install boxlite"):
        _import_boxlite()


async def test_start_requires_image(fake_runtime):
    pool = BoxLiteVMIsolationPool(adapter_factory=lambda: None, settings=_settings(image=None))
    with pytest.raises(RuntimeError, match="FLOWJET_BOXLITE_IMAGE"):
        await pool.start()


# ---------------------------------------------------------------------------
# Per-workspace Box creation
# ---------------------------------------------------------------------------


async def test_box_options_single_workspace_volume(fake_runtime, monkeypatch):
    pool = BoxLiteVMIsolationPool(
        adapter_factory=lambda: None,
        settings=_settings(pass_env=["FLOWJET_NANO_CONFIG"], allow_net=["api.example.com"]),
    )
    monkeypatch.setenv("FLOWJET_NANO_CONFIG", "/flowjet/config/nano.yml")
    await pool.start()

    opts = pool._box_options("/flowjet/data/workspaces/wsA")
    assert opts.kwargs["volumes"] == [("/flowjet/data/workspaces/wsA", "/workspace", False)], (
        "the ONLY data volume must be this worker's own workspace"
    )
    assert opts.kwargs["image"] == "flowjet-server:test"
    assert ("FLOWJET_NANO_CONFIG", "/flowjet/config/nano.yml") in opts.kwargs["env"]
    assert opts.kwargs["network"].kwargs["mode"] == "enabled"
    assert opts.kwargs["network"].kwargs["allow_net"] == ["api.example.com"]
    await pool.shutdown()


async def test_spawn_awaits_ready_and_tracks_workspace(fake_runtime):
    pool = BoxLiteVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()
    async with pool._lock:
        state = await pool._spawn_worker_unlocked(workspace="/flowjet/data/workspaces/wsA")
    assert state.status == "idle"
    assert state.workspace == "/flowjet/data/workspaces/wsA"
    # Worker VM runs the unchanged worker entrypoint.
    box = fake_runtime.created[-1][0]
    assert box.exec_calls == [
        ("python", ["-m", "flowjet.core.backends.isolation.boxlite_worker"], False)
    ]
    await pool.shutdown()


async def test_spawn_bad_first_frame_recycles_box(fake_runtime):
    pool = BoxLiteVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()
    # Pre-seed a non-ready first frame on the NEXT created box.
    box_holder = {}

    original_create = fake_runtime.create

    async def create_with_bad_frame(options, name=None):
        box = await original_create(options, name=name)
        # Replace the queued ready frame with an error frame.
        await box.execution.stdout_queue.get()
        await box.execution.stdout_queue.put(b'{"type": "error", "message": "x"}\n')
        box_holder["box"] = box
        return box

    fake_runtime.create = create_with_bad_frame
    with pytest.raises(RuntimeError, match="unexpected first frame"):
        async with pool._lock:
            await pool._spawn_worker_unlocked(workspace="/flowjet/data/workspaces/wsB")
    assert box_holder["box"].stopped, "a VM that never becomes ready must be stopped"
    await pool.shutdown()


# ---------------------------------------------------------------------------
# Workspace-keyed dispatch + frame rewriting
# ---------------------------------------------------------------------------


def test_worker_serves_only_its_own_workspace(fake_runtime):
    from flowjet.core.backends.isolation.boxlite_vm_pool import _VMWorkerState

    pool = BoxLiteVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    state = _VMWorkerState(
        worker_id="bl_0_x",
        workspace="/flowjet/data/workspaces/wsA",
        box=None,
        execution=None,
        stdin=None,
        lines=asyncio.Queue(),
    )
    assert pool._worker_serves(state, "/flowjet/data/workspaces/wsA")
    assert not pool._worker_serves(state, "/flowjet/data/workspaces/wsB")
    assert not pool._worker_serves(state, None)


async def test_submit_rewrites_workspace_and_bridges_events(fake_runtime):
    pool = BoxLiteVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()

    req = _request("/flowjet/data/workspaces/wsA")

    async def feed_events():
        # Wait for the worker to be spawned + request to be written.
        while not fake_runtime.created:
            await asyncio.sleep(0.01)
        box: FakeBox = fake_runtime.created[-1][0]
        while not box.execution.stdin_obj.written:
            await asyncio.sleep(0.01)
        q = box.execution.stdout_queue
        await q.put((encode_event(Narration(text="hello")) + "\n").encode())
        await q.put(b'{"type": "done"}\n')
        await q.put(b'{"type": "ready"}\n')

    feeder = asyncio.create_task(feed_events())
    events = []
    async for event in pool.submit(req):
        events.append(event)
    await feeder

    assert any(isinstance(e, Narration) and e.text == "hello" for e in events)
    # The request frame's workspace must be rewritten to the VM mount point.
    box: FakeBox = fake_runtime.created[-1][0]
    sent = parse_frame(box.execution.stdin_obj.written[0].decode())
    assert sent["workspace"] == "/workspace"
    assert sent["run_id"] == "r1"
    # Worker is idle again and reusable for the same workspace.
    assert pool.metrics().idle_workers == 1
    await pool.shutdown()


async def test_acquire_spawns_per_workspace_and_reuses(fake_runtime):
    pool = BoxLiteVMIsolationPool(adapter_factory=lambda: None, settings=_settings(max_size=4))
    await pool.start()

    w1 = await pool._acquire_idle_worker("s1", "r1", "q1", "/flowjet/data/workspaces/wsA")
    await pool._release_worker(w1, _request("/flowjet/data/workspaces/wsA", "s1", "r1"))
    w2 = await pool._acquire_idle_worker("s2", "r2", "q2", "/flowjet/data/workspaces/wsA")
    assert w2 is w1, "same workspace must reuse its idle VM"
    await pool._release_worker(w2, _request("/flowjet/data/workspaces/wsA", "s2", "r2"))

    w3 = await pool._acquire_idle_worker("s3", "r3", "q3", "/flowjet/data/workspaces/wsB")
    assert w3 is not w1, "different workspace must get a different VM"
    assert len(fake_runtime.created) == 2
    await pool._release_worker(w3, _request("/flowjet/data/workspaces/wsB", "s3", "r3"))
    await pool.shutdown()


# ---------------------------------------------------------------------------
# Recycling / shutdown
# ---------------------------------------------------------------------------


async def test_release_recycles_after_max_requests(fake_runtime):
    pool = BoxLiteVMIsolationPool(
        adapter_factory=lambda: None,
        settings=_settings(max_requests_per_worker=1),
    )
    await pool.start()
    w = await pool._acquire_idle_worker("s1", "r1", "q1", "/flowjet/data/workspaces/wsA")
    box = fake_runtime.created[-1][0]
    await pool._release_worker(w, _request("/flowjet/data/workspaces/wsA", "s1", "r1"))
    assert w.status == "dead"
    assert box.stopped, "VM must be stopped once the request budget is exhausted"


async def test_idle_timeout_reaps_vm(fake_runtime):
    pool = BoxLiteVMIsolationPool(
        adapter_factory=lambda: None,
        settings=_settings(idle_timeout_seconds=0.0),
    )
    await pool.start()
    w = await pool._acquire_idle_worker("s1", "r1", "q1", "/flowjet/data/workspaces/wsA")
    await pool._release_worker(w, _request("/flowjet/data/workspaces/wsA", "s1", "r1"))
    box = fake_runtime.created[-1][0]
    await pool._reap_dead_and_idle()
    assert box.stopped, "idle VM past the timeout must be stopped"
    assert pool.metrics().total_workers == 0


async def test_shutdown_stops_all_vms(fake_runtime):
    pool = BoxLiteVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()
    for i, ws in enumerate(["wsA", "wsB"]):
        w = await pool._acquire_idle_worker(
            f"s{i}", f"r{i}", f"q{i}", f"/flowjet/data/workspaces/{ws}"
        )
        await pool._release_worker(w, _request(f"/flowjet/data/workspaces/{ws}", f"s{i}", f"r{i}"))
    assert len(fake_runtime.created) == 2
    await pool.shutdown()
    for box, _opts, _name in fake_runtime.created:
        assert box.stopped
    assert pool.metrics().total_workers == 0


async def test_cancel_run_kills_execution(fake_runtime):
    pool = BoxLiteVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()
    w = await pool._acquire_idle_worker("s1", "r1", "q1", "/flowjet/data/workspaces/wsA")
    assert pool.cancel_run("r1") is True
    assert w.execution.killed
    assert pool.cancel_run("nope") is False
    await pool._release_worker(w, _request("/flowjet/data/workspaces/wsA", "s1", "r1"))
    await pool.shutdown()
