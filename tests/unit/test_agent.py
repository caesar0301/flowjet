"""Tests for fj runtime config defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from soothe_nano.config import SootheConfig

from flowjet.cli.agent import apply_fj_defaults, ensure_workspace


def test_apply_fj_defaults_forces_sqlite() -> None:
    cfg = SootheConfig()
    cfg = cfg.model_copy(
        update={
            "persistence": cfg.persistence.model_copy(update={"default_backend": "postgresql"}),
        }
    )
    assert cfg.resolve_checkpointer_backend() == "postgresql"
    forced = apply_fj_defaults(cfg)
    assert forced.resolve_checkpointer_backend() == "sqlite"


def test_apply_fj_defaults_sets_core_skills_when_unset() -> None:
    from flowjet.cli.agent import fj_core_skill_names

    cfg = SootheConfig()
    assert cfg.progressive_skills.core_skills is None
    forced = apply_fj_defaults(cfg)
    assert forced.progressive_skills.core_skills == fj_core_skill_names()
    # Nano defaults are included via DEFAULT_CORE_SKILL_NAMES, not hard-coded in fj.
    assert "weather" in forced.progressive_skills.core_skills


def test_apply_fj_defaults_preserves_explicit_core_skills() -> None:
    cfg = SootheConfig()
    cfg = cfg.model_copy(
        update={
            "progressive_skills": cfg.progressive_skills.model_copy(
                update={"core_skills": ["xlsx"]}
            ),
        }
    )
    forced = apply_fj_defaults(cfg)
    assert forced.progressive_skills.core_skills == ["xlsx"]


def test_apply_fj_defaults_disables_virtual_mode() -> None:
    cfg = SootheConfig()
    assert cfg.security.allow_paths_outside_workspace is False
    forced = apply_fj_defaults(cfg)
    assert forced.security.allow_paths_outside_workspace is True
    # Nano derives: virtual_mode = not allow_paths_outside_workspace
    virtual_mode = not forced.security.allow_paths_outside_workspace
    assert virtual_mode is False


def test_ensure_workspace_sets_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SOOTHE_WORKSPACE", raising=False)
    root = ensure_workspace(tmp_path)
    assert root == tmp_path.resolve()
    import os

    assert os.environ["SOOTHE_WORKSPACE"] == str(root)


@pytest.mark.asyncio
async def test_open_sqlite_checkpointer_yields_none_when_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import flowjet.core.bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod, "resolve_checkpointer", lambda _cfg: None)
    async with bootstrap_mod.open_sqlite_checkpointer(SootheConfig()) as cp:
        assert cp is None


@pytest.mark.asyncio
async def test_open_sqlite_checkpointer_opens_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import flowjet.core.bootstrap as bootstrap_mod

    db_path = tmp_path / "checkpoints.db"
    monkeypatch.setattr(
        bootstrap_mod, "resolve_checkpointer", lambda _cfg: (object(), str(db_path))
    )

    class FakeSaver:
        def __init__(self, conn: object, serde: object = None) -> None:
            self.conn = conn
            self.setup_called = False

        async def setup(self) -> None:
            self.setup_called = True

    class FakeConn:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    fake_conn = FakeConn()
    savers: list[FakeSaver] = []

    async def fake_connect(_path: str) -> FakeConn:
        return fake_conn

    def fake_saver(conn: object, serde: object = None) -> FakeSaver:
        saver = FakeSaver(conn, serde)
        savers.append(saver)
        return saver

    import aiosqlite
    import langgraph.checkpoint.sqlite.aio as aio_mod
    import soothe_sdk.utils.serde as serde_mod

    monkeypatch.setattr(aiosqlite, "connect", fake_connect)
    monkeypatch.setattr(aio_mod, "AsyncSqliteSaver", fake_saver)
    monkeypatch.setattr(serde_mod, "create_soothe_serde", lambda: object())

    async with bootstrap_mod.open_sqlite_checkpointer(SootheConfig()) as cp:
        assert cp is savers[0]
        assert savers[0].setup_called is True
    assert fake_conn.closed is True


@pytest.mark.asyncio
async def test_build_agent_wires_checkpointer(monkeypatch: pytest.MonkeyPatch) -> None:
    import flowjet.cli.agent as agent_mod

    created: dict[str, object] = {}

    class FakeGraph:
        def __init__(self) -> None:
            self.checkpointer = None

    class FakeAgent:
        def __init__(self) -> None:
            self.graph = FakeGraph()

    def fake_create(_cfg: object, _profile: object = None, **_kwargs: object) -> FakeAgent:
        agent = FakeAgent()
        created["agent"] = agent
        return agent

    monkeypatch.setattr(agent_mod, "configure_cli_logging", lambda **_k: None)
    monkeypatch.setattr(agent_mod, "ensure_workspace", lambda _w=None: Path.cwd())
    monkeypatch.setattr(agent_mod, "create_agent", fake_create)
    monkeypatch.setattr(agent_mod, "silence_after_plugins", lambda **_k: None)

    cp = object()
    agent = await agent_mod.build_agent(SootheConfig(), checkpointer=cp, verbose=True)
    assert agent is created["agent"]
    assert agent.graph.checkpointer is cp


@pytest.mark.asyncio
async def test_build_agent_ask_mode_forwards_interaction_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ask_mode=True must forward interaction_mode="ask" to create_nano_agent.

    soothe-nano 1.1.1's builder now owns the ask-mode surface natively:
    read-only filesystem tools, ask policy profile, and ask system prompt.
    fj no longer re-implements these; it just selects the mode.
    """
    import flowjet.cli.agent as agent_mod

    captured: dict[str, object] = {}

    class FakeGraph:
        checkpointer = None

    class FakeAgent:
        def __init__(self) -> None:
            self.graph = FakeGraph()

    def fake_create(_cfg: object, _profile: object = None, **kwargs: object) -> FakeAgent:
        captured.update(kwargs)
        return FakeAgent()

    monkeypatch.setattr(agent_mod, "configure_cli_logging", lambda **_k: None)
    monkeypatch.setattr(agent_mod, "ensure_workspace", lambda _w=None: Path.cwd())
    monkeypatch.setattr(agent_mod, "create_agent", fake_create)
    monkeypatch.setattr(agent_mod, "silence_after_plugins", lambda **_k: None)

    await agent_mod.build_agent(SootheConfig(), ask_mode=True)
    # The native ask mode must be selected; no hand-built ask policy is forwarded.
    assert captured.get("interaction_mode") == "ask"
    assert captured.get("policy") is None


