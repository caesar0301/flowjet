"""HTTP integration for RFC-002 isolation: the full ASGI app path.

Exercises the HTTP layer -> ResponseService -> IsolatingRuntimeBackend -> pool
-> FakeAgentAdapter path end-to-end without a live LLM. The pure unit tests
for workspace resolution, admission, thread pool, and ContextVar isolation live
in ``tests/unit/test_isolation.py``.
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


@pytest.mark.asyncio
async def test_isolating_backend_via_http(tmp_path: Path):
    backend = IsolatingRuntimeBackend(
        models=["default"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=2),
        home=tmp_path,
    )
    app = create_app(backend=backend)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/responses",
            json={
                "model": "default",
                "input": "http-hi",
                "flowjet": {"session": "fj-http"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert "http-hi" in body["output"][0]["content"][0]["text"]
    await backend.shutdown()
