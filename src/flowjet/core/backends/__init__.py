"""RuntimeBackend implementations.

``direct``    — runs the agent in the caller's event loop (one-shot CLI).
``isolation`` — persistent thread pool with per-session workspaces (server).
``fake``      — echo backend with no LLM (tests / DI).
"""

from __future__ import annotations

from flowjet.core.backends.fake import FakeRuntimeBackend

__all__ = ["FakeRuntimeBackend"]
