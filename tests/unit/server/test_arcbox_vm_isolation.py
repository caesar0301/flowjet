"""Tests for the ArcBox VM isolation pool (arcboxlabs/arcbox backend).

The external ``arcbox`` SDK is faked via ``sys.modules`` injection: the pool
imports it lazily inside functions, so a fake module is enough to exercise
the full lifecycle — sandbox creation (per-workspace), the
command↔JSON-lines bridge (single output pump + line queue), the ready
handshake, workspace-keyed dispatch, frame workspace rewriting, recycling,
and shutdown.
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from pathlib import Path

import pytest

from flowjet.core.backends.isolation.arcbox_protocol import (
    encode_event,
    parse_frame,
)
from flowjet.core.backends.isolation.arcbox_vm_pool import (
    ArcBoxVMIsolationPool,
    ArcBoxVMSettings,
    _import_arcbox,
)
from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import Narration

# ---------------------------------------------------------------------------
# Fake arcbox SDK
# ---------------------------------------------------------------------------


class FakeOutputChunk:
    """Mimics arcbox OutputChunk: (channel, data)."""

    def __init__(self, channel: str, data: bytes) -> None:
        self.channel = channel
        self.data = data


class FakeCommandHandle:
    """Mimics arcbox AsyncCommandHandle."""

    def __init__(self) -> None:
        self.written_stdin: list[bytes] = []
        self.output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.killed = False
        self.command_id = "fake-cmd-1"

    @property
    def output(self):
        return self._output_iter()

    async def _output_iter(self):
        while True:
            item = await self.output_queue.get()
            if item is None:
                return
            yield FakeOutputChunk("stdout", item)

    async def write_stdin(self, data: str | bytes) -> None:
        payload = data.encode() if isinstance(data, str) else data
        self.written_stdin.append(payload)

    async def kill(self, signal: str = "SIGTERM") -> None:
        self.killed = True


class FakeCommands:
    """Mimics arcbox AsyncCommands."""

    def __init__(self) -> None:
        self.handle = FakeCommandHandle()
        self.run_calls: list[tuple] = []

    async def run(
        self,
        cmd,
        *,
        cwd=None,
        env=None,
        user=None,
        timeout=None,
        check=False,
        background=False,
        pty=None,
        stdin=None,
    ):
        self.run_calls.append((cmd, cwd, env, background, stdin))
        return self.handle


class FakeFiles:
    """Mimics arcbox AsyncFiles — records writes, does nothing."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []

    async def write_bytes(self, path, data, mode=None) -> None:
        self.writes.append((str(path), data))

    async def write_text(self, path, text, encoding="utf-8", mode=None) -> None:
        self.writes.append((str(path), text.encode(encoding)))

    async def mkdir(self, path, mode=None) -> None:
        pass

    async def remove(self, path, recursive=False) -> None:
        pass

    async def stat(self, path):
        pass

    async def list(self, path):
        return []


class FakeSandbox:
    """Mimics arcbox AsyncSandbox."""

    def __init__(self, sandbox_id: str) -> None:
        self.id = sandbox_id
        self.commands = FakeCommands()
        self.files = FakeFiles()
        self.killed = False

    async def kill(self) -> None:
        self.killed = True


class FakeArcBox:
    """Mimics arcbox AsyncArcBox — each create() returns a fresh FakeSandbox.
    The first frame the worker would emit ({"type": "ready"}) is queued on
    the command's output."""

    def __init__(self, connection=None) -> None:
        self.created: list[tuple[FakeSandbox, str, object]] = []

    async def create(
        self,
        template="",
        *,
        ttl=None,
        idle_timeout=None,
        on_idle=None,
        vcpus=None,
        memory_mib=None,
        cmd=None,
        no_default_cmd=False,
        env=None,
        no_default_env=False,
        labels=None,
        network=None,
        wait_until_ready=True,
    ):
        sandbox = FakeSandbox(str(uuid.uuid4()))
        # Pre-seed the "ready" frame the worker would emit.
        await sandbox.commands.handle.output_queue.put(b'{"type": "ready"}\n')
        self.created.append((sandbox, template, {
            "vcpus": vcpus,
            "memory_mib": memory_mib,
            "env": env,
            "network": network,
            "cmd": cmd,
        }))
        return sandbox

    async def aclose(self) -> None:
        pass


def _install_fake_arcbox(monkeypatch) -> FakeArcBox:
    """Inject a fake `arcbox` module; returns the shared fake client instance."""
    fake_module = types.ModuleType("arcbox")
    client = FakeArcBox()

    class FakeConnection:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    # AsyncArcBox is a class that returns the shared client instance
    # regardless of the connection argument (the pool creates one client).
    class _AsyncArcBoxFactory:
        def __new__(cls, connection=None):
            return client

    fake_module.AsyncArcBox = _AsyncArcBoxFactory
    fake_module.Connection = FakeConnection
    monkeypatch.setitem(sys.modules, "arcbox", fake_module)
    return client


