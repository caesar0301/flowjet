# OpenAI-Compatible Server Implementation Architecture

> Implementation guide for the Phase-1 OpenAI Responses adapter in flowjet-server.
>
> **Crate/Module**: `flowjet_server`
> **Source**: Derived from [RFC-001](../specs/RFC-001-openai-compatible-api.md)
> **Related RFCs**: RFC-001
> **Language**: Python 3.11+
> **Framework**: FastAPI + Uvicorn

---

## 1. Overview

### 1.1 Purpose

Concrete module layout, types, interfaces, and testing strategy for RFC-001 Phase 1: FastAPI shell, reusable `openai_compat`, Agent Runtime Protocol, and a nano bridge (with a fake backend for tests).

### 1.2 Scope

**In Scope**:

- Package `flowjet_server` with layered modules per RFC-001
- In-memory run store, SSE projection (`report` / `progress` / `developer`)
- Bearer auth when `FLOWJET_API_KEY` is set
- `FakeRuntimeBackend` for unit/integration tests
- Nano bridge skeleton that implements `RuntimeBackend` (real soothe-nano wiring may be thin/optional until nano is a declared dependency)

**Out of Scope**:

- Background runs, cancel, `previous_response_id`
- Durable run store
- OpenAPI artifact generation
- Full port of flowjet-agent progress formatting (nano bridge uses a minimal equivalent)

### 1.3 Spec Compliance

This guide **MUST NOT** contradict RFC-001. Dependency rule: `openai_compat` → `agent_runtime` ← `bridges.*`; soothe-nano only under `bridges/nano/`.

---

## 2. Architectural Position

### 2.1 System Context

```
Client → flowjet_server.http (FastAPI)
              → openai_compat
                    → agent_runtime.RuntimeBackend
                          ↑
                    bridges.nano | FakeRuntimeBackend
```

### 2.2 Dependency Graph

```
flowjet_server.http
        │
        ▼
flowjet_server.openai_compat ──► flowjet_server.agent_runtime
                                        ▲
                                        │
                        flowjet_server.bridges.nano
                        (tests: FakeRuntimeBackend)
```

### 2.3 Module Responsibilities

| Module | Responsibility | Dependencies |
|--------|----------------|--------------|
| `http` | App factory, auth middleware, `/health`, mount routes | `openai_compat`, `agent_runtime`, bridge factory |
| `openai_compat` | Schemas, run store, projection, `/v1/*` routes | `agent_runtime` only |
| `agent_runtime` | Events + `RuntimeBackend` protocol | stdlib / typing |
| `bridges.nano` | soothe-nano → runtime events | `agent_runtime`, optional `soothe-nano` |

### 2.4 Dependency Constraints

- **MUST** keep `openai_compat` free of soothe-nano imports
- **MUST NOT** put CoT, prompts, or tool args on runtime events or SSE
- **MAY** inject `FakeRuntimeBackend` in tests via `create_app(backend=...)`

---

## 3. Module Structure

```
flowjet-server/
├── pyproject.toml
├── src/
│   └── flowjet_server/
│       ├── __init__.py
│       ├── __main__.py              # uvicorn entry
│       ├── config.py                # Settings from env
│       ├── http/
│       │   ├── __init__.py
│       │   ├── app.py               # create_app()
│       │   └── auth.py              # Bearer dependency
│       ├── agent_runtime/
│       │   ├── __init__.py
│       │   ├── events.py            # dataclasses / Typed events
│       │   ├── protocol.py          # RuntimeBackend Protocol
│       │   └── fake.py              # FakeRuntimeBackend
│       ├── openai_compat/
│       │   ├── __init__.py
│       │   ├── errors.py
│       │   ├── schemas.py
│       │   ├── store.py             # InMemoryRunStore
│       │   ├── projection.py        # events → SSE / Response
│       │   ├── service.py           # create/get/delete orchestration
│       │   └── routes.py            # APIRouter /v1
│       └── bridges/
│           ├── __init__.py
│           └── nano/
│               ├── __init__.py
│               └── backend.py       # NanoRuntimeBackend
└── tests/
    ├── conftest.py
    ├── test_agent_runtime_fake.py
    ├── test_projection.py
    └── test_api_responses.py
```

