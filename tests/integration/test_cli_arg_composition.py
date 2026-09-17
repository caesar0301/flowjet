"""Integration tests for ``fj`` CLI argument composition.

These drive ``cli.main()`` end-to-end with an isolated ``SOOTHE_HOME``. Agent /
stream calls are stubbed so compositions never require a live model, while pin /
reset / lock / validation use the real CLI paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Conflict matrix — must fail fast with a clear error (exit 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["-n", "5"], "-n requires -l/--list"),
        (["-n", "5", "hello"], "-n requires -l/--list"),
        (["--list", "-n", "3", "leftover"], "-l/--list does not take a query"),
        (["-l", "hello"], "-l/--list does not take a query"),
        (["-l", "-t", "fj-x"], "-l/--list cannot be combined with -t/--thread"),
        (["-l", "-f"], "-l/--list cannot be combined with -f/--follow"),
        (["-l", "-w", "/tmp"], "-l/--list cannot be combined with -w/--workspace"),
        (["-l", "--no-stream"], "-l/--list cannot be combined with --no-stream"),
        (["-lv", "hello"], "-l/--list does not take a query"),
        (["-vl", "hello"], "-l/--list does not take a query"),
    ],
)
def test_main_rejects_invalid_compositions(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
    argv: list[str],
    needle: str,
) -> None:
    code, out, err = run_fj(argv)
    assert code == 2, (argv, code, err)
    assert needle in err, (argv, err)
    assert out == ""
    assert stub_agent_runtime["build_calls"] == 0
    assert stub_agent_runtime["stream"] is None
    assert stub_agent_runtime["invoke"] is None


def test_main_rejects_negative_list_limit(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    code, _out, err = run_fj(["-l", "-n", "-1"])
    assert code == 2
    assert "-n must be >= 0" in err
    assert stub_agent_runtime["list_limit"] is None


# ---------------------------------------------------------------------------
# Meta / no-query flags
# ---------------------------------------------------------------------------


def test_main_help_and_version_exit_cleanly(run_fj: Any) -> None:
    code, out, err = run_fj(["-h"])
    assert code == 0
    help_l = out.lower()
    assert "usage: fj" in help_l or "usage: flowjet" in help_l
    # Full composition docs live in the epilog.
    assert "-l/--list" in out or "-l" in out

    code, out, err = run_fj(["--version"])
    assert code == 0
    assert "fj " in out or "flowjet " in out
    assert err == ""


def test_main_empty_and_option_only_prints_usage(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    for argv in ([], ["-v"], ["--verbose"], ["--no-stream"], ["-w", "/tmp"], ["-c", "/tmp/x.yml"]):
        code, _out, err = run_fj(argv)
        assert code == 2, argv
        assert "FlowJet — coding agent CLI" in err
    assert stub_agent_runtime["build_calls"] == 0


# ---------------------------------------------------------------------------
# Thread pin (real filesystem under isolated SOOTHE_HOME)
# ---------------------------------------------------------------------------


def test_main_thread_alone_pins_explicit_id(
    run_fj: Any,
    active_thread_file: Path,
    stub_agent_runtime: dict[str, Any],
) -> None:
    code, out, err = run_fj(["-t", "fj-pinned-from-cli"])
    assert code == 0
    assert out.strip() == "fj-pinned-from-cli"
    assert active_thread_file.read_text(encoding="utf-8").strip() == "fj-pinned-from-cli"
    assert stub_agent_runtime["build_calls"] == 0

    code, out, err = run_fj(["--thread", "fj-other"])
    assert code == 0
    assert out.strip() == "fj-other"


def test_main_thread_pin_is_scoped_per_workdir(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two projects keep independent pins, so parallel work never collides."""
    from flowjet.cli.threads import active_thread_path

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    for repo in (repo_a, repo_b):
        (repo / ".git").mkdir(parents=True)
    nested_a = repo_a / "src" / "pkg"
    nested_a.mkdir(parents=True)

    # A subdirectory of repo-a shares repo-a's pin (git root is the key).
    monkeypatch.chdir(nested_a)
    assert run_fj(["-t", "fj-in-a"])[0] == 0
    pin_a = active_thread_path()
    assert pin_a == active_thread_path(repo_a)

    monkeypatch.chdir(repo_b)
    assert run_fj(["-t", "fj-in-b"])[0] == 0
    pin_b = active_thread_path()

    assert pin_a != pin_b
    assert pin_a.read_text(encoding="utf-8").strip() == "fj-in-a"
    assert pin_b.read_text(encoding="utf-8").strip() == "fj-in-b"


