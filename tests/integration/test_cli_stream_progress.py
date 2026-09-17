"""Integration tests for live stream progress (CJK width, tail preview, throttle).

Uses real ``stream_query`` / ``ProgressLine`` / ``AnswerWriter`` with a stubbed
agent stream — no live model required.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

pytestmark = pytest.mark.integration

_ANSI_RE = re.compile(r"\033\[[0-9?]*[ -/]*[@-~]")
_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _msg_chunk(message: object) -> tuple[tuple[()], str, tuple[object, dict[str, object]]]:
    return ((), "messages", (message, {}))


def _plain_progress_text(raw: str) -> str:
    """Last ephemeral progress frame with spinner/ANSI stripped."""
    frames = re.findall(r"\r\033\[2K([^\r]*)", raw)
    if not frames:
        return ""
    plain = _ANSI_RE.sub("", frames[-1])
    return plain.lstrip(_SPINNER_CHARS + " ").strip()


@pytest.fixture
def run_fj_live_stream(
    soothe_home: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    live_stream_runtime: dict[str, Any],
) -> Any:
    from flowjet.cli import cli

    monkeypatch.setattr(cli, "configure_cli_logging", lambda **_k: None)

    def _run(argv: list[str]) -> tuple[int, str, str, dict[str, Any]]:
        code = cli.main(argv)
        captured = capsys.readouterr()
        return code, captured.out, captured.err, live_stream_runtime

    return _run


def test_cli_cjk_stream_prints_final_answer_once(
    live_stream_runtime: dict[str, Any],
    run_fj_live_stream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "最近一次 CI 运行（29836068180）全部绿色通过。"
    suffix = "现在创建 GitHub Release v1.0.8。"
    full = prefix + suffix
    live_stream_runtime["chunks"] = [
        _msg_chunk(AIMessageChunk(content=prefix)),
        _msg_chunk(AIMessageChunk(content=full)),
        _msg_chunk(AIMessage(content=full)),
    ]

    code, out, err, seen = run_fj_live_stream(["check", "release"])
    assert code == 0, err
    assert full + "\n" in out
    assert out.endswith(" · fj-stream-stub\n")
    assert out.count(full) == 1
    assert seen["tail_updates"] >= 1


def test_cli_cjk_progress_preview_shows_latest_clause(
    live_stream_runtime: dict[str, Any],
    run_fj_live_stream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flowjet.cli.progress import _display_width, _line_budget

    monkeypatch.setenv("FJ_PROGRESS_WIDTH", "36")
    prefix = "前面说明。" * 4
    suffix = "现在创建 GitHub Release v1.0.8。"
    full = prefix + suffix
    live_stream_runtime["chunks"] = [
        _msg_chunk(AIMessageChunk(content=full)),
        _msg_chunk(AIMessage(content=full)),
    ]

    code, out, err, _seen = run_fj_live_stream(["release"])
    assert code == 0, err
    preview = _plain_progress_text(out)
    if preview:
        assert _display_width(preview) <= _line_budget()
        assert "Release" in preview or "v1.0.8" in preview
        assert "前面说明" not in preview


def test_cli_stream_throttles_rapid_narration_chunks(
    live_stream_runtime: dict[str, Any],
    run_fj_live_stream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter([float(i) * 0.01 for i in range(2000)])
    monkeypatch.setattr("flowjet.cli.stream.time.monotonic", lambda: next(times))

    chunks = [_msg_chunk(AIMessageChunk(content="中" * (i + 1))) for i in range(50)]
    chunks.append(_msg_chunk(AIMessage(content="中" * 50)))
    live_stream_runtime["chunks"] = chunks

    code, _out, err, seen = run_fj_live_stream(["status"])
    assert code == 0, err
    assert seen["tail_updates"] < 50


def test_cli_no_stream_skips_live_narration_preview(
    live_stream_runtime: dict[str, Any],
    run_fj_live_stream: Any,
) -> None:
    narration = "正在分析仓库结构。"
    live_stream_runtime["chunks"] = [
        _msg_chunk(AIMessageChunk(content=narration)),
        _msg_chunk(AIMessage(content="## 分析结果\n完成。")),
    ]

    code, out, err, seen = run_fj_live_stream(["--no-stream", "analyze"])
    assert code == 0, err
    assert "## 分析结果\n完成。\n" in out
    assert out.endswith(" · fj-stream-stub\n")
    assert seen["tail_updates"] == 0
    preview = _plain_progress_text(out)
    assert narration not in preview


def test_cli_stream_mixed_cjk_english_tool_then_answer(
    live_stream_runtime: dict[str, Any],
    run_fj_live_stream: Any,
) -> None:
    tool_msg = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": "run_command",
                "args": '{"command": "gh run list --limit 1"}',
                "id": "call_1",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
        tool_calls=[
            {
                "name": "run_command",
                "args": {"command": "gh run list --limit 1"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    from langchain_core.messages import ToolMessage

    final = "CI 全部绿色通过，可以发布 v1.0.8。"
    live_stream_runtime["chunks"] = [
        _msg_chunk(AIMessageChunk(content="检查 CI 状态。")),
        _msg_chunk(tool_msg),
        _msg_chunk(ToolMessage(content="success", tool_call_id="call_1", name="run_command")),
        _msg_chunk(AIMessage(content=final)),
    ]

    code, out, err, seen = run_fj_live_stream(["ci", "status"])
    assert code == 0, err
    assert final + "\n" in out
    assert out.endswith(" · fj-stream-stub\n")
    assert seen["progress_updates"] >= 2
    assert seen["tail_updates"] >= 1
