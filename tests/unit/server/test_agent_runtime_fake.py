"""FakeRuntimeBackend event stream."""

from __future__ import annotations

from flowjet.core.backends.fake import FakeRuntimeBackend
from flowjet.core.events import (
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunRequest,
    RunStarted,
    ToolCompleted,
    ToolStarted,
)


async def test_fake_stream_order() -> None:
    backend = FakeRuntimeBackend(models=["default"])
    events = [
        e
        async for e in backend.stream_run(
            RunRequest(model="default", input_text="hi", metadata={"emit_tools": True})
        )
    ]
    types = [type(e) for e in events]
    assert types[0] is RunStarted
    assert Progress in types
    assert ToolStarted in types
    assert ToolCompleted in types
    assert OutputTextDelta in types
    assert types[-1] is RunCompleted
    assert isinstance(events[-1], RunCompleted)
    assert events[-1].output_text.startswith("Echo: hi")
    # No tool args on events
    for e in events:
        assert not hasattr(e, "arguments")
        assert not hasattr(e, "prompt")
