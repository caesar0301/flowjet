"""BoxLite worker entrypoint — runs inside a container (or local subprocess).

Reads JSON-lines requests on stdin, executes them via the
:class:`~flowjet.core.backends.isolation.adapter.AgentAdapter`, and emits
JSON-lines events on stdout.

Protocol (one JSON object per line)::

    stdin  →  {"type": "request", ...}
    stdin  →  {"type": "cancel"}
    stdin  →  {"type": "shutdown"}
    stdout ←  {"type": "ready"}
    stdout ←  {"type": "event", "event_type": "RunStarted", ...}
    stdout ←  {"type": "done"}
    stdout ←  {"type": "error", "message": "..."}
    stdout ←  {"type": "cancelled"}
    stdout ←  {"type": "ready"}   (after each request, signals readiness)

Run directly::

    python -m flowjet.core.backends.isolation.boxlite_worker

The worker builds its adapter lazily on the first request (same pattern as
the thread-pool worker).  It reuses the adapter across requests until a
``shutdown`` frame is received or stdin closes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from flowjet.core.backends.isolation.boxlite_protocol import (
    decode_request,
    encode_event,
    make_control,
    parse_frame,
)
from flowjet.core.events import RuntimeEvent

logger = logging.getLogger(__name__)

_WORKER_ID = os.environ.get("BOXLITE_WORKER_ID", "bl-unknown")


def _write_frame(line: str) -> None:
    """Write a JSON-lines frame to stdout (flushed immediately)."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _write_control(frame_type: str, **fields: Any) -> None:
    _write_frame(make_control(frame_type, **fields))


def _write_event(event: RuntimeEvent) -> None:
    _write_frame(encode_event(event))


async def _run_request(req_json: str, adapter: Any) -> None:
    """Execute one request and emit events."""
    frame = parse_frame(req_json)
    req = decode_request(frame)

    try:
        async for event in adapter.astream(req):
            _write_event(event)
        _write_control("done")
    except asyncio.CancelledError:
        _write_control("cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Worker %s failed session=%s", _WORKER_ID, req.session)
        _write_control("error", message=str(exc))


async def _main() -> None:
    """Main worker loop: read stdin, dispatch requests, emit events."""
    # Build the adapter lazily on first use.
    adapter: Any = None

    # Signal readiness immediately.
    _write_control("ready")

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            # stdin closed → shutdown.
            break
        line_str = line.decode("utf-8").strip()
        if not line_str:
            continue

        try:
            frame = parse_frame(line_str)
        except Exception:  # noqa: BLE001
            logger.warning("Worker %s: malformed frame: %s", _WORKER_ID, line_str[:200])
            continue

        msg_type = frame.get("type")
        if msg_type == "shutdown":
            break
        if msg_type == "cancel":
            # In-flight cancellation is handled by the current request task.
            continue
        if msg_type != "request":
            logger.warning("Worker %s: unknown frame type: %s", _WORKER_ID, msg_type)
            continue

        # Lazily build the adapter on first request.
        if adapter is None:
            try:
                adapter = _build_adapter()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Worker %s: adapter build failed", _WORKER_ID)
                _write_control("error", message=f"adapter build failed: {exc}")
                _write_control("ready")
                continue

        # Execute the request.
        await _run_request(line_str, adapter)

        # Signal readiness for the next request.
        _write_control("ready")

    # Clean up adapter on shutdown.
    if adapter is not None:
        try:
            await adapter.cleanup()
        except Exception:  # noqa: BLE001
            pass


def _build_adapter() -> Any:
    """Build the AgentAdapter for this worker.

    Uses the same factory pattern as the thread pool: the adapter is
    constructed via the bridge's factory function.  The specific bridge
    (soothe or nano) is determined by the server backend setting.

    When the ``BOXLITE_ADAPTER`` env var is set to ``fake``, the worker
    uses :class:`FakeAgentAdapter` — useful for testing without an LLM.

    If the real adapter fails to build (e.g. no config file), the worker
    falls back to :class:`FakeAgentAdapter` so the request still completes.
    """
    adapter_mode = os.environ.get("BOXLITE_ADAPTER", "")
    if adapter_mode == "fake":
        from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

        return FakeAgentAdapter()

    from flowjet.core.backend import Backend, resolve_backend
    from flowjet.core.modes import Surface

    backend = resolve_backend(Surface.SERVER)
    try:
        if backend is Backend.SOOTHE:
            from flowjet.core.bridges.soothe.adapter import SootheAgentAdapter

            return SootheAgentAdapter()
        else:
            from flowjet.core.bridges.nano.adapter import NanoAgentAdapter

            return NanoAgentAdapter()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Worker %s: adapter build failed (%s); falling back to FakeAgentAdapter",
            _WORKER_ID,
            exc,
        )
        from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

        return FakeAgentAdapter()


def main() -> None:
    """Entry point for ``python -m flowjet.core.backends.isolation.boxlite_worker``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [boxlite:%(worker_id)s] %(message)s",
    )
    # Attach worker_id to log records.
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        record.worker_id = _WORKER_ID  # type: ignore[attr-defined]
        return record

    logging.setLogRecordFactory(record_factory)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
