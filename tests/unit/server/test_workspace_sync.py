"""Phase 2 — workspace sync (fsspec) integration tests.

Tests the ``SyncAwareWorkspaceResolver`` lifecycle hooks and the
``IsolatingRuntimeBackend`` integration with an injectable resolver.
Uses an in-memory fsspec filesystem (``MemoryFileSystem``) so no cloud
credentials or network are needed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
# Phase 2 — per-workspace run_id keyed by ACP cwd (triarch workspaceId)
# ---------------------------------------------------------------------------


def test_resolve_uses_workspace_id_from_cwd(tmp_path: Path):
    """A bare workspace id in the ACP cwd keys the workspace path/run_id."""
    resolver = SyncAwareWorkspaceResolver(tmp_path, sync_source="s3://b/p")
    path = resolver.resolve("session-1", {"workspace": "0f1e2d3c-4b5a-6978-87a6-b5c4d3e2f1a0"})
    assert path.name == "0f1e2d3c-4b5a-6978-87a6-b5c4d3e2f1a0"
    assert path.parent == tmp_path / "data" / "workspaces"


def test_resolve_same_workspace_id_across_sessions_shares_path(tmp_path: Path):
    """Different sessions with the same cwd wsId resolve to one workspace."""
    resolver = SyncAwareWorkspaceResolver(tmp_path, sync_source="s3://b/p")
    ws_id = "ws-alpha-123"
    p1 = resolver.resolve("session-1", {"workspace": ws_id})
    p2 = resolver.resolve("session-2", {"workspace": ws_id})
    assert p1 == p2


def test_resolve_falls_back_to_session_hash_for_path_cwd(tmp_path: Path):
    """Path-shaped cwd (desktop client project dir) is NOT a workspace id."""
    resolver = SyncAwareWorkspaceResolver(tmp_path, sync_source="s3://b/p")
    path = resolver.resolve("session-1", {"workspace": "/Users/x/project"})
    assert path.name.startswith("ws_")
    assert len(path.name) == len("ws_") + 16


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "..", ".", "has/slash", "a b", "-leading-dash", "x" * 129],
)
def test_resolve_falls_back_for_invalid_workspace_ids(tmp_path: Path, bad: str):
    """Ids failing the whitelist fall back to the session-hash workspace."""
    resolver = SyncAwareWorkspaceResolver(tmp_path, sync_source="s3://b/p")
    path = resolver.resolve("session-1", {"workspace": bad})
    assert path.name.startswith("ws_")


@pytest.mark.asyncio
async def test_open_close_keyed_by_workspace_id(tmp_path: Path):
    """open/close derive the run_id from the resolved path name (wsId)."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://b/p", cas_root=str(tmp_path / "cas")
    )
    mock_ws = MagicMock()
    mock_ws.recover = AsyncMock(return_value=None)
    mock_ws.manifest = None
    mock_ws.start_tracking = AsyncMock()
    mock_ws.checkpoint = AsyncMock()
    mock_manager = MagicMock()
    mock_manager.open = AsyncMock(return_value=mock_ws)
    mock_manager.close = AsyncMock()
    resolver._manager = mock_manager

    ws_id = "0f1e2d3c-4b5a-6978-87a6-b5c4d3e2f1a0"
    path = resolver.resolve("session-1", {"workspace": ws_id})
    await resolver.open_session("session-1", path)
    assert mock_manager.open.call_args[0][0] == ws_id
    await resolver.close_session("session-1", path)
    mock_ws.checkpoint.assert_awaited_once()
    assert mock_manager.close.call_args[0][0] == ws_id


@pytest.mark.asyncio
async def test_two_sessions_same_workspace_share_open_run(tmp_path: Path):
    """Concurrent sessions on one workspace reuse the same open Workspace."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://b/p", cas_root=str(tmp_path / "cas")
    )
    mock_ws = MagicMock()
    mock_ws.recover = AsyncMock(return_value=None)
    mock_ws.manifest = None
    mock_ws.start_tracking = AsyncMock()
    mock_ws.checkpoint = AsyncMock()
    mock_manager = MagicMock()
    mock_manager.open = AsyncMock(return_value=mock_ws)
    mock_manager.close = AsyncMock()
    resolver._manager = mock_manager

    ws_id = "shared-ws"
    p1 = resolver.resolve("session-a", {"workspace": ws_id})
    p2 = resolver.resolve("session-b", {"workspace": ws_id})
    await resolver.open_session("session-a", p1)
    await resolver.open_session("session-b", p2)
    # One open per run_id (workspace), not per session.
    assert mock_manager.open.await_count == 1


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
    mock_ws.recover = AsyncMock(return_value=None)
    mock_ws.manifest = None
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
    # The prior remote state must be recovered before tracking starts.
    mock_ws.recover.assert_awaited_once()
    # start_tracking should have been called.
    mock_ws.start_tracking.assert_awaited_once()
    # The workspace should be tracked in _open_workspaces.
    assert called_run_id in resolver._open_workspaces


@pytest.mark.asyncio
async def test_sync_resolver_open_session_restores_artifacts(tmp_path: Path):
    """open_session restores prior artifacts; missing blobs are skipped safely."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )

    ws_root = tmp_path / "data" / "workspaces" / "ws_x"
    ws_root.mkdir(parents=True)
    # An artifact whose local file already matches its hash → skipped.
    good = ws_root / "keep.txt"
    good.write_text("same")
    import hashlib as _hl

    sha_good = _hl.sha256(b"same").hexdigest()

    mock_ws = MagicMock()
    mock_ws.recover = AsyncMock(return_value=None)
    mock_ws.root = ws_root
    mock_ws.manifest = SimpleNamespace(
        artifacts=[
            SimpleNamespace(path="keep.txt", sha256=sha_good, size=4),
            SimpleNamespace(path="missing-blob.txt", sha256="f" * 64, size=9),
        ],
        resources=[],
    )
    mock_ws.start_tracking = AsyncMock()
    mock_manager = MagicMock()
    mock_manager.open = AsyncMock(return_value=mock_ws)
    resolver._manager = mock_manager
    # Restore CAS returns no blob for the missing artifact.
    restore_cas = MagicMock()
    restore_cas.fetch_and_cache = AsyncMock(return_value=None)
    resolver._restore_cas = restore_cas

    path = resolver.resolve("session-restore")
    await resolver.open_session("session-restore", path)

    mock_ws.recover.assert_awaited_once()
    mock_ws.start_tracking.assert_awaited_once()
    # The matching file was left untouched; no file for the missing blob.
    assert good.read_text() == "same"
    assert not (ws_root / "missing-blob.txt").exists()


