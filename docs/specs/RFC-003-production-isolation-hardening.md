# RFC-003: Production Isolation Hardening

**Status**: Draft  
**Authors**: FlowJet  
**Created**: 2026-07-31  
**Last Updated**: 2026-07-31  
**Depends on**: [RFC-002](RFC-002-isolated-thread-pool-runtime.md)  
**Supersedes**: —  
**Kind**: Architecture Design  

---

## 1. Abstract

This RFC hardens the RFC-002 isolating ThreadPool for production: reliable cross-thread delivery (no silent drop of terminal/`ready` frames), worker lifecycle (idle shrink, max-requests recycle, dead-worker respawn), post-turn orphan cleanup, ready-barrier timeouts, workspace override policy, and per-session `interaction_mode` pinning. Patterns follow soothe-daemon’s thread pool; flowjet-server remains a thin adapter and does **not** embed soothe-daemon.

---

## 2. Scope and Non-Goals

### 2.1 Scope

* Response bridge backpressure and control-frame delivery guarantees
* Worker lifecycle: baseline vs scaled, idle timeout, `max_requests_per_worker`
* Health watchdog: detect dead threads, respawn baseline capacity
* Ready-barrier timeout and worker recycle on stuck cleanup
* Orphan asyncio task cancellation after each turn
* Workspace override policy (`FLOWJET_ALLOW_EXTERNAL_WORKSPACE`)
* Session-scoped `interaction_mode` pin (first turn wins)
* Pool metrics snapshot for `/health` (or adjacent ops surface)
* Config knobs and production defaults for timeouts

### 2.2 Non-Goals

* Subprocess / Ray process isolation
* soothe-daemon dependency, identity, autopilot, WebSocket sessions
* Multi-tenant Response store / cross-process store ownership
* Changes to OpenAI Responses schemas or projection modes (RFC-001)

---

## 3. Background & Motivation

RFC-002 delivered request-carried binding, session admission, and a thread pool inspired by soothe-daemon. Gaps remain that cause production instability under load or failure:

1. Dropping messages when the asyncio response queue is full can lose `ready` and hang a worker forever.
2. Idle-timeout and recycle knobs exist in config but are not enforced.
3. Dead worker threads permanently reduce pool capacity.
4. Cancelled turns may leave orphan tasks on the worker loop.
5. Unvalidated `metadata.workspace` overrides can target arbitrary host paths.
6. `interaction_mode` can flip mid-session contrary to documented pin semantics.

---

## 4. Design Principles

1. **Control frames are never dropped** — `done`, `error`, `cancelled`, and `ready` MUST be delivered (block with timeout); only optional `event` payloads MAY be dropped under backpressure after timeout.
2. **Fail closed on stuck workers** — ready timeout ⇒ cancel + mark dead + respawn capacity.
3. **Bound worker lifetime** — scaled workers idle out; all workers recycle after `max_requests_per_worker`.
4. **Preserve RFC-002 contracts** — `IsolatedRunRequest`, `AgentAdapter`, and HTTP surface unchanged except documented options/errors.
5. **Thin port** — reuse daemon concepts; no daemon imports.

---

## 5. Architecture

### 5.1 Hardened ThreadPool

```text
IsolatingRuntimeBackend
  ├── SessionAdmission
  ├── WorkspaceResolver (+ override policy)
  ├── InteractionModeGate (pin per session)
  └── ThreadPool
        ├── ResponseBridge (await put; control vs event)
        ├── Worker threads (baseline | scaled)
        ├── HealthWatchdog (respawn)
        └── PoolMetrics
```

### 5.2 Response bridge

| Message | Delivery |
|---------|----------|
| `event` | Await queue capacity with short timeout; on timeout log + drop content only |
| `done` / `error` / `cancelled` / `ready` | Await queue capacity with long timeout; MUST NOT drop; on failure treat worker as failed |

### 5.3 Worker lifecycle

