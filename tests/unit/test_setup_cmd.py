"""Unit tests for fj setup command helpers."""

from __future__ import annotations

from typing import Any

import pytest

from flowjet.cli.setup_cmd import (
    DEFAULT_NEW_PROVIDER_NAME,
    DEFAULT_PROVIDER_NAME,
    _parse_model_filter,
    _prompt_secret_with_default,
    choose_model_interactive,
    choose_provider_interactive,
    resolve_config_value,
    suggest_provider_name,
    update_config_for_model,
)


def test_update_config_for_model_preserves_unrelated_keys() -> None:
    existing: dict[str, Any] = {
        "providers": [
            {
                "name": "local",
                "provider_type": "openai",
                "api_base_url": "http://old/v1",
                "api_key": "old",
                "models": ["old-model"],
            },
            {"name": "other", "provider_type": "openai"},
        ],
        "tools": {"wizsearch": {"enabled": False}},
        "persistence": {"default_backend": "sqlite"},
    }
    updated = update_config_for_model(
        existing,
        provider_name="local",
        endpoint="http://127.0.0.1:11434/v1",
        api_key="ollama",
        model="llama3.2",
    )

    local_provider = next(p for p in updated["providers"] if p["name"] == DEFAULT_PROVIDER_NAME)
    assert local_provider["api_base_url"] == "http://127.0.0.1:11434/v1"
    assert local_provider["api_key"] == "ollama"
    assert local_provider["models"] == ["llama3.2", "old-model"]
    assert updated["tools"] == {"wizsearch": {"enabled": False}}
    assert updated["persistence"] == {"default_backend": "sqlite"}
    assert updated["active_router_profile"] == "default"
    assert updated["router_profiles"][0]["router"]["default"] == "local:llama3.2"


def test_update_config_adds_default_new_provider_name() -> None:
    updated = update_config_for_model(
        {},
        endpoint="http://127.0.0.1:11434/v1",
        api_key="ollama",
        model="llama3.2",
    )
    assert updated["providers"][0]["name"] == DEFAULT_NEW_PROVIDER_NAME
    assert updated["router_profiles"][0]["router"]["default"] == "flowjet-default:llama3.2"
    assert updated["active_router_profile"] == "default"


def test_update_config_uses_provider_prefix_and_activates_current_profile() -> None:
    existing: dict[str, Any] = {
        "providers": [
            {
                "name": "dashscope",
                "provider_type": "openai",
                "api_base_url": "https://dashscope.example/v1",
                "api_key": "${DASHSCOPE_API_KEY}",
                "models": ["glm-5.2"],
            }
        ],
        "router_profiles": [
            {
                "name": "production",
                "router": {
                    "default": "dashscope:glm-5.2",
                    "fast": "dashscope:qwen3.6-flash",
                    "think": "dashscope:glm-5.2",
                },
            },
            {"name": "default", "router": {"default": "local:llama3.2"}},
        ],
        "active_router_profile": "production",
    }
    updated = update_config_for_model(
        existing,
        provider_name="coding-dashscope",
        endpoint="https://coding.dashscope.aliyuncs.com/v1",
        api_key="sk-test",
        model="qwen3.7-plus",
    )

    names = [p["name"] for p in updated["providers"]]
    assert names == ["dashscope", "coding-dashscope"]
    coding = next(p for p in updated["providers"] if p["name"] == "coding-dashscope")
    assert coding["models"] == ["qwen3.7-plus"]
    assert updated["active_router_profile"] == "production"

    production = next(p for p in updated["router_profiles"] if p["name"] == "production")
    assert production["router"]["default"] == "coding-dashscope:qwen3.7-plus"
    # Preserve sibling router roles.
    assert production["router"]["fast"] == "dashscope:qwen3.6-flash"
    assert production["router"]["think"] == "dashscope:glm-5.2"


