"""BoxLite isolation pool — Docker-container-based session isolation (Phase 3).

This pool launches one Docker container per worker slot.  Each container runs
the ``boxlite_worker`` entrypoint, which reads JSON-lines requests on stdin
and emits JSON-lines events on stdout.  The pool bridges these to
:data:`RuntimeEvent` objects.

Unlike :class:`~flowjet.core.backends.isolation.pool.ThreadPool`, each worker
is a separate OS-level process in a Docker container — providing filesystem,
network, and process isolation between sessions.

**No soothe-daemon dependency.**  The pool communicates directly with the
container process via stdin/stdout JSON-lines.  The container image must
contain the flowjet package (or at least the worker script + event types).

Usage::

    pool = BoxLiteIsolationPool(
        adapter_factory=MyAdapterFactory(),
        settings=BoxLitePoolSettings(image="flowjet/worker:latest"),
    )
    await pool.start()
    async for event in pool.submit(request):
        ...
    await pool.shutdown()

When ``BoxLitePoolSettings.image`` is ``None`` or the Docker binary is not
found, the pool falls back to running the worker as a local subprocess
(useful for testing without Docker).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime

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

_CONTROL_TYPES = frozenset({"done", "error", "cancelled", "ready"})
_WORKER_SCRIPT = "flowjet/core/backends/isolation/boxlite_worker.py"


@dataclass(slots=True)
class BoxLitePoolSettings:
    """Configuration for :class:`BoxLiteIsolationPool`."""

    image: str | None = None
    min_size: int = 1
    max_size: int = 4
    cpu_count: int = 2
    mem_mib: int = 2048
    workspace_mount_mode: str = "bind"  # "bind" or "sync"
    idle_timeout_seconds: float = 300.0
    max_requests_per_worker: int = 100
    request_timeout_seconds: float = 0.0
    ready_timeout_seconds: float = 60.0
    # When True, run the worker as a local subprocess (no Docker).  Useful for
    # testing and for environments without Docker.
    local_fallback: bool = True
    # Path to the Docker binary.  When None, auto-detect.
    docker_binary: str | None = None

    def __post_init__(self) -> None:
        if self.min_size < 1:
            raise ValueError("min_size must be >= 1")
        if self.max_size < self.min_size:
            raise ValueError("max_size must be >= min_size")


@dataclass
class _BoxLiteWorkerState:
    """State of a single boxlite worker container."""

    worker_id: str
    process: asyncio.subprocess.Process | None = None
    status: str = "starting"  # starting, idle, busy, cleaning_up, dead
    session: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    last_used: datetime = field(default_factory=datetime.now)
    requests_completed: int = 0
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

    def mark_dead(self) -> None:
        self.status = "dead"
        self.session = None
        self.run_id = None
        self.request_id = None

    def is_alive(self) -> bool:
        if self.process is None:
            return False
        return self.process.returncode is None


class BoxLiteIsolationPool:
    """Docker-container-based isolation pool (Phase 3).

    Each worker slot is a long-running container process that accepts
    requests on stdin and emits events on stdout.  The pool manages
    container lifecycle, request dispatch, and event bridging.

    The pool implements the same interface as :class:`ThreadPool`:
    ``start``, ``submit``, ``cancel_run``, ``cancel_session``,
    ``metrics``, ``shutdown``, ``await_session_dispatchable``.
    """

    def __init__(
        self,
        adapter_factory: AdapterFactory,
        settings: BoxLitePoolSettings | None = None,
    ) -> None:
        self._factory = adapter_factory
        self._settings = settings or BoxLitePoolSettings()
        if self._settings.min_size < 1:
            raise ValueError("min_size must be >= 1")
        if self._settings.max_size < self._settings.min_size:
            raise ValueError("max_size must be >= min_size")

        self._workers: dict[str, _BoxLiteWorkerState] = {}
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
    def settings(self) -> BoxLitePoolSettings:
        return self._settings

    def metrics(self) -> PoolMetrics:
        idle = busy = dead = 0
        for w in self._workers.values():
            if not w.is_alive() or w.status == "dead":
                dead += 1
            elif w.status == "idle":
                idle += 1
            elif w.status in ("busy", "cleaning_up"):
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

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._main_loop = asyncio.get_running_loop()
            self._worker_available = asyncio.Condition()
            self._running = True
            for _ in range(self._settings.min_size):
                await self._spawn_worker_unlocked(is_baseline=True)
            self._health_task = asyncio.create_task(
                self._health_watchdog(), name="boxlite-pool-health"
            )

    async def _spawn_worker_unlocked(self, *, is_baseline: bool) -> _BoxLiteWorkerState:
        worker_id = f"bl_{self._next_index}_{uuid.uuid4().hex[:6]}"
        self._next_index += 1

        process = await self._launch_container(worker_id)
        state = _BoxLiteWorkerState(worker_id=worker_id, process=process)
        self._workers[worker_id] = state

        # Wait for the worker to signal readiness.
        try:
            await asyncio.wait_for(
                self._await_ready(state),
                timeout=self._settings.ready_timeout_seconds,
            )
            state.status = "idle"
        except TimeoutError:
            state.mark_dead()
            logger.error("BoxLite worker %s failed to become ready", worker_id)
            raise RuntimeError(f"Worker {worker_id} failed to become ready")

        return state

    async def _launch_container(self, worker_id: str) -> asyncio.subprocess.Process:
        """Launch a worker container (or local subprocess fallback)."""
        if self._settings.image and not self._settings.local_fallback:
            return await self._launch_docker_container(worker_id)
        # Local fallback: run the worker script directly as a subprocess.
        return await self._launch_local_subprocess(worker_id)

    async def _launch_docker_container(self, worker_id: str) -> asyncio.subprocess.Process:
        """Launch a Docker container running the boxlite worker."""
        binary = self._settings.docker_binary or "docker"
        args = [
            binary,
            "run",
            "--rm",
            "--name",
            f"flowjet-boxlite-{worker_id}",
            "--cpus",
            str(self._settings.cpu_count),
            "--memory",
            f"{self._settings.mem_mib}m",
            "-i",  # interactive (stdin)
            "--env",
            "BOXLITE_WORKER_ID=" + worker_id,
            self._settings.image,
            "python",
            "-m",
            "flowjet.core.backends.isolation.boxlite_worker",
        ]
        logger.info("Launching boxlite container %s (image=%s)", worker_id, self._settings.image)
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _launch_local_subprocess(self, worker_id: str) -> asyncio.subprocess.Process:
        """Launch the worker as a local subprocess (fallback / testing)."""
        import sys

        env = dict(os.environ, BOXLITE_WORKER_ID=worker_id)
        # When the host's adapter_factory is FakeAgentAdapter, signal the
        # worker to use it too (for testing without an LLM).
        if self._factory is not None:
            factory_name = getattr(self._factory, "__name__", "")
            if "Fake" in factory_name:
                env["BOXLITE_ADAPTER"] = "fake"

        logger.info("Launching boxlite local subprocess %s", worker_id)
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "flowjet.core.backends.isolation.boxlite_worker",
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _await_ready(self, state: _BoxLiteWorkerState) -> None:
        """Read frames until we get a 'ready' from the worker."""
        assert state.process is not None
        while True:
            line = await state.process.stdout.readline()
            if not line:
                raise RuntimeError(f"Worker {state.worker_id} closed stdout before ready")
            frame = parse_frame(line.decode("utf-8").strip())
            if frame.get("type") == "ready":
                return
            logger.debug("Worker %s pre-ready frame: %s", state.worker_id, frame)

    def _session_condition(self, session: str) -> asyncio.Condition:
        cond = self._session_waiters.get(session)
        if cond is None:
            cond = asyncio.Condition()
            self._session_waiters[session] = cond
        return cond

    async def await_session_dispatchable(self, session: str) -> None:
        """Wait until no worker is busy with this session."""
        cond = self._session_condition(session)
        async with cond:
            while session in self._session_to_worker:
                await cond.wait()

    async def _notify_available(self) -> None:
        assert self._worker_available is not None
        async with self._worker_available:
            self._worker_available.notify_all()

    def _live_count(self) -> int:
        return sum(1 for w in self._workers.values() if w.is_alive())

    async def _acquire_idle_worker(
        self, session: str, run_id: str, request_id: str
    ) -> _BoxLiteWorkerState:
        assert self._worker_available is not None
        while True:
            async with self._lock:
                for w in list(self._workers.values()):
                    if w.status == "idle" and w.is_alive():
                        w.mark_busy(session, run_id, request_id)
                        self._session_to_worker[session] = w.worker_id
                        self._run_to_worker[run_id] = w.worker_id
                        return w
                alive = self._live_count()
                if alive < self._settings.max_size:
                    w = await self._spawn_worker_unlocked(is_baseline=False)
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

        worker = await self._acquire_idle_worker(req.session, req.run_id, request_id)
        assert worker.process is not None
        assert worker.process.stdin is not None

        # Send the request as a JSON-lines frame.
        request_line = encode_request(req) + "\n"
        worker.process.stdin.write(request_line.encode("utf-8"))
        await worker.process.stdin.drain()

        terminal_received = False
        worker_ready = False
        mark_idle = True
        try:
            while not terminal_received:
                line = await worker.process.stdout.readline()
                if not line:
                    # Worker closed stdout unexpectedly.
                    raise RuntimeError(
                        f"Worker {worker.worker_id} closed stdout for run {req.run_id}"
                    )
                frame = parse_frame(line.decode("utf-8").strip())
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
                    worker_ready = True
        finally:
            worker.status = "cleaning_up"
            if not worker_ready:
                if not terminal_received:
                    worker._cancel_event.set()
                # Drain remaining frames (including the 'ready' that the
                # worker sends after 'done') so we can recycle the worker.
                try:
                    await asyncio.wait_for(
                        self._drain_until_ready(worker),
                        timeout=self._settings.ready_timeout_seconds,
                    )
                    worker_ready = True
                except TimeoutError:
                    self._ready_timeouts += 1
                    mark_idle = False
                    logger.error(
                        "BoxLite pool: ready timeout worker=%s run=%s; recycling",
                        worker.worker_id,
                        req.run_id,
                    )
            await self._release_worker(
                worker, req, request_id, mark_idle=mark_idle and worker_ready
            )

    async def _drain_until_ready(self, worker: _BoxLiteWorkerState) -> None:
        """Drain remaining frames until 'ready' or stdout closes."""
        assert worker.process is not None
        while True:
            line = await worker.process.stdout.readline()
            if not line:
                return
            frame = parse_frame(line.decode("utf-8").strip())
            if frame.get("type") == "ready":
                return

    async def _release_worker(
        self,
        worker: _BoxLiteWorkerState,
        req: IsolatedRunRequest,
        request_id: str,
        *,
        mark_idle: bool,
    ) -> None:
        async with self._lock:
            self._run_to_worker.pop(req.run_id, None)
            self._session_to_worker.pop(req.session, None)
            if mark_idle and worker.is_alive():
                worker.mark_idle()
                worker.requests_completed += 1
                self._requests_completed += 1
            else:
                worker.mark_dead()
        cond = self._session_condition(req.session)
        async with cond:
            cond.notify_all()
        await self._notify_available()

    async def _health_watchdog(self) -> None:
        while self._running:
            try:
                await self._reap_dead_workers()
            except Exception:  # noqa: BLE001
                logger.exception("BoxLite health watchdog error")
            await asyncio.sleep(1.0)

    async def _reap_dead_workers(self) -> None:
        async with self._lock:
            dead: list[_BoxLiteWorkerState] = []
            for w in list(self._workers.values()):
                if not w.is_alive() or w.status == "dead":
                    dead.append(w)
            for w in dead:
                self._workers.pop(w.worker_id, None)
                if w.session:
                    self._session_to_worker.pop(w.session, None)
                if w.run_id:
                    self._run_to_worker.pop(w.run_id, None)
            live = self._live_count()
            while live < self._settings.min_size and self._running:
                await self._spawn_worker_unlocked(is_baseline=True)
                live = self._live_count()
        if dead:
            await self._notify_available()

    def cancel_run(self, run_id: str) -> bool:
        worker_id = self._run_to_worker.get(run_id)
        if worker_id is None:
            return False
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        worker._cancel_event.set()
        # Try to send a cancel signal to the container.
        if worker.process and worker.process.stdin:
            try:
                cancel_line = make_control("cancel") + "\n"
                worker.process.stdin.write(cancel_line.encode("utf-8"))
            except Exception:  # noqa: BLE001
                pass
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

    async def shutdown(self, timeout: float = 5.0) -> None:
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
            with contextlib.suppress(asyncio.CancelledError):
                await health
        for w in workers:
            await self._terminate_worker(w, timeout)
        self._session_waiters.clear()

    async def _terminate_worker(self, worker: _BoxLiteWorkerState, timeout: float) -> None:
        if worker.process is None:
            return
        if worker.process.stdin:
            try:
                eof_line = make_control("shutdown") + "\n"
                worker.process.stdin.write(eof_line.encode("utf-8"))
                await worker.process.stdin.drain()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(worker.process.wait(), timeout=timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                worker.process.kill()
                await worker.process.wait()
