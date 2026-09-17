"""Bearer auth dependency."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, Request

from flowjet.server.openai_compat.errors import OpenAIError


async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = getattr(request.app.state, "api_key", None)
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise OpenAIError(
            "Missing bearer token.",
            type="authentication_error",
            code="invalid_api_key",
            status_code=401,
        )
    token = authorization.removeprefix("Bearer ").strip()
    # Constant-time comparison: a plain ``!=`` short-circuits on the first
    # differing byte, which leaks how much of the key the caller got right.
    if not hmac.compare_digest(token.encode(), str(expected).encode()):
        raise OpenAIError(
            "Incorrect API key provided.",
            type="authentication_error",
            code="invalid_api_key",
            status_code=401,
        )
