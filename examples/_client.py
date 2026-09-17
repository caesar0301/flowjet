"""Shared helpers for flowjet-server end-to-end examples."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from openai import OpenAI


def api_key() -> str:
    return os.environ.get("FLOWJET_API_KEY") or os.environ.get("OPENAI_API_KEY") or "local"


def base_url() -> str:
    """OpenAI SDK base URL (…/v1)."""
    return os.environ.get("FLOWJET_BASE_URL", "http://127.0.0.1:8618/v1").rstrip("/")


def root_url() -> str:
    """Server root (without /v1) for /health."""
    parsed = urlparse(base_url())
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")] or ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def make_client() -> OpenAI:
    """Official OpenAI client pointed at flowjet-server."""
    return OpenAI(api_key=api_key(), base_url=base_url())


def make_http() -> httpx.Client:
    """Raw HTTP client (Bearer when a key is configured on either side)."""
    return httpx.Client(
        base_url=root_url() or "http://127.0.0.1:8618",
        headers={"Authorization": f"Bearer {api_key()}"},
        timeout=60.0,
    )


def output_text(response: Any) -> str:
    """Best-effort plain text from a Responses API result."""
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text:
        return text
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) == "output_text":
                parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def output_text_from_dict(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                parts.append(str(block.get("text") or ""))
    return "".join(parts)


def event_extra(event: Any, key: str) -> Any:
    """Read FlowJet extension fields from typed SDK events (may live in model_extra)."""
    value = getattr(event, key, None)
    if value is not None:
        return value
    extra = getattr(event, "model_extra", None) or {}
    return extra.get(key)


def parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        data_line = None
        for line in block.splitlines():
            if line.startswith("data: "):
                data_line = line[6:]
        if data_line:
            events.append(json.loads(data_line))
    return events


def require_completed(events: list[dict[str, Any]], label: str) -> None:
    """Stop with the server's own error when a streamed run did not complete."""
    for event in events:
        if event.get("type") != "response.failed":
            continue
        error = (event.get("response") or {}).get("error") or {}
        message = error.get("message") or "no error message returned"
        raise SystemExit(f"{label}: the agent run failed — {message}")
    types = [e.get("type") for e in events]
    if "response.completed" not in types:
        raise SystemExit(f"{label}: stream ended without response.completed; saw {types}")


def section(title: str) -> None:
    print()
    print(f"== {title} ==")
