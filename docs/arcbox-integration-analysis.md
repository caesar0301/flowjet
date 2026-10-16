# ArcBox Integration Analysis for FlowJet Soothe-in-VM

## Executive Summary

ArcBox (https://github.com/arcboxlabs/arcbox) is an open-source Rust microVM
runtime for macOS that boots isolated Linux sandboxes with <100ms boot time
via Hypervisor.framework. It ships a Python SDK (`pip install arcbox`) with a
clean async-first API over a local Unix socket daemon. FlowJet's existing
boxlite integration follows the same architectural pattern — a control-plane
server managing sandbox-per-workspace worker VMs — making arcbox a natural
drop-in alternative to boxlite with stronger macOS-native ergonomics.

---

## 1. ArcBox Architecture (as observed)

### Runtime layers
```
┌─────────────────────────────────────────────┐
│  Python SDK (arcbox)                        │
│  AsyncSandbox / ArcBox / Commands / Files   │
│  Connect-RPC over Unix socket               │
├─────────────────────────────────────────────┤
│  arcbox-daemon (Rust)                        │
│  Manages sandbox lifecycle, networking,     │
│  file sharing, port forwarding, DNS         │
├─────────────────────────────────────────────┤
│  arcbox-vmm (Rust VMM)                       │
│  darwin_hv/ (Hypervisor.framework)          │
│  linux.rs (KVM)                              │
│  VirtIO devices, vsock, virtiofs            │
└─────────────────────────────────────────────┘
```

### Key properties
- **Local-first**: daemon listens on `~/.arcbox/run/arcbox.sock` — no cloud, no API key
- **macOS-native**: uses `darwin_hv/` (Hypervisor.framework) on Apple Silicon, same as boxlite
- **<100ms boot**: own VMM with lightweight guest kernel
- **OCI-compatible**: sandboxes boot from Docker image references (`template` param)
- **E2B-compatible surface**: `arcbox.e2b` module mirrors the `e2b` SDK shape
- **Connect-RPC protocol**: protobuf over HTTP/2 (Unix socket transport via httpx)

### Python SDK API surface (the integration target)

```python
from arcbox import AsyncSandbox, AsyncArcBox

# Create a sandbox from an OCI image, wait until READY
sandbox = await AsyncSandbox.create(
    "registry.example.com/flowjet-worker:latest",
    vcpus=2,
    memory_mib=2048,
    ttl=3600,              # hard lifetime
    idle_timeout=300,       # idle reaping
    env={"FLOWJET_SERVER_BACKEND": "soothe"},
    network=True,           # or False for egress isolation
    wait_until_ready=True,
)

# Run commands inside the sandbox
result = await sandbox.commands.run(
    ["python", "-m", "flowjet.core.backends.isolation.boxlite_worker"],
    stdin=True,   # keep stdin open for interactive protocol
    background=True,
)

# Stream stdout
async for chunk in result.output:
    line = chunk.data.decode()
    # process JSON-lines protocol frames

# Write files into the sandbox
await sandbox.files.write_text("/workspace/hello.txt", "content")

# Kill sandbox
await sandbox.kill()
```

---

## 2. FlowJet's Existing BoxLite Integration (the integration target)

### Architecture
```
FlowJet Server (control plane)
├── build_backend() → IsolatingRuntimeBackend
│   └── pool = BoxLiteVMIsolationPool(adapter_factory, settings)
│
├── BoxLiteVMIsolationPool
│   ├── start() → imports boxlite SDK, builds Boxlite runtime
│   ├── _spawn_worker_unlocked(workspace)
│   │   ├── runtime.create(BoxOptions(image, vcpus, mem, volumes, env, network))
│   │   ├── box.exec("python", ["-m", "flowjet...boxlite_worker"])
│   │   ├── pump stdout → asyncio.Queue (JSON-lines frames)
│   │   └── await "ready" frame
│   ├── submit(request) → encode JSON, write to stdin, yield events
│   ├── cancel_run(run_id) → execution.kill(9)
│   └── shutdown() → stop all boxes
│
└── boxlite_worker.py (runs INSIDE the VM)
    ├── reads JSON-lines from stdin
    ├── builds SootheAgentAdapter (or NanoAgentAdapter)
    ├── adapter.astream(req) → yields RuntimeEvent
    └── writes JSON-lines events to stdout
```

### IsolationPool protocol (what arcbox must implement)
```python
class IsolationPool(Protocol):
    async def start(self) -> None: ...
    async def submit(self, request: IsolatedRunRequest) -> AsyncIterator[RuntimeEvent]: ...
    def cancel_run(self, run_id: str) -> bool: ...
    def cancel_session(self, session: str) -> bool: ...
    def metrics(self) -> PoolMetrics: ...
    async def shutdown(self, timeout: float = 5.0) -> None: ...
    async def await_session_dispatchable(self, session: str) -> None: ...
```

### Worker protocol (JSON-lines over stdin/stdout)
```
stdin  →  {"type": "request", "run_id": ..., "session": ..., "input_text": ..., ...}
stdout ←  {"type": "ready"}
stdout ←  {"type": "event", "event_type": "RunStarted", ...}
stdout ←  {"type": "done"}
```

---

## 3. Integration Approach: ArcBoxVMIsolationPool

### Why arcbox is a better fit than boxlite for macOS

| Dimension              | boxlite                          | arcbox                                    |
|------------------------|----------------------------------|-------------------------------------------|
| macOS hypervisor       | Hypervisor.framework (works)     | Hypervisor.framework (native, darwin_hv/) |
| Boot time              | ~500ms-1s                        | <100ms (own VMM, lightweight guest)        |
| SDK transport          | In-process Rust library (PyO3)    | Unix socket (Connect-RPC over httpx)       |
| Image source           | OCI registry pull (docker.io)    | OCI image or catalog template              |
| Network isolation      | NetworkSpec(allow_net=[...])      | `network=True/False` per sandbox           |
| File operations        | copy_in/copy_out (bulk)           | files.write_text/read (streamed, 256KB chunks) |
| Egress control         | allow_net list                   | network flag + port forwarding             |
| Daemon dependency      | None (embedded in server process) | Requires `abctl daemon start`              |
| Python SDK             | `pip install boxlite`            | `pip install arcbox` (or `uv add arcbox`)  |
| Error model            | Raw exceptions                   | Typed error hierarchy (SandboxNotFoundError, etc.) |

### Key architectural difference
- **boxlite**: The VMM runtime is *embedded in the server process* (PyO3). VM
  guest memory is mmap'd by the server, counting against the server's cgroup
  limit. The server IS the hypervisor.
- **arcbox**: The VMM runs in a *separate daemon process* (`arcbox-daemon`).
  The server communicates over a Unix socket. Memory isolation is stronger
  (daemon owns VM memory, not the server), but requires the daemon to be
  running.

### Proposed integration: new `ArcBoxVMIsolationPool`

The pool implements the same `IsolationPool` protocol as
`BoxLiteVMIsolationPool`, replacing the boxlite SDK calls with arcbox SDK
calls. The worker protocol (JSON-lines over stdin/stdout) stays identical,
so **no changes to `boxlite_worker.py` or `boxlite_protocol.py` are needed**.

```python
# src/flowjet/core/backends/isolation/arcbox_vm_pool.py

class ArcBoxVMSettings:
    image: str | None = None          # OCI image for worker container
    max_size: int = 4                  # max concurrent sandboxes
    vcpus: int = 2
    mem_mib: int = 2048
    workspace_container_path: str = "/workspace"
    allow_net: bool = False            # egress isolation
    pass_env: list[str] | None = None
    idle_timeout_seconds: float = 300.0
    max_requests_per_worker: int = 100
    ready_timeout_seconds: float = 60.0
    # ArcBox-specific:
    socket_path: str | None = None     # override ARCBOX_SOCKET
    ttl_seconds: float | None = None   # hard sandbox lifetime

class ArcBoxVMIsolationPool:
    """Implements IsolationPool using arcbox SDK sandboxes."""

    async def start(self):
        # Validate image is set
        # Create AsyncArcBox client (holds connection to daemon)
        # Start health watchdog

    async def _spawn_worker(self, workspace: str) -> _WorkerState:
        # 1. Create sandbox via arcbox SDK
        sandbox = await self._arcbox.create(
            self._settings.image,
            vcpus=self._settings.vcpus,
            memory_mib=self._settings.mem_mib,
            env=self._build_env(),
            network=self._settings.allow_net,
            idle_timeout=self._settings.idle_timeout_seconds,
            ttl=self._settings.ttl_seconds,
        )
        # 2. Start worker process inside sandbox
        execution = await sandbox.commands.run(
            ["python", "-m", "flowjet.core.backends.isolation.boxlite_worker"],
            background=True,
            stdin=True,
        )
        # 3. Pump stdout → asyncio.Queue (same as boxlite pool)
        # 4. Write workspace files into sandbox via sandbox.files
        # 5. Await "ready" frame

    async def submit(self, request) -> AsyncIterator[RuntimeEvent]:
        # Same JSON-lines protocol as BoxLiteVMIsolationPool
        # Write request frame to execution.stdin
        # Yield decoded events from stdout queue

    async def _terminate_worker(self, state):
        await state.sandbox.kill()

    def cancel_run(self, run_id: str) -> bool:
        # Signal the execution to kill the process group
        # or sandbox.commands.kill()

    async def shutdown(self, timeout: float = 5.0):
        # Kill all sandboxes, close AsyncArcBox client
```

### Workspace mounting difference

**boxlite**: Bind-mounts the host workspace directory directly into the VM at
`workspace_container_path` via `BoxOptions(volumes=[(host_path, guest_path, False)])`.

**arcbox**: Does not support bind mounts (volumes are listed as
`UnsupportedException` in the E2B-compatible surface). Instead, workspace
files are transferred via the SDK's `files` API:
```python
# Materialize workspace into sandbox
await sandbox.files.write_text("/workspace/file.py", content)
# Or for binary files:
await sandbox.files.write("/workspace/data.bin", binary_data)
```

This is a **key integration challenge**: the workspace must be copied into
the sandbox rather than mounted. For large workspaces this adds latency.
Mitigation options:
1. **Snapshot-based templates**: Pre-bake common workspace state into a
   template image, then overlay only changed files.
2. **Lazy file transfer**: Only transfer files when the agent accesses them
   (requires a FUSE-like layer, more complex).
3. **Accept the cost**: For typical agent workspaces (<100MB), the transfer
   is fast enough (<1s over Unix socket).

### Configuration additions

```python
# config.py additions
isolation_mode: Literal["thread_pool", "boxlite", "arcbox"] = "thread_pool"
arcbox_image: str | None = None
arcbox_max_pool: int = 4
arcbox_vcpus: int = 2
arcbox_mem_mib: int = 2048
arcbox_workspace_container_path: str = "/workspace"
arcbox_allow_net: bool = False
arcbox_pass_env: str | None = None
arcbox_socket_path: str | None = None  # ARCBOX_SOCKET override
arcbox_idle_timeout: float = 300.0
arcbox_max_requests: int = 100
arcbox_ttl: float | None = None  # hard sandbox lifetime
```

### Server backend selection

```python
# app.py — _build_isolation_pool()
def _build_isolation_pool(settings: Settings) -> Any:
    if settings.isolation_mode == "boxlite":
        return BoxLiteVMIsolationPool(...)
    if settings.isolation_mode == "arcbox":
        return ArcBoxVMIsolationPool(...)
    return None  # default ThreadPool
```

### validate_isolation_compatibility update

The existing guard requires `backend=soothe` for boxlite. The same applies
to arcbox — the worker builds a `SootheAgentAdapter` inside the sandbox:
```python
if self.isolation_mode in ("boxlite", "arcbox") and backend != "soothe":
    raise ValueError(...)
```

---

## 4. Integration Steps (Implementation Plan)

### Phase 1: ArcBoxVMIsolationPool implementation
1. Create `src/flowjet/core/backends/isolation/arcbox_vm_pool.py` mirroring
   `boxlite_vm_pool.py` but using `arcbox.AsyncArcBox` / `AsyncSandbox` instead
   of `boxlite.Boxlite` / `Box`.
2. Reuse `boxlite_protocol.py` (JSON-lines encode/decode) — the worker protocol
   is transport-agnostic.
3. Reuse `boxlite_worker.py` — the worker runs inside any container/VM that
   has Python + flowjet installed.
4. Implement workspace file transfer via `sandbox.files.write_text()` /
   `sandbox.files.write()` instead of bind-mount volumes.

### Phase 2: Configuration wiring
1. Add `arcbox_*` settings to `config.py` `Settings`.
2. Add `"arcbox"` to the `isolation_mode` Literal type.
3. Update `validate_isolation_compatibility` to cover arcbox.
4. Update `_build_isolation_pool()` in `app.py`.
5. Update `_build_isolation_pool()` to return `ArcBoxVMIsolationPool` when
   `isolation_mode == "arcbox"`.

### Phase 3: Worker image
1. The arcbox sandbox needs an OCI image containing:
   - Python 3.11+
   - flowjet (with `server` extra for soothe agent)
   - The `boxlite_worker` module (already in flowjet)
2. This is the same image used for boxlite — the worker code is identical.
3. The image must be available locally or in a registry arcbox can pull from.

### Phase 4: Testing
1. Unit tests: mock `AsyncArcBox` / `AsyncSandbox` to test pool lifecycle.
2. Integration test: start arcbox daemon, create pool, submit request,
   verify events.
3. E2E test: full agent run inside arcbox sandbox.

---

## 5. Key Differences and Challenges

### 5.1 No bind mounts → workspace file transfer
ArcBox sandboxes don't support bind-mounting host directories. The workspace
must be transferred via the SDK's `files` API. This is the biggest behavioral
difference from boxlite.

**Impact**: Adds file-transfer latency on sandbox creation. For a typical
workspace (<100MB), this is <1s over the Unix socket. For large monorepos,
consider pre-baking the workspace into the template image.

### 5.2 Daemon dependency
ArcBox requires `abctl daemon start` to be running. This is an external
dependency that boxlite doesn't have (boxlite embeds the runtime in-process).

**Mitigation**: The pool's `start()` can check daemon health via
`arcbox.capabilities()` and provide an actionable error if the daemon isn't
running.

### 5.3 Process management via Connect-RPC
BoxLite's `execution.stdin()` / `execution.stdout()` are direct pipe
handles from the embedded runtime. ArcBox's `commands.run(background=True)`
returns a `CommandHandle` with streaming output over Connect-RPC.

**Impact**: The stdout pump logic is structurally identical (async iterator
→ line queue), but the transport is HTTP/2 over Unix socket instead of
in-process pipes. Latency is slightly higher but negligible for a
JSON-lines protocol.

### 5.4 Cancellation
BoxLite: `execution.kill(9)` — direct signal to the process group.
ArcBox: `sandbox.commands` supports `signal(SIGKILL)` on background commands,
or `sandbox.kill()` to destroy the entire sandbox.

### 5.5 No cgroup memory concern
BoxLite's in-process VMM means VM guest memory counts against the server's
cgroup limit. ArcBox's daemon-isolated VMM means the server process is
lighter — only the Connect-RPC client overhead, not guest memory.

---

## 6. Minimal Working Configuration

```bash
# 1. Install arcbox daemon
brew install --cask arcboxlabs/tap/arcbox
abctl daemon start

# 2. Configure flowjet
export FLOWJET_SERVER_BACKEND=soothe
export FLOWJET_ISOLATION_MODE=arcbox
export FLOWJET_ARCBOX_IMAGE=registry.cn-hangzhou.aliyuncs.com/lacogito/soothed:latest
# Optional: restrict egress
export FLOWJET_ARCBOX_ALLOW_NET=false
# Optional: pass env vars into sandbox
export FLOWJET_ARCBOX_PASS_ENV=FLOWJET_NANO_CONFIG,OPENAI_API_KEY

# 3. Start server
flowjet-server
```

---

## 7. Conclusion

ArcBox is a strong candidate to replace or complement boxlite as FlowJet's
microVM isolation backend on macOS. The integration is structurally
identical to the existing boxlite integration (same IsolationPool protocol,
same JSON-lines worker protocol, same SootheAgentAdapter inside the VM),
with the main implementation work being:

1. **New pool class** (~300 lines, mirroring `boxlite_vm_pool.py` with
   arcbox SDK calls)
2. **Config additions** (~15 new settings fields)
3. **Workspace transfer** (files API instead of bind mounts — the one
   behavioral difference requiring design attention)

The arcbox Python SDK's async-first design, typed error hierarchy, and
local-first daemon model align well with FlowJet's architecture. The
<100ms boot time and daemon-isolated memory model are concrete advantages
over boxlite for macOS deployments.
