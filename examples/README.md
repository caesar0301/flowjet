# FlowJet Server examples

End-to-end demos of the **public API** against a running `flowjet-server`.

These replace the older numbered one-off scripts. Each script walks the full
surface you need as an app developer.

| Script | Client | Covers |
|--------|--------|--------|
| [`e2e_openai_sdk.py`](e2e_openai_sdk.py) | Official OpenAI Python SDK | `models.list`, create (string + list input), stream, retrieve, delete, FlowJet `report` / `progress` / `developer` via `extra_body`, error paths |
| [`e2e_ask_agent_modes.py`](e2e_ask_agent_modes.py) | Official OpenAI Python SDK | **Ask vs Agent** `interaction_mode`, progress streaming, session mode pin (400 on flip), switch via new session |
| [`e2e_http_api.py`](e2e_http_api.py) | Raw `httpx` | `GET /health`, all `/v1/*` endpoints, SSE wire format, `response.flowjet.*` events, OpenAI-style error JSON |
| [`e2e_acp_websocket.py`](e2e_acp_websocket.py) | Official ACP SDK + raw JSON-RPC | All ACP agent-side URIs via `ws://host/acp`: `initialize`, `session/new`, `session/load`, `session/list`, `session/prompt` (with tool-call events), `session/cancel`, `session/close`, `session/fork`, `session/resume`, `session/set_mode`, `session/set_config_option`, `authenticate` |

Shared helpers live in [`_client.py`](_client.py).

## Run against the server

Terminal 1 — sync deps and start soothe-nano:

```bash
make sync-dev
make run
```

Terminal 2 — run an end-to-end example:

```bash
make examples-sdk    # OpenAI SDK walkthrough
make examples-modes  # Ask vs Agent interaction_mode (real nano)
make examples-http   # Raw HTTP walkthrough
make examples-acp    # ACP WebSocket walkthrough (all ACP URIs)
# or all:
make examples-e2e
```

The server uses soothe-nano with thread-pool isolation. Nano loads
`$FLOWJET_HOME/config/nano.yml` (default `~/.flowjet/config/nano.yml`, or
`FLOWJET_NANO_CONFIG`) and its active router profile.

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `FLOWJET_BASE_URL` | `http://127.0.0.1:8618/v1` | OpenAI SDK `base_url` |
| `FLOWJET_API_KEY` / `OPENAI_API_KEY` | `local` | Bearer token (any value if server auth is off) |

If the server was started with `FLOWJET_API_KEY=secret`, use the same value for the client.

## API map

| Step | HTTP | SDK |
|------|------|-----|
| Health | `GET /health` | _(httpx only)_ |
| List models | `GET /v1/models` | `client.models.list()` |
| Create | `POST /v1/responses` | `client.responses.create(...)` |
| Stream | `POST /v1/responses` + SSE | `client.responses.stream(...)` / `stream=True` |
| Retrieve | `GET /v1/responses/{id}` | `client.responses.retrieve(id)` |
| Delete | `DELETE /v1/responses/{id}` | `client.responses.delete(id)` |
| FlowJet options | body `flowjet` | `extra_body={"flowjet": {...}}` |

`flowjet.interaction_mode` may be `"agent"` (default) or `"ask"` (hard read-only; soothe-nano ≥ 1.2.23). Mode is **pinned per session** — flip on the same session returns `400 interaction_mode_conflict`; use a new `flowjet.session` to switch. Walkthrough: [`e2e_ask_agent_modes.py`](e2e_ask_agent_modes.py). See also the root [README](../README.md#openai-protocol--flowjet-options).

## ACP (Agent Client Protocol)

flowjet-server exposes the ACP at the WebSocket endpoint ``ws://host:port/acp``.
Other ACP clients (e.g. Zed, backchat) connect via the official
``agent-client-protocol`` SDK and drive the same nano-backed runtime.

| ACP method | URI | Description |
|------------|-----|-------------|
| `initialize` | `initialize` | Handshake; returns protocol version + agent info |
| `session/new` | `session/new` | Create a new session |
| `session/load` | `session/load` | Load an existing session by id |
| `session/list` | `session/list` | List sessions for the cwd |
| `session/prompt` | `session/prompt` | Send a prompt; streams `session/update` notifications |
| `session/cancel` | `session/cancel` | Cancel an in-progress prompt (notification) |
| `session/close` | `session/close` | Close and clean up a session |
| `session/fork` | `session/fork` | Fork a session into a new session id |
| `session/resume` | `session/resume` | Resume a forked session |
| `session/set_mode` | `session/set_mode` | Set the session mode |
| `session/set_config_option` | `session/set_config_option` | Set a config option |
| `authenticate` | `authenticate` | Authenticate with the agent |

Walkthrough: [`e2e_acp_websocket.py`](e2e_acp_websocket.py).
