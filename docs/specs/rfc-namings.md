# flowjet-server Terminology Reference

Authoritative terminology reference for flowjet-server RFC specifications.

---

## Rules

1. All RFCs **MUST** use the terms defined here when referring to project concepts
2. New terms introduced in an RFC **MUST** be registered in this document
3. Deprecated terms **MUST** be removed when the defining RFC is deprecated
4. This document reflects the **current** state of terminology (not historical)

---

## Terms

| Term | Source RFC | Brief Description |
|------|-----------|-------------------|
| FlowJet | RFC-001 | Product brand for the FlowJet family (agent CLI + server) |
| flowjet-server | RFC-001 | OpenAI-compatible HTTP service project |
| FastAPI HTTP Shell | RFC-001 | Thin ASGI layer (`http/`): routes, auth, SSE transport, lifespan |
| OpenAI-Compatible Module | RFC-001 | Reusable `openai_compat` package: Responses schemas, projection, run store |
| Agent Runtime Protocol | RFC-001 | Internal runtime interface in `agent_runtime/` (not named Core Agent) |
| RuntimeBackend | RFC-001 | Backend interface that implements the Agent Runtime Protocol |
| Agent Runtime Event | RFC-001 | Typed runtime event (`RunStarted`, `Progress`, `OutputTextDelta`, …) |
| Nano Bridge | RFC-001 | `bridges/nano/` adapter from soothe-nano to Agent Runtime Protocol |
| soothe-nano | RFC-001 | Underlying agent runtime (`SootheNanoAgent`) used by Phase-1 bridge |
| Projection | RFC-001 | Mapping from Agent Runtime events to OpenAI / `response.flowjet.*` SSE |
| Projection Mode | RFC-001 | `report` \| `progress` \| `developer` visibility preset |
| flowjet namespace | RFC-001 | Request extension object `flowjet` and SSE types `response.flowjet.*` |
| Response | RFC-001 | OpenAI Responses API response object / resource |
| Run Store | RFC-001 | Persistence for Response resources (Phase 1: in-process) |
| Session | RFC-001 | Opaque conversation/thread id (`flowjet.session`); nano maps to `thread_id` |
| IsolatedRunRequest | RFC-002 | Request-carried session, thread_id, workspace, and input for pool workers |
| AgentAdapter | RFC-002 | Protocol wrapping the nano runner for isolated `astream` |
| ThreadPool (isolation) | RFC-002 | Persistent worker threads with private asyncio loops for agent runs |
| SessionAdmission | RFC-002 | One-in-flight-per-session gate before pool submit |
| WorkspaceResolver | RFC-002 | Maps session (+ optional metadata.workspace) to an absolute workspace path |
| IsolatingRuntimeBackend | RFC-002 | `RuntimeBackend` that admits, resolves workspace, and submits to ThreadPool |
| FLOWJET_HOME | RFC-002 | Root directory for per-session workspaces and local data |
| ResponseBridge | RFC-003 | Worker→asyncio delivery with control-frame guarantees and event backpressure |
| HealthWatchdog | RFC-003 | Periodic dead-worker detection and baseline capacity respawn |
| InteractionModeGate | RFC-003 | Pins `interaction_mode` per session (first turn wins) |
| PoolMetrics | RFC-003 | Read-only snapshot of pool utilization and recovery counters |

### Deprecated / Avoid

| Term | Replacement | Notes |
|------|-------------|-------|
| DeepAgents (as public API concept) | soothe-nano / Agent Runtime Protocol | Do not expose DeepAgents types on the HTTP surface |
| Core Agent Protocol | Agent Runtime Protocol | Renamed; do not use |

---

## Usage Guidelines

- **Capitalization**: Use the capitalization shown in the Term column when referring to defined terms
- **First use**: On first use in an RFC, link to this document or the defining RFC
- **Synonyms**: Avoid synonyms; use the canonical term from this table

---

## Related Documents

- [rfc-standard.md](templates/rfc-standard.md) - RFC process and conventions
- [rfc-index.md](rfc-index.md) - RFC index
- [rfc-history.md](rfc-history.md) - Change history
