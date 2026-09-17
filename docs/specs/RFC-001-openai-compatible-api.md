# RFC-001: OpenAI-Compatible API Architecture

**Status**: Draft  
**Authors**: FlowJet  
**Created**: 2026-07-31  
**Last Updated**: 2026-07-31  
**Depends on**: —  
**Supersedes**: [docs/drafts/api-spec.md](../drafts/api-spec.md)  
**Kind**: Architecture Design  

---

## 1. Abstract

`flowjet-server` is an OpenAI Responses–compatible HTTP service. It is a **protocol adapter**, not an agent framework: clients speak the OpenAI Responses API; the server projects an internal **Agent Runtime Protocol** onto that surface. Phase 1 binds soothe-nano through a dedicated nano bridge. The OpenAI-compatible module never imports soothe-nano, so other agent runtimes can plug in later without changing the public API.

---

## 2. Scope and Non-Goals

### 2.1 Scope

This RFC defines:

* Three-layer architecture: FastAPI HTTP shell, reusable `openai_compat` module, Agent Runtime Protocol (`agent_runtime`), and `bridges.nano`
* Phase-1 public HTTP contract: `POST/GET/DELETE /v1/responses`, `GET /v1/models`, `GET /health`
* SSE streaming, projection modes (`report`, `progress`, `developer`), and the `flowjet` request namespace
* Agent Runtime Protocol operations and event vocabulary (contract-level)
* Nano bridge mapping from soothe-nano / flowjet-agent stream semantics into runtime events
* Auth, error shape, run store semantics, and long-running SSE guidance for Phase 1
* Explicit Phase-2 / Phase-3 roadmap

### 2.2 Non-Goals

This RFC does **not** define:

* Application source code or package scaffolding beyond module boundaries
* OpenAPI / JSON Schema artifact files
* Deployment topology, multi-tenancy, or billing
* Background runs, cancellation APIs, or `previous_response_id` as Phase-1 normative behavior
* Human-in-the-loop resume HTTP APIs
* Additional agent bridges beyond documenting the extension point

---

## 3. Background & Motivation

