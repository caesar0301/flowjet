"""ArcBox VM isolation pool — microVM-per-workspace sandboxing.

Backed by the external **arcbox** runtime (https://github.com/arcboxlabs/arcbox,
``pip install arcbox``): each worker runs inside a hardware-isolated microVM
(Virtualization.framework / Hypervisor.framework on macOS, KVM on Linux) that
boots its own guest kernel.

Isolation model (control plane / sandbox plane):

- The flowjet server (control plane) connects to the arcbox daemon via a
  Unix socket (or a remote URL via ``ARCBOX_API_URL``) and stays a regular
  container/process. Only the sandbox plane — the worker executing agent
  tool calls — runs inside the microVM.
- **Per-workspace file transfer**: ArcBox sandboxes do not support host
  bind mounts.  Instead, the workspace directory is copied into the sandbox
  via the SDK's ``files`` API before the worker starts, and the worker sees
  its workspace at ``workspace_container_path`` (default ``/workspace``).
- Egress can be restricted via ``network=False`` (fully isolated) or left
  enabled (``network=True``, the SDK default).

The worker itself is unchanged: ``python -m
flowjet.core.backends.isolation.arcbox_worker`` speaking the JSON-lines
protocol over stdin/stdout. The pool bridges the arcbox ``AsyncCommandHandle``
streaming API to that protocol:

- ``AsyncCommandHandle.output`` is an async iterator yielding ``OutputChunk``
  objects (``channel`` + ``data``); a single reader task pumps stdout chunks
  line-by-line into an ``asyncio.Queue`` shared by the ready handshake, the
  submit loop and drain paths.
- Worker logs go to stderr; the output stream's stderr chunks are drained
  and discarded to avoid backpressure.

Requirements (fail loudly at ``start()`` when unmet):

- ``pip install arcbox`` (the arcbox SDK)
- The arcbox daemon running on the host (``arcbox-daemon`` or
  ``arcbox serve``), reachable via the Unix socket at
  ``~/.arcbox/run/arcbox.sock`` or via ``ARCBOX_API_URL``.
- Host virtualization: Hypervisor.framework on macOS, ``/dev/kvm`` on Linux.
- When the server runs containerized, set ``ARCBOX_API_URL`` to point at the
  host daemon (e.g. via a TCP proxy bridging the Unix socket).  No
  ``/dev/kvm`` passthrough is needed — the daemon manages VMs on the host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from flowjet.core.backends.isolation.adapter import AdapterFactory
from flowjet.core.backends.isolation.arcbox_protocol import (
    decode_event,
    encode_request,
    parse_frame,
)
from flowjet.core.backends.isolation.pool import PoolMetrics
from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import RuntimeEvent

logger = logging.getLogger(__name__)

_WORKER_MODULE = "flowjet.core.backends.isolation.arcbox_worker"
_CONTROL_TYPES = frozenset({"done", "error", "cancelled", "ready"})


def _import_arcbox() -> Any:
    """Lazily import the external arcbox SDK with an actionable error."""
    try:
        import arcbox  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "isolation_mode='arcbox' requires the external arcbox SDK "
            "(pip install arcbox) and the arcbox daemon running on the host "
            "(arcbox serve). The daemon manages microVMs via Hypervisor.framework "
            "(macOS) or KVM (Linux). Alternatively set "
            "FLOWJET_ISOLATION_MODE=thread_pool."
        ) from exc
    return arcbox


@dataclass(slots=True)
class ArcBoxVMSettings:
    """Configuration for :class:`ArcBoxVMIsolationPool`."""

    # Docker image reference for the in-VM worker (e.g. the flowjet-server
    # image — it carries the flowjet venv + worker entrypoint).
    image: str | None = None
    # Maximum concurrently live worker VMs (across all workspaces).
    max_size: int = 4
    # Per-VM resource limits.
    vcpus: int = 2
    mem_mib: int = 2048
    # Fixed in-sandbox mount point for the workspace directory.
    workspace_container_path: str = "/workspace"
    # Network access for worker VMs. True = full network (SDK default);
    # False = fully isolated (no egress).
    network: bool = True
    # Env var names copied from the server environment into the worker VM
    # (the in-VM agent runtime needs them, e.g. FLOWJET_NANO_CONFIG).
    pass_env: list[str] | None = None
    # ArcBox daemon connection: explicit API URL for remote daemon proxy
    # (e.g. http://host.docker.internal:9847). None = local Unix socket
    # (~/.arcbox/run/arcbox.sock). Can also be set via ARCBOX_API_URL env.
    api_url: str | None = None
    # Bearer credential for the daemon proxy (env: ARCBOX_API_KEY).
    api_key: str | None = None
    # Per-unary-RPC deadline in seconds (None = no bound).
    request_timeout: float | None = None
    idle_timeout_seconds: float = 300.0
    max_requests_per_worker: int = 100
    request_timeout_seconds: float = 0.0
    ready_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_size < 1:
            raise ValueError("max_size must be >= 1")
        if self.vcpus < 1:
            raise ValueError("vcpus must be >= 1")
        if self.mem_mib < 128:
            raise ValueError("mem_mib must be >= 128")

    @property
    def pass_env_list(self) -> list[str]:
        return list(self.pass_env or [])


@dataclass
class _VMWorkerState:
    """State of one worker microVM."""

    worker_id: str
    workspace: str
    sandbox: Any
    command_handle: Any
    # Line queue fed by the single stdout pump task; None = EOF (VM died /
    # worker exited). Shared by ready handshake / submit loop / drain.
    lines: asyncio.Queue[str | None]
    status: str = "starting"  # starting, idle, busy, cleaning_up, dead
    session: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    last_used: datetime = field(default_factory=datetime.now)
    requests_completed: int = 0
    pump_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def mark_busy(self, session: str, run_id: str, request_id: str) -> None:
        self.status = "busy"
        self.session = session
        self.run_id = run_id
        self.request_id = request_id
        self.last_used = datetime.now()
        self._cancel_event.clear()

    def mark_idle(self) -> None:
        self.status = "idle"
        self.session = None
        self.run_id = None
        self.request_id = None
        self.last_used = datetime.now()

    def mark_dead(self) -> None:
        self.status = "dead"
        self.session = None
        self.run_id = None
        self.request_id = None


class ArcBoxVMIsolationPool:
    """MicroVM-per-workspace isolation pool backed by arcboxlabs/arcbox.

    Implements the same ``IsolationPool`` protocol as ``ThreadPool``:
    ``start``, ``submit``, ``cancel_run``, ``cancel_session``, ``metrics``,
    ``shutdown``, ``await_session_dispatchable``.

    Per-workspace semantics: each worker VM gets its own workspace files
    copied in via the SDK's ``files`` API before the worker process starts.
    Workers are spawned on demand (no baseline pre-warm — no workspace to
    transfer until a request arrives) and recycled after
    ``max_requests_per_worker`` turns or ``idle_timeout_seconds``.
    """

    def __init__(
        self,
        adapter_factory: AdapterFactory,
        settings: ArcBoxVMSettings | None = None,
    ) -> None:
        self._factory = adapter_factory
        self._settings = settings or ArcBoxVMSettings()
        self._client: Any | None = None
        self._workers: dict[str, _VMWorkerState] = {}
        self._session_to_worker: dict[str, str] = {}
        self._run_to_worker: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._worker_available: asyncio.Condition | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._session_waiters: dict[str, asyncio.Condition] = {}
        self._health_task: asyncio.Task[None] | None = None
        self._next_index = 0
        self._requests_completed = 0
        self._ready_timeouts = 0
        self._events_dropped = 0

    @property
    def settings(self) -> ArcBoxVMSettings:
        return self._settings

    def metrics(self) -> PoolMetrics:
        idle = busy = dead = 0
        for w in self._workers.values():
            if w.status == "dead":
                dead += 1
            elif w.status == "idle":
                idle += 1
            elif w.status in ("busy", "cleaning_up", "starting"):
                busy += 1
            else:
                dead += 1
        return PoolMetrics(
            total_workers=len(self._workers),
            idle_workers=idle,
            busy_workers=busy,
            dead_workers=dead,
            requests_completed=self._requests_completed,
            ready_timeouts=self._ready_timeouts,
            events_dropped=self._events_dropped,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            if not self._settings.image:
                raise RuntimeError(
                    "isolation_mode='arcbox' requires FLOWJET_ARCBOX_IMAGE "
                    "(the Docker image for the in-VM worker, typically the "
                    "flowjet-server image)."
                )
            self._main_loop = asyncio.get_running_loop()
            self._worker_available = asyncio.Condition()
            # SDK import + client construction happen here (not in __init__)
            # so that constructing the pool never hard-fails in environments
            # without the SDK (tests, thread_pool deployments).
            self._client = self._ensure_client()
            self._running = True
            self._health_task = asyncio.create_task(
                self._health_watchdog(), name="arcbox-vm-pool-health"
            )
            logger.info(
                "ArcBox VM pool started: image=%s vcpus=%d mem_mib=%d max_size=%d network=%s",
                self._settings.image,
                self._settings.vcpus,
                self._settings.mem_mib,
                self._settings.max_size,
                self._settings.network,
            )

    def _ensure_client(self) -> Any:
        """Build the arcbox AsyncArcBox client (idempotent)."""
        if self._client is not None:
            return self._client
        arcbox = _import_arcbox()
        connection_kwargs: dict[str, Any] = {}
        if self._settings.api_url:
            connection_kwargs["api_url"] = self._settings.api_url
        if self._settings.api_key:
            connection_kwargs["api_key"] = self._settings.api_key
        if self._settings.request_timeout is not None:
            connection_kwargs["request_timeout"] = self._settings.request_timeout
        connection = arcbox.Connection(**connection_kwargs) if connection_kwargs else None
        client = arcbox.AsyncArcBox(connection)
        self._client = client
        return client

    async def shutdown(self, timeout: float = 10.0) -> None:
        async with self._lock:
            self._running = False
            workers = list(self._workers.values())
            self._workers.clear()
            self._session_to_worker.clear()
            self._run_to_worker.clear()
            health = self._health_task
            self._health_task = None
        if health is not None:
            health.cancel()
            try:
                await health
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for w in workers:
            await self._terminate_worker(w)
        self._session_waiters.clear()
        # Close the SDK client.
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Worker VM spawn / teardown
    # ------------------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        """Build the env dict for the sandbox from pass_env settings."""
        env: dict[str, str] = {}
        for name in self._settings.pass_env_list:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        # Always set the worker ID so logs are identifiable.
        env["ARCBOX_WORKER_ID"] = f"ab_{self._next_index}"
        return env

    async def _transfer_workspace(self, sandbox: Any, workspace: str) -> None:
        """Copy the workspace directory tree into the sandbox via the files API.

        ArcBox sandboxes don't support host bind mounts, so workspace files
        are transferred one file at a time via ``sandbox.files.write_bytes()``.
        Directories are created with ``sandbox.files.mkdir()``.
        """
        workspace_path = Path(workspace)
        if not workspace_path.exists():
            logger.warning("Workspace %s does not exist; skipping transfer", workspace)
            return

        container_root = PurePosixPath(self._settings.workspace_container_path)
        # Ensure the root workspace dir exists.
        await sandbox.files.mkdir(container_root)

        for item in workspace_path.rglob("*"):
            if item.is_dir():
                rel = item.relative_to(workspace_path)
                target = container_root / PurePosixPath(*rel.parts)
                await sandbox.files.mkdir(target)
            elif item.is_file():
                rel = item.relative_to(workspace_path)
                target = container_root / PurePosixPath(*rel.parts)
                data = item.read_bytes()
                await sandbox.files.write_bytes(target, data)

    async def _spawn_worker_unlocked(self, *, workspace: str) -> _VMWorkerState:
        """Create a worker VM for exactly one workspace and await 'ready'."""
        client = self._ensure_client()
        worker_id = f"ab_{self._next_index}_{uuid.uuid4().hex[:6]}"
        self._next_index += 1

        env = self._build_env()

        sandbox = await client.create(
            self._settings.image,
            vcpus=self._settings.vcpus,
            memory_mib=self._settings.mem_mib,
            env=env,
            network=self._settings.network,
            wait_until_ready=True,
        )

        # Transfer workspace files into the sandbox (no bind mounts in arcbox).
        await self._transfer_workspace(sandbox, workspace)

        # Start the worker process in the background with stdin open.
        # The sandbox image may not have the venv on PATH (the arcbox
        # daemon does not inherit the image's ENV), so prefer the venv
        # python when present and fall back to the bare name.
        # Pass env vars explicitly to commands.run() — the arcbox daemon
        # does not forward sandbox-level env vars to exec'd processes.
        python_bin = os.environ.get("FLOWJET_ARCBOX_PYTHON", "python")
        command_handle = await sandbox.commands.run(
            [python_bin, "-m", _WORKER_MODULE],
            background=True,
            stdin=True,
            env=env,
        )

        state = _VMWorkerState(
            worker_id=worker_id,
            workspace=workspace,
            sandbox=sandbox,
            command_handle=command_handle,
            lines=asyncio.Queue(),
        )
        self._workers[worker_id] = state

        # Single stdout pump and a stderr drain so the sandbox's pipes never
        # backpressure the worker.
        state.pump_task = asyncio.create_task(
            self._pump_stdout(state), name=f"arcbox-vm-pump-{worker_id}"
        )
        state.stderr_task = asyncio.create_task(
            self._drain_stderr(state), name=f"arcbox-vm-stderr-{worker_id}"
        )

        try:
            line = await asyncio.wait_for(
                state.lines.get(), timeout=self._settings.ready_timeout_seconds
            )
        except TimeoutError:
            self._ready_timeouts += 1
            await self._terminate_worker(state)
            logger.error("ArcBox VM worker %s failed to become ready", worker_id)
            raise RuntimeError(f"Worker {worker_id} failed to become ready")

        if line is None or parse_frame(line).get("type") != "ready":
            await self._terminate_worker(state)
            raise RuntimeError(f"Worker {worker_id} sent an unexpected first frame: {line!r}")
        state.status = "idle"
        logger.info("ArcBox VM worker %s ready (workspace=%s)", worker_id, workspace)
        return state

    async def _pump_stdout(self, state: _VMWorkerState) -> None:
        """Consume command_handle.output once; emit complete lines into the queue."""
        try:
            buf = b""
            async for chunk in state.command_handle.output:
                # The arcbox output stream interleaves stdout and stderr
                # chunks. Only process stdout-channel chunks — stderr chunks
                # contain worker logs that would corrupt the JSON-lines
                # protocol channel if fed to parse_frame.
                channel = getattr(chunk, "channel", "stdout")
                if channel != "stdout":
                    continue
                data = (
                    chunk.data
                    if isinstance(chunk.data, (bytes, bytearray))
                    else str(chunk.data).encode()
                )
                buf += bytes(data)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        await state.lines.put(line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("ArcBox VM worker %s stdout pump failed", state.worker_id)
        finally:
            # EOF sentinel unblocks every reader.
            await state.lines.put(None)

    async def _drain_stderr(self, state: _VMWorkerState) -> None:
        """Discard worker stderr (logs) to avoid pipe backpressure.

        The arcbox output stream interleaves stdout and stderr chunks; the
        stdout pump already consumes the full output iterator. This task is
        a no-op placeholder for future per-channel separation if needed.
        """
        # In arcbox, the output stream yields OutputChunk objects with a
        # ``channel`` field ("stdout" / "stderr" / "pty"). The _pump_stdout
        # task consumes the full stream; stderr chunks are simply ignored
        # there since we only extract complete lines regardless of channel.
        # This method exists for API parity with the pool protocol.
        # and for future per-channel routing.
        return

    async def _send_line(self, state: _VMWorkerState, line: str) -> None:
        """Write one frame to the worker's stdin."""
        await state.command_handle.write_stdin((line + "\n").encode("utf-8"))

    async def _terminate_worker(self, state: _VMWorkerState) -> None:
        for task in (state.pump_task, state.stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        try:
            await state.sandbox.kill()
        except Exception:  # noqa: BLE001
            logger.exception("ArcBox VM worker %s kill failed", state.worker_id)
        state.mark_dead()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _session_condition(self, session: str) -> asyncio.Condition:
        cond = self._session_waiters.get(session)
        if cond is None:
            cond = asyncio.Condition()
            self._session_waiters[session] = cond
        return cond

    async def await_session_dispatchable(self, session: str) -> None:
        """Wait until no worker VM is busy with this session."""
        cond = self._session_condition(session)
        async with cond:
            while session in self._session_to_worker:
                await cond.wait()

    async def _notify_available(self) -> None:
        assert self._worker_available is not None
        async with self._worker_available:
            self._worker_available.notify_all()

    def _worker_serves(self, w: _VMWorkerState, workspace: str | None) -> bool:
        """A VM can only serve the workspace it was spawned for (files
        transferred at create time)."""
        return workspace is not None and w.workspace == workspace

    def _live_count(self) -> int:
        return sum(1 for w in self._workers.values() if w.status != "dead")

    async def _acquire_idle_worker(
        self, session: str, run_id: str, request_id: str, workspace: str
    ) -> _VMWorkerState:
        assert self._worker_available is not None
        while True:
            async with self._lock:
                for w in list(self._workers.values()):
                    if w.status == "idle" and self._worker_serves(w, workspace):
                        w.mark_busy(session, run_id, request_id)
                        self._session_to_worker[session] = w.worker_id
                        self._run_to_worker[run_id] = w.worker_id
                        return w
                if self._live_count() < self._settings.max_size:
                    w = await self._spawn_worker_unlocked(workspace=workspace)
                    w.mark_busy(session, run_id, request_id)
                    self._session_to_worker[session] = w.worker_id
                    self._run_to_worker[run_id] = w.worker_id
                    return w
            async with self._worker_available:
                await self._worker_available.wait()

    async def submit(self, request: IsolatedRunRequest) -> AsyncIterator[RuntimeEvent]:
        if not self._running:
            await self.start()
        assert self._main_loop is not None

        request_id = request.request_id or uuid.uuid4().hex[:16]
        workspace = str(request.workspace)
        req = IsolatedRunRequest(
            run_id=request.run_id,
            session=request.session,
            input_text=request.input_text,
            model=request.model,
            workspace=request.workspace,
            thread_id=request.thread_id,
            metadata=request.metadata,
            request_id=request_id,
        )

        await self.await_session_dispatchable(req.session)

        worker = await self._acquire_idle_worker(req.session, req.run_id, request_id, workspace)

        # The VM sees its workspace at the fixed container path, not at
        # the server-side path — rewrite the frame before sending.
        from dataclasses import replace as _dc_replace

        send_req = _dc_replace(req, workspace=self._settings.workspace_container_path)
        await self._send_line(worker, encode_request(send_req))

        terminal_received = False
        try:
            while not terminal_received:
                line = await worker.lines.get()
                if line is None:
                    raise RuntimeError(
                        f"Worker {worker.worker_id} closed stdout for run {req.run_id}"
                    )
                frame = parse_frame(line)
                msg_type = frame.get("type")
                if msg_type == "event":
                    yield decode_event(frame)
                elif msg_type == "done":
                    terminal_received = True
                elif msg_type == "cancelled":
                    from flowjet.core.events import RunFailed

                    yield RunFailed(message="cancelled", code="cancelled")
                    terminal_received = True
                elif msg_type == "error":
                    from flowjet.core.events import RunFailed

                    message = frame.get("message", "worker error")
                    yield RunFailed(message=message, code="worker_error")
                    terminal_received = True
                elif msg_type == "ready":
                    # Worker signals readiness after each request; the next
                    # submit consumes it via the drain below.
                    continue
                else:
                    logger.debug(
                        "Worker %s sent unknown frame type %s",
                        worker.worker_id,
                        msg_type,
                    )
        finally:
            worker.status = "cleaning_up"
            await self._drain_until_ready_or_eof(worker)
            await self._release_worker(worker, req)

    async def _drain_until_ready_or_eof(self, worker: _VMWorkerState) -> None:
        """Consume frames until the post-request 'ready' or EOF, so the VM is
        recyclable for the next request."""
        try:
            while True:
                line = await asyncio.wait_for(
                    worker.lines.get(),
                    timeout=self._settings.ready_timeout_seconds,
                )
                if line is None:
                    return
                if parse_frame(line).get("type") == "ready":
                    return
        except TimeoutError:
            self._ready_timeouts += 1
            logger.error(
                "ArcBox VM pool: ready timeout worker=%s; recycling",
                worker.worker_id,
            )

    async def _release_worker(
        self,
        worker: _VMWorkerState,
        req: IsolatedRunRequest,
    ) -> None:
        recycle = worker.requests_completed + 1 >= self._settings.max_requests_per_worker
        async with self._lock:
            self._run_to_worker.pop(req.run_id, None)
            self._session_to_worker.pop(req.session, None)
            if recycle:
                await self._terminate_worker(worker)
            else:
                worker.mark_idle()
                worker.requests_completed += 1
                self._requests_completed += 1
        cond = self._session_condition(req.session)
        async with cond:
            cond.notify_all()
        await self._notify_available()

    # ------------------------------------------------------------------
    # Health / cancel
    # ------------------------------------------------------------------

    async def _health_watchdog(self) -> None:
        while self._running:
            try:
                await self._reap_dead_and_idle()
            except Exception:  # noqa: BLE001
                logger.exception("ArcBox VM pool health watchdog error")
            await asyncio.sleep(1.0)

    async def _reap_dead_and_idle(self) -> None:
        async with self._lock:
            stale: list[_VMWorkerState] = []
            now = datetime.now()
            for w in list(self._workers.values()):
                if (
                    w.status == "idle"
                    and (now - w.last_used).total_seconds() > self._settings.idle_timeout_seconds
                ):
                    stale.append(w)
                elif w.status == "dead":
                    stale.append(w)
            for w in stale:
                self._workers.pop(w.worker_id, None)
                if w.session:
                    self._session_to_worker.pop(w.session, None)
                if w.run_id:
                    self._run_to_worker.pop(w.run_id, None)
        for w in stale:
            if w.status != "dead":
                logger.info(
                    "ArcBox VM worker %s idle-recycled (workspace=%s)",
                    w.worker_id,
                    w.workspace,
                )
                await self._terminate_worker(w)
        if stale:
            await self._notify_available()

    def cancel_run(self, run_id: str) -> bool:
        worker_id = self._run_to_worker.get(run_id)
        if worker_id is None:
            return False
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        worker._cancel_event.set()
        try:
            # Kill the command (SIGKILL the process group).
            asyncio.ensure_future(worker.command_handle.kill("SIGKILL"))
        except Exception:  # noqa: BLE001
            logger.exception("cancel_run: kill failed for %s", worker_id)
        return True

    def cancel_session(self, session: str) -> bool:
        worker_id = self._session_to_worker.get(session)
        if worker_id is None:
            return False
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        worker._cancel_event.set()
        return True