@pytest.mark.asyncio
async def test_build_agent_default_mode_auto_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ask_mode=False forwards interaction_mode=None so the mode auto-resolves.

    fj no longer forces a mode; passing ``None`` lets soothe-nano resolve from
    the config (default ``agent``) instead of pinning to bypass.
    """
    import flowjet.cli.agent as agent_mod

    captured: dict[str, object] = {}

    class FakeGraph:
        checkpointer = None

    class FakeAgent:
        def __init__(self) -> None:
            self.graph = FakeGraph()

    def fake_create(_cfg: object, _profile: object = None, **kwargs: object) -> FakeAgent:
        captured.update(kwargs)
        return FakeAgent()

    monkeypatch.setattr(agent_mod, "configure_cli_logging", lambda **_k: None)
    monkeypatch.setattr(agent_mod, "ensure_workspace", lambda _w=None: Path.cwd())
    monkeypatch.setattr(agent_mod, "create_agent", fake_create)
    monkeypatch.setattr(agent_mod, "silence_after_plugins", lambda **_k: None)

    await agent_mod.build_agent(SootheConfig(), ask_mode=False)
    mode = captured.get("interaction_mode")
    assert mode is None


# ---------------------------------------------------------------------------
# Ask mode
# ---------------------------------------------------------------------------


def test_apply_fj_defaults_preserves_sqlite_persistence_in_ask_context() -> None:
    """fj defaults force sqlite; ask mode no longer layers its own config on top,
    so this just re-checks the sqlite default is honored for the ask path too."""
    cfg = SootheConfig()
    cfg = cfg.model_copy(
        update={
            "persistence": cfg.persistence.model_copy(update={"default_backend": "postgresql"}),
        }
    )
    forced = apply_fj_defaults(cfg)
    assert forced.resolve_checkpointer_backend() == "sqlite"


# ---------------------------------------------------------------------------
# Bypass mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_bypass_mode_forwards_interaction_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bypass_mode=True must forward interaction_mode="bypass" to create_nano_agent.

    Bypass mode skips all security enforcement layers; the nano builder handles
    the policy profile and system prompt natively. fj only selects the mode.
    """
    import flowjet.cli.agent as agent_mod

    captured: dict[str, object] = {}

    class FakeGraph:
        checkpointer = None

    class FakeAgent:
        def __init__(self) -> None:
            self.graph = FakeGraph()

    def fake_create(_cfg: object, _profile: object = None, **kwargs: object) -> FakeAgent:
        captured.update(kwargs)
        return FakeAgent()

    monkeypatch.setattr(agent_mod, "configure_cli_logging", lambda **_k: None)
    monkeypatch.setattr(agent_mod, "ensure_workspace", lambda _w=None: Path.cwd())
    monkeypatch.setattr(agent_mod, "create_agent", fake_create)
    monkeypatch.setattr(agent_mod, "silence_after_plugins", lambda **_k: None)

    await agent_mod.build_agent(SootheConfig(), bypass_mode=True)
    assert captured.get("interaction_mode") == "bypass"
    assert captured.get("policy") is None


@pytest.mark.asyncio
async def test_build_agent_bypass_overrides_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both ask_mode and bypass_mode are set, bypass takes precedence."""
    import flowjet.cli.agent as agent_mod

    captured: dict[str, object] = {}

    class FakeGraph:
        checkpointer = None

    class FakeAgent:
        def __init__(self) -> None:
            self.graph = FakeGraph()

    def fake_create(_cfg: object, _profile: object = None, **kwargs: object) -> FakeAgent:
        captured.update(kwargs)
        return FakeAgent()

    monkeypatch.setattr(agent_mod, "configure_cli_logging", lambda **_k: None)
    monkeypatch.setattr(agent_mod, "ensure_workspace", lambda _w=None: Path.cwd())
    monkeypatch.setattr(agent_mod, "create_agent", fake_create)
    monkeypatch.setattr(agent_mod, "silence_after_plugins", lambda **_k: None)

    await agent_mod.build_agent(SootheConfig(), ask_mode=True, bypass_mode=True)
    assert captured.get("interaction_mode") == "bypass"
