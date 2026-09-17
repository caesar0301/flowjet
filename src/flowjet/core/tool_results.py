"""Pure helpers for interpreting tool results (no TTY / no I/O).

Shared by the nano→event mapper (which attaches a short error detail to
``ToolCompleted``) and by the CLI's error formatting.
"""

from __future__ import annotations

import json
import re
from typing import Any


def tool_result_error_detail(content: Any) -> str | None:
    """Extract an error string from a tool result payload, if any."""
    if isinstance(content, dict):
        err = content.get("error")
        return str(err).strip() if err else None

    if not isinstance(content, str):
        return None

    text = content.strip()
    if not text:
        return None

    for prefix in ("Error: ", "error: ", "ERROR: "):
        if text.startswith(prefix):
            return text[len(prefix) :].strip() or text

    if text.startswith("{") and '"error"' in text[:80]:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"]).strip()
    return None


def preview_text(content: Any, *, limit: int = 200) -> str:
    """Single-line preview of a tool result (no mid-line newlines)."""
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        try:
            raw = json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(content)
    else:
        raw = str(content)
    line = re.sub(r"\s+", " ", raw).strip()
    if len(line) <= limit:
        return line
    return line[:limit] + "…"


def simplify_tool_error(detail: str) -> str:
    """Shorten common tool failure strings for display."""
    text = detail.strip()
    match = re.search(r"unexpected keyword argument ['\"](\w+)['\"]", text)
    if match:
        return f"unexpected argument '{match.group(1)}'"
    match = re.search(r"missing \d+ required positional argument[s]?: (.+)$", text)
    if match:
        return f"missing argument {match.group(1)}"
    # Drop redundant exception type prefix when the message already explains itself.
    text = re.sub(r"^(TypeError|ValueError|RuntimeError|KeyError):\s*", "", text)
    text = re.sub(r"^\w+\.\w+\(\)\s+", "", text)  # e.g. Class.method()
    return text.strip() or detail.strip()
