# JGK-01: Make Soothe the FlowJet Server Default Backend

**Status**: Implemented (Phases 1–3)
**Authors**: FlowJet
**Created**: 2026-10-11
**Step**: JGK-01
**Depends on**: RFC-004 (Unified Package), RFC-906 (Workspace Sync)
**Kind**: Architecture Design

---

## 1. Abstract

Today the FlowJet server (`Surface.SERVER`) defaults to the `nano` backend
(`DEFAULT_SERVER_BACKEND = Backend.NANO`). The reason is documented in
`flowjet/core/backend.py`: nano exposes `create_dual_mode_nano_agent` — one
agent graph that selects AGENT vs ASK per request — while `soothe` (the host
runtime) had no dual-mode API. FlowJet worked around this with
`ModeRoutedSootheAgent` (one host agent per mode, lazily built), but kept the
default on nano because the host runtime was not yet needed for server
operation.

The goal now is to **flip the server default to `soothe`** so that FlowJet can
use the host runtime's capabilities that nano does not expose:

1. **soothe package fsspec workspace sync** — `soothe.workspace.sync`
   (`WorkspaceManager`, `FsspecSyncBackend`, `Materializer`, CAS cache, dirty
   tracker, checkpointing) provides incremental materialization from an object
   store (S3/GCS/Azure/local) into a local filesystem workspace, with
   content-addressed caching and debounced persistence. Nano has none of this.

2. **BoxLite session isolation** — the boxlite container isolation concepts
   from `soothe_daemon.runner.boxlite_runner` are **migrated into FlowJet's
   own `core/backends/isolation/` package** as self-contained modules
   (`boxlite_pool.py`, `boxlite_protocol.py`, `boxlite_worker.py`), providing
   Docker-container-based session isolation with zero `soothe-daemon`
   dependency. FlowJet's current `IsolatingRuntimeBackend` uses an in-process
   `ThreadPool` with no real sandboxing between sessions.

This document proposes the design for making soothe the default server backend.
It covers: the backend flip, the workspace sync integration surface, and the
boxlite isolation integration surface — with a phased rollout.

---

## 2. Current Architecture (As-Is)

### 2.1 Backend Selection

```
flowjet/core/backend.py
  Backend.SOOTHE  →  soothe.coreagent.create_soothe_agent (host runtime)
  Backend.NANO    →  soothe_nano.create_nano_agent / create_dual_mode_nano_agent

  DEFAULT_CLI_BACKEND    = Backend.SOOTHE   # CLI already on soothe
  DEFAULT_SERVER_BACKEND = Backend.NANO     # ← this is what we flip
```

Resolution is per-surface via env:
- `FLOWJET_BACKEND` (CLI)
- `FLOWJET_SERVER_BACKEND` (server)

### 2.2 Server Wiring

```
server/__main__.py  →  server/config.py (Settings)
                     →  server/http/app.py
                         build_backend(settings)
                           → build_isolating_nano_backend()       ← hardcoded nano
                               → IsolatingRuntimeBackend(
                                     adapter_factory=NanoAgentAdapter,
                                     pool_settings=ThreadPool,
                                     workspace=WorkspaceResolver
                                 )
```

The `build_backend()` function in `server/http/app.py` is **hardcoded** to call
`build_isolating_nano_backend()` — it does not consult `resolve_backend()`.
This is the primary wiring point that must change.

### 2.3 Agent Construction (soothe path already exists)

`bootstrap.create_agent()` already handles `Backend.SOOTHE` for the server
surface via `ModeRoutedSootheAgent`:

```python
if resolved is Backend.SOOTHE:
    return create_soothe_agent(cfg, profile, mode)
    # → ModeRoutedSootheAgent(config, factory)  for Surface.SERVER
```

`ModeRoutedSootheAgent` lazily builds one host agent per interaction_mode
(`agent` / `ask`) and routes `astream` calls based on
`configurable["interaction_mode"]`. This is already tested and working.

### 2.4 What's Missing

