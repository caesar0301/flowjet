"""Unit tests for fj completion (agent-free path)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from flowjet.cli import cli
from flowjet.cli.completion.context import append_history, build_context, read_history
from flowjet.cli.completion.engine import (
    Candidate,
    complete,
    merge_candidates,
    parse_candidates,
    static_candidates,
)


def test_static_flags() -> None:
    assert "--verbose" in static_candidates("--v")
    assert "--config" in static_candidates("--c")
    assert static_candidates("set") == ["setup"]
    assert "zsh" in static_candidates("completion z")


def test_parse_candidates_strips_noise() -> None:
    raw = """
    1. review recent changes
    - summarize this repository
    fj explain architecture
    `find TODOs`
    """
    got = parse_candidates(raw)
    assert got[0] == "review recent changes"
    assert "summarize this repository" in got
    assert "explain architecture" in got
    assert "find TODOs" in got


def test_history_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "hist.jsonl"
    append_history("review PR", path=path)
    append_history("summarize README", path=path)
    append_history("review PR", path=path)
    rows = read_history(path, limit=10)
    assert rows[0] == "review PR"
    assert "summarize README" in rows


def test_merge_prefers_history_score() -> None:
    got = merge_candidates(
        [
            [Candidate("review PR", "history", 0.95)],
            [Candidate("review tests", "llm", 0.82)],
            [Candidate("review PR", "llm", 0.80)],
        ],
        top_k=5,
    )
    assert got[0] == "review PR"
    assert got.count("review PR") == 1


def test_build_context_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert build_context([]).mode == "task"
    assert build_context(["--ver"]).mode == "static"
    assert build_context(["setup"]).mode == "static"
    assert build_context(["review", "recent"]).mode == "intent"


def test_complete_static_no_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    got = complete(["--ver"], use_llm=False)
    assert "--verbose" in got
    assert "--version" in got


def test_complete_task_builtins_without_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "flowjet.cli.completion.context.read_history",
        lambda limit=40: [],
    )
    got = complete([], use_llm=False)
    assert any("summarize" in g for g in got)


def test_complete_does_not_import_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    import sys

    # Ensure agent module is not required on the completion path.
    sys.modules.pop("flowjet.cli.agent", None)

    real_import = __import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name == "flowjet.cli.agent" or name.startswith("flowjet.cli.agent."):
            raise AssertionError("completion must not import flowjet.cli.agent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)
    complete(["rev"], use_llm=False)


def test_parse_args_complete_and_completion() -> None:
    args = cli.parse_args(["__complete", "--", "rev"])
    assert args.command == "__complete"
    assert args.complete_argv == ["--", "rev"]

    args2 = cli.parse_args(["completion", "zsh"])
    assert args2.command == "completion"
    assert args2.completion_argv == ["zsh"]


def test_main_complete_no_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "configure_cli_logging", lambda: None)

    def boom_agent(*_a: object, **_k: object) -> object:
        raise AssertionError("completion must not build an agent")

    # Agent is imported lazily inside run_async; patch the module if loaded.
    import flowjet.cli.agent as agent_mod
    import flowjet.cli.completion.context as history_mod

    monkeypatch.setattr(agent_mod, "build_agent", boom_agent)
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", boom_agent)
    monkeypatch.setattr(history_mod, "history_path", lambda: tmp_path / "history.jsonl")
    code = cli.main(["__complete", "--no-llm", "--"])
    assert code == 0
    out = capsys.readouterr().out
    assert "summarize" in out or "review" in out


def test_completion_script_zsh() -> None:
    from flowjet.cli.completion.engine import run_completion_script

    code = run_completion_script(["zsh"])
    assert code == 0


def test_llm_candidates_uses_fast_role() -> None:
    import asyncio
    from pathlib import Path

    from flowjet.cli.completion import engine as llm_mod
    from flowjet.cli.completion.context import CompletionContext

    calls: list[str] = []

    class FakeModel:
        async def ainvoke(self, _messages: object) -> MagicMock:
            msg = MagicMock()
            msg.content = "review staged files\nreview recent commits\n"
            return msg

    class FakeConfig:
        def create_chat_model(self, role: str = "default", **_k: object) -> FakeModel:
            calls.append(role)
            return FakeModel()

    ctx = CompletionContext(
        cwd=Path("."),
        project_root=None,
        git_repo=False,
        git_branch=None,
        query_prefix="review",
        mode="intent",
    )
    got = asyncio.run(llm_mod.llm_candidates(ctx, FakeConfig()))
    assert calls == ["fast"]
    assert "review staged files" in got
