"""BoxLite VM isolation pool — microVM-per-workspace sandboxing.

Backed by the external **boxlite** runtime (https://github.com/boxlite-ai/boxlite,
``pip install boxlite``): each worker runs inside a hardware-isolated microVM
(KVM on Linux / Hypervisor.framework on macOS) that boots its own guest kernel
and runs an OCI container inside it. This replaces the former in-repo
Docker/local-subprocess pool migrated from ``soothe_daemon``.

Isolation model (control plane / sandbox plane):

- The flowjet server (control plane) embeds the boxlite runtime library and
  stays a regular container/process. Only the sandbox plane — the worker
  executing agent tool calls — runs inside the microVM.
- **Per-workspace mounts**: a Box's volumes are fixed at ``runtime.create()``
  time, so each worker VM bind-mounts exactly ONE workspace (its own) at
  ``workspace_container_path``. Other workspaces are not visible inside the
  VM — not merely hidden by tool policy, but absent from the filesystem the
  guest kernel can see.
- Egress can be restricted to an allow-list via ``NetworkSpec(allow_net=...)``.

The worker itself is unchanged: ``python -m
flowjet.core.backends.isolation.boxlite_worker`` speaking the JSON-lines
protocol over stdin/stdout. The pool bridges the boxlite ``Execution``
streaming API to that protocol:

- ``Execution.stdout()`` may be iterated exactly ONCE, so a single reader task
  pumps it line-by-line into an ``asyncio.Queue`` shared by the ready
  handshake, the submit loop and drain paths.
- Worker logs already go to stderr (the worker forces this), so
  ``Execution.stderr()`` is drained and discarded to avoid backpressure.

Requirements (fail loudly at ``start()`` when unmet):

- ``pip install boxlite`` (flowjet extra: ``flowjet-server[boxlite]``)
- Host virtualization: ``/dev/kvm`` accessible on Linux, Hypervisor.framework
  on macOS. When the server itself runs containerized, ``/dev/kvm`` must be
  passed through to the container.
- The runtime is embedded in the server process, so VM guest memory is
  mmap'd by the server process and counts against the server container's
  cgroup memory limit — size it as
  ``concurrent VMs × (mem_mib + ~512 MiB overhead) + server baseline``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from flowjet.core.backends.isolation.adapter import AdapterFactory
from flowjet.core.backends.isolation.boxlite_protocol import (
    decode_event,
    encode_request,
    make_control,
    parse_frame,
)
from flowjet.core.backends.isolation.pool import PoolMetrics
from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import RuntimeEvent

logger = logging.getLogger(__name__)

_WORKER_MODULE = "flowjet.core.backends.isolation.boxlite_worker"
_CONTROL_TYPES = frozenset({"done", "error", "cancelled", "ready"})


def _import_boxlite() -> Any:
    """Lazily import the external boxlite SDK with an actionable error."""
    try:
        import boxlite  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "isolation_mode='boxlite' requires the external boxlite runtime "
            "(pip install 'flowjet[boxlite]' or pip install boxlite) and host "
            "virtualization (/dev/kvm on Linux; Hypervisor.framework on macOS). "
            "Alternatively set FLOWJET_ISOLATION_MODE=thread_pool."
        ) from exc
    return boxlite


@dataclass(slots=True)
class BoxLiteVMSettings:
    """Configuration for :class:`BoxLiteVMIsolationPool`."""

    # OCI image for the in-VM worker container. Usually the flowjet-server
    # image itself (it contains the flowjet venv + worker entrypoint).
    image: str | None = None
    # Maximum concurrently live worker VMs (across all workspaces).
    max_size: int = 4
    # Per-VM resource limits.
    vcpus: int = 2
    mem_mib: int = 2048
    # Fixed in-container mount point for the (single) workspace volume.
    workspace_container_path: str = "/workspace"
    # Egress allow-list. None = SDK default network (no filtering); a list
    # enables the sandbox egress policy (e.g. LLM API domains only).
    allow_net: list[str] | None = None
    # Env var names copied from the server environment into the worker VM
    # (the in-VM agent runtime needs them, e.g. FLOWJET_NANO_CONFIG).
    pass_env: list[str] | None = None
    # Private OCI registry credentials (env-only; never in config files).
    registry_host: str | None = None
    registry_username: str | None = None
    registry_password: str | None = None
    # Boxlite runtime data dir; None = SDK default (~/.boxlite).
    home_dir: str | None = None
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
    box: Any
    execution: Any
    stdin: Any
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


class BoxLiteVMIsolationPool:
    """MicroVM-per-workspace isolation pool backed by boxlite-ai/boxlite.

    Implements the same ``IsolationPool`` protocol as ``ThreadPool``:
    ``start``, ``submit``, ``cancel_run``, ``cancel_session``, ``metrics``,
    ``shutdown``, ``await_session_dispatchable``.

    Per-workspace semantics: a Box's volume set is fixed at creation, so a
    worker VM can only ever serve the single workspace it was spawned for.
    Workers are spawned on demand (no baseline pre-warm — no workspace to
    mount until a request arrives) and recycled after
    ``max_requests_per_worker`` turns or ``idle_timeout_seconds``.
    """

    def __init__(
        self,
        adapter_factory: AdapterFactory,
        settings: BoxLiteVMSettings | None = None,
    ) -> None:
        self._factory = adapter_factory
        self._settings = settings or BoxLiteVMSettings()
        self._runtime: Any | None = None
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
    def settings(self) -> BoxLiteVMSettings:
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
                    "isolation_mode='boxlite' requires FLOWJET_BOXLITE_IMAGE "
                    "(the OCI image for the in-VM worker, typically the "
                    "flowjet-server image)."
                )
            self._main_loop = asyncio.get_running_loop()
            self._worker_available = asyncio.Condition()
            # SDK import + runtime construction happen here (not in __init__)
            # so that constructing the pool never hard-fails in environments
            # without the SDK (tests, thread_pool deployments).
            self._runtime = self._ensure_runtime()
            self._running = True
            self._health_task = asyncio.create_task(
                self._health_watchdog(), name="boxlite-vm-pool-health"
            )
            logger.info(
                "BoxLite VM pool started: image=%s vcpus=%d mem_mib=%d max_size=%d allow_net=%s",
                self._settings.image,
                self._settings.vcpus,
                self._settings.mem_mib,
                self._settings.max_size,
                self._settings.allow_net,
            )

    def _ensure_runtime(self) -> Any:
        """Build the embedded boxlite runtime (idempotent)."""
        if self._runtime is not None:
            return self._runtime
        boxlite = _import_boxlite()
        registry_kwargs: dict[str, Any] = {}
        if self._settings.registry_host:
            registry_kwargs = {
                "host": self._settings.registry_host,
            }
            if self._settings.registry_username:
                registry_kwargs["username"] = self._settings.registry_username
            if self._settings.registry_password:
                registry_kwargs["password"] = self._settings.registry_password
        if registry_kwargs or self._settings.home_dir:
            options_kwargs: dict[str, Any] = {}
            if self._settings.home_dir:
                options_kwargs["home_dir"] = self._settings.home_dir
            if registry_kwargs:
                options_kwargs["image_registries"] = [boxlite.ImageRegistry(**registry_kwargs)]
            runtime = boxlite.Boxlite(boxlite.Options(**options_kwargs))
        else:
            runtime = boxlite.Boxlite.default()
        self._runtime = runtime
        return runtime

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

    # ------------------------------------------------------------------
    # Worker VM spawn / teardown
    # ------------------------------------------------------------------

    def _box_options(self, workspace: str) -> Any:
        """Build BoxOptions: single workspace volume + env + network policy."""
        boxlite = _import_boxlite()
        settings = self._settings
        assert settings.image is not None  # guaranteed by start()

        env_pairs: list[tuple[str, str]] = []
        for name in settings.pass_env_list:
            value = os.environ.get(name)
            if value is not None:
                env_pairs.append((name, value))

        network: Any = None
        if settings.allow_net is not None:
            network = boxlite.NetworkSpec(mode="enabled", allow_net=list(settings.allow_net))

        return boxlite.BoxOptions(
            image=settings.image,
            cpus=settings.vcpus,
            memory_mib=settings.mem_mib,
            working_dir="/",
            volumes=[(workspace, settings.workspace_container_path, False)],
            env=env_pairs,
            network=network,
        )

    async def _spawn_worker_unlocked(self, *, workspace: str) -> _VMWorkerState:
        """Create a worker VM for exactly one workspace and await 'ready'."""
        runtime = self._ensure_runtime()
        worker_id = f"bl_{self._next_index}_{uuid.uuid4().hex[:6]}"
        self._next_index += 1

        box = await runtime.create(self._box_options(workspace), name=f"flowjet-{worker_id}")
        execution = await box.exec("python", args=["-m", _WORKER_MODULE], tty=False)
        stdin = execution.stdin()

        state = _VMWorkerState(
            worker_id=worker_id,
            workspace=workspace,
            box=box,
            execution=execution,
            stdin=stdin,
            lines=asyncio.Queue(),
        )
        self._workers[worker_id] = state

        # Single stdout pump (Execution.stdout() is single-iteration) and a
        # stderr drain so the VM's pipes never backpressure the worker.
        state.pump_task = asyncio.create_task(
            self._pump_stdout(state), name=f"boxlite-vm-pump-{worker_id}"
        )
        state.stderr_task = asyncio.create_task(
            self._drain_stderr(state), name=f"boxlite-vm-stderr-{worker_id}"
        )

        try:
            line = await asyncio.wait_for(
                state.lines.get(), timeout=self._settings.ready_timeout_seconds
            )
        except TimeoutError:
            self._ready_timeouts += 1
            await self._terminate_worker(state)
            logger.error("BoxLite VM worker %s failed to become ready", worker_id)
            raise RuntimeError(f"Worker {worker_id} failed to become ready")

        if line is None or parse_frame(line).get("type") != "ready":
            await self._terminate_worker(state)
            raise RuntimeError(f"Worker {worker_id} sent an unexpected first frame: {line!r}")
        state.status = "idle"
        logger.info("BoxLite VM worker %s ready (workspace=%s)", worker_id, workspace)
        return state

    async def _pump_stdout(self, state: _VMWorkerState) -> None:
        """Consume Execution.stdout() once; emit complete lines into the queue."""
        try:
            stdout = state.execution.stdout()
            buf = b""
            async for chunk in stdout:
                data = chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
                buf += bytes(data)
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        await state.lines.put(line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("BoxLite VM worker %s stdout pump failed", state.worker_id)
        finally:
            # EOF sentinel unblocks every reader.
            await state.lines.put(None)

    async def _drain_stderr(self, state: _VMWorkerState) -> None:
        """Discard worker stderr (logs) to avoid pipe backpressure."""
        try:
            stderr = state.execution.stderr()
            if stderr is None:
                return
            async for _chunk in stderr:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Some SDK shapes may not expose stderr(); draining is best-effort.
            logger.debug("BoxLite VM worker %s stderr drain ended", state.worker_id)

    def _send_line(self, state: _VMWorkerState, line: str) -> Any:
        """Write one frame to the worker's stdin (await if the SDK is async)."""
        result = state.stdin.send_input((line + "\n").encode("utf-8"))
        return result

    async def _terminate_worker(self, state: _VMWorkerState) -> None:
        for task in (state.pump_task, state.stderr_task):
            if task is not None and not task.done():
                task.cancel()
        try:
            await state.box.stop()
        except Exception:  # noqa: BLE001
            logger.exception("BoxLite VM worker %s stop failed", state.worker_id)
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
        """A VM can only serve the workspace it was spawned for (volume fixed
        at create time)."""
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

        # The VM sees its workspace at the fixed container mount point, not at
        # the server-side path — rewrite the frame before sending.
        from dataclasses import replace as _dc_replace

        send_req = _dc_replace(req, workspace=self._settings.workspace_container_path)
        result = self._send_line(worker, encode_request(send_req))
        if inspect.isawaitable(result):
            await result

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
                "BoxLite VM pool: ready timeout worker=%s; recycling",
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
                logger.exception("BoxLite VM pool health watchdog error")
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
                    "BoxLite VM worker %s idle-recycled (workspace=%s)",
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
            result = worker.execution.kill(9)
            if inspect.isawaitable(result):
                # Fire-and-forget: the submit loop observes EOF / error frames.
                asyncio.ensure_future(result)
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
