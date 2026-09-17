"""ProjectionEngine mode behavior."""

from __future__ import annotations

from flowjet.core.events import (
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunStarted,
    ToolCompleted,
    ToolStarted,
)
from flowjet.server.openai_compat.projection import ProjectionEngine


def _drain(mode: str) -> list[dict]:
    eng = ProjectionEngine(mode, "resp_1", "default")  # type: ignore[arg-type]
    out: list[dict] = []
    for event in (
        RunStarted(run_id="resp_1", model="default"),
        Progress(stage="Working", message="Thinking…"),
        ToolStarted(tool="search", call_id="c1"),
        ToolCompleted(tool="search", ok=True, call_id="c1", duration_ms=3),
        OutputTextDelta(delta="Hello"),
        RunCompleted(output_text="Hello"),
    ):
        out.extend(eng.handle(event))
    return out


def test_report_hides_progress_and_tools() -> None:
    types = [e["type"] for e in _drain("report")]
    assert "response.flowjet.progress" not in types
    assert "response.flowjet.tool.started" not in types
    assert "response.output_text.delta" in types
    assert "response.completed" in types


def test_progress_emits_flowjet_progress() -> None:
    types = [e["type"] for e in _drain("progress")]
    assert "response.flowjet.progress" in types
    assert "response.flowjet.tool.started" not in types


def test_developer_emits_tools() -> None:
    events = _drain("developer")
    types = [e["type"] for e in events]
    assert "response.flowjet.tool.started" in types
    assert "response.flowjet.tool.completed" in types
    for e in events:
        assert "arguments" not in e
        assert "prompt" not in e
