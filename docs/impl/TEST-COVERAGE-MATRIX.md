# flowjet-server Test Coverage Matrix

Attachment to [IG-003](IG-003-production-isolation-hardening.md) / RFC-001–003.
Maps product features → automated tests. Run with `make test` (or targets below).

Tests are split into two categories:

* **Unit tests** (`tests/unit/`) — test isolated components (ProjectionEngine,
  FakeRuntimeBackend, NanoAgentAdapter, ThreadPool, WorkspaceResolver,
  SessionAdmission, InteractionModeGate, Settings) without spinning up the HTTP
  server or a live LLM.
* **Integration tests** (`tests/integration/`) — test interactions across the
  HTTP layer, ResponseService, IsolatingRuntimeBackend, and (for the SDK and
  concurrent suites) a real uvicorn process.

---

## How to run

| Target | Scope |
|--------|--------|
| `make test` | Full suite (unit + integration) |
| `make test-unit` | Unit tests only (isolated components, no HTTP) |
| `make test-integration` | Integration tests only (HTTP/SSE, live uvicorn) |
| `make test-sdk` | OpenAI SDK against live uvicorn + FakeRuntimeBackend |
| `make test-concurrent` | Parallel HTTP/SSE load |
| `make test-features` | Comprehensive server feature ASGI suite |
| `make test-production` | RFC-003 production isolation suite (unit + integration) |

---

## RFC-001 — OpenAI surface & projection

| Feature | Tests |
|---------|--------|
| `GET /health` | `tests/integration/test_api_responses`, `tests/integration/test_server_features` |
| `GET /v1/models` | `tests/integration/test_api_responses`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Create non-stream | `tests/integration/test_api_responses`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Create stream (SSE) | `tests/integration/test_api_responses`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Retrieve / delete | `tests/integration/test_api_responses`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Bearer auth | `tests/integration/test_api_responses`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Unknown model 404 | `tests/integration/test_api_responses`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Projection `report` | `tests/unit/test_projection`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Projection `progress` | `tests/unit/test_projection`, `tests/integration/test_api_responses`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Projection `developer` | `tests/unit/test_projection`, `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| List / message input | `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| `flowjet` extra_body | `tests/integration/test_openai_sdk_compat`, `tests/integration/test_server_features` |
| Fake runtime event order | `tests/unit/test_agent_runtime_fake` |

---

## RFC-002 — Isolation

| Feature | Tests |
|---------|--------|
| Workspace hash vs override | `tests/unit/test_isolation`, `tests/unit/test_production_isolation` |
| Session admission serialize | `tests/unit/test_isolation` |
| Thread pool submit / cancel | `tests/unit/test_isolation` |
| Fresh ContextVar per turn | `tests/unit/test_isolation`, `tests/unit/test_production_isolation` (orphan) |
| Cross-session parallel FS | `tests/unit/test_isolation`, `tests/integration/test_server_features` |
| Same-session serialize | `tests/unit/test_isolation`, `tests/integration/test_server_features` |
| Disconnect before reuse | `tests/unit/test_isolation` |
| Isolating backend HTTP | `tests/integration/test_isolation_http`, `tests/integration/test_server_features` |
| Concurrent unique ids / SSE | `tests/integration/test_concurrent` |

---

## RFC-003 — Production hardening

| Feature | Tests |
|---------|--------|
| Control-frame delivery under backpressure | `tests/unit/test_production_isolation::test_control_frames_*` |
| Event drop counter | `tests/unit/test_production_isolation::test_backpressure_increments_events_dropped` |
| Scaled idle shrink | `tests/unit/test_production_isolation::test_scaled_worker_idles_out` |
| Max-requests recycle | `tests/unit/test_production_isolation::test_max_requests_recycles_baseline` |
| Ready timeout recycle | `tests/unit/test_production_isolation::test_ready_timeout_marks_worker_dead` |
| Request timeout | `tests/unit/test_production_isolation::test_request_timeout_yields_worker_error` |
| Cancel session | `tests/unit/test_production_isolation::test_cancel_session_cancels_in_flight` |
| Dead-worker watchdog respawn | `tests/unit/test_production_isolation::test_dead_worker_respawned_by_watchdog` |
| Orphan task cleanup | `tests/unit/test_production_isolation::test_orphan_tasks_cancelled_between_turns` |
| External workspace reject | `tests/unit/test_production_isolation`, `tests/integration/test_server_features`, `tests/integration/test_production_isolation_http` |
| Interaction mode pin | `tests/unit/test_production_isolation`, `tests/integration/test_server_features`, `tests/integration/test_production_isolation_http` |
| Settings knobs | `tests/unit/test_production_isolation::test_settings_pool_settings_*` |
| `/health` pool metrics | `tests/integration/test_production_isolation_http`, `tests/integration/test_server_features` |

---

## Nano bridge

| Feature | Tests |
|---------|--------|
| Progress / tool mapping | `tests/unit/test_nano_bridge` |
| Force workspace boundary | `tests/unit/test_nano_bridge` |
| Ask mode configurable | `tests/unit/test_nano_bridge` |
| Ask vs Agent e2e (live nano) | `make examples-modes` → `examples/e2e_ask_agent_modes.py` |
| Taint / recycle on fail/cancel | `tests/unit/test_nano_bridge` |

---

## Gaps (not automated here)

* Live LLM / soothe-nano end-to-end (use `make examples-sdk` / `examples-http`)
* Subprocess / Ray isolation (explicit RFC-003 non-goal)
* Multi-process Response store
