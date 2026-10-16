"""JSON-lines protocol for arcbox sandbox workers.

Serializes :class:`IsolatedRunRequest` to JSON for transmission over the
worker's stdin, and deserializes JSON event frames from the worker's stdout
back into :data:`RuntimeEvent` objects.

Wire format (one JSON object per line)::

    {"type": "request", ...}        ← sent to worker stdin
    {"type": "event", "event_type": "RunStarted", ...}
    {"type": "done"}
    {"type": "error", "message": "..."}
    {"type": "cancelled"}
    {"type": "ready"}              ← worker is ready for next request

No external dependencies — only stdlib ``json`` and the flowjet event
dataclasses.  This module is imported both by the pool (host side) and by
the worker script (sandbox side).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import (
    InterruptWaiting,
    Narration,
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunFailed,
    RunStarted,
    RuntimeEvent,
    ToolCompleted,
    ToolStarted,
    UsageInfo,
)

# Tag used in JSON frames to identify the event class.
_EVENT_TYPE_TAG = "event_type"

# Mapping from event type tag → constructor (for deserialization).
_EVENT_REGISTRY: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        RunStarted,
        Progress,
        ToolStarted,
        ToolCompleted,
        Narration,
        OutputTextDelta,
        InterruptWaiting,
        RunCompleted,
        RunFailed,
    )
}


# ---------------------------------------------------------------------------
# Request serialization (host → worker)
# ---------------------------------------------------------------------------


def encode_request(req: IsolatedRunRequest) -> str:
    """Serialize an :class:`IsolatedRunRequest` to a JSON-lines string."""
    payload: dict[str, Any] = {
        "type": "request",
        "request_id": req.request_id,
        "run_id": req.run_id,
        "session": req.session,
        "input_text": req.input_text,
        "model": req.model,
        "workspace": str(req.workspace),
        "thread_id": req.thread_id,
        "metadata": req.metadata,
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Event serialization (worker → host)
# ---------------------------------------------------------------------------


def encode_event(event: RuntimeEvent) -> str:
    """Serialize a :data:`RuntimeEvent` to a JSON-lines string."""
    from dataclasses import asdict

    data = asdict(event)  # type: ignore[arg-type]
    data["type"] = "event"
    data[_EVENT_TYPE_TAG] = type(event).__name__
    return json.dumps(data, ensure_ascii=False)


def decode_event(frame: dict[str, Any]) -> RuntimeEvent:
    """Deserialize a JSON frame dict into a :data:`RuntimeEvent`.

    Raises :class:`ValueError` for unknown event types.
    """
    event_type = frame.get(_EVENT_TYPE_TAG)
    if event_type is None:
        raise ValueError(f"frame missing {_EVENT_TYPE_TAG!r}: {frame!r}")
    cls = _EVENT_REGISTRY.get(event_type)
    if cls is None:
        raise ValueError(f"unknown event type: {event_type!r}")
    # Strip protocol-level keys before constructing the dataclass.
    kwargs = {k: v for k, v in frame.items() if k not in ("type", _EVENT_TYPE_TAG)}
    # Reconstruct nested dataclass fields (e.g. RunCompleted.usage → UsageInfo).
    kwargs = _reconstruct_nested(kwargs)
    return cls(**kwargs)  # type: ignore[call-arg]


def _reconstruct_nested(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Convert dict values back into nested dataclass instances where needed.

    :func:`dataclasses.asdict` recursively converts nested dataclasses to
    plain dicts.  This reverses that for known fields whose constructors
    expect a dataclass, not a dict.
    """
    if "usage" in kwargs and isinstance(kwargs["usage"], dict):
        kwargs["usage"] = UsageInfo(**kwargs["usage"])
    return kwargs


# ---------------------------------------------------------------------------
# Control frame helpers
# ---------------------------------------------------------------------------


def make_control(frame_type: str, **fields: Any) -> str:
    """Build a JSON-lines control frame (``done``, ``error``, etc.)."""
    payload: dict[str, Any] = {"type": frame_type}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def parse_frame(line: str) -> dict[str, Any]:
    """Parse one JSON-lines frame from a worker's stdout."""
    return json.loads(line)


# ---------------------------------------------------------------------------
# Request deserialization (worker side)
# ---------------------------------------------------------------------------


def decode_request(frame: dict[str, Any]) -> IsolatedRunRequest:
    """Deserialize a JSON frame dict into an :class:`IsolatedRunRequest`."""
    return IsolatedRunRequest(
        run_id=frame["run_id"],
        session=frame["session"],
        input_text=frame["input_text"],
        model=frame["model"],
        workspace=Path(frame["workspace"]),
        thread_id=frame.get("thread_id"),
        metadata=frame.get("metadata"),
        request_id=frame.get("request_id"),
    )
