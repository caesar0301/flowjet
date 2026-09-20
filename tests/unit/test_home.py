"""Unit tests for ``flowjet.home`` — config-path resolution and migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from flowjet.core.backend import Backend
from flowjet.core.bootstrap import load_config
from flowjet.home import (
    DEFAULT_FLOWJET_HOME,
    default_config_path,
    ensure_flowjet_config,
    flowjet_home,
    legacy_soothe_home,
    migrate_legacy_config,
)

_NANO_YML = """
providers:
  - name: local
    provider_type: openai
    api_base_url: "http://127.0.0.1:9/v1"
    api_key: local
    models: [test-model]
router_profiles:
  - name: default
    router:
      default: "local:test-model"
active_router_profile: default
"""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_flowjet_home_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLOWJET_HOME", raising=False)
    assert flowjet_home() == Path(DEFAULT_FLOWJET_HOME).expanduser()
    assert flowjet_home() == Path.home() / ".flowjet"


def test_flowjet_home_reads_env_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FLOWJET_HOME", raising=False)
    assert flowjet_home() != tmp_path
    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))
    assert flowjet_home() == tmp_path


def test_flowjet_home_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOWJET_HOME", "~/custom-flowjet")
    assert flowjet_home() == Path.home() / "custom-flowjet"


def test_flowjet_home_empty_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOWJET_HOME", "")
    assert flowjet_home() == Path.home() / ".flowjet"


def test_legacy_soothe_home_ignores_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The copy source is a fixed location — a redirect must not move it."""
    monkeypatch.setenv("SOOTHE_HOME", str(tmp_path / "elsewhere"))
    assert legacy_soothe_home() == Path.home() / ".soothe"


def test_default_config_path_uses_flowjet_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "config" / "nano.yml"


def test_default_config_path_ignores_soothe_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: SOOTHE_HOME must not affect the config path."""
    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path / "flowjet"))
    monkeypatch.setenv("SOOTHE_HOME", str(tmp_path / "soothe"))
    assert default_config_path() == tmp_path / "flowjet" / "config" / "nano.yml"


def test_default_config_path_does_no_io(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Called by argparse help builders, so it must not touch the filesystem."""
    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path / "nope"))
    assert default_config_path().name == "nano.yml"
    assert not (tmp_path / "nope").exists()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _seed_legacy(root: Path, *names: str) -> Path:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    for name in names:
        (config / name).write_text(f"# {name}\n", encoding="utf-8")
    return root


def test_migrate_copies_nano_and_soothe(tmp_path: Path) -> None:
    legacy = _seed_legacy(tmp_path / "soothe", "nano.yml", "soothe.yml")
    target = tmp_path / "flowjet"

    copied = migrate_legacy_config(legacy_home=legacy, target_home=target)

    assert copied == ["nano.yml", "soothe.yml"]
    assert (target / "config" / "nano.yml").read_text(encoding="utf-8") == "# nano.yml\n"
    assert (target / "config" / "soothe.yml").read_text(encoding="utf-8") == "# soothe.yml\n"
    # Non-destructive: the originals stay.
    assert (legacy / "config" / "nano.yml").is_file()
    assert (legacy / "config" / "soothe.yml").is_file()


def test_migrate_copies_nano_without_soothe_sibling(tmp_path: Path) -> None:
    legacy = _seed_legacy(tmp_path / "soothe", "nano.yml")
    target = tmp_path / "flowjet"

    assert migrate_legacy_config(legacy_home=legacy, target_home=target) == ["nano.yml"]
    assert not (target / "config" / "soothe.yml").exists()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    legacy = _seed_legacy(tmp_path / "soothe", "nano.yml", "soothe.yml")
    target = tmp_path / "flowjet"

    assert migrate_legacy_config(legacy_home=legacy, target_home=target) == [
        "nano.yml",
        "soothe.yml",
    ]
    assert migrate_legacy_config(legacy_home=legacy, target_home=target) == []


def test_migrate_never_overwrites_existing_target(tmp_path: Path) -> None:
    legacy = _seed_legacy(tmp_path / "soothe", "nano.yml")
    target = tmp_path / "flowjet"
    (target / "config").mkdir(parents=True)
    (target / "config" / "nano.yml").write_text("# mine\n", encoding="utf-8")

    assert migrate_legacy_config(legacy_home=legacy, target_home=target) == []
    assert (target / "config" / "nano.yml").read_text(encoding="utf-8") == "# mine\n"


