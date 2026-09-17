"""FlowJet CLI — any characters after the command are the agent query.

Formal command: ``flowjet``. Aliases: ``fj`` (same), ``fjf`` (``-f``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from flowjet.cli import __version__
from flowjet.cli.agent import configure_cli_logging

FORMAL_CLI = "flowjet"
CLI_ALIASES = frozenset({"fj", "fjf", FORMAL_CLI})


def resolve_cli_prog(argv0: str | None = None) -> str:
    """Return the invoked command name for help / version strings.

    Known entrypoints (``flowjet``, ``fj``, ``fjf``) are preserved.
    Unknown wrappers (``python -m …``) fall back to the formal name.
    """
    name = Path(argv0 if argv0 is not None else sys.argv[0]).name
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    if name in CLI_ALIASES:
        return name
    return FORMAL_CLI


def _cli_description(prog: str) -> str:
    alias_note = {
        FORMAL_CLI: "Aliases: fj (same), fjf (same as flowjet -f).",
        "fj": "Alias of flowjet. Also: fjf (= fj -f).",
        "fjf": "Alias of flowjet -f / fj -f (follow this project's latest thread).",
    }.get(prog, "Formal command: flowjet. Aliases: fj, fjf (= -f).")
    return (
        "FlowJet — coding agent CLI (soothe-nano)\n"
        f"\n{alias_note}\n"
        "\nPass a natural-language query after options. Remaining words are joined into\n"
        "one query string (any Unicode). Use -- to force the rest of the line into the\n"
        "query when it looks like flags."
    )


def _cli_epilog(prog: str) -> str:
    # Prefer short alias in examples when the user invoked fj/fjf.
    show = prog if prog in {"fj", "fjf"} else FORMAL_CLI
    follow = show if show == "fjf" else f"{show} -f"
    return f"""\
commands:
  {show} setup                 Interactive nano.yml setup
  {show} doctor                Diagnose runtime readiness (tool deps, providers, …)
  {show} doctor-fix            Repair Chrome/chromedriver (install/download if needed)
  {show} serve                 Run the FlowJet server (ACP + OpenAI-compatible HTTP)
  {show} completion zsh|bash   Print shell completion script

query modes:
  {show} QUERY...              Start a new thread (default)
  {follow} QUERY...           Continue this project's latest active thread
  {show} -t ID QUERY...        Continue a specific thread
  {show} -t ID                 Pin thread as active (no query)
  {show} -a QUERY...           Ask mode: answer only, no tool calls
  {show} -b QUERY...           Bypass mode: skip all security enforcement

examples:
  {show} explain this repo
  {follow} what did we decide last time?
  {show} -t abc-123 continue here
  {show} -l -n 10
  {show} doctor
  {show} doctor --deep --live-llm
  eval "$({show if show != "fjf" else "fj"} completion zsh)"

notes:
  • Formal CLI: flowjet · aliases: fj, fjf (= -f)
  • -t overrides -f when both are given; -n requires -l; -l takes no query
  • -f is per project (git root, else cwd); resume elsewhere with -t ID
  • -a/--ask disables all tools — pure Q&A, no side effects
  • -b/--bypass skips all security enforcement — use with care
  • -b overrides -a when both are given
  • One query per thread at a time; different threads may run concurrently
  • With -v, prints thread <id> and the project dir on stderr before the run
"""


# Boolean short flags that may appear clustered (``-lv`` → ``-l -v``).
_BOOL_SHORTS = frozenset({"h", "V", "l", "v", "f", "a", "b"})
# Short flags that consume the next argv token as a value.
_VALUE_FLAGS = frozenset({"-c", "--config", "-t", "--thread", "-w", "--workspace", "-n"})
_FLAG_ONLY = frozenset(
    {
        "-h",
        "--help",
        "-V",
        "--version",
        "--no-stream",
        "-v",
        "--verbose",
        "-l",
        "--list",
        "-f",
        "--follow",
        "-a",
        "--ask",
        "-b",
        "--bypass",
    }
)
_EQUALS_PREFIXES = ("--config=", "--thread=", "--workspace=")


def _expand_short_cluster(tok: str) -> list[str] | None:
    """Expand ``-lv`` → ``['-l', '-v']`` when every letter is a known bool short.

    Returns ``None`` when ``tok`` is not a pure boolean short cluster (caller
    treats it as a query token or a normal flag).
    """
    if not tok.startswith("-") or tok.startswith("--") or len(tok) < 3 or "=" in tok:
        return None
    chars = tok[1:]
    if not chars.isalpha() or not all(c in _BOOL_SHORTS for c in chars):
        return None
    return [f"-{c}" for c in chars]


def split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv into (option_tokens, query_tokens).

    Options are peeled from the left. After ``--``, or the first non-option
    token, the remainder is the query (preserving spaces when rejoined).

    Boolean short flags may be clustered (``-lv`` → ``-l -v``). Clusters that
    mix unknown letters stay in the query (so ``-weird`` is still a query).
    """
    options: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return options, argv[i + 1 :]
        expanded = _expand_short_cluster(tok)
        if expanded is not None:
            options.extend(expanded)
            i += 1
            continue
        if tok in _FLAG_ONLY:
            options.append(tok)
            i += 1
            continue
        if tok in _VALUE_FLAGS:
            options.append(tok)
            if i + 1 < len(argv):
                options.append(argv[i + 1])
                i += 2
            else:
                i += 1
            continue
        if any(tok.startswith(p) for p in _EQUALS_PREFIXES):
            options.append(tok)
            i += 1
            continue
        # First non-option token starts the query (may include leading dashes).
        return options, argv[i:]
    return options, []


