"""Single nano bootstrap shared by the CLI and the server.

Both surfaces used to build their agent separately (``fj_ai.agent.build_agent``
and ``flowjet_server.bridges.nano.adapter.create_nano_agent_instance``), with
different constructors and contradictory security defaults. This module is the
one place where a ``SootheConfig`` is loaded, profiled and turned into an agent.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from soothe_nano.config import SOOTHE_HOME, SootheConfig
from soothe_nano.resolve import resolve_checkpointer

from flowjet.core.modes import CLI_PROFILE, InteractionMode, SurfaceProfile, require_mode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Builtin skills
# ---------------------------------------------------------------------------

_REGISTERED = False

# Skills belong to the distribution, not to either surface: both the CLI and
# the server expose the same FlowJet skill set.
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parents[1] / "builtin_skills"

# FlowJet additions on top of soothe-nano's DEFAULT_CORE_SKILL_NAMES.
FJ_CORE_SKILL_NAMES: tuple[str, ...] = ()


def default_core_skill_names() -> list[str]:
    """Nano default core skills plus FlowJet workflow skills."""
    from soothe_nano.skills.registry import DEFAULT_CORE_SKILL_NAMES

    return sorted(DEFAULT_CORE_SKILL_NAMES | {n.lower() for n in FJ_CORE_SKILL_NAMES})


def register_flowjet_builtin_skills() -> None:
    """Register ``flowjet/builtin_skills`` as a soothe-nano builtin skill root.

    Idempotent — safe to call from either surface and from pool worker threads.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    if not BUILTIN_SKILLS_DIR.is_dir():
        return
    from soothe_nano.skills import register_builtin_skill_root

    register_builtin_skill_root(BUILTIN_SKILLS_DIR, source="builtin")
    _REGISTERED = True


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    """Return ``~/.soothe/config/nano.yml`` (respects ``SOOTHE_HOME``)."""
    return SOOTHE_HOME / "config" / "nano.yml"


def load_config(config_path: str | Path | None = None) -> SootheConfig:
    """Load ``SootheConfig`` from YAML, or bootstrap from env when missing.

    Resolution order:
    1. Explicit ``config_path``
    2. ``SOOTHE_HOME / config / nano.yml`` (default ``~/.soothe/config/nano.yml``)
    3. ``SootheConfig()`` zero-config from ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``
    """
    path = Path(config_path).expanduser() if config_path else default_config_path()
    if path.is_file():
        return SootheConfig.from_yaml_file(str(path))
    if config_path is not None:
        raise FileNotFoundError(f"Config not found: {path}")
    return SootheConfig()


# ---------------------------------------------------------------------------
# Workspace / checkpointer
# ---------------------------------------------------------------------------


def ensure_workspace(workspace: Path | None = None) -> Path:
    """Use ``workspace`` or cwd as ``SOOTHE_WORKSPACE`` for file/shell tools."""
    root = (workspace or Path.cwd()).resolve()
    os.environ.setdefault("SOOTHE_WORKSPACE", str(root))
    try:
        from soothe_nano.workspace import FrameworkFilesystem

        FrameworkFilesystem.set_current_workspace(root)
    except Exception:  # pragma: no cover - optional API surface
        logger.debug("Could not set FrameworkFilesystem workspace", exc_info=True)
    return root


@asynccontextmanager
async def open_sqlite_checkpointer(config: SootheConfig) -> AsyncIterator[Any | None]:
    """Yield an ``AsyncSqliteSaver`` using nano's default sqlite data path.

    Path: ``$SOOTHE_DATA_DIR/soothe_checkpoints.db`` (default ``~/.soothe/data/``).
    """
    sqlite_cfg = config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={
                    "protocols": config.agent.protocols.model_copy(
                        update={
                            "durability": config.agent.protocols.durability.model_copy(
                                update={"backend": "sqlite", "checkpointer": "sqlite"}
                            )
                        }
                    )
                }
            )
        }
    )
    result = resolve_checkpointer(sqlite_cfg)
    db_path: str | None = None
    if isinstance(result, tuple):
        _placeholder, pool_or_path = result
        if isinstance(pool_or_path, str):
            db_path = pool_or_path

    if not db_path:
        logger.warning("SQLite checkpointer path unresolved; running without persistence")
        yield None
        return

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from soothe_sdk.utils.serde import create_soothe_serde

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    checkpointer = AsyncSqliteSaver(conn, serde=create_soothe_serde())
    await checkpointer.setup()
    logger.debug("SQLite checkpointer ready at %s", db_path)
    try:
        yield checkpointer
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def create_agent(
    config: SootheConfig,
    profile: SurfaceProfile = CLI_PROFILE,
    *,
    interaction_mode: InteractionMode | str | None = None,
) -> Any:
    """Build a soothe-nano agent for ``profile``'s surface.

    The server uses nano's dual-mode agent and supplies the interaction mode
    per request through the LangGraph ``configurable`` dict; the CLI resolves
    the mode up front and passes it to the single-mode constructor.
    """
    from flowjet.core.modes import apply_profile

    mode = require_mode(interaction_mode, profile)
    cfg = apply_profile(config, profile)

    if profile.surface.value == "server":
        from soothe_nano import create_dual_mode_nano_agent

        return create_dual_mode_nano_agent(cfg)

    from soothe_nano import create_nano_agent

    return create_nano_agent(cfg, interaction_mode=mode)
