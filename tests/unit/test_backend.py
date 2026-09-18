"""Backend selection — ``FLOWJET_BACKEND`` (CLI) / ``FLOWJET_SERVER_BACKEND`` (server).

The two surfaces default to different runtimes: the CLI builds one agent per
invocation and runs on the host runtime (``soothe``); the server needs a
dual-mode agent that soothe does not expose yet, so it stays on ``nano``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import soothe_nano
from soothe_nano.config import SootheConfig

from flowjet.core import bootstrap
from flowjet.core.backend import (
    CLI_BACKEND_ENV,
    SERVER_BACKEND_ENV,
    Backend,
    parse_backend,
    resolve_backend,
)
from flowjet.core.modes import CLI_PROFILE, SERVER_PROFILE, Surface

_NANO_YAML = """
providers:
  - name: local
    provider_type: openai
    api_base_url: "http://localhost:9642/v1"
    models:
      - demo-model
router_profiles:
  - name: default
    router:
      default: "local:demo-model"
active_router_profile: default
"""


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLI_BACKEND_ENV, raising=False)
    monkeypatch.delenv(SERVER_BACKEND_ENV, raising=False)


class _FakeAgent:
    """Records the kwargs it was built with and replays one astream chunk."""

    def __init__(self, mode: str | None) -> None:
        self.mode = mode
        self.streamed: list[dict] = []

    async def astream(self, payload, **kwargs):  # type: ignore[no-untyped-def]
        self.streamed.append(kwargs)
        yield ("ns", "messages", (payload, {}))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_defaults_are_soothe_for_cli_and_nano_for_server(
    _clean_env: None,
) -> None:
    assert resolve_backend(Surface.CLI) is Backend.SOOTHE
    assert resolve_backend(Surface.SERVER) is Backend.NANO


def test_env_switches_each_surface_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLI_BACKEND_ENV, "nano")
    monkeypatch.setenv(SERVER_BACKEND_ENV, "soothe")
    assert resolve_backend(Surface.CLI) is Backend.NANO
    assert resolve_backend(Surface.SERVER) is Backend.SOOTHE


def test_values_are_case_and_whitespace_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLI_BACKEND_ENV, "  SOOTHE ")
    assert resolve_backend(Surface.CLI) is Backend.SOOTHE


def test_resolve_backend_accepts_a_string_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SERVER_BACKEND_ENV, "nano")
    assert resolve_backend("server") is Backend.NANO


def test_unknown_value_is_a_hard_error() -> None:
    with pytest.raises(ValueError, match="FLOWJET_BACKEND"):
        parse_backend("claude", default=Backend.SOOTHE, env_name=CLI_BACKEND_ENV)


def test_empty_value_falls_back_to_the_default() -> None:
    assert parse_backend("   ", default=Backend.NANO, env_name=CLI_BACKEND_ENV) is Backend.NANO
    assert parse_backend(None, default=Backend.NANO, env_name=CLI_BACKEND_ENV) is Backend.NANO


def test_profile_backend_follows_the_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    from flowjet.core.backend import backend_for_profile

    monkeypatch.setenv(CLI_BACKEND_ENV, "nano")
    monkeypatch.setenv(SERVER_BACKEND_ENV, "nano")
    assert backend_for_profile(CLI_PROFILE) is Backend.NANO
    assert backend_for_profile(SERVER_PROFILE) is Backend.NANO


# ---------------------------------------------------------------------------
# Config class follows the backend
# ---------------------------------------------------------------------------


def test_load_config_nano_backend_returns_nano_config(tmp_path: Path) -> None:
    path = tmp_path / "nano.yml"
    path.write_text(_NANO_YAML)

    cfg = bootstrap.load_config(path, backend=Backend.NANO)

    assert isinstance(cfg, SootheConfig)
    assert cfg.active_router_profile == "default"


def test_load_config_soothe_backend_returns_host_config(tmp_path: Path) -> None:
    pytest.importorskip("soothe")
    from soothe.config import SootheConfig as HostSootheConfig

    path = tmp_path / "nano.yml"
    path.write_text(_NANO_YAML)

    cfg = bootstrap.load_config(path, backend=Backend.SOOTHE)

    # Host config is a superset of nano's (adds agent.loop / clarification / ...).
    assert isinstance(cfg, HostSootheConfig)
    assert cfg.active_router_profile == "default"
    assert hasattr(cfg.agent, "loop")


def test_load_config_soothe_composes_sibling_soothe_yml(tmp_path: Path) -> None:
    pytest.importorskip("soothe")

    (tmp_path / "nano.yml").write_text(_NANO_YAML)
    (tmp_path / "soothe.yml").write_text("agent:\n  loop:\n    max_step_retries: 7\n")

    cfg = bootstrap.load_config(tmp_path / "nano.yml", backend=Backend.SOOTHE)

    assert cfg.agent.loop.max_step_retries == 7


def test_default_config_follows_backend() -> None:
    pytest.importorskip("soothe")
    from soothe.config import SootheConfig as HostSootheConfig

    assert isinstance(bootstrap.default_config(Backend.NANO), SootheConfig)
    assert isinstance(bootstrap.default_config(Backend.SOOTHE), HostSootheConfig)


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def test_cli_nano_backend_uses_nano_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_create(cfg, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return "nano-agent"

    monkeypatch.setattr(soothe_nano, "create_nano_agent", fake_create)

    agent = bootstrap.create_agent(SootheConfig(), CLI_PROFILE, backend=Backend.NANO)

    assert agent == "nano-agent"
    assert captured == {"interaction_mode": None}


def test_cli_soothe_backend_disables_hitl_interrupts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host builder would otherwise interrupt on write/edit/run_command.

    FlowJet renders ``InterruptWaiting`` and stops — it has no resume path — so
    a mutating tool call would stall the run.
    """
    captured: dict = {}

    def factory(cfg, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return "soothe-agent"

    monkeypatch.setattr(bootstrap, "soothe_agent_factory", lambda: factory)

    agent = bootstrap.create_agent(
        SootheConfig(), CLI_PROFILE, backend=Backend.SOOTHE, interaction_mode="ask"
    )

    assert agent == "soothe-agent"
    assert captured["interaction_mode"] == "ask"
    assert captured["interrupt_on"] == {}


def test_server_nano_backend_uses_dual_mode_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def fake_create(cfg, **kwargs):  # type: ignore[no-untyped-def]
        called.append("dual")
        return "dual-agent"

    monkeypatch.setattr(soothe_nano, "create_dual_mode_nano_agent", fake_create)

    agent = bootstrap.create_agent(SootheConfig(), SERVER_PROFILE, backend=Backend.NANO)

    assert agent == "dual-agent"
    assert called == ["dual"]


def test_server_soothe_backend_returns_mode_router(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, _FakeAgent] = {}

    def factory(cfg, **kwargs):  # type: ignore[no-untyped-def]
        mode = str(kwargs.get("interaction_mode"))
        built[mode] = _FakeAgent(mode)
        return built[mode]

    monkeypatch.setattr(bootstrap, "soothe_agent_factory", lambda: factory)

    routed = bootstrap.create_agent(SootheConfig(), SERVER_PROFILE, backend=Backend.SOOTHE)

    # Lazy: nothing is built until a request arrives.
    assert isinstance(routed, bootstrap.ModeRoutedSootheAgent)
    assert built == {}


@pytest.mark.asyncio
async def test_server_soothe_routes_ask_to_the_ask_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, _FakeAgent] = {}

    def factory(cfg, **kwargs):  # type: ignore[no-untyped-def]
        mode = str(kwargs.get("interaction_mode"))
        built[mode] = _FakeAgent(mode)
        return built[mode]

    monkeypatch.setattr(bootstrap, "soothe_agent_factory", lambda: factory)
    routed = bootstrap.create_agent(SootheConfig(), SERVER_PROFILE, backend=Backend.SOOTHE)

    await _drain(routed, interaction_mode="ask")

    assert set(built) == {"ask"}

    await _drain(routed, interaction_mode="agent")
    assert set(built) == {"ask", "agent"}


