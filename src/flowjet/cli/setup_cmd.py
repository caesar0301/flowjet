"""Interactive ``fj setup`` command for minimal OpenAI-compatible config."""

from __future__ import annotations

import getpass
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

import yaml
from soothe_nano.config import _ENV_VAR_RE, _resolve_env

from flowjet.home import default_config_path, ensure_flowjet_config

DEFAULT_PROVIDER_NAME = "local"
DEFAULT_NEW_PROVIDER_NAME = "flowjet-default"
DEFAULT_PROVIDER_TYPE = "openai"
DEFAULT_API_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_API_KEY = "ollama"
DEFAULT_MODEL = "llama3.2"
DEFAULT_ROUTER_PROFILE = "default"

# Host labels that are rarely useful as a provider name.
_HOST_NOISE = frozenset(
    {
        "www",
        "api",
        "apis",
        "openai",
        "compatible",
        "v1",
        "com",
        "cn",
        "ai",
        "io",
        "net",
        "org",
        "co",
        "dev",
        "app",
        "cloud",
    }
)


def run_setup(config_path: str | None = None) -> int:
    """Run interactive setup and write minimal nano.yml values."""
    try:
        return _run_setup(config_path)
    except KeyboardInterrupt:
        sys.stderr.write("\nsetup cancelled\n")
        return 130


def _run_setup(config_path: str | None = None) -> int:
    path = Path(config_path).expanduser() if config_path else default_config_path()
    if config_path is None:
        # ``fj setup`` bypasses ``load_config``, so it pulls in a ``~/.soothe/config``
        # copy itself before reading the provider list.
        ensure_flowjet_config()
    existing = _load_existing_config(path)
    providers = _list_providers(existing)
    _, seed_model = _seed_from_config(existing)

    sys.stdout.write(f"Config path: {path}\n")
    selected = choose_provider_interactive(providers)
    if selected is None:
        sys.stderr.write("setup cancelled\n")
        return 1

    if selected:
        provider_name = str(selected.get("name") or DEFAULT_NEW_PROVIDER_NAME)
        endpoint_default = str(selected.get("api_base_url") or DEFAULT_API_BASE_URL)
        api_key_default = str(selected.get("api_key") or DEFAULT_API_KEY)
        sys.stdout.write(f"Modifying provider: {provider_name}\n")
    else:
        existing_names = {str(p.get("name", "")) for p in providers}
        name_default = DEFAULT_NEW_PROVIDER_NAME
        if name_default in existing_names:
            name_default = _unique_provider_name(name_default, existing_names)
        provider_name = _prompt_with_default("Provider name", name_default)
        provider_name = _slugify_provider_name(provider_name) or DEFAULT_NEW_PROVIDER_NAME
        if provider_name in existing_names:
            sys.stderr.write(
                f"error: provider '{provider_name}' already exists; pick it from the list to modify\n"
            )
            return 1
        endpoint_default = DEFAULT_API_BASE_URL
        api_key_default = DEFAULT_API_KEY
        sys.stdout.write(f"Adding provider: {provider_name}\n")

    endpoint = _prompt_with_default("API endpoint", endpoint_default)
    api_key = _prompt_secret_with_default("API key", api_key_default)

    sys.stdout.write("Fetching available models...\n")
    models: list[str] = []
    fetch_issue: str | None = None
    try:
        resolved_endpoint = resolve_config_value(endpoint, field="API endpoint")
        resolved_api_key = resolve_config_value(api_key, field="API key")
    except RuntimeError as exc:
        fetch_issue = str(exc)
    else:
        try:
            models = fetch_models(resolved_endpoint, resolved_api_key)
        except RuntimeError as exc:
            fetch_issue = str(exc)
        if not models and fetch_issue is None:
            fetch_issue = f"{resolved_endpoint} returned no models"

    if fetch_issue:
        # An unreachable endpoint is not fatal: save the provider anyway so the
        # user can start the model server later and pick a model by hand now.
        sys.stderr.write(f"warning: could not list models ({fetch_issue})\n")
        sys.stderr.write("warning: continuing without a model list\n")

    if models:
        preferred = seed_model
        if selected:
            selected_models = selected.get("models")
            if isinstance(selected_models, list) and selected_models:
                preferred = str(selected_models[0])
        if preferred and preferred in models:
            models = [preferred] + [m for m in models if m != preferred]
        model = choose_model_interactive(models)
    else:
        model = _prompt_model_with_fallback(_fallback_model_candidates(selected, seed_model))

    if not model:
        sys.stderr.write("setup cancelled\n")
        return 1

    updated = update_config_for_model(
        existing,
        provider_name=provider_name,
        endpoint=endpoint,
        api_key=api_key,
        model=model,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(updated, sort_keys=False, allow_unicode=True), encoding="utf-8")

    profile = updated.get("active_router_profile", DEFAULT_ROUTER_PROFILE)
    sys.stdout.write(f"Saved config to: {path}\n")
    sys.stdout.write(f"Active profile: {profile}\n")
    sys.stdout.write(f"Configured model: {provider_name}:{model}\n")
    if fetch_issue:
        sys.stdout.write(
            "Note: model list was not verified; start the endpoint and run 'fj doctor' to confirm.\n"
        )
    return 0


