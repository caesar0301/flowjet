"""Persistent thread pool for isolated agent runs (RFC-002 / RFC-003)."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import queue
import threading
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from flowjet.core.backends.isolation.adapter import AdapterFactory, AgentAdapter
from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import RunFailed, RuntimeEvent

logger = logging.getLogger(__name__)

_POISON = object()
_WORKER_READY_TIMEOUT_SECONDS = 30.0
_CONTROL_TYPES = frozenset({"done", "error", "cancelled", "ready"})


@dataclass(slots=True)
class PoolSettings:
    min_size: int = 2
    max_size: int = 8
    idle_timeout_seconds: float = 300.0
    max_requests_per_worker: int = 100
    reuse_runner: bool = True
    request_timeout_seconds: float = 0.0
    ready_timeout_seconds: float = 30.0
    event_enqueue_timeout_seconds: float = 5.0
    control_enqueue_timeout_seconds: float = 60.0
    response_queue_maxsize: int = 200


@dataclass(slots=True)
class PoolMetrics:
    total_workers: int
    idle_workers: int
    busy_workers: int
    dead_workers: int
    requests_completed: int
    ready_timeouts: int
    events_dropped: int


class WorkerStatus(StrEnum):
    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    CLEANING_UP = "cleaning_up"
    DEAD = "dead"


@dataclass
class WorkerState:
    worker_id: str
    thread: threading.Thread
    request_queue: queue.Queue[Any]
    cancel_event: threading.Event
    stop_event: threading.Event
    ready_event: threading.Event
    is_baseline: bool = True
    status: WorkerStatus = WorkerStatus.STARTING
    session: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    last_used: datetime = field(default_factory=datetime.now)
    requests_completed: int = 0

    def mark_busy(self, session: str, run_id: str, request_id: str) -> None:
        self.status = WorkerStatus.BUSY
        self.session = session
        self.run_id = run_id
        self.request_id = request_id
        self.last_used = datetime.now()

    def mark_idle(self) -> None:
        self.status = WorkerStatus.IDLE
        self.session = None
        self.run_id = None
        self.request_id = None
        self.last_used = datetime.now()
        self.cancel_event.clear()

    def mark_dead(self) -> None:
        self.status = WorkerStatus.DEAD
        self.session = None
        self.run_id = None
        self.request_id = None

    def is_alive(self) -> bool:
        return self.thread.is_alive()


class _ResponseBridge:
    """Push messages from a worker thread onto an asyncio.Queue on the main loop.

    Control frames (done/error/cancelled/ready) block until enqueued.
    Content events may be dropped after a short backpressure timeout.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        response_queue: asyncio.Queue[Any],
        *,
        event_timeout: float,
        control_timeout: float,
        drop_counter: list[int],
    ) -> None:
        self._loop = loop
        self._queue = response_queue
        self._event_timeout = event_timeout
        self._control_timeout = control_timeout
        self._drop_counter = drop_counter

    def emit(self, msg_type: str, payload: Any = None) -> None:
        if self._loop.is_closed():
            logger.warning("isolation ThreadPool: loop closed; cannot emit %s", msg_type)
            return

        async def _put() -> None:
            await self._queue.put((msg_type, payload))

        fut: Future[None] = asyncio.run_coroutine_threadsafe(_put(), self._loop)
        timeout = self._control_timeout if msg_type in _CONTROL_TYPES else self._event_timeout
        try:
            fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001
            if msg_type in _CONTROL_TYPES:
                logger.error(
                    "isolation ThreadPool: failed to deliver control frame %s",
                    msg_type,
                    exc_info=True,
                )
            else:
                self._drop_counter[0] += 1
                logger.warning("isolation ThreadPool: dropping event under backpressure")
                fut.cancel()


def _cancel_orphan_loop_tasks(loop: asyncio.AbstractEventLoop) -> None:
    current = asyncio.current_task(loop)
    for task in asyncio.all_tasks(loop):
        if task is current or task.done():
            continue
        task.cancel()