@pytest.mark.asyncio
async def test_server_soothe_unknown_mode_falls_back_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, _FakeAgent] = {}

    def factory(cfg, **kwargs):  # type: ignore[no-untyped-def]
        mode = str(kwargs.get("interaction_mode"))
        built[mode] = _FakeAgent(mode)
        return built[mode]

    monkeypatch.setattr(bootstrap, "soothe_agent_factory", lambda: factory)
    routed = bootstrap.create_agent(SootheConfig(), SERVER_PROFILE, backend=Backend.SOOTHE)

    await _drain(routed, interaction_mode="bypass")

    assert set(built) == {"agent"}


async def _drain(routed: object, *, interaction_mode: str) -> None:
    chunks = [
        chunk
        async for chunk in routed.astream(  # type: ignore[attr-defined]
            {"messages": []},
            config={"configurable": {"interaction_mode": interaction_mode}},
            stream_mode=["messages"],
            subgraphs=True,
        )
    ]
    assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Persona ownership on the soothe backend
# ---------------------------------------------------------------------------


def test_soothe_backend_owns_the_persona_via_config(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("soothe")
    import soothe_nano.prompts.fragments as fragments
    import soothe_nano.prompts.identity as identity
    from soothe.config import SootheConfig as HostSootheConfig

    # soothe patches these module globals; restore them so other tests see the
    # FlowJet override rather than the host-rendered fragment.
    monkeypatch.setattr(fragments, "ASSISTANT_IDENTITY_FRAGMENT", "ORIGINAL")
    monkeypatch.setattr(identity, "ASSISTANT_IDENTITY_FRAGMENT", "ORIGINAL")

    from flowjet.core.identity import FLOWJET_CREATOR
    from flowjet.core.modes import apply_profile

    forced = apply_profile(HostSootheConfig(), CLI_PROFILE, backend=Backend.SOOTHE)

    assert forced.agent.assistant_identity.creator == FLOWJET_CREATOR
    assert "soothe-nano" in forced.agent.assistant_identity.vendor_denylist
    # soothe rendered the fragment from that config (single writer).
    assert FLOWJET_CREATOR in fragments.ASSISTANT_IDENTITY_FRAGMENT
    assert "Soothe" in fragments.ASSISTANT_IDENTITY_FRAGMENT


def test_soothe_backend_keeps_an_explicit_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("soothe")
    import soothe_nano.prompts.fragments as fragments
    import soothe_nano.prompts.identity as identity
    from soothe.config import SootheConfig as HostSootheConfig
    from soothe.config.models import AssistantIdentity

    from flowjet.core.modes import apply_profile

    monkeypatch.setattr(fragments, "ASSISTANT_IDENTITY_FRAGMENT", "ORIGINAL")
    monkeypatch.setattr(identity, "ASSISTANT_IDENTITY_FRAGMENT", "ORIGINAL")

    cfg = HostSootheConfig(agent={"assistant_identity": AssistantIdentity(creator="Acme Inc.")})
    forced = apply_profile(cfg, CLI_PROFILE, backend=Backend.SOOTHE)

    assert forced.agent.assistant_identity.creator == "Acme Inc."


def test_nano_config_is_left_to_the_fragment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``agent.assistant_identity`` on nano config → override stays in force."""
    import soothe_nano.prompts.fragments as fragments
    import soothe_nano.prompts.identity as nano_identity

    from flowjet.core import identity as flowjet_identity
    from flowjet.core.identity import FLOWJET_IDENTITY_FRAGMENT, install_flowjet_identity
    from flowjet.core.modes import apply_profile

    # install_flowjet_identity() is idempotent process-wide; reset it so this
    # test proves the nano path installs the FlowJet fragment itself.
    monkeypatch.setattr(flowjet_identity, "_INSTALLED", False)
    monkeypatch.setattr(fragments, "ASSISTANT_IDENTITY_FRAGMENT", "ORIGINAL")
    monkeypatch.setattr(nano_identity, "ASSISTANT_IDENTITY_FRAGMENT", "ORIGINAL")

    install_flowjet_identity()
    apply_profile(SootheConfig(), CLI_PROFILE, backend=Backend.NANO)

    assert fragments.ASSISTANT_IDENTITY_FRAGMENT == FLOWJET_IDENTITY_FRAGMENT
