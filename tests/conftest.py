"""Shared test helpers and fixtures for flowjet-server tests.

Live-server fixtures (``LiveServer``, ``live_server``, ``openai_client``,
``authed_live_server``) that spin up a real uvicorn process live in
``tests/integration/conftest.py`` and are only available to integration tests.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """Parse an SSE body into a list of event dicts.

    Skips the ``[DONE]`` sentinel used by OpenAI streaming endpoints,
    which is not valid JSON.
    """
    events: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        data_line = None
        for line in block.splitlines():
            if line.startswith("data: "):
                data_line = line[6:]
        if data_line and data_line != "[DONE]":
            events.append(json.loads(data_line))
    return events


@pytest.fixture
def parse_sse() -> Any:
    """Return the SSE-body parser helper (shared by integration tests)."""
    return _parse_sse
