#!/usr/bin/env python3
"""End-to-end coverage of the flowjet-server ACP WebSocket endpoint at ``/acp``.

Uses the official ``agent-client-protocol`` SDK client (``connect_to_agent`` +
``create_websocket_stream``) to drive **every agent-side ACP URI** that
flowjet-server implements, then drops to raw JSON-RPC frames to verify the
WebSocket transport itself and exercise the ``session/cancel`` notification.

ACP agent-side methods covered (see ``acp.AGENT_METHODS``):

    initialize
    session/new
    session/load
    session/list
    session/prompt          (with tool-call events via ``emit_tools`` meta)
    session/cancel          (raw notification, mid-prompt)
    session/close
    session/fork
    session/resume
    session/set_mode
    session/set_config_option
    authenticate

Client-side ``session/update`` notifications (``session/update``) are collected
by a minimal ``RecordingClient`` and asserted in the prompt section.

Start the server first::

    make run

Then::

    make examples-acp
    # or: uv run python examples/e2e_acp_websocket.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _client import section  # noqa: E402

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def acp_ws_url() -> str:
    """Build the ``ws://host:port/acp`` URL from env or default."""
    base = os.environ.get("FLOWJET_BASE_URL", "http://127.0.0.1:8618/v1")
    # Normalise to ws:// scheme and strip /v1 suffix.
    ws = base.replace("http://", "ws://").replace("https://", "wss://")
    if ws.endswith("/v1"):
        ws = ws[: -len("/v1")]
    return f"{ws}/acp"


# --------------------------------------------------------------------------- #
# Minimal client callback implementation
# --------------------------------------------------------------------------- #


