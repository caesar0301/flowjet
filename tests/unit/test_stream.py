"""Tests for AI text accumulation, progress narration, and stream_query."""

from __future__ import annotations

from collections.abc import AsyncIterator
from io import StringIO
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from flowjet.cli.progress import ProgressLine
from flowjet.cli.stream import (
    INTERRUPTED,
    AnswerWriter,
    _ai_text,
    _status_preview,
    _verbose_preview,
    accumulate_ai_text,
    format_questions_block,
    invoke_query,
    stream_query,
)


def test_ai_text_string() -> None:
    assert _ai_text(AIMessage(content="hello")) == "hello"


def test_ai_text_blocks() -> None:
    msg = AIMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert _ai_text(msg) == "ab"


def test_ai_text_mixed_blocks_and_strings() -> None:
    msg = AIMessage(content=["pre", {"type": "text", "text": "mid"}, {"type": "image"}])
    assert _ai_text(msg) == "premid"


def test_ai_text_fallback_str(monkeypatch: pytest.MonkeyPatch) -> None:
    class WeirdMessage:
        content = 12345

    def boom(_msg: object) -> str:
        raise RuntimeError("no helper")

    # Patch inside the function's try/import path.
    import soothe_nano.llm.response_text as rt

    monkeypatch.setattr(rt, "llm_response_text", boom)
    assert _ai_text(WeirdMessage()) == "12345"  # type: ignore[arg-type]


def test_verbose_preview_collapses_and_truncates() -> None:
    assert _verbose_preview("plain") == "plain"
    assert _verbose_preview("a\nb\nc") == "a b c"
    assert _verbose_preview("x" * 10, limit=5) == "xxxxx…"
    assert "[" in _verbose_preview([{"a": 1}])
    assert _verbose_preview(42) == "42"


def test_accumulate_chunk_deltas() -> None:
    buf = ""
    for part in ["My name is ", "Soothe", ". How can I help", " you today?"]:
        buf = accumulate_ai_text(buf, AIMessageChunk(content=part))
    assert buf == "My name is Soothe. How can I help you today?"


def test_accumulate_cumulative_snapshots() -> None:
    buf = ""
    buf = accumulate_ai_text(buf, AIMessageChunk(content="Hello"))
    buf = accumulate_ai_text(buf, AIMessageChunk(content="Hello world"))
    assert buf == "Hello world"


def test_accumulate_keeps_longer_on_shorter_replay() -> None:
    buf = accumulate_ai_text("", AIMessageChunk(content="Hello world"))
    buf = accumulate_ai_text(buf, AIMessageChunk(content="Hello"))
    assert buf == "Hello world"


def test_accumulate_empty_text_keeps_current() -> None:
    assert accumulate_ai_text("keep", AIMessageChunk(content="")) == "keep"


def test_accumulate_full_message_replaces() -> None:
    buf = accumulate_ai_text("partial", AIMessageChunk(content="partial"))
    buf = accumulate_ai_text(buf, AIMessage(content="Full final answer."))
    assert buf == "Full final answer."


def test_accumulate_full_message_keeps_longer_chunk_buffer() -> None:
    buf = ""
    for part in ["I've analyzed the full project. ", "The diagrams above show everything."]:
        buf = accumulate_ai_text(buf, AIMessageChunk(content=part))
    assert len(buf) > 40
    shorter = accumulate_ai_text(buf, AIMessage(content="I've analyzed the full project."))
    assert shorter == buf


def test_status_preview_prefers_latest_sentence() -> None:
    assert _status_preview("First. Second step now") == "Second step now"
    assert _status_preview("   ") == "Writing answer…"


def test_status_preview_splits_cjk_sentences() -> None:
    text = "最近一次 CI 全部绿色通过。现在创建 GitHub Release v1.0.8。"
    assert _status_preview(text) == "现在创建 GitHub Release v1.0.8。"


def test_status_preview_splits_em_dash() -> None:
    text = "annotation 仅是警告——现在提交并发布。"
    assert _status_preview(text) == "现在提交并发布。"


