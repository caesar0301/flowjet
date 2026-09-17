"""Integration tests for ``fjf`` — alias for ``fj --follow``.

``fjf`` prepends ``--follow`` then runs the same ``main()`` path as ``fj``.
These tests cover injection, follow-mode queries, and conflicts that only
apply when follow is always on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Conflicts — follow is implicit, so -l is always invalid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["-l"], "-l/--list cannot be combined with -f/--follow"),
        (["-l", "-n", "5"], "-l/--list cannot be combined with -f/--follow"),
        (["-l", "hello"], "-l/--list does not take a query"),
        (["-lv"], "-l/--list cannot be combined with -f/--follow"),
        (["-lv", "hello"], "-l/--list does not take a query"),
        (["-n", "5"], "-n requires -l/--list"),
    ],
)
def test_fjf_rejects_invalid_compositions(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
    argv: list[str],
    needle: str,
) -> None:
    code, out, err = run_fjf(argv)
    assert code == 2, (argv, code, err)
    assert needle in err, (argv, err)
    assert out == ""
    assert stub_agent_runtime["build_calls"] == 0


# ---------------------------------------------------------------------------
# Valid follow-mode query compositions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_query", "expect_invoke"),
    [
        (["continue"], "continue", False),
        (["hello", "world"], "hello world", False),
        (["-v", "ask"], "ask", False),
        (["--no-stream", "ask"], "ask", True),
        (["-v", "--no-stream", "ask"], "ask", True),
        (["-f", "continue"], "continue", False),
        (["--follow", "continue"], "continue", False),
        (["--", "-v", "as", "query"], "-v as query", False),
        (["-w", "/tmp", "in", "workspace"], "in workspace", False),
        (["-c", "/tmp/nano.yml", "q"], "q", False),
    ],
)
def test_fjf_valid_query_compositions(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
    argv: list[str],
    expected_query: str,
    expect_invoke: bool,
) -> None:
    code, _out, err = run_fjf(argv)
    assert code == 0, (argv, err)
    assert stub_agent_runtime["resolve"] == {"explicit": None, "follow": True}
    assert stub_agent_runtime["build_calls"] == 1
    if expect_invoke:
        assert stub_agent_runtime["invoke"] is not None
        assert stub_agent_runtime["invoke"]["query"] == expected_query
        assert stub_agent_runtime["stream"] is None
    else:
        assert stub_agent_runtime["stream"] is not None
        assert stub_agent_runtime["stream"]["query"] == expected_query
        assert stub_agent_runtime["invoke"] is None


def test_fjf_uses_active_thread(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    code, _out, err = run_fjf(["continue", "please"])
    assert code == 0, err
    assert stub_agent_runtime["stream"]["thread_id"] == "fj-active-stub"


def test_fjf_thread_overrides_follow(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    """``fjf`` always injects ``--follow``; an explicit ``-t`` overrides it."""
    code, _out, err = run_fjf(["-t", "fj-explicit", "continue"])
    assert code == 0, err
    assert stub_agent_runtime["resolve"] == {"explicit": "fj-explicit", "follow": True}
    assert stub_agent_runtime["stream"]["query"] == "continue"
    assert stub_agent_runtime["stream"]["thread_id"] == "fj-explicit"


def test_fjf_verbose_prints_thread_on_stderr(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    code, _out, err = run_fjf(["-v", "hello"])
    assert code == 0
    assert "thread fj-active-stub" in err


def test_fjf_empty_and_option_only_prints_usage(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    for argv in ([], ["-v"], ["--no-stream"], ["-w", "/tmp"]):
        code, _out, err = run_fjf(argv)
        assert code == 2, argv
        assert "FlowJet — coding agent CLI" in err
    assert stub_agent_runtime["build_calls"] == 0


# ---------------------------------------------------------------------------
# Subcommands — no --follow injection when subcommand is argv[0]
# ---------------------------------------------------------------------------


def test_fjf_setup_does_not_inject_follow(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import flowjet.cli.setup_cmd as setup_cmd

    called: list[str | None] = []
    monkeypatch.setattr(setup_cmd, "run_setup", lambda path=None: called.append(path) or 0)
    code, _out, err = run_fjf(["setup"])
    assert code == 0
    assert called == [None]
    assert stub_agent_runtime["build_calls"] == 0


def test_fjf_completion_does_not_inject_follow(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    code, out, err = run_fjf(["completion", "zsh"])
    assert code == 0, err
    assert "#compdef fj" in out
    assert stub_agent_runtime["build_calls"] == 0


def test_fjf_workspace_flag_reaches_build(
    run_fjf: Any,
    stub_agent_runtime: dict[str, Any],
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    code, _out, err = run_fjf(["-w", str(ws), "continue"])
    assert code == 0, err
    assert stub_agent_runtime["workspace"] == ws.resolve()