| Gap | Current | Needed |
|-----|---------|--------|
| Server backend default | `nano` | `soothe` |
| `build_backend()` | Hardcoded nano factory | Backend-aware factory |
| Workspace sync | `WorkspaceResolver` (local dir only) | `WorkspaceManager` (fsspec CAS + materialization) |
| Isolation | In-process `ThreadPool` | Optional `boxlite` container isolation |
| Agent adapter | `NanoAgentAdapter` | `SootheAgentAdapter` (host runtime) |

---

## 3. Proposed Design

### 3.1 Phase 1 — Flip the Default (Minimal, No New Dependencies)

**Goal**: `DEFAULT_SERVER_BACKEND = Backend.SOOTHE`, and `build_backend()`
constructs a soothe-backed `IsolatingRuntimeBackend` instead of a nano-backed
one.

#### 3.1.1 Change the default

```python
# flowjet/core/backend.py
DEFAULT_SERVER_BACKEND = Backend.SOOTHE  # was: Backend.NANO
```

Update the module docstring: the server now uses the host runtime by default.
`ModeRoutedSootheAgent` replaces `create_dual_mode_nano_agent` as the server's
agent constructor. The `nano` backend remains selectable via
`FLOWJET_SERVER_BACKEND=nano` for users who don't need host capabilities.

#### 3.1.2 Backend-aware `build_backend()`

```python
# server/http/app.py
def build_backend(settings: Settings) -> RuntimeBackend:
    backend = resolve_backend(Surface.SERVER)
    if backend is Backend.SOOTHE:
        return build_isolating_soothe_backend(
            models=settings.model_ids(),
            config_path=settings.nano_config,
            home=settings.home_path(),
            pool_settings=settings.pool_settings(),
            allow_external_workspace=settings.allow_external_workspace,
        )
    return build_isolating_nano_backend(
        models=settings.model_ids(),
        config_path=settings.nano_config,
        home=settings.home_path(),
        pool_settings=settings.pool_settings(),
        allow_external_workspace=settings.allow_external_workspace,
    )
```

#### 3.1.3 `SootheAgentAdapter` + `build_isolating_soothe_backend()`

A new adapter in `flowjet/core/bridges/soothe/` (parallel to
`bridges/nano/`):

```
flowjet/core/bridges/soothe/
  __init__.py
  adapter.py      → SootheAgentAdapter (AgentAdapter protocol)
  backend.py      → build_isolating_soothe_backend()
  mapping.py      → (reuse nano mapping — same RuntimeEvent contract)
```

`SootheAgentAdapter` wraps `ModeRoutedSootheAgent` (or a plain host agent for
CLI). It implements the same `AgentAdapter` protocol as `NanoAgentAdapter`:
`astream()`, `prepare_for_request()`, `cleanup()`. The stream mapping is shared
— `iter_nano_runtime_events()` already handles soothe's custom chunk types
(`soothe.stream.tool_call.update`, host middleware phase events, etc.).

`build_isolating_soothe_backend()` mirrors `build_isolating_nano_backend()`:
same `IsolatingRuntimeBackend` + `ThreadPool`, just with a
`SootheAgentAdapter` factory instead of `NanoAgentAdapter`.

**Key insight**: The `IsolatingRuntimeBackend`, `ThreadPool`,
`SessionAdmission`, `InteractionModeGate`, and `WorkspaceResolver` are all
backend-agnostic — they operate on the `AgentAdapter` protocol and
`IsolatedRunRequest`. Only the adapter and the factory function change.

#### 3.1.4 Config loading

`bootstrap.load_config()` already selects the right config class:
- `Backend.SOOTHE` → `soothe.config.SootheConfig` (host superset, reads
  `soothe.yml` + `nano.yml`)
- `Backend.NANO` → `soothe_nano.config.SootheConfig`

No change needed here.

#### 3.1.5 Test impact

Tests in `test_backend.py` that assert `resolve_backend(Surface.SERVER) is
Backend.NANO` must be updated to assert `Backend.SOOTHE`. The
`ModeRoutedSootheAgent` tests already pass and cover the soothe server path.

---

### 3.2 Phase 2 — Workspace Sync Integration (fsspec)

**Goal**: Replace the flat `WorkspaceResolver` (local directory per session)
with soothe's `WorkspaceManager` when a workspace sync source is configured.

#### 3.2.1 New settings

