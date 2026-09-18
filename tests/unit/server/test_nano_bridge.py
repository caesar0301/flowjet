"""Nano bridge translation of soothe-nano streams into Agent Runtime events."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from flowjet.core.events import (
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunFailed,
    RunRequest,
    RunStarted,
    ToolCompleted,
    ToolStarted,
)

pytest.importorskip("langchain_core", reason="nano extra not installed")

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage  # noqa: E402

from flowjet.core.backends.isolation.request import IsolatedRunRequest  # noqa: E402
from flowjet.core.bridges.nano.adapter import (  # noqa: E402
    NanoAgentAdapter,
    create_nano_agent_instance,
)
from flowjet.core.bridges.nano.backend import NanoRuntimeBackend  # noqa: E402


class StubAgent:
    """Replays a canned soothe-nano ``astream`` transcript."""

    def __init__(self, chunks: list[tuple[str, str, Any]]) -> None:
        self._chunks = chunks
        self.last_config: dict[str, Any] | None = None

    async def astream(self, _input, **kwargs):
        self.last_config = kwargs.get("config")
        for chunk in self._chunks:
            yield chunk


def message_chunk(message: Any) -> tuple[str, str, Any]:
    return ("ns", "messages", (message, {}))


async def collect(chunks: list[tuple[str, str, Any]]) -> list[Any]:
    backend = NanoRuntimeBackend(agent=StubAgent(chunks))
    request = RunRequest(model="default", input_text="hi")
    return [event async for event in backend.stream_run(request)]


@pytest.mark.asyncio
async def test_token_chunks_produce_one_progress_milestone():
    events = await collect(
        [
            message_chunk(AIMessage(content="Hello")),
            message_chunk(AIMessage(content=" streaming")),
            message_chunk(AIMessage(content=" world")),
        ]
    )

    progress = [e for e in events if isinstance(e, Progress)]
    assert len(progress) == 1, "each token chunk must not become its own progress event"

    completed = [e for e in events if isinstance(e, RunCompleted)]
    assert [e.output_text for e in completed] == ["Hello streaming world"]
    assert [e.delta for e in events if isinstance(e, OutputTextDelta)] == ["Hello streaming world"]


@pytest.mark.asyncio
async def test_progress_never_carries_answer_text():
    events = await collect([message_chunk(AIMessage(content="The secret is 42."))])

    for event in events:
        if isinstance(event, Progress):
            assert "secret" not in event.message
            assert "42" not in event.message


@pytest.mark.asyncio
async def test_pre_tool_narration_is_dropped_and_tools_are_summarised():
    tool_call = {"name": "search", "args": {"q": "x"}, "id": "call_1", "type": "tool_call"}
    events = await collect(
        [
            message_chunk(AIMessage(content="Let me look that up.")),
            message_chunk(AIMessage(content="", tool_calls=[tool_call])),
            message_chunk(ToolMessage(content="result", tool_call_id="call_1")),
            message_chunk(AIMessage(content="Final answer.")),
        ]
    )

    assert [e.tool for e in events if isinstance(e, ToolStarted)] == ["search"]
    assert [(e.tool, e.ok) for e in events if isinstance(e, ToolCompleted)] == [("search", True)]

    completed = next(e for e in events if isinstance(e, RunCompleted))
    assert completed.output_text == "Final answer."
    assert "Let me look that up." not in completed.output_text

    # The narration is superseded by the tool call, so it must not reach the client.
    assert all("look that up" not in e.delta for e in events if isinstance(e, OutputTextDelta))


def update_chunk(payload: dict[str, Any]) -> tuple[str, str, Any]:
    return ("ns", "updates", payload)


@pytest.mark.asyncio
async def test_todo_update_becomes_step_progress():
    """The agent's todo list is the plan the user is waiting on."""
    todos = [
        {"content": "Create a1.txt", "status": "completed"},
        {"content": "Create a2.txt", "status": "in_progress"},
        {"content": "Create index.md", "status": "pending"},
    ]
    events = await collect([update_chunk({"tools": {"todos": todos, "messages": []}})])

    steps = [e for e in events if isinstance(e, Progress)]
    assert len(steps) == 1
    assert steps[0].message == "Step 2/3 · Create a2.txt"
    assert steps[0].raw is not None
    assert steps[0].raw["type"] == "soothe.step.progress"
    # raw keeps the payload so the CLI can apply its own label/colour mapping.
    assert steps[0].raw["todo"] == "Create a2.txt"


