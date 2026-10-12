"""Unit tests for the six ACP event-stream enhancements.

Covers:
1. Narration → agent_thought_chunk (live reasoning)
2. OutputTextDelta split into multiple deltas (streaming)
3. Tool args/detail/preview → raw_input/raw_output (rich tool metadata)
4. Tool kind inference (read/edit/execute/search vs always "other")
5. Progress.raw step payloads → AgentPlanUpdate (structured plan)
6. Progress.raw → agent_thought_chunk _meta (richer rendering)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from flowjet.core.events import (
    ModelInfo,
    Narration,
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunRequest,
    RunStarted,
    RuntimeEvent,
    ToolCompleted,
    ToolStarted,
    UsageInfo,
)
from flowjet.server.acp.agent import (
    FlowJetAcpAgent,
    _infer_tool_kind,
    _raw_to_plan_entries,
    _tool_locations,
    _tool_raw_input,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _TextBlock:
    """Stand-in for an ACP text content block."""

    text: str


class _RecordingClient:
    """Minimal ACP client that records every session_update call."""

    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append(update)


class _ScriptedBackend:
    """Backend that replays a scripted list of RuntimeEvents."""

    def __init__(self, events: list[RuntimeEvent]) -> None:
        self._events = events

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="default")]

    async def delete_run(self, run_id: str) -> None:
        return None

    async def stream_run(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        for event in self._events:
            yield event


async def _run_prompt(
    events: list[RuntimeEvent],
) -> tuple[FlowJetAcpAgent, _RecordingClient, Any]:
    """Drive one ACP prompt with the given backend events.

    Returns (agent, client, prompt_response).
    """
    backend = _ScriptedBackend(events)
    client = _RecordingClient()
    agent = FlowJetAcpAgent(backend=backend, client=client)
    # Register a session so prompt() has a session_id.
    await agent.new_session(cwd="/tmp", **{})
    # Access the session id from the internal set.
    session_id = next(iter(agent._sessions))
    response = await agent.prompt(session_id, [_TextBlock("hello")])
    return agent, client, response


# ---------------------------------------------------------------------------
# Enhancement 1: Narration → agent_thought_chunk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_emitted_as_agent_thought_chunk():
    """Live narration text between tool calls reaches ACP clients."""
    events = [
        RunStarted(run_id="r1", model="default"),
        Narration(text="Let me look that up."),
        ToolStarted(tool="search", call_id="c1"),
        ToolCompleted(tool="search", ok=True, call_id="c1"),
        Narration(text="Found it."),
        OutputTextDelta(delta="Here is the answer."),
        RunCompleted(output_text="Here is the answer."),
    ]
    _, client, _ = await _run_prompt(events)

    thought_chunks = [
        u for u in client.updates if getattr(u, "session_update", None) == "agent_thought_chunk"
    ]
    thought_texts = [u.content.text for u in thought_chunks]
    assert "Let me look that up." in thought_texts
    assert "Found it." in thought_texts


@pytest.mark.asyncio
async def test_narration_empty_text_is_not_emitted():
    """Empty narration text should not produce a thought chunk."""
    events = [
        RunStarted(run_id="r1", model="default"),
        Narration(text=""),
        RunCompleted(output_text=""),
    ]
    _, client, _ = await _run_prompt(events)

    thought_chunks = [
        u for u in client.updates if getattr(u, "session_update", None) == "agent_thought_chunk"
    ]
    # RunStarted produces one thought chunk; the empty Narration should not.
    assert len(thought_chunks) == 1


# ---------------------------------------------------------------------------
# Enhancement 2: OutputTextDelta split into multiple deltas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_text_delta_streamed_as_multiple_chunks():
    """Multiple OutputTextDelta events produce multiple agent_message_chunk."""
    events = [
        RunStarted(run_id="r1", model="default"),
        OutputTextDelta(delta="Hello "),
        OutputTextDelta(delta="world!"),
        RunCompleted(output_text="Hello world!"),
    ]
    _, client, _ = await _run_prompt(events)

    message_chunks = [
        u for u in client.updates if getattr(u, "session_update", None) == "agent_message_chunk"
    ]
    assert len(message_chunks) == 2
    text = "".join(u.content.text for u in message_chunks)
    assert text == "Hello world!"


# ---------------------------------------------------------------------------
# Enhancement 3: Tool args/detail/preview → raw_input/raw_output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_started_carries_raw_input_and_locations():
    """ToolStarted args are passed as raw_input and file paths as locations."""
    events = [
        RunStarted(run_id="r1", model="default"),
        ToolStarted(
            tool="write_file",
            call_id="c1",
            args={"file_path": "/tmp/a.txt", "content": "hello"},
        ),
        ToolCompleted(tool="write_file", ok=True, call_id="c1"),
        RunCompleted(output_text="done"),
    ]
    _, client, _ = await _run_prompt(events)

    tool_starts = [u for u in client.updates if getattr(u, "session_update", None) == "tool_call"]
    assert len(tool_starts) == 1
    start = tool_starts[0]
    assert start.raw_input == {"file_path": "/tmp/a.txt", "content": "hello"}
    assert start.locations is not None
    assert start.locations[0].path == "/tmp/a.txt"


@pytest.mark.asyncio
async def test_tool_completed_carries_raw_output():
    """ToolCompleted detail/preview are passed as raw_output."""
    events = [
        RunStarted(run_id="r1", model="default"),
        ToolStarted(tool="search", call_id="c1"),
        ToolCompleted(
            tool="search",
            ok=True,
            call_id="c1",
            detail="found 3 results",
            preview="result1, result2, result3",
        ),
        RunCompleted(output_text="done"),
    ]
    _, client, _ = await _run_prompt(events)

    tool_updates = [
        u for u in client.updates if getattr(u, "session_update", None) == "tool_call_update"
    ]
    assert len(tool_updates) == 1
    update = tool_updates[0]
    assert update.raw_output is not None
    assert update.raw_output["detail"] == "found 3 results"
    assert update.raw_output["preview"] == "result1, result2, result3"


@pytest.mark.asyncio
async def test_tool_completed_failed_has_raw_output():
    """Failed tool calls carry raw_output with detail."""
    events = [
        RunStarted(run_id="r1", model="default"),
        ToolStarted(tool="run_command", call_id="c1"),
        ToolCompleted(
            tool="run_command",
            ok=False,
            call_id="c1",
            detail="exit code 1",
            preview=None,
        ),
        RunCompleted(output_text="done"),
    ]
    _, client, _ = await _run_prompt(events)

    tool_updates = [
        u for u in client.updates if getattr(u, "session_update", None) == "tool_call_update"
    ]
    assert len(tool_updates) == 1
    assert tool_updates[0].status == "failed"
    assert tool_updates[0].raw_output is not None
    assert tool_updates[0].raw_output["detail"] == "exit code 1"


# ---------------------------------------------------------------------------
# Enhancement 4: Tool kind inference
# ---------------------------------------------------------------------------


def test_infer_tool_kind_read():
    assert _infer_tool_kind("read_file") == "read"
    assert _infer_tool_kind("grep") == "read"
    assert _infer_tool_kind("ls") == "read"


def test_infer_tool_kind_edit():
    assert _infer_tool_kind("write_file") == "edit"
    assert _infer_tool_kind("edit_file") == "edit"
    assert _infer_tool_kind("delete_file") == "edit"


def test_infer_tool_kind_execute():
    assert _infer_tool_kind("run_command") == "execute"
    assert _infer_tool_kind("bash") == "execute"


def test_infer_tool_kind_search():
    assert _infer_tool_kind("web_search") == "fetch"
    assert _infer_tool_kind("fetch") == "fetch"


def test_infer_tool_kind_other():
    assert _infer_tool_kind("search") == "read"  # search is in _READ_TOOLS
    assert _infer_tool_kind("unknown_tool") == "other"
    assert _infer_tool_kind("custom_mcp_tool") == "other"


@pytest.mark.asyncio
async def test_tool_kind_inferred_in_acp_notification():
    """The ACP tool_call notification carries the inferred kind."""
    events = [
        RunStarted(run_id="r1", model="default"),
        ToolStarted(tool="read_file", call_id="c1"),
        ToolCompleted(tool="read_file", ok=True, call_id="c1"),
        RunCompleted(output_text="done"),
    ]
    _, client, _ = await _run_prompt(events)

    tool_starts = [u for u in client.updates if getattr(u, "session_update", None) == "tool_call"]
    assert tool_starts[0].kind == "read"


# ---------------------------------------------------------------------------
# Enhancement 5: Progress.raw step payloads → AgentPlanUpdate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_progress_emits_plan_update():
    """Progress events with soothe.step.progress raw emit AgentPlanUpdate."""
    raw = {
        "type": "soothe.step.progress",
        "step": 2,
        "steps": 3,
        "done": 1,
        "status": "in_progress",
        "todo": "Write report",
    }
    events = [
        RunStarted(run_id="r1", model="default"),
        Progress(stage="Step", message="Step 2/3 · Write report", raw=raw),
        RunCompleted(output_text="done"),
    ]
    _, client, _ = await _run_prompt(events)

    plan_updates = [u for u in client.updates if getattr(u, "session_update", None) == "plan"]
    assert len(plan_updates) == 1
    entries = plan_updates[0].entries
    assert len(entries) == 3
    # Step 1 is completed (done=1), step 2 is in_progress, step 3 is pending.
    assert entries[0].status == "completed"
    assert entries[1].status == "in_progress"
    assert entries[1].content == "Write report"
    assert entries[2].status == "pending"


@pytest.mark.asyncio
async def test_non_step_progress_does_not_emit_plan_update():
    """Progress events without step.progress raw do not emit plan updates."""
    events = [
        RunStarted(run_id="r1", model="default"),
        Progress(stage="Working", message="Composing response…", raw=None),
        RunCompleted(output_text="done"),
    ]
    _, client, _ = await _run_prompt(events)

    plan_updates = [u for u in client.updates if getattr(u, "session_update", None) == "plan"]
    assert len(plan_updates) == 0


def test_raw_to_plan_entries_returns_none_for_non_step():
    assert _raw_to_plan_entries(None) is None
    assert _raw_to_plan_entries({}) is None
    assert _raw_to_plan_entries({"type": "soothe.skill.activated"}) is None


def test_raw_to_plan_entries_builds_correct_entries():
    raw = {
        "type": "soothe.step.progress",
        "step": 1,
        "steps": 2,
        "done": 0,
        "status": "in_progress",
        "todo": "First task",
    }
    entries = _raw_to_plan_entries(raw)
    assert entries is not None
    assert len(entries) == 2
    assert entries[0].status == "in_progress"
    assert entries[0].content == "First task"
    assert entries[1].status == "pending"


# ---------------------------------------------------------------------------
# Enhancement 6: Progress.raw → agent_thought_chunk _meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_raw_passed_to_thought_meta():
    """Progress events with raw payload carry it as _meta on the thought chunk."""
    raw = {
        "type": "soothe.step.progress",
        "step": 1,
        "steps": 2,
        "done": 0,
        "status": "in_progress",
        "todo": "First task",
    }
    events = [
        RunStarted(run_id="r1", model="default"),
        Progress(stage="Step", message="Step 1/2 · First task", raw=raw),
        RunCompleted(output_text="done"),
    ]
    _, client, _ = await _run_prompt(events)

    thought_chunks = [
        u for u in client.updates if getattr(u, "session_update", None) == "agent_thought_chunk"
    ]
    # RunStarted produces one, Progress produces one.
    assert len(thought_chunks) >= 2
    # The Progress-derived thought chunk should carry the raw payload.
    progress_thought = thought_chunks[-1]
    assert progress_thought.field_meta is not None
    assert progress_thought.field_meta["type"] == "soothe.step.progress"
    assert progress_thought.field_meta["todo"] == "First task"


@pytest.mark.asyncio
async def test_progress_without_raw_has_null_meta():
    """Progress events without raw payload have _meta=None."""
    events = [
        RunStarted(run_id="r1", model="default"),
        Progress(stage="Working", message="Composing…", raw=None),
        RunCompleted(output_text="done"),
    ]
    _, client, _ = await _run_prompt(events)

    thought_chunks = [
        u for u in client.updates if getattr(u, "session_update", None) == "agent_thought_chunk"
    ]
    progress_thought = thought_chunks[-1]
    assert progress_thought.field_meta is None


# ---------------------------------------------------------------------------
# Tool location / raw_input helpers
# ---------------------------------------------------------------------------


def test_tool_locations_extracts_file_path():
    locs = _tool_locations({"file_path": "/tmp/a.txt"})
    assert locs is not None
    assert locs[0].path == "/tmp/a.txt"


def test_tool_locations_extracts_multiple_paths():
    locs = _tool_locations({"file_path": "/tmp/a.txt", "path": "/tmp/b.txt"})
    assert locs is not None
    assert len(locs) == 2


def test_tool_locations_returns_none_for_no_paths():
    assert _tool_locations({"query": "hello"}) is None
    assert _tool_locations(None) is None


def test_tool_raw_input_returns_args():
    args = {"file_path": "/tmp/a.txt", "content": "hello"}
    assert _tool_raw_input(args) == args


def test_tool_raw_input_returns_none_for_empty():
    assert _tool_raw_input(None) is None
    assert _tool_raw_input({}) is None  # empty dict is falsy


# ---------------------------------------------------------------------------
# Integration: full event stream with all enhancements
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_event_stream_exercises_all_enhancements():
    """A realistic event stream exercises all six enhancements at once."""
    step_raw = {
        "type": "soothe.step.progress",
        "step": 1,
        "steps": 2,
        "done": 0,
        "status": "in_progress",
        "todo": "Search for files",
    }
    events = [
        RunStarted(run_id="r1", model="default"),
        Progress(stage="Step", message="Step 1/2 · Search for files", raw=step_raw),
        Narration(text="Let me look that up."),  # Enhancement 1
        ToolStarted(  # Enhancement 3 + 4
            tool="grep",
            call_id="c1",
            args={"pattern": "hello", "path": "/tmp"},
        ),
        ToolCompleted(
            tool="grep",
            ok=True,
            call_id="c1",
            detail="found 2 matches",
            preview="file1.txt: hello",
        ),
        OutputTextDelta(delta="Here "),  # Enhancement 2
        OutputTextDelta(delta="is the "),
        OutputTextDelta(delta="answer."),
        RunCompleted(
            output_text="Here is the answer.",
            usage=UsageInfo(input_tokens=5, output_tokens=5, total_tokens=10),
        ),
    ]
    _, client, response = await _run_prompt(events)

    # Enhancement 1: Narration reached the client as a thought chunk.
    thought_texts = [
        u.content.text
        for u in client.updates
        if getattr(u, "session_update", None) == "agent_thought_chunk"
    ]
    assert "Let me look that up." in thought_texts

    # Enhancement 2: Multiple message chunks.
    message_chunks = [
        u for u in client.updates if getattr(u, "session_update", None) == "agent_message_chunk"
    ]
    assert len(message_chunks) == 3

    # Enhancement 3: Tool raw_input and raw_output.
    tool_starts = [u for u in client.updates if getattr(u, "session_update", None) == "tool_call"]
    assert tool_starts[0].raw_input == {"pattern": "hello", "path": "/tmp"}
    assert tool_starts[0].locations[0].path == "/tmp"

    tool_updates = [
        u for u in client.updates if getattr(u, "session_update", None) == "tool_call_update"
    ]
    assert tool_updates[0].raw_output["detail"] == "found 2 matches"

    # Enhancement 4: Tool kind inferred.
    assert tool_starts[0].kind == "read"  # grep is a read tool

    # Enhancement 5: Plan update emitted.
    plan_updates = [u for u in client.updates if getattr(u, "session_update", None) == "plan"]
    assert len(plan_updates) == 1
    assert plan_updates[0].entries[0].content == "Search for files"

    # Enhancement 6: Thought chunk carries field_meta.
    progress_thought = [
        u
        for u in client.updates
        if getattr(u, "session_update", None) == "agent_thought_chunk" and u.field_meta is not None
    ]
    assert len(progress_thought) >= 1
    assert progress_thought[0].field_meta["todo"] == "Search for files"

    # Response carries correct output text and usage.
    assert response.stopReason == "end_turn"
    assert response.usage.totalTokens == 10
