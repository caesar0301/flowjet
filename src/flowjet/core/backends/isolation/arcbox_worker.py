"""ArcBox worker entrypoint — runs inside a sandbox microVM.

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

    python -m flowjet.core.backends.isolation.arcbox_worker

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

from flowjet.core.backends.isolation.arcbox_protocol import (
    decode_request,
    encode_event,
    make_control,
    parse_frame,
)
from flowjet.core.events import RuntimeEvent

logger = logging.getLogger(__name__)

_WORKER_ID = os.environ.get("ARCBOX_WORKER_ID", "ab-unknown")
_REAL_STDOUT: Any = sys.stdout  # set by _force_logs_to_stderr; used by _write_frame


def _write_frame(line: str) -> None:
    """Write a JSON-lines frame to the real stdout (flushed immediately).

    After :func:`_force_logs_to_stderr` swaps ``sys.stdout`` → ``sys.stderr``,
    this function writes directly to the original stdout file descriptor so
    protocol frames reach the pool without being polluted by log lines.
    """
    _REAL_STDOUT.write(line + "\n")
    _REAL_STDOUT.flush()


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

    # Force all log handlers to stderr so third-party libraries (LiteLLM,
    # soothe) don't corrupt the JSON-lines stdout protocol channel.
    _force_logs_to_stderr()

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

    When the ``ARCBOX_ADAPTER`` env var is set to ``fake``, the worker
    uses :class:`FakeAgentAdapter` — useful for testing without an LLM.

    If the real adapter fails to build (e.g. no config file), the worker
    falls back to :class:`FakeAgentAdapter` so the request still completes.
    """
    adapter_mode = os.environ.get("ARCBOX_ADAPTER", "")
    if adapter_mode == "fake":
        from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

        return FakeAgentAdapter()

    from flowjet.core.backend import Backend, resolve_backend
    from flowjet.core.modes import Surface

    backend = resolve_backend(Surface.SERVER)
    # Workers run as subprocesses of the server, so they inherit its env —
    # including FLOWJET_NANO_CONFIG. Pass it through: with sync/arcbox deploys
    # the config mounts may live outside FLOWJET_HOME, and load_config(None)
    # would otherwise fall back to an empty zero-config agent (missing keys).
    config_path = os.environ.get("FLOWJET_NANO_CONFIG") or None
    try:
        if backend is Backend.SOOTHE:
            from flowjet.core.bridges.soothe.adapter import SootheAgentAdapter

            return SootheAgentAdapter(config_path=config_path)
        else:
            from flowjet.core.bridges.nano.adapter import NanoAgentAdapter

            return NanoAgentAdapter(config_path=config_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Worker %s: adapter build failed (%s); falling back to FakeAgentAdapter",
            _WORKER_ID,
            exc,
        )
        from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

        return FakeAgentAdapter()


def _force_logs_to_stderr() -> None:
    """Redirect all log handlers and stray ``print()`` calls to stderr.

    The worker communicates with the pool via JSON-lines on **stdout**.
    Third-party libraries (LiteLLM, soothe) sometimes install StreamHandlers
    that write to ``sys.stdout`` or call ``print()`` directly, corrupting the
    protocol channel.  This function:

    1. Replaces all logging handlers with a stderr-only handler.
    2. Swaps ``sys.stdout`` → ``sys.stderr`` so stray ``print()`` calls go
       to stderr instead of the protocol channel.
    3. Keeps a reference to the original stdout (``_REAL_STDOUT``) so
       :func:`_write_frame` can still write protocol frames.
    """
    global _REAL_STDOUT

    import logging

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s [arcbox:%(worker_id)s] %(name)s %(levelname)s %(message)s")
    )

    root = logging.getLogger()
    root.handlers = [stderr_handler]
    root.setLevel(logging.INFO)

    for name, logger in logging.Logger.manager.loggerDict.items():
        if isinstance(logger, logging.Logger) and logger.handlers:
            logger.handlers = [stderr_handler]

    # Swap stdout → stderr so third-party print() calls don't corrupt frames.
    _REAL_STDOUT = sys.stdout
    sys.stdout = sys.stderr


def main() -> None:
    """Entry point for ``python -m flowjet.core.backends.isolation.arcbox_worker``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [arcbox:%(worker_id)s] %(message)s",
        stream=sys.stderr,
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
