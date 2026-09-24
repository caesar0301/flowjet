"""DirectRuntimeBackend — run the agent in the caller's event loop.

Used by the one-shot CLI: no thread pool, no per-session workspace, no
admission control. It exposes the same ``RuntimeBackend`` contract as the
server's isolating backend, so the terminal and the HTTP/SSE projections are
renderers over one identical event stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from flowjet.core.bridges.nano.mapping import iter_nano_runtime_events
from flowjet.core.events import ModelInfo, RunRequest, RuntimeEvent


class DirectRuntimeBackend:
    """RuntimeBackend backed by soothe-nano, executed inline."""

    def __init__(
        self,
        agent: Any | None = None,
        *,
        models: list[str] | None = None,
        workspace: str | None = None,
    ) -> None:
        self._agent = agent
        self._models = models or ["default"]
        self._workspace = workspace

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=m) for m in self._models]

    async def delete_run(self, run_id: str) -> None:
        return None

    async def stream_run(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        run_id = request.run_id or f"resp_{uuid4().hex}"
        session = request.session or f"fj-{uuid4()}"
        workspace = self._workspace
        metadata = request.metadata or {}
        if workspace is None and isinstance(metadata.get("workspace"), str):
            workspace = metadata["workspace"]

        async for event in iter_nano_runtime_events(
            self._agent,
            run_id=run_id,
            model=request.model,
            session=session,
            input_text=request.input_text,
            workspace=workspace,
            thread_id=session,
            # The CLI resolves the interaction mode when it builds the agent
            # (auto / ask / bypass); nano must not be pinned back to "agent".
            interaction_mode=None,
            resume_value=request.resume_value,
        ):
            yield event