def validate_arg_composition(args: Any) -> str | None:
    """Return an error message when flag combinations are invalid, else ``None``.

    Rules:
    - ``-n`` requires ``-l``/``--list``
    - ``-l`` is exclusive with a query, ``-f``, ``-t``, ``-w``, ``--no-stream``, ``--ask``
    - ``-t``/``--thread`` overrides ``-f``/``--follow`` when both are given
    """
    listing = bool(getattr(args, "list", False))
    list_limit = getattr(args, "list_limit", None)
    follow = bool(getattr(args, "follow", False))
    thread = getattr(args, "thread", None)
    workspace = getattr(args, "workspace", None)
    no_stream = bool(getattr(args, "no_stream", False))
    ask = bool(getattr(args, "ask", False))
    bypass = bool(getattr(args, "bypass", False))
    query = (getattr(args, "query_text", None) or "").strip()

    if list_limit is not None and not listing:
        return "-n requires -l/--list"

    if listing:
        if query:
            return "-l/--list does not take a query (got leftover text after options)"
        if follow:
            return "-l/--list cannot be combined with -f/--follow"
        if thread:
            return "-l/--list cannot be combined with -t/--thread"
        if workspace:
            return "-l/--list cannot be combined with -w/--workspace"
        if no_stream:
            return "-l/--list cannot be combined with --no-stream"
        if ask:
            return "-l/--list cannot be combined with -a/--ask"
        if bypass:
            return "-l/--list cannot be combined with -b/--bypass"

    return None


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Preserve paragraph breaks; align option help cleanly."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=26, width=92)


def _default_config_help() -> str:
    from flowjet.cli.agent import default_config_path

    return f"Alternate nano.yml (default: {default_config_path()})"


def _build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    cli_prog = prog or resolve_cli_prog()
    parser = argparse.ArgumentParser(
        prog=cli_prog,
        description=_cli_description(cli_prog),
        epilog=_cli_epilog(cli_prog),
        formatter_class=_HelpFormatter,
        add_help=True,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"{cli_prog} {__version__}",
    )

    thread = parser.add_argument_group("thread")
    thread.add_argument(
        "-t",
        "--thread",
        metavar="ID",
        help="Thread id (-t alone: pin active; with query: continue it; overrides -f)",
    )
    thread.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Continue the latest active thread in this project directory",
    )
    thread.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="List threads, newest first (default: 20), then exit",
    )
    thread.add_argument(
        "-n",
        metavar="NUM",
        type=int,
        dest="list_limit",
        help="With -l: max threads to show (0 = all)",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Mirror tool calls and custom events on stderr",
    )
    output.add_argument(
        "--no-stream",
        action="store_true",
        help="Wait for the full answer instead of streaming tokens",
    )
    output.add_argument(
        "-a",
        "--ask",
        action="store_true",
        help="Ask mode: answer the query with no tools (pure Q&A)",
    )
    output.add_argument(
        "-b",
        "--bypass",
        action="store_true",
        help="Bypass mode: skip all security enforcement (full tools, no guardrails)",
    )

    paths = parser.add_argument_group("paths")
    paths.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help=_default_config_help(),
    )
    paths.add_argument(
        "-w",
        "--workspace",
        metavar="DIR",
        help="Workspace root for tools (default: cwd)",
    )
    return parser


def cli_help_text(prog: str | None = None) -> str:
    """Full help text (same as ``flowjet -h`` / ``fj -h``)."""
    return _build_parser(prog).format_help()