def test_update_config_preserves_env_placeholders() -> None:
    existing: dict[str, Any] = {
        "providers": [
            {
                "name": "dashscope",
                "provider_type": "openai",
                "api_base_url": "${DASHSCOPE_BASE_URL}",
                "api_key": "${DASHSCOPE_API_KEY}",
                "models": ["glm-5.2"],
            }
        ],
        "active_router_profile": "default",
    }
    updated = update_config_for_model(
        existing,
        provider_name="dashscope",
        endpoint="${DASHSCOPE_BASE_URL}",
        api_key="${DASHSCOPE_API_KEY}",
        model="qwen3.7-plus",
    )
    provider = next(p for p in updated["providers"] if p["name"] == "dashscope")
    assert provider["api_base_url"] == "${DASHSCOPE_BASE_URL}"
    assert provider["api_key"] == "${DASHSCOPE_API_KEY}"
    assert provider["models"] == ["qwen3.7-plus", "glm-5.2"]


def test_resolve_config_value_expands_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://dashscope.example/v1")
    assert (
        resolve_config_value("${DASHSCOPE_BASE_URL}", field="API endpoint")
        == "https://dashscope.example/v1"
    )
    assert (
        resolve_config_value("prefix-${DASHSCOPE_BASE_URL}-suffix", field="API endpoint")
        == "prefix-https://dashscope.example/v1-suffix"
    )


def test_resolve_config_value_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_SETUP_VAR", raising=False)
    with pytest.raises(RuntimeError, match="unresolved env var \\$\\{MISSING_SETUP_VAR\\}"):
        resolve_config_value("${MISSING_SETUP_VAR}", field="API key")


def test_fetch_models_invalid_url_raises_runtime_error() -> None:
    from flowjet.cli.setup_cmd import fetch_models

    with pytest.raises(RuntimeError, match="unknown url type"):
        fetch_models("${DASHSCOPE_BASE_URL}", "sk-test")


def test_suggest_provider_name_from_endpoint() -> None:
    assert suggest_provider_name("http://127.0.0.1:11434/v1") == "flowjet-default"
    assert suggest_provider_name("https://coding.dashscope.aliyuncs.com/v1") == "coding-dashscope"
    assert suggest_provider_name("https://apihub.agnes-ai.com/v1") == "apihub-agnes-ai"
    assert suggest_provider_name("http://100.75.70.86:9642/v1") == "host-100-75-70-86"


