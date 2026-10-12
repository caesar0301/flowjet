"""Phase 2 — workspace sync (fsspec) integration tests.

Tests the ``SyncAwareWorkspaceResolver`` lifecycle hooks and the
``IsolatingRuntimeBackend`` integration with an injectable resolver.
Uses an in-memory fsspec filesystem (``MemoryFileSystem``) so no cloud
credentials or network are needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from flowjet.core.backends.isolation import (
    FakeAgentAdapter,
    IsolatedRunRequest,
    IsolatingRuntimeBackend,
    PoolSettings,
    SyncAwareWorkspaceResolver,
    WorkspaceResolver,
)
from flowjet.core.events import RunCompleted, RunRequest, RunStarted

# ---------------------------------------------------------------------------
# WorkspaceResolver lifecycle hooks (base class — Phase 1 no-ops)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_resolver_open_close_are_noops(tmp_path: Path):
    """Base WorkspaceResolver lifecycle hooks are no-ops (no sync)."""
    resolver = WorkspaceResolver(tmp_path)
    path = resolver.resolve("session-x")
    # These should not raise and should return None.
    await resolver.open_session("session-x", path)
    await resolver.close_session("session-x", path)


# ---------------------------------------------------------------------------
# SyncAwareWorkspaceResolver — disabled mode (no sync_source)
# ---------------------------------------------------------------------------


def test_sync_resolver_without_source_delegates_to_base(tmp_path: Path):
    """Without sync_source, SyncAwareWorkspaceResolver behaves like base."""
    resolver = SyncAwareWorkspaceResolver(tmp_path, sync_source=None)
    assert resolver.is_sync_enabled is False

    path = resolver.resolve("session-a")
    assert path.is_dir()
    assert path.parent == tmp_path / "data" / "workspaces"


@pytest.mark.asyncio
async def test_sync_resolver_without_source_hooks_are_noops(tmp_path: Path):
    """Without sync_source, lifecycle hooks are no-ops."""
    resolver = SyncAwareWorkspaceResolver(tmp_path, sync_source=None)
    path = resolver.resolve("session-a")
    await resolver.open_session("session-a", path)
    await resolver.close_session("session-a", path)
    assert resolver.is_sync_enabled is False


# ---------------------------------------------------------------------------
# SyncAwareWorkspaceResolver — enabled mode (with mock WorkspaceManager)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_resolver_open_session_materializes_and_tracks(tmp_path: Path):
    """open_session calls manager.open + workspace.start_tracking."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )

    mock_ws = MagicMock()
    mock_ws.start_tracking = AsyncMock()
    mock_manager = MagicMock()
    mock_manager.open = AsyncMock(return_value=mock_ws)
    # Inject the mock manager directly.
    resolver._manager = mock_manager

    path = resolver.resolve("session-sync-1")
    await resolver.open_session("session-sync-1", path)

    # manager.open should have been called with the hashed run_id.
    assert mock_manager.open.await_count == 1
    called_run_id = mock_manager.open.call_args[0][0]
    assert called_run_id.startswith("ws_")
    # start_tracking should have been called.
    mock_ws.start_tracking.assert_awaited_once()
    # The workspace should be tracked in _open_workspaces.
    assert called_run_id in resolver._open_workspaces