def _build_setup_parser(prog: str | None = None) -> argparse.ArgumentParser:
    cli_prog = prog or resolve_cli_prog()
    parser = argparse.ArgumentParser(
        prog=f"{cli_prog} setup",
        description="Interactive setup for nano.yml",
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help=_default_config_help(),
    )
    return parser


def _namespace_with_command(ns: argparse.Namespace, command: str) -> argparse.Namespace:
    data: dict[str, Any] = vars(ns)
    data["command"] = command
    return argparse.Namespace(**data)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args; remaining tokens become the query string."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "setup":
        setup_args = _build_setup_parser().parse_args(raw[1:])
        setup_args.query_text = ""
        return _namespace_with_command(setup_args, "setup")
    if raw and raw[0] == "doctor":
        ns = argparse.Namespace(query_text="", doctor_argv=raw[1:])
        return _namespace_with_command(ns, "doctor")
    if raw and raw[0] == "doctor-fix":
        ns = argparse.Namespace(query_text="", doctor_fix_argv=raw[1:])
        return _namespace_with_command(ns, "doctor-fix")
    if raw and raw[0] == "serve":
        ns = argparse.Namespace(query_text="", serve_argv=raw[1:])
        return _namespace_with_command(ns, "serve")
    if raw and raw[0] == "__complete":
        ns = argparse.Namespace(query_text="", complete_argv=raw[1:])
        return _namespace_with_command(ns, "__complete")
    if raw and raw[0] == "completion":
        ns = argparse.Namespace(query_text="", completion_argv=raw[1:])
        return _namespace_with_command(ns, "completion")

    option_tokens, query_tokens = split_argv(raw)
    args = _build_parser().parse_args(option_tokens)
    args.query_text = " ".join(query_tokens).strip()
    return _namespace_with_command(args, "query")


def _load_config_or_exit(args: argparse.Namespace) -> Any | int:
    from flowjet.cli.agent import load_config

    try:
        return load_config(args.config)
    except FileNotFoundError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"error: failed to load config: {exc}\n")
        return 1


async def run_list_async(args: argparse.Namespace) -> int:
    """Print persisted threads, newest first."""
    from flowjet.cli.agent import open_sqlite_checkpointer
    from flowjet.cli.threads import DEFAULT_LIST_LIMIT, list_threads, write_thread_list

    config = _load_config_or_exit(args)
    if isinstance(config, int):
        return config

    limit = getattr(args, "list_limit", None)
    if limit is None:
        limit = DEFAULT_LIST_LIMIT
    elif limit < 0:
        sys.stderr.write("error: -n must be >= 0 (0 = list all)\n")
        return 2

    try:
        async with open_sqlite_checkpointer(config) as checkpointer:
            threads = await list_threads(checkpointer, limit=limit)
            write_thread_list(threads, sys.stdout)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130
    except Exception as exc:
        from flowjet.cli.stream import write_cli_error

        write_cli_error(exc, verbose=getattr(args, "verbose", False))
        return 1
    return 0


def run_pin_thread(thread_id: str) -> int:
    """Pin ``thread_id`` as active and print it (no agent run)."""
    from flowjet.cli.threads import write_active_thread_id

    tid = thread_id.strip()
    if not tid:
        sys.stderr.write("error: -t/--thread requires a non-empty id\n")
        return 2
    write_active_thread_id(tid)
    sys.stdout.write(f"{tid}\n")
    return 0


async def run_async(args: argparse.Namespace) -> int:
    conflict = validate_arg_composition(args)
    if conflict:
        sys.stderr.write(f"error: {conflict}\n")
        return 2

    if getattr(args, "list", False):
        return await run_list_async(args)

    if args.thread and not args.query_text:
        return run_pin_thread(args.thread)

    if not args.query_text:
        sys.stderr.write(cli_help_text())
        return 2

    # Lazy: keep ``fj __complete`` / setup free of agent import cost.
    from flowjet.cli.agent import build_agent, open_sqlite_checkpointer
    from flowjet.cli.stream import invoke_query, stream_query
    from flowjet.cli.threads import (
        ConcurrentSessionError,
        hold_thread_lock,
        resolve_thread_id,
        resolve_workdir,
    )

    config = _load_config_or_exit(args)
    if isinstance(config, int):
        return config

    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None

    try:
        async with open_sqlite_checkpointer(config) as checkpointer:
            thread_id = await resolve_thread_id(
                checkpointer,
                explicit=args.thread,
                follow=getattr(args, "follow", False),
            )
            with hold_thread_lock(thread_id):
                if args.verbose:
                    sys.stderr.write(f"thread {thread_id} · {resolve_workdir()}\n")
                    sys.stderr.flush()

                agent = await build_agent(
                    config,
                    workspace=workspace,
                    checkpointer=checkpointer,
                    verbose=args.verbose,
                    ask_mode=getattr(args, "ask", False),
                    bypass_mode=getattr(args, "bypass", False),
                )
                if args.no_stream:
                    await invoke_query(agent, args.query_text, thread_id=thread_id)
                else:
                    await stream_query(
                        agent,
                        args.query_text,
                        thread_id=thread_id,
                        show_tool_calls=args.verbose,
                    )
    except ConcurrentSessionError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130
    except Exception as exc:
        from flowjet.cli.stream import write_cli_error

        write_cli_error(exc, verbose=args.verbose)
        return 1

    try:
        from flowjet.cli.completion.context import append_history

        append_history(args.query_text)
    except Exception:
        pass
    return 0


