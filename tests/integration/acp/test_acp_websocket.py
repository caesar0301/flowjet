"""Integration tests for the ACP WebSocket endpoint at /acp.

Exercises the full JSON-RPC flow: initialize → session/new → session/prompt,
verifying that agent message chunks and the terminal prompt response are
delivered over the WebSocket.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from flowjet.core.backends.fake import FakeRuntimeBackend
from flowjet.server.config import Settings
from flowjet.server.http.app import create_app


def _recv_until(ws, predicate, max_messages: int = 100) -> list[dict]:
    """Receive WebSocket text frames until ``predicate(msg)`` is true."""
    messages: list[dict] = []
    for _ in range(max_messages):
        raw = ws.receive_text()
        msg = json.loads(raw)
        messages.append(msg)
        if predicate(msg):
            return messages
    raise AssertionError(f"predicate never satisfied after {max_messages} messages")


@pytest.fixture()
def acp_client():
    app = create_app(
        settings=Settings(api_key=None, models="default"),
        backend=FakeRuntimeBackend(models=["default"]),
    )
    with TestClient(app) as client:
        yield client


def _initialize(ws) -> dict:
    ws.send_text(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "test-client", "version": "0.1.0"},
                },
            }
        )
    )
    msgs = _recv_until(ws, lambda m: m.get("id") == 1)
    return msgs[-1]


def _new_session(ws, request_id: int = 2, cwd: str = "/tmp") -> str:
    ws.send_text(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/new",
                "params": {"cwd": cwd, "mcpServers": []},
            }
        )
    )
    msgs = _recv_until(ws, lambda m: m.get("id") == request_id)
    resp = msgs[-1]
    assert "result" in resp, f"session/new failed: {resp}"
    return resp["result"]["sessionId"]


class TestAcpInitialize:
    def test_initialize_returns_protocol_version(self, acp_client):
        with acp_client.websocket_connect("/acp") as ws:
            resp = _initialize(ws)
        assert resp["jsonrpc"] == "2.0"
        assert resp["result"]["protocolVersion"] == 1

    def test_initialize_returns_agent_info(self, acp_client):
        with acp_client.websocket_connect("/acp") as ws:
            resp = _initialize(ws)
        info = resp["result"]["agentInfo"]
        assert info["name"] == "flowjet-server"
        assert "version" in info

    def test_initialize_advertises_load_session(self, acp_client):
        with acp_client.websocket_connect("/acp") as ws:
            resp = _initialize(ws)
        caps = resp["result"]["agentCapabilities"]
        assert caps["loadSession"] is True


class TestAcpSession:
    def test_new_session_returns_session_id(self, acp_client):
        with acp_client.websocket_connect("/acp") as ws:
            _initialize(ws)
            session_id = _new_session(ws)
        assert session_id.startswith("acp-")

    def test_list_sessions_returns_empty(self, acp_client):
        with acp_client.websocket_connect("/acp") as ws:
            _initialize(ws)
            ws.send_text(
                json.dumps({"jsonrpc": "2.0", "id": 10, "method": "session/list", "params": {}})
            )
            msgs = _recv_until(ws, lambda m: m.get("id") == 10)
        resp = msgs[-1]
        assert resp["result"]["sessions"] == []


class TestAcpPrompt:
    def test_prompt_streams_agent_message_and_completes(self, acp_client):
        with acp_client.websocket_connect("/acp") as ws:
            _initialize(ws)
            session_id = _new_session(ws)

            ws.send_text(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {
                            "sessionId": session_id,
                            "prompt": [{"type": "text", "text": "Hello ACP!"}],
                        },
                    }
                )
            )

            msgs = _recv_until(ws, lambda m: m.get("id") == 3)

        # The prompt response should signal end_turn.
        prompt_resp = msgs[-1]
        assert prompt_resp["result"]["stopReason"] == "end_turn"

        # At least one session/update notification should carry agent_message_chunk.
        agent_chunks = [
            m
            for m in msgs
            if m.get("method") == "session/update"
            and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        assert len(agent_chunks) >= 1, "expected at least one agent_message_chunk"

        # Reassemble the streamed text.
        text = "".join(m["params"]["update"]["content"]["text"] for m in agent_chunks)
        assert "Echo: Hello ACP!" in text

    def test_prompt_includes_usage(self, acp_client):
        with acp_client.websocket_connect("/acp") as ws:
            _initialize(ws)
            session_id = _new_session(ws)
            ws.send_text(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {
                            "sessionId": session_id,
                            "prompt": [{"type": "text", "text": "Hi"}],
                        },
                    }
                )
            )
            msgs = _recv_until(ws, lambda m: m.get("id") == 3)
        usage = msgs[-1]["result"]["usage"]
        assert usage["totalTokens"] > 0
        assert usage["inputTokens"] > 0
        assert usage["outputTokens"] > 0