def test_choose_provider_interactive_select_existing(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    providers = [
        {"name": "dashscope", "api_base_url": "https://a/v1", "models": ["m1"]},
        {"name": "local", "api_base_url": "http://127.0.0.1:11434/v1", "models": ["llama"]},
    ]
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    selected = choose_provider_interactive(providers)
    assert selected == providers[0]
    out = capsys.readouterr().out
    assert "dashscope" in out
    assert "Add new provider" in out


def test_choose_provider_interactive_add_new(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    providers = [{"name": "dashscope", "api_base_url": "https://a/v1", "models": ["m1"]}]
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    assert choose_provider_interactive(providers) == {}


def test_choose_provider_interactive_default_is_add_new(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    providers = [{"name": "dashscope", "api_base_url": "https://a/v1", "models": ["m1"]}]
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert choose_provider_interactive(providers) == {}


def test_choose_provider_interactive_cancel(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    providers = [{"name": "dashscope", "api_base_url": "https://a/v1", "models": ["m1"]}]
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    assert choose_provider_interactive(providers) is None


def test_choose_provider_interactive_empty_adds_new() -> None:
    assert choose_provider_interactive([]) == {}


def test_choose_model_interactive_select_number(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    answers = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    selected = choose_model_interactive(["a", "b", "c"])
    assert selected == "b"


def test_choose_model_interactive_filter_then_select(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    answers = iter(["/llama", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    selected = choose_model_interactive(["gpt-4o", "llama3.2", "qwen2.5"])
    assert selected == "llama3.2"


def test_choose_model_interactive_text_prefix_filter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    answers = iter(["/text qwen3.7", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    selected = choose_model_interactive(
        ["glm-5", "qwen3.6-plus", "qwen3.7-plus", "kimi-k2.5"],
    )
    assert selected == "qwen3.7-plus"


def test_parse_model_filter() -> None:
    assert _parse_model_filter("/qwen3.7") == "qwen3.7"
    assert _parse_model_filter("/text qwen3.7") == "qwen3.7"
    assert _parse_model_filter("/filter Qwen") == "qwen"
    assert _parse_model_filter("/") == ""


def test_prompt_secret_shows_masked_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []

    def fake_read(prompt: str) -> str:
        seen.append(prompt)
        return ""

    monkeypatch.setattr("flowjet.cli.setup_cmd._read_secret_masked", fake_read)
    assert _prompt_secret_with_default("API key", "secret-value") == "secret-value"
    assert seen == ["API key [****]: "]


def test_prompt_secret_uses_entered_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("flowjet.cli.setup_cmd._read_secret_masked", lambda _prompt: "new-key")
    assert _prompt_secret_with_default("API key", "old") == "new-key"


def test_choose_model_interactive_eof_cancels(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert choose_model_interactive(["a", "b"]) is None


def test_run_setup_resolves_env_before_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.setup_cmd import _run_setup

    cfg_path = tmp_path / "nano.yml"
    cfg_path.write_text(
        """
providers:
  - name: dashscope
    provider_type: openai
    api_base_url: "${DASHSCOPE_BASE_URL}"
    api_key: "${DASHSCOPE_API_KEY}"
    models: [glm-5.2]
router_profiles:
  - name: default
    router:
      default: "dashscope:glm-5.2"
active_router_profile: default
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://dashscope.example/v1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-from-env")

    answers = iter(["1", "", "1"])  # provider, endpoint default, model
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("flowjet.cli.setup_cmd._read_secret_masked", lambda _prompt: "")

    seen: dict[str, str] = {}

    def fake_fetch(endpoint: str, api_key: str, timeout_s: float = 15.0) -> list[str]:
        seen["endpoint"] = endpoint
        seen["api_key"] = api_key
        return ["glm-5.2", "qwen3.7-plus"]

    monkeypatch.setattr("flowjet.cli.setup_cmd.fetch_models", fake_fetch)
    assert _run_setup(str(cfg_path)) == 0
    assert seen == {
        "endpoint": "https://dashscope.example/v1",
        "api_key": "sk-from-env",
    }
    saved = cfg_path.read_text(encoding="utf-8")
    assert "${DASHSCOPE_BASE_URL}" in saved
    assert "${DASHSCOPE_API_KEY}" in saved


def test_run_setup_keyboard_interrupt_is_clean(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.setup_cmd import run_setup

    def boom(_config_path: str | None = None) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("flowjet.cli.setup_cmd._run_setup", boom)
    assert run_setup() == 130
    assert "setup cancelled" in capsys.readouterr().err


def test_prompt_model_with_fallback_accepts_typed_name(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.setup_cmd import _prompt_model_with_fallback

    monkeypatch.setattr("builtins.input", lambda _prompt: "custom-model")
    assert _prompt_model_with_fallback([]) == "custom-model"


def test_prompt_model_with_fallback_uses_default_on_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.setup_cmd import _prompt_model_with_fallback

    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert _prompt_model_with_fallback(["llama3.2"]) == "llama3.2"
    assert _prompt_model_with_fallback([]) == "llama3.2"


def test_prompt_model_with_fallback_selects_candidate_number(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.setup_cmd import _prompt_model_with_fallback

    answers = iter(["5", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert _prompt_model_with_fallback(["a", "b"]) == "b"


def test_run_setup_endpoint_down_warns_and_saves(monkeypatch, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.setup_cmd import _run_setup

    cfg_path = tmp_path / "nano.yml"
    answers = iter(["", "", "custom-model"])  # provider name, endpoint, model
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("flowjet.cli.setup_cmd._read_secret_masked", lambda _prompt: "")

    def boom(endpoint: str, api_key: str, timeout_s: float = 15.0) -> list[str]:
        raise RuntimeError(f"{endpoint}/models -> [Errno 61] Connection refused")

    monkeypatch.setattr("flowjet.cli.setup_cmd.fetch_models", boom)

    assert _run_setup(str(cfg_path)) == 0
    err = capsys.readouterr().err
    assert "warning: could not list models" in err
    assert "Connection refused" in err

    saved = cfg_path.read_text(encoding="utf-8")
    assert "custom-model" in saved
    assert "flowjet-default:custom-model" in saved


def test_run_setup_endpoint_down_offers_saved_models(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.setup_cmd import _run_setup

    cfg_path = tmp_path / "nano.yml"
    cfg_path.write_text(
        """
providers:
  - name: omlx
    provider_type: openai
    api_base_url: "http://localhost:9642/v1"
    api_key: ""
    models: [DeepSeek-V4-Flash-2bit-DQ]
router_profiles:
  - name: default
    router:
      default: "omlx:DeepSeek-V4-Flash-2bit-DQ"
active_router_profile: default
""",
        encoding="utf-8",
    )
    answers = iter(["1", "", "1"])  # provider, endpoint default, saved model
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("flowjet.cli.setup_cmd._read_secret_masked", lambda _prompt: "")

    def boom(endpoint: str, api_key: str, timeout_s: float = 15.0) -> list[str]:
        raise RuntimeError(f"{endpoint}/models -> [Errno 61] Connection refused")

    monkeypatch.setattr("flowjet.cli.setup_cmd.fetch_models", boom)

    assert _run_setup(str(cfg_path)) == 0
    saved = cfg_path.read_text(encoding="utf-8")
    assert "omlx:DeepSeek-V4-Flash-2bit-DQ" in saved


def test_run_setup_empty_model_list_warns_and_prompts(monkeypatch, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.setup_cmd import _run_setup

    cfg_path = tmp_path / "nano.yml"
    answers = iter(["", "", "typed-model"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("flowjet.cli.setup_cmd._read_secret_masked", lambda _prompt: "")
    monkeypatch.setattr("flowjet.cli.setup_cmd.fetch_models", lambda *a, **k: [])

    assert _run_setup(str(cfg_path)) == 0
    assert "returned no models" in capsys.readouterr().err
    assert "typed-model" in cfg_path.read_text(encoding="utf-8")


def test_run_setup_targets_flowjet_home_not_soothe_home(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Config belongs to ``$FLOWJET_HOME/config/nano.yml``, never ``~/.soothe``.

    ``SOOTHE_HOME`` holds CLI data; it must not decide where flowjet reads and
    writes its config.
    """
    import flowjet.home as home_mod
    from flowjet.cli.setup_cmd import _run_setup

    flowjet_home = tmp_path / "flowjet"
    soothe_home = tmp_path / "soothe"
    monkeypatch.setenv("FLOWJET_HOME", str(flowjet_home))
    monkeypatch.setenv("SOOTHE_HOME", str(soothe_home))
    # Never read a developer's real ~/.soothe config during tests.
    monkeypatch.setattr(home_mod, "legacy_soothe_home", lambda: tmp_path / "no-legacy-soothe")

    answers = iter(["", "", "1"])  # provider name, endpoint default, model
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("flowjet.cli.setup_cmd._read_secret_masked", lambda _prompt: "")
    monkeypatch.setattr("flowjet.cli.setup_cmd.fetch_models", lambda *a, **k: ["llama3.2"])

    assert _run_setup(None) == 0
    assert (flowjet_home / "config" / "nano.yml").is_file()
    assert not (soothe_home / "config").exists()


def test_run_setup_migrates_legacy_soothe_config(monkeypatch, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    """A config in ``~/.soothe`` is copied in, so its providers are offered."""
    import flowjet.home as home_mod
    from flowjet.cli.setup_cmd import _run_setup

    legacy = tmp_path / "soothe"
    (legacy / "config").mkdir(parents=True)
    (legacy / "config" / "nano.yml").write_text(
        """
providers:
  - name: omlx
    provider_type: openai
    api_base_url: "http://localhost:9642/v1"
    api_key: ""
    models: [DeepSeek-V4-Flash-2bit-DQ]
router_profiles:
  - name: default
    router:
      default: "omlx:DeepSeek-V4-Flash-2bit-DQ"
active_router_profile: default
""",
        encoding="utf-8",
    )
    flowjet_home = tmp_path / "flowjet"
    monkeypatch.setenv("FLOWJET_HOME", str(flowjet_home))
    monkeypatch.setattr(home_mod, "legacy_soothe_home", lambda: legacy)

    answers = iter(["1", "", "1"])  # provider, endpoint default, migrated model
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("flowjet.cli.setup_cmd._read_secret_masked", lambda _prompt: "")
    monkeypatch.setattr("flowjet.cli.setup_cmd.fetch_models", lambda *a, **k: [])

    assert _run_setup(None) == 0
    err = capsys.readouterr().err
    assert "Copied nano.yml" in err
    # Migration is non-destructive.
    assert (legacy / "config" / "nano.yml").is_file()

    saved = (flowjet_home / "config" / "nano.yml").read_text(encoding="utf-8")
    assert "omlx:DeepSeek-V4-Flash-2bit-DQ" in saved