def test_answer_writer_live_buffers_until_finish() -> None:
    out = StringIO()
    status = ProgressLine(out, enabled=False)
    writer = AnswerWriter(out, status, live=True)

    writer.set("Hello")
    writer.set("Hello world")
    assert out.getvalue() == ""
    assert writer.finish() == "Hello world"
    assert out.getvalue() == "Hello world\n"


def test_answer_writer_buffered_waits_until_finish() -> None:
    out = StringIO()
    status = ProgressLine(out, enabled=False)
    writer = AnswerWriter(out, status, live=False)

    writer.set("Hello")
    writer.set("Hello world")
    assert out.getvalue() == ""
    assert writer.finish() == "Hello world"
    assert out.getvalue() == "Hello world\n"


def test_answer_writer_reset_for_tools_drops_intermediate() -> None:
    out = StringIO()
    status = ProgressLine(out, enabled=False)
    writer = AnswerWriter(out, status, live=True)

    writer.set("Looking around")
    writer.reset_for_tools()
    writer.set("Done.")
    assert writer.finish() == "Done."
    assert out.getvalue() == "Done.\n"


def test_answer_writer_divergent_replace_keeps_latest_only() -> None:
    out = StringIO()
    status = ProgressLine(out, enabled=False)
    writer = AnswerWriter(out, status, live=True)

    writer.set("First draft")
    writer.set("Completely different")
    assert writer.finish() == "Completely different"
    assert out.getvalue() == "Completely different\n"


def test_answer_writer_live_updates_progress_preview() -> None:
    out = StringIO()
    status = ProgressLine(out, enabled=True)
    writer = AnswerWriter(out, status, live=True)
    writer.set("I'll fetch the latest stock news")
    assert "fetch the latest" in out.getvalue() or "stock news" in out.getvalue()
    assert writer.buf == "I'll fetch the latest stock news"
    assert not out.getvalue().endswith("I'll fetch the latest stock news\n")


def test_answer_writer_throttles_rapid_preview_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    out = StringIO()
    status = ProgressLine(out, enabled=True)
    writer = AnswerWriter(out, status, live=True)
    clock = {"t": 0.0}
    monkeypatch.setattr("flowjet.cli.stream.time.monotonic", lambda: clock["t"])

    writer.set("a")
    clock["t"] = 0.01
    writer.set("ab")
    clock["t"] = 0.02
    writer.set("abc")
    paints_before = out.getvalue().count("\r")
    clock["t"] = 0.20
    writer.set("abcd")
    paints_after = out.getvalue().count("\r")
    assert paints_before == 1
    assert paints_after == 2


def test_answer_writer_reset_clears_throttle_state(monkeypatch: pytest.MonkeyPatch) -> None:
    out = StringIO()
    status = ProgressLine(out, enabled=True)
    writer = AnswerWriter(out, status, live=True)
    clock = {"t": 0.0}
    monkeypatch.setattr("flowjet.cli.stream.time.monotonic", lambda: clock["t"])

    writer.set("before tools")
    paints_after_first = out.getvalue().count("\r")
    writer.reset_for_tools()
    clock["t"] = 0.01
    writer.set("after tools")
    paints_after_reset = out.getvalue().count("\r")
    assert paints_after_reset == paints_after_first + 1


def test_status_preview_skips_trailing_marker_without_tail() -> None:
    assert _status_preview("全部通过。") == "全部通过。"
    assert _status_preview("第一段。第二段。") == "第二段。"


@pytest.mark.asyncio
async def test_stream_query_cjk_narration_preview_uses_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flowjet.cli.progress import ProgressLine, _display_width, _line_budget

    monkeypatch.setenv("FJ_PROGRESS_WIDTH", "36")
    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        clock["t"] += 0.15
        return clock["t"]

    monkeypatch.setattr("flowjet.cli.stream.time.monotonic", fake_monotonic)
    out = StringIO()
    progress = ProgressLine(out, enabled=True)
    updates: list[tuple[str, bool]] = []
    original_update = ProgressLine.update

    def capture_update(
        self: ProgressLine, message: str, *, color: str = "cyan", tail: bool = False
    ) -> None:
        updates.append((message, tail))
        original_update(self, message, color=color, tail=tail)

    monkeypatch.setattr(ProgressLine, "update", capture_update)

    prefix = "最近一次 CI 运行全部绿色通过。"
    suffix = "现在创建 GitHub Release v1.0.8。"
    full = prefix + suffix
    agent = _FakeAgent(
        [
            _msg_chunk(AIMessageChunk(content=prefix)),
            _msg_chunk(AIMessageChunk(content=full)),
        ]
    )
    result = await stream_query(
        agent,  # type: ignore[arg-type]
        "status",
        thread_id="t1",
        live_answer=True,
        out=out,
        progress=progress,
    )
    assert result == full
    assert full + "\n" in out.getvalue()
    assert out.getvalue().endswith(" · t1\n")
    tail_updates = [msg for msg, used_tail in updates if used_tail]
    assert tail_updates
    assert any("现在创建" in msg for msg in tail_updates)
    assert tail_updates[-1] == suffix or "现在创建" in tail_updates[-1]
    assert _display_width(tail_updates[-1]) <= _line_budget()


