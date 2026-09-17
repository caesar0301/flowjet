"""Agent Runtime Protocol — typed events and RuntimeBackend.

Surface-agnostic: neither ``flowjet.cli`` nor ``flowjet.server`` is imported
here, and both import from here.
"""

from __future__ import annotations

from flowjet.core.backends.fake import FakeRuntimeBackend
from flowjet.core.events import (
    InterruptWaiting,
    ModelInfo,
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunFailed,
    RunRequest,
    RunStarted,
    RuntimeEvent,
    ToolCompleted,
    ToolStarted,
    UsageInfo,
)
from flowjet.core.modes import (
    CLI_PROFILE,
    SERVER_PROFILE,
    InteractionMode,
    Surface,
    SurfaceProfile,
)
from flowjet.core.protocol import RuntimeBackend

__all__ = [
    "CLI_PROFILE",
    "SERVER_PROFILE",
    "FakeRuntimeBackend",
    "InteractionMode",
    "InterruptWaiting",
    "ModelInfo",
    "OutputTextDelta",
    "Progress",
    "RunCompleted",
    "RunFailed",
    "RunRequest",
    "RunStarted",
    "RuntimeBackend",
    "RuntimeEvent",
    "Surface",
    "SurfaceProfile",
    "ToolCompleted",
    "ToolStarted",
    "UsageInfo",
]
