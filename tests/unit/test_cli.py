"""Unit tests for fj CLI argument parsing."""

from __future__ import annotations

import pytest

from flowjet.cli.cli import main_follow, parse_args, split_argv


@pytest.mark.parametrize(
    ("argv", "options", "query"),
    [
        (["who", "is", "your", "name"], [], ["who", "is", "your", "name"]),
        (["café résumé"], [], ["café résumé"]),
        (["-v", "hello"], ["-v"], ["hello"]),
        (["--verbose", "hello", "world"], ["--verbose"], ["hello", "world"]),
        (["--", "-weird", "flag"], [], ["-weird", "flag"]),
        (["-c", "/tmp/nano.yml", "hi"], ["-c", "/tmp/nano.yml"], ["hi"]),
        (["--config=/tmp/x.yml", "q"], ["--config=/tmp/x.yml"], ["q"]),
        (["--no-stream", "ask"], ["--no-stream"], ["ask"]),
        (["-l"], ["-l"], []),
        (["--list"], ["--list"], []),
        (["-l", "-n", "5"], ["-l", "-n", "5"], []),
        (["-f", "continue", "please"], ["-f"], ["continue", "please"]),
        (["-lv"], ["-l", "-v"], []),
        (["-vl", "hello"], ["-v", "-l"], ["hello"]),
        (["-weird"], [], ["-weird"]),
        ([], [], []),
    ],
)
def test_split_argv(argv: list[str], options: list[str], query: list[str]) -> None:
    got_opts, got_query = split_argv(argv)
    assert got_opts == options
    assert got_query == query


def test_parse_args_joins_unicode_query() -> None:
    args = parse_args(["café", "résumé"])
    assert args.query_text == "café résumé"
    assert args.verbose is False


def test_parse_args_options() -> None:
    args = parse_args(["-v", "--thread", "t1", "-w", "/tmp", "do", "stuff"])
    assert args.verbose is True
    assert args.thread == "t1"
    assert args.workspace == "/tmp"
    assert args.query_text == "do stuff"


def test_parse_args_list_flag() -> None:
    args = parse_args(["-l"])
    assert args.list is True
    assert args.list_limit is None
    assert args.query_text == ""
    assert args.command == "query"


def test_parse_args_list_limit() -> None:
    args = parse_args(["-l", "-n", "5"])
    assert args.list is True
    assert args.list_limit == 5


def test_parse_args_follow_flag() -> None:
    args = parse_args(["-f", "continue", "please"])
    assert args.follow is True
    assert args.query_text == "continue please"


def test_parse_args_ask_flag_short() -> None:
    args = parse_args(["-a", "what", "is", "2+2"])
    assert args.ask is True
    assert args.query_text == "what is 2+2"


def test_parse_args_ask_flag_long() -> None:
    args = parse_args(["--ask", "explain recursion"])
    assert args.ask is True
    assert args.query_text == "explain recursion"


def test_parse_args_ask_with_follow() -> None:
    args = parse_args(["-a", "-f", "continue here"])
    assert args.ask is True
    assert args.follow is True
    assert args.query_text == "continue here"


def test_parse_args_bypass_flag_short() -> None:
    args = parse_args(["-b", "run", "anything"])
    assert args.bypass is True
    assert args.query_text == "run anything"


def test_parse_args_bypass_flag_long() -> None:
    args = parse_args(["--bypass", "explain the repo"])
    assert args.bypass is True
    assert args.query_text == "explain the repo"


def test_parse_args_bypass_overrides_ask() -> None:
    args = parse_args(["-a", "-b", "do stuff"])
    assert args.ask is True
    assert args.bypass is True
    assert args.query_text == "do stuff"


def test_split_argv_bypass_cluster() -> None:
    # ``-bv`` → ``-b -v`` (both boolean shorts)
    opts, query = split_argv(["-bv", "hi"])
    assert opts == ["-b", "-v"]
    assert query == ["hi"]


def test_split_argv_ask_cluster() -> None:
    # ``-av`` → ``-a -v`` (both boolean shorts)
    opts, query = split_argv(["-av", "hi"])
    assert opts == ["-a", "-v"]
    assert query == ["hi"]


def test_validate_arg_composition_ask_with_list_rejected() -> None:
    from argparse import Namespace

    from flowjet.cli.cli import validate_arg_composition

    ns = Namespace(
        list=True,
        list_limit=None,
        follow=False,
        thread=None,
        workspace=None,
        no_stream=False,
        ask=True,
        bypass=False,
        query_text="",
    )
    err = validate_arg_composition(ns)
    assert err is not None
    assert "-a/--ask" in err


