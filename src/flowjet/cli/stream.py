"""Stream agent events: ephemeral progress + complete final answer."""

from __future__ import annotations

import re
import sys
import time
import traceback
import warnings
from typing import Any, TextIO

from langchain_core.messages import AIMessage, AIMessageChunk
from soothe_nano import SootheNanoAgent

from flowjet.cli.progress import (
    ProgressLine,
    format_tool_activity,
    format_tool_done,
    friendly_progress,
)
from flowjet.core.backends.direct import DirectRuntimeBackend
from flowjet.core.events import (
    InterruptWaiting,
    Narration,
    Progress,
    RunCompleted,
    RunFailed,
    RunRequest,
    ToolCompleted,
    ToolStarted,
)
from flowjet.core.tool_results import (
    preview_text,
)

# Min interval between live narration previews on the progress line (seconds).
_STATUS_PREVIEW_MIN_INTERVAL = 0.12

# Returned by ``stream_query`` / ``invoke_query`` when the agent paused for
# human input (``ask_user`` interrupt) instead of completing. The CLI checks
# for this sentinel to print the resume hint and accept an answer via ``fjf``.
INTERRUPTED = "__FJ_INTERRUPTED__"


def _format_duration(seconds: float) -> str:
    """Format an elapsed time as a short human-readable duration.

    Components are separated by spaces (e.g. ``1m 19s``, ``2h 5m``) so the
    value reads cleanly when embedded in the final summary line.
    """
    if seconds < 0 or not seconds:
        return "0s"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if parts:
        parts.append(f"{secs}s")
        return " ".join(parts)
    if seconds >= 10:
        return f"{seconds:.0f}s"
    return f"{seconds:.1f}s"


# Western + CJK sentence boundaries for status preview.
_STATUS_SENTENCE_SEPS = (
    ". ",
    "! ",
    "? ",
    "; ",
    "。",
    "！",
    "？",
    "；",
    "——",
    "… ",
)


# ---------------------------------------------------------------------------
# User-facing error formatting
# ---------------------------------------------------------------------------


def format_cli_error(exc: BaseException) -> str:
    """One-line summary for an uncaught run failure."""
    name = type(exc).__name__
    msg = str(exc).strip()
    if not msg:
        return f"error: {name}"
    first = next((line.strip() for line in msg.splitlines() if line.strip()), msg)
    if first.startswith(name + ":"):
        return f"error: {first}"
    # Avoid "error: RuntimeError: RuntimeError: ..." duplication.
    if first.startswith(name):
        return f"error: {first}"
    return f"error: {name}: {first}"


def write_cli_error(
    exc: BaseException,
    *,
    verbose: bool = False,
    err: TextIO | None = None,
) -> None:
    """Write a clean error to stderr; include traceback only when verbose."""
    stream = err or sys.stderr
    stream.write(format_cli_error(exc) + "\n")
    if verbose:
        stream.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    stream.flush()


# ---------------------------------------------------------------------------
# Stream helpers
# ---------------------------------------------------------------------------


def _verbose_preview(content: Any, *, limit: int = 200) -> str:
    """Single-line preview for ``-v`` tool results (no mid-line newlines)."""
    return preview_text(content, limit=limit)


def _write_verbose(status: ProgressLine, stderr: TextIO, text: str) -> None:
    """Write a verbose mirror line without colliding with the progress spinner."""
    status.blank()
    stderr.write(text if text.endswith("\n") else text + "\n")
    stderr.flush()
    status.repaint()


def _mirror_tool(
    status: ProgressLine,
    stderr: TextIO,
    mirrored: set[str],
    tc_id: str,
    name: str | None,
    args: dict[str, Any],
) -> None:
    """Emit ``[tool]`` once per call id when args are available."""
    if not tc_id or tc_id in mirrored or not name or not args:
        return
    mirrored.add(tc_id)
    _write_verbose(status, stderr, f"  [tool] {name} {args}\n")


def _ai_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    try:
        from soothe_nano.llm.response_text import llm_response_text

        return llm_response_text(message) or ""
    except Exception:
        return str(content) if content else ""


def accumulate_ai_text(current: str, message: AIMessage) -> str:
    """Merge streamed AI text.

    ``messages`` mode often yields ``AIMessageChunk`` deltas. Replacing with each
    chunk leaves only the last token fragment — accumulate instead.
    """
    text = _ai_text(message)
    if not text:
        return current

    if isinstance(message, AIMessageChunk):
        # Cumulative snapshot (some providers) vs pure delta.
        if current and text.startswith(current):
            return text
        if current and current.startswith(text) and len(current) >= len(text):
            # Out-of-order / shorter replay — keep longer buffer.
            return current
        return current + text

    # Full AIMessage snapshot: streaming chunks may already be longer than the
    # assembled message object — never discard the longer buffer.
    if len(text) >= len(current):
        return text
    return current