def test_main_follow_with_thread_uses_explicit_id(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    """``-t`` overrides ``-f`` — both may appear together; explicit id wins."""
    code, _out, err = run_fj(["-f", "-t", "fj-x", "continue"])
    assert code == 0, err
    assert stub_agent_runtime["resolve"] == {"explicit": "fj-x", "follow": True}
    assert stub_agent_runtime["stream"]["query"] == "continue"
    assert stub_agent_runtime["stream"]["thread_id"] == "fj-x"


# ---------------------------------------------------------------------------
# List compositions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_limit"),
    [
        (["-l"], 20),
        (["--list"], 20),
        (["-l", "-n", "5"], 5),
        (["-l", "-n", "0"], 0),
        (["-lv"], 20),
        (["-vl"], 20),
        (["-l", "-v"], 20),
        (["-l", "-c", "/tmp/nano.yml"], 20),
    ],
)
def test_main_list_compositions(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
    argv: list[str],
    expected_limit: int,
) -> None:
    code, out, err = run_fj(argv)
    assert code == 0, (argv, err)
    assert "fj-newer" in out
    assert "fj-older" in out
    assert out.index("fj-newer") < out.index("fj-older")
    assert stub_agent_runtime["list_limit"] == expected_limit
    assert stub_agent_runtime["build_calls"] == 0


# ---------------------------------------------------------------------------
# Valid query compositions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_query", "expected_resolve", "expect_invoke"),
    [
        (["hello"], "hello", {"explicit": None, "follow": False}, False),
        (["hello", "world"], "hello world", {"explicit": None, "follow": False}, False),
        (["café", "résumé"], "café résumé", {"explicit": None, "follow": False}, False),
        (["-v", "ask"], "ask", {"explicit": None, "follow": False}, False),
        (["--verbose", "ask", "now"], "ask now", {"explicit": None, "follow": False}, False),
        (["--no-stream", "ask"], "ask", {"explicit": None, "follow": False}, True),
        (["-v", "--no-stream", "ask"], "ask", {"explicit": None, "follow": False}, True),
        (["-f", "continue"], "continue", {"explicit": None, "follow": True}, False),
        (["-t", "fj-x", "continue"], "continue", {"explicit": "fj-x", "follow": False}, False),
        (
            ["--thread", "fj-x", "continue"],
            "continue",
            {"explicit": "fj-x", "follow": False},
            False,
        ),
        (
            ["--thread=fj-x", "continue"],
            "continue",
            {"explicit": "fj-x", "follow": False},
            False,
        ),
        (["--", "-v", "as", "query"], "-v as query", {"explicit": None, "follow": False}, False),
        (["--", "--reset"], "--reset", {"explicit": None, "follow": False}, False),
        (["--", "-lv"], "-lv", {"explicit": None, "follow": False}, False),
        (["-weird"], "-weird", {"explicit": None, "follow": False}, False),
        (
            ["-w", "/tmp", "in", "workspace"],
            "in workspace",
            {"explicit": None, "follow": False},
            False,
        ),
        (
            ["--workspace=/tmp", "q"],
            "q",
            {"explicit": None, "follow": False},
            False,
        ),
        (
            ["-c", "/tmp/nano.yml", "q"],
            "q",
            {"explicit": None, "follow": False},
            False,
        ),
        (
            ["--config=/tmp/nano.yml", "q"],
            "q",
            {"explicit": None, "follow": False},
            False,
        ),
    ],
)
def test_main_valid_query_compositions(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
    argv: list[str],
    expected_query: str,
    expected_resolve: dict[str, object],
    expect_invoke: bool,
) -> None:
    code, _out, err = run_fj(argv)
    assert code == 0, (argv, err)
    assert stub_agent_runtime["resolve"] == expected_resolve
    assert stub_agent_runtime["build_calls"] == 1
    if expect_invoke:
        assert stub_agent_runtime["invoke"] is not None
        assert stub_agent_runtime["invoke"]["query"] == expected_query
        assert stub_agent_runtime["stream"] is None
    else:
        assert stub_agent_runtime["stream"] is not None
        assert stub_agent_runtime["stream"]["query"] == expected_query
        assert stub_agent_runtime["invoke"] is None