@pytest.mark.asyncio
async def test_sync_resolver_close_session_checkpoints_and_closes(tmp_path: Path):
    """close_session calls workspace.checkpoint + manager.close."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )

    mock_ws = MagicMock()
    mock_ws.checkpoint = AsyncMock(return_value="ckpt-1")
    mock_ws.start_tracking = AsyncMock()
    mock_manager = MagicMock()
    mock_manager.open = AsyncMock(return_value=mock_ws)
    mock_manager.close = AsyncMock()
    resolver._manager = mock_manager

    path = resolver.resolve("session-sync-2")
    await resolver.open_session("session-sync-2", path)
    await resolver.close_session("session-sync-2", path)

    mock_ws.checkpoint.assert_awaited_once()
    mock_manager.close.assert_awaited_once()
    # After close, the workspace should be removed from _open_workspaces.
    assert len(resolver._open_workspaces) == 0


@pytest.mark.asyncio
async def test_sync_resolver_close_session_swallows_checkpoint_errors(tmp_path: Path):
    """close_session should not raise if checkpoint fails."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )

    mock_ws = MagicMock()
    mock_ws.checkpoint = AsyncMock(side_effect=RuntimeError("boom"))
    mock_ws.start_tracking = AsyncMock()
    mock_manager = MagicMock()
    mock_manager.open = AsyncMock(return_value=mock_ws)
    mock_manager.close = AsyncMock()
    resolver._manager = mock_manager

    path = resolver.resolve("session-err")
    await resolver.open_session("session-err", path)
    # Should not raise.
    await resolver.close_session("session-err", path)
    mock_manager.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_resolver_close_without_open_is_safe(tmp_path: Path):
    """close_session on a session that was never opened should not raise."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )
    mock_manager = MagicMock()
    mock_manager.close = AsyncMock()
    resolver._manager = mock_manager

    path = resolver.resolve("session-noopen")
    await resolver.close_session("session-noopen", path)
    mock_manager.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_resolver_open_session_idempotent(tmp_path: Path):
    """open_session on an already-open session should not re-open."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )

    mock_ws = MagicMock()
    mock_ws.start_tracking = AsyncMock()
    mock_manager = MagicMock()
    mock_manager.open = AsyncMock(return_value=mock_ws)
    resolver._manager = mock_manager

    path = resolver.resolve("session-dup")
    await resolver.open_session("session-dup", path)
    await resolver.open_session("session-dup", path)

    # Should only open once.
    assert mock_manager.open.await_count == 1


@pytest.mark.asyncio
async def test_sync_resolver_shutdown_closes_all(tmp_path: Path):
    """shutdown calls manager.close_all and clears the open set."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )

    mock_ws = MagicMock()
    mock_ws.start_tracking = AsyncMock()
    mock_ws.checkpoint = AsyncMock()
    mock_manager = MagicMock()
    mock_manager.open = AsyncMock(return_value=mock_ws)
    mock_manager.close = AsyncMock()
    mock_manager.close_all = AsyncMock()
    resolver._manager = mock_manager

    path = resolver.resolve("session-shutdown")
    await resolver.open_session("session-shutdown", path)
    await resolver.shutdown()

    mock_manager.close_all.assert_awaited_once()
    assert len(resolver._open_workspaces) == 0


@pytest.mark.asyncio
async def test_sync_resolver_shutdown_without_manager_is_noop(tmp_path: Path):
    """shutdown without a manager should not raise."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )
    # _manager is None — shutdown should be a no-op.
    await resolver.shutdown()


# ---------------------------------------------------------------------------
# SyncAwareWorkspaceResolver.from_settings
# ---------------------------------------------------------------------------


def test_from_settings_constructs_resolver(tmp_path: Path):
    """from_settings builds a resolver with all fields wired."""
    resolver = SyncAwareWorkspaceResolver.from_settings(
        home=tmp_path,
        cas_root=str(tmp_path / "cas"),
        sync_source="s3://bucket/flowjet",
        sync_config_path=None,
        allow_external_workspace=True,
    )
    assert isinstance(resolver, SyncAwareWorkspaceResolver)
    assert resolver._sync_source == "s3://bucket/flowjet"
    assert resolver._allow_external is True


# ---------------------------------------------------------------------------
# IsolatingRuntimeBackend — injectable resolver + lifecycle hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_calls_lifecycle_hooks(tmp_path: Path):
    """IsolatingRuntimeBackend calls open_session/close_session around each run."""
    open_calls: list[str] = []
    close_calls: list[str] = []

    class TrackingResolver(WorkspaceResolver):
        async def open_session(self, session: str, workspace: Path) -> None:
            open_calls.append(session)

        async def close_session(self, session: str, workspace: Path) -> None:
            close_calls.append(session)

    backend = IsolatingRuntimeBackend(
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=1),
        home=tmp_path,
        workspace_resolver=TrackingResolver(tmp_path),
    )
    try:
        events = [
            e
            async for e in backend.stream_run(
                RunRequest(
                    input_text="hello",
                    model="default",
                    session="fj-lifecycle",
                )
            )
        ]
        assert any(isinstance(e, RunStarted) for e in events)
        assert any(isinstance(e, RunCompleted) for e in events)
        assert open_calls == ["fj-lifecycle"]
        assert close_calls == ["fj-lifecycle"]
    finally:
        await backend.shutdown()


