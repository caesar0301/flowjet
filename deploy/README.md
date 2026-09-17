# FlowJet Server Deployment

Self-contained stack: PostgreSQL + pgvector + flowjet-server. Mirrors [`../soothe/deploy`](../../soothe/deploy).

## Quick Start

```bash
cd deploy
cp env-example .env && vim .env   # Set DASHSCOPE_API_KEY
docker compose up -d
```

Verify: `docker compose ps` — should show `flowjet-pgvector` and `flowjet-server` running.

Health: `curl http://127.0.0.1:8618/health`

## Environment Variables

Required (`.env`):
- `DASHSCOPE_API_KEY` — DashScope (OpenAI-compatible) provider key
- `DASHSCOPE_BASE_URL` — DashScope API base URL

Optional:
- `FLOWJET_API_KEY` — Require Bearer auth on `/v1` endpoints (blank = no auth)
- `FLOWJET_PORT_PUBLISHED` — Host port to publish (default `8618`)
- `FLOWJET_BIND_IP` — Host bind IP (default `127.0.0.1`)
- `POSTGRES_USER` / `POSTGRES_PASSWORD` — PG credentials (default `postgres`/`postgres`)

## Architecture

```
flowjet-pgvector (PostgreSQL 17 + pgvector)
├── soothe_checkpoints   → LangGraph state (auto-provisioned on server start)
├── soothe_metadata      → Thread metadata
├── flowjet_vectors      → Embeddings (+ pgvector extension)
└── soothe_memory        → Long-term memory

flowjet-server (OpenAI Responses–compatible HTTP service)
└── Port 8618 (/v1/responses, /v1/models, /health)
```

Services bound to localhost only. PostgreSQL uses default credentials (postgres/postgres).

## Operations

| Action | Command |
|--------|---------|
| Status | `docker compose ps` |
| Logs | `docker compose logs flowjet-server` |
| Connect DB | `docker compose exec flowjet-pgvector psql -U postgres` |
| Backup | `docker compose exec flowjet-pgvector pg_dumpall -U postgres > backup.sql` |
| Stop | `docker compose down` |
| Clean restart | `docker compose down -v && docker compose up -d` |

## Config

`nano.yml` uses the DashScope (OpenAI-compatible) provider with `${ENV_VAR}` substitution and PostgreSQL + pgvector persistence. It is mounted over the image's baked default (`/var/lib/soothe/config/nano.yml`), which uses SQLite for local development — the mount switches the whole runtime to Postgres mode for the container stack.

## Security

- API keys in `.env` (not git)
- Localhost binding only
- Docker network isolation