```python
# server/config.py
class Settings(BaseSettings):
    ...
    # Workspace sync (Phase 2) — when set, sessions materialize from / persist
    # to this fsspec-backed object store. Empty = local-only (Phase 1 behavior).
    workspace_sync_source: str | None = None  # e.g. "s3://bucket/flowjet"
    workspace_sync_config: str | None = None  # path to sync config JSON
    workspace_cas_root: str = "~/.flowjet/data/cas"  # local CAS cache
```

#### 3.2.2 `SyncAwareWorkspaceResolver`

A new class wrapping `WorkspaceManager`, implementing the same `resolve()`
interface as `WorkspaceResolver` so `IsolatingRuntimeBackend` needs no change:

```
flowjet/core/backends/isolation/workspace.py
  WorkspaceResolver               ← existing (local-only, Phase 1)
  SyncAwareWorkspaceResolver      ← new (fsspec-backed, Phase 2)
```

When `workspace_sync_source` is set:
1. `construct_sync_backend(uri)` builds an `FsspecSyncBackend` from the URI.
2. `WorkspaceManager(workspaces_root=..., cas_root=..., backend=sync_backend)`
   is constructed once at server startup.
3. Per session: `WorkspaceManager.open(session_id)` materializes resources
   from the object store into a local workspace directory (via CAS cache +
   `Materializer`), returns a `Workspace` handle.
4. During the run: the agent writes to the local filesystem normally.
5. After the run (or debounced during): `Workspace.checkpoint()` +
   `BackgroundUploader` persist dirty files back to the object store.

When `workspace_sync_source` is **not** set, `WorkspaceResolver` is used
(Phase 1 behavior — local directory per session, no sync).

#### 3.2.3 Security

`construct_sync_backend()` already enforces a scheme allowlist (`s3`, `gs`,
`az`) and disables the fsspec global cache to prevent credential leakage. The
agent process never sees storage credentials — they stay in the
`FsspecSyncBackend`'s thread pool.

`SERVER_PROFILE.allow_paths_outside_workspace` stays `False`: the agent is
confined to the materialized workspace directory, same as today.

---

### 3.3 Phase 3 — BoxLite Isolation Integration

**Goal**: Offer an optional isolation upgrade from in-process `ThreadPool` to
container-based session isolation by **migrating the boxlite logic into
FlowJet** — not by depending on the `soothe-daemon` package.

The boxlite isolation concepts (container-per-worker, stdin/stdout JSON-lines
protocol, worker lifecycle management, health watchdog, ready/done handshake)
are ported directly into FlowJet's `core/backends/isolation/` package as
self-contained modules with zero external daemon dependencies.

#### 3.3.1 New settings

```python
# server/config.py
class Settings(BaseSettings):
    ...
    # Isolation mode (Phase 3)
    isolation_mode: Literal["thread_pool", "boxlite"] = "thread_pool"
    boxlite_image: str | None = None
    boxlite_min_pool: int = 1
    boxlite_max_pool: int = 4
    boxlite_cpu_count: int = 2
    boxlite_mem_mib: int = 2048
    boxlite_workspace_mount_mode: Literal["bind", "sync"] = "bind"
    boxlite_local_fallback: bool = True
```

#### 3.3.2 Isolation backend selection

```
IsolatingRuntimeBackend
  ├── isolation_mode="thread_pool"  → ThreadPool (existing, Phase 1/2)
  └── isolation_mode="boxlite"      → BoxLiteIsolationPool (migrated, Phase 3)
```

`BoxLiteIsolationPool` is a new pool implementation (in
`core/backends/isolation/boxlite_pool.py`) that:
1. Launches one worker process per pool slot — either a Docker container
   (`docker run --rm -i ...`) when `boxlite_image` is set, or a local
   subprocess (the same worker script via `python -m ...boxlite_worker`)
   when `local_fallback=True`.
2. Communicates with each worker over stdin/stdout using a JSON-lines
   protocol (`boxlite_protocol.py`): the host sends `{"type":"request",...}`
   frames; the worker emits `{"type":"event","event_type":"RunStarted",...}`,
   `{"type":"done"}`, `{"type":"error",...}`, and `{"type":"ready"}` frames.
3. Implements the same `IsolationPool` protocol as `ThreadPool`:
   `start`, `submit`, `cancel_run`, `cancel_session`, `metrics`, `shutdown`,
   `await_session_dispatchable`.