def _worker_main(
    worker_id: str,
    request_queue: queue.Queue[Any],
    cancel_event: threading.Event,
    stop_event: threading.Event,
    ready_event: threading.Event,
    adapter_factory: AdapterFactory,
    reuse_runner: bool,
    request_timeout_seconds: float,
    *,
    is_baseline: bool,
    idle_timeout_seconds: float,
    max_requests: int,
) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    adapter: AgentAdapter | None = None
    completed = 0

    def ensure_adapter() -> AgentAdapter:
        nonlocal adapter
        if adapter is None:
            adapter = adapter_factory()
        return adapter

    async def execute(req: IsolatedRunRequest, bridge: _ResponseBridge) -> None:
        nonlocal adapter
        cancel_event.clear()
        runner = ensure_adapter()
        try:
            timeout = request_timeout_seconds if request_timeout_seconds > 0 else None

            async def _stream() -> None:
                async for event in runner.astream(req):
                    if cancel_event.is_set():
                        bridge.emit("cancelled")
                        return
                    bridge.emit("event", event)
                bridge.emit("done")

            stream_task = asyncio.create_task(_stream(), context=contextvars.Context())

            async def _watch_cancel() -> None:
                while not stream_task.done():
                    if cancel_event.is_set():
                        stream_task.cancel()
                        return
                    await asyncio.sleep(0.02)

            watcher = asyncio.create_task(_watch_cancel())
            try:
                if timeout is not None:
                    async with asyncio.timeout(timeout):
                        await stream_task
                else:
                    await stream_task
            except asyncio.CancelledError:
                if cancel_event.is_set():
                    bridge.emit("cancelled")
                else:
                    raise
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        except TimeoutError:
            bridge.emit("error", RuntimeError(f"Request exceeded {timeout}s timeout"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Worker %s failed session=%s", worker_id, req.session)
            bridge.emit("error", exc)
        finally:
            _cancel_orphan_loop_tasks(loop)
            # Let cancellations settle before prepare/cleanup.
            await asyncio.sleep(0)
            try:
                if reuse_runner:
                    runner.prepare_for_request()
                else:
                    await runner.cleanup()
                    adapter = None
            except Exception:  # noqa: BLE001
                logger.debug("Worker %s prepare/cleanup failed", worker_id, exc_info=True)

    ready_event.set()
    try:
        while not stop_event.is_set():
            if max_requests > 0 and completed >= max_requests:
                logger.info(
                    "Worker %s reached max_requests=%d, exiting",
                    worker_id,
                    max_requests,
                )
                break
            try:
                if is_baseline or idle_timeout_seconds <= 0:
                    item = request_queue.get(timeout=1.0)
                else:
                    item = request_queue.get(timeout=idle_timeout_seconds)
            except queue.Empty:
                if stop_event.is_set():
                    break
                if is_baseline or idle_timeout_seconds <= 0:
                    continue
                logger.info(
                    "Worker %s idle timeout (%.0fs), exiting",
                    worker_id,
                    idle_timeout_seconds,
                )
                break

            if item is _POISON:
                break
            _kind, _request_id, req, bridge = item
            assert isinstance(req, IsolatedRunRequest)
            assert isinstance(bridge, _ResponseBridge)
            try:
                loop.run_until_complete(execute(req, bridge))
            except Exception as exc:  # noqa: BLE001
                bridge.emit("error", exc)
            finally:
                bridge.emit("ready")
                completed += 1
    finally:
        if adapter is not None:
            try:
                loop.run_until_complete(adapter.cleanup())
            except Exception:  # noqa: BLE001
                pass
        loop.close()


class ThreadPool:
    """Shared pool of worker threads for IsolatedRunRequest execution."""

    def __init__(
        self,
        adapter_factory: AdapterFactory,
        settings: PoolSettings | None = None,
    ) -> None:
        self._factory = adapter_factory
        self._settings = settings or PoolSettings()
        if self._settings.min_size < 1:
            raise ValueError("min_size must be >= 1")
        if self._settings.max_size < self._settings.min_size:
            raise ValueError("max_size must be >= min_size")

        self._workers: dict[str, WorkerState] = {}
        self._session_to_worker: dict[str, str] = {}
        self._run_to_worker: dict[str, str] = {}
        self._pending: dict[str, asyncio.Queue[Any]] = {}
        self._lock = asyncio.Lock()
        self._worker_available: asyncio.Condition | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._session_waiters: dict[str, asyncio.Condition] = {}
        self._health_task: asyncio.Task[None] | None = None
        self._next_index = 0
        self._requests_completed = 0
        self._ready_timeouts = 0
        self._events_dropped = [0]

    @property
    def settings(self) -> PoolSettings:
        return self._settings

    def metrics(self) -> PoolMetrics:
        idle = busy = dead = 0
        for w in self._workers.values():
            if not w.is_alive() or w.status == WorkerStatus.DEAD:
                dead += 1
            elif w.status == WorkerStatus.IDLE:
                idle += 1
            elif w.status in (WorkerStatus.BUSY, WorkerStatus.CLEANING_UP):
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
            events_dropped=self._events_dropped[0],
        )

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._main_loop = asyncio.get_running_loop()
            self._worker_available = asyncio.Condition()
            self._running = True
            for _ in range(self._settings.min_size):
                self._spawn_worker_unlocked(is_baseline=True)
            self._health_task = asyncio.create_task(
                self._health_watchdog(), name="flowjet-pool-health"
            )

    def _spawn_worker_unlocked(self, *, is_baseline: bool) -> WorkerState:
        worker_id = f"w_{self._next_index}_{uuid.uuid4().hex[:6]}"
        self._next_index += 1
        request_queue: queue.Queue[Any] = queue.Queue()
        cancel_event = threading.Event()
        stop_event = threading.Event()
        ready_event = threading.Event()
        thread = threading.Thread(
            target=_worker_main,
            name=f"flowjet-pool-{worker_id}",
            args=(
                worker_id,
                request_queue,
                cancel_event,
                stop_event,
                ready_event,
                self._factory,
                self._settings.reuse_runner,
                self._settings.request_timeout_seconds,
            ),
            kwargs={
                "is_baseline": is_baseline,
                "idle_timeout_seconds": self._settings.idle_timeout_seconds,
                "max_requests": self._settings.max_requests_per_worker,
            },
            daemon=True,
        )
        state = WorkerState(
            worker_id=worker_id,
            thread=thread,
            request_queue=request_queue,
            cancel_event=cancel_event,
            stop_event=stop_event,
            ready_event=ready_event,
            is_baseline=is_baseline,
        )
        self._workers[worker_id] = state
        thread.start()
        if not ready_event.wait(timeout=_WORKER_READY_TIMEOUT_SECONDS):
            state.mark_dead()
            raise RuntimeError(f"Worker {worker_id} failed to become ready")
        state.status = WorkerStatus.IDLE
        return state

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
            w.stop_event.set()
            w.request_queue.put(_POISON)
            w.cancel_event.set()
        for w in workers:
            w.thread.join(timeout=timeout)

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

    async def _acquire_idle_worker(self, session: str, run_id: str, request_id: str) -> WorkerState:
        assert self._worker_available is not None
        while True:
            async with self._lock:
                for w in list(self._workers.values()):
                    if w.status == WorkerStatus.IDLE and w.is_alive():
                        w.mark_busy(session, run_id, request_id)
                        self._session_to_worker[session] = w.worker_id
                        self._run_to_worker[run_id] = w.worker_id
                        return w
                alive = self._live_count()
                if alive < self._settings.max_size:
                    w = self._spawn_worker_unlocked(is_baseline=False)
                    w.mark_busy(session, run_id, request_id)
                    self._session_to_worker[session] = w.worker_id
                    self._run_to_worker[run_id] = w.worker_id
                    return w
            async with self._worker_available:
                await self._worker_available.wait()

    async def _await_ready(self, response_queue: asyncio.Queue[Any], *, already: bool) -> None:
        if already:
            return
        while True:
            msg_type, _payload = await response_queue.get()
            if msg_type == "ready":
                return

    async def _release_worker(
        self,
        worker: WorkerState,
        req: IsolatedRunRequest,
        request_id: str,
        *,
        mark_idle: bool,
    ) -> None:
        async with self._lock:
            self._pending.pop(request_id, None)
            self._run_to_worker.pop(req.run_id, None)
            self._session_to_worker.pop(req.session, None)
            if mark_idle and worker.worker_id in self._workers and worker.is_alive():
                worker.mark_idle()
                worker.requests_completed += 1
                self._requests_completed += 1
            elif worker.worker_id in self._workers:
                worker.mark_dead()
        cond = self._session_condition(req.session)
        async with cond:
            cond.notify_all()
        await self._notify_available()

    async def submit(self, request: IsolatedRunRequest) -> AsyncIterator[RuntimeEvent]:
        if not self._running:
            await self.start()
        assert self._main_loop is not None
        assert self._worker_available is not None

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

        response_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=self._settings.response_queue_maxsize
        )
        worker = await self._acquire_idle_worker(req.session, req.run_id, request_id)
        bridge = _ResponseBridge(
            self._main_loop,
            response_queue,
            event_timeout=self._settings.event_enqueue_timeout_seconds,
            control_timeout=self._settings.control_enqueue_timeout_seconds,
            drop_counter=self._events_dropped,
        )

        async with self._lock:
            self._pending[request_id] = response_queue

        worker.request_queue.put(("request", request_id, req, bridge))

        terminal_received = False
        worker_ready = False
        mark_idle = True
        try:
            while not terminal_received:
                msg_type, payload = await response_queue.get()
                if msg_type == "event":
                    yield payload  # type: ignore[misc]
                elif msg_type == "done":
                    terminal_received = True
                elif msg_type == "cancelled":
                    yield RunFailed(message="cancelled", code="cancelled")
                    terminal_received = True
                elif msg_type == "error":
                    exc = payload
                    message = str(exc) if exc else "worker error"
                    yield RunFailed(message=message, code="worker_error")
                    terminal_received = True
                elif msg_type == "ready":
                    worker_ready = True
        finally:
            worker.status = WorkerStatus.CLEANING_UP
            if not worker_ready:
                if not terminal_received:
                    worker.cancel_event.set()
                try:
                    await asyncio.wait_for(
                        self._await_ready(response_queue, already=False),
                        timeout=self._settings.ready_timeout_seconds,
                    )
                    worker_ready = True
                except TimeoutError:
                    self._ready_timeouts += 1
                    mark_idle = False
                    worker.cancel_event.set()
                    worker.stop_event.set()
                    logger.error(
                        "ThreadPool: ready timeout worker=%s run=%s; recycling",
                        worker.worker_id,
                        req.run_id,
                    )
            await self._release_worker(
                worker, req, request_id, mark_idle=mark_idle and worker_ready
            )

    async def _health_watchdog(self) -> None:
        while self._running:
            try:
                await self._reap_dead_workers()
            except Exception:  # noqa: BLE001
                logger.exception("ThreadPool health watchdog error")
            await asyncio.sleep(1.0)

    async def _reap_dead_workers(self) -> None:
        async with self._lock:
            dead: list[WorkerState] = []
            for w in list(self._workers.values()):
                if not w.is_alive() or w.status == WorkerStatus.DEAD:
                    dead.append(w)
            for w in dead:
                self._workers.pop(w.worker_id, None)
                if w.session:
                    self._session_to_worker.pop(w.session, None)
                if w.run_id:
                    self._run_to_worker.pop(w.run_id, None)
                if w.request_id:
                    pending = self._pending.pop(w.request_id, None)
                    if pending is not None:
                        with contextlib.suppress(asyncio.QueueFull):
                            pending.put_nowait(
                                (
                                    "error",
                                    RuntimeError(f"worker {w.worker_id} died"),
                                )
                            )
                        with contextlib.suppress(asyncio.QueueFull):
                            pending.put_nowait(("ready", None))
            live = self._live_count()
            while live < self._settings.min_size and self._running:
                self._spawn_worker_unlocked(is_baseline=True)
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
        worker.cancel_event.set()
        return True

    def cancel_session(self, session: str) -> bool:
        worker_id = self._session_to_worker.get(session)
        if worker_id is None:
            return False
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        worker.cancel_event.set()
        return True
