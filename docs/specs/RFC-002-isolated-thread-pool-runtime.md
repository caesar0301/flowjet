# RFC-002: Isolated Thread-Pool Runtime

**Status**: Draft  
**Authors**: FlowJet  
**Created**: 2026-07-31  
**Last Updated**: 2026-07-31  
**Depends on**: [RFC-001](RFC-001-openai-compatible-api.md)  
**Supersedes**: —  
**Kind**: Architecture Design  

---

## 1. Abstract

This RFC defines **thread-pool isolation** for agent execution behind `RuntimeBackend`. Concurrent sessions run on a shared pool of worker threads (each with a private asyncio loop and a reused agent runner). Bindings (`session` / `thread_id`, workspace path) are **request-carried**. The OpenAI-compatible HTTP layer is unchanged; isolation is internal to the agent runtime and bridges. Patterns are inspired by soothe-daemon’s RFC-221 thread pool, but flowjet-server does **not** depend on soothe-daemon.

---

## 2. Scope and Non-Goals

### 2.1 Scope

* Isolation boundaries: session admission, cross-session concurrency, workspace FS, runner state
* `IsolatedRunRequest` and `AgentAdapter` contracts (agent-agnostic)
* Thread-pool execution model for the soothe-nano backend
* Workspace resolution rules and optional `flowjet.metadata.workspace` override
* Cooperative cancel via `delete_run` for in-flight pool runs
* Config knobs for pool size, reuse, timeouts, and `FLOWJET_HOME`

### 2.2 Non-Goals

* soothe-daemon dependency, sidecar, WebSocket protocol, identity, autopilot
* Process worker pool or Ray / distributed actors
* Sticky worker-per-session
* Multi-tenant API keys or cross-process Response store ownership
* Changes to OpenAI Responses schemas, projection modes, or SSE shapes (RFC-001)

---

## 3. Background & Motivation

Phase 1 (`RFC-001`) runs soothe-nano through a single shared agent in the FastAPI process, keyed only by LangGraph `thread_id`. Concurrent requests share middleware mutable state, one process-default workspace, and one compiled graph. soothe-daemon fixed the analogous singleton race (RFC-221) by:

1. Passing thread/workspace on each `LoopRunRequest` (no shared “current session” mutation).
2. Executing turns on a **thread pool** of workers with private asyncio loops.
3. Reusing one runner per worker with `prepare_for_request()` between turns.
4. Serializing same-loop turns while allowing cross-loop parallelism.

flowjet-server needs the same isolation depth for production multi-session use, while remaining a thin OpenAI adapter over **soothe-nano**.

---

## 4. Design Principles

1. **Request-carried binding**: Session, thread id, and workspace travel on every run object; workers must not rely on process-global “current session.”
2. **One-in-flight per session**: Same `session` cannot execute two turns concurrently; different sessions may, up to pool size.
3. **Adapter-agnostic isolation**: Thread pool and workspace rules sit above `AgentAdapter`; the production adapter is soothe-nano.
4. **Thin port, not daemon embed**: Reuse concepts from soothe-daemon; do not import its packages.
5. **Preserve RFC-001 surface**: Clients keep `flowjet.session` / `flowjet.metadata`; isolation is invisible except for safer concurrency and optional workspace override.

---

## 5. Architecture

### 5.1 Placement

```text
POST /v1/responses
        │
        ▼
openai_compat.ResponseService          (unchanged)
        │
        ▼
RuntimeBackend.stream_run(RunRequest)
        │
        ▼
IsolatingRuntimeBackend
  ├── SessionAdmission
  ├── WorkspaceResolver
  └── ThreadPool.submit(IsolatedRunRequest)
            │
            ▼
      Worker thread (private asyncio loop)
            │
            ▼
      AgentAdapter.astream → RuntimeEvent stream
            │
     ┌──────┴──────┐
  NanoAdapter   SootheAdapter
```

`FakeAgentAdapter` / `FakeRuntimeBackend` exist only as test doubles (injected in unit tests). Production always uses the isolating nano backend.

### 5.2 Isolation Boundaries

| Boundary | Mechanism |
|----------|-----------|
| Same-session concurrent turns | `SessionAdmission` — wait or fail with `session_busy` |
| Cross-session concurrency | Up to `max_pool_size` workers |
| Conversation / checkpoint | `session` == LangGraph / runner `thread_id` |
| Filesystem | Per-session directory under `$FLOWJET_HOME/data/workspaces/` |
| Request-local context | Each turn runs in a fresh Python `Context`; ContextVars are not inherited across worker turns |
| Runner mutable state | One reused adapter **per worker**; `prepare_for_request()` after each turn |
| Response store | Still process-global `InMemoryRunStore` (unchanged) |

Workers are **not sticky** to a session. Isolation is request-scoped binding plus one-in-flight-per-session.

### 5.3 IsolatedRunRequest

Conceptual fields:

| Field | Meaning |
|-------|---------|
| `run_id` | Response / run id (`resp_…`) |
| `session` | Opaque session id (`flowjet.session` or allocated `fj-<uuid>`) |
| `thread_id` | Checkpoint / conversation id; **defaults to `session`** |
| `input_text` | User input |
| `model` | Logical model id |
| `workspace` | Absolute workspace `Path` (resolved before submit) |
| `metadata` | Opaque client metadata |
| `request_id` | Pool-internal correlation id |

