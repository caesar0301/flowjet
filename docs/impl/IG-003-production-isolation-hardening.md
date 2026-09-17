# Production Isolation Hardening Implementation Architecture

> Implementation guide for RFC-003 production hardening of the RFC-002 ThreadPool.
>
> **Crate/Module**: `flowjet_server.agent_runtime.isolation`, `bridges.nano`, `http`, `config`
> **Source**: Derived from [RFC-003](../specs/RFC-003-production-isolation-hardening.md)
> **Related RFCs**: RFC-001, RFC-002, RFC-003
> **Language**: Python 3.11+

---

## 1. Overview

### 1.1 Purpose

Concrete bridge, lifecycle, watchdog, workspace policy, mode pin, metrics, config, and tests for RFC-003.

### 1.2 Spec Compliance

MUST NOT contradict RFC-001–003. Prefer extending `pool.py` / `workspace.py` / `backend.py` over new packages unless a file exceeds ~600 lines.

---

## 2. Module Changes

```
agent_runtime/isolation/
  pool.py              # bridge, lifecycle, watchdog, metrics, ready timeout
  workspace.py         # allow_external policy
  backend.py           # InteractionModeGate + workspace flags + metrics passthrough
  mode_gate.py         # NEW: per-session interaction_mode pin
config.py              # new knobs → PoolSettings
http/app.py            # /health includes pool snapshot when available
```

---

## 3. PoolSettings

```python
@dataclass(slots=True)
class PoolSettings:
    min_size: int = 2
    max_size: int = 8
    idle_timeout_seconds: float = 300.0
    max_requests_per_worker: int = 100  # 0 = unlimited
    reuse_runner: bool = True
    request_timeout_seconds: float = 0.0
    ready_timeout_seconds: float = 30.0
    event_enqueue_timeout_seconds: float = 5.0
    control_enqueue_timeout_seconds: float = 60.0
    response_queue_maxsize: int = 200
```

---

## 4. ResponseBridge

- `emit(msg_type, payload)` from worker thread via `asyncio.run_coroutine_threadsafe(queue.put(...), loop)`.
- Control set: `done`, `error`, `cancelled`, `ready` → `result(timeout=control_enqueue_timeout)`.
- `event` → `result(timeout=event_enqueue_timeout)`; on timeout increment `events_dropped` and return.
- On control delivery failure: log error; worker still emits best-effort `ready` in `finally` when possible.

---

## 5. Worker body

Args add: `is_baseline`, `idle_timeout_seconds`, `max_requests`, enqueue timeouts, stop_event.

Loop:

1. Baseline: `queue.get(timeout=1.0)` / continue on empty while not stopped.
2. Scaled: `queue.get(timeout=idle_timeout)` → exit on empty if idle_timeout &gt; 0.
3. After each request: `requests_completed += 1`; if `max_requests &gt; 0` and completed ≥ max → break.
4. After stream finally: `_cancel_orphan_loop_tasks(loop)` then prepare/cleanup then `bridge.emit("ready")`.

Spawn scaled workers with `is_baseline=False`. Track `is_baseline` on `WorkerState`.

---

## 6. Submit / ready timeout

```python
# after terminal:
await asyncio.wait_for(wait_ready(), timeout=settings.ready_timeout_seconds)
# TimeoutError → cancel_event.set(); mark worker DEAD; pop maps; notify; do not mark_idle
```

Consumer disconnect path: same ready wait with timeout.

---

## 7. Health watchdog

`asyncio.create_task` on `start()`; cancel on `shutdown()`.

Every 1s:

- For each worker not alive → `_handle_dead_worker` (route pending error if busy, remove, respawn if live &lt; min_size or was baseline).
- `notify_all` on `_worker_available`.

---

## 8. WorkspaceResolver

```python
def __init__(self, home: Path, *, allow_external_workspace: bool = False): ...
def resolve(...) -> Path:
    # override: resolve; if not allow_external and not is_relative_to(home): raise ValueError
```

`IsolatingRuntimeBackend.stream_run` maps `ValueError` → caller; `ResponseService` / routes should surface as OpenAIError 400 `invalid_workspace`. Prefer raising `OpenAIError` from a thin helper in backend or catch in service.

Simplest: backend raises `OpenAIError` if imported carefully — better keep isolation free of openai_compat. Raise `ValueError("invalid_workspace: ...")` and catch in `ResponseService._prepare` / stream path when iterating… Actually workspace resolve happens inside `stream_run` after prepare. Catch in `ResponseService.create` / `create_stream` when iterating events is late. Better: resolve in service is wrong layer.

Option: `IsolatingRuntimeBackend.stream_run` yields `RunFailed(code="invalid_workspace")` immediately. Or raise a small `IsolationError` in isolation package that service maps.

```python
# isolation/errors.py
class IsolationError(Exception):
    def __init__(self, message: str, *, code: str): ...
```

Service maps to OpenAIError.

---

## 9. InteractionModeGate

```python
class InteractionModeGate:
    def resolve(self, session: str, requested: str) -> str:
        # pin first; conflict → IsolationError(code="interaction_mode_conflict")
```

Called from `IsolatingRuntimeBackend.stream_run` after session allocation; merge pinned mode into metadata before building `IsolatedRunRequest`.

---

## 10. Health

```python
@app.get("/health")
async def health():
    body = {"status": "ok"}
    metrics = getattr(backend, "pool_metrics", None)
    if callable(metrics):
        body["pool"] = metrics()
    return body
```

---

## 11. Testing

Full feature → test map: [TEST-COVERAGE-MATRIX.md](TEST-COVERAGE-MATRIX.md).

| Test | Focus |
|------|-------|
| Bridge does not drop ready under pressure | Fill queue; control still arrives |
| Event drop counter | Tiny queue + flood → `events_dropped > 0` |
| Scaled idle exit | max=2, min=1, short idle → worker count shrinks |
| Max requests recycle | max_requests=1 → second turn uses new worker / prepare path |
| Ready timeout | Adapter hangs in prepare → worker marked dead, pool recovers |
| Request timeout | Slow adapter → `worker_error` |
| Cancel session | In-flight turn cancelled via `cancel_session` |
| Dead worker respawn | Kill thread → watchdog restores min_size |
| Orphan cleanup | Background task cancelled between turns |
| External workspace rejected | allow_external=False → HTTP 400 |
| Mode pin conflict | ask then agent → HTTP 400 |
| Comprehensive server features | `tests/test_server_features.py` |

---

## 12. App wiring

`Settings.pool_settings()` and `build_isolating_nano_backend(...)` pass new fields. `IsolatingRuntimeBackend` accepts `allow_external_workspace`.
