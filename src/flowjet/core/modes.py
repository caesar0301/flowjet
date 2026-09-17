"""Surface profiles — the explicit home for per-surface invariants.

Before the merge, the two products carried **opposite** security defaults in
two different modules:

* ``fj_ai.agent.apply_fj_defaults`` forced ``allow_paths_outside_workspace=True``
  (a CLI runs on the user's own machine, with the user's own files).
* ``flowjet_server.bridges.nano.adapter`` forced it to ``False`` (a server
  hands each request an isolated workspace that tools must never escape).

Sharing one bootstrap without naming that difference would silently flip one
product's security posture. ``SurfaceProfile`` makes each invariant explicit
and per-surface, and ``require_mode`` makes ``bypass`` unreachable over HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from soothe_nano.config import SootheConfig


class Surface(StrEnum):
    """Which product surface is driving the runtime."""

    CLI = "cli"
    SERVER = "server"


class InteractionMode(StrEnum):
    AGENT = "agent"
    ASK = "ask"
    BYPASS = "bypass"


@dataclass(frozen=True, slots=True)
class SurfaceProfile:
    """Invariants applied to a ``SootheConfig`` before an agent is built."""

    surface: Surface
    allow_paths_outside_workspace: bool
    allowed_modes: frozenset[InteractionMode]
    force_sqlite: bool = False
    seed_core_skills: bool = False
    register_builtin_skills: bool = True


CLI_PROFILE = SurfaceProfile(
    surface=Surface.CLI,
    allow_paths_outside_workspace=True,
    allowed_modes=frozenset({InteractionMode.AGENT, InteractionMode.ASK, InteractionMode.BYPASS}),
    force_sqlite=True,
    seed_core_skills=True,
    register_builtin_skills=True,
)

SERVER_PROFILE = SurfaceProfile(
    surface=Surface.SERVER,
    allow_paths_outside_workspace=False,
    allowed_modes=frozenset({InteractionMode.AGENT, InteractionMode.ASK}),
    force_sqlite=False,
    seed_core_skills=False,
    register_builtin_skills=True,
)


def require_mode(mode: InteractionMode | str | None, profile: SurfaceProfile) -> str | None:
    """Validate ``mode`` against ``profile``; return the nano-mode string.

    ``None`` means "let nano resolve the mode from config". ``bypass`` is a
    CLI-only escape hatch and is rejected for the server surface.
    """
    if mode is None:
        return None
    resolved = InteractionMode(str(mode))
    if resolved not in profile.allowed_modes:
        raise ValueError(
            f"interaction mode {resolved.value!r} is not allowed on the "
            f"{profile.surface.value} surface "
            f"(allowed: {sorted(m.value for m in profile.allowed_modes)})"
        )
    return resolved.value


def apply_profile(config: SootheConfig, profile: SurfaceProfile) -> SootheConfig:
    """Return a copy of ``config`` with ``profile``'s invariants applied."""
    from flowjet.core.bootstrap import (
        default_core_skill_names,
        register_flowjet_builtin_skills,
    )

    if profile.register_builtin_skills:
        register_flowjet_builtin_skills()

    updates: dict[str, Any] = {}

    if profile.force_sqlite:
        durability = config.agent.protocols.durability.model_copy(
            update={"backend": "sqlite", "checkpointer": "sqlite"}
        )
        protocols = config.agent.protocols.model_copy(update={"durability": durability})
        updates["agent"] = config.agent.model_copy(update={"protocols": protocols})
        updates["persistence"] = config.persistence.model_copy(update={"default_backend": "sqlite"})

    updates["security"] = config.security.model_copy(
        update={"allow_paths_outside_workspace": profile.allow_paths_outside_workspace}
    )

    if profile.seed_core_skills and config.progressive_skills.core_skills is None:
        updates["progressive_skills"] = config.progressive_skills.model_copy(
            update={"core_skills": default_core_skill_names()}
        )

    return config.model_copy(update=updates)