# Seconds a stray background task gets to honour cancellation before the loop is
# closed anyway.
_TEARDOWN_GRACE_S = 3.0


async def _cancel_stray_tasks(loop: asyncio.AbstractEventLoop, grace: float) -> None:
    """Cancel every task but this one, and close async generators, within ``grace``.

    Runtimes reached through tools may ignore cancellation: ``bubus`` event buses
    behind ``browser_use`` catch ``CancelledError`` and re-arm a 0.1s poll loop,
    so waiting on them without a deadline never returns.
    """
    stray = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
    if stray:
        for task in stray:
            task.cancel()
        await asyncio.wait(stray, timeout=grace)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(loop.shutdown_asyncgens(), timeout=grace)


def run_one_shot(coro: Coroutine[Any, Any, int], *, grace: float = _TEARDOWN_GRACE_S) -> int:
    """Run ``coro`` on a fresh loop, then tear that loop down with a deadline.

    ``asyncio.run`` cancels leftover tasks and gathers them with no timeout, so
    one task that swallows ``CancelledError`` pins the process after the answer
    has already been printed. ``loop.close()`` is also where libraries install
    their own cleanup hooks, so it runs whether or not the strays went quietly.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        with contextlib.suppress(Exception):
            loop.run_until_complete(_cancel_stray_tasks(loop, grace))
        asyncio.set_event_loop(None)
        loop.close()


_FOLLOW_SUBCOMMANDS = frozenset(
    {"setup", "doctor", "doctor-fix", "serve", "completion", "__complete"}
)


def _inject_follow(argv: list[str]) -> list[str]:
    """Prepend ``--follow`` unless argv already sets it or is a subcommand."""
    if argv and argv[0] in _FOLLOW_SUBCOMMANDS:
        return argv
    if "-f" in argv or "--follow" in argv:
        return argv
    return ["--follow", *argv]


def main_follow(argv: list[str] | None = None) -> int:
    """``fjf`` entry point — alias of ``flowjet -f`` / ``fj -f``."""
    raw = list(sys.argv[1:] if argv is None else argv)
    return main(_inject_follow(raw))


def main(argv: list[str] | None = None) -> int:
    try:
        configure_cli_logging()
        args = parse_args(argv)
        # Re-apply after parse so ``-v`` can enable compact console warnings.
        if getattr(args, "verbose", False):
            configure_cli_logging(verbose=True)
        # Keep interactive setup / doctor / completion off the event loop so Ctrl+C exits cleanly.
        if args.command == "setup":
            from flowjet.cli.setup_cmd import run_setup

            return run_setup(getattr(args, "config", None))
        if args.command == "doctor":
            from flowjet.cli.doctor_cmd import run_doctor

            return run_doctor(getattr(args, "doctor_argv", []))
        if args.command == "doctor-fix":
            from flowjet.cli.doctor_fix_cmd import run_doctor_fix

            return run_doctor_fix(getattr(args, "doctor_fix_argv", []))
        if args.command == "__complete":
            from flowjet.cli.completion import run_complete

            return run_complete(getattr(args, "complete_argv", []))
        if args.command == "serve":
            # Lazy: the server extra (fastapi/uvicorn/acp) must never be an
            # import-time dependency of the CLI.
            from flowjet.cli.serve_cmd import run_serve

            return run_serve(getattr(args, "serve_argv", []))
        if args.command == "completion":
            from flowjet.cli.completion import run_completion_script

            return run_completion_script(getattr(args, "completion_argv", []))
        return run_one_shot(run_async(args))
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