@pytest.mark.asyncio
async def test_repeated_identical_todo_updates_emit_once():
    todos = [{"content": "Write report", "status": "in_progress"}]
    events = await collect(
        [
            update_chunk({"tools": {"todos": todos}}),
            update_chunk({"tools": {"todos": todos}}),
        ]
    )

    assert len([e for e in events if isinstance(e, Progress)]) == 1


@pytest.mark.asyncio
async def test_step_progress_advances_when_the_todo_changes():
    first = [
        {"content": "Write report", "status": "in_progress"},
        {"content": "Review it", "status": "pending"},
    ]
    second = [
        {"content": "Write report", "status": "completed"},
        {"content": "Review it", "status": "in_progress"},
    ]
    events = await collect(
        [
            update_chunk({"tools": {"todos": first}}),
            update_chunk({"tools": {"todos": second}}),
        ]
    )

    messages = [e.message for e in events if isinstance(e, Progress)]
    assert messages == ["Step 1/2 · Write report", "Step 2/2 · Review it"]


@pytest.mark.asyncio
async def test_host_middleware_updates_become_phase_progress():
    events = await collect(
        [
            update_chunk({"DecomposeTaskMiddleware.before_agent": None}),
            update_chunk({"EvalStepMiddleware.after_model": None}),
            update_chunk({"AskUserPromptMiddleware.before_model": {"question": "which file?"}}),
        ]
    )

    types = [e.raw["type"] for e in events if isinstance(e, Progress) and e.raw]
    assert types == [
        "soothe.step.decomposed",
        "soothe.step.evaluated",
        "soothe.ask.requested",
    ]


@pytest.mark.asyncio
async def test_plumbing_updates_are_not_emitted():
    """Per-turn plumbing would otherwise drown the step and tool lines."""
    events = await collect(
        [
            update_chunk({"PatchToolCallsMiddleware.before_agent": None}),
            update_chunk({"ProgressiveToolMiddleware.after_model": None}),
            update_chunk({"TodoListMiddleware.after_model": None}),
            update_chunk({"model": {"messages": []}}),
        ]
    )

    assert [e for e in events if isinstance(e, Progress)] == []


@pytest.mark.asyncio
async def test_skill_activation_update_becomes_skill_progress():
    events = await collect(
        [update_chunk({"SkillActivationMiddleware.before_agent": {"skill_activation": ["docs"]}})]
    )

    steps = [e for e in events if isinstance(e, Progress)]
    assert len(steps) == 1
    assert steps[0].raw["type"] == "soothe.skill.activated"
    assert steps[0].raw["skill"] == "docs"


@pytest.mark.asyncio
async def test_late_tool_args_refresh_the_status_line():
    """Args stream as partial JSON; surface them once they parse.

    Without the refresh the progress line stays a bare "Writing" for the whole
    call, which is the least informative moment of a run.
    """
    events = await collect(
        [
            message_chunk(
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[{"name": "write_file", "args": "", "id": "c1", "index": 0}],
                )
            ),
            message_chunk(
                AIMessageChunk(
                    content="",
                    tool_calls=[{"name": "write_file", "args": {"file_path": "a.txt"}, "id": "c1"}],
                )
            ),
            message_chunk(ToolMessage(content="ok", tool_call_id="c1", name="write_file")),
        ]
    )

    started = [e for e in events if isinstance(e, ToolStarted)]
    assert [e.tool for e in started] == ["write_file"]

    refresh = [
        e
        for e in events
        if isinstance(e, Progress) and (e.raw or {}).get("type") == "soothe.tool.started"
    ]
    assert len(refresh) == 1
    assert refresh[0].raw["args"]["file_path"] == "a.txt"


@pytest.mark.asyncio
async def test_write_todos_is_planning_not_a_tool_call():
    """Todo bookkeeping would otherwise read as a tool call on every turn."""
    events = await collect(
        [
            message_chunk(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_todos",
                            "args": {"todos": [{"content": "Do it", "status": "pending"}]},
                            "id": "c1",
                            "type": "tool_call",
                        }
                    ],
                )
            ),
            message_chunk(ToolMessage(content="updated", tool_call_id="c1", name="write_todos")),
        ]
    )

    assert [e.tool for e in events if isinstance(e, ToolCompleted)] == []
    assert [
        e for e in events if isinstance(e, Progress) and (e.raw or {}).get("tool") == "write_todos"
    ] == []


@pytest.mark.asyncio
async def test_run_starts_before_any_output():
    events = await collect([message_chunk(AIMessage(content="ok"))])
    assert isinstance(events[0], RunStarted)