| Kind | Spawn | Idle | Max requests |
|------|-------|------|----------------|
| Baseline | `min_size` at start | Wait forever (poll) | Exit + respawn when exceeded |
| Scaled | On demand up to `max_size` | Exit after `idle_timeout_seconds` | Exit when exceeded (no forced respawn if above min) |

Health watchdog (≈1s): if a thread is dead, remove it; if live count &lt; `min_size`, spawn baseline replacements; notify waiters.

### 5.4 Ready barrier

After terminal frame, consumer waits for `ready` up to `ready_timeout_seconds` (default 30). On timeout: set cancel, force-release bookkeeping, mark worker dead for respawn. Do not return the same thread to IDLE.

### 5.5 Orphan cleanup

After each turn’s stream task finishes (success, cancel, error, timeout), cancel other non-done tasks on that worker’s asyncio loop before `prepare_for_request` / `cleanup` and before emitting `ready`.

### 5.6 Workspace policy

1. Default hashed path under `$FLOWJET_HOME/data/workspaces/` — unchanged.
2. If `metadata.workspace` is set:
   - If `FLOWJET_ALLOW_EXTERNAL_WORKSPACE=true` → resolve/create as today.
   - Else path MUST resolve under `$FLOWJET_HOME` (or equal home); otherwise reject with a clear runtime/OpenAI error (`invalid_workspace`).

### 5.7 Interaction mode pin

First admitted turn for a `session` records `interaction_mode` (`agent`|`ask`). Later turns with a different mode MUST fail with `interaction_mode_conflict` (HTTP 400). Omitted mode continues the pinned value (default `agent` if never set).

### 5.8 Metrics / health

Expose a read-only snapshot: `total`, `idle`, `busy`, `dead`, `requests_completed`, `ready_timeouts`, `events_dropped`. `/health` MAY include a `pool` object when the isolating backend is wired.

### 5.9 Timeouts

| Knob | Default | Meaning |
|------|---------|---------|
| `request_timeout_seconds` | `0` (none) | Per-turn agent timeout; production SHOULD set &gt; 0 |
| `ready_timeout_seconds` | `30` | Max wait for post-turn `ready` |
| `event_enqueue_timeout_seconds` | `5` | Backpressure wait for `event` |
| `control_enqueue_timeout_seconds` | `60` | Wait for control frames |

---

## 6. Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `FLOWJET_THREAD_POOL_MIN` | `2` | Baseline workers |
| `FLOWJET_THREAD_POOL_MAX` | `8` | Max workers |
| `FLOWJET_THREAD_POOL_IDLE_TIMEOUT` | `300` | Scaled-worker idle exit (s); `0` = never |
| `FLOWJET_MAX_REQUESTS_PER_WORKER` | `100` | Recycle after N turns; `0` = unlimited |
| `FLOWJET_REUSE_RUNNER` | `true` | Reuse adapter per worker |
| `FLOWJET_REQUEST_TIMEOUT` | `0` | Per-run timeout (s); production SHOULD set |
| `FLOWJET_READY_TIMEOUT` | `30` | Ready-barrier timeout (s) |
| `FLOWJET_ALLOW_EXTERNAL_WORKSPACE` | `false` | Allow workspace paths outside `FLOWJET_HOME` |

---

## 7. Relationship to Other RFCs

* **RFC-002**: Defines isolation model; this RFC makes the pool production-safe without changing the public OpenAI contract.
* **RFC-001**: Error codes for workspace / mode conflict surface as OpenAI-style errors when raised through `ResponseService`.
* Implementation: [IG-003](../impl/IG-003-production-isolation-hardening.md).

---

## 8. Open Questions

* Whether `/health` should remain unauthenticated while embedding pool metrics (default: yes, counts only, no session ids).
* Whether production images SHOULD force a non-zero `FLOWJET_REQUEST_TIMEOUT` in deploy compose (recommended: yes, e.g. 600).

---

## 9. References

* soothe-daemon `thread_runner.py` / `response_bridge.py` (reference only)
* [RFC-002](RFC-002-isolated-thread-pool-runtime.md)
