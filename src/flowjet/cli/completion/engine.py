"""Completion engine: static/LLM providers, merge, and shell entrypoints."""

from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any

from flowjet.cli.completion.context import (
    DEFAULT_TASKS,
    SUBCOMMANDS,
    CompletionContext,
    build_context,
)

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 8
DEFAULT_LLM_TIMEOUT_S = 0.25

FLAGS = (
    "-h",
    "--help",
    "-V",
    "--version",
    "-c",
    "--config",
    "-t",
    "--thread",
    "-l",
    "--list",
    "-n",
    "-f",
    "--follow",
    "-w",
    "--workspace",
    "--no-stream",
    "-v",
    "--verbose",
)

COMPLETION_SHELLS = ("zsh", "bash")

SYSTEM_PROMPT = """\
You are an autocomplete engine for the FlowJet (fj) coding-agent CLI.
Predict the user's intended natural-language query.

Rules:
- Return exactly 5 candidates, one per line.
- Each candidate is a full query continuation (not a shell command).
- Do not explain.
- Do not execute.
- Do not include markdown, bullets, or numbering.
- Do not prefix with "fj".
- Prefer repository-aware suggestions.
- Each line must be independently usable after: fj <candidate>
"""

_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_FJ_PREFIX_RE = re.compile(r"^\s*fj\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Static / traditional completions
# ---------------------------------------------------------------------------


def is_static_prefix(prefix: str) -> bool:
    """True when the incomplete token looks like a flag or known subcommand."""
    text = prefix.strip()
    if not text:
        return False
    if text.startswith("-"):
        return True
    first = text.split()[0]
    return any(cmd.startswith(first) for cmd in SUBCOMMANDS)


def static_candidates(prefix: str) -> list[str]:
    """Return flag/subcommand completions matching ``prefix``."""
    text = prefix.strip()
    if not text:
        return []

    if text.startswith("-"):
        return [flag for flag in FLAGS if flag.startswith(text)]

    parts = text.split()
    first = parts[0]
    if first == "completion" or "completion".startswith(first):
        if first == "completion" and len(parts) >= 2:
            shell_prefix = parts[1]
            return [s for s in COMPLETION_SHELLS if s.startswith(shell_prefix)]
        if first == "completion":
            return list(COMPLETION_SHELLS)
        return ["completion"] if "completion".startswith(first) else []

    if first == "doctor" or "doctor".startswith(first):
        doctor_flags = (
            "--deep",
            "--live-llm",
            "--format",
            "--no-color",
            "--fail-on",
            "-c",
            "--config",
        )
        if first == "doctor" and len(parts) >= 2:
            flag_prefix = parts[-1]
            if flag_prefix.startswith("-"):
                return [f for f in doctor_flags if f.startswith(flag_prefix)]
            return []
        if first == "doctor":
            return list(doctor_flags)
        return ["doctor"] if "doctor".startswith(first) else []

    if first == "doctor-fix" or "doctor-fix".startswith(first):
        doctor_fix_flags = ("--no-color",)
        if first == "doctor-fix" and len(parts) >= 2:
            flag_prefix = parts[-1]
            if flag_prefix.startswith("-"):
                return [f for f in doctor_fix_flags if f.startswith(flag_prefix)]
            return []
        if first == "doctor-fix":
            return list(doctor_fix_flags)
        return ["doctor-fix"] if "doctor-fix".startswith(first) else []

    if first == "serve" or "serve".startswith(first):
        serve_flags = ("--host", "-p", "--port", "--api-key", "--nano-config")
        if first == "serve" and len(parts) >= 2:
            flag_prefix = parts[-1]
            if flag_prefix.startswith("-"):
                return [f for f in serve_flags if f.startswith(flag_prefix)]
            return []
        if first == "serve":
            return list(serve_flags)
        return ["serve"] if "serve".startswith(first) else []

    return [cmd for cmd in SUBCOMMANDS if cmd.startswith(first)]


# ---------------------------------------------------------------------------
# LLM intent provider
# ---------------------------------------------------------------------------


def build_user_prompt(ctx: CompletionContext) -> str:
    """Compact user prompt from slim context."""
    lines = [
        f"cwd: {ctx.cwd}",
    ]
    if ctx.project_root:
        lines.append(f"project_root: {ctx.project_root.name} ({ctx.project_root})")
    if ctx.language:
        lines.append(f"language: {ctx.language}")
    if ctx.project_type:
        lines.append(f"project_type: {ctx.project_type}")
    if ctx.git_repo:
        lines.append(f"git_branch: {ctx.git_branch or '?'}")
        if ctx.staged_names:
            lines.append("staged: " + ", ".join(ctx.staged_names))
        if ctx.modified_names:
            lines.append("modified: " + ", ".join(ctx.modified_names))
    if ctx.recent_history:
        lines.append("recent_history:")
        for item in ctx.recent_history[:8]:
            lines.append(f"- {item}")
    lines.append(f"current_input: {ctx.query_prefix or '(empty)'}")
    if not ctx.query_prefix:
        lines.append("mode: suggest useful next tasks for this workspace")
    else:
        lines.append("mode: complete or refine the current_input prefix")
    return "\n".join(lines)


def parse_candidates(text: str, *, limit: int = 5) -> list[str]:
    """Parse model output into clean candidate strings."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _BULLET_RE.sub("", line).strip()
        line = _FJ_PREFIX_RE.sub("", line).strip()
        line = line.strip("`\"'")
        if not line:
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= limit:
            break
    return out


async def llm_candidates(
    ctx: CompletionContext,
    config: Any,
    *,
    limit: int = 5,
) -> list[str]:
    """Call the configured fast-role chat model; never boots an agent."""
    try:
        model = config.create_chat_model("fast")
    except Exception:
        logger.debug("create_chat_model(fast) failed", exc_info=True)
        return []

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(ctx)},
    ]
    try:
        result = await model.ainvoke(messages)
    except Exception:
        logger.debug("fast model ainvoke failed", exc_info=True)
        return []

    content = getattr(result, "content", result)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        content = "".join(parts)
    return parse_candidates(str(content), limit=limit)


# ---------------------------------------------------------------------------
# Merge / complete
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    text: str
    provider: str
    score: float = 0.0


def history_candidates(ctx: CompletionContext, *, limit: int = 8) -> list[Candidate]:
    """Filter recent history by query prefix."""
    prefix = ctx.query_prefix.casefold()
    out: list[Candidate] = []
    for i, item in enumerate(ctx.recent_history):
        folded = item.casefold()
        if prefix:
            if folded.startswith(prefix):
                pass
            elif len(prefix) >= 3 and prefix in folded:
                pass
            else:
                continue
        score = 0.95 - i * 0.01
        if prefix and folded.startswith(prefix):
            score += 0.05
        out.append(Candidate(text=item, provider="history", score=score))
        if len(out) >= limit:
            break
    return out


def merge_candidates(
    groups: list[list[Candidate]],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[str]:
    """Dedupe case-insensitively; preserve group priority order then score."""
    ranked: list[Candidate] = []
    for group in groups:
        ranked.extend(group)
    ranked.sort(key=lambda c: c.score, reverse=True)

    seen: set[str] = set()
    out: list[str] = []
    for cand in ranked:
        key = cand.text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cand.text)
        if len(out) >= top_k:
            break
    return out


async def complete_async(
    words: list[str],
    *,
    config: Any | None = None,
    cwd: Any | None = None,
    top_k: int = DEFAULT_TOP_K,
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S,
    use_llm: bool = True,
) -> list[str]:
    """Return top-k completion strings for shell display."""
    from pathlib import Path

    ctx = build_context(words, cwd=Path(cwd) if cwd else None)

    if ctx.mode == "static":
        token = ctx.query_prefix
        if ctx.words and ctx.words[-1].startswith("-"):
            token = ctx.words[-1]
        static = [Candidate(text=s, provider="static", score=0.7) for s in static_candidates(token)]
        return merge_candidates([static], top_k=top_k)

    hist = history_candidates(ctx)
    builtins = [
        Candidate(text=t, provider="builtin", score=0.4)
        for t in DEFAULT_TASKS
        if not ctx.query_prefix or t.casefold().startswith(ctx.query_prefix.casefold())
    ]

    llm: list[Candidate] = []
    if use_llm and config is not None:
        try:
            texts = await asyncio.wait_for(
                llm_candidates(ctx, config, limit=5),
                timeout=llm_timeout_s,
            )
            llm = [
                Candidate(text=t, provider="llm", score=0.82 - i * 0.01)
                for i, t in enumerate(texts)
            ]
        except TimeoutError:
            logger.debug("LLM completion timed out after %.0fms", llm_timeout_s * 1000)
        except Exception:
            logger.debug("LLM completion failed", exc_info=True)

    # Priority: history → llm → builtins (merge_candidates also sorts by score).
    return merge_candidates([hist, llm, builtins], top_k=top_k)


def complete(
    words: list[str],
    *,
    config: Any | None = None,
    cwd: Any | None = None,
    top_k: int = DEFAULT_TOP_K,
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S,
    use_llm: bool = True,
) -> list[str]:
    """Sync wrapper for shell entrypoints."""

    def _run() -> list[str]:
        return asyncio.run(
            complete_async(
                words,
                config=config,
                cwd=cwd,
                top_k=top_k,
                llm_timeout_s=llm_timeout_s,
                use_llm=use_llm,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()

    # Already inside an event loop (e.g. some test runners): use a worker thread.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


# ---------------------------------------------------------------------------
# Shell entrypoints: ``fj __complete`` / ``fj completion``
# ---------------------------------------------------------------------------


def _split_complete_argv(raw: list[str]) -> tuple[list[str], list[str]]:
    """Split ``__complete`` argv into (our_flags, shell_words)."""
    if "--" in raw:
        idx = raw.index("--")
        return raw[:idx], raw[idx + 1 :]
    # No separator: treat known flags first, remainder as words.
    flags: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok in {"-h", "--help", "--no-llm"}:
            flags.append(tok)
            i += 1
            continue
        if tok in {"-c", "--config", "--timeout"}:
            flags.append(tok)
            if i + 1 < len(raw):
                flags.append(raw[i + 1])
                i += 2
            else:
                i += 1
            continue
        if tok.startswith("--config=") or tok.startswith("--timeout="):
            flags.append(tok)
            i += 1
            continue
        break
    return flags, raw[i:]


def run_complete(argv: list[str] | None = None) -> int:
    """Hidden shell protocol: print one candidate per stdout line."""
    from flowjet.cli.cli import resolve_cli_prog

    parser = argparse.ArgumentParser(prog=f"{resolve_cli_prog()} __complete", add_help=True)
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help="nano.yml path (default: ~/.soothe/config/nano.yml)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip fast-model call (local candidates only)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.25,
        metavar="SEC",
        help="LLM timeout in seconds (default: 0.25)",
    )
    raw = list(sys.argv[1:] if argv is None else argv)
    flag_tokens, words = _split_complete_argv(raw)
    args = parser.parse_args(flag_tokens)

    config: Any | None = None
    if not args.no_llm:
        try:
            from flowjet.cli.agent import load_config

            config = load_config(args.config)
        except Exception:
            config = None

    try:
        candidates = complete(
            list(words),
            config=config,
            llm_timeout_s=float(args.timeout),
            use_llm=not args.no_llm and config is not None,
        )
    except Exception:
        candidates = []

    for item in candidates:
        sys.stdout.write(item.replace("\n", " ").strip() + "\n")
    return 0


def run_completion_script(argv: list[str] | None = None) -> int:
    """Print shell install script: ``fj completion zsh|bash``."""
    from flowjet.cli.cli import resolve_cli_prog

    parser = argparse.ArgumentParser(
        prog=f"{resolve_cli_prog()} completion",
        description="Print shell completion script to stdout",
    )
    parser.add_argument(
        "shell",
        choices=("zsh", "bash"),
        help="Shell type",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    name = "_fj.zsh" if args.shell == "zsh" else "fj.bash"
    try:
        script = (
            importlib.resources.files("flowjet.cli.shell")
            .joinpath(name)
            .read_text(encoding="utf-8")
        )
    except Exception as exc:
        sys.stderr.write(f"error: completion script unavailable: {exc}\n")
        return 1
    sys.stdout.write(script)
    if not script.endswith("\n"):
        sys.stdout.write("\n")
    return 0