def test_migrate_noop_without_legacy_nano(tmp_path: Path) -> None:
    legacy = _seed_legacy(tmp_path / "soothe", "soothe.yml")
    target = tmp_path / "flowjet"

    assert migrate_legacy_config(legacy_home=legacy, target_home=target) == []
    assert not (target / "config").exists()


def test_migrate_noop_when_legacy_is_target(tmp_path: Path) -> None:
    _seed_legacy(tmp_path, "nano.yml")
    assert migrate_legacy_config(legacy_home=tmp_path, target_home=tmp_path) == []


def test_migrate_creates_target_config_dir(tmp_path: Path) -> None:
    legacy = _seed_legacy(tmp_path / "soothe", "nano.yml")
    target = tmp_path / "deep" / "flowjet"

    assert migrate_legacy_config(legacy_home=legacy, target_home=target) == ["nano.yml"]
    assert (target / "config" / "nano.yml").is_file()


def test_migrate_survives_unwritable_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed copy must degrade to a warning, never raise into the CLI/request path."""
    import flowjet.home as home_mod

    legacy = _seed_legacy(tmp_path / "soothe", "nano.yml")

    def boom(_source: Path, _dest: Path) -> bool:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(home_mod, "_copy_atomic", boom)
    with caplog.at_level("WARNING"):
        assert migrate_legacy_config(legacy_home=legacy, target_home=tmp_path / "flowjet") == []
    assert "could not migrate legacy config" in caplog.text


def test_ensure_flowjet_config_announces_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = _seed_legacy(tmp_path / "soothe", "nano.yml")
    target = tmp_path / "flowjet"

    assert ensure_flowjet_config(legacy_home=legacy, target_home=target) == ["nano.yml"]
    err = capsys.readouterr().err
    assert "Copied nano.yml" in err
    assert str(legacy / "config") in err
    assert str(target / "config") in err

    assert ensure_flowjet_config(legacy_home=legacy, target_home=target) == []
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# load_config integration
# ---------------------------------------------------------------------------


def test_load_config_default_migrates_and_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import flowjet.home as home_mod

    legacy = tmp_path / "soothe"
    (legacy / "config").mkdir(parents=True)
    (legacy / "config" / "nano.yml").write_text(_NANO_YML, encoding="utf-8")
    target = tmp_path / "flowjet"
    monkeypatch.setenv("FLOWJET_HOME", str(target))
    monkeypatch.setattr(home_mod, "legacy_soothe_home", lambda: legacy)

    config = load_config(backend=Backend.NANO)

    assert config.active_router_profile == "default"
    assert (target / "config" / "nano.yml").is_file()
    assert (legacy / "config" / "nano.yml").is_file()


def test_load_config_explicit_path_does_not_migrate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import flowjet.home as home_mod

    legacy = tmp_path / "soothe"
    (legacy / "config").mkdir(parents=True)
    (legacy / "config" / "nano.yml").write_text(_NANO_YML, encoding="utf-8")
    target = tmp_path / "flowjet"
    monkeypatch.setenv("FLOWJET_HOME", str(target))
    monkeypatch.setattr(home_mod, "legacy_soothe_home", lambda: legacy)

    explicit = tmp_path / "explicit.yml"
    explicit.write_text(_NANO_YML, encoding="utf-8")
    config = load_config(explicit, backend=Backend.NANO)

    assert config.active_router_profile == "default"
    assert not (target / "config").exists()


def test_load_config_missing_explicit_path_raises_without_migrating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import flowjet.home as home_mod

    legacy = _seed_legacy(tmp_path / "soothe", "nano.yml")
    target = tmp_path / "flowjet"
    monkeypatch.setenv("FLOWJET_HOME", str(target))
    monkeypatch.setattr(home_mod, "legacy_soothe_home", lambda: legacy)

    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yml", backend=Backend.NANO)
    assert not (target / "config").exists()


def test_load_config_default_path_honours_flowjet_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real config placed at $FLOWJET_HOME is loaded, SOOTHE_HOME notwithstanding."""
    target = tmp_path / "flowjet"
    (target / "config").mkdir(parents=True)
    (target / "config" / "nano.yml").write_text(_NANO_YML, encoding="utf-8")
    monkeypatch.setenv("FLOWJET_HOME", str(target))
    monkeypatch.setenv("SOOTHE_HOME", str(tmp_path / "soothe"))

    config = load_config(backend=Backend.NANO)

    assert config.active_router_profile == "default"
