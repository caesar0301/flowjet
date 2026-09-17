# Upgrading to FlowJet 2.0

2.0 merges the `flowjet-server` repository into this one. There is **one**
distribution (`flowjet`) that supplies both the CLI and the HTTP service, and
**one** import namespace (`flowjet.*`).

This is a **breaking release**. No compatibility shims were kept: `fj_ai` and
`flowjet_server` are gone as importable packages.

## Install

| Before | After |
|--------|-------|
| `pip install flowjet-agent` | `pip install flowjet` |
| `pip install flowjet-server` | `pip install 'flowjet[server]'` |

The CLI keeps a single runtime dependency (`soothe-nano`). FastAPI, uvicorn,
pydantic-settings and `agent-client-protocol` moved to the optional `server`
extra, so `pip install flowjet` (without the extra) still installs only the CLI.

## Console scripts

| Before | After | Notes |
|--------|-------|-------|
| `flowjet-agent` | `flowjet` | Formal CLI name |
| `fj` | `fj` | Unchanged |
| `fjf` | `fjf` | Unchanged |
| `flowjet-server` | `flowjet-server` | Unchanged — Docker/compose keep working |
| — | `fj serve` | New alias on the CLI dispatcher |

Server flags: `fj serve --host HOST --port PORT --api-key KEY --nano-config PATH`
(thin wrapper over the `FLOWJET_*` settings, then the same `main()` as
`flowjet-server`).

## Import paths

| Before | After |
|--------|-------|
| `fj_ai.cli` | `flowjet.cli.cli` |
| `fj_ai.agent` | `flowjet.cli.agent` |
| `fj_ai.stream` | `flowjet.cli.stream` |
| `fj_ai.threads` | `flowjet.cli.threads` |
| `fj_ai.progress` | `flowjet.cli.progress` |
| `fj_ai.completion.*` | `flowjet.cli.completion.*` |
| `flowjet_server.*` | `flowjet.server.*` |
| `flowjet_server.agent_runtime.*` | `flowjet.core.*` |
| `flowjet_server.agent_runtime.isolation.*` | `flowjet.core.backends.isolation.*` |
| `flowjet_server.bridges.nano.*` | `flowjet.core.bridges.nano.*` |
| `flowjet_server.agent_runtime.fake` | `flowjet.core.backends.fake` |

### Name changes inside the core

| Before | After |
|--------|-------|
| `apply_fj_defaults(config)` | `apply_profile(config, CLI_PROFILE)` (`fj`'s `apply_fj_defaults` still exists as a thin wrapper) |
| `create_nano_agent_instance(path)` | `create_agent(config, SERVER_PROFILE)` |
| `NanoRuntimeBackend` | unchanged (plus new `DirectRuntimeBackend` for the CLI) |

## Behaviour changes worth knowing

1. **Security is now an explicit surface policy.** The CLI forces
   `allow_paths_outside_workspace = True`; the server forces `False`. Both are
   declared in `flowjet.core.modes` (`CLI_PROFILE` / `SERVER_PROFILE`) instead of
   being an accident of which module built the agent. `bypass` mode is rejected
   on the server surface.
2. **The CLI runs on the shared `RuntimeBackend`.** `fj` now consumes the same
   `RuntimeEvent` stream as SSE. Terminal output is unchanged, but the streaming
   path is one code path shared with the server.
3. **`Narration` is a new event.** Text produced before a tool call is exposed as
   `Narration` (terminal live-preview only). Protocol clients keep receiving only
   committed `OutputTextDelta` text, exactly as before.
4. **Builtin skills moved to `flowjet/builtin_skills`** and are now registered for
   both surfaces (previously CLI-only).
5. **Version single-sourcing.** `flowjet.__version__` is the only version;
   the server no longer hard-codes `0.2.0`.

## Deployment

The image installs with `--extra server` and keeps `CMD ["flowjet-server"]` and
the `:8618/health` healthcheck. Bump the compose image tag to the 2.0 build.
Full guide: [deploy/README.md](../deploy/README.md).
