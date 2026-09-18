"""Backend selection — which runtime builds FlowJet's agent.

FlowJet can build its agent two ways:

``soothe``
    The host runtime (``soothe.coreagent.create_soothe_agent``). ``soothe`` is a
    layer **on top of** soothe-nano, not a replacement for it: it injects host
    middleware (goal/step guard, decompose, eval, ask-user), host tools
    (``request_plan_mode``, ``ask_user``) and a configurable persona over the
    same nano agent graph.

``nano``
    soothe-nano directly (``create_nano_agent`` for the CLI,
    ``create_dual_mode_nano_agent`` for the server).

The two surfaces switch **independently**, because they do not share the same
constraints: the CLI builds one agent per invocation, while the server needs a
dual-mode agent (AGENT + ASK graphs selected per request) — an API soothe does
not expose today. Hence the defaults differ:

* ``FLOWJET_BACKEND`` (CLI) — default ``soothe``.
* ``FLOWJET_SERVER_BACKEND`` (HTTP/ACP) — default ``nano``.

Both accept ``soothe`` or ``nano`` (case-insensitive); anything else is a hard
error rather than a silent fallback, so a typo can never quietly run the wrong
runtime.
"""

from __future__ import annotations

import os
from enum import StrEnum

from flowjet.core.modes import Surface

CLI_BACKEND_ENV = "FLOWJET_BACKEND"
SERVER_BACKEND_ENV = "FLOWJET_SERVER_BACKEND"


class Backend(StrEnum):
    """Runtime that builds the agent."""

    SOOTHE = "soothe"
    NANO = "nano"


#: CLI builds one agent per invocation; the host runtime is the default there.
DEFAULT_CLI_BACKEND = Backend.SOOTHE

#: The server needs per-request mode switching (dual-mode agent), which soothe
#: does not expose; it stays on nano until host dual-mode exists.
DEFAULT_SERVER_BACKEND = Backend.NANO

_BACKENDS: dict[str, Backend] = {b.value: b for b in Backend}


def parse_backend(value: str | None, *, default: Backend, env_name: str) -> Backend:
    """Resolve one backend value (env string → :class:`Backend`).

    ``None`` and empty/whitespace values fall back to ``default``; an unknown
    value raises ``ValueError`` naming the offending variable.
    """
    if value is None:
        return default
    token = value.strip().lower()
    if not token:
        return default
    try:
        return _BACKENDS[token]
    except KeyError:
        raise ValueError(
            f"{env_name}={value!r} is not a valid backend "
            f"(expected one of: {', '.join(sorted(_BACKENDS))})"
        ) from None


def resolve_backend(surface: Surface | str) -> Backend:
    """Return the backend configured for ``surface`` (read from the env)."""
    resolved = Surface(str(surface))
    if resolved is Surface.SERVER:
        return parse_backend(
            os.environ.get(SERVER_BACKEND_ENV),
            default=DEFAULT_SERVER_BACKEND,
            env_name=SERVER_BACKEND_ENV,
        )
    return parse_backend(
        os.environ.get(CLI_BACKEND_ENV),
        default=DEFAULT_CLI_BACKEND,
        env_name=CLI_BACKEND_ENV,
    )


def backend_for_profile(profile: object) -> Backend:
    """Return the backend for a :class:`~flowjet.core.modes.SurfaceProfile`."""
    return resolve_backend(getattr(profile, "surface", Surface.CLI))


__all__ = [
    "CLI_BACKEND_ENV",
    "DEFAULT_CLI_BACKEND",
    "DEFAULT_SERVER_BACKEND",
    "SERVER_BACKEND_ENV",
    "Backend",
    "backend_for_profile",
    "parse_backend",
    "resolve_backend",
]
