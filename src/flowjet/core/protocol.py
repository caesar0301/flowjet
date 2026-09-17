"""RuntimeBackend protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from flowjet.core.events import ModelInfo, RunRequest, RuntimeEvent


class RuntimeBackend(Protocol):
    async def list_models(self) -> list[ModelInfo]:
        """Return logical model / profile ids."""

    def stream_run(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        """Execute a run and yield Agent Runtime events."""

    async def delete_run(self, run_id: str) -> None:
        """Best-effort cancel/cleanup; Phase 1 may no-op."""
        ...