@pytest.mark.asyncio
async def test_stream_query_many_chunks_throttles_narration_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = StringIO()
    times = iter([float(i) * 0.01 for i in range(2000)])
    monkeypatch.setattr("flowjet.cli.stream.time.monotonic", lambda: next(times))

    chunks = [_msg_chunk(AIMessageChunk(content="x" * (i + 1))) for i in range(40)]
    chunks.append(_msg_chunk(AIMessage(content="x" * 40)))
    agent = _FakeAgent(chunks)

    narration_updates = 0
    original_set = AnswerWriter.set

    def counting_set(self: AnswerWriter, new_buf: str) -> None:
        nonlocal narration_updates
        before = self._last_preview
        original_set(self, new_buf)
        if self._last_preview != before and self._live:
            narration_updates += 1

    monkeypatch.setattr(AnswerWriter, "set", counting_set)

    await stream_query(
        agent,  # type: ignore[arg-type]
        "q",
        thread_id="t1",
        live_answer=True,
        out=out,
        progress=ProgressLine(out, enabled=False),
    )
    assert narration_updates < 40


def test_answer_writer_finish_prints_complete_buffer() -> None:
    out = StringIO()
    status = ProgressLine(out, enabled=False)
    writer = AnswerWriter(out, status, live=True)
    writer.buf = (
        "The external boundary to `soothe_nano`/`langchain`/`aiosqlite`, "
        "and a per-module responsibility table."
    )
    writer.finish()
    assert "soothe_nano" in out.getvalue()
    assert out.getvalue().endswith("table.\n")


class _FakeAgent:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    async def astream(self, *_a: Any, **_k: Any) -> AsyncIterator[Any]:
        for chunk in self._chunks:
            yield chunk


def _msg_chunk(message: Any) -> tuple[tuple[()], str, tuple[Any, dict[str, Any]]]:
    return ((), "messages", (message, {}))


@pytest.mark.asyncio
async def test_stream_query_live_answer_and_custom_event() -> None:
    out = StringIO()
    err = StringIO()
    agent = _FakeAgent(
        [
            ((), "custom", {"type": "soothe.cognition.strange_loop.started"}),
            "skip-me",
            ((), "other", {}),
            _msg_chunk(AIMessageChunk(content="Hi")),
            _msg_chunk(AIMessageChunk(content="Hi there")),
        ]
    )
    result = await stream_query(
        agent,  # type: ignore[arg-type]
        "hello",
        thread_id="t1",
        show_tool_calls=True,
        live_answer=True,
        out=out,
        err=err,
        progress=ProgressLine(out, enabled=False),
    )
    assert result == "Hi there"
    assert out.getvalue().startswith("Hi there\n")
    assert out.getvalue().endswith(" · t1\n")
    assert "[event]" in err.getvalue()


