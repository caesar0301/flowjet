"""Comprehensive ASGI integration coverage of flowjet-server public features.

Maps to RFC-001 (OpenAI surface + projection), RFC-002 (isolation), and
RFC-003 (production hardening). Uses IsolatingRuntimeBackend + FakeAgentAdapter
so the full HTTP → pool → adapter path is exercised without a live LLM.
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
from flowjet.server.config import Settings
from flowjet.server.http.app import create_app


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def backend(home: Path) -> IsolatingRuntimeBackend:
    return IsolatingRuntimeBackend(
        models=["default", "researcher"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=2, max_size=4, max_requests_per_worker=0),
        home=home,
        allow_external_workspace=False,
    )


@pytest.fixture
async def client(backend: IsolatingRuntimeBackend, home: Path):
    app = create_app(
        settings=Settings(api_key=None, models="default,researcher", home=str(home)),
        backend=backend,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await backend.shutdown()


# --- Health & models ---


async def test_feature_health_status_and_pool(client: AsyncClient) -> None:
    # Start pool via a create so metrics are meaningful.
    await client.post(
        "/v1/responses",
        json={"model": "default", "input": "warm", "flowjet": {"session": "fj-warm"}},
    )
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "pool" in body
    assert body["pool"]["total_workers"] >= 1


async def test_feature_list_models(client: AsyncClient) -> None:
    r = await client.get("/v1/models")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert ids == {"default", "researcher"}


# --- Create / retrieve / delete ---


async def test_feature_create_retrieve_delete_lifecycle(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "hello lifecycle",
            "flowjet": {"session": "fj-life", "projection": "report"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert "hello lifecycle" in body["output"][0]["content"][0]["text"]
    rid = body["id"]

    got = await client.get(f"/v1/responses/{rid}")
    assert got.status_code == 200
    assert got.json()["id"] == rid

    deleted = await client.delete(f"/v1/responses/{rid}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    missing = await client.get(f"/v1/responses/{rid}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "response_not_found"


async def test_feature_list_input_messages(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": [
                {"role": "user", "content": "part-a"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "part-b"}],
                },
            ],
            "flowjet": {"session": "fj-list-in"},
        },
    )
    assert r.status_code == 200
    text = r.json()["output"][0]["content"][0]["text"]
    assert "part-a" in text or "part-b" in text


# --- Streaming + projections ---


async def test_feature_stream_report_lifecycle(client: AsyncClient, parse_sse) -> None:
    r = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "stream report",
            "stream": True,
            "flowjet": {"session": "fj-s-report", "projection": "report"},
        },
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    types = [e["type"] for e in parse_sse(r.text)]
    assert "response.created" in types
    assert "response.output_text.delta" in types
    assert "response.completed" in types
    assert "response.flowjet.progress" not in types


async def test_feature_stream_progress_projection(client: AsyncClient, parse_sse) -> None:
    r = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "stream progress",
            "stream": True,
            "flowjet": {"session": "fj-s-prog", "projection": "progress"},
        },
    )
    types = [e["type"] for e in parse_sse(r.text)]
    assert "response.flowjet.progress" in types
    assert "response.completed" in types


async def test_feature_stream_developer_projection(client: AsyncClient, parse_sse) -> None:
    r = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "stream developer",
            "stream": True,
            "flowjet": {"session": "fj-s-dev", "projection": "developer"},
        },
    )
    types = [e["type"] for e in parse_sse(r.text)]
    # Fake adapter emits Progress; developer mode still includes progress-class events.
    assert "response.completed" in types
    assert "response.output_text.delta" in types


# --- Auth & errors ---


async def test_feature_bearer_auth_gate(home: Path) -> None:
    backend = IsolatingRuntimeBackend(
        models=["default"],
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=1, max_requests_per_worker=0),
        home=home,
    )
    app = create_app(
        settings=Settings(api_key="secret", models="default", home=str(home)),
        backend=backend,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/v1/models")
        assert denied.status_code == 401
        ok = await client.get("/v1/models", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
        # Health stays open.
        health = await client.get("/health")
        assert health.status_code == 200
    await backend.shutdown()


async def test_feature_unknown_model(client: AsyncClient) -> None:
    r = await client.post("/v1/responses", json={"model": "nope", "input": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


async def test_feature_retrieve_missing(client: AsyncClient) -> None:
    r = await client.get("/v1/responses/resp_does_not_exist")
    assert r.status_code == 404


# --- FlowJet options / isolation policy ---


async def test_feature_session_workspace_isolation(client: AsyncClient, home: Path) -> None:
    for session, text in (("fj-a", "alpha"), ("fj-b", "beta")):
        r = await client.post(
            "/v1/responses",
            json={"model": "default", "input": text, "flowjet": {"session": session}},
        )
        assert r.status_code == 200

    workspaces = list((home / "data" / "workspaces").iterdir())
    assert len(workspaces) == 2
    texts = {p.joinpath("last_input.txt").read_text(encoding="utf-8") for p in workspaces}
    assert texts == {"alpha", "beta"}


async def test_feature_workspace_under_home_override(client: AsyncClient, home: Path) -> None:
    custom = home / "projects" / "demo"
    r = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "custom-ws",
            "flowjet": {
                "session": "fj-custom",
                "metadata": {"workspace": str(custom)},
            },
        },
    )
    assert r.status_code == 200
    assert (custom / "last_input.txt").read_text(encoding="utf-8") == "custom-ws"


async def test_feature_external_workspace_rejected(client: AsyncClient, home: Path) -> None:
    outside = home.parent / "escape-ws"
    r = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "nope",
            "flowjet": {
                "session": "fj-escape",
                "metadata": {"workspace": str(outside)},
            },
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_workspace"


async def test_feature_interaction_mode_ask_then_pin(client: AsyncClient) -> None:
    ok = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "ask turn",
            "flowjet": {"session": "fj-ask", "interaction_mode": "ask"},
        },
    )
    assert ok.status_code == 200
    conflict = await client.post(
        "/v1/responses",
        json={
            "model": "default",
            "input": "agent flip",
            "flowjet": {"session": "fj-ask", "interaction_mode": "agent"},
        },
    )
    assert conflict.status_code == 400
    assert conflict.json()["error"]["code"] == "interaction_mode_conflict"


async def test_feature_parallel_sessions_complete(client: AsyncClient) -> None:
    import asyncio

    async def one(i: int) -> int:
        r = await client.post(
            "/v1/responses",
            json={
                "model": "default",
                "input": f"parallel {i}",
                "flowjet": {"session": f"fj-par-{i}"},
            },
        )
        return r.status_code

    codes = await asyncio.gather(*[one(i) for i in range(6)])
    assert codes == [200] * 6


async def test_feature_same_session_serialized(client: AsyncClient) -> None:
    import asyncio

    async def one(label: str) -> str:
        r = await client.post(
            "/v1/responses",
            json={
                "model": "default",
                "input": label,
                "flowjet": {"session": "fj-serial"},
            },
        )
        assert r.status_code == 200
        return r.json()["output"][0]["content"][0]["text"]

    texts = await asyncio.gather(one("first"), one("second"))
    assert all(t.startswith("Echo:") for t in texts)