def test_main_workspace_flag_reaches_build(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    code, _out, err = run_fj(["-w", str(ws), "do", "stuff"])
    assert code == 0, err
    assert stub_agent_runtime["workspace"] == ws.resolve()


def test_main_verbose_prints_thread_on_stderr(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    code, _out, err = run_fj(["-v", "hello"])
    assert code == 0
    assert "thread fj-new-stub" in err


# ---------------------------------------------------------------------------
# Subcommands: setup / doctor / completion / __complete
# ---------------------------------------------------------------------------


def test_main_setup_does_not_enter_query_path(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import flowjet.cli.setup_cmd as setup_cmd

    called: list[str | None] = []
    monkeypatch.setattr(setup_cmd, "run_setup", lambda path=None: called.append(path) or 0)
    code, _out, err = run_fj(["setup"])
    assert code == 0
    assert called == [None]
    assert stub_agent_runtime["build_calls"] == 0

    code, _out, err = run_fj(["setup", "-c", "/tmp/custom.yml"])
    assert code == 0
    assert called[-1] == "/tmp/custom.yml"


def test_main_doctor_does_not_enter_query_path(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import flowjet.cli.doctor_cmd as doctor_cmd

    called: list[list[str]] = []
    monkeypatch.setattr(
        doctor_cmd,
        "run_doctor",
        lambda argv: called.append(list(argv)) or 0,
    )
    code, _out, err = run_fj(["doctor", "--deep"])
    assert code == 0, err
    assert called == [["--deep"]]
    assert stub_agent_runtime["build_calls"] == 0


def test_main_completion_scripts(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    code, out, err = run_fj(["completion", "zsh"])
    assert code == 0, err
    assert "#compdef fj" in out
    assert "--follow" in out

    code, out, err = run_fj(["completion", "bash"])
    assert code == 0, err
    assert "_fj()" in out
    assert "--follow" in out

    assert stub_agent_runtime["build_calls"] == 0


def test_main_completion_rejects_unknown_shell(run_fj: Any) -> None:
    code, _out, err = run_fj(["completion", "fish"])
    assert code == 2
    assert "invalid choice" in err.lower() or "fish" in err


def test_main_complete_static_flags(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    code, out, err = run_fj(["__complete", "--no-llm", "--", "--ver"])
    assert code == 0, err
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert "--verbose" in lines
    assert "--version" in lines
    assert stub_agent_runtime["build_calls"] == 0


# ---------------------------------------------------------------------------
# Thread lock composition (real lock under isolated home)
# ---------------------------------------------------------------------------


def test_main_concurrent_query_same_thread_refused(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    from flowjet.cli.threads import hold_thread_lock

    with hold_thread_lock("fj-new-stub"):
        code, _out, err = run_fj(["hello"])
        assert code == 1
        assert "thread fj-new-stub" in err
    assert stub_agent_runtime["stream"] is None


def test_main_concurrent_query_different_threads_allowed(
    run_fj: Any,
    stub_agent_runtime: dict[str, Any],
) -> None:
    from flowjet.cli.threads import hold_thread_lock

    with hold_thread_lock("fj-other"):
        code, _out, err = run_fj(["hello"])
        assert code == 0, err
    assert stub_agent_runtime["stream"] is not None


# ---------------------------------------------------------------------------
# Missing-value argparse failures (process-level)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["-c"],
        ["-t"],
        ["-w"],
        ["-n"],
        ["-l", "-n"],
    ],
)
def test_main_missing_option_values_exit_2(run_fj: Any, argv: list[str]) -> None:
    code, _out, err = run_fj(argv)
    assert code == 2
    assert "expected one argument" in err or "the following arguments are required" in err