def _status_preview(text: str) -> str:
    """Single-line preview of AI narration for the ephemeral progress line."""
    line = re.sub(r"\s+", " ", text.strip())
    if not line:
        return "Writing answer…"
    # Prefer the latest sentence/clause with non-empty tail (skip trailing markers).
    best_idx = -1
    best_len = 0
    for sep in _STATUS_SENTENCE_SEPS:
        idx = 0
        while True:
            found = line.find(sep, idx)
            if found < 0:
                break
            tail = line[found + len(sep) :].strip()
            if tail and found >= best_idx:
                best_idx = found
                best_len = len(sep)
            idx = found + len(sep)
    if best_idx >= 0:
        line = line[best_idx + best_len :].strip()
    return line


class AnswerWriter:
    """Buffer answer text; show narration on the progress line; print only at end.

    Multi-step agent turns emit intermediate AI text ("Let me try…") before tool
    calls. That stays on the ephemeral progress line. ``finish()`` always writes
    the full buffer so the finalized response is never truncated.
    """

    def __init__(
        self,
        stdout: TextIO,
        status: ProgressLine,
        *,
        live: bool = True,
    ) -> None:
        self.buf = ""
        self._stdout = stdout
        self._status = status
        self._live = live
        self._last_preview_at = 0.0
        self._last_preview = ""

    def set(self, new_buf: str) -> None:
        """Replace the answer buffer; mirror a preview onto the progress line."""
        self.buf = new_buf
        if not new_buf:
            return
        if self._live:
            preview = _status_preview(new_buf)
            now = time.monotonic()
            if preview != self._last_preview and (
                not self._last_preview
                or now - self._last_preview_at >= _STATUS_PREVIEW_MIN_INTERVAL
            ):
                self._status.update(preview, color="green", tail=True)
                self._last_preview = preview
                self._last_preview_at = now
        else:
            self._status.update("Writing answer…", color="green")

    def reset_for_tools(self) -> None:
        """Drop buffered narration when a tool call starts (it was status, not result)."""
        self.buf = ""
        self._last_preview = ""
        self._last_preview_at = 0.0

    def finish(self) -> str:
        """Print the complete answer buffer once (progress is cleared by ``stop``)."""
        if self.buf:
            self._stdout.write(self.buf)
            if not self.buf.endswith("\n"):
                self._stdout.write("\n")
            self._stdout.flush()
        return self.buf


def format_questions_block(questions: tuple[dict[str, Any], ...], thread_id: str) -> str:
    """Render ``ask_user`` questions as a terminal block with a resume hint.

    The block is written to stdout after the ephemeral progress line is
    cleared, so it stays visible. Each question lists its options with the
    recommended one (label ending in "(Recommended)") marked. The footer
    tells the user how to resume with an answer via ``fjf``.
    """
    if not questions:
        return f"\n⏸ Paused for input · resume with: fjf -t {thread_id} <your answer>\n"
    lines: list[str] = ["", "⏸ Paused for input:"]
    for qi, q in enumerate(questions, 1):
        header = str(q.get("header") or "").strip()
        question = str(q.get("question") or "").strip()
        if len(questions) > 1:
            prefix = f"  Q{qi}: "
        else:
            prefix = "  "
        if header:
            lines.append(f"{prefix}[{header}] {question}")
        else:
            lines.append(f"{prefix}{question}")
        options = q.get("options")
        if isinstance(options, list):
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                label = str(opt.get("label") or "").strip()
                desc = str(opt.get("description") or "").strip()
                marker = " ★" if "(Recommended)" in label or "Recommended" in label else ""
                if desc:
                    lines.append(f"      • {label}{marker} — {desc}")
                else:
                    lines.append(f"      • {label}{marker}")
    lines.append(f"\n  Resume with: fjf -t {thread_id} <your answer>\n")
    return "\n".join(lines) + "\n"


