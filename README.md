# FlowJet

[![PyPI version](https://img.shields.io/pypi/v/flowjet.svg)](https://pypi.org/p/flowjet)
[![Python versions](https://img.shields.io/pypi/pyversions/flowjet.svg)](https://pypi.org/p/flowjet)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**FlowJet** is a coding agent you can run two ways:

- **`fj`** — ask a question in your terminal, get an answer. No UI, no context-switching.
- **`flowjet-server`** — the same agent as a service: **ACP** over WebSocket and an **OpenAI-compatible HTTP API**, so any OpenAI SDK can drive it.

It runs on [soothe-nano](https://github.com/mirasoth/soothe-nano) — tools, skills, MCP, subagents — with SQLite persistence, so every conversation is resumable.

---

## Install

```bash
pip install flowjet              # CLI
pip install 'flowjet[server]'    # CLI + HTTP service
```

Requires Python 3.11+. The server is an optional extra — installing just `flowjet` pulls a single runtime dependency.

## Configure

```bash
fj setup                         # guided: pick a local or hosted model
# or, with no config file at all:
export OPENAI_API_KEY=sk-...
fj summarize README.md
```

`fj setup` writes `~/.soothe/config/nano.yml`. Without it, FlowJet falls back to `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

Not sure your machine is ready? `fj doctor` (add `--deep`, `--live-llm`).

---

## CLI

```bash
fj explain this repo
fj -f and now add tests          # continue this project's latest conversation
fjf what did we decide?          # short alias of fj -f
fj -l                            # list recent conversations
```

| Flag | Meaning |
|------|---------|
| `-f` / `--follow` | Continue the latest thread in this project |
| `-t ID` / `--thread` | Continue (or pin) a specific thread — overrides `-f` |
| `-l` / `--list` | List recent threads (newest first) |
| `-n NUM` | How many threads `-l` shows (`0` = all) |
| `-a` / `--ask` | Read-only: answer without touching files |
| `-v` / `--verbose` | Mirror tool calls on stderr |
| `-c PATH` / `-w DIR` | Alternate `nano.yml` / workspace root |

`-f` is scoped to your current project, so work in other checkouts never hijacks your thread.

Enable shell completion (predicts full queries, not just flags):

```bash
eval "$(fj completion zsh)"      # or: fj completion bash
```

---

## Server

```bash
flowjet-server                   # host :: (dual-stack), port 8618
fj serve --port 8618             # same, from the CLI
```

```python
from openai import OpenAI

client = OpenAI(api_key="local", base_url="http://127.0.0.1:8618/v1")
print(client.responses.create(model="default", input="Hello"))
```

| Endpoint | Purpose |
|----------|---------|
| `POST /v1/responses` | Create a response (`stream=true` → SSE) |
| `GET` / `DELETE /v1/responses/{id}` | Retrieve / cancel |
| `POST /v1/chat/completions` | Chat Completions (non-stream + SSE) |
| `GET /v1/models` | Model ids |
| `GET /health` | Liveness |
| `WS /acp` | Agent Client Protocol |

Per-request options go in `extra_body.flowjet`:

| Field | Values | Meaning |
|-------|--------|---------|
| `session` | string | Isolates workspace + conversation thread |
| `interaction_mode` | `agent` (default) \| `ask` | `ask` is hard read-only |
| `projection` | `report` \| `progress` \| `developer` | How much SSE detail to expose |

Full configuration, environment variables and the security model: [docs/server.md](docs/server.md) · deployment: [deploy/README.md](deploy/README.md).

---

## Extend

Add skills and MCP servers in `nano.yml`:

```yaml
skills:
  - ~/.soothe/skills/my-reviewer

mcp_servers:
  - name: filesystem
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

Skills are `SKILL.md` files loaded on demand; MCP tools activate on demand by default (`defer: true`).

---

## Development

```bash
git clone https://github.com/caesar0301/flowjet.git
cd flowjet
make sync-dev && make test && make lint
```

## Upgrading from 1.x

Version 2.0 merges the former `flowjet-server` project into this one. The distribution is now `flowjet` and import paths changed. See [docs/upgrade.md](docs/upgrade.md).

## License

MIT
