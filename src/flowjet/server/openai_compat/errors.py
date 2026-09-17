"""OpenAI-style API errors."""

from __future__ import annotations

from typing import Any


class OpenAIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.type = type
        self.code = code
        self.param = param
        self.status_code = status_code


def error_body(exc: OpenAIError) -> dict[str, Any]:
    return {
        "error": {
            "message": exc.message,
            "type": exc.type,
            "param": exc.param,
            "code": exc.code,
        }
    }