@pytest.mark.asyncio
async def test_stream_query_tool_call_and_error_result() -> None:
    out = StringIO()
    err = StringIO()
    tool_msg = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": "read_file",
                "args": '{"file_path": "a.py"}',
                "id": "call_1",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
        tool_calls=[
            {
                "name": "read_file",
                "args": {"file_path": "a.py"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    result_msg = ToolMessage(
        content="Error: unexpected keyword argument 'limit'",
        tool_call_id="call_1",
        name="read_file",
        status="error",
    )
    agent = _FakeAgent(
        [
            ((), "updates", {"__interrupt__": True}),
            _msg_chunk(tool_msg),
            _msg_chunk(result_msg),
            _msg_chunk(AIMessage(content="Recovered.")),
        ]
    )
    result = await stream_query(
        agent,  # type: ignore[arg-type]
        "read it",
        thread_id="t1",
        show_tool_calls=True,
        live_answer=True,
        out=out,
        err=err,
        progress=ProgressLine(out, enabled=False),
    )
    assert result == "Recovered."
    assert out.getvalue().startswith("Recovered.\n")
    assert out.getvalue().endswith(" · t1\n")
    stderr = err.getvalue()
    assert "[interrupted]" in stderr
    assert "[tool]" in stderr
    assert "[error]" in stderr
    assert "limit" in stderr


@pytest.mark.asyncio
async def test_verbose_skips_noisy_events_and_partial_tool_args() -> None:
    """``-v`` must not dump policy.checked or mid-stream arg fragments."""
    out = StringIO()
    err = StringIO()

    def path_chunk(args_fragment: str, *, name: str | None = None, tc_id: str = "") -> Any:
        return AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": name,
                    "args": args_fragment,
                    "id": tc_id,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )

    agent = _FakeAgent(
        [
            ((), "custom", {"type": "soothe.internal.policy.checked", "verdict": "allow"}),
            ((), "custom", {"type": "soothe.internal.plugin.health_checked"}),
            ((), "custom", {"type": "soothe.output.token", "text": "x"}),
            ((), "custom", {"type": "soothe.cognition.strange_loop.started"}),
            _msg_chunk(path_chunk("", name="ls", tc_id="call_ls")),
            _msg_chunk(path_chunk('{"path": "/Users/')),
            _msg_chunk(path_chunk('chenxm/Workspace"}')),
            _msg_chunk(
                ToolMessage(
                    content="dir\na\nb\nc\n" * 20,
                    tool_call_id="call_ls",
                    name="ls",
                )
            ),
            _msg_chunk(AIMessage(content="done")),
        ]
    )
    result = await stream_query(
        agent,  # type: ignore[arg-type]
        "list",
        thread_id="t1",
        show_tool_calls=True,
        live_answer=True,
        out=out,
        err=err,
        progress=ProgressLine(out, enabled=False),
    )
    assert result == "done"
    stderr = err.getvalue()
    assert "policy.checked" not in stderr
    assert "health_checked" not in stderr
    assert "soothe.output.token" not in stderr
    assert "[event] soothe.cognition.strange_loop.started" in stderr
    # One complete tool mirror — not a line per partial path fragment.
    assert stderr.count("[tool] ls") == 1
    assert "/Users/chenxm/Workspace" in stderr
    assert "[result]" in stderr
    # Result preview is a single logical line (no raw newlines from tool output).
    result_line = next(line for line in stderr.splitlines() if "[result]" in line)
    assert result_line.count("dir") >= 1
    assert len(result_line) <= 220


@pytest.mark.asyncio
async def test_stream_query_drops_intermediate_ai_narration() -> None:
    """Multi-step agent talk before tools must not appear in the final result."""
    out = StringIO()

    def tool_chunk(call_id: str) -> AIMessageChunk:
        return AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "web_search",
                    "args": '{"query": "stock news"}',
                    "id": call_id,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
            tool_calls=[
                {
                    "name": "web_search",
                    "args": {"query": "stock news"},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        )

    agent = _FakeAgent(
        [
            _msg_chunk(AIMessage(content="I'll fetch the latest stock news.")),
            _msg_chunk(tool_chunk("call_1")),
            _msg_chunk(ToolMessage(content="headlines…", tool_call_id="call_1", name="web_search")),
            _msg_chunk(AIMessage(content="Yahoo is rate-limiting. Trying RSS.")),
            _msg_chunk(tool_chunk("call_2")),
            _msg_chunk(ToolMessage(content="rss ok", tool_call_id="call_2", name="web_search")),
            _msg_chunk(AIMessage(content="## Latest Stock Market News\n1. Futures rise")),
        ]
    )
    result = await stream_query(
        agent,  # type: ignore[arg-type]
        "latest stock news",
        thread_id="t1",
        live_answer=True,
        out=out,
        progress=ProgressLine(out, enabled=False),
    )
    assert result.startswith("## Latest Stock Market News")
    printed = out.getvalue()
    assert printed.startswith("## Latest Stock Market News\n1. Futures rise\n")
    assert printed.endswith(" · t1\n")
    assert "I'll fetch" not in printed
    assert "rate-limiting" not in printed


@pytest.mark.asyncio
async def test_stream_query_keeps_longer_chunk_buffer_at_end() -> None:
    """Final AIMessage must not truncate text already accumulated from chunks."""
    out = StringIO()
    long_answer = (
        "The external boundary to `soothe_nano`/`langchain`/`aiosqlite`, "
        "and a per-module responsibility table with LOC counts."
    )
    agent = _FakeAgent(
        [
            _msg_chunk(AIMessageChunk(content=long_answer[:60])),
            _msg_chunk(AIMessageChunk(content=long_answer)),
            _msg_chunk(AIMessage(content=long_answer[:40])),
        ]
    )
    result = await stream_query(
        agent,  # type: ignore[arg-type]
        "analyze deps",
        thread_id="t1",
        live_answer=True,
        out=out,
        progress=ProgressLine(out, enabled=False),
    )
    assert result == long_answer
    assert out.getvalue().startswith(long_answer + "\n")
    assert out.getvalue().endswith(" · t1\n")


@pytest.mark.asyncio
async def test_invoke_query_buffers_until_end() -> None:
    out = StringIO()
    agent = _FakeAgent(
        [
            _msg_chunk(AIMessageChunk(content="Final")),
            _msg_chunk(AIMessageChunk(content="Final answer")),
        ]
    )
    result = await invoke_query(
        agent,  # type: ignore[arg-type]
        "q",
        thread_id="t1",
        out=out,
        progress=ProgressLine(out, enabled=False),
    )
    assert result == "Final answer"
    assert out.getvalue().startswith("Final answer\n")
    assert out.getvalue().endswith(" · t1\n")


# ---------------------------------------------------------------------------
# ask_user interrupt — questions rendering + INTERRUPTED sentinel + resume
# ---------------------------------------------------------------------------


def _interrupt_chunk(questions: list[dict[str, Any]]) -> tuple[tuple[()], str, dict[str, Any]]:
    """Build an ``updates`` chunk carrying an ``ask_user`` __interrupt__."""
    from langgraph.types import Interrupt

    value = {"type": "ask_user", "questions": questions}
    return ((), "updates", {"__interrupt__": (Interrupt(value=value, id="int-1"),)})


_QUESTIONS = [
    {
        "question": "Which auth method should I use?",
        "header": "Auth method",
        "options": [
            {"label": "API Key (Recommended)", "description": "Use an API key"},
            {"label": "OAuth", "description": "Use OAuth flow"},
        ],
    }
]


@pytest.mark.asyncio
async def test_stream_query_ask_user_interrupt_returns_interrupted() -> None:
    """An ask_user interrupt renders questions and returns INTERRUPTED."""
    out = StringIO()
    err = StringIO()
    agent = _FakeAgent(
        [
            _interrupt_chunk(_QUESTIONS),
        ]
    )
    result = await stream_query(
        agent,  # type: ignore[arg-type]
        "set up auth",
        thread_id="t-auth",
        live_answer=True,
        out=out,
        err=err,
        progress=ProgressLine(out, enabled=False),
    )
    assert result == INTERRUPTED
    output = out.getvalue()
    assert "Paused for input" in output
    assert "Auth method" in output
    assert "Which auth method" in output
    assert "API Key (Recommended)" in output
    assert "fjf -t t-auth" in output
    # Must NOT print "Done" when interrupted.
    assert "Done" not in output


@pytest.mark.asyncio
async def test_stream_query_non_ask_user_interrupt_continues() -> None:
    """A non-ask_user interrupt (no questions) does not set INTERRUPTED."""
    out = StringIO()
    err = StringIO()
    agent = _FakeAgent(
        [
            ((), "updates", {"__interrupt__": True}),
            _msg_chunk(AIMessage(content="Recovered.")),
        ]
    )
    result = await stream_query(
        agent,  # type: ignore[arg-type]
        "go",
        thread_id="t1",
        show_tool_calls=True,
        live_answer=True,
        out=out,
        err=err,
        progress=ProgressLine(out, enabled=False),
    )
    assert result == "Recovered."
    assert out.getvalue().startswith("Recovered.\n")


@pytest.mark.asyncio
async def test_stream_query_resume_value_passed_to_backend() -> None:
    """When resume_value is set, the run resumes the paused graph."""
    from langgraph.types import Command

    captured: dict[str, Any] = {}

    class _CaptureAgent:
        async def astream(self, payload: Any, *_a: Any, **_k: Any) -> Any:
            captured["payload"] = payload
            yield _msg_chunk(AIMessage(content="Resumed!"))

    out = StringIO()
    result = await stream_query(
        _CaptureAgent(),  # type: ignore[arg-type]
        "my answer",
        thread_id="t1",
        resume_value="my answer",
        live_answer=True,
        out=out,
        progress=ProgressLine(out, enabled=False),
    )
    assert result == "Resumed!"
    # Resume sends a Command(resume=...) — not a HumanMessage dict.
    assert isinstance(captured["payload"], Command)
    assert captured["payload"].resume == "my answer"


def test_format_questions_block_renders_header_and_options() -> None:
    block = format_questions_block(tuple(_QUESTIONS), "t-123")
    assert "Paused for input" in block
    assert "[Auth method]" in block
    assert "Which auth method should I use?" in block
    assert "API Key (Recommended)" in block
    assert "OAuth" in block
    assert "fjf -t t-123" in block


def test_format_questions_block_empty_shows_generic_hint() -> None:
    block = format_questions_block((), "t-456")
    assert "Paused for input" in block
    assert "fjf -t t-456" in block


# ---------------------------------------------------------------------------
# _extract_ask_user_interrupts — pulls questions from __interrupt__ payloads
# ---------------------------------------------------------------------------


def test_extract_ask_user_interrupts_from_interrupt_objects() -> None:
    from langgraph.types import Interrupt

    from flowjet.core.bridges.nano.mapping import _extract_ask_user_interrupts

    value = {"type": "ask_user", "questions": _QUESTIONS}
    raw = (Interrupt(value=value, id="int-1"),)
    questions, ids = _extract_ask_user_interrupts(raw)
    assert len(questions) == 1
    assert questions[0]["header"] == "Auth method"
    assert ids == ("int-1",)


def test_extract_ask_user_interrupts_from_dicts() -> None:
    from flowjet.core.bridges.nano.mapping import _extract_ask_user_interrupts

    value = {"type": "ask_user", "questions": _QUESTIONS}
    raw = ({"value": value, "id": "int-2"},)
    questions, ids = _extract_ask_user_interrupts(raw)
    assert len(questions) == 1
    assert ids == ("int-2",)


def test_extract_ask_user_interrupts_skips_non_ask_user() -> None:
    from langgraph.types import Interrupt

    from flowjet.core.bridges.nano.mapping import _extract_ask_user_interrupts

    # A tool-approval interrupt (no ask_user type) — no questions, but id kept.
    raw = (Interrupt(value={"action_requests": []}, id="int-3"),)
    questions, ids = _extract_ask_user_interrupts(raw)
    assert questions == ()
    assert ids == ("int-3",)


def test_extract_ask_user_interrupts_empty() -> None:
    from flowjet.core.bridges.nano.mapping import _extract_ask_user_interrupts

    questions, ids = _extract_ask_user_interrupts(())
    assert questions == ()
    assert ids == ()


def test_extract_ask_user_interrupts_multiple_questions() -> None:
    from langgraph.types import Interrupt

    from flowjet.core.bridges.nano.mapping import _extract_ask_user_interrupts

    qs = [
        {"question": "Q1?", "header": "H1", "options": []},
        {"question": "Q2?", "header": "H2", "options": []},
    ]
    value = {"type": "ask_user", "questions": qs}
    raw = (Interrupt(value=value, id="int-4"),)
    questions, ids = _extract_ask_user_interrupts(raw)
    assert len(questions) == 2
    assert questions[0]["header"] == "H1"
    assert questions[1]["header"] == "H2"
    assert ids == ("int-4",)