def test_validate_arg_composition_bypass_with_list_rejected() -> None:
    from argparse import Namespace

    from flowjet.cli.cli import validate_arg_composition

    ns = Namespace(
        list=True,
        list_limit=None,
        follow=False,
        thread=None,
        workspace=None,
        no_stream=False,
        ask=False,
        bypass=True,
        query_text="",
    )
    err = validate_arg_composition(ns)
    assert err is not None
    assert "-b/--bypass" in err


def test_resolve_cli_prog_known_entrypoints() -> None:
    from flowjet.cli.cli import FORMAL_CLI, resolve_cli_prog

    assert resolve_cli_prog("/usr/local/bin/flowjet") == "flowjet"
    assert resolve_cli_prog("/usr/local/bin/fj") == "fj"
    assert resolve_cli_prog("fjf") == "fjf"
    assert resolve_cli_prog("fj.exe") == "fj"
    assert resolve_cli_prog("python") == FORMAL_CLI
    assert resolve_cli_prog("__main__.py") == FORMAL_CLI


def test_help_mentions_formal_cli_and_aliases() -> None:
    from flowjet.cli.cli import cli_help_text

    help_text = cli_help_text("flowjet")
    assert "flowjet" in help_text
    assert "Aliases: fj" in help_text
    assert "fjf" in help_text


