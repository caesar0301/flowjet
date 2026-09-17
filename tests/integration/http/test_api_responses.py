"""HTTP API integration tests against FakeRuntimeBackend."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from flowjet.core.backends.fake import FakeRuntimeBackend
from flowjet.server.config import Settings
from flowjet.server.http.app import create_app


@pytest.fixture
def app():
    return create_app(
        settings=Settings(api_key=None, models="default,researcher"),
        backend=FakeRuntimeBackend(models=["default", "researcher"]),
    )


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_list_models(client: AsyncClient) -> None:
    r = await client.get("/v1/models")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert ids == {"default", "researcher"}


async def test_create_non_stream(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/responses",
        json={"model": "default", "input": "hello world"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"].startswith("Echo: hello world")
    rid = body["id"]

    g = await client.get(f"/v1/responses/{rid}")
    assert g.status_code == 200
    assert g.json()["id"] == rid

    d = await client.delete(f"/v1/responses/{rid}")
    assert d.status_code == 200
    assert d.json()["deleted"] is True

    missing = await client.get(f"/v1/responses/{rid}")
    assert missing.status_code == 404


async def test_create_stream_progress(client: AsyncClient, parse_sse) -> None:
    r = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "stream me",
            "stream": True,
            "flowjet": {"projection": "progress"},
        },
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = parse_sse(r.text)
    types = [e["type"] for e in events]
    assert "response.created" in types
    assert "response.flowjet.progress" in types
    assert "response.output_text.delta" in types
    assert "response.completed" in types


async def test_auth_required() -> None:
    app = create_app(
        settings=Settings(api_key="secret", models="default"),
        backend=FakeRuntimeBackend(models=["default"]),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/v1/models")
        assert denied.status_code == 401
        ok = await client.get("/v1/models", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200


async def test_unknown_model(client: AsyncClient) -> None:
    r = await client.post("/v1/responses", json={"model": "nope", "input": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
