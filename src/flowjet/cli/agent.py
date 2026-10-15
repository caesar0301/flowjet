"""CLI agent bootstrap and logging — thin surface over ``flowjet.core``.

Nano bootstrap, config loading and the builtin-skill root now live in
``flowjet.core.bootstrap``; this module keeps the CLI's historical names so
``fj``, the completion engine and the test-suite keep working unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from soothe_nano.config import SootheConfig

from flowjet.cli.cli import _CompactConsoleFormatter
from flowjet.core.bootstrap import (
    BUILTIN_SKILLS_DIR,
    FJ_CORE_SKILL_NAMES,
    create_agent,
    default_config_path,
    default_core_skill_names,
    ensure_workspace,
    load_config,
    open_sqlite_checkpointer,
    register_flowjet_builtin_skills,
)
from flowjet.core.modes import CLI_PROFILE, apply_profile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CLI logging (delegated to ``flowjet.cli.cli`` at call time)
# ---------------------------------------------------------------------------


def configure_cli_logging(*, verbose: bool = False) -> None:
    """Quiet the console for one-shot CLI use (see ``flowjet.cli.cli``).

    Delegated dynamically so monkeypatching ``flowjet.cli.cli.configure_cli_logging``
    (as tests do via ``monkeypatch.setattr(cli, ...)``) is honored here too.
    """
    from flowjet.cli import cli as _cli

    _cli.configure_cli_logging(verbose=verbose)


def silence_after_plugins(*, verbose: bool = False) -> None:
    """Re-apply quieting after plugin imports (belt-and-suspenders)."""
    configure_cli_logging(verbose=verbose)


# ---------------------------------------------------------------------------
# Builtin skills (re-exported; owned by flowjet.core.bootstrap)
# ---------------------------------------------------------------------------


def fj_core_skill_names() -> list[str]:
    """Nano default core skills plus FlowJet workflow skills."""
    return default_core_skill_names()


def register_fj_builtin_skills() -> None:
    """Register ``flowjet/builtin_skills`` as a soothe-nano builtin skill root."""
    register_flowjet_builtin_skills()


def apply_fj_defaults(config: SootheConfig) -> SootheConfig:
    """Apply the CLI surface profile to ``config``.

    - SQLite checkpointer / durability
    - Allow paths outside the workspace (this is the user's own machine)
    - Register FlowJet builtin skills
    - Core listing = nano defaults + FlowJet workflow skills (rest deferred)
    """
    return apply_profile(config, CLI_PROFILE)


# ---------------------------------------------------------------------------
# Agent build
# ---------------------------------------------------------------------------


async def build_agent(
    config: SootheConfig,
    *,
    workspace: Path | None = None,
    checkpointer: Any | None = None,
    verbose: bool = False,
    ask_mode: bool = False,
) -> Any:
    """Build a nano coding agent for the current workspace.

    The CLI runs on the user's own machine, so bypass mode (which skips all
    security enforcement layers) is the default. ``ask_mode`` switches to
    read-only ask mode instead.
    """
    if ask_mode:
        mode: str | None = "ask"
    else:
        mode = "bypass"
    configure_cli_logging(verbose=verbose)
    ensure_workspace(workspace)
    agent = create_agent(config, CLI_PROFILE, interaction_mode=mode)
    # Plugin imports (e.g. browser_use) may still attach root console handlers.
    silence_after_plugins(verbose=verbose)
    if checkpointer is not None:
        agent.graph.checkpointer = checkpointer
    return agent


__all__ = [
    "BUILTIN_SKILLS_DIR",
    "FJ_CORE_SKILL_NAMES",
    "_CompactConsoleFormatter",
    "apply_fj_defaults",
    "build_agent",
    "configure_cli_logging",
    "default_config_path",
    "ensure_workspace",
    "fj_core_skill_names",
    "load_config",
    "open_sqlite_checkpointer",
    "register_fj_builtin_skills",
    "silence_after_plugins",
]