def suggest_provider_name(endpoint: str) -> str:
    """Derive a short provider id from an OpenAI-compatible endpoint URL."""
    host = (urlparse(endpoint).hostname or "").lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return DEFAULT_NEW_PROVIDER_NAME
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return "host-" + host.replace(".", "-")

    labels = [part for part in host.split(".") if part and part not in _HOST_NOISE]
    if not labels:
        labels = [part for part in host.split(".") if part]
    if not labels:
        return DEFAULT_NEW_PROVIDER_NAME
    if len(labels) == 1:
        return _slugify_provider_name(labels[0]) or DEFAULT_NEW_PROVIDER_NAME
    # coding.dashscope.aliyuncs.com → coding-dashscope
    return _slugify_provider_name("-".join(labels[:2])) or DEFAULT_NEW_PROVIDER_NAME


def _slugify_provider_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return slug


def _unique_provider_name(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    idx = 2
    while f"{base}-{idx}" in existing:
        idx += 1
    return f"{base}-{idx}"


def _list_providers(config: dict[str, Any]) -> list[dict[str, Any]]:
    providers = config.get("providers")
    if not isinstance(providers, list):
        return []
    return [item for item in providers if isinstance(item, dict) and item.get("name")]


def choose_provider_interactive(providers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick an existing provider to modify, ``{}`` to add new, or ``None`` to cancel.

    An empty dict means "add new provider". A non-empty dict is the selected provider.
    """
    if not providers:
        sys.stdout.write("No existing providers found; will add a new one.\n")
        return {}

    sys.stdout.write("\nExisting providers:\n")
    for idx, provider in enumerate(providers, start=1):
        name = str(provider.get("name", ""))
        endpoint = str(provider.get("api_base_url") or "")
        models = provider.get("models") if isinstance(provider.get("models"), list) else []
        model_hint = str(models[0]) if models else "-"
        sys.stdout.write(f"  {idx}. {name}  ({endpoint or 'no endpoint'}, model={model_hint})\n")
    add_idx = len(providers) + 1
    sys.stdout.write(f"  {add_idx}. Add new provider\n")

    while True:
        try:
            choice = input(f"\nSelect provider 1-{add_idx}, or q to cancel [{add_idx}]: ").strip()
        except EOFError:
            return None
        if not choice:
            return {}
        if choice.lower() == "q":
            return None
        if choice.isdigit():
            selected = int(choice)
            if 1 <= selected <= len(providers):
                return providers[selected - 1]
            if selected == add_idx:
                return {}
        sys.stdout.write("Invalid selection. Try again.\n")


def resolve_config_value(value: str, *, field: str) -> str:
    """Expand ``${ENV_VAR}`` placeholders; raise if any remain unresolved."""
    resolved = _resolve_env(value)
    match = _ENV_VAR_RE.search(resolved)
    if match:
        env_name = match.group(1)
        raise RuntimeError(
            f"unresolved env var ${{{env_name}}} in {field}. "
            f"Set {env_name} or enter a literal value."
        )
    return resolved


def fetch_models(endpoint: str, api_key: str, timeout_s: float = 15.0) -> list[str]:
    """Fetch model ids from an OpenAI-compatible endpoint."""
    normalized = endpoint.rstrip("/")
    candidates = [f"{normalized}/models"]
    if not normalized.endswith("/v1"):
        candidates.append(f"{normalized}/v1/models")

    last_error: str | None = None
    for url in candidates:
        try:
            req = request.Request(
                url=url,
                method="GET",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
        except ValueError as exc:
            last_error = f"{url} -> {exc}"
            continue
        try:
            with request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            last_error = f"{url} -> HTTP {exc.code}: {body or exc.reason}"
            continue
        except error.URLError as exc:
            last_error = f"{url} -> {exc.reason}"
            continue
        except Exception as exc:  # pragma: no cover - defensive
            last_error = f"{url} -> {type(exc).__name__}: {exc}"
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = f"{url} -> invalid JSON: {exc}"
            continue

        data = payload.get("data", []) if isinstance(payload, dict) else []
        models = sorted(
            {
                str(item.get("id", "")).strip()
                for item in data
                if isinstance(item, dict) and str(item.get("id", "")).strip()
            }
        )
        return models

    raise RuntimeError(last_error or "failed to fetch models")


def update_config_for_model(
    existing: dict[str, Any],
    *,
    endpoint: str,
    api_key: str,
    model: str,
    provider_name: str = DEFAULT_NEW_PROVIDER_NAME,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """Upsert provider and point the active router profile at ``provider:model``.

    Preserves unrelated config keys and other router role entries on the profile.
    """
    cfg = dict(existing)
    name = _slugify_provider_name(provider_name) or DEFAULT_NEW_PROVIDER_NAME
    profile = profile_name or str(cfg.get("active_router_profile") or DEFAULT_ROUTER_PROFILE)

    providers = cfg.get("providers")
    if not isinstance(providers, list):
        providers = []
        cfg["providers"] = providers

    provider = _find_provider(cfg, name)
    if not provider:
        provider = {"name": name}
        providers.append(provider)

    provider["name"] = name
    provider["provider_type"] = DEFAULT_PROVIDER_TYPE
    provider["api_base_url"] = endpoint
    provider["api_key"] = api_key
    provider["models"] = _merge_models(provider.get("models"), model)

    router_profiles = cfg.get("router_profiles")
    if not isinstance(router_profiles, list):
        router_profiles = []
        cfg["router_profiles"] = router_profiles

    router_profile = next(
        (
            item
            for item in router_profiles
            if isinstance(item, dict) and item.get("name") == profile
        ),
        None,
    )
    if not router_profile:
        router_profile = {"name": profile, "router": {}}
        router_profiles.append(router_profile)

    router = router_profile.get("router")
    if not isinstance(router, dict):
        router = {}
        router_profile["router"] = router
    router["default"] = f"{name}:{model}"

    cfg["active_router_profile"] = profile
    return cfg


def _merge_models(existing: Any, model: str) -> list[str]:
    models: list[str] = []
    if isinstance(existing, list):
        models = [str(item) for item in existing if str(item).strip()]
    if model not in models:
        models.insert(0, model)
    return models


def _seed_from_config(config: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Pick endpoint/key defaults from the active profile's ``default`` route when possible."""
    provider_name, model = _parse_provider_model(_active_default_route(config))
    if provider_name:
        provider = _find_provider(config, provider_name)
        if provider:
            return provider, model
    local = _find_provider(config, DEFAULT_PROVIDER_NAME)
    if local:
        return local, model
    return {}, model


def _active_default_route(config: dict[str, Any]) -> str | None:
    profile_name = config.get("active_router_profile") or DEFAULT_ROUTER_PROFILE
    profiles = config.get("router_profiles")
    if not isinstance(profiles, list):
        return None
    for item in profiles:
        if isinstance(item, dict) and item.get("name") == profile_name:
            router = item.get("router")
            if isinstance(router, dict):
                default = router.get("default")
                return str(default) if default else None
    return None


def _parse_provider_model(ref: str | None) -> tuple[str | None, str | None]:
    if not ref or ":" not in ref:
        return None, None
    provider, model = ref.split(":", 1)
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        return None, None
    return provider, model


def choose_model_interactive(models: list[str], page_size: int = 20) -> str | None:
    """Select one model with paging/filtering for long lists."""
    visible = list(models)
    page = 0
    active_filter = ""

    while True:
        if not visible:
            sys.stdout.write(
                "No models match current filter. Try /qwen, c to clear, q to cancel.\n"
            )
        else:
            total_pages = (len(visible) - 1) // page_size + 1
            page = min(page, total_pages - 1)
            start = page * page_size
            end = min(len(visible), start + page_size)
            sys.stdout.write(
                f"\nAvailable models ({len(visible)} total"
                + (f", filter='{active_filter}'" if active_filter else "")
                + f"), page {page + 1}/{total_pages}:\n"
            )
            for idx, model in enumerate(visible[start:end], start=start + 1):
                sys.stdout.write(f"  {idx}. {model}\n")

        try:
            choice = input(
                "\nSelect number, n/p (page), /qwen (filter), c (clear), q (cancel): "
            ).strip()
        except EOFError:
            return None

        if not choice:
            continue
        if choice.lower() == "q":
            return None
        if choice.lower() == "n":
            if visible:
                page += 1
            continue
        if choice.lower() == "p":
            if visible:
                page = max(0, page - 1)
            continue
        if choice.lower() == "c":
            visible = list(models)
            active_filter = ""
            page = 0
            continue
        if choice.startswith("/"):
            active_filter = _parse_model_filter(choice)
            visible = (
                [m for m in models if active_filter in m.lower()] if active_filter else list(models)
            )
            page = 0
            continue
        if choice.isdigit():
            selected = int(choice)
            if 1 <= selected <= len(visible):
                return visible[selected - 1]
        sys.stdout.write("Invalid selection. Try again.\n")


def _parse_model_filter(choice: str) -> str:
    """Parse ``/qwen``, ``/text qwen``, or ``/filter qwen`` into a substring."""
    raw = choice[1:].strip().lower()
    for prefix in ("text ", "filter "):
        if raw.startswith(prefix):
            return raw[len(prefix) :].strip()
    return raw


def _fallback_model_candidates(selected: dict[str, Any], seed_model: str | None) -> list[str]:
    """Models already known from config, used when the endpoint cannot be queried."""
    candidates: list[str] = []
    if selected:
        models = selected.get("models")
        if isinstance(models, list):
            candidates.extend(str(item) for item in models if str(item).strip())
    if seed_model and seed_model not in candidates:
        candidates.insert(0, seed_model)
    return candidates


def _prompt_model_with_fallback(candidates: list[str]) -> str | None:
    """Ask for a model id directly when the endpoint has no model list to pick from."""
    default = candidates[0] if candidates else DEFAULT_MODEL
    if candidates:
        sys.stdout.write("Models saved for this provider:\n")
        for idx, name in enumerate(candidates, start=1):
            sys.stdout.write(f"  {idx}. {name}\n")
        label = f"Model name or number 1-{len(candidates)}"
    else:
        label = "Model name"

    while True:
        try:
            value = input(f"{label} [{default}]: ").strip()
        except EOFError:
            return default
        if not value:
            return default
        if value.isdigit() and candidates:
            idx = int(value)
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]
            sys.stdout.write(f"Invalid number; enter 1-{len(candidates)} or a model name.\n")
            continue
        return value


def _load_existing_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload
    return {}


def _find_provider(config: dict[str, Any], name: str) -> dict[str, Any]:
    providers = config.get("providers", [])
    if not isinstance(providers, list):
        return {}
    for item in providers:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return {}


def _prompt_with_default(prompt: str, default: str) -> str:
    value = input(f"{prompt} [{default}]: ").strip()
    return value or default


def _prompt_secret_with_default(prompt: str, default: str) -> str:
    hint = "****" if default else ""
    label = f"{prompt} [{hint}]: " if hint else f"{prompt}: "
    value = _read_secret_masked(label).strip()
    return value or default


def _read_secret_masked(prompt: str) -> str:
    """Read a secret, echoing ``*`` per character (including paste)."""
    if termios is None or tty is None or not sys.stdin.isatty() or not sys.stdout.isatty():
        return getpass.getpass(prompt)

    sys.stdout.write(prompt)
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    chars: list[str] = []
    try:
        tty.setcbreak(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\n", "\r", "\x04"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x7f", "\b"):
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if not ch or ord(ch) < 32:
                continue
            chars.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return "".join(chars)
