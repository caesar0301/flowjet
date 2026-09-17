# FlowJet server reference

Everything you need to run `flowjet-server` (or `fj serve`) beyond the
[quick start in the README](../README.md#server).

## Endpoints

Base path for the OpenAI-compatible API is `/v1`, so an SDK `base_url` should
end in `/v1`.

| HTTP | OpenAI SDK | Purpose |
|------|-----------|---------|
| `GET /health` | — | Liveness + thread-pool metrics |
| `GET /v1/models` | `client.models.list()` | Logical model ids |
| `POST /v1/responses` | `client.responses.create(...)` | Create a response |
| `POST /v1/responses` + SSE | `client.responses.stream(...)` | Streaming response |
| `GET /v1/responses/{id}` | `client.responses.retrieve(id)` | Retrieve a stored response |
| `DELETE /v1/responses/{id}` | `client.responses.delete(id)` | Delete + cancel the run |
| `POST /v1/chat/completions` | `client.chat.completions.create(...)` | Chat Completions (non-stream + SSE) |
| `WS /acp` | — | Agent Client Protocol (JSON-RPC) |

Responses are held in an in-process store; agent conversation state is
persisted by soothe-nano (SQLite by default, Postgres with `deploy/nano.yml`).

## `extra_body.flowjet`

| Field | Values | Default | Meaning |
|-------|--------|---------|---------|
| `projection` | `report` \| `progress` \| `developer` | `report` | How much SSE detail to expose |
| `session` | string | new `fj-<uuid>` | Isolates workspace + conversation thread |
| `interaction_mode` | `agent` \| `ask` | `agent` | `ask` is hard read-only |
| `metadata` | object | — | Opaque bag forwarded to the runtime |

### Projection modes

- **`report`** — final answer plus standard OpenAI lifecycle events.
- **`progress`** — adds `response.flowjet.progress` stages.
- **`developer`** — adds `response.flowjet.tool.started` / `.completed` summaries.

No mode exposes chain-of-thought or tool arguments.

### Ask vs agent

The service builds nano's dual-mode agent and routes each request by
`interaction_mode`:

| Mode | Behavior |
|------|----------|
| `agent` | Full coding agent (read/write tools, shell if enabled in `nano.yml`) |
| `ask` | Read-only: `ls`, `read_file`, `glob`, `grep`, `file_info` — no writes, no shell |

Mode is pinned per session. To switch, start a new session.

```python
with client.responses.stream(
    model="default",
    input="Explain how auth works in this workspace.",
    extra_body={"flowjet": {"projection": "progress", "interaction_mode": "ask"}},
) as stream:
    for event in stream:
        if event.type == "response.flowjet.progress":
            print(event.stage, event.message)
        elif event.type == "response.output_text.delta":
            print(event.delta, end="", flush=True)
```

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `FLOWJET_API_KEY` | unset | If set, require Bearer auth |
| `FLOWJET_MODELS` | `default` | Comma-separated logical model ids |
| `FLOWJET_HOST` | `::` | Bind host (dual-stack IPv4/IPv6) |
| `FLOWJET_PORT` | `8618` | Bind port |
| `FLOWJET_HOME` | `~/.flowjet` | Root for per-session workspaces |
| `FLOWJET_NANO_CONFIG` | unset | Optional `nano.yml` (else `$SOOTHE_HOME/config/nano.yml`) |
| `FLOWJET_THREAD_POOL_MIN` | `2` | Min isolation worker threads |
| `FLOWJET_THREAD_POOL_MAX` | `8` | Max isolation worker threads |
| `FLOWJET_THREAD_POOL_IDLE_TIMEOUT` | `300` | Scaled-worker idle exit seconds (`0` = never) |
| `FLOWJET_MAX_REQUESTS_PER_WORKER` | `100` | Recycle worker after N turns (`0` = unlimited) |
| `FLOWJET_REUSE_RUNNER` | `true` | Reuse agent adapter per worker |
| `FLOWJET_REQUEST_TIMEOUT` | `0` | Per-run timeout seconds (`0` = none — **set this in production**) |
| `FLOWJET_READY_TIMEOUT` | `30` | Max wait for post-turn worker ready |
| `FLOWJET_ALLOW_EXTERNAL_WORKSPACE` | `true` | Honour `metadata.workspace` outside `FLOWJET_HOME`; set `false` to force hashed per-session workspaces |
| `FLOWJET_CORS_ORIGINS` | `*` | Comma-separated browser origins allowed to call the API |

## Security model

The CLI and the server have deliberately different invariants, declared in
`flowjet.core.modes` rather than implied by whichever module builds the agent.

| | CLI | Server |
|---|---|---|
| `allow_paths_outside_workspace` | `True` (your own machine) | **`False`** — per-request workspace, non-overridable |
| `bypass` interaction mode | available | **rejected** — tools must never escape the request workspace |
| Authentication | — | optional `FLOWJET_API_KEY` (Bearer, constant-time comparison) |

Notes:

- The server forces `allow_paths_outside_workspace = false` even when the
  loaded `nano.yml` asks for wider access.
- **The workspace itself is caller-chosen by default.** An ACP client's
  `session/new` `cwd`, or `metadata.workspace` on an HTTP request, is honoured
  even outside `FLOWJET_HOME` (`FLOWJET_ALLOW_EXTERNAL_WORKSPACE=true`). That
  moves the confinement boundary to the caller's directory — it does not
  remove it: tools still cannot escape the selected workspace. Set
  `FLOWJET_ALLOW_EXTERNAL_WORKSPACE=false` to reject external paths and force
  the hashed per-session workspace instead.
- `FLOWJET_CORS_ORIGINS` defaults to `*` with credentials enabled so browser
  clients work out of the box. Set an explicit origin list in production.
- `/acp` (WebSocket) is unauthenticated unless `FLOWJET_API_KEY` is set;
  put it behind a reverse proxy if you expose it publicly.

## Concurrency

Each session admits one in-flight request at a time; extra requests for the
same session queue rather than interleave. Requests execute on a persistent
thread pool (each worker owns an event loop), so long tool calls never block
the HTTP loop. Tune with the `FLOWJET_THREAD_POOL_*` variables.

The session's workspace is the caller's (`session/new` `cwd`, or
`metadata.workspace`), falling back to `$FLOWJET_HOME/data/workspaces/ws_<hash>`
when the caller does not name one.

## Deploy

Docker image and compose stack (Postgres + pgvector) are in
[deploy/README.md](../deploy/README.md). The image installs with
`--extra server` and keeps `CMD ["flowjet-server"]` plus a `:8618/health`
healthcheck.

## Specification

- [RFC-001: OpenAI-Compatible API](specs/RFC-001-openai-compatible-api.md)
- [RFC-002: Isolated Thread-Pool Runtime](specs/RFC-002-isolated-thread-pool-runtime.md)
- [RFC-003: Production Isolation Hardening](specs/RFC-003-production-isolation-hardening.md)
