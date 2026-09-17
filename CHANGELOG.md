# Changelog

All notable changes to **flowjet** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> History from 0.1.0–0.2.0 below was released as the separate `flowjet-server`
> distribution, now merged into `flowjet`. See [docs/upgrade.md](docs/upgrade.md).

## [Unreleased]

## [2.0.1] — 2026-09-17

### Added

- **ACP sessions run in the client's directory.** `FlowJetAcpAgent` now records
  the `cwd` from `session/new`, `session/load`, `session/resume` and
  `session/fork` and forwards it as the run's `metadata.workspace`. Previously
  the client `cwd` was accepted and discarded, so every ACP session ran in a
  hashed workspace under `FLOWJET_HOME`.

### Changed

- **The assistant identifies as FlowJet, not Soothe.** soothe-nano prepends its
  own `<ASSISTANT_IDENTITY>` block — "a helpful AI assistant" named `Soothe` —
  to every system prompt tier, and only parameterises it by `agent.name`.
  `flowjet.core.identity` now installs a FlowJet template (coding agent, creator
  line, and an explicit "never identify as Soothe / soothe-nano / Claude /
  ChatGPT / Gemini / Anthropic / OpenAI / Google" rule) and `apply_profile`
  defaults `agent.name` to `FlowJet` on both the CLI and server surfaces. An
  explicit `agent.name` or `agent.system_prompt` in `nano.yml` still wins.
- **Workspace resolution is caller-driven by default.**
  `FLOWJET_ALLOW_EXTERNAL_WORKSPACE` now defaults to `true`, so a workspace
  outside `FLOWJET_HOME` is honoured rather than rejected. This is not a
  sandbox escape — tools remain confined to whichever workspace is selected
  (`allow_paths_outside_workspace` is still `false`); the boundary moves to the
  caller's directory instead of disappearing. Set
  `FLOWJET_ALLOW_EXTERNAL_WORKSPACE=false` to restore forced hashed
  per-session workspaces.

## [2.0.0] — 2026-09-17 — unified CLI + server

**Breaking.** `flowjet-server` is merged into this repository and ships as one
distribution, `flowjet`. See [RFC-004](docs/specs/RFC-004-unified-package.md).

### Added

- **One distribution, two surfaces.** `flowjet.core` (shared runtime),
  `flowjet.cli` (was `fj_ai`) and `flowjet.server` (was `flowjet_server`).
- **`fj serve`** — run the HTTP service from the CLI (alias of `flowjet-server`,
  flags: `--host`, `-p/--port`, `--api-key`, `--nano-config`).
- **Optional `[server]` extra.** `pip install flowjet` pulls one runtime
  dependency (soothe-nano); FastAPI/uvicorn/ACP/pydantic-settings are opt-in
  and lazily imported so `fj` never loads them.
- **`DirectRuntimeBackend`** — the CLI now runs on the same `RuntimeBackend`
  contract and `RuntimeEvent` stream as SSE and ACP.
- **`Narration` event** — pre-tool-call text is available to the terminal for
  live preview while remaining invisible to protocol clients.
- **`SurfaceProfile`** (`flowjet.core.modes`) — the CLI's permissive workspace
  and the server's workspace confinement are now explicit, per-surface
  invariants; `bypass` mode is rejected on the server surface.
- Auth now uses `hmac.compare_digest` instead of `!=`.

### Changed

- Distribution renamed `flowjet-agent` → `flowjet`; version single-sourced from
  `flowjet.__version__` (the server no longer hard-codes `0.2.0`).
- Builtin skills moved to `flowjet/builtin_skills` and are registered for both
  surfaces.
- `langchain-core` is now an explicit `server` dependency (it was imported but
  undeclared).

### Removed

- The `fj_ai` and `flowjet_server` import namespaces. No compatibility shims —
  see [docs/upgrade.md](docs/upgrade.md) for the migration table.

## [0.2.0] — 2026-09-16

### Added

- **ACP (Agent Client Protocol) support.** flowjet-server now exposes an ACP
  `Agent` over a WebSocket endpoint at `/acp`. Other ACP clients (Zed, custom
  integrations) connect via `ws://host:port/acp` and drive the same
  nano-backed `RuntimeBackend` used by the OpenAI-compatible Responses API.
  The implementation bridges `RuntimeEvent` streams (RunStarted, Progress,
  OutputTextDelta, ToolStarted/Completed, RunCompleted) to ACP
  `session/update` notifications (agent_message_chunk, agent_thought_chunk,
  tool_call/tool_call_update) and returns `PromptResponse` with
  `stopReason=end_turn` + token usage. Initialize, session/new,
  session/load, session/list, session/prompt, session/cancel, and
  session/close are all supported.

### Changed

- **Bumped `soothe-nano` dependency** from `>=1.2.23,<2.0.0` to
  `>=1.2.28,<2.0.0` (latest release).
- **Added `agent-client-protocol[http]`** as a runtime dependency
  (`>=0.12.1,<1.0.0`), providing the ACP SDK server + WebSocket transport.

## [0.1.1] — 2026-09-05

### Fixed

- **litellm corrupted-install fix.** The previous image (v0.1.0) shipped a
  broken `litellm` 1.99.0 wheel: `litellm/__init__.py` was listed in the
  dist-info RECORD but missing from disk, so Python treated the package as an
  empty namespace package and every `/v1/responses` call failed with
  `module 'litellm' has no attribute 'acompletion'`. Reinstalling litellm with
  `uv pip install --force-reinstall litellm==1.99.0` restores
  `litellm/__init__.py` and `acompletion` becomes available again. The Docker
  image now rebuilds the venv cleanly so the fix is baked into the published
  image.

### Verified

Full E2E suite (`examples/e2e_http_api.py`) passes against the live server:

| Endpoint | Method | Result |
|---|---|---|
| `/health` | GET | ✅ `{"status":"ok",...}` |
| `/v1/models` | GET | ✅ returns `default` model |
| `/v1/responses` (stream=false) | POST | ✅ status `completed`, real LLM text |
| `/v1/responses/{id}` | GET | ✅ retrieve returns stored response |
| `/v1/responses/{id}` | DELETE | ✅ `deleted:true`, subsequent GET → 404 |
| `/v1/responses` (SSE, report) | POST | ✅ `response.created` → `output_text.delta` → `response.completed` |
| `/v1/responses` (SSE, progress) | POST | ✅ includes `response.flowjet.progress` stages |
| `/v1/responses` (SSE, developer) | POST | ✅ completes; no tool args leaked to wire |
| unknown model | POST | ✅ HTTP 404 `model_not_found` |
| missing id | GET | ✅ HTTP 404 `response_not_found` |

## [0.1.0] — initial release

OpenAI Responses–compatible HTTP service backed by soothe-nano (≥1.2.23).
Includes ask/agent interaction modes, three SSE projection modes
(report / progress / developer), thread-pool isolation (RFC-002), production
isolation suite (RFC-003), and a self-contained Docker deploy stack
(PostgreSQL + pgvector + flowjet-server).

[0.1.1]: https://github.com/caesar0301/flowjet-server/releases/tag/v0.1.1
[0.1.0]: https://github.com/caesar0301/flowjet-server/releases/tag/v0.1.0