async def stream_query(
    agent: SootheNanoAgent,
    query: str,
    *,
    thread_id: str,
    show_tool_calls: bool = False,
    live_answer: bool = True,
    out: TextIO | None = None,
    err: TextIO | None = None,
    progress: ProgressLine | None = None,
    resume_value: Any = None,
) -> str:
    """Run a query with ephemeral progress; print the complete final answer.

    When the agent pauses for human input (``ask_user`` interrupt), the
    questions are rendered and :data:`INTERRUPTED` is returned — the run is
    not complete and must be resumed with ``fjf -t <thread> <answer>``.
    """
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    status = progress if progress is not None else ProgressLine(stdout)
    backend = DirectRuntimeBackend(agent)
    answer = AnswerWriter(stdout, status, live=live_answer)
    # ``-v``: emit each tool call once when args are available.
    mirrored_tool_ids: set[str] = set()
    started_at = time.monotonic()
    interrupted = False

    # Deepagents emits a deprecation warning mid-run that would smash the
    # ephemeral progress line when mixed onto the terminal.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=DeprecationWarning,
            module=r"soothe_deepagents\.middleware\.filesystem",
        )
        warnings.filterwarnings(
            "ignore",
            message=r".*Passing a callable \(factory\) as `backend`.*",
        )
        async with status:
            status.update("Thinking", color="cyan")
            async for event in backend.stream_run(
                RunRequest(
                    model="default",
                    input_text=query,
                    session=thread_id,
                    resume_value=resume_value,
                )
            ):
                if isinstance(event, Progress):
                    # ``raw`` carries the soothe-nano custom payload so the
                    # terminal can keep using its own label/colour mapping.
                    if event.raw is not None:
                        mapped = friendly_progress(event.raw)
                        if mapped:
                            label, color = mapped
                            status.update(label, color=color)
                            # Only mirror events that drive the progress line
                            # (skips policy.checked, output.*, …).
                            if show_tool_calls:
                                event_type = event.raw.get("type", "unknown")
                                _write_verbose(status, stderr, f"  [event] {event_type}\n")
                    continue

                if isinstance(event, ToolStarted):
                    answer.reset_for_tools()
                    label, color = format_tool_activity(event.tool, event.args)
                    status.update(label, color=color)
                    if show_tool_calls:
                        _mirror_tool(
                            status,
                            stderr,
                            mirrored_tool_ids,
                            event.call_id or "",
                            event.tool,
                            event.args or {},
                        )
                    continue

                if isinstance(event, ToolCompleted):
                    is_error = not event.ok
                    label, color = format_tool_done(
                        event.tool,
                        event.args,
                        is_error=is_error,
                        detail=event.detail,
                    )
                    status.update(label, color=color)
                    if show_tool_calls:
                        # Late mirror if args never parsed as complete mid-stream.
                        _mirror_tool(
                            status,
                            stderr,
                            mirrored_tool_ids,
                            event.call_id or "",
                            event.tool,
                            event.args or {},
                        )
                        if event.preview:
                            tag = "error" if is_error else "result"
                            _write_verbose(status, stderr, f"  [{tag}] {event.preview}\n")
                    continue

                if isinstance(event, Narration):
                    # Live preview only; dropped automatically when a tool call
                    # starts (see ToolStarted → reset_for_tools).
                    answer.set(event.text)
                    continue

                if isinstance(event, RunCompleted):
                    if event.output_text:
                        answer.set(event.output_text)
                    continue

                if isinstance(event, InterruptWaiting):
                    status.update("Waiting for input…", color="yellow")
                    if event.questions:
                        # A real ``ask_user`` interrupt pauses the graph —
                        # render the questions and mark the run as interrupted
                        # so the CLI returns ``INTERRUPTED`` instead of "Done".
                        status.blank()
                        stdout.write(format_questions_block(event.questions, thread_id))
                        stdout.flush()
                        interrupted = True
                    elif show_tool_calls:
                        _write_verbose(
                            status,
                            stderr,
                            "\n  [interrupted] agent paused for input\n",
                        )
                    continue

                if isinstance(event, RunFailed):
                    raise RuntimeError(event.message)

    answer_text = answer.finish()
    if interrupted:
        # The run paused for input — don't print "Done", the questions block
        # (or generic pause message) is already on stdout.
        return INTERRUPTED
    duration = _format_duration(time.monotonic() - started_at)
    stdout.write(f"\n✓ Done · {duration} · {thread_id}\n")
    stdout.flush()
    return answer_text


async def invoke_query(
    agent: SootheNanoAgent,
    query: str,
    *,
    thread_id: str,
    out: TextIO | None = None,
    progress: ProgressLine | None = None,
    resume_value: Any = None,
) -> str:
    """Progress until done, then print the final text (no live narration preview)."""
    return await stream_query(
        agent,
        query,
        thread_id=thread_id,
        show_tool_calls=False,
        live_answer=False,
        out=out,
        progress=progress,
        resume_value=resume_value,
    )
