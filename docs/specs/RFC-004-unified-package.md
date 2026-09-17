# RFC-004: Unified FlowJet Package (CLI + Server)

**Status**: Accepted
**Authors**: FlowJet
**Created**: 2026-09-17
**Last Updated**: 2026-09-17
**Depends on**: [RFC-001](RFC-001-openai-compatible-api.md), [RFC-002](RFC-002-isolated-thread-pool-runtime.md), [RFC-003](RFC-003-production-isolation-hardening.md)
**Supersedes**: —
**Kind**: Architecture Design

---

## 1. Abstract

`flowjet-agent` (terminal CLI) and `flowjet-server` (OpenAI-compatible HTTP +
ACP) were separate repositories that both drove the same engine,
`soothe-nano`, through two independent bootstraps. They had drifted apart on
three load-bearing points: security defaults, stream mapping, and session
identity.

This RFC merges the two repositories into **one distribution, `flowjet`**, with a
surface-agnostic core (`flowjet.core`) that owns the nano bootstrap, the runtime
event contract, and the stream mapper. `flowjet.cli` and `flowjet.server` become
thin adapters over that core. The merge is a **hard cutover**: no `fj_ai` or
`flowjet_server` compatibility packages are retained.

---

## 2. Motivation

Before the merge, the two products disagreed on:

| Concern | CLI (`fj_ai`) | Server (`flowjet_server`) |
|---------|---------------|---------------------------|
| `allow_paths_outside_workspace` | forced `True` (`agent.py:182`) | forced `False` (`bridges/nano/adapter.py:29`) |
| Agent constructor | `create_nano_agent(config, interaction_mode=…)` | `create_dual_mode_nano_agent(config)` |
| Stream mapping | `stream.py` + `progress.py` + `tool_stream.py` | `bridges/nano/mapping.py` |
| Session identity | uuid thread id, workdir pinning, `flock` guard | `ws_<sha256[:16]>` workspace per session + admission + mode gate |
| Interaction modes | `agent` \| `ask` \| `bypass` | `agent` \| `ask` (pinned per session) |

Two of these are not cosmetic: the security defaults are **opposite**, and the
`bypass` mode (which disables security enforcement) existed only on the CLI. A
naive shared bootstrap would have silently flipped one product's posture.

---

## 3. Scope and Non-Goals

### 3.1 Scope

* One distribution `flowjet`; packages `flowjet.core`, `flowjet.cli`, `flowjet.server`.
* `flowjet.core` owns: `RuntimeEvent`s, `RuntimeBackend`, nano bootstrap, stream
  mapping, and the surface-invariant profiles.
* The CLI consumes `RuntimeEvent`s through a `DirectRuntimeBackend` instead of
  calling `agent.graph.astream` directly.
* Server-only dependencies behind an optional `[server]` extra, with lazy imports.
* Both launch paths preserved: `flowjet-server` console script and `fj serve`.

### 3.2 Non-Goals

* No HTTP/ACP changes: the wire protocols stay exactly as RFC-001/RFC-003 define.
* No rewrite of the isolation thread pool (RFC-002/003); it moves unchanged.
* No compatibility shims for `fj_ai` / `flowjet_server` import paths.
* No unification of the two session models beyond what the surfaces need;
  `threads.py` (CLI listing) and `WorkspaceResolver` (server isolation) keep
  their own semantics.

---

## 4. Architecture

```
flowjet.core                     (surface-agnostic)
├── events.py        RuntimeEvent union: RunStarted, Progress, Narration,
│                    ToolStarted/Completed, OutputTextDelta, InterruptWaiting,
│                    RunCompleted/Failed, RunRequest, ModelInfo, UsageInfo
├── protocol.py      RuntimeBackend: list_models / stream_run / delete_run
├── modes.py         Surface, InteractionMode, SurfaceProfile, apply_profile,
│                    require_mode      <-- the security-default authority
├── bootstrap.py     load_config, create_agent, ensure_workspace,
│                    open_sqlite_checkpointer, builtin-skills registration
├── tool_stream.py   partial-JSON tool-arg accumulator
├── tool_results.py  error/detail/preview helpers
└── backends/
    ├── direct.py        in-loop backend (CLI)
    ├── isolation/       thread pool, admission, mode gate, workspace (server)
    └── fake.py          echo backend (tests / DI)

flowjet.cli     — argparse CLI, TTY renderer over RuntimeEvents, `fj serve`
flowjet.server  — FastAPI shell, OpenAI-compatible projection, ACP over WS
```

