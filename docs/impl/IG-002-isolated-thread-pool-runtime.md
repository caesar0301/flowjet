# Isolated Thread-Pool Runtime Implementation Architecture

> Implementation guide for thread-pool isolation behind `RuntimeBackend`.
>
> **Crate/Module**: `flowjet_server.agent_runtime.isolation`, `bridges.nano`
> **Source**: Derived from [RFC-002](../specs/RFC-002-isolated-thread-pool-runtime.md)
> **Related RFCs**: RFC-001, RFC-002
> **Language**: Python 3.11+

---

## 1. Overview

### 1.1 Purpose

Concrete types, pool lifecycle, adapter mapping, config, and tests for RFC-002.

### 1.2 Scope

**In Scope**:

- `agent_runtime/isolation/` (request, workspace, admission, pool, isolating backend)
- `NanoAgentAdapter` (production) and `FakeAgentAdapter` / `FakeRuntimeBackend` (tests only)
- Settings + `config/nano.yml` workspace harden
- Unit/concurrency tests with fake adapter

**Out of Scope**:

- Process pool / Ray
- soothe-daemon imports
- OpenAI projection changes

### 1.3 Spec Compliance

MUST NOT contradict RFC-001 or RFC-002. `openai_compat` MUST remain free of nano/isolation pool imports beyond `RuntimeBackend`.

---

## 2. Module Structure

```
src/flowjet_server/
├── config.py
├── agent_runtime/
│   ├── events.py
│   ├── protocol.py
│   ├── fake.py                 # FakeRuntimeBackend (tests / DI only)
│   └── isolation/
│       ├── __init__.py
│       ├── request.py          # IsolatedRunRequest
│       ├── adapter.py          # AgentAdapter Protocol + FakeAgentAdapter
│       ├── workspace.py        # WorkspaceResolver
│       ├── admission.py        # SessionAdmission
│       ├── pool.py             # ThreadPool
│       └── backend.py          # IsolatingRuntimeBackend
├── bridges/
│   └── nano/
│       ├── __init__.py
│       ├── backend.py          # build_isolating_nano_backend + in-process test helper
│       ├── adapter.py          # NanoAgentAdapter
│       └── mapping.py          # stream → RuntimeEvent
└── http/app.py                 # always wires isolating nano backend
```

---

## 3. Types

### 3.1 IsolatedRunRequest

```python
@dataclass(frozen=True, slots=True)
class IsolatedRunRequest:
    run_id: str
    session: str
    input_text: str
    model: str
    workspace: Path
    thread_id: str | None = None      # defaults to session when None
    metadata: dict[str, Any] | None = None
    request_id: str | None = None

    def effective_thread_id(self) -> str:
        return self.thread_id or self.session
```

### 3.2 AgentAdapter

```python
class AgentAdapter(Protocol):
    def astream(self, req: IsolatedRunRequest) -> AsyncIterator[RuntimeEvent]: ...
    def prepare_for_request(self) -> None: ...
    async def cleanup(self) -> None: ...

AdapterFactory = Callable[[], AgentAdapter]
```

### 3.3 PoolSettings

```python
@dataclass(slots=True)
class PoolSettings:
    min_size: int = 2
    max_size: int = 8
    idle_timeout_seconds: float = 300.0
    reuse_runner: bool = True
    request_timeout_seconds: float = 0.0  # 0 = none
    response_queue_maxsize: int = 200
```

---

## 4. SessionAdmission

- Track `set[str]` of sessions with an active in-flight run (asyncio lock).
- `async with admit(session):` acquires; second enter for same session waits on a per-session `asyncio.Condition` / Event until released.
- Used by `IsolatingRuntimeBackend.stream_run` wrapping `pool.submit`.

---

## 5. WorkspaceResolver

```python
class WorkspaceResolver:
    def __init__(self, home: Path): ...
    def resolve(self, session: str, metadata: dict | None) -> Path: ...
```

- `metadata["workspace"]` if str → `Path(s).expanduser().resolve()`; `mkdir(parents=True, exist_ok=True)`.
- Else `home / "data" / "workspaces" / f"ws_{sha256(session).hexdigest()[:16]}"`.

---

## 6. ThreadPool Lifecycle