4. Manages worker lifecycle: baseline pool spawn, on-demand scale-up to
   `max_size`, idle recycling, dead-worker reaping via a health watchdog,
   and graceful shutdown via `{"type":"shutdown"}` + `SIGKILL` fallback.

The worker (`boxlite_worker.py`) runs inside the container (or as a local
subprocess). It:
1. Signals `ready` immediately on startup.
2. Reads JSON-lines requests from stdin.
3. Lazily builds an `AgentAdapter` on first request — resolving the backend
   (`soothe` or `nano`) from the server config, with `FakeAgentAdapter`
   fallback for testing.
4. Executes each request via `adapter.astream()`, emitting events as
   JSON-lines on stdout.
5. Signals `ready` after each request, enabling worker reuse.

#### 3.3.3 Workspace mount modes

- **`bind`**: The local workspace directory is bind-mounted into the container
  (`-v` flag). Works with Phase 1/2 local workspaces.
- **`sync`**: The workspace is materialized inside the container from the
  fsspec object store (requires Phase 2 `workspace_sync_source`). This is the
  strongest isolation: the container has no host filesystem access, only the
  object store.

#### 3.3.4 Dependency boundary

**No `soothe-daemon` dependency.** The boxlite logic is fully migrated into
FlowJet's own `core/backends/isolation/` package:
- `boxlite_pool.py` — `BoxLiteIsolationPool` + `BoxLitePoolSettings`
- `boxlite_protocol.py` — JSON-lines serialization (encode/decode events,
  requests, control frames)
- `boxlite_worker.py` — container/subprocess worker entrypoint
- `isolation_pool.py` — `IsolationPool` protocol (shared with `ThreadPool`)

The pool communicates directly with the worker process via stdin/stdout.
Docker is only needed when `boxlite_image` is set; `local_fallback=True`
(default) runs the worker as a local subprocess for testing/CI without Docker.
`thread_pool` mode (the default) never imports any boxlite module.

---

## 4. Component Map

```
                        ┌─────────────────────────────────────────┐
                        │         server/http/app.py              │
                        │     build_backend(settings)             │
                        │   resolve_backend(Surface.SERVER)       │
                        └────────────┬────────────────────────────┘
                                     │
                    ┌────────────────┴────────────────┐
                    │ soothe                          │ nano (fallback)
                    ▼                                 ▼
  build_isolating_soothe_backend()      build_isolating_nano_backend()
                    │                                 │
                    │  IsolatingRuntimeBackend         │  IsolatingRuntimeBackend
                    │  ├── ThreadPool (Phase 1)        │  ├── ThreadPool
                    │  │   or BoxLitePool (Phase 3)    │  ├── WorkspaceResolver
                    │  ├── WorkspaceResolver (Ph 1)    │  └── NanoAgentAdapter
                    │  │   or SyncAwareWSR (Ph 2)      │
                    │  └── SootheAgentAdapter           │
                    │      └── ModeRoutedSootheAgent   │
                    │          (agent / ask graphs)     │
                    ▼                                 │
          ┌──────────────────────┐                    │
          │  WorkspaceManager    │ (Phase 2)          │
          │  ├── FsspecSyncBackend (fsspec)            │
          │  ├── CASCache         │                    │
          │  ├── DirtyTracker     │                    │
          │  ├── Materializer     │                    │
          │  └── BackgroundUploader│                   │
          └──────────────────────┘                    │
                    │                                 │
          ┌─────────┴──────────┐                     │
          │ BoxLiteWorkerPool  │ (Phase 3)           │
          │  (soothe-daemon)   │                     │
          │  container per     │                     │
          │  worker slot       │                     │
          └────────────────────┘                     │
```

---

## 5. Phased Rollout

| Phase | Scope | New deps | Default change |
|-------|-------|----------|----------------|
| **1** | Flip default to `soothe`; add `SootheAgentAdapter` + `build_isolating_soothe_backend()` | `soothe` (already a dep for CLI) | `DEFAULT_SERVER_BACKEND = SOOTHE` |
| **2** | Workspace sync via `WorkspaceManager` + `FsspecSyncBackend` | `fsspec`, `s3fs`/`gcsfs`/`adlfs` (optional) | Opt-in via `workspace_sync_source` |
| **3** | BoxLite container isolation (migrated into FlowJet) | Docker (optional, `local_fallback` for testing) | Opt-in via `isolation_mode=boxlite` |