def _settings(**overrides) -> ArcBoxVMSettings:
    defaults = {
        "image": "flowjet-server:test",
        "max_size": 4,
        "ready_timeout_seconds": 5.0,
    }
    defaults.update(overrides)
    return ArcBoxVMSettings(**defaults)


def _request(workspace: str, session: str = "s1", run_id: str = "r1") -> IsolatedRunRequest:
    return IsolatedRunRequest(
        run_id=run_id,
        session=session,
        input_text="hi",
        model="default",
        workspace=Path(workspace),
    )


@pytest.fixture
def fake_client(monkeypatch):
    return _install_fake_arcbox(monkeypatch)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_validates():
    with pytest.raises(ValueError, match="max_size"):
        ArcBoxVMSettings(image="img", max_size=0)
    with pytest.raises(ValueError, match="vcpus"):
        ArcBoxVMSettings(image="img", vcpus=0)
    with pytest.raises(ValueError, match="mem_mib"):
        ArcBoxVMSettings(image="img", mem_mib=16)


def test_import_arcbox_missing_sdk_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "arcbox", None)  # None → ImportError
    with pytest.raises(RuntimeError, match="pip install arcbox"):
        _import_arcbox()


async def test_start_requires_image(fake_client):
    pool = ArcBoxVMIsolationPool(adapter_factory=lambda: None, settings=_settings(image=None))
    with pytest.raises(RuntimeError, match="FLOWJET_ARCBOX_IMAGE"):
        await pool.start()


# ---------------------------------------------------------------------------
# Per-workspace sandbox creation
# ---------------------------------------------------------------------------


async def test_spawn_awaits_ready_and_tracks_workspace(fake_client):
    pool = ArcBoxVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()
    async with pool._lock:
        state = await pool._spawn_worker_unlocked(workspace="/flowjet/data/workspaces/wsA")
    assert state.status == "idle"
    assert state.workspace == "/flowjet/data/workspaces/wsA"
    # Worker sandbox runs the unchanged worker entrypoint.
    sandbox = fake_client.created[-1][0]
    assert sandbox.commands.run_calls[0][0] == ["python", "-m", "flowjet.core.backends.isolation.arcbox_worker"]
    await pool.shutdown()


async def test_spawn_bad_first_frame_recycles_sandbox(fake_client):
    pool = ArcBoxVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()
    # Pre-seed a non-ready first frame on the NEXT created sandbox.
    sandbox_holder = {}

    original_create = fake_client.create

    async def create_with_bad_frame(*args, **kwargs):
        sandbox = await original_create(*args, **kwargs)
        # Replace the queued ready frame with an error frame.
        await sandbox.commands.handle.output_queue.get()
        await sandbox.commands.handle.output_queue.put(b'{"type": "error", "message": "x"}\n')
        sandbox_holder["sandbox"] = sandbox
        return sandbox

    fake_client.create = create_with_bad_frame
    with pytest.raises(RuntimeError, match="unexpected first frame"):
        async with pool._lock:
            await pool._spawn_worker_unlocked(workspace="/flowjet/data/workspaces/wsB")
    assert sandbox_holder["sandbox"].killed, "a sandbox that never becomes ready must be killed"
    await pool.shutdown()


# ---------------------------------------------------------------------------
# Workspace-keyed dispatch + frame rewriting
# ---------------------------------------------------------------------------


def test_worker_serves_only_its_own_workspace(fake_client):
    from flowjet.core.backends.isolation.arcbox_vm_pool import _VMWorkerState

    pool = ArcBoxVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    state = _VMWorkerState(
        worker_id="ab_0_x",
        workspace="/flowjet/data/workspaces/wsA",
        sandbox=None,
        command_handle=None,
        lines=asyncio.Queue(),
    )
    assert pool._worker_serves(state, "/flowjet/data/workspaces/wsA")
    assert not pool._worker_serves(state, "/flowjet/data/workspaces/wsB")
    assert not pool._worker_serves(state, None)