def test_main_follow_injects_follow_flag(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import cli

    seen: list[list[str] | None] = []

    def fake_main(argv: list[str] | None = None) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    assert main_follow(["continue", "please"]) == 0
    assert seen == [["--follow", "continue", "please"]]


def test_main_follow_skips_subcommands(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import cli

    seen: list[list[str] | None] = []

    def fake_main(argv: list[str] | None = None) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    assert main_follow(["setup"]) == 0
    assert main_follow(["doctor", "--deep"]) == 0
    assert main_follow(["completion", "zsh"]) == 0
    assert seen == [["setup"], ["doctor", "--deep"], ["completion", "zsh"]]


def test_main_follow_idempotent_when_follow_present(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import cli

    seen: list[list[str] | None] = []

    def fake_main(argv: list[str] | None = None) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    assert main_follow(["-f", "continue"]) == 0
    assert seen == [["-f", "continue"]]


@pytest.mark.asyncio
async def test_run_async_list_threads(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import flowjet.cli.agent as agent_mod
    import flowjet.cli.agent as config_mod
    import flowjet.cli.threads as threads_mod
    from flowjet.cli.cli import parse_args, run_async
    from flowjet.cli.threads import ThreadInfo

    seen: dict[str, int] = {}

    @asynccontextmanager
    async def fake_cp(_config: object):
        yield object()

    async def fake_list(_cp: object, *, limit: int = 20):
        seen["limit"] = limit
        return [
            ThreadInfo("fj-new", "2026-07-21 12:00:00", "latest question"),
            ThreadInfo("fj-old", "2026-07-20 12:00:00", "older question"),
        ]

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", fake_cp)
    monkeypatch.setattr(threads_mod, "list_threads", fake_list)

    assert await run_async(parse_args(["-l"])) == 0
    out = capsys.readouterr().out
    assert out.index("fj-new") < out.index("fj-old")
    assert "2026-07-21 12:00:00" in out
    assert "latest question" in out
    assert seen["limit"] == 20

    assert await run_async(parse_args(["-l", "-n", "3"])) == 0
    assert seen["limit"] == 3


@pytest.mark.asyncio
async def test_run_async_list_invalid_limit(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    import flowjet.cli.agent as config_mod
    from flowjet.cli.cli import parse_args, run_async

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    assert await run_async(parse_args(["-l", "-n", "-1"])) == 2
    assert "-n must be >= 0" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_async_list_zero_means_all(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import flowjet.cli.agent as agent_mod
    import flowjet.cli.agent as config_mod
    import flowjet.cli.threads as threads_mod
    from flowjet.cli.cli import parse_args, run_async

    seen: dict[str, int] = {}

    @asynccontextmanager
    async def fake_cp(_config: object):
        yield object()

    async def fake_list(_cp: object, *, limit: int = 20):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", fake_cp)
    monkeypatch.setattr(threads_mod, "list_threads", fake_list)
    monkeypatch.setattr(threads_mod, "write_thread_list", lambda *a, **k: None)

    assert await run_async(parse_args(["-l", "-n", "0"])) == 0
    assert seen["limit"] == 0


@pytest.mark.asyncio
async def test_arg_composition_conflicts(capsys) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.cli import parse_args, run_async

    cases = [
        (["-n", "5"], "-n requires -l/--list"),
        (["-n", "5", "hello"], "-n requires -l/--list"),
        (["-l", "hello"], "-l/--list does not take a query"),
        (["-l", "-t", "fj-x"], "-l/--list cannot be combined with -t/--thread"),
        (["-l", "-w", "/tmp"], "-l/--list cannot be combined with -w/--workspace"),
        (["-l", "--no-stream"], "-l/--list cannot be combined with --no-stream"),
    ]
    for argv, needle in cases:
        assert await run_async(parse_args(argv)) == 2
        err = capsys.readouterr().err
        assert needle in err, (argv, err)


def test_run_pin_thread(monkeypatch, capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    import flowjet.cli.threads as threads_mod
    from flowjet.cli.cli import parse_args, run_async, run_pin_thread

    path = tmp_path / "fj_active_thread"
    monkeypatch.setattr(threads_mod, "active_thread_path", lambda *_a, **_k: path)

    assert run_pin_thread("fj-pinned") == 0
    assert capsys.readouterr().out.strip() == "fj-pinned"
    assert path.read_text(encoding="utf-8").strip() == "fj-pinned"

    assert asyncio.run(run_async(parse_args(["-t", "fj-from-cli"]))) == 0
    assert capsys.readouterr().out.strip() == "fj-from-cli"
    assert path.read_text(encoding="utf-8").strip() == "fj-from-cli"


def test_parse_args_clustered_shorts() -> None:
    args = parse_args(["-lv"])
    assert args.list is True
    assert args.verbose is True
    assert args.query_text == ""


@pytest.mark.asyncio
async def test_run_async_default_starts_new_thread(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import flowjet.cli.agent as agent_mod
    import flowjet.cli.agent as config_mod
    import flowjet.cli.stream as stream_mod
    import flowjet.cli.threads as threads_mod
    from flowjet.cli.cli import parse_args, run_async

    seen: dict[str, object] = {}

    @asynccontextmanager
    async def fake_cp(_config: object):
        yield object()

    async def fake_resolve(
        _cp: object,
        *,
        explicit: str | None = None,
        follow: bool = False,
    ) -> str:
        seen["explicit"] = explicit
        seen["follow"] = follow
        return "fj-new"

    async def fake_stream(_agent: object, query: str, *, thread_id: str, **_k: object) -> str:
        seen["query"] = query
        seen["thread_id"] = thread_id
        return "ok"

    async def fake_build(*_a: object, **_k: object) -> object:
        return object()

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", fake_cp)
    monkeypatch.setattr(agent_mod, "build_agent", fake_build)
    monkeypatch.setattr(threads_mod, "resolve_thread_id", fake_resolve)
    monkeypatch.setattr(stream_mod, "stream_query", fake_stream)

    assert await run_async(parse_args(["continue", "please"])) == 0
    assert seen == {
        "explicit": None,
        "follow": False,
        "query": "continue please",
        "thread_id": "fj-new",
    }


@pytest.mark.asyncio
async def test_run_async_follow_uses_latest_thread(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import flowjet.cli.agent as agent_mod
    import flowjet.cli.agent as config_mod
    import flowjet.cli.stream as stream_mod
    import flowjet.cli.threads as threads_mod
    from flowjet.cli.cli import parse_args, run_async

    seen: dict[str, object] = {}

    @asynccontextmanager
    async def fake_cp(_config: object):
        yield object()

    async def fake_resolve(
        _cp: object,
        *,
        explicit: str | None = None,
        follow: bool = False,
    ) -> str:
        seen["follow"] = follow
        return "fj-active"

    async def fake_stream(_agent: object, query: str, *, thread_id: str, **_k: object) -> str:
        seen["thread_id"] = thread_id
        return "ok"

    async def fake_build(*_a: object, **_k: object) -> object:
        return object()

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", fake_cp)
    monkeypatch.setattr(agent_mod, "build_agent", fake_build)
    monkeypatch.setattr(threads_mod, "resolve_thread_id", fake_resolve)
    monkeypatch.setattr(stream_mod, "stream_query", fake_stream)

    assert await run_async(parse_args(["-f", "continue"])) == 0
    assert seen["follow"] is True
    assert seen["thread_id"] == "fj-active"


@pytest.mark.asyncio
async def test_run_async_thread_overrides_follow(monkeypatch) -> None:  # type: ignore[untyped-def]
    """``-t`` overrides ``-f`` — both may be given together; explicit id wins."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import flowjet.cli.agent as agent_mod
    import flowjet.cli.agent as config_mod
    import flowjet.cli.stream as stream_mod
    import flowjet.cli.threads as threads_mod
    from flowjet.cli.cli import parse_args, run_async

    seen: dict[str, object] = {}

    @asynccontextmanager
    async def fake_cp(_config: object):
        yield object()

    async def fake_resolve(
        _cp: object,
        *,
        explicit: str | None = None,
        follow: bool = False,
    ) -> str:
        seen["explicit"] = explicit
        seen["follow"] = follow
        return explicit.strip() if explicit else "fj-active"

    async def fake_stream(_agent: object, query: str, *, thread_id: str, **_k: object) -> str:
        seen["query"] = query
        seen["thread_id"] = thread_id
        return "ok"

    async def fake_build(*_a: object, **_k: object) -> object:
        return object()

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", fake_cp)
    monkeypatch.setattr(agent_mod, "build_agent", fake_build)
    monkeypatch.setattr(threads_mod, "resolve_thread_id", fake_resolve)
    monkeypatch.setattr(stream_mod, "stream_query", fake_stream)

    assert await run_async(parse_args(["-f", "-t", "fj-explicit", "continue"])) == 0
    assert seen["explicit"] == "fj-explicit"
    assert seen["follow"] is True
    assert seen["thread_id"] == "fj-explicit"
    assert seen["query"] == "continue"


def test_parse_args_empty_query() -> None:
    args = parse_args(["-v"])
    assert args.query_text == ""


def test_parse_args_setup_command() -> None:
    args = parse_args(["setup"])
    assert args.command == "setup"
    assert args.query_text == ""


def test_main_setup_skips_asyncio(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import flowjet.cli.setup_cmd as setup_cmd
    from flowjet.cli import cli

    called: list[str] = []

    monkeypatch.setattr(cli, "configure_cli_logging", lambda: None)
    monkeypatch.setattr(setup_cmd, "run_setup", lambda _path: called.append("setup") or 0)

    def boom(*_a: object, **_k: object) -> int:
        raise AssertionError("setup must not use asyncio.run")

    monkeypatch.setattr(cli.asyncio, "run", boom)
    assert cli.main(["setup"]) == 0
    assert called == ["setup"]


def test_main_keyboard_interrupt_is_clean(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import cli

    def raise_ki(_argv: list[str] | None = None) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "configure_cli_logging", lambda: None)
    monkeypatch.setattr(cli, "parse_args", raise_ki)
    assert cli.main([]) == 130
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_async_empty_query_prints_usage(capsys) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.cli import parse_args, run_async

    assert await run_async(parse_args(["-v"])) == 2
    assert "FlowJet — coding agent CLI" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_async_missing_config(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import flowjet.cli.agent as config_mod
    from flowjet.cli.cli import parse_args, run_async

    def missing(_path: object = None) -> object:
        raise FileNotFoundError("no config")

    monkeypatch.setattr(config_mod, "load_config", missing)
    code = await run_async(parse_args(["hello"]))
    assert code == 1
    assert "error: no config" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_async_config_load_failure(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import flowjet.cli.agent as config_mod
    from flowjet.cli.cli import parse_args, run_async

    monkeypatch.setattr(
        config_mod, "load_config", lambda _p=None: (_ for _ in ()).throw(ValueError("bad yaml"))
    )
    assert await run_async(parse_args(["hello"])) == 1
    assert "failed to load config" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_async_query_success_and_history(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import flowjet.cli.agent as agent_mod
    import flowjet.cli.agent as config_mod
    import flowjet.cli.completion.context as history_mod
    import flowjet.cli.stream as stream_mod
    from flowjet.cli.cli import parse_args, run_async

    calls: list[str] = []

    @asynccontextmanager
    async def fake_cp(_config: object):
        yield object()

    async def fake_build(*_a: object, **_k: object) -> object:
        return object()

    async def fake_stream(*_a: object, **_k: object) -> str:
        calls.append("stream")
        return "ok"

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", fake_cp)
    monkeypatch.setattr(agent_mod, "build_agent", fake_build)
    monkeypatch.setattr(stream_mod, "stream_query", fake_stream)
    monkeypatch.setattr(history_mod, "append_history", lambda q: calls.append(f"hist:{q}"))

    async def fake_resolve(*_a: object, **_k: object) -> str:
        return "fj-test"

    import flowjet.cli.threads as threads_mod

    monkeypatch.setattr(threads_mod, "resolve_thread_id", fake_resolve)

    assert await run_async(parse_args(["do", "stuff"])) == 0
    assert calls == ["stream", "hist:do stuff"]


@pytest.mark.asyncio
async def test_run_async_no_stream_and_error(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import flowjet.cli.agent as agent_mod
    import flowjet.cli.agent as config_mod
    import flowjet.cli.stream as stream_mod
    from flowjet.cli.cli import parse_args, run_async

    @asynccontextmanager
    async def fake_cp(_config: object):
        yield object()

    async def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", fake_cp)

    async def fake_build(*_a: object, **_k: object) -> object:
        return object()

    async def fake_resolve(*_a: object, **_k: object) -> str:
        return "fj-test"

    import flowjet.cli.threads as threads_mod

    monkeypatch.setattr(agent_mod, "build_agent", fake_build)
    monkeypatch.setattr(threads_mod, "resolve_thread_id", fake_resolve)
    monkeypatch.setattr(stream_mod, "invoke_query", boom)

    args = parse_args(["--no-stream", "ask"])
    assert await run_async(args) == 1
    assert "provider down" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_async_keyboard_interrupt(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import flowjet.cli.agent as agent_mod
    import flowjet.cli.agent as config_mod
    import flowjet.cli.stream as stream_mod
    from flowjet.cli.cli import parse_args, run_async

    @asynccontextmanager
    async def fake_cp(_config: object):
        yield object()

    async def raise_ki(*_a: object, **_k: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(config_mod, "load_config", lambda _p=None: SimpleNamespace())
    monkeypatch.setattr(agent_mod, "open_sqlite_checkpointer", fake_cp)

    async def fake_build(*_a: object, **_k: object) -> object:
        return object()

    async def fake_resolve(*_a: object, **_k: object) -> str:
        return "fj-test"

    import flowjet.cli.threads as threads_mod

    monkeypatch.setattr(agent_mod, "build_agent", fake_build)
    monkeypatch.setattr(threads_mod, "resolve_thread_id", fake_resolve)
    monkeypatch.setattr(stream_mod, "stream_query", raise_ki)

    assert await run_async(parse_args(["q"])) == 130
    assert "interrupted" in capsys.readouterr().err


def test_main_verbose_reconfigures_logging(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import cli

    seen: list[bool] = []

    def fake_logging(*, verbose: bool = False) -> None:
        seen.append(verbose)

    def fake_run(coro: object) -> int:
        # Close the coroutine to avoid "never awaited" warnings.
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[union-attr]
        return 0

    monkeypatch.setattr(cli, "configure_cli_logging", fake_logging)
    monkeypatch.setattr(cli, "run_one_shot", fake_run)
    assert cli.main(["-v", "hi"]) == 0
    assert False in seen and True in seen


# ---------------------------------------------------------------------------
# _detect_pending_interrupt — checks the graph snapshot for ask_user pauses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_pending_interrupt_returns_ask_user_value() -> None:
    from flowjet.cli.cli import _detect_pending_interrupt

    class _Interrupt:
        def __init__(self, value: object) -> None:
            self.value = value

    class _Snapshot:
        interrupts = (_Interrupt({"type": "ask_user", "questions": [{"header": "H"}]}),)

    class _Agent:
        async def aget_state(self, _config: object) -> object:
            return _Snapshot()

    result = await _detect_pending_interrupt(_Agent(), "t-1")
    assert result == {"type": "ask_user", "questions": [{"header": "H"}]}


@pytest.mark.asyncio
async def test_detect_pending_interrupt_skips_non_ask_user() -> None:
    from flowjet.cli.cli import _detect_pending_interrupt

    class _Interrupt:
        def __init__(self, value: object) -> None:
            self.value = value

    class _Snapshot:
        interrupts = (_Interrupt({"action_requests": []}),)

    class _Agent:
        async def aget_state(self, _config: object) -> object:
            return _Snapshot()

    result = await _detect_pending_interrupt(_Agent(), "t-2")
    assert result is None


@pytest.mark.asyncio
async def test_detect_pending_interrupt_none_when_no_interrupts() -> None:
    from flowjet.cli.cli import _detect_pending_interrupt

    class _Snapshot:
        interrupts = ()

    class _Agent:
        async def aget_state(self, _config: object) -> object:
            return _Snapshot()

    result = await _detect_pending_interrupt(_Agent(), "t-3")
    assert result is None


@pytest.mark.asyncio
async def test_detect_pending_interrupt_none_when_no_checkpointer() -> None:
    from flowjet.cli.cli import _detect_pending_interrupt

    class _Agent:
        pass

    result = await _detect_pending_interrupt(_Agent(), "t-4")
    assert result is None


@pytest.mark.asyncio
async def test_detect_pending_interrupt_soft_fails_on_error() -> None:
    from flowjet.cli.cli import _detect_pending_interrupt

    class _Agent:
        async def aget_state(self, _config: object) -> object:
            raise RuntimeError("boom")

    result = await _detect_pending_interrupt(_Agent(), "t-5")
    assert result is None
