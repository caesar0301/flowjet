"""Tests for the FlowJet identity override over soothe-nano's default persona."""

from __future__ import annotations

from soothe_nano.config import SootheConfig

from flowjet.cli.agent import apply_fj_defaults
from flowjet.core.identity import (
    FLOWJET_ASSISTANT_NAME,
    FLOWJET_IDENTITY_FRAGMENT,
    install_flowjet_identity,
)
from flowjet.core.modes import SERVER_PROFILE, apply_profile

_NANO_WORDING = "a helpful AI assistant invented by"


def test_identity_fragment_replaces_nano_template() -> None:
    import soothe_nano.prompts.fragments as fragments
    import soothe_nano.prompts.identity as identity

    assert install_flowjet_identity() is True
    assert identity.ASSISTANT_IDENTITY_FRAGMENT == FLOWJET_IDENTITY_FRAGMENT
    assert fragments.ASSISTANT_IDENTITY_FRAGMENT == FLOWJET_IDENTITY_FRAGMENT
    assert _NANO_WORDING not in FLOWJET_IDENTITY_FRAGMENT


def test_cli_prompt_identifies_as_flowjet() -> None:
    prompt = apply_fj_defaults(SootheConfig()).resolve_system_prompt()

    assert "<ASSISTANT_IDENTITY>" in prompt
    assert f"You are {FLOWJET_ASSISTANT_NAME}," in prompt
    assert "invented by Dr. Xiaming Chen" in prompt
    assert _NANO_WORDING not in prompt
    assert "Never identify as Soothe" in prompt


def test_agent_name_defaults_to_flowjet() -> None:
    assert apply_fj_defaults(SootheConfig()).agent.name == FLOWJET_ASSISTANT_NAME


def test_explicit_agent_name_wins() -> None:
    cfg = SootheConfig(agent={"name": "Acme"})

    forced = apply_fj_defaults(cfg)

    assert forced.agent.name == "Acme"
    assert "You are Acme," in forced.resolve_system_prompt()
    assert "Never identify as Soothe" in forced.resolve_system_prompt()


def test_explicit_system_prompt_is_preserved() -> None:
    cfg = SootheConfig(agent={"system_prompt": "CUSTOM BODY {assistant_name}"})

    forced = apply_fj_defaults(cfg)

    prompt = forced.resolve_system_prompt()
    assert "CUSTOM BODY FlowJet" in prompt
    assert _NANO_WORDING not in prompt


def test_server_profile_identifies_as_flowjet() -> None:
    forced = apply_profile(SootheConfig(), SERVER_PROFILE)

    assert forced.agent.name == FLOWJET_ASSISTANT_NAME
    assert "You are FlowJet," in forced.resolve_system_prompt()