async def test_submit_rewrites_workspace_and_bridges_events(fake_client):
    pool = ArcBoxVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()

    req = _request("/flowjet/data/workspaces/wsA")

    async def feed_events():
        # Wait for the worker to be spawned + request to be written.
        while not fake_client.created:
            await asyncio.sleep(0.01)
        sandbox = fake_client.created[-1][0]
        while not sandbox.commands.handle.written_stdin:
            await asyncio.sleep(0.01)
        q = sandbox.commands.handle.output_queue
        await q.put((encode_event(Narration(text="hello")) + "\n").encode())
        await q.put(b'{"type": "done"}\n')
        await q.put(b'{"type": "ready"}\n')

    feeder = asyncio.create_task(feed_events())
    events = []
    async for event in pool.submit(req):
        events.append(event)
    await feeder

    assert any(isinstance(e, Narration) and e.text == "hello" for e in events)
    # The request frame's workspace must be rewritten to the sandbox path.
    sandbox = fake_client.created[-1][0]
    sent = parse_frame(sandbox.commands.handle.written_stdin[0].decode())
    assert sent["workspace"] == "/workspace"
    assert sent["run_id"] == "r1"
    # Worker is idle again and reusable for the same workspace.
    assert pool.metrics().idle_workers == 1
    await pool.shutdown()


async def test_acquire_spawns_per_workspace_and_reuses(fake_client):
    pool = ArcBoxVMIsolationPool(adapter_factory=lambda: None, settings=_settings(max_size=4))
    await pool.start()

    w1 = await pool._acquire_idle_worker("s1", "r1", "q1", "/flowjet/data/workspaces/wsA")
    await pool._release_worker(w1, _request("/flowjet/data/workspaces/wsA", "s1", "r1"))
    w2 = await pool._acquire_idle_worker("s2", "r2", "q2", "/flowjet/data/workspaces/wsA")
    assert w2 is w1, "same workspace must reuse its idle sandbox"
    await pool._release_worker(w2, _request("/flowjet/data/workspaces/wsA", "s2", "r2"))

    w3 = await pool._acquire_idle_worker("s3", "r3", "q3", "/flowjet/data/workspaces/wsB")
    assert w3 is not w1, "different workspace must get a different sandbox"
    assert len(fake_client.created) == 2
    await pool._release_worker(w3, _request("/flowjet/data/workspaces/wsB", "s3", "r3"))
    await pool.shutdown()


# ---------------------------------------------------------------------------
# Recycling / shutdown
# ---------------------------------------------------------------------------


async def test_release_recycles_after_max_requests(fake_client):
    pool = ArcBoxVMIsolationPool(
        adapter_factory=lambda: None,
        settings=_settings(max_requests_per_worker=1),
    )
    await pool.start()
    w = await pool._acquire_idle_worker("s1", "r1", "q1", "/flowjet/data/workspaces/wsA")
    sandbox = fake_client.created[-1][0]
    await pool._release_worker(w, _request("/flowjet/data/workspaces/wsA", "s1", "r1"))
    assert w.status == "dead"
    assert sandbox.killed, "sandbox must be killed once the request budget is exhausted"


async def test_idle_timeout_reaps_sandbox(fake_client):
    pool = ArcBoxVMIsolationPool(
        adapter_factory=lambda: None,
        settings=_settings(idle_timeout_seconds=0.0),
    )
    await pool.start()
    w = await pool._acquire_idle_worker("s1", "r1", "q1", "/flowjet/data/workspaces/wsA")
    await pool._release_worker(w, _request("/flowjet/data/workspaces/wsA", "s1", "r1"))
    sandbox = fake_client.created[-1][0]
    await pool._reap_dead_and_idle()
    assert sandbox.killed, "idle sandbox past the timeout must be killed"
    assert pool.metrics().total_workers == 0


async def test_shutdown_kills_all_sandboxes(fake_client):
    pool = ArcBoxVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()
    for i, ws in enumerate(["wsA", "wsB"]):
        w = await pool._acquire_idle_worker(
            f"s{i}", f"r{i}", f"q{i}", f"/flowjet/data/workspaces/{ws}"
        )
        await pool._release_worker(w, _request(f"/flowjet/data/workspaces/{ws}", f"s{i}", f"r{i}"))
    assert len(fake_client.created) == 2
    await pool.shutdown()
    for sandbox, _tmpl, _opts in fake_client.created:
        assert sandbox.killed
    assert pool.metrics().total_workers == 0


async def test_cancel_run_kills_command(fake_client):
    pool = ArcBoxVMIsolationPool(adapter_factory=lambda: None, settings=_settings())
    await pool.start()
    w = await pool._acquire_idle_worker("s1", "r1", "q1", "/flowjet/data/workspaces/wsA")
    assert pool.cancel_run("r1") is True
    # kill is async (fire-and-forget via ensure_future); let the loop run it.
    await asyncio.sleep(0.01)
    assert w.command_handle.killed
    assert pool.cancel_run("nope") is False
    await pool._release_worker(w, _request("/flowjet/data/workspaces/wsA", "s1", "r1"))
    await pool.shutdown()