---

## 4. Core Types

### 4.1 Agent Runtime Events (`agent_runtime/events.py`)

```python
@dataclass(frozen=True, slots=True)
class RunStarted:
    run_id: str
    model: str
    session: str | None = None

@dataclass(frozen=True, slots=True)
class Progress:
    stage: str
    message: str

@dataclass(frozen=True, slots=True)
class ToolStarted:
    tool: str
    call_id: str | None = None

@dataclass(frozen=True, slots=True)
class ToolCompleted:
    tool: str
    ok: bool
    call_id: str | None = None
    duration_ms: int | None = None

@dataclass(frozen=True, slots=True)
class OutputTextDelta:
    delta: str

@dataclass(frozen=True, slots=True)
class InterruptWaiting:
    message: str | None = None

@dataclass(frozen=True, slots=True)
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

@dataclass(frozen=True, slots=True)
class RunCompleted:
    output_text: str
    usage: UsageInfo | None = None

@dataclass(frozen=True, slots=True)
class RunFailed:
    message: str
    code: str | None = None

RuntimeEvent = (
    RunStarted | Progress | ToolStarted | ToolCompleted
    | OutputTextDelta | InterruptWaiting | RunCompleted | RunFailed
)
```

### 4.2 RunRequest / ModelInfo

```python
@dataclass(frozen=True, slots=True)
class RunRequest:
    model: str
    input_text: str
    session: str | None = None
    metadata: dict[str, Any] | None = None
    run_id: str | None = None

@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    owned_by: str = "flowjet"
```

### 4.3 OpenAI Schemas (`openai_compat/schemas.py`)

Pydantic models:

- `FlowjetOptions`: `projection: Literal["report","progress","developer"] = "report"`, `session: str | None`, `metadata: dict | None`
- `CreateResponseRequest`: `model`, `input` (str | list), `stream: bool = False`, `flowjet: FlowjetOptions | None`
- Response object dict builders matching RFC-001 §10.5

**Input normalization**: if `input` is a string, use as-is; if list, concatenate text parts from message/content items into one `input_text` for Phase 1.

**Ignored unknown fields**: use Pydantic `model_config = ConfigDict(extra="ignore")` on create request.

### 4.4 Settings (`config.py`)

| Option | Env | Default |
|--------|-----|---------|
| `api_key` | `FLOWJET_API_KEY` | `None` (auth off) |
| `nano_config` | `FLOWJET_NANO_CONFIG` | Optional path to nano.yml |
| `host` / `port` | `FLOWJET_HOST` / `FLOWJET_PORT` | `0.0.0.0` / `8618` |
| `models` | `FLOWJET_MODELS` | `default` (comma-separated ids) |

---

## 5. Key Interfaces

### 5.1 RuntimeBackend

```python
class RuntimeBackend(Protocol):
    async def list_models(self) -> list[ModelInfo]: ...

    def stream_run(
        self, request: RunRequest
    ) -> AsyncIterator[RuntimeEvent]: ...

    async def delete_run(self, run_id: str) -> None:
        """Best-effort; default no-op."""
        ...
```

### 5.2 FakeRuntimeBackend

Deterministic event sequence for tests:

1. `RunStarted`
2. `Progress(stage="Working", message="Thinking…")`
3. optional `ToolStarted` / `ToolCompleted` when `metadata={"emit_tools": True}`
4. `OutputTextDelta` chunks for a fixed answer (echo input or `"ok"`)
5. `RunCompleted`

### 5.3 ProjectionEngine

```python
class ProjectionEngine:
    def __init__(self, mode: ProjectionMode, response_id: str, model: str): ...

    def handle(self, event: RuntimeEvent) -> list[dict[str, Any]]:
        """Return zero or more SSE JSON payloads (with type + sequence_number)."""

    def final_response(self) -> dict[str, Any]:
        """OpenAI Response object snapshot for store / completed event."""
```

