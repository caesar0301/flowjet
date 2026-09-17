"""Ephemeral colored progress line for fj (stdout line 1)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from typing import Any, TextIO

# ANSI
_RESET = "\033[0m"
_BOLD = "\033[1m"
_COLORS = {
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "magenta": "\033[35m",
    "green": "\033[32m",
    "red": "\033[31m",
    "blue": "\033[34m",
    "white": "\033[37m",
}

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TICK_SECONDS = 0.08
_STEP_TIMER_MIN_SECONDS = 1.0
_DIM = "\033[2m"
# Visible status text budget (spinner glyph + space excluded).
_PROGRESS_MIN = 36
_PROGRESS_DEFAULT = 72
_PROGRESS_MAX = 200
# Extra arg parts when the arg budget is generous enough.
_ARGS_WIDE_BUDGET = 64
_ARGS_DEFAULT_PARTS = 1
_ARGS_WIDE_PARTS = 2
# Columns reserved for a typical verb + space when building arg previews.
_ARGS_VERB_RESERVE = 10
_ELLIPSIS = "…"
_SKIP_ARG_KEYS = frozenset(
    {
        "_raw",
        "_subgraph_tool",
        "value",
        "tool_call_id",
        "id",
        "type",
        "index",
    }
)

# Friendly verb for soothe event action suffixes
_ACTION_LABELS: dict[str, str] = {
    "started": "Starting",
    "completed": "Finished",
    "failed": "Failed",
    "error": "Error",
    "cancelled": "Cancelled",
    "iterated": "Working",
    "created": "Created",
    "classified": "Classified",
    "requested": "Waiting",
    "answered": "Resumed",
    "queued": "Queued",
}

# Verb + color for well-known tools (fallback: Running / yellow)
_TOOL_VERBS: dict[str, tuple[str, str]] = {
    "read_file": ("Reading", "yellow"),
    "write_file": ("Writing", "yellow"),
    "edit_file": ("Editing", "yellow"),
    "ls": ("Listing", "yellow"),
    "list_files": ("Listing", "yellow"),
    "glob": ("Globbing", "yellow"),
    "search_files": ("Globbing", "yellow"),
    "grep": ("Grepping", "yellow"),
    "run_command": ("Running", "yellow"),
    "shell": ("Running", "yellow"),
    "bash": ("Running", "yellow"),
    "web_search": ("Searching", "yellow"),
    "search_web": ("Searching", "yellow"),
    "fetch_url": ("Fetching", "yellow"),
    "http_request": ("Requesting", "yellow"),
    "search_tools": ("Finding tools", "blue"),
    "search_skills": ("Finding skills", "blue"),
    "invoke_skill": ("Invoking skill", "blue"),
    "task": ("Delegating", "magenta"),
}


def _char_display_width(ch: str) -> int:
    if unicodedata.east_asian_width(ch) in ("F", "W"):
        return 2
    return 1


def _display_width(text: str) -> int:
    """Terminal column count (CJK and fullwidth count double)."""
    return sum(_char_display_width(ch) for ch in text)


def _take_prefix_cols(text: str, budget: int) -> str:
    """Take a prefix of ``text`` that fits in ``budget`` display columns."""
    if budget <= 0:
        return ""
    picked: list[str] = []
    used = 0
    for ch in text:
        cw = _char_display_width(ch)
        if used + cw > budget:
            break
        picked.append(ch)
        used += cw
    return "".join(picked)


def _take_suffix_cols(text: str, budget: int) -> str:
    """Take a suffix of ``text`` that fits in ``budget`` display columns."""
    if budget <= 0:
        return ""
    picked: list[str] = []
    used = 0
    for ch in reversed(text):
        cw = _char_display_width(ch)
        if used + cw > budget:
            break
        picked.append(ch)
        used += cw
    return "".join(reversed(picked))


def _truncate_cols(text: str, limit: int, *, tail: bool = False, middle: bool = False) -> str:
    """Fit ``text`` to ``limit`` terminal columns.

    Modes:
    - default: keep the start (``aaaa…``)
    - ``tail=True``: keep the end (``…bbbb``)
    - ``middle=True``: keep head and tail (``aaaa…bbbb``); overrides ``tail``
    """
    if limit <= 0:
        return _ELLIPSIS if limit == 1 else ""
    text = re.sub(r"\s+", " ", text.strip())
    if _display_width(text) <= limit:
        return text

    ell_w = _display_width(_ELLIPSIS)
    if limit <= ell_w:
        return _ELLIPSIS[:1] if limit == 1 else _ELLIPSIS

    budget = limit - ell_w
    if middle:
        # Prefer a slightly longer head; need at least 1 col each side when possible.
        if budget < 2:
            return _take_prefix_cols(text, budget) + _ELLIPSIS
        head_budget = (budget + 1) // 2
        tail_budget = budget - head_budget
        head_chars: list[str] = []
        head_used = 0
        i = 0
        while i < len(text):
            cw = _char_display_width(text[i])
            if head_used + cw > head_budget:
                break
            head_chars.append(text[i])
            head_used += cw
            i += 1
        tail_chars: list[str] = []
        tail_used = 0
        j = len(text)
        while j > i:
            cw = _char_display_width(text[j - 1])
            if tail_used + cw > tail_budget:
                break
            tail_chars.append(text[j - 1])
            tail_used += cw
            j -= 1
        return "".join(head_chars) + _ELLIPSIS + "".join(reversed(tail_chars))

    if tail:
        return _ELLIPSIS + _take_suffix_cols(text, budget)

    return _take_prefix_cols(text, budget) + _ELLIPSIS


def _color_enabled(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FJ_FORCE_COLOR") in {"1", "true", "yes"}:
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def _line_budget() -> int:
    """Max visible chars for the status text (spinner + margin excluded)."""
    env = os.environ.get("FJ_PROGRESS_WIDTH", "").strip()
    if env.isdigit():
        return max(_PROGRESS_MIN, min(_PROGRESS_MAX, int(env)))
    try:
        cols = shutil.get_terminal_size(fallback=(_PROGRESS_DEFAULT + 6, 24)).columns
    except OSError:
        cols = _PROGRESS_DEFAULT + 6
    # Leave room for spinner glyph, space, and terminal margin.
    return max(_PROGRESS_MIN, min(_PROGRESS_MAX, cols - 6))


def _truncate_middle(text: str, limit: int | None = None) -> str:
    """Keep head and tail of ``text`` (``aaaa…bbbb``) for content previews."""
    limit = _line_budget() if limit is None else limit
    return _truncate_cols(text, limit, middle=True)


def _truncate_path(text: str, limit: int | None = None) -> str:
    """Keep the end of a path so the basename stays visible."""
    limit = _line_budget() if limit is None else limit
    text = re.sub(r"\s+", " ", text.strip())
    if _display_width(text) <= limit:
        return text
    if limit <= 1:
        return _ELLIPSIS
    # Prefer cutting after a path separator when possible.
    tail = _truncate_cols(text, limit, tail=True)
    if tail.startswith(_ELLIPSIS):
        body = tail[len(_ELLIPSIS) :]
        slash = body.find("/")
        if slash > 0 and slash < len(body) - 4:
            body = body[slash + 1 :]
            candidate = _ELLIPSIS + "/" + body
            if _display_width(candidate) <= limit:
                return candidate
    return tail


def _short_path(path: str, limit: int) -> str:
    """Compact path for the activity line: last two segments, else basename."""
    text = re.sub(r"\s+", " ", _abbrev_path(path).strip())
    if not text:
        return ""
    # Normalize separators for segment splitting.
    norm = text.replace("\\", "/")
    segments = [s for s in norm.split("/") if s and s != "~"]
    if len(segments) >= 2:
        candidate = f"{segments[-2]}/{segments[-1]}"
    elif segments:
        candidate = segments[-1]
    else:
        candidate = text
    if _display_width(candidate) <= limit:
        return candidate
    # Fall back to basename, then truncate from the end.
    base = segments[-1] if segments else text
    if _display_width(base) <= limit:
        return base
    return _truncate_cols(base, limit, tail=True)


def _fit(
    label: str,
    *,
    budget: int | None = None,
    tail: bool = False,
    middle: bool = False,
) -> str:
    """Final single-line fit for the progress status."""
    limit = _line_budget() if budget is None else budget
    normalized = re.sub(r"\s+", " ", label.strip())
    return _truncate_cols(normalized, limit, tail=tail, middle=middle)


def _compact(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value.strip())
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        if all(isinstance(x, (str, int, float, bool)) or x is None for x in value[:5]):
            inner = ", ".join(_compact(x) for x in value[:5])
            if len(value) > 5:
                inner += ", …"
            return inner
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _abbrev_path(path: str) -> str:
    try:
        from soothe_sdk.utils.formatting import convert_and_abbreviate_path

        return convert_and_abbreviate_path(path)
    except Exception:
        home = os.path.expanduser("~")
        if path.startswith(home + os.sep):
            return "~" + path[len(home) :]
        return path


def _tool_meta(name: str) -> Any | None:
    try:
        from soothe_sdk.tools.metadata import get_tool_meta

        return get_tool_meta(name)
    except Exception:
        return None


def _normalize_args(args: Any) -> dict[str, Any]:
    """Coerce tool-call args (dict / JSON string / partial) into a flat dict."""
    if args is None:
        return {}
    if isinstance(args, str):
        text = args.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except (TypeError, ValueError):
            return {"_text": text}
        if isinstance(loaded, dict):
            return loaded
        return {"_text": text}
    if isinstance(args, dict):
        # Nested ``value`` may hold JSON from some adapters.
        if "value" in args and isinstance(args["value"], str) and args["value"].strip():
            try:
                loaded = json.loads(args["value"])
            except (TypeError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                merged = {k: v for k, v in args.items() if k != "value"}
                merged.update(loaded)
                return merged
        return dict(args)
    return {"_text": _compact(args)}


def _ordered_arg_keys(tool_name: str, args: dict[str, Any]) -> list[str]:
    meta = _tool_meta(tool_name)
    ordered: list[str] = []
    preferred: tuple[str, ...] = ()
    if meta is not None and getattr(meta, "arg_keys", None):
        preferred = tuple(meta.arg_keys)
    # Sensible defaults when meta is missing.
    if not preferred:
        preferred = (
            "command",
            "cmd",
            "script",
            "file_path",
            "path",
            "target_file",
            "pattern",
            "query",
            "url",
            "regex",
            "old_string",
            "new_string",
            "content",
            "skill",
            "description",
            "prompt",
            "subagent_type",
            "_text",
        )
    for key in preferred:
        if key in args and key not in _SKIP_ARG_KEYS:
            ordered.append(key)
    for key in sorted(args.keys()):
        if key not in ordered and key not in _SKIP_ARG_KEYS:
            ordered.append(key)
    return ordered


def _format_arg_value(tool_name: str, key: str, value: Any, *, budget: int) -> str:
    text = _compact(value)
    if not text:
        return ""
    meta = _tool_meta(tool_name)
    path_keys = set(getattr(meta, "path_arg_keys", ()) or ())
    is_path = key in path_keys or key in {
        "file_path",
        "path",
        "target_file",
        "directory",
        "target_directory",
        "dir",
        "filepath",
        "filename",
        "relative_path",
    }
    if is_path:
        return _short_path(text, budget)
    if key in {"pattern", "query", "regex", "regexp", "old_string", "new_string", "skill"}:
        return _truncate_middle(text, max(12, budget))
    if key in {"command", "cmd", "script"}:
        # Keep command start (program + first args).
        return _truncate_cols(text, max(16, budget), middle=False)
    if key in {"content"}:
        return _truncate_middle(text, max(10, budget))
    return _truncate_middle(text, max(12, budget))


def format_args_preview(
    tool_name: str,
    args: Any | None,
    *,
    max_parts: int | None = None,
    prefix_width: int | None = None,
) -> str:
    """Human-readable arg summary, e.g. ``flowjet.cli/cli.py`` or ``TODO in src/``."""
    clean = _normalize_args(args)
    if not clean:
        return ""

    budget = _line_budget()
    reserve = _ARGS_VERB_RESERVE if prefix_width is None else max(0, prefix_width)
    arg_budget = max(20, budget - reserve)
    if max_parts is None:
        max_parts = _ARGS_WIDE_PARTS if arg_budget >= _ARGS_WIDE_BUDGET else _ARGS_DEFAULT_PARTS

    canonical = tool_name
    meta = _tool_meta(tool_name)
    if meta is not None:
        canonical = meta.name

    parts: list[str] = []
    sep_w = _display_width(" · ")
    for key in _ordered_arg_keys(canonical, clean):
        used = sum(_display_width(p) for p in parts) + sep_w * len(parts)
        remaining = max(12, arg_budget - used)
        text = _format_arg_value(canonical, key, clean[key], budget=remaining)
        if not text:
            continue
        if not parts:
            # grep/glob: fold path into the pattern part so max_parts=1 still shows both.
            if canonical in {"grep", "glob"} and key in {"pattern", "query", "regex", "regexp"}:
                path_key = next(
                    (
                        k
                        for k in ("path", "file_path", "directory", "target_directory", "dir")
                        if k in clean and clean[k] not in (None, "")
                    ),
                    None,
                )
                if path_key is not None:
                    path_text = _format_arg_value(
                        canonical, path_key, clean[path_key], budget=max(8, remaining // 2)
                    )
                    if path_text:
                        text = f"{text} in {path_text}"
            parts.append(text)
        elif key in {"path", "file_path", "directory", "target_directory", "dir"} and parts:
            if canonical in {"grep", "glob"}:
                if " in " in parts[0]:
                    continue
                parts[0] = f"{parts[0]} in {text}"
            else:
                parts.append(text)
        elif key in {"old_string", "new_string"} and canonical == "edit_file":
            if any(" → " in p for p in parts):
                continue
            old_val = clean.get("old_string")
            new_val = clean.get("new_string")
            old_text = (
                _format_arg_value(canonical, "old_string", old_val, budget=max(8, remaining // 2))
                if old_val not in (None, "")
                else ""
            )
            new_text = (
                _format_arg_value(canonical, "new_string", new_val, budget=max(8, remaining // 2))
                if new_val not in (None, "")
                else ""
            )
            if old_text and new_text:
                parts.append(f"{old_text} → {new_text}")
            elif old_text:
                parts.append(old_text)
            elif new_text:
                parts.append(f"→ {new_text}")
            else:
                continue
        else:
            parts.append(text)
        if len(parts) >= max_parts:
            break

    return _fit(" · ".join(parts), budget=arg_budget, middle=True)


def format_tool_activity(tool_name: str, args: Any | None = None) -> tuple[str, str]:
    """Status line for an in-flight tool call: ``Reading flowjet.cli/cli.py``."""
    name = (tool_name or "tool").strip() or "tool"
    verb, color = _TOOL_VERBS.get(name, ("Running", "yellow"))
    meta = _tool_meta(name)
    if meta is not None:
        verb, color = _TOOL_VERBS.get(meta.name, (verb, color))
        name = meta.name

    known = name in _TOOL_VERBS or (meta is not None and meta.name in _TOOL_VERBS)
    # Reserve columns for the verb (or "Running tool_name ") before args.
    if known:
        prefix_width = _display_width(verb) + 1
    else:
        prefix_width = _display_width(f"{verb} {name} ")
    preview = format_args_preview(name, args, prefix_width=prefix_width)
    if preview:
        if known:
            label = f"{verb} {preview}"
        else:
            label = f"{verb} {name} {preview}"
    else:
        label = verb if known else f"{verb} {name}"
    return _fit(label), color


def _detail_budget(*, prefix: str, fraction: float = 0.5, floor: int = 16) -> int:
    """Columns available for a detail snippet after a fixed status prefix."""
    line = _line_budget()
    remaining = line - _display_width(prefix)
    return max(floor, min(remaining, int(line * fraction)))


def format_tool_done(
    tool_name: str | None,
    args: Any | None = None,
    *,
    is_error: bool = False,
    detail: str | None = None,
) -> tuple[str, str]:
    """Status after a tool returns — keep context while model thinks."""
    name = (tool_name or "tool").strip() or "tool"
    meta = _tool_meta(name)
    if meta is not None:
        name = meta.name
    prefix = "Failed " if is_error else "Thinking · "
    preview = format_args_preview(
        name, args, max_parts=1, prefix_width=_display_width(prefix + name + " ")
    )
    summary = f"{name} {preview}" if preview else name
    if is_error:
        label = f"Failed {summary}"
        if detail:
            detail_limit = _detail_budget(prefix=f"{label} · ", fraction=0.45, floor=20)
            label = f"{label} · {_truncate_middle(_compact(detail), detail_limit)}"
        return _fit(label), "red"
    return _fit(f"Thinking · {summary}"), "cyan"


def _tool_name(data: dict[str, Any]) -> str | None:
    for key in ("tool", "tool_name", "name"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _event_args(data: dict[str, Any]) -> dict[str, Any]:
    """Pull arg-like fields from a soothe custom event."""
    raw = data.get("args")
    if isinstance(raw, dict):
        return dict(raw)
    collected: dict[str, Any] = {}
    for key in (
        "command",
        "cmd",
        "script",
        "file_path",
        "path",
        "file",
        "url",
        "query",
        "pattern",
        "regex",
        "action_preview",
        "skill",
        "message",
        "prompt",
        "description",
        "subagent_type",
        "old_string",
        "new_string",
    ):
        if key in data and data[key] not in (None, ""):
            collected[key] = data[key]
    return collected


def friendly_progress(data: dict[str, Any]) -> tuple[str, str] | None:
    """Map a soothe-nano custom event to ``(label, color)`` or ``None`` to skip."""
    if not isinstance(data, dict):
        return None
    event_type = str(data.get("type") or "").strip()
    if not event_type:
        return None

    # Skip noisy / internal stream plumbing
    if event_type in {
        "soothe.stream.end",
        "soothe.protocol.message.received",
        # Fired on every tool call; stomps useful tool status with a bare verb.
        "soothe.internal.policy.checked",
        "soothe.internal.plugin.health_checked",
    }:
        return None
    if event_type.startswith("soothe.output."):
        return None

    short = event_type[7:] if event_type.startswith("soothe.") else event_type
    parts = short.split(".")
    domain = parts[0] if parts else "agent"
    action = parts[-1] if parts else ""
    verb = _ACTION_LABELS.get(action, action.replace("_", " ").title() or "Working")

    tool = _tool_name(data)
    args = _event_args(data)
    subagent = None
    if domain == "subagent" and len(parts) >= 2:
        subagent = parts[1]
    elif "subagent" in data and isinstance(data["subagent"], str):
        subagent = data["subagent"]
    elif "subagent_type" in data and isinstance(data["subagent_type"], str):
        subagent = data["subagent_type"]

    if event_type == "soothe.internal.policy.denied":
        denied_action = _compact(data.get("action"))
        reason = _compact(data.get("reason") or data.get("message"))
        head = f"Policy denied {denied_action}" if denied_action else "Policy denied"
        if reason:
            reason_limit = _detail_budget(prefix=f"{head} · ", fraction=0.55, floor=20)
            label = f"{head} · {_truncate_middle(reason, reason_limit)}"
        else:
            label = head
        return _fit(label), "red"

    if event_type == "soothe.internal.memory.recalled":
        count = data.get("count")
        query = _compact(data.get("query"))
        if query:
            q_limit = _detail_budget(prefix="Recalling memory · ", fraction=0.55, floor=16)
            label = f"Recalling memory · {_truncate_middle(query, q_limit)}"
        elif isinstance(count, int) and count > 0:
            label = f"Recalled {count} memor{'y' if count == 1 else 'ies'}"
        else:
            label = "Recalling memory"
        return _fit(label), "cyan"

    if event_type == "soothe.internal.memory.stored":
        mem_id = _compact(data.get("id"))
        label = f"Stored memory · {mem_id}" if mem_id else "Stored memory"
        return _fit(label), "cyan"

    if domain in {"tool", "mcp"}:
        name = tool or (parts[1] if len(parts) > 1 else "tool")
        if action in {"completed", "finished"}:
            return format_tool_done(name, args)
        if action in {"failed", "error"}:
            return format_tool_done(name, args, is_error=True)
        return format_tool_activity(name, args)

    if domain == "subagent" or "wired_subagent" in short:
        color = "magenta"
        name = subagent or tool or "subagent"
        preview = format_args_preview(name, args) if args else ""
        if not preview:
            prefix = f"{verb} {name} · " if action else f"Delegating {name} · "
            detail = _fit(
                _compact(data.get("action_preview") or data.get("query") or ""),
                budget=_detail_budget(prefix=prefix, fraction=0.55, floor=20),
            )
        else:
            detail = preview
        if action in {"completed", "finished"}:
            label = f"Thinking · {name}"
            color = "cyan"
        else:
            label = f"Delegating {name}"
            if detail:
                label = f"{label} · {detail}"
        return _fit(label), color

    if domain == "skill" or "skill" in short:
        color = "blue"
        name = data.get("skill") or data.get("name") or "skill"
        label = f"Skill {name}"
        return _fit(label), color

    if domain in {"error"} or action in {"failed", "error"}:
        color = "red"
        err = data.get("error") or data.get("message") or short
        err_limit = _detail_budget(prefix="Error · ", fraction=0.55, floor=24)
        label = f"Error · {_truncate_middle(_compact(err), err_limit)}"
        return _fit(label), color

    if domain == "cognition":
        color = "cyan"
        if "plan" in short:
            label = "Planning"
        elif "goal" in short and action == "completed":
            color = "green"
            label = "Goal complete"
        elif "strange_loop" in short:
            label = "Thinking"
        elif "intent" in short:
            intent = data.get("intent") or data.get("label") or data.get("message")
            if intent:
                intent_limit = _detail_budget(prefix="Understanding · ", fraction=0.5, floor=16)
                label = f"Understanding · {_truncate_middle(_compact(intent), intent_limit)}"
            else:
                label = "Understanding"
        else:
            label = verb
        return _fit(label), color

    label = verb
    if tool:
        return format_tool_activity(tool, args)
    detail = format_args_preview("tool", args) if args else ""
    if detail:
        label = f"{verb} · {detail}"
    return _fit(label), "cyan"


class ProgressLine:
    """Overwrite a single ephemeral status line on stdout (TTY only).

    A background asyncio tick keeps the spinner moving between stream events
    (e.g. while waiting on the model after a tool result).
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        enabled: bool | None = None,
        tick_seconds: float = _TICK_SECONDS,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        if enabled is None:
            enabled = bool(getattr(self._stream, "isatty", lambda: False)())
        self._enabled = enabled
        self._color = _color_enabled(self._stream) if self._enabled else False
        self._active = False
        self._frame = 0
        self._message = "Working"
        self._style = "cyan"
        self._tick_seconds = tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._step_started_at = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def __aenter__(self) -> ProgressLine:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the background spinner ticker."""
        if not self._enabled or self._task is not None:
            return
        self._active = True
        self._step_started_at = time.monotonic()
        self._paint()
        self._task = asyncio.create_task(self._spin(), name="fj-progress-spinner")

    async def stop(self) -> None:
        """Stop the ticker and clear the progress line."""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.clear()

    async def _spin(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._tick_seconds)
                if self._active:
                    self._paint()
        except asyncio.CancelledError:
            raise

    def update(self, message: str, *, color: str = "cyan", tail: bool = False) -> None:
        if not self._enabled:
            return
        fitted = _fit(message.strip() or "Working", tail=tail)
        if fitted != self._message:
            self._step_started_at = time.monotonic()
        self._message = fitted
        self._style = color
        self._active = True
        self._paint()
        self._ensure_ticker()

    def _ensure_ticker(self) -> None:
        if not self._enabled or self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._spin(), name="fj-progress-spinner")

    def _elapsed_suffix(self) -> str:
        elapsed = time.monotonic() - self._step_started_at
        if elapsed < _STEP_TIMER_MIN_SECONDS:
            return ""
        total = int(elapsed)
        if total < 60:
            return f" · {total}s"
        minutes, secs = divmod(total, 60)
        return f" · {minutes}m{secs:02d}s"

    def _paint(self) -> None:
        if not self._enabled:
            return
        suffix = self._elapsed_suffix()
        budget = _line_budget()
        suffix_w = _display_width(suffix)
        text = _fit(self._message, budget=max(1, budget - suffix_w))
        frame = _SPINNER[self._frame % len(_SPINNER)]
        self._frame += 1
        if self._color:
            code = _COLORS.get(self._style, _COLORS["cyan"])
            dim_suffix = f"{_DIM}{suffix}{_RESET}{code}" if suffix else ""
            rendered = f"{code}{_BOLD}{frame}{_RESET}{code} {text}{dim_suffix}{_RESET}"
        else:
            rendered = f"{frame} {text}{suffix}"
        self._stream.write(f"\r\033[2K{rendered}")
        self._stream.flush()

    def blank(self) -> None:
        """Erase the progress line so other writers can print cleanly."""
        if not self._enabled:
            return
        if self._frame > 0 or self._message:
            self._stream.write("\r\033[2K")
            self._stream.flush()

    def repaint(self) -> None:
        """Redraw the current progress frame after a ``blank()``."""
        if self._active:
            self._paint()

    def clear(self) -> None:
        self._active = False
        if not self._enabled:
            return
        if self._frame > 0 or self._message:
            self._stream.write("\r\033[2K")
            self._stream.flush()
        self._message = ""
