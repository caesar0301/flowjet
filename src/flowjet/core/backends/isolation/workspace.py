"""Per-session workspace resolution (RFC-002 / RFC-003)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from flowjet.core.backends.isolation.errors import IsolationError


class WorkspaceResolver:
    """Map session (+ optional metadata.workspace) to an absolute workspace path."""

    def __init__(self, home: Path, *, allow_external_workspace: bool = False) -> None:
        self._home = Path(home).expanduser().resolve()
        self._allow_external = allow_external_workspace

    @property
    def home(self) -> Path:
        return self._home

    def resolve(self, session: str, metadata: dict[str, Any] | None = None) -> Path:
        meta = metadata or {}
        override = meta.get("workspace")
        if isinstance(override, str) and override.strip():
            path = Path(override.strip()).expanduser().resolve()
            if not self._allow_external and not self._is_under_home(path):
                raise IsolationError(
                    f"workspace override {path} is outside FLOWJET_HOME ({self._home}); "
                    "set FLOWJET_ALLOW_EXTERNAL_WORKSPACE=true to permit",
                    code="invalid_workspace",
                )
            path.mkdir(parents=True, exist_ok=True)
            return path

        digest = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
        path = self._home / "data" / "workspaces" / f"ws_{digest}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _is_under_home(self, path: Path) -> bool:
        try:
            path.relative_to(self._home)
            return True
        except ValueError:
            return path == self._home