class FailingAgent:
    async def astream(self, _input, **_kwargs):
        raise RuntimeError("broken graph")
        yield  # pragma: no cover


class BlockingAgent:
    async def astream(self, _input, **_kwargs):
        await asyncio.sleep(10)
        yield message_chunk(AIMessage(content="too late"))


def isolated_request() -> IsolatedRunRequest:
    return IsolatedRunRequest(
        run_id="resp_isolated",
        session="fj-isolated",
        input_text="hi",
        model="default",
        workspace=Path("/tmp/flowjet-test-workspace"),
    )


def test_flowjet_forces_workspace_boundary(monkeypatch, tmp_path):
    import soothe_nano

    # This test pins the nano bridge; the server surface is otherwise free to
    # run on the soothe backend (FLOWJET_SERVER_BACKEND), which builds different
    # agent objects and would not see the monkeypatch below.
    monkeypatch.setenv("FLOWJET_SERVER_BACKEND", "nano")

    config_path = tmp_path / "nano.yml"
    config_path.write_text(
        "security:\n  allow_paths_outside_workspace: true\n",
        encoding="utf-8",
    )
    captured: list[Any] = []
    sentinel = object()

    def create_agent(config):
        captured.append(config)
        return sentinel

    monkeypatch.setattr(soothe_nano, "create_dual_mode_nano_agent", create_agent)

    assert create_nano_agent_instance(config_path) is sentinel
    assert captured[0].security.allow_paths_outside_workspace is False


@pytest.mark.asyncio
async def test_nano_adapter_recycles_agent_after_failed_turn():
    agents = [
        FailingAgent(),
        StubAgent([message_chunk(AIMessage(content="recovered"))]),
    ]
    adapter = NanoAgentAdapter(agent_factory=lambda: agents.pop(0))

    failed = [event async for event in adapter.astream(isolated_request())]
    assert any(isinstance(event, RunFailed) for event in failed)
    assert adapter.generation == 1

    adapter.prepare_for_request()
    recovered = [event async for event in adapter.astream(isolated_request())]
    assert any(
        isinstance(event, RunCompleted) and event.output_text == "recovered" for event in recovered
    )
    assert adapter.generation == 2


@pytest.mark.asyncio
async def test_ask_interaction_mode_is_passed_in_configurable():
    agent = StubAgent([message_chunk(AIMessage(content="readonly answer"))])
    adapter = NanoAgentAdapter(agent=agent)
    req = IsolatedRunRequest(
        run_id="resp_ask",
        session="fj-ask",
        input_text="explain this file",
        model="default",
        workspace=Path("/tmp/flowjet-test-workspace"),
        metadata={"interaction_mode": "ask"},
    )
    events = [event async for event in adapter.astream(req)]
    assert any(isinstance(e, RunCompleted) for e in events)
    assert agent.last_config is not None
    assert agent.last_config["configurable"]["interaction_mode"] == "ask"
    assert agent.last_config["configurable"]["thread_id"] == "fj-ask"


def test_merge_flowjet_metadata_includes_interaction_mode():
    from flowjet.server.openai_compat.schemas import FlowjetOptions, merge_flowjet_metadata

    assert merge_flowjet_metadata(None) == {"interaction_mode": "agent"}
    assert merge_flowjet_metadata(FlowjetOptions(interaction_mode="ask")) == {
        "interaction_mode": "ask"
    }
    assert merge_flowjet_metadata(
        FlowjetOptions(interaction_mode="ask", metadata={"suite": "t"})
    ) == {"suite": "t", "interaction_mode": "ask"}


@pytest.mark.asyncio
async def test_nano_adapter_reuses_agent_after_clean_turn():
    agent = StubAgent([message_chunk(AIMessage(content="ok"))])
    adapter = NanoAgentAdapter(agent_factory=lambda: agent)

    for _ in range(2):
        events = [event async for event in adapter.astream(isolated_request())]
        assert isinstance(events[-1], RunCompleted)
        adapter.prepare_for_request()

    assert adapter.generation == 1


@pytest.mark.asyncio
async def test_nano_adapter_recycles_agent_after_cancelled_turn():
    agents = [
        BlockingAgent(),
        StubAgent([message_chunk(AIMessage(content="recovered"))]),
    ]
    adapter = NanoAgentAdapter(agent_factory=lambda: agents.pop(0))

    async def consume() -> list[Any]:
        return [event async for event in adapter.astream(isolated_request())]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    adapter.prepare_for_request()
    recovered = [event async for event in adapter.astream(isolated_request())]
    assert isinstance(recovered[-1], RunCompleted)
    assert adapter.generation == 2
