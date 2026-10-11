"""SootheRuntimeBackend factory — IsolatingRuntimeBackend with SootheAgentAdapter.

Mirrors :func:`flowjet.core.bridges.nano.backend.build_isolating_nano_backend`:
same ``IsolatingRuntimeBackend`` + ``ThreadPool``, just with a
``SootheAgentAdapter`` factory instead of ``NanoAgentAdapter``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowjet.core.bridges.soothe.adapter import SootheAgentAdapter


def build_isolating_soothe_backend(
    *,
    models: list[str] | None = None,
    config_path: str | Path | None = None,
    home: Path | str | None = None,
    pool_settings: Any = None,
    allow_external_workspace: bool = False,
    workspace_resolver: Any = None,
    pool: Any | None = None,
) -> Any:
    """Construct IsolatingRuntimeBackend with SootheAgentAdapter factory.

    When ``pool`` is provided (Phase 3), it is used directly instead of
    constructing a new :class:`ThreadPool`.  This allows callers to inject
    a :class:`BoxLiteIsolationPool` for container-based isolation.
    """
    from flowjet.core.backends.isolation import IsolatingRuntimeBackend, PoolSettings

    settings = pool_settings if pool_settings is not None else PoolSettings()

    def factory() -> SootheAgentAdapter:
        return SootheAgentAdapter(config_path=config_path)

    return IsolatingRuntimeBackend(
        models=models,
        adapter_factory=factory,
        pool_settings=settings,
        home=home,
        allow_external_workspace=allow_external_workspace,
        workspace_resolver=workspace_resolver,
        pool=pool,
    )