Lifecycle for text (once): emit `response.created`, `response.in_progress`, output item/part added, then deltas, then done events, then `response.completed` on `RunCompleted`.

### 5.4 InMemoryRunStore

```python
class InMemoryRunStore:
    def put(self, response_id: str, body: dict[str, Any]) -> None: ...
    def get(self, response_id: str) -> dict[str, Any] | None: ...
    def delete(self, response_id: str) -> bool: ...
```

### 5.5 ResponseService

Orchestrates create (stream and non-stream), get, delete, list models.

---

## 6. Implementation Details

### 6.1 SSE formatting

```text
event: {type}
data: {json}

```

Also support clients that only parse `data:` lines (include `"type"` inside JSON). Emit SSE comment heartbeats (`: ping`) every 15s while waiting if the run is idle (optional Phase 1 nicety; skip if stream is continuously active).

### 6.2 Auth

If `settings.api_key` is set, require `Authorization: Bearer <key>`; else allow all. Failures → HTTP 401 + OpenAI error JSON.

### 6.3 Nano bridge (Phase 1 skeleton)

- If `soothe_nano` is not installed, constructing `NanoRuntimeBackend` raises a clear error; app defaults to `fake`.
- When available: `stream_run` calls `agent.astream` with modes `messages`, `updates`, `custom`; map per RFC-001 §9 with a minimal progress mapper (skip `soothe.output.*`, `soothe.internal.policy.checked`, etc.).
- Allocate `fj-<uuid>` session when request session is None.

### 6.4 ID allocation

- Response ids: `resp_` + uuid4 hex
- Message/item ids: `msg_` + uuid4 hex

---

## 7. Error Handling

```python
class OpenAIError(Exception):
    def __init__(self, message: str, *, type: str, code: str | None = None,
                 param: str | None = None, status_code: int = 400): ...
```

| Category | Approach |
|----------|----------|
| Validation | 400 `invalid_request_error` |
| Auth | 401 `authentication_error` |
| Not found | 404 `invalid_request_error` / `response_not_found` |
| Backend failure | 500 `server_error` or stream `response.failed` |

---

## 8. Configuration

See §4.4. App factory: `create_app(settings=None, backend=None)` for DI in tests.

---

## 9. Testing Strategy

### 9.1 Unit Tests

| Component | Focus |
|-----------|-------|
| `FakeRuntimeBackend` | Event order and fields |
| `ProjectionEngine` | report hides progress/tools; progress emits `response.flowjet.progress`; developer emits tool events; no tool args |
| `InMemoryRunStore` | put/get/delete |

### 9.2 Integration Tests

`httpx.AsyncClient` + `ASGITransport` against `create_app(backend=FakeRuntimeBackend())`:

- `GET /health`
- `GET /v1/models`
- `POST /v1/responses` non-stream → completed output text
- `POST` stream → parse SSE, expect `response.output_text.delta` + `response.completed`
- `GET` / `DELETE` `/v1/responses/{id}`
- Auth on/off

### 9.3 Test Utilities

Parse SSE helper; assert no event payload contains keys like `arguments` / `prompt`.

---

## 10. Migration / Compatibility

Greenfield package. No migration. CLI entry: `python -m flowjet_server` or `flowjet-server` script → uvicorn.

---

## Appendix A: RFC Requirement Mapping

| RFC Requirement | Guide Section | Implementation |
|-----------------|---------------|----------------|
| Layer dependency rule | §2–3 | package layout |
| Runtime events | §4.1 | `events.py` |
| RuntimeBackend ops | §5.1 | `protocol.py` |
| Projection modes + SSE | §5.3, §6.1 | `projection.py` |
| Endpoints + auth + errors | §5.5, §7 | `routes.py`, `auth.py` |
| Nano mapping | §6.3 | `bridges/nano/backend.py` |
| In-memory store | §5.4 | `store.py` |

---

## Appendix B: Revision History

| Date | RFC Version | Changes |
|------|-------------|---------|
| 2026-07-31 | RFC-001 Draft | Initial guide |