@pytest.mark.asyncio
async def test_sync_resolver_close_session_checkpoints_and_closes(tmp_path: Path):
    """close_session calls workspace.checkpoint + manager.close."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path, sync_source="s3://bucket/pfx", cas_root=str(tmp_path / "cas")
    )

    mock_ws = MagicMock()
    mock_ws.checkpoint = AsyncMock(return_value="ckpt-1")
    mock_ws.recover = AsyncMock(return_value=None)
    mock_ws.manifest = None
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
    mock_ws.recover = AsyncMock(return_value=None)
    mock_ws.manifest = None
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
    mock_ws.recover = AsyncMock(return_value=None)
    mock_ws.manifest = None
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
    mock_ws.recover = AsyncMock(return_value=None)
    mock_ws.manifest = None
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
# Phase 3 — dual-source sync (content root materialization + mirror write-back)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_session_materializes_content_root(tmp_path: Path):
    """open_session pulls the content root into the workspace before state replay.

    Full chain with file:// backends: triarch-style materials land in the
    workspace directory the agent will run in, keyed by the ACP cwd wsId.
    """
    # "backend" uploads a material into the content root for ws-alpha.
    content_root = tmp_path / "content-roots" / "ws-alpha"
    (content_root / "materials" / "m1").mkdir(parents=True)
    (content_root / "materials" / "m1" / "notes.md").write_bytes(b"material v1")

    sync_root = tmp_path / "sync-roots"
    sync_root.mkdir()

    resolver = SyncAwareWorkspaceResolver(
        tmp_path,
        sync_source=f"file://{sync_root}",
        cas_root=str(tmp_path / "cas"),
        content_source=f"file://{tmp_path}/content-roots/{{workspace}}",
    )

    ws_id = "ws-alpha"
    path = resolver.resolve("session-1", {"workspace": ws_id})
    await resolver.open_session("session-1", path)

    # The material is visible at the agent-visible workspace path.
    assert (path / "materials" / "m1" / "notes.md").read_bytes() == b"material v1"
    await resolver.shutdown()


@pytest.mark.asyncio
async def test_checkpoint_mirrors_agent_outputs_to_content_root(tmp_path: Path):
    """close_session checkpoints dirty files back into the content root."""
    content_root = tmp_path / "content-roots" / "ws-beta"
    content_root.mkdir(parents=True)
    sync_root = tmp_path / "sync-roots"
    sync_root.mkdir()

    resolver = SyncAwareWorkspaceResolver(
        tmp_path,
        sync_source=f"file://{sync_root}",
        cas_root=str(tmp_path / "cas"),
        content_source=f"file://{tmp_path}/content-roots/{{workspace}}",
    )

    ws_id = "ws-beta"
    path = resolver.resolve("session-1", {"workspace": ws_id})
    await resolver.open_session("session-1", path)

    # The agent writes an output file into the workspace.
    out = path / "output" / "analysis.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"agent analysis")

    # close_session → checkpoint → dirty files mirrored to the content root.
    await resolver.close_session("session-1", path)
    assert (content_root / "output" / "analysis.md").read_bytes() == b"agent analysis"
    # And the sync state (manifest) exists under the sync root for ws-beta.
    assert (sync_root / "runs" / ws_id / "manifest.json").exists()
    await resolver.shutdown()


@pytest.mark.asyncio
async def test_content_materialization_failure_is_non_fatal(tmp_path: Path):
    """A failing content root does not break session open (sync continues)."""
    sync_root = tmp_path / "sync-roots"
    sync_root.mkdir()

    resolver = SyncAwareWorkspaceResolver(
        tmp_path,
        sync_source=f"file://{sync_root}",
        cas_root=str(tmp_path / "cas"),
        # Template without {workspace} → factory disabled → no materialize call,
        # open must still succeed.
        content_source="s3://nowhere/static-root",
    )

    path = resolver.resolve("session-1", {"workspace": "ws-gamma"})
    await resolver.open_session("session-1", path)  # must not raise
    await resolver.close_session("session-1", path)
    await resolver.shutdown()


def test_content_factory_requires_workspace_placeholder(tmp_path: Path):
    """A content template without {workspace} disables content mirroring."""
    resolver = SyncAwareWorkspaceResolver(
        tmp_path,
        sync_source="s3://b/p",
        content_source="s3://b/static",
    )
    assert resolver._content_backend_factory() is None


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
