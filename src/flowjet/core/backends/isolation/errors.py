"""Isolation-layer errors (mapped to OpenAI errors by the HTTP service)."""

from __future__ import annotations


class IsolationError(Exception):
    """Request rejected by isolation policy (workspace, mode pin, etc.)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
