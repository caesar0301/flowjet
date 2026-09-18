"""NanoAgentAdapter — soothe-nano runner for the isolation ThreadPool."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.bridges.nano.mapping import (
    iter_nano_runtime_events,
    resolve_interaction_mode,
)
from flowjet.core.events import RunCompleted, RunFailed, RuntimeEvent
from flowjet.core.modes import SERVER_PROFILE


def create_nano_agent_instance(config_path: str | Path | None = None) -> Any:
    """Build a DualModeCoreAgent (AGENT + ASK graphs) from nano config.

    Server-owned invariants (workspace confinement, no ``bypass`` mode) live in
    :data:`flowjet.core.modes.SERVER_PROFILE`; they are applied by the shared
    bootstrap rather than re-implemented here.
    """
    from flowjet.core.backend import resolve_backend
    from flowjet.core.bootstrap import create_agent, default_config, load_config
    from flowjet.core.modes import Surface

    backend = resolve_backend(Surface.SERVER)
    try:
        config = load_config(config_path, backend=backend)
    except FileNotFoundError:
        config = default_config(backend)
    return create_agent(config, SERVER_PROFILE, backend=backend)


class NanoAgentAdapter:
    """AgentAdapter wrapping DualModeCoreAgent (one instance per pool worker)."""

    def __init__(
        self,
        agent: Any | None = None,
        *,
        config_path: str | Path | None = None,
        agent_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._agent = agent
        self._config_path = config_path
        self._agent_factory = agent_factory
        self._active = False
        self._tainted = False
        self._generation = 1 if agent is not None else 0

    def _ensure(self) -> Any:
        if self._agent is None:
            self._agent = (
                self._agent_factory()
                if self._agent_factory is not None
                else create_nano_agent_instance(self._config_path)
            )
            self._generation += 1
        return self._agent

    async def astream(self, req: IsolatedRunRequest) -> AsyncIterator[RuntimeEvent]:
        if self._active:
            raise RuntimeError("NanoAgentAdapter cannot execute concurrent requests")
        self._active = True
        completed = False
        agent = self._ensure()
        mode = resolve_interaction_mode(req.metadata)
        try:
            async for event in iter_nano_runtime_events(
                agent,
                run_id=req.run_id,
                model=req.model,
                session=req.session,
                input_text=req.input_text,
                workspace=str(req.workspace),
                thread_id=req.effective_thread_id(),
                interaction_mode=mode,
            ):
                if isinstance(event, RunFailed):
                    self._tainted = True
                elif isinstance(event, RunCompleted):
                    completed = True
                yield event
        finally:
            # Cancellation can interrupt middleware cleanup and leave mutable
            # graph state (for example edit-coalescing buffers) incomplete.
            # Never reuse that graph for another request.
            if not completed:
                self._tainted = True
            self._active = False

    def prepare_for_request(self) -> None:
        """Finalize one turn and recycle an agent after abnormal termination."""
        if self._active:
            raise RuntimeError("cannot prepare an active NanoAgentAdapter")
        if self._tainted:
            self._agent = None
            self._tainted = False

    async def cleanup(self) -> None:
        self._agent = None
        self._tainted = False
        self._active = False

    @property
    def generation(self) -> int:
        """Number of nano agent instances materialized by this adapter."""
        return self._generation


def nano_adapter_factory(*, config_path: str | Path | None = None):
    """Return a zero-arg factory suitable for ThreadPool / IsolatingRuntimeBackend."""

    def factory() -> NanoAgentAdapter:
        return NanoAgentAdapter(config_path=config_path)

    return factory