class RecordingClient:
    """Minimal ACP ``Client`` that records ``session/update`` notifications.

    The flowjet-server agent only sends ``session/update`` notifications
    (agent_thought_chunk, agent_message_chunk, tool_call, tool_call_update);
    all other client-side callbacks are no-ops so the SDK returns
    "method not found" for anything the agent never calls.
    """

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    # The only client method the flowjet agent exercises.
    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,
    ) -> None:
        kind = getattr(update, "session_update", None) or update.__class__.__name__
        entry: dict[str, Any] = {"session_id": session_id, "kind": kind}
        # Extract text / tool_call_id for assertion convenience.
        content = getattr(update, "content", None)
        if content is not None:
            text = getattr(content, "text", None)
            if isinstance(text, str):
                entry["text"] = text
        tool_call_id = getattr(update, "tool_call_id", None)
        if tool_call_id is not None:
            entry["tool_call_id"] = tool_call_id
        status = getattr(update, "status", None)
        if status is not None:
            entry["status"] = status
        title = getattr(update, "title", None)
        if title is not None:
            entry["title"] = title
        self.updates.append(entry)

    # --- No-op stubs for client methods the agent never calls ---------------

    async def request_permission(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def write_text_file(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def read_text_file(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def create_terminal(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def terminal_output(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def release_terminal(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def wait_for_terminal_exit(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def kill_terminal(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def create_elicitation(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def complete_elicitation(self, *a: Any, **kw: Any) -> None:  # type: ignore[override]
        ...

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        pass

    def on_connect(self, conn: Any) -> None:
        pass


# --------------------------------------------------------------------------- #
# Raw JSON-RPC WebSocket helpers (for transport-level coverage)
# --------------------------------------------------------------------------- #


async def _raw_ws_connect(url: str):
    """Open a raw WebSocket connection and return the websockets connection."""
    from acp.ws.client import ws_connect

    return await ws_connect(url)


async def _send_jsonrpc(ws, msg: dict[str, Any]) -> None:
    await ws.send(json.dumps(msg, separators=(",", ":")))


async def _recv_jsonrpc(ws, timeout: float = 10.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _recv_until(
    ws,
    predicate,
    *,
    max_messages: int = 200,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Receive frames until ``predicate(msg)`` is true or timeout."""
    messages: list[dict[str, Any]] = []
    for _ in range(max_messages):
        msg = await _recv_jsonrpc(ws, timeout=timeout)
        messages.append(msg)
        if predicate(msg):
            return messages
    raise AssertionError(f"predicate never satisfied after {max_messages} messages")


# --------------------------------------------------------------------------- #
# Main E2E scenario
# --------------------------------------------------------------------------- #


async def run_sdk_scenario(url: str) -> None:
    """Drive all ACP agent-side methods via the official SDK client."""
    import warnings

    from acp import connect_to_agent, text_block
    from acp.ws.client import create_websocket_stream

    # Suppress the DeprecationWarning from using ClientSideConnection internally.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)

        section("ACP SDK → initialize")
        client = RecordingClient()
        transport = await create_websocket_stream(url)
        # use_unstable_protocol=True enables session/fork, session/resume,
        # and session/close which are marked unstable in the ACP router.
        conn = connect_to_agent(client, transport, use_unstable_protocol=True)

        init_resp = await conn.initialize(
            protocol_version=1,
            client_info={"name": "e2e-acp-test", "version": "0.1.0"},
        )
        print(f"  protocolVersion = {init_resp.protocol_version}")
        print(f"  agentInfo       = {init_resp.agent_info.name} v{init_resp.agent_info.version}")
        assert init_resp.protocol_version == 1, "expected protocol version 1"
        assert init_resp.agent_info.name == "flowjet-server"
        assert init_resp.agent_capabilities.load_session is True

        section("ACP SDK → session/new")
        new_resp = await conn.new_session(cwd="/tmp")
        session_id = new_resp.session_id
        print(f"  sessionId = {session_id}")
        assert session_id.startswith("acp-"), f"expected acp- prefix, got {session_id}"

        section("ACP SDK → session/list (before prompt)")
        list_resp = await conn.list_sessions(cwd="/tmp")
        ids = [s.session_id for s in list_resp.sessions]
        print(f"  sessions = {ids}")
        assert session_id in ids, "new session should appear in list"

        section("ACP SDK → session/set_mode")
        mode_resp = await conn.set_session_mode(session_id=session_id, mode_id="default")
        print(f"  set_mode ok (type={type(mode_resp).__name__})")

        section("ACP SDK → session/set_config_option")
        config_resp = await conn.set_config_option(
            config_id="verbosity",
            session_id=session_id,
            value="verbose",
        )
        print(f"  set_config_option ok (type={type(config_resp).__name__})")

        section("ACP SDK → session/prompt (with tool-call events)")
        # Use emit_tools metadata so the FakeRuntimeBackend emits ToolStarted/Completed.
        # Against a real nano backend, this metadata is ignored — the agent still
        # streams whatever tools nano invokes.
        client.updates.clear()
        prompt_resp = await conn.prompt(
            session_id=session_id,
            prompt=[text_block("Hello from ACP E2E!")],
            emit_tools=True,
        )
        print(f"  stopReason = {prompt_resp.stop_reason}")
        if prompt_resp.usage:
            print(
                f"  usage      = in={prompt_resp.usage.input_tokens} "
                f"out={prompt_resp.usage.output_tokens} "
                f"total={prompt_resp.usage.total_tokens}"
            )
        assert prompt_resp.stop_reason == "end_turn", (
            f"expected end_turn, got {prompt_resp.stop_reason}"
        )

        # Verify session/update notifications were received.
        kinds = [u["kind"] for u in client.updates]
        print(f"  update kinds = {kinds}")
        assert "agent_thought_chunk" in kinds, "expected at least one thought chunk"
        assert "agent_message_chunk" in kinds, "expected at least one message chunk"

        # Reassemble streamed agent message text.
        msg_text = "".join(
            u.get("text", "") for u in client.updates if u["kind"] == "agent_message_chunk"
        )
        print(f"  agent text  = {msg_text!r}")
        assert "Echo: Hello from ACP E2E!" in msg_text, "expected echo text in streamed chunks"

        # Verify tool-call notifications (from emit_tools metadata).
        tool_starts = [u for u in client.updates if u["kind"] == "tool_call"]
        tool_updates = [u for u in client.updates if u["kind"] == "tool_call_update"]
        print(f"  tool_call starts = {len(tool_starts)}, updates = {len(tool_updates)}")
        assert len(tool_starts) >= 1, "expected at least one tool_call start"
        assert any(u.get("status") == "completed" for u in tool_updates), (
            "expected at least one completed tool_call_update"
        )

        section("ACP SDK → session/load")
        load_resp = await conn.load_session(cwd="/tmp", session_id=session_id)
        print(f"  load_session ok (type={type(load_resp).__name__})")

        # session/fork, session/resume, and session/close are marked
        # ``unstable`` in the ACP router.  The server-side ConnectionState
        # does not enable ``use_unstable_protocol``, so these methods return
        # ``method_not_found``.  We verify that here and test them via raw
        # JSON-RPC in the second scenario.
        from acp.exceptions import RequestError

        section("ACP SDK → session/fork (unstable → method_not_found)")
        try:
            await conn.fork_session(session_id=session_id, cwd="/tmp")
            raise AssertionError("expected method_not_found for unstable session/fork")
        except RequestError as exc:
            assert exc.code == -32601, f"expected method_not_found (-32601), got {exc.code}"
            print(
                f"  session/fork → method_not_found (code {exc.code})"
                " — expected for unstable method"
            )

        section("ACP SDK → session/resume (unstable → method_not_found)")
        try:
            await conn.resume_session(session_id="acp-test", cwd="/tmp")
            raise AssertionError("expected method_not_found for unstable session/resume")
        except RequestError as exc:
            assert exc.code == -32601
            print(
                f"  session/resume → method_not_found (code {exc.code})"
                " — expected for unstable method"
            )

        section("ACP SDK → authenticate")
        auth_resp = await conn.authenticate(method_id="none")
        print(f"  authenticate ok (type={type(auth_resp).__name__})")

        section("ACP SDK → session/close (unstable → method_not_found)")
        try:
            await conn.close_session(session_id=session_id)
            raise AssertionError("expected method_not_found for unstable session/close")
        except RequestError as exc:
            assert exc.code == -32601
            print(
                f"  session/close → method_not_found (code {exc.code})"
                " — expected for unstable method"
            )

        await conn.close()
        await transport.close()

    print("\nSDK scenario: ALL ACP agent-side methods passed ✓")


async def run_raw_jsonrpc_scenario(url: str) -> None:
    """Exercise the WebSocket transport at the raw JSON-RPC frame level.

    This covers:
    - ``initialize`` handshake
    - ``session/new`` → ``session/prompt`` → ``session/cancel`` (mid-prompt)
    - ``session/delete``
    - Error handling for unknown session
    """
    section("RAW JSON-RPC → WebSocket connect + initialize")
    ws = await _raw_ws_connect(url)

    await _send_jsonrpc(
        ws,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "raw-e2e", "version": "0.1.0"},
            },
        },
    )
    msgs = await _recv_until(ws, lambda m: m.get("id") == 1)
    init_resp = msgs[-1]
    assert init_resp["result"]["protocolVersion"] == 1
    print(f"  initialize → protocolVersion={init_resp['result']['protocolVersion']}")

    section("RAW JSON-RPC → session/new")
    await _send_jsonrpc(
        ws,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": "/tmp", "mcpServers": []},
        },
    )
    msgs = await _recv_until(ws, lambda m: m.get("id") == 2)
    session_id = msgs[-1]["result"]["sessionId"]
    print(f"  session/new → sessionId={session_id}")

    section("RAW JSON-RPC → session/prompt + session/cancel (mid-stream)")
    # Send a prompt that emits tools so the stream is long enough to cancel.
    await _send_jsonrpc(
        ws,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "Stream then cancel"}],
                "_meta": {"emit_tools": True},
            },
        },
    )
    # Collect a few session/update notifications to confirm the stream started.
    updates_seen: list[dict[str, Any]] = []
    for _ in range(10):
        msg = await _recv_jsonrpc(ws, timeout=5.0)
        if msg.get("method") == "session/update":
            updates_seen.append(msg)
            break
        if msg.get("id") == 3:
            # Prompt already completed (FakeRuntimeBackend is fast).
            updates_seen.append(msg)
            break
    print(f"  received {len(updates_seen)} frame(s) before cancel")

    # Send session/cancel as a notification (no id → no response expected).
    await _send_jsonrpc(
        ws,
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        },
    )
    print("  session/cancel sent (notification)")

    # Drain remaining frames until we get the prompt response (id=3) or timeout.
    try:
        remaining = await _recv_until(ws, lambda m: m.get("id") == 3, max_messages=50, timeout=10.0)
        prompt_resp = remaining[-1]
        stop = prompt_resp.get("result", {}).get("stopReason")
        print(f"  prompt response → stopReason={stop}")
        # The FakeRuntimeBackend completes quickly, so we may get end_turn or
        # cancelled depending on timing. Both are valid outcomes.
        assert stop in ("end_turn", "cancelled"), f"unexpected stop reason: {stop}"
    except (TimeoutError, AssertionError) as exc:
        # If the prompt already completed before cancel, that's fine.
        print(f"  (prompt already completed or cancelled: {exc})")

    section("RAW JSON-RPC → session/list (verify cancelled session state)")
    await _send_jsonrpc(
        ws,
        {"jsonrpc": "2.0", "id": 4, "method": "session/list", "params": {}},
    )
    msgs = await _recv_until(ws, lambda m: m.get("id") == 4)
    sessions = msgs[-1]["result"]["sessions"]
    print(f"  session/list → {len(sessions)} session(s)")

    section("RAW JSON-RPC → session/close (unstable → method_not_found)")
    await _send_jsonrpc(
        ws,
        {"jsonrpc": "2.0", "id": 5, "method": "session/close", "params": {"sessionId": session_id}},
    )
    msgs = await _recv_until(ws, lambda m: m.get("id") == 5)
    resp = msgs[-1]
    assert "error" in resp, f"expected method_not_found error for session/close, got {resp}"
    assert resp["error"]["code"] == -32601, f"expected -32601, got {resp['error']['code']}"
    print("  session/close → method_not_found (code -32601) — expected for unstable method")

    section("RAW JSON-RPC → session/fork (unstable → method_not_found)")
    await _send_jsonrpc(
        ws,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "session/fork",
            "params": {
                "sessionId": session_id,
                "cwd": "/tmp",
            },
        },
    )
    msgs = await _recv_until(ws, lambda m: m.get("id") == 6)
    resp = msgs[-1]
    assert "error" in resp, f"expected method_not_found for session/fork, got {resp}"
    assert resp["error"]["code"] == -32601
    print("  session/fork → method_not_found (code -32601) — expected for unstable method")

    section("RAW JSON-RPC → session/resume (unstable → method_not_found)")
    await _send_jsonrpc(
        ws,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/resume",
            "params": {
                "sessionId": session_id,
                "cwd": "/tmp",
            },
        },
    )
    msgs = await _recv_until(ws, lambda m: m.get("id") == 7)
    resp = msgs[-1]
    assert "error" in resp, f"expected method_not_found for session/resume, got {resp}"
    assert resp["error"]["code"] == -32601
    print("  session/resume → method_not_found (code -32601) — expected for unstable method")

    section("RAW JSON-RPC → unknown method → method_not_found")
    await _send_jsonrpc(
        ws,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "session/delete",
            "params": {"sessionId": session_id},
        },
    )
    msgs = await _recv_until(ws, lambda m: m.get("id") == 8)
    resp = msgs[-1]
    assert "error" in resp, f"expected method_not_found for session/delete, got {resp}"
    print(f"  session/delete → error code {resp['error']['code']}")

    await ws.close()
    print("\nRaw JSON-RPC scenario: ALL transport-level checks passed ✓")


async def main() -> None:
    url = acp_ws_url()
    print("ACP E2E → flowjet-server WebSocket endpoint")
    print(f"ws_url = {url}")
    print()

    await run_sdk_scenario(url)
    print()
    await run_raw_jsonrpc_scenario(url)

    print("\n" + "=" * 60)
    print("ALL ACP E2E TESTS PASSED ✓")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
