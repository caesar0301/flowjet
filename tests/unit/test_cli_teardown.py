"""Unit tests for the bounded event-loop teardown used by one-shot queries."""

from __future__ import annotations

import asyncio
import time

from flowjet.cli.cli import run_one_shot


async def _uncancellable_poller() -> None:
    """Swallow cancellation and re-arm, like a ``bubus`` event-bus run loop."""
    while True:
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            continue


def test_run_one_shot_returns_result() -> None:
    async def query() -> int:
        await asyncio.sleep(0)
        return 7

    assert run_one_shot(query()) == 7


def test_run_one_shot_gives_up_on_task_that_ignores_cancellation() -> None:
    async def query() -> int:
        asyncio.get_running_loop().create_task(_uncancellable_poller())
        await asyncio.sleep(0)
        return 0

    started = time.monotonic()
    assert run_one_shot(query(), grace=0.05) == 0
    assert time.monotonic() - started < 5.0


def test_run_one_shot_cancels_well_behaved_tasks() -> None:
    observed: list[str] = []

    async def cooperative() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            observed.append("cancelled")
            raise

    async def query() -> int:
        asyncio.get_running_loop().create_task(cooperative())
        await asyncio.sleep(0)
        return 0

    assert run_one_shot(query()) == 0
    assert observed == ["cancelled"]


def test_run_one_shot_closes_loop_after_failure() -> None:
    loops: list[asyncio.AbstractEventLoop] = []

    async def query() -> int:
        loops.append(asyncio.get_running_loop())
        raise RuntimeError("boom")

    try:
        run_one_shot(query())
    except RuntimeError:
        pass
    else:  # pragma: no cover - guards the assertion below
        raise AssertionError("expected RuntimeError to propagate")

    assert loops and loops[0].is_closed()