No autopilot, identity, or clarification fields (daemon-only).

### 5.4 AgentAdapter

```text
AgentAdapter:
  astream(IsolatedRunRequest) → AsyncIterator[RuntimeEvent]
  prepare_for_request() → None
  cleanup() → awaitable
```

* **Nano**: `create_nano_agent`; `astream` with `configurable.thread_id` and `configurable.workspace`; map stream chunks to runtime events (RFC-001 §9). A clean turn may reuse the worker-local graph. A failed or cancelled turn MUST taint and discard that graph before the worker accepts another turn.

### 5.5 Thread Pool

* Persistent `threading.Thread` workers, each with `asyncio.new_event_loop()`.
* Per-worker: request queue, response queue bridge to the FastAPI loop, `cancel_event`, cached `AgentAdapter`.
* Create the per-turn stream task with a fresh `contextvars.Context()` so workspace, model override, logging, and tool registry ContextVars cannot leak from a prior turn.
* A worker remains reserved through the terminal event until its `ready` barrier, emitted after adapter preparation. If the consumer disconnects first, cancel the turn and wait for that barrier before reuse.
* `submit(IsolatedRunRequest) → AsyncIterator[RuntimeEvent]` on the ASGI event loop.
* Same-session serialization before handoff (`await_session_dispatchable`).
* Knobs: `min_pool_size`, `max_pool_size`, `idle_timeout_seconds`, `reuse_runner`, `request_timeout_seconds`.

### 5.6 Workspace Resolution

Precedence:

1. If `metadata["workspace"]` is a non-empty string → expand/resolve as absolute path (must exist or be creatable; reject empty / relative-only without resolve).
2. Else `$FLOWJET_HOME/data/workspaces/ws_<sha256(session)[:16]>` (create parents).

FlowJet MUST set `security.allow_paths_outside_workspace = false` in code after
loading nano configuration. A local, mounted, or client-selected nano.yml cannot
weaken this invariant.

### 5.7 Session Identity

* HTTP `flowjet.session` → `RunRequest.session` → `IsolatedRunRequest.session` / `thread_id`.
* If omitted, allocate `fj-<uuid>` (RFC-001).
* Reusing a session continues checkpoint history **when** the underlying nano graph’s checkpointer persists. Isolation does not require persistence to be correct for FS/runner safety.

### 5.8 Cancellation

`RuntimeBackend.delete_run(run_id)` MUST attempt cooperative cancel of an in-flight pool request for that run (set worker `cancel_event`). Best-effort; stream may already have completed.

---

## 6. Configuration (normative knobs)

| Variable / setting | Default | Meaning |
|--------------------|---------|---------|
| `FLOWJET_NANO_CONFIG` | unset | Optional path to nano.yml |
| `FLOWJET_HOME` | `~/.flowjet` | Root for workspaces and local data |
| `FLOWJET_THREAD_POOL_MIN` | `2` | Min workers |
| `FLOWJET_THREAD_POOL_MAX` | `8` | Max workers |
| `FLOWJET_THREAD_POOL_IDLE_TIMEOUT` | `300` | Idle worker reclaim (seconds); `0` = never |
| `FLOWJET_REUSE_RUNNER` | `true` | Reuse adapter per worker |
| `FLOWJET_REQUEST_TIMEOUT` | `0` | Per-run timeout seconds; `0` = none |

---

## 7. Examples

### 7.1 Two sessions, parallel

Client A: `flowjet.session=fj-a`, Client B: `flowjet.session=fj-b`. Both `POST /v1/responses` concurrently. Two workers may run in parallel; workspaces `ws_<hash(a)>` and `ws_<hash(b)>` are distinct. Relative path `notes.md` does not collide.

### 7.2 Same session, serialized

Two concurrent posts with `flowjet.session=fj-a`. Second turn waits until the first completes (or fails with busy if configured to reject). Checkpoint / FS edits for `fj-a` are not interleaved.

### 7.3 Workspace override

```json
{
  "model": "default",
  "input": "List files",
  "flowjet": {
    "session": "fj-a",
    "metadata": { "workspace": "/Users/me/project" }
  }
}
```

Resolved workspace is `/Users/me/project` when allowed.

---

## 8. Relationship to Other RFCs

* **RFC-001**: Defines Agent Runtime Protocol and OpenAI surface. This RFC adds isolation behind `RuntimeBackend` without changing the public HTTP contract.
* Implementation: [IG-002](../impl/IG-002-isolated-thread-pool-runtime.md).
* Production hardening: [RFC-003](RFC-003-production-isolation-hardening.md).

---

## 9. Open Questions

* Whether same-session second turn **waits** (queue) vs immediate `session_busy` error — default **wait** with pool admission.
* When nano ships a durable checkpointer, whether `thread_id` must remain equal to `session` (preferred: yes).

---

## 10. References

* soothe-daemon thread pool / RFC-221 patterns (reference only; not a dependency)
* [RFC-001](RFC-001-openai-compatible-api.md)
* [RFC-003](RFC-003-production-isolation-hardening.md)
