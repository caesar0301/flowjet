# FlowJet

[![PyPI version](https://img.shields.io/pypi/v/flowjet.svg)](https://pypi.org/p/flowjet)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/flowjet.svg)](https://pypi.org/p/flowjet)
[![CI](https://github.com/caesar0301/flowjet-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/caesar0301/flowjet-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

🎥 [Watch the demo on Vimeo](https://vimeo.com/1211730182)

**FlowJet** is one agent runtime, two surfaces:

| Surface | Entry point | What it gives you |
|---------|-------------|-------------------|
| **Terminal CLI** | `fj` | A friendly one-shot coding agent — ask in plain English, get an answer |
| **HTTP service** | `flowjet-server` / `fj serve` | **ACP** over WebSocket + an **OpenAI-compatible API** (`/v1/responses`, `/v1/chat/completions`, `/v1/models`) |

Both are the same distribution and the same `flowjet.core` runtime: one nano
bootstrap, one stream mapper, one `RuntimeEvent` contract. The terminal renders
those events as a progress line; the server renders them as SSE or ACP updates.

```bash
# CLI
fj explain this repo
fj -f what did we decide last time     # continue the latest conversation
fjf what did we decide last time       # alias of fj -f

# Server (OpenAI-compatible + ACP)
flowjet-server                         # or: fj serve --port 8618
```

It runs on [soothe-nano](https://github.com/mirasoth/soothe-nano) — tools, skills, MCP, subagents, and progressive loading — with SQLite persistence so every conversation is resumable.

> Package: **flowjet** · CLI aliases: `fj`, `fjf` (=`-f`) · Server: `flowjet-server` · Runtime: [soothe-nano](https://github.com/mirasoth/soothe-nano) · Naming details: [docs/naming.md](docs/naming.md)

> **Upgrading from 1.x?** The distribution and all import paths were renamed. See [docs/upgrade.md](docs/upgrade.md).

---

## Install

```bash
pip install flowjet            # CLI only (one runtime dependency: soothe-nano)
pip install 'flowjet[server]'  # + the HTTP service (fastapi, uvicorn, ACP)
# or
uv tool install 'flowjet[server]'
```

Requires Python 3.11+. The server half is an optional extra — `fj` never imports
FastAPI or uvicorn.

## Configure

**Option A — Local model (guided setup):**

```bash
fj setup
```

This walks you through an OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, …) and writes the basics to `~/.soothe/config/nano.yml`.

**Option B — Cloud (no config file needed):**

```bash
export OPENAI_API_KEY=sk-...
fj summarize README.md
```

Without a `nano.yml`, FlowJet falls back to `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

## Doctor

Check whether your machine is ready to run `fj` (tool binaries, providers, observability):

```bash
fj doctor                 # quick check
fj doctor --deep          # thorough check
fj doctor --live-llm      # actually call the model
fj doctor --format json   # machine-readable
```

---

## Conversations

Threads persist in SQLite, so you can pick up where you left off — continue the latest, jump to a specific one, or list them:

```bash
fj -f and now add tests          # continue this project's latest active thread
fj -t abc123 continue from here  # continue a specific thread
fj -l                            # list recent threads
```

`-f` is scoped to the project you are in — the git repo root, or the current directory when it is not a repo — so parallel work in other checkouts never hijacks your thread. `fj -l` still lists every thread; pick one up from anywhere with `-t ID`.

## Flags

```text
fj [options] [--] <query...>
```

| Flag | Meaning |
|------|---------|
| `-f` / `--follow` | Continue the latest active thread in this project directory |
| `-t ID` / `--thread` | Continue (or pin) a specific thread (overrides `-f`) |
| `-l` / `--list` | List recent threads (newest first) |
| `-n NUM` | How many threads `-l` shows (`0` = all) |
| `-c PATH` / `--config` | Use an alternate `nano.yml` |
| `-w DIR` / `--workspace` | Workspace root |
| `--no-stream` | Wait for the full answer instead of streaming |
| `-v` / `--verbose` | Mirror tool calls on stderr |

> `-t` and `-f` can be combined; `-t` wins (explicit id overrides follow).

Shell completion is AI-assisted — it predicts natural-language intents, not just flags:

```bash
eval "$(fj completion zsh)"     # or: fj completion bash
```

---

## Extend

### Skills

FlowJet ships with AgentSkills (planning, TDD, debugging, document tools, MCP builder, and more). Add your own in `nano.yml`:

```yaml
skills:
  - ~/.soothe/skills/my-reviewer
  - ./skills/deploy
```

Each skill is a `SKILL.md` with frontmatter; progressive loading keeps the catalog compact and loads skills on demand.

### MCP servers

Connect any Model Context Protocol server:

```yaml
mcp_servers:
  - name: filesystem
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

With `defer: true` (the default), MCP tools activate on demand.

---

## Server

Start the service and point any OpenAI-compatible client at it:

```bash
flowjet-server                       # defaults: host :: (dual-stack), port 8618
fj serve --port 8618                 # same thing from the CLI
```

```python
from openai import OpenAI

client = OpenAI(api_key="local", base_url="http://127.0.0.1:8618/v1")
print(client.responses.create(model="default", input="Hello"))
```

| HTTP | Purpose |
|------|---------|
| `GET /health` | Liveness + thread-pool metrics |
| `GET /v1/models` | Logical model ids |
| `POST /v1/responses` | Create a response (`stream=true` → SSE) |
| `GET` / `DELETE /v1/responses/{id}` | Retrieve / cancel a response |
| `POST /v1/chat/completions` | Chat Completions (non-stream + SSE) |
| `WS /acp` | Agent Client Protocol (JSON-RPC) |

FlowJet options travel in `extra_body.flowjet`:

| Field | Values | Default | Meaning |
|-------|--------|---------|---------|
| `projection` | `report` \| `progress` \| `developer` | `report` | How much SSE detail to expose |
| `session` | string | new `fj-<uuid>` | Isolates workspace + conversation thread |
| `interaction_mode` | `agent` \| `ask` | `agent` | `ask` is hard read-only |
| `metadata` | object | — | Opaque bag forwarded to the runtime |

### Environment

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
| `FLOWJET_REQUEST_TIMEOUT` | `0` | Per-run timeout seconds (`0` = none; set in production) |
| `FLOWJET_READY_TIMEOUT` | `30` | Max wait for post-turn worker ready |
| `FLOWJET_ALLOW_EXTERNAL_WORKSPACE` | `false` | Allow `metadata.workspace` outside `FLOWJET_HOME` |
| `FLOWJET_CORS_ORIGINS` | `*` | Comma-separated browser origins allowed to call the API |

### Security model

The two surfaces have deliberately different invariants (enforced in
`flowjet.core.modes`, not by whichever module loaded first):

| | CLI | Server |
|---|---|---|
| `allow_paths_outside_workspace` | `True` (your machine) | **`False`** (per-request workspace, non-overridable) |
| `bypass` interaction mode | available | **rejected** — tools must never escape the request workspace |
| API key | — | optional `FLOWJET_API_KEY` (Bearer, constant-time compare) |

`FLOWJET_CORS_ORIGINS` defaults to `*` with credentials enabled so browser
clients work out of the box; set it to an explicit origin list in production.

Full protocol reference and deployment guide: [deploy/README.md](deploy/README.md),
[docs/specs/RFC-001-openai-compatible-api.md](docs/specs/RFC-001-openai-compatible-api.md).

---

## Development

```bash
git clone https://github.com/caesar0301/flowjet-agent.git
cd flowjet-agent
make sync-dev          # dev + server extras
make test              # unit + integration + server suites
make test-server       # HTTP/ACP suites only
make run               # start the server locally
make lint
```

CI runs format, lint, and tests on Python 3.11–3.13; releases go GitHub Release → PyPI.

## Powered by

Built on [soothe-nano](https://github.com/mirasoth/soothe-nano). For a full TUI coding agent from the same stack, see [mirasoth/soothe](https://github.com/mirasoth/soothe).

## License

MIT
