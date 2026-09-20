"""``FLOWJET_HOME`` resolution for the config file.

FlowJet reads its config from ``$FLOWJET_HOME/config/nano.yml`` (default
``~/.flowjet/config/nano.yml``).  ``FLOWJET_HOME`` is the only authority for that
path; ``SOOTHE_HOME`` does not affect it, and soothe-owned state — CLI data,
logs, agents, plugins, sqlite databases — stays where the soothe runtime puts it.

This module is deliberately a stdlib-only leaf: it is imported from argparse
help builders (``fj -c``), so it must stay cheap, and it must not do file I/O
during import or inside :func:`default_config_path`.

:func:`ensure_flowjet_config` copies an existing ``~/.soothe/config`` into the
flowjet home once, leaving the originals in place.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_FLOWJET_HOME = "~/.flowjet"

# Config filenames that make up a runnable flowjet setup. ``nano.yml`` is the
# provider/router file the CLI and server read; ``soothe.yml`` is the optional
# host-owned sibling composed over it on the ``soothe`` backend.
_CONFIG_FILENAMES = ("nano.yml", "soothe.yml")


def flowjet_home() -> Path:
    """Return ``$FLOWJET_HOME`` (default ``~/.flowjet``), expanded.

    Read at call time, so callers can set the env var late.
    """
    raw = os.environ.get("FLOWJET_HOME") or DEFAULT_FLOWJET_HOME
    return Path(raw).expanduser()


def legacy_soothe_home() -> Path:
    """Return ``~/.soothe``, the home :func:`ensure_flowjet_config` copies from.

    Not env-driven: the source of a copy is a fixed location, not a redirect.
    """
    return Path.home() / ".soothe"


def default_config_path() -> Path:
    """Return ``$FLOWJET_HOME/config/nano.yml``.

    Pure — no directory creation, no existence checks — because argparse help
    builders call it while rendering ``-h``.
    """
    return flowjet_home() / "config" / "nano.yml"


def migrate_legacy_config(
    *,
    legacy_home: Path | None = None,
    target_home: Path | None = None,
) -> list[str]:
    """Copy ``~/.soothe/config`` into the flowjet home, once.

    No-op unless the target ``nano.yml`` is missing and the source has one, so an
    existing flowjet config is never overwritten and this is safe to call on
    every start.  Originals are left in place.  Returns the copied filenames.
    """
    legacy = Path(legacy_home) if legacy_home is not None else legacy_soothe_home()
    target = Path(target_home) if target_home is not None else flowjet_home()

    try:
        if legacy.resolve() == target.resolve():
            return []
        legacy_config = legacy / "config"
        target_config = target / "config"
        if not (legacy_config / "nano.yml").is_file():
            return []
        if (target_config / "nano.yml").is_file():
            return []

        copied: list[str] = []
        for name in _CONFIG_FILENAMES:
            source = legacy_config / name
            if not source.is_file():
                continue
            if _copy_atomic(source, target_config / name):
                copied.append(name)
    except OSError as exc:
        logger.warning("could not migrate legacy config from %s: %s", legacy, exc)
        return []
    return copied


def ensure_flowjet_config(
    *,
    legacy_home: Path | None = None,
    target_home: Path | None = None,
) -> list[str]:
    """Copy a soothe-home config into the flowjet home, reporting it on stderr.

    Idempotent: returns the copied filenames, empty when there was nothing to copy.
    """
    copied = migrate_legacy_config(legacy_home=legacy_home, target_home=target_home)
    if copied:
        source = Path(legacy_home) if legacy_home is not None else legacy_soothe_home()
        target = Path(target_home) if target_home is not None else flowjet_home()
        names = ", ".join(copied)
        sys.stderr.write(f"Copied {names} from {source / 'config'} to {target / 'config'}\n")
        sys.stderr.write("The original soothe config was left in place.\n")
    return copied


def _copy_atomic(source: Path, dest: Path) -> bool:
    """Copy ``source`` to ``dest`` via a temp file + ``os.replace``.

    Concurrent callers — server pool workers reaching this through
    ``load_config`` — must never observe a half-written destination.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".{dest.name}.tmp-{os.getpid()}"
        shutil.copyfile(source, tmp)
        os.replace(tmp, dest)
    except OSError as exc:
        logger.warning("could not copy %s to %s: %s", source, dest, exc)
        return False
    return True
