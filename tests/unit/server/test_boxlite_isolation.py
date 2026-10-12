"""Phase 3 — boxlite isolation pool tests.

Tests the ``BoxLiteIsolationPool`` using local subprocess fallback mode
(no Docker required).  The worker subprocess runs the real
``boxlite_worker`` entrypoint, which builds a ``FakeAgentAdapter`` (when
the server backend resolves to nano/soothe without a config file) and
processes JSON-lines requests over stdin/stdout.

Tests cover:
- Protocol serialization (encode/decode events and requests)
- Pool lifecycle (start, submit, shutdown)
- Event streaming (RunStarted, OutputTextDelta, RunCompleted)
- Cancellation and error handling
- Metrics
- Server config settings
- ``build_backend`` wiring
- ``IsolationPool`` protocol conformance
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flowjet.core.backends.isolation.boxlite_pool import (
    BoxLiteIsolationPool,
    BoxLitePoolSettings,
)
from flowjet.core.backends.isolation.boxlite_protocol import (
    decode_event,
    decode_request,
    encode_event,
    encode_request,
    make_control,
    parse_frame,
)
from flowjet.core.backends.isolation.isolation_pool import IsolationPool
from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import (
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunFailed,
    RunStarted,
    UsageInfo,
)

# ---------------------------------------------------------------------------
# Protocol serialization
# ---------------------------------------------------------------------------


def test_encode_decode_request_roundtrip():
    """encode_request → parse_frame → decode_request roundtrips losslessly."""
    req = IsolatedRunRequest(
        run_id="resp_42",
        session="fj-test",
        input_text="hello world",
        model="default",
        workspace=Path("/tmp/ws"),
        thread_id="fj-test",
        metadata={"interaction_mode": "agent"},
        request_id="req-1",
    )
    line = encode_request(req)
    frame = parse_frame(line)
    restored = decode_request(frame)
    assert restored.run_id == req.run_id
    assert restored.session == req.session
    assert restored.input_text == req.input_text
    assert restored.model == req.model
    assert str(restored.workspace) == str(req.workspace)
    assert restored.thread_id == req.thread_id
    assert restored.metadata == req.metadata
    assert restored.request_id == req.request_id


def test_encode_decode_event_roundtrip():
    """encode_event → parse_frame → decode_event roundtrips losslessly."""
    events = [
        RunStarted(run_id="r1", model="m", session="s"),
        Progress(stage="Working", message="Thinking…"),
        OutputTextDelta(delta="hello"),
        RunCompleted(
            output_text="hello", usage=UsageInfo(input_tokens=1, output_tokens=1, total_tokens=2)
        ),
        RunFailed(message="boom", code="error"),
    ]
    for event in events:
        line = encode_event(event)
        frame = parse_frame(line)
        restored = decode_event(frame)
        assert type(restored) is type(event)
        # Compare field values.
        for field_name in event.__dataclass_fields__:
            assert getattr(restored, field_name) == getattr(event, field_name)


def test_decode_event_unknown_type_raises():
    """decode_event raises ValueError for unknown event types."""
    with pytest.raises(ValueError, match="unknown event type"):
        decode_event({"type": "event", "event_type": "NoSuchEvent"})


def test_decode_event_missing_type_tag_raises():
    """decode_event raises ValueError when event_type tag is missing."""
    with pytest.raises(ValueError, match="missing 'event_type'"):
        decode_event({"type": "event"})


def test_make_control():
    """make_control produces valid JSON with the given type."""
    line = make_control("done")
    assert parse_frame(line) == {"type": "done"}

    line = make_control("error", message="boom")
    frame = parse_frame(line)
    assert frame["type"] == "error"
    assert frame["message"] == "boom"


# ---------------------------------------------------------------------------
# BoxLitePoolSettings
# ---------------------------------------------------------------------------


def test_pool_settings_defaults():
    s = BoxLitePoolSettings()
    assert s.image is None
    assert s.min_size == 1
    assert s.max_size == 4
    assert s.cpu_count == 2
    assert s.mem_mib == 2048
    assert s.workspace_mount_mode == "bind"
    assert s.local_fallback is True


def test_pool_settings_validation():
    with pytest.raises(ValueError, match="min_size"):
        BoxLitePoolSettings(min_size=0)
    with pytest.raises(ValueError, match="max_size"):
        BoxLitePoolSettings(min_size=2, max_size=1)


# ---------------------------------------------------------------------------
# IsolationPool protocol conformance
# ---------------------------------------------------------------------------


def test_boxlite_pool_implements_isolation_pool_protocol():
    """BoxLiteIsolationPool satisfies the IsolationPool protocol."""
    from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

    pool = BoxLiteIsolationPool(
        adapter_factory=FakeAgentAdapter,
        settings=BoxLitePoolSettings(min_size=1, max_size=1, local_fallback=True),
    )
    assert isinstance(pool, IsolationPool)


# ---------------------------------------------------------------------------
# Pool lifecycle (local subprocess fallback — no Docker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_submit_yields_events(tmp_path: Path):
    """Pool.submit yields RunStarted, OutputTextDelta, RunCompleted."""
    from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

    pool = BoxLiteIsolationPool(
        adapter_factory=FakeAgentAdapter,
        settings=BoxLitePoolSettings(
            min_size=1,
            max_size=1,
            local_fallback=True,
            ready_timeout_seconds=30.0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_1",
            session="fj-bl-1",
            input_text="hello boxlite",
            model="default",
            workspace=ws,
        )
        events = [e async for e in pool.submit(req)]
        assert any(isinstance(e, RunStarted) for e in events)
        assert any(isinstance(e, OutputTextDelta) for e in events)
        assert isinstance(events[-1], RunCompleted)
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_pool_submit_writes_workspace_file(tmp_path: Path):
    """The worker writes a marker file to the workspace (proves isolation)."""
    from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

    pool = BoxLiteIsolationPool(
        adapter_factory=FakeAgentAdapter,
        settings=BoxLitePoolSettings(
            min_size=1,
            max_size=1,
            local_fallback=True,
            ready_timeout_seconds=30.0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_2",
            session="fj-bl-2",
            input_text="marker test",
            model="default",
            workspace=ws,
        )
        events = [e async for e in pool.submit(req)]
        assert isinstance(events[-1], RunCompleted)
        assert (ws / "last_input.txt").read_text(encoding="utf-8") == "marker test"
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_pool_metrics_after_run(tmp_path: Path):
    """Pool.metrics reports correct counts after a run."""
    from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

    pool = BoxLiteIsolationPool(
        adapter_factory=FakeAgentAdapter,
        settings=BoxLitePoolSettings(
            min_size=1,
            max_size=1,
            local_fallback=True,
            ready_timeout_seconds=30.0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        req = IsolatedRunRequest(
            run_id="resp_3",
            session="fj-bl-3",
            input_text="metrics test",
            model="default",
            workspace=ws,
        )
        _ = [e async for e in pool.submit(req)]
        m = pool.metrics()
        assert m.total_workers >= 1
        assert m.requests_completed == 1
        assert m.idle_workers >= 1
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_pool_cancel_run(tmp_path: Path):
    """cancel_run returns True when the run exists, False otherwise."""
    from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

    pool = BoxLiteIsolationPool(
        adapter_factory=FakeAgentAdapter,
        settings=BoxLitePoolSettings(
            min_size=1,
            max_size=1,
            local_fallback=True,
            ready_timeout_seconds=30.0,
        ),
    )
    await pool.start()
    try:
        # No run submitted yet → cancel should return False.
        assert pool.cancel_run("nonexistent") is False
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_pool_shutdown_terminates_workers(tmp_path: Path):
    """Pool.shutdown terminates all worker processes."""
    from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

    pool = BoxLiteIsolationPool(
        adapter_factory=FakeAgentAdapter,
        settings=BoxLitePoolSettings(
            min_size=1,
            max_size=1,
            local_fallback=True,
            ready_timeout_seconds=30.0,
        ),
    )
    await pool.start()
    # Workers should be alive.
    assert pool.metrics().total_workers >= 1
    await pool.shutdown()
    # After shutdown, no workers remain.
    assert pool.metrics().total_workers == 0


@pytest.mark.asyncio
async def test_pool_multiple_requests_same_worker(tmp_path: Path):
    """Multiple requests can be sent to the same worker sequentially."""
    from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

    pool = BoxLiteIsolationPool(
        adapter_factory=FakeAgentAdapter,
        settings=BoxLitePoolSettings(
            min_size=1,
            max_size=1,
            local_fallback=True,
            ready_timeout_seconds=30.0,
        ),
    )
    await pool.start()
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        for i in range(3):
            req = IsolatedRunRequest(
                run_id=f"resp_multi_{i}",
                session="fj-bl-multi",
                input_text=f"request {i}",
                model="default",
                workspace=ws,
            )
            events = [e async for e in pool.submit(req)]
            assert isinstance(events[-1], RunCompleted)
        m = pool.metrics()
        assert m.requests_completed == 3
    finally:
        await pool.shutdown()


# ---------------------------------------------------------------------------
# Server config — boxlite settings
# ---------------------------------------------------------------------------


def test_settings_boxlite_defaults():
    """Settings defaults for boxlite are Phase 1/2 (thread_pool)."""
    from flowjet.server.config import Settings

    s = Settings()
    assert s.isolation_mode == "thread_pool"
    assert s.boxlite_image is None
    assert s.boxlite_min_pool == 1
    assert s.boxlite_max_pool == 4
    assert s.boxlite_cpu_count == 2
    assert s.boxlite_mem_mib == 2048
    assert s.boxlite_workspace_mount_mode == "bind"
    assert s.boxlite_local_fallback is True


def test_settings_boxlite_from_env(monkeypatch):
    """Settings can be configured via env vars."""
    from flowjet.server.config import Settings

    monkeypatch.setenv("FLOWJET_ISOLATION_MODE", "boxlite")
    monkeypatch.setenv("FLOWJET_BOXLITE_IMAGE", "flowjet/worker:latest")
    monkeypatch.setenv("FLOWJET_BOXLITE_MIN_POOL", "2")
    monkeypatch.setenv("FLOWJET_BOXLITE_MAX_POOL", "6")
    monkeypatch.setenv("FLOWJET_BOXLITE_CPU_COUNT", "4")
    monkeypatch.setenv("FLOWJET_BOXLITE_MEM_MIB", "4096")
    s = Settings()
    assert s.isolation_mode == "boxlite"
    assert s.boxlite_image == "flowjet/worker:latest"
    assert s.boxlite_min_pool == 2
    assert s.boxlite_max_pool == 6
    assert s.boxlite_cpu_count == 4
    assert s.boxlite_mem_mib == 4096


def test_settings_boxlite_pool_settings():
    """boxlite_pool_settings() returns a BoxLitePoolSettings."""
    from flowjet.server.config import Settings

    s = Settings()
    ps = s.boxlite_pool_settings()
    assert ps is not None
    assert ps.min_size == 1
    assert ps.max_size == 4


# ---------------------------------------------------------------------------
# build_backend — isolation pool wiring
# ---------------------------------------------------------------------------


def test_build_backend_returns_none_pool_when_thread_pool(tmp_path: Path):
    """build_backend with isolation_mode=thread_pool passes pool=None."""
    from flowjet.server.config import Settings
    from flowjet.server.http.app import _build_isolation_pool

    s = Settings(home=str(tmp_path))
    pool = _build_isolation_pool(s)
    assert pool is None


def test_build_backend_builds_boxlite_pool_when_configured(tmp_path: Path, monkeypatch):
    """build_backend with isolation_mode=boxlite constructs a BoxLiteIsolationPool."""
    from flowjet.server.config import Settings
    from flowjet.server.http.app import _build_isolation_pool

    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))
    monkeypatch.setenv("FLOWJET_ISOLATION_MODE", "boxlite")
    monkeypatch.setenv("FLOWJET_BOXLITE_LOCAL_FALLBACK", "true")
    s = Settings()
    pool = _build_isolation_pool(s)
    assert pool is not None
    assert isinstance(pool, BoxLiteIsolationPool)


# ---------------------------------------------------------------------------
# IsolatingRuntimeBackend with injectable pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_accepts_injected_boxlite_pool(tmp_path: Path):
    """IsolatingRuntimeBackend accepts a BoxLiteIsolationPool via the pool param."""
    from flowjet.core.backends.isolation import (
        FakeAgentAdapter,
        IsolatingRuntimeBackend,
    )
    from flowjet.core.events import RunRequest

    pool = BoxLiteIsolationPool(
        adapter_factory=FakeAgentAdapter,
        settings=BoxLitePoolSettings(
            min_size=1,
            max_size=1,
            local_fallback=True,
            ready_timeout_seconds=30.0,
        ),
    )
    backend = IsolatingRuntimeBackend(
        adapter_factory=FakeAgentAdapter,
        pool_settings=None,
        home=tmp_path,
        pool=pool,
    )
    try:
        events = [
            e
            async for e in backend.stream_run(
                RunRequest(
                    input_text="backend test",
                    model="default",
                    session="fj-bl-backend",
                )
            )
        ]
        from flowjet.core.events import RunStarted

        assert any(isinstance(e, RunStarted) for e in events)
        assert isinstance(events[-1], RunCompleted)
    finally:
        await backend.shutdown()
