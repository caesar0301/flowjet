"""RFC-003 production isolation hardening — HTTP / service integration tests.

These exercise the full path across the ASGI app, ResponseService,
IsolatingRuntimeBackend, and the thread pool. The pure unit tests for
workspace resolution, interaction-mode gate, thread-pool backpressure/timeouts,
and orphan-task cleanup live in ``tests/unit/test_production_isolation.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from flowjet.core.backends.isolation import (
    FakeAgentAdapter,
    IsolatingRuntimeBackend,
    PoolSettings,
)
from flowjet.server.http.app import create_app
from flowjet.server.openai_compat.errors import OpenAIError
from flowjet.server.openai_compat.schemas import CreateResponseRequest, FlowjetOptions
from flowjet.server.openai_compat.service import ResponseService


@pytest.mark.asyncio
async def test_backend_mode_conflict_via_service(tmp_path: Path):
    backend = IsolatingRuntimeBackend(
        models=["default"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=1, max_requests_per_worker=0),
        home=tmp_path,
    )
    service = ResponseService(backend=backend)
    try:
        body_ask = CreateResponseRequest(
            model="default",
            input="one",
            flowjet=FlowjetOptions(session="fj-pin", interaction_mode="ask"),
        )
        out = await service.create(body_ask)
        assert out["status"] == "completed"

        body_agent = CreateResponseRequest(
            model="default",
            input="two",
            flowjet=FlowjetOptions(session="fj-pin", interaction_mode="agent"),
        )
        with pytest.raises(OpenAIError) as exc:
            await service.create(body_agent)
        assert exc.value.code == "interaction_mode_conflict"
        assert exc.value.status_code == 400
    finally:
        await backend.shutdown()


@pytest.mark.asyncio
async def test_http_invalid_workspace_returns_400(tmp_path: Path):
    backend = IsolatingRuntimeBackend(
        models=["default"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=1, max_requests_per_worker=0),
        home=tmp_path,
        allow_external_workspace=False,
    )
    app = create_app(backend=backend)
    outside = str(tmp_path.parent / "not-allowed-ws")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/responses",
            json={
                "model": "default",
                "input": "x",
                "flowjet": {
                    "session": "fj-ws",
                    "metadata": {"workspace": outside},
                },
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_workspace"
    await backend.shutdown()


@pytest.mark.asyncio
async def test_http_mode_conflict_returns_400(tmp_path: Path):
    backend = IsolatingRuntimeBackend(
        models=["default"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=1, max_requests_per_worker=0),
        home=tmp_path,
    )
    app = create_app(backend=backend)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post(
            "/v1/responses",
            json={
                "model": "default",
                "input": "one",
                "flowjet": {"session": "fj-m", "interaction_mode": "ask"},
            },
        )
        assert ok.status_code == 200
        bad = await client.post(
            "/v1/responses",
            json={
                "model": "default",
                "input": "two",
                "flowjet": {"session": "fj-m", "interaction_mode": "agent"},
            },
        )
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "interaction_mode_conflict"
    await backend.shutdown()


@pytest.mark.asyncio
async def test_health_includes_pool_metrics(tmp_path: Path):
    backend = IsolatingRuntimeBackend(
        models=["default"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=1, max_requests_per_worker=0),
        home=tmp_path,
    )
    app = create_app(backend=backend)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/responses",
            json={"model": "default", "input": "hi", "flowjet": {"session": "fj-h"}},
        )
        health = await client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        pool = body["pool"]
        for key in (
            "total_workers",
            "idle_workers",
            "busy_workers",
            "dead_workers",
            "requests_completed",
            "ready_timeouts",
            "events_dropped",
        ):
            assert key in pool
        assert pool["total_workers"] >= 1
        assert pool["requests_completed"] >= 1
    await backend.shutdown()


@pytest.mark.asyncio
async def test_pool_metrics_snapshot_shape(tmp_path: Path):
    backend = IsolatingRuntimeBackend(
        models=["default"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=2, max_size=2, max_requests_per_worker=0),
        home=tmp_path,
    )
    await backend.pool.start()
    try:
        m = backend.pool_metrics()
        assert m["total_workers"] == 2
        assert m["idle_workers"] == 2
        assert m["busy_workers"] == 0
        assert isinstance(m["events_dropped"], int)
    finally:
        await backend.shutdown()
