"""Unit tests for config loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from flowjet.cli.agent import default_config_path, load_config


def test_default_config_path_ends_with_nano_yml() -> None:
    path = default_config_path()
    assert path.name == "nano.yml"
    assert path.parts[-2] == "config"


def test_default_config_path_uses_flowjet_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import flowjet.core.bootstrap as config_mod

    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))
    assert config_mod.default_config_path() == tmp_path / "config" / "nano.yml"


def test_default_config_path_is_not_soothe_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FLOWJET_HOME owns the config file; SOOTHE_HOME must not redirect it."""
    import flowjet.core.bootstrap as config_mod

    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path / "flowjet"))
    monkeypatch.setenv("SOOTHE_HOME", str(tmp_path / "soothe"))
    assert config_mod.default_config_path() == tmp_path / "flowjet" / "config" / "nano.yml"


def test_load_config_missing_explicit_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yml")


def test_load_config_from_yaml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "nano.yml"
    cfg_path.write_text(
        yaml.dump(
            {
                "providers": [
                    {
                        "name": "local",
                        "provider_type": "openai",
                        "api_base_url": "http://127.0.0.1:9/v1",
                        "api_key": "local",
                        "models": ["test-model"],
                    }
                ],
                "router_profiles": [
                    {
                        "name": "default",
                        "router": {"default": "local:test-model"},
                    }
                ],
                "active_router_profile": "default",
                "persistence": {"default_backend": "sqlite"},
                "tools": {"wizsearch": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(cfg_path)
    assert config.active_router_profile == "default"
    assert config.resolve_checkpointer_backend() == "sqlite"