OpenAI SDKs are the de facto client ecosystem for chat and agent-style products. FlowJet’s agent runtime today is [soothe-nano](https://github.com/mirasoth/soothe-nano), used by sibling CLI [flowjet-agent](https://github.com/caesar0301/flowjet-agent) (`fj`). That CLI already projects nano streams into user-visible progress without exposing chain-of-thought or tool arguments by default.

`flowjet-server` must:

1. Let existing OpenAI SDK clients work by changing only `base_url` (and API key).
2. Hide nano internals (planner, CoT, prompts, tool args, sub-agent chatter).
3. Keep the HTTP/OpenAI layer reusable for non-nano backends.
4. Stay a thin adapter around the runtime, not a second agent framework.

Coupling the Responses API directly to soothe-nano would freeze both sides together. An intermediate Agent Runtime Protocol is the stability boundary.

---

## 4. Architecture Overview

### 4.1 System Context

```text
                OpenAI SDK (Python / JS / Go / …)
                              │
                   OpenAI Responses API (HTTP + SSE)
                              │
                        flowjet-server
         ┌────────────────────┼────────────────────┐
         │  FastAPI HTTP shell │                   │
         │  openai_compat      │← Agent Runtime    │
         │  (projection)       │  Protocol         │
         └────────────────────┼────────────────────┘
                              │
                    bridges.nano (Phase 1)
                              │
                       soothe-nano
                              │
                    LLMs / MCP / tools / skills
```

### 4.2 Component Diagram

```text
flowjet_server/
  http/              # FastAPI: routes, auth middleware, SSE transport, lifespan
  openai_compat/     # Responses protocol + projection + run store (agent-agnostic)
  agent_runtime/     # RuntimeBackend protocol + typed runtime events
  bridges/
    nano/            # soothe-nano → Agent Runtime Protocol
```

```text
Client ──► FastAPI ──► openai_compat ──► RuntimeBackend
                                              ▲
                                              │
                                       bridges.nano
                                              │
                                       SootheNanoAgent
```

**Dependency rule**: `openai_compat` → `agent_runtime` ← `bridges.*`.  
There is **no** import edge from `openai_compat` (or `http`) to soothe-nano.

### 4.3 Design Principles

1. **Results, not internals** — The public API exposes final output and optional sanitized progress/tool summaries, never CoT or prompts.
2. **HTTP is an adapter** — Projection decides what becomes visible; the runtime may evolve freely.
3. **Stable public contract** — OpenAI Responses shapes stay stable; FlowJet extensions live under `flowjet` / `response.flowjet.*`.
4. **Pluggable backends** — New agents implement `RuntimeBackend` under `bridges/<name>/` without touching `openai_compat`.

---

## 5. Components

### 5.1 FastAPI HTTP Shell (`http/`)

**Responsibility**: Mature ASGI HTTP surface only.

* Mount OpenAI-compatible routes from `openai_compat`
* Bearer auth middleware (when API key is configured)
* SSE response wiring (`text/event-stream`)
* Application lifespan (construct and inject `RuntimeBackend`)
* `GET /health` (liveness; may live here rather than inside `openai_compat`)

Does **not** contain projection logic or soothe-nano imports.

### 5.2 OpenAI-Compatible Module (`openai_compat/`)

**Responsibility**: Reusable OpenAI Responses protocol layer.

* Request / response schemas for `/v1/responses` and `/v1/models`
* Run store (Phase 1: in-process)
* Projection modes and SSE event emission
* OpenAI-style error objects
* Depends only on `agent_runtime.RuntimeBackend` and runtime events

This module must be usable against a fake or alternate `RuntimeBackend` in tests and against future bridges.

### 5.3 Agent Runtime Protocol (`agent_runtime/`)

**Responsibility**: Stable internal runtime interface.

* `RuntimeBackend` protocol (operations below)
* Typed runtime events (section 8)
* Opaque session / thread identifiers
* No OpenAI types; no soothe-nano types

### 5.4 Nano Bridge (`bridges/nano/`)

**Responsibility**: soothe-nano adapter implementing `RuntimeBackend`.

* Build / hold `SootheNanoAgent` (config from nano.yml / env, same family as flowjet-agent)
* Translate `agent.astream(..., stream_mode=["messages", "updates", "custom"], subgraphs=True)` into runtime events
* Reuse flowjet-agent semantics: `friendly_progress`-style custom-event mapping; final-answer text path; tool activity without arguments; hide intermediate narration by default

---

## 6. Data Flow

### 6.1 Primary Flow (streaming create)

```text
POST /v1/responses  (stream=true)
        │
        ▼
http auth + route
        │
        ▼
openai_compat: validate request, allocate response id, persist run (in_progress)
        │
        ▼
RuntimeBackend.stream_run(model, input, session, …)
        │
        ▼
bridges.nano: SootheNanoAgent.astream → Agent Runtime events
        │
        ▼
openai_compat projection (report | progress | developer)
        │
        ▼
SSE: OpenAI lifecycle events + optional response.flowjet.* events
        │
        ▼
response.completed / response.failed → run store updated
```

### 6.2 Flow Description

1. Client creates a response with `model` (logical agent/profile id), `input`, optional `stream`, optional `flowjet` block.
2. `openai_compat` creates a run record (`resp_…` id) and calls the injected `RuntimeBackend`.
3. The nano bridge maps nano stream chunks to Agent Runtime events only.
4. Projection maps those events to OpenAI SSE (and namespaced FlowJet events). Stock OpenAI SDKs ignore unknown event types.
5. On completion, the run store holds the final Response object for `GET /v1/responses/{id}`.

Non-stream (`stream=false`) buffers until `RunCompleted` / `RunFailed`, then returns one JSON Response object.

---

## 7. Invariants and Constraints

### 7.1 Architectural Invariants

| Invariant | Meaning | Consequence of Violation |
|-----------|---------|--------------------------|
| No CoT on the wire | Chain-of-thought, scratchpad, planner internals never appear in API events or Response objects | Security / product boundary broken |
| No prompts or tool args | Tool parameters, system prompts, and skill prompts stay private | Clients can exfiltrate agent internals |
| openai_compat is nano-free | OpenAI module imports only `agent_runtime` | Backend swaps require rewriting the protocol layer |
| Projection owns visibility | Runtime may emit rich events; projection filters per mode | Mode semantics leak inconsistently |
| Namespaced extensions | FlowJet SSE types use `response.flowjet.*`; request extras use `flowjet` | Collisions with future OpenAI fields/events |

### 7.2 Dependency Constraints

| Constraint | Rule |
|------------|------|
| Layer edges | `http` → `openai_compat` → `agent_runtime` ← `bridges.*` |
| soothe-nano | Importable only under `bridges/nano/` |
| flowjet-agent code | Reference for semantics; do not require the CLI package as a runtime dependency of the OpenAI module |

---

## 8. Agent Runtime Protocol

Contract-level interface. Concrete Python signatures belong in an implementation guide; this section is normative for behavior and event shape.

### 8.1 Operations (Phase 1)

| Operation | Purpose |
|-----------|---------|
| `list_models()` | Return logical model/profile ids the backend can run |
| `stream_run(...)` | Execute a run; yield Agent Runtime events asynchronously |
| `get_run(run_id)` | Optional backend-side handle; Phase 1 may rely on `openai_compat` run store only |
| `delete_run(run_id)` | Best-effort cancel/cleanup hint; Phase 1 DELETE primarily clears the HTTP run store |

`stream_run` inputs (conceptual):

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Logical agent / profile id from `/v1/models` |
| `input` | string or structured messages | User input (OpenAI `input` already normalized by `openai_compat`) |
| `session` | string \| null | Opaque thread/session id; nano maps to LangGraph `thread_id` |
| `metadata` | object \| null | Pass-through opaque metadata |

### 8.2 Event Vocabulary

Events **MUST NOT** carry CoT, prompts, or tool arguments.

| Event | Fields (conceptual) | Notes |
|-------|---------------------|-------|
| `RunStarted` | `run_id`, `model`, `session?` | Start of execution |
| `Progress` | `stage`, `message` | Sanitized milestone / status line |
| `ToolStarted` | `tool`, `call_id?` | Tool name only |
| `ToolCompleted` | `tool`, `call_id?`, `ok`, `duration_ms?` | No args, no raw result body |
| `OutputTextDelta` | `delta` | Final-answer token/chunk path |
| `InterruptWaiting` | `message?` | Agent paused for input; Phase 1: surface as progress / incomplete, no HITL API |
| `RunCompleted` | `output_text`, `usage?` | Terminal success |
| `RunFailed` | `message`, `code?` | Terminal failure |

### 8.3 Session Identity

* `flowjet.session` on the HTTP request maps to the runtime `session` string.
* If omitted, the nano bridge allocates a new id (e.g. `fj-<uuid>`, matching flowjet-agent).
* Session is opaque to `openai_compat`.

---

## 9. Nano Bridge Mapping

Informed by flowjet-agent `stream_query` / `friendly_progress` — **not** part of `openai_compat`.

| Nano / stream source | Agent Runtime event |
|----------------------|---------------------|
| Run begin | `RunStarted` |
| `custom` dict mapped by friendly-progress rules | `Progress` (or skip noisy internals) |
| Tool call activity (name known; args discarded) | `ToolStarted` |
| `ToolMessage` completion / error | `ToolCompleted` (`ok` false on error) |
| Final AI answer text accumulation | `OutputTextDelta` (and final text on `RunCompleted`) |
| Intermediate AI narration before tools | **Dropped** (fj default: progress line only, not answer) |
| `updates` with `__interrupt__` | `InterruptWaiting` |
| Successful end | `RunCompleted` |
| Uncaught failure | `RunFailed` |

Skipped / never forwarded as Progress: policy-check noise, `soothe.output.*`, protocol plumbing events, and any payload containing prompts or tool argument JSON.

`debug` projection is **not** Phase 1 public API (internal/ops only if ever enabled via config; default off).

---

## 10. Phase-1 Public HTTP Contract

Owned by `openai_compat` (+ `/health` in `http`).

### 10.1 Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/responses` | Create (and optionally stream) a response |
| `GET` | `/v1/responses/{id}` | Retrieve stored response |
| `DELETE` | `/v1/responses/{id}` | Delete stored response (404 if unknown) |
| `GET` | `/v1/models` | List logical models / agent profiles |
| `GET` | `/health` | Liveness (`{"status":"ok"}`) |

Base URL for SDKs: `http://<host>:<port>/v1` (health is outside `/v1` or also acceptable at `/v1/health` if implemented; canonical is `/health`).

### 10.2 Authentication

* When an API key is configured: require `Authorization: Bearer <api_key>`.
* When unset (local dev): authentication may be disabled.
* Mismatch → `401` with OpenAI-style error object.

### 10.3 Error Object

All error responses use:

```json
{
  "error": {
    "message": "human-readable description",
    "type": "invalid_request_error",
    "param": "model",
    "code": "model_not_found"
  }
}
```

`param` and `code` may be null. Types follow OpenAI conventions (`invalid_request_error`, `authentication_error`, `server_error`, …).

### 10.4 Create Request

Compatible core fields:

| Field | Support | Notes |
|-------|---------|-------|
| `model` | required | Logical id from `/v1/models` |
| `input` | required | string or OpenAI input array; `openai_compat` normalizes to runtime input |
| `stream` | optional | default `false` |
| `flowjet` | optional | FlowJet extensions (below) |

**Unknown top-level OpenAI fields**: ignore if harmless for forward-compat, or reject with `invalid_request_error` when they would change semantics the server cannot honor (document per-field in the implementation guide). Do **not** invent conflicting top-level FlowJet fields.

```json
{
  "model": "researcher",
  "input": "Summarize this paper.",
  "stream": true,
  "flowjet": {
    "projection": "progress",
    "session": "fj-abc123",
    "metadata": { "project": "demo" }
  }
}
```

#### `flowjet` object

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `projection` | `"report"` \| `"progress"` \| `"developer"` | `"report"` | Visibility mode |
| `session` | string | allocate new | Opaque session / thread id |
| `interaction_mode` | `"agent"` \| `"ask"` | `"agent"` | soothe-nano dual mode: full agent vs hard read-only ask |
| `metadata` | object | omit | Opaque client metadata |

### 10.5 Response Object (non-stream and `response.completed`)

Minimal Phase-1 shape (OpenAI-compatible subset):

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | `resp_…` |
| `object` | `"response"` | Literal |
| `created_at` | integer | Unix seconds |
| `status` | `"completed"` \| `"failed"` \| `"incomplete"` \| `"in_progress"` | Run status |
| `model` | string | Echo of request model |
| `output` | array | At least one message item with `output_text` content when completed |
| `usage` | object \| null | Token usage when backend provides it; else null / zeros |

### 10.6 SSE Wire Format

Each event:

```text
event: <type>
data: <json>

```

JSON **MUST** include `"type"` matching the event name and a monotonic `sequence_number` (integer, starting at 0 or 1 consistently).

#### Report projection (default) — OpenAI lifecycle

Typical sequence:

1. `response.created`
2. `response.in_progress`
3. `response.output_item.added` (message)
4. `response.content_part.added` (output_text)
5. `response.output_text.delta` (repeated)
6. `response.output_text.done`
7. `response.content_part.done`
8. `response.output_item.done`
9. `response.completed`

`response.output_text.delta` payload includes at least: `type`, `sequence_number`, `item_id`, `output_index`, `content_index`, `delta`.

#### Progress projection

Same text lifecycle as report, plus:

* `response.flowjet.progress` — `{ "type", "sequence_number", "stage", "message" }`

Emitted from Agent Runtime `Progress` (and optionally a sanitized form of `InterruptWaiting`).

#### Developer projection

Same as progress, plus:

* `response.flowjet.tool.started` — `{ "type", "sequence_number", "tool", "call_id?" }`
* `response.flowjet.tool.completed` — `{ "type", "sequence_number", "tool", "call_id?", "ok", "duration_ms?" }`

Never include tool parameters, prompts, or raw tool result bodies.

### 10.7 Projection Table (Agent Runtime → SSE)

| Agent Runtime event | `report` | `progress` | `developer` |
|---------------------|----------|------------|-------------|
| `RunStarted` | drives `response.created` / `in_progress` | same | same |
| `Progress` | hidden | `response.flowjet.progress` | `response.flowjet.progress` |
| `ToolStarted` | hidden | hidden (or coarse progress only if bridge already folded into Progress) | `response.flowjet.tool.started` |
| `ToolCompleted` | hidden | hidden | `response.flowjet.tool.completed` |
| `OutputTextDelta` | `response.output_text.delta` (+ item/part lifecycle once) | same | same |
| `InterruptWaiting` | may end `incomplete` or progress-only | `response.flowjet.progress` | same |
| `RunCompleted` | complete lifecycle + `response.completed` | same | same |
| `RunFailed` | `response.failed` / error event | same | same |

### 10.8 Models

`GET /v1/models` returns OpenAI-style list objects whose `id` values are **logical agent/profile ids** from server config (not necessarily raw LLM model names). `POST /v1/responses` `model` must match one of these ids (or a documented alias).

### 10.9 GET / DELETE responses

* Phase 1 run store is **in-process** (lost on restart).
* `GET` returns the last known Response object or `404`.
* `DELETE` removes the record; does not guarantee stopping an in-flight nano graph in Phase 1 (cancellation is Phase 2).
* Retention: best-effort until process restart or explicit delete; no durability SLA in Phase 1.

### 10.10 Long-running runs (Phase 1)

* Streaming: keep the HTTP/SSE connection open for the full run.
* Emit periodic SSE comments or harmless keepalive events if needed to satisfy proxies (implementation detail; recommend idle heartbeat ≤ 15–30s).
* Document reverse-proxy `proxy_read_timeout` guidance in ops notes (not part of this RFC’s normative API).
* Background mode (`flowjet.background` / OpenAI background) is **out of Phase 1**.

### 10.11 Client usage

```python
from openai import OpenAI

client = OpenAI(api_key="…", base_url="http://localhost:8618/v1")
stream = client.responses.create(
    model="researcher",
    input="Summarize the paper.",
    stream=True,
)
```

No SDK fork required. FlowJet-specific fields may require raw request extras depending on SDK version; projection defaults keep stock clients working without them.

---

## 11. Roadmap

### Phase 1 (this RFC)

* Responses API compatibility (create / get / delete)
* SSE streaming + report / progress / developer projections
* Agent Runtime Protocol + nano bridge
* FastAPI shell, Bearer auth, in-memory run store

### Phase 2

* Conversation continuity (`previous_response_id` and/or first-class session APIs)
* Background runs + poll
* Run cancellation
* Usage accounting fidelity
* Durable run store
* Tracing hooks under `flowjet.trace` (still namespaced)

### Phase 3

* Multi-agent orchestration APIs
* Human-in-the-loop resume
* Workflow execution
* Distributed workers
* MCP-native transport (optional)

---

## 12. Relationship to Other RFCs

* First architecture RFC for `flowjet-server`; no dependencies.
* Future Implementation Interface Design RFCs may tighten Python protocol signatures for `RuntimeBackend` and OpenAI schema structs without changing this architecture.
* Sibling product docs: flowjet-agent naming and stream/progress behavior are **reference**, not normative for the OpenAI wire format.

---

## 13. Open Questions

1. Exact allow/ignore list for unsupported official Responses request fields (resolved in IG).
2. Whether `/health` is duplicated under `/v1/health` for some probes.
3. Whether Phase-1 `InterruptWaiting` should fail the response as `incomplete` vs wait until client disconnect.

---

## 14. Conclusion

`flowjet-server` separates three concerns: a mature FastAPI shell, a reusable OpenAI Responses + projection module, and an Agent Runtime Protocol implemented first by a soothe-nano bridge. That split keeps the public API OpenAI-compatible and secure (no CoT / prompts / tool args), while letting FlowJet progress UX and alternate agent backends evolve behind a stable internal boundary.
