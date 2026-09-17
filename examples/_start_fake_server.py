#!/usr/bin/env python3
"""Start a flowjet-server instance with FakeRuntimeBackend for E2E testing.

Usage:
    python examples/_start_fake_server.py [port]

This is a dev-only helper used by the ACP E2E example script when no real
nano backend is available. It spins up uvicorn on a free (or specified) port
with a deterministic FakeRuntimeBackend (tool-calling enabled) so the ACP
WebSocket endpoint can be exercised end-to-end without LLM credentials.
"""

from __future__ import annotations

import socket
import sys

import uvicorn

from flowjet.core.backends.fake import FakeRuntimeBackend
from flowjet.core.events import RunRequest
from flowjet.server.config import Settings
from flowjet.server.http.app import create_app


class _ToolEmittingBackend(FakeRuntimeBackend):
    """FakeRuntimeBackend that always emits tool-call events."""

    async def stream_run(self, request: RunRequest):  # type: ignore[override]
        meta = dict(request.metadata or {})
        meta["emit_tools"] = True
        # Reconstruct with emit_tools enabled.
        from dataclasses import replace

        request = replace(request, metadata=meta)
        async for event in super().stream_run(request):
            yield event


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else _free_port()
    settings = Settings(host="127.0.0.1", port=port, models="default")
    app = create_app(
        settings=settings,
        backend=_ToolEmittingBackend(models=["default"]),
    )
    print(f"Starting flowjet-server (FakeRuntimeBackend+tools) on http://127.0.0.1:{port}")
    print(f"ACP WebSocket endpoint: ws://127.0.0.1:{port}/acp")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