@pytest.mark.asyncio
async def test_backend_close_session_called_on_failure(tmp_path: Path):
    """close_session is called even when the run raises."""
    open_calls: list[str] = []
    close_calls: list[str] = []

    class TrackingResolver(WorkspaceResolver):
        async def open_session(self, session: str, workspace: Path) -> None:
            open_calls.append(session)

        async def close_session(self, session: str, workspace: Path) -> None:
            close_calls.append(session)

    class CrashAdapter:
        async def astream(self, req: IsolatedRunRequest):
            yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
            raise RuntimeError("agent crashed")

        def prepare_for_request(self) -> None:
            pass

        async def cleanup(self) -> None:
            pass

    backend = IsolatingRuntimeBackend(
        adapter_factory=CrashAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=1),
        home=tmp_path,
        workspace_resolver=TrackingResolver(tmp_path),
    )
    try:
        [
            e
            async for e in backend.stream_run(
                RunRequest(
                    input_text="boom",
                    model="default",
                    session="fj-crash",
                )
            )
        ]
        # open should have been called.
        assert open_calls == ["fj-crash"]
        # close should have been called even after the crash.
        assert close_calls == ["fj-crash"]
    finally:
        await backend.shutdown()


@pytest.mark.asyncio
async def test_backend_shutdown_calls_resolver_shutdown(tmp_path: Path):
    """Backend.shutdown calls resolver.shutdown if it exists."""
    shutdown_calls: list[bool] = []

    class ShutdownResolver(WorkspaceResolver):
        async def shutdown(self) -> None:
            shutdown_calls.append(True)

    backend = IsolatingRuntimeBackend(
        adapter_factory=FakeAgentAdapter,
        pool_settings=PoolSettings(min_size=1, max_size=1),
        home=tmp_path,
        workspace_resolver=ShutdownResolver(tmp_path),
    )
    await backend.shutdown()
    assert shutdown_calls == [True]


# ---------------------------------------------------------------------------
# Server config — workspace sync settings
# ---------------------------------------------------------------------------


def test_settings_workspace_sync_defaults():
    """Settings defaults for workspace sync are Phase 1 (disabled)."""
    from flowjet.server.config import Settings

    s = Settings()
    assert s.workspace_sync_source is None
    assert s.workspace_sync_config is None
    assert s.workspace_cas_root == "~/.flowjet/data/cas"


def test_settings_workspace_sync_from_env(monkeypatch):
    """Settings can be configured via env vars."""
    from flowjet.server.config import Settings

    monkeypatch.setenv("FLOWJET_WORKSPACE_SYNC_SOURCE", "s3://bucket/flowjet")
    monkeypatch.setenv("FLOWJET_WORKSPACE_CAS_ROOT", "/tmp/cas")
    s = Settings()
    assert s.workspace_sync_source == "s3://bucket/flowjet"
    assert s.workspace_cas_root == "/tmp/cas"


# ---------------------------------------------------------------------------
# build_backend — workspace resolver wiring
# ---------------------------------------------------------------------------


def test_build_backend_returns_none_resolver_when_no_sync(tmp_path: Path):
    """build_backend without sync_source passes workspace_resolver=None."""
    from flowjet.server.config import Settings
    from flowjet.server.http.app import _build_workspace_resolver

    s = Settings(home=str(tmp_path))
    resolver = _build_workspace_resolver(s)
    assert resolver is None


def test_build_backend_builds_sync_resolver_when_configured(tmp_path: Path, monkeypatch):
    """build_backend with sync_source constructs a SyncAwareWorkspaceResolver."""
    from flowjet.server.config import Settings
    from flowjet.server.http.app import _build_workspace_resolver

    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))
    monkeypatch.setenv("FLOWJET_WORKSPACE_SYNC_SOURCE", "s3://bucket/flowjet")
    monkeypatch.setenv("FLOWJET_WORKSPACE_CAS_ROOT", str(tmp_path / "cas"))
    s = Settings()
    resolver = _build_workspace_resolver(s)
    assert resolver is not None
    assert isinstance(resolver, SyncAwareWorkspaceResolver)
    assert resolver._sync_source == "s3://bucket/flowjet"
