"""Per-session workspace resolution (RFC-002 / RFC-003, Phase 2 sync)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any

from flowjet.core.backends.isolation.errors import IsolationError

logger = logging.getLogger(__name__)


def _sqlite_state_store_factory(*, loop_id: str, workspace_dir: Path) -> Any:
    """Build a SQLite workspace state store for a run.

    ``WorkspaceManager`` needs a state store to construct the background
    uploader; without it ``Workspace.start_tracking()`` crashes because the
    uploader is ``None``.
    """
    from soothe.workspace.state.sqlite import SqliteWorkspaceStateStore

    return SqliteWorkspaceStateStore(
        db_path=Path(workspace_dir) / ".workspace" / "state.db",
        loop_id=loop_id,
    )


class WorkspaceResolver:
    """Map session (+ optional metadata.workspace) to an absolute workspace path.

    The lifecycle hooks (``open_session`` / ``close_session``) are no-ops here
    — they exist so :class:`IsolatingRuntimeBackend` can call them uniformly
    whether the resolver is local-only or sync-backed (Phase 2).
    """

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

    async def open_session(self, session: str, workspace: Path) -> None:
        """Hook called before a run starts. No-op for local-only workspaces."""

    async def close_session(self, session: str, workspace: Path) -> None:
        """Hook called after a run completes. No-op for local-only workspaces."""

    def _is_under_home(self, path: Path) -> bool:
        try:
            path.relative_to(self._home)
            return True
        except ValueError:
            return path == self._home


class SyncAwareWorkspaceResolver(WorkspaceResolver):
    """Workspace resolver backed by soothe's fsspec workspace sync.

    When ``workspace_sync_source`` is set, sessions are materialized from /
    persisted to an fsspec-backed object store (S3/GCS/Azure) via
    ``soothe.workspace.sync.WorkspaceManager``. The resolver still returns a
    local :class:`~pathlib.Path` (the materialized workspace directory), so
    :class:`IsolatingRuntimeBackend` needs no change — only the lifecycle
    hooks differ.

    Lifecycle per session run::

        path = resolver.resolve(session, metadata)
        await resolver.open_session(session, path)  # materialize + start tracking
        # ... agent runs, writes to local filesystem ...
        await resolver.close_session(session, path)  # checkpoint + upload + close

    When ``workspace_sync_source`` is **not** set at construction time, the
    resolver delegates to the base ``WorkspaceResolver`` (local-only, Phase 1
    behavior) and the lifecycle hooks are no-ops.
    """

    def __init__(
        self,
        home: Path,
        *,
        allow_external_workspace: bool = False,
        sync_source: str | None = None,
        sync_config: str | None = None,
        cas_root: str | Path | None = None,
    ) -> None:
        super().__init__(home, allow_external_workspace=allow_external_workspace)
        self._sync_source = sync_source
        self._sync_config = sync_config
        self._cas_root = Path(cas_root).expanduser() if cas_root else self._home / "data" / "cas"
        self._manager: Any | None = None
        self._sync_backend: Any | None = None
        self._restore_cas: Any | None = None
        self._open_workspaces: dict[str, Any] = {}

    @property
    def is_sync_enabled(self) -> bool:
        """Return True when fsspec workspace sync is active."""
        return self._sync_source is not None and self._manager is not None

    def _ensure_manager(self) -> Any | None:
        """Lazily construct the WorkspaceManager (import + build backend)."""
        if self._sync_source is None:
            return None
        if self._manager is not None:
            return self._manager

        from soothe.workspace.sync import WorkspaceManager

        config = self._load_sync_config()
        backend = self._construct_sync_backend(config)
        self._sync_backend = backend
        workspaces_root = self._home / "data" / "workspaces"
        workspaces_root.mkdir(parents=True, exist_ok=True)
        self._cas_root.mkdir(parents=True, exist_ok=True)
        self._manager = WorkspaceManager(
            workspaces_root=workspaces_root,
            cas_root=self._cas_root,
            backend=backend,
            state_store_factory=_sqlite_state_store_factory,
        )
        logger.info(
            "SyncAwareWorkspaceResolver initialized: source=%s, cas=%s",
            self._sync_source,
            self._cas_root,
        )
        return self._manager

    def _construct_sync_backend(self, config: dict[str, Any] | None) -> Any:
        """Build the fsspec sync backend for ``_sync_source``.

        Cloud schemes (s3/gs/az) go through soothe's guarded
        ``construct_sync_backend`` factory. ``file://`` URIs are built
        directly via fsspec (LocalFileSystem) — the factory's SSRF
        allowlist intentionally excludes local paths, but a local object
        store is a supported target for single-node deployments and tests.
        """
        scheme = self._sync_source.split("://", 1)[0].lower()
        if scheme == "file":
            import fsspec
            from soothe.workspace.sync.backends.fsspec import FsspecSyncBackend

            fs, root = fsspec.url_to_fs(self._sync_source)
            return FsspecSyncBackend(fs=fs, root=str(root))

        from soothe.workspace.sync import construct_sync_backend

        return construct_sync_backend(self._sync_source, config=config)

    def _load_sync_config(self) -> dict[str, Any] | None:
        """Load optional sync config JSON (credentials, endpoint, etc.)."""
        if self._sync_config is None:
            return None
        import json

        path = Path(self._sync_config).expanduser()
        with path.open() as f:
            return json.load(f)

    def resolve(self, session: str, metadata: dict[str, Any] | None = None) -> Path:
        """Resolve workspace path — delegates to base when sync is disabled."""
        if self._sync_source is None:
            return super().resolve(session, metadata)
        # When sync is enabled, use a deterministic session-keyed path under
        # the workspaces root so WorkspaceManager.open() can find it.
        digest = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
        path = self._home / "data" / "workspaces" / f"ws_{digest}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def open_session(self, session: str, workspace: Path) -> None:
        """Materialize workspace from object store + start dirty tracking."""
        manager = self._ensure_manager()
        if manager is None:
            return

        # Use the session hash as the run_id key for WorkspaceManager.
        digest = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
        run_id = f"ws_{digest}"

        # If a workspace is already open for this session (reuse), skip.
        if run_id in self._open_workspaces:
            return

        ws = await manager.open(run_id)
        await self._restore_prior_state(ws)
        self._open_workspaces[run_id] = ws

        # Start dirty tracking + background uploader for incremental persistence.
        await ws.start_tracking()
        logger.info("Opened sync workspace for session %s at %s", session, workspace)

    async def _restore_prior_state(self, ws: Any) -> None:
        """Recover the prior manifest and materialize its files locally.

        Round-trip half of workspace sync: ``close_session`` checkpoints and
        uploads dirty files; this restores them so a fresh server process (or
        an evicted session workspace) sees the agent's prior outputs.

        - Agent-written files live in ``manifest.artifacts`` (path → sha256,
          blobs under ``<root>/blobs/``) and are restored in place at their
          recorded relative paths under the workspace root.
        - Declared input resources (``manifest.resources``) are materialized
          under ``<root>/input/`` via the soothe Materializer.
        """
        payload = await ws.recover()  # restores checkpoint state, returns snapshot payload
        manifest = payload.manifest_snapshot if payload is not None else None
        if manifest is None:
            return

        restored = 0
        for entry in manifest.artifacts:
            rel = entry.path
            if (
                rel.startswith(".workspace/")
                or rel.startswith("__pycache__/")
                or rel.startswith(".git/")
            ):
                continue
            dest = ws.root / rel
            if dest.is_file() and not dest.is_symlink():
                import hashlib as _hashlib

                existing = await asyncio.to_thread(dest.read_bytes)
                if _hashlib.sha256(existing).hexdigest() == entry.sha256:
                    continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            cas = await self._restore_cas_handle()
            blob_path = await cas.fetch_and_cache(entry.sha256, self._sync_backend)
            if blob_path is None:
                logger.warning("Sync restore: blob missing for %s (%s)", rel, entry.sha256[:12])
                continue
            await asyncio.to_thread(shutil.copyfile, blob_path, dest)
            restored += 1

        if manifest.resources:
            # Materializer writes declared resources under <root>/input/.
            await ws.materialize(manifest)

        if restored or manifest.resources:
            logger.info(
                "Sync restore for run %s: %d artifacts materialized, %d input resources",
                ws.run_id,
                restored,
                len(manifest.resources),
            )

    async def _restore_cas_handle(self) -> Any:
        """Lazily build a CAS cache for artifact restore (host-side, outside ws)."""
        if self._restore_cas is None:
            from soothe.workspace.sync.cas import CASCache

            self._restore_cas = CASCache(cache_root=self._cas_root)
        return self._restore_cas

    async def close_session(self, session: str, workspace: Path) -> None:
        """Checkpoint dirty files + upload to object store + close workspace."""
        if self._manager is None:
            return

        digest = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
        run_id = f"ws_{digest}"

        ws = self._open_workspaces.pop(run_id, None)
        if ws is None:
            return

        try:
            await ws.checkpoint()
        except Exception:
            logger.warning("Checkpoint failed for session %s", session, exc_info=True)
        try:
            await self._manager.close(run_id)
        except Exception:
            logger.warning("Workspace close failed for session %s", session, exc_info=True)

    async def shutdown(self) -> None:
        """Close all open workspaces on server shutdown."""
        if self._manager is None:
            return
        await self._manager.close_all()
        self._open_workspaces.clear()

    @classmethod
    def from_settings(
        cls,
        *,
        home: Path,
        cas_root: str | Path,
        sync_source: str,
        sync_config_path: str | None = None,
        allow_external_workspace: bool = False,
    ) -> SyncAwareWorkspaceResolver:
        """Build a resolver from server settings (called by ``build_backend``)."""
        return cls(
            home,
            allow_external_workspace=allow_external_workspace,
            sync_source=sync_source,
            sync_config=sync_config_path,
            cas_root=cas_root,
        )