1. `start()`: spawn `min_size` workers; each runs `_worker_main` with private event loop.
2. Worker loop: pull `(request_id, IsolatedRunRequest)` from `queue.Queue`; create the `adapter.astream` task with a new `contextvars.Context()`; push `("event", RuntimeEvent) | ("done", None) | ("error", Exc) | ("cancelled", None)` into asyncio queue via `call_soon_threadsafe`.
3. `submit`: await session dispatchable (no other busy worker for same session in pool map); acquire idle worker; stream through the terminal frame and wait for the worker `ready` barrier before releasing it. Consumer disconnect cancels the turn and drains to `ready`.
4. After stream: `prepare_for_request()` if `reuse_runner` else `cleanup()` + rebuild next time. Nano marks its graph tainted on `RunFailed`, exceptions, or cancellation; preparation then discards it so the next turn gets a fresh graph. Clean nano turns retain the compiled graph.
5. `cancel(run_id)` / `cancel_session(session)`: set that worker’s `threading.Event`.
6. `shutdown()`: poison workers, join with timeout.

Same-session: maintain `_session_to_worker` while busy so a second submit for that session waits until the worker marks idle (aligned with admission).

---

## 7. IsolatingRuntimeBackend

```python
class IsolatingRuntimeBackend:
    def __init__(self, *, models, adapter_factory, pool_settings, workspace_resolver, ...): ...
    async def list_models(self) -> list[ModelInfo]: ...
    async def stream_run(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        session = request.session or f"fj-{uuid4()}"
        ws = self._workspaces.resolve(session, request.metadata)
        isolated = IsolatedRunRequest(...)
        async with self._admission.admit(session):
            async for ev in self._pool.submit(isolated):
                yield ev
    async def delete_run(self, run_id: str) -> None:
        self._pool.cancel_run(run_id)
```

Eager-start pool on first `stream_run` or in app lifespan.

---

## 8. Adapter Mapping

### 8.1 Nano → RuntimeEvent

Reuse Phase-1 mapping (messages / updates / custom):

| Nano signal | RuntimeEvent |
|-------------|--------------|
| start | `RunStarted` |
| custom progress | `Progress` |
| `__interrupt__` | `InterruptWaiting` |
| AIMessage tool_calls | `ToolStarted` |
| ToolMessage | `ToolCompleted` |
| final text | `OutputTextDelta` + `RunCompleted` |
| exception | `RunFailed` |

Pass `config={"configurable": {"thread_id": tid, "workspace": str(ws)}}`.

`NanoAgentAdapter` rejects overlapping calls on the same worker-local instance. Its
generation counter is diagnostic only. Normal completed turns reuse the compiled
graph; abnormal turns recycle it before the worker is made available.

---

## 9. Configuration

| Env | Settings field | Default |
|-----|----------------|---------|
| `FLOWJET_HOME` | `home` | `~/.flowjet` |
| `FLOWJET_NANO_CONFIG` | `nano_config` | unset → `$SOOTHE_HOME/config/nano.yml` |
| `FLOWJET_THREAD_POOL_MIN` | `thread_pool_min` | `2` |
| `FLOWJET_THREAD_POOL_MAX` | `thread_pool_max` | `8` |
| `FLOWJET_THREAD_POOL_IDLE_TIMEOUT` | `thread_pool_idle_timeout` | `300` |
| `FLOWJET_REUSE_RUNNER` | `reuse_runner` | `true` |
| `FLOWJET_REQUEST_TIMEOUT` | `request_timeout` | `0` |

`config/nano.yml` sets `security.allow_paths_outside_workspace: false`.
`create_nano_agent_instance` also overwrites the loaded config field to `False`;
this code-level invariant prevents external nano.yml files from enabling escape.

Default dependency: `soothe-nano`. Dev optional extra: `[dev]`.

---

## 10. Testing

| Test | Focus |
|------|-------|
| `test_workspace_resolver` | hash path vs metadata override |
| `test_session_admission` | serialize same session |
| `test_thread_pool_fake_adapter` | submit yields events; cancel |
| `test_pool_cross_session_parallel` | two sessions concurrent |
| `test_isolating_backend_sse` | ASGI + IsolatingRuntimeBackend(fake adapter) |
| Nano mapping unit | recorded chunks → events (no live LLM) |

---

## 11. App Wiring

```python
def build_backend(settings: Settings) -> RuntimeBackend:
    return build_isolating_nano_backend(
        models=settings.model_ids(),
        config_path=settings.nano_config,
        home=settings.home_path(),
        pool_settings=settings.pool_settings(),
    )
```
