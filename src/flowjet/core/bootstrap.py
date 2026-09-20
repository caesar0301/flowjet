"""Single bootstrap shared by the CLI and the server.

Both surfaces used to build their agent separately (``fj_ai.agent.build_agent``
and ``flowjet_server.bridges.nano.adapter.create_nano_agent_instance``), with
different constructors and contradictory security defaults. This module is the
one place where a ``SootheConfig`` is loaded, profiled and turned into an agent.

Which runtime builds that agent is decided by :mod:`flowjet.core.backend`
(``soothe`` host runtime or ``soothe-nano``), per surface.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from soothe_nano.config import SootheConfig
from soothe_nano.resolve import resolve_checkpointer

from flowjet.core.backend import Backend, resolve_backend
from flowjet.core.modes import (
    CLI_PROFILE,
    InteractionMode,
    Surface,
    SurfaceProfile,
    require_mode,
)
from flowjet.home import default_config_path as _flowjet_config_path
from flowjet.home import ensure_flowjet_config

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
    """Return ``$FLOWJET_HOME/config/nano.yml`` (default ``~/.flowjet/config/nano.yml``)."""
    return _flowjet_config_path()


def load_config(
    config_path: str | Path | None = None,
    *,
    backend: Backend | None = None,
) -> Any:
    """Load a ``SootheConfig`` for ``backend``, or bootstrap from env when missing.

    The config class follows the backend: ``soothe`` loads the *host*
    ``soothe.config.SootheConfig`` (a superset of nano's — the host agent reads
    host-owned fields such as ``agent.loop`` / ``agent.clarification`` that nano's
    config does not define), ``nano`` loads ``soothe_nano.config.SootheConfig``.

    Resolution order:
    1. Explicit ``config_path``
    2. ``$FLOWJET_HOME / config / nano.yml`` (default ``~/.flowjet/config/nano.yml``)
    3. Zero-config ``SootheConfig()`` from ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``

    On the ``soothe`` backend, a sibling ``soothe.yml`` (host-owned keys) is
    composed over ``nano.yml`` when present.

    Without an explicit ``config_path``, a config found in ``~/.soothe/config`` is
    copied into the flowjet home first — see :func:`flowjet.home.ensure_flowjet_config`.
    """
    backend = backend or resolve_backend(Surface.CLI)
    if config_path is None:
        ensure_flowjet_config()
        path = default_config_path()
    else:
        path = Path(config_path).expanduser()
    if path.is_file():
        return _load_config_file(path, backend)
    if config_path is not None:
        raise FileNotFoundError(f"Config not found: {path}")
    return default_config(backend)


def default_config(backend: Backend) -> Any:
    """Zero-config ``SootheConfig`` for ``backend`` (env-derived providers)."""
    if backend is Backend.SOOTHE:
        from soothe.config import SootheConfig as HostSootheConfig

        return HostSootheConfig()
    return SootheConfig()


def _load_config_file(path: Path, backend: Backend) -> Any:
    """Parse ``path`` into the config class owned by ``backend``."""
    if backend is Backend.SOOTHE:
        from soothe.config import SootheConfig as HostSootheConfig

        host_path = path.with_name("soothe.yml")
        if host_path.is_file():
            return HostSootheConfig.from_split_yaml_files(
                nano_path=str(path), soothe_path=str(host_path)
            )
        return HostSootheConfig.from_yaml_file(str(path))
    return SootheConfig.from_yaml_file(str(path))


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
async def open_sqlite_checkpointer(config: Any) -> AsyncIterator[Any | None]:
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
    config: Any,
    profile: SurfaceProfile = CLI_PROFILE,
    *,
    interaction_mode: InteractionMode | str | None = None,
    backend: Backend | None = None,
) -> Any:
    """Build an agent for ``profile``'s surface on the selected backend.

    The backend comes from ``FLOWJET_BACKEND`` (CLI) / ``FLOWJET_SERVER_BACKEND``
    (server) unless ``backend`` is passed explicitly — see
    :mod:`flowjet.core.backend`.

    The server uses a dual-mode agent and supplies the interaction mode per
    request through the LangGraph ``configurable`` dict; the CLI resolves the
    mode up front and passes it to the single-mode constructor.
    """
    from flowjet.core.modes import apply_profile

    mode = require_mode(interaction_mode, profile)
    resolved = backend or resolve_backend(profile.surface)
    cfg = apply_profile(config, profile, backend=resolved)

    if resolved is Backend.SOOTHE:
        return create_soothe_agent(cfg, profile, mode)

    if profile.surface is Surface.SERVER:
        from soothe_nano import create_dual_mode_nano_agent

        return create_dual_mode_nano_agent(cfg)

    from soothe_nano import create_nano_agent

    return create_nano_agent(cfg, interaction_mode=mode)


def soothe_agent_factory() -> Any:
    """Return ``soothe.coreagent.create_soothe_agent`` (imported lazily).

    Kept as a seam so the host runtime is imported only when it is actually
    selected — the ``nano`` backend never pays for it, and tests patch this
    instead of building a real host agent.
    """
    from soothe.coreagent import create_soothe_agent

    return create_soothe_agent


def create_soothe_agent(
    config: Any,
    profile: SurfaceProfile = CLI_PROFILE,
    mode: str | None = None,
) -> Any:
    """Build a ``soothe`` (host runtime) agent for ``profile``'s surface.

    ``interrupt_on`` is pinned to ``{}``: the host builder otherwise injects
    HITL interrupts for mutating tools (``edit_file`` / ``write_file`` /
    ``delete`` / ``run_command``), and FlowJet has no resume path — it renders
    ``InterruptWaiting`` and stops, so any write would stall the run.
    """
    factory = soothe_agent_factory()
    if profile.surface is Surface.SERVER:
        return ModeRoutedSootheAgent(config, factory)
    return factory(config, interaction_mode=mode, interrupt_on={})


class ModeRoutedSootheAgent:
    """Route one server request to the host agent built for its mode.

    soothe has no dual-mode API (nano's ``create_dual_mode_nano_agent`` has no
    host equivalent), so the server surface keeps one host agent per allowed
    mode and picks it from ``configurable["interaction_mode"]`` — an ``ask``
    request must never land on the ``agent`` graph. Agents are built lazily, so
    a worker that only serves ``ask`` never builds the ``agent`` graph.

    Only ``astream`` is proxied: that is the whole surface the nano bridge
    (``flowjet.core.bridges.nano.mapping``) drives.
    """

    def __init__(
        self,
        config: Any,
        factory: Any,
        modes: tuple[str, ...] = ("agent", "ask"),
    ) -> None:
        self._config = config
        self._factory = factory
        self._modes = modes
        self._agents: dict[str, Any] = {}

    def _agent_for(self, mode: str) -> Any:
        resolved = mode if mode in self._modes else "agent"
        agent = self._agents.get(resolved)
        if agent is None:
            agent = self._factory(self._config, interaction_mode=resolved, interrupt_on={})
            self._agents[resolved] = agent
        return agent

    async def astream(self, payload: Any, **kwargs: Any) -> Any:
        """Proxy ``astream`` to the agent built for the request's mode."""
        config = kwargs.get("config") or {}
        configurable = config.get("configurable") or {} if isinstance(config, dict) else {}
        mode = str(configurable.get("interaction_mode") or "agent")
        agent = self._agent_for(mode)
        async for chunk in agent.astream(payload, **kwargs):
            yield chunk