Phase 1 is safe to ship independently: it changes the agent runtime but not the
isolation or workspace model. Users who need nano can set
`FLOWJET_SERVER_BACKEND=nano`.

---

## 6. Key Design Decisions

### 6.1 Why not replace `IsolatingRuntimeBackend`?

The `IsolatingRuntimeBackend` is a well-tested orchestration layer (admission,
mode gate, workspace resolution, pool lifecycle). It is **backend-agnostic** —
it depends on the `AgentAdapter` protocol, not on nano. Reusing it for soothe
means we only swap the adapter factory. The same applies to the pool:
`ThreadPool` works for Phase 1/2; `BoxLiteIsolationPool` is a drop-in
replacement for Phase 3.

### 6.2 Why `ModeRoutedSootheAgent` instead of waiting for a host dual-mode API?

`ModeRoutedSootheAgent` already solves the dual-mode problem for the server
surface: it lazily builds one host agent per interaction_mode and routes based
on `configurable["interaction_mode"]`. It is tested and working. Waiting for
`soothe` to expose a native dual-mode API would block the default flip
indefinitely. When/if the host runtime adds a dual-mode API, the router can be
replaced with a one-line change in `create_soothe_agent()`.

### 6.3 Why keep `nano` as a selectable backend?

Not all deployments need host capabilities. `nano` is lighter (no host
middleware, no workspace sync, no daemon). Keeping it as
`FLOWJET_SERVER_BACKEND=nano` gives users a escape hatch and a fallback if the
host runtime has issues.

### 6.4 Workspace sync is opt-in, not default

Phase 2 workspace sync requires an object store URI and credentials. Making it
the default would break zero-config local deployments. The `WorkspaceResolver`
(local directory per session) remains the default; `SyncAwareWorkspaceResolver`
activates only when `workspace_sync_source` is set.

### 6.5 BoxLite is opt-in, not default

Phase 3 boxlite isolation requires Docker when `boxlite_image` is set, but
defaults to `local_fallback=True` (local subprocess) for testing/CI. The
default isolation stays `thread_pool` (in-process). BoxLite is for deployments
that need container-level session isolation (multi-tenant, untrusted users).
The boxlite logic is fully migrated into FlowJet — no `soothe-daemon`
dependency.

---

## 7. Files to Change (Phase 1)

| File | Change |
|------|--------|
| `src/flowjet/core/backend.py` | `DEFAULT_SERVER_BACKEND = Backend.SOOTHE`; update docstring |
| `src/flowjet/server/http/app.py` | `build_backend()` → backend-aware (soothe or nano) |
| `src/flowjet/core/bridges/soothe/__init__.py` | New package |
| `src/flowjet/core/bridges/soothe/adapter.py` | `SootheAgentAdapter` |
| `src/flowjet/core/bridges/soothe/backend.py` | `build_isolating_soothe_backend()` |
| `tests/unit/test_backend.py` | Update default assertions; add soothe server backend tests |

---

## 8. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| `soothe` host runtime not installed | Lazy import + clear error message (`pip install 'flowjet[server]'` pulls `soothe`) |
| `ModeRoutedSootheAgent` builds 2 agents per worker (memory) | Lazy build — a worker serving only `ask` never builds `agent`. Pool sizing (`thread_pool_max`) bounds total agents. |
| Stream mapping differences between nano and soothe | `iter_nano_runtime_events()` already handles both — it was designed for the unified package (RFC-004). Soothe's custom chunk types (`soothe.stream.*`) are mapped in the same function. |
| Workspace sync credential leakage | `FsspecSyncBackend` disables fsspec cache; scheme allowlist prevents SSRF; credentials stay in the backend's thread pool, never exposed to the agent. |
| Boxlite adds Docker dependency | Phase 3 is opt-in; `thread_pool` default never imports boxlite modules. `local_fallback=True` enables testing without Docker. No `soothe-daemon` dependency — boxlite logic is fully migrated into FlowJet. |
