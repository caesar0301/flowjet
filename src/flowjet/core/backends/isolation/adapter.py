"""AgentAdapter protocol and a deterministic fake for pool tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Protocol

from flowjet.core.backends.isolation.request import IsolatedRunRequest
from flowjet.core.events import (
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunStarted,
    RuntimeEvent,
    UsageInfo,
)

AdapterFactory = Callable[[], "AgentAdapter"]


class AgentAdapter(Protocol):
    """Common runner surface for the nano AgentAdapter (and test fakes)."""

    def astream(self, req: IsolatedRunRequest) -> AsyncIterator[RuntimeEvent]:
        """Execute one turn and yield Agent Runtime events."""
        ...

    def prepare_for_request(self) -> None:
        """Finalize request state; recycle the runner when it is unsafe to reuse."""
        ...

    async def cleanup(self) -> None:
        """Release resources when the worker will not reuse this adapter."""
        ...


class FakeAgentAdapter:
    """Echo-style adapter for isolation unit tests (no LLM)."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.prepare_calls = 0
        self.cleanup_calls = 0
        self.last_workspace: str | None = None
        self.last_thread_id: str | None = None

    async def astream(self, req: IsolatedRunRequest) -> AsyncIterator[RuntimeEvent]:
        import asyncio

        self.last_workspace = str(req.workspace)
        self.last_thread_id = req.effective_thread_id()
        yield RunStarted(run_id=req.run_id, model=req.model, session=req.session)
        yield Progress(stage="Working", message="Thinking…")
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        # Write a marker file so concurrency tests can assert workspace isolation.
        marker = req.workspace / "last_input.txt"
        marker.write_text(req.input_text, encoding="utf-8")
        answer = f"Echo: {req.input_text}"
        yield OutputTextDelta(delta=answer)
        yield RunCompleted(
            output_text=answer,
            usage=UsageInfo(
                input_tokens=len(req.input_text.split()),
                output_tokens=len(answer.split()),
                total_tokens=len(req.input_text.split()) + len(answer.split()),
            ),
        )

    def prepare_for_request(self) -> None:
        self.prepare_calls += 1

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
