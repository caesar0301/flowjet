#!/usr/bin/env python3
"""Test arcbox isolation with the REAL soothe adapter (not fake).

Sends a real LLM query through the ACP WebSocket endpoint and verifies:
1. The arcbox worker spawns and processes the request
2. Real LLM response is received (not an echo)
3. Pool metrics show the worker was used
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _client import section  # noqa: E402


class RecordingClient:
    """Minimal ACP client that records session/update notifications."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        kind = getattr(update, "session_update", None) or update.__class__.__name__
        entry: dict[str, Any] = {"session_id": session_id, "kind": kind}
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

    async def request_permission(self, *a: Any, **kw: Any) -> None: ...
    async def write_text_file(self, *a: Any, **kw: Any) -> None: ...
    async def read_text_file(self, *a: Any, **kw: Any) -> None: ...
    async def create_terminal(self, *a: Any, **kw: Any) -> None: ...
    async def terminal_output(self, *a: Any, **kw: Any) -> None: ...
    async def release_terminal(self, *a: Any, **kw: Any) -> None: ...
    async def wait_for_terminal_exit(self, *a: Any, **kw: Any) -> None: ...
    async def kill_terminal(self, *a: Any, **kw: Any) -> None: ...
    async def create_elicitation(self, *a: Any, **kw: Any) -> None: ...
    async def complete_elicitation(self, *a: Any, **kw: Any) -> None: ...
    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]: return {}
    async def ext_notification(self, method: str, params: dict[str, Any]) -> None: pass
    def on_connect(self, conn: Any) -> None: pass


async def main() -> int:
    import warnings
    from acp import connect_to_agent, text_block
    from acp.ws.client import create_websocket_stream

    base_url = os.environ.get("FLOWJET_BASE_URL", "http://127.0.0.1:8618/v1")
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    if ws_url.endswith("/v1"):
        ws_url = ws_url[: -len("/v1")]
    ws_url = f"{ws_url}/acp"

    section("Connecting to ACP WebSocket endpoint")
    print(f"  URL: {ws_url}")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        client = RecordingClient()
        transport = await create_websocket_stream(ws_url)
        conn = connect_to_agent(client, transport, use_unstable_protocol=True)

    # 1. Initialize
    section("ACP initialize")
    init_resp = await conn.initialize(
        protocol_version=1,
        client_info={"name": "arcbox-real-test", "version": "1.0"},
    )
    print(f"  protocolVersion: {init_resp.protocol_version}")
    print(f"  agentInfo: {init_resp.agent_info.name} v{init_resp.agent_info.version}")

    # 2. New session
    section("session/new")
    new_resp = await conn.new_session(cwd="/tmp")
    session_id = new_resp.session_id
    print(f"  sessionId: {session_id}")

    # 3. Prompt with a real query
    section("session/prompt (real LLM query)")
    query = "What is 2+2? Reply with just the number, nothing else."
    print(f"  Sending: {query!r}")

    client.updates.clear()
    prompt_resp = await conn.prompt(
        session_id=session_id,
        prompt=[text_block(query)],
        emit_tools=True,
    )
    print(f"  stopReason: {prompt_resp.stop_reason}")
    if prompt_resp.usage:
        print(f"  usage: in={prompt_resp.usage.input_tokens} out={prompt_resp.usage.output_tokens}")

    # Reassemble streamed agent message text
    msg_text = "".join(
        u.get("text", "") for u in client.updates if u["kind"] == "agent_message_chunk"
    )
    print(f"  Agent response: {msg_text[:300]!r}")

    # Check update kinds
    kinds = [u["kind"] for u in client.updates]
    print(f"  update kinds: {kinds}")

    is_real = "Echo:" not in msg_text and len(msg_text) > 0
    if "Echo:" in msg_text:
        print("  WARNING: Response looks like FakeAgentAdapter echo, not real LLM!")
    elif len(msg_text) == 0:
        print("  WARNING: No text response received!")
    else:
        print("  OK: Real LLM response received (not an echo)")

    # 4. Check health endpoint for pool metrics
    section("Health endpoint — pool metrics")
    with urllib.request.urlopen("http://127.0.0.1:8618/health") as resp:
        health = json.loads(resp.read())
    pool = health.get("pool", {})
    print(f"  total_workers: {pool.get('total_workers')}")
    print(f"  idle_workers: {pool.get('idle_workers')}")
    print(f"  busy_workers: {pool.get('busy_workers')}")
    print(f"  dead_workers: {pool.get('dead_workers')}")
    print(f"  requests_completed: {pool.get('requests_completed')}")

    pool_ok = (
        pool.get("requests_completed", 0) > 0
        and pool.get("dead_workers", 1) == 0
        and pool.get("total_workers", 0) > 0
    )
    if pool_ok:
        print("  OK: ArcBox pool processed request successfully")
    else:
        print("  WARNING: Pool metrics look wrong!")

    # 5. Cleanup
    section("Cleanup")
    await transport.close()
    print("  WebSocket closed")

    # Summary
    section("SUMMARY")
    success = is_real and pool_ok
    print(f"  Real LLM response:     {'YES' if is_real else 'NO'}")
    print(f"  ArcBox worker spawned: {pool.get('total_workers', 0) > 0}")
    print(f"  Requests completed:    {pool.get('requests_completed', 0)}")
    print(f"  Dead workers:          {pool.get('dead_workers', 0)}")
    print(f"  Overall:               {'PASS' if success else 'FAIL'}")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
