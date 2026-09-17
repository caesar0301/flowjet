"""Integration tests for the /v1/chat/completions endpoint and CORS.

These tests verify that the Chat Completions API surface works for
browser-based OpenAI-compatible clients (e.g. Airi) that default to the
``chat-completions`` protocol rather than the Responses API.
"""

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


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


async def test_cors_preflight_models(client: AsyncClient) -> None:
    """Browser preflight (OPTIONS) on /v1/models must return CORS headers.

    With ``allow_credentials=True`` the CORS spec forbids ``"*"`` for the
    ``Access-Control-Allow-Origin`` header; Starlette reflects the request
    Origin instead.  This is the correct, browser-accepted behaviour for
    credentialed cross-origin requests.
    """
    r = await client.options(
        "/v1/models",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "GET" in r.headers["access-control-allow-methods"]


async def test_cors_preflight_chat_completions(client: AsyncClient) -> None:
    """Browser preflight (OPTIONS) on /v1/chat/completions must pass."""
    r = await client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in r.headers["access-control-allow-methods"]


async def test_cors_actual_request_models(client: AsyncClient) -> None:
    """Actual GET /v1/models with an Origin header must include CORS headers."""
    r = await client.get(
        "/v1/models",
        headers={"Origin": "http://localhost:5173"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


# ---------------------------------------------------------------------------
# Chat Completions — non-streaming
# ---------------------------------------------------------------------------


async def test_chat_completion_non_stream(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "default",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hello world"},
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "default"
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"].startswith("Echo:")
    assert choice["finish_reason"] == "stop"
    assert "usage" in body
    assert body["usage"]["total_tokens"] >= 0


async def test_chat_completion_cors_headers(client: AsyncClient) -> None:
    """Chat completions response must include CORS headers for browser clients."""
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "default",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"Origin": "http://localhost:5173"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_chat_completion_unknown_model(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "nope", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


async def test_chat_completion_multi_content(client: AsyncClient) -> None:
    """Messages with multi-part content arrays should be flattened."""
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "default",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "part one "},
                        {"type": "text", "text": "part two"},
                    ],
                }
            ],
        },
    )
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert "part one" in content or "part two" in content


# ---------------------------------------------------------------------------
# Chat Completions — streaming
# ---------------------------------------------------------------------------


async def test_chat_completion_stream(client: AsyncClient, parse_sse) -> None:
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "default",
            "messages": [{"role": "user", "content": "stream me"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    chunks = parse_sse(r.text)
    # First chunk should set the role
    first = chunks[0]
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"]["role"] == "assistant"

    # At least one content delta
    content_deltas = [
        c for c in chunks if c.get("choices", [{}])[0].get("delta", {}).get("content")
    ]
    assert len(content_deltas) >= 1

    # Last chunk before [DONE] should have finish_reason
    finish_chunks = [c for c in chunks if c.get("choices", [{}])[0].get("finish_reason") == "stop"]
    assert len(finish_chunks) >= 1

    # [DONE] sentinel
    assert r.text.rstrip().endswith("[DONE]")


async def test_chat_completion_stream_cors(client: AsyncClient) -> None:
    """Streaming chat completions must also include CORS headers."""
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "default",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers={"Origin": "http://localhost:5173"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


# ---------------------------------------------------------------------------
# Auth still works on chat completions
# ---------------------------------------------------------------------------


async def test_chat_completion_auth_required() -> None:
    app = create_app(
        settings=Settings(api_key="secret", models="default"),
        backend=FakeRuntimeBackend(models=["default"]),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": [{"role": "user", "content": "x"}]},
        )
        assert denied.status_code == 401
        ok = await client.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer secret"},
        )
        assert ok.status_code == 200