Data flow is identical on both surfaces:

```
request → RuntimeBackend.stream_run(RunRequest)
        → nano astream(stream_mode=[messages, updates, custom], subgraphs=True)
        → bridges/nano/mapping.iter_nano_runtime_events
        → AsyncIterator[RuntimeEvent]
        → renderer (TTY | SSE | ACP)
```

### 4.1 Surface profiles

```python
CLI_PROFILE = SurfaceProfile(Surface.CLI, allow_paths_outside_workspace=True,
                             allowed_modes={AGENT, ASK, BYPASS}, force_sqlite=True)
SERVER_PROFILE = SurfaceProfile(Surface.SERVER, allow_paths_outside_workspace=False,
                                allowed_modes={AGENT, ASK}, force_sqlite=False)
```

`require_mode()` raises on `bypass` over HTTP, making that escape hatch
structurally unreachable from the network surface rather than merely unused.

### 4.2 One mapper, lossless for both renderers

The terminal needs detail a protocol client must never see (tool arguments,
in-flight narration). Rather than maintain two mappers, the shared event set
carries it and each renderer picks what it may use:

* `Progress.raw` — the original soothe-nano custom payload. The terminal feeds it
  to `friendly_progress()`; SSE/ACP use `stage`/`message` only.
* `ToolStarted.args` / `ToolCompleted.args`, `.detail`, `.preview` — the terminal
  renders previews; the protocol surfaces emit tool names and status only.
* `Narration` — text produced before a tool call. The terminal shows it as a live
  progress preview; protocol clients never receive it, because it is superseded
  as soon as a tool starts. Only committed text becomes `OutputTextDelta`.

This is what keeps the "one mapper" claim honest instead of lossy.

---

## 5. Packaging

```toml
[project]
name = "flowjet"
dependencies = ["soothe-nano>=1.2.28,<1.3.0"]

[project.optional-dependencies]
server = ["fastapi>=0.115.0", "uvicorn[standard]>=0.32.0", "pydantic>=2.0",
          "pydantic-settings>=2.0", "agent-client-protocol[http]>=0.12.1,<1.0.0",
          "langchain-core>=0.3"]

[project.scripts]
flowjet = "flowjet.cli.cli:main"
fj = "flowjet.cli.cli:main"
fjf = "flowjet.cli.cli:main_follow"
flowjet-server = "flowjet.server.__main__:main"
```

`langchain-core` is declared explicitly; the server imported it without declaring
it. `flowjet.server.__init__` must stay FastAPI-free and `cli.py` imports
`serve_cmd` only inside the `serve` dispatch branch — a test guards this
(`tests/unit/test_cli_no_server_deps.py`).

---

## 6. Migration

Major version bump to **2.0.0**. Import paths, install names and behaviour
changes are enumerated in [docs/upgrade.md](../upgrade.md); the naming decision
record lives in [docs/naming.md](../naming.md).

---

## 7. Risks

| Risk | Mitigation |
|------|------------|
| CLI loses rendering fidelity (tool args, live narration) | Event set carries `raw`/`args`/`preview`/`Narration`; unit suite (47 progress + 32 stream tests) kept green with no semantic rewrites |
| Server behaviour drifts while moving | Server suites relocated unchanged; integration tests run against a live uvicorn |
| Isolation pool regression | Moved byte-for-byte; `test_isolation`, `test_production_isolation`, `test_concurrent` unchanged |
| CLI starts requiring FastAPI | `[server]` extra + lazy imports + a subprocess import guard test |

---

## 8. Status

Accepted and implemented in 2.0.0.
