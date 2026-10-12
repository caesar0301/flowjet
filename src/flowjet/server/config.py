"""Server settings from environment."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from flowjet.core.backends.isolation.pool import PoolSettings


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLOWJET_", extra="ignore")

    api_key: str | None = None
    models: str = "default"
    # Bind to ``::`` (IPv6 wildcard) instead of ``0.0.0.0`` so the server
    # accepts connections on both IPv4 and IPv6.  On macOS, ``localhost``
    # resolves to ``::1`` (IPv6) first; a server bound to ``0.0.0.0`` only
    # listens on IPv4, so browser fetch() calls to
    # ``http://localhost:<port>`` fail with "Failed to fetch" before CORS
    # even applies.  Binding to ``::`` with the default dual-stack socket
    # accepts both address families.
    host: str = "::"
    port: int = 8618

    # CORS — origins allowed to call the API from a browser.  Use a
    # comma-separated list in FLOWJET_CORS_ORIGINS, or "*" for all.
    # Default is permissive so browser-based OpenAI-compatible clients
    # (e.g. Airi) work out of the box.
    cors_origins: list[str] = Field(default=["*"])

    home: str = Field(default="~/.flowjet")
    thread_pool_min: int = 2
    thread_pool_max: int = 8
    thread_pool_idle_timeout: float = 300.0
    max_requests_per_worker: int = 100
    reuse_runner: bool = True
    request_timeout: float = 0.0
    ready_timeout: float = 30.0
    # Workspace resolution is client-driven by default: an ACP client (e.g. a
    # desktop app) sends the project directory it wants the session to run in,
    # and `metadata.workspace` is honoured even when it sits outside
    # FLOWJET_HOME. Set FLOWJET_ALLOW_EXTERNAL_WORKSPACE=false to force every
    # session into its own hashed workspace under FLOWJET_HOME instead.
    #
    # This is not a sandbox escape: tools stay confined to whichever workspace
    # is selected (SERVER_PROFILE.allow_paths_outside_workspace is False), so
    # the workspace boundary moves with the caller's choice rather than
    # disappearing.
    allow_external_workspace: bool = True
    nano_config: str | None = None

    # Workspace sync (Phase 2) — when set, sessions materialize from / persist
    # to this fsspec-backed object store. Empty = local-only (Phase 1 behavior).
    # Example: "s3://bucket/flowjet", "gs://bucket/flowjet", "az://container/pfx"
    workspace_sync_source: str | None = None
    workspace_sync_config: str | None = None
    workspace_cas_root: str = "~/.flowjet/data/cas"

    # Isolation mode (Phase 3) — "thread_pool" (default, in-process) or
    # "boxlite" (Docker-container-based session isolation).
    # When set to "boxlite", each worker slot runs in its own container,
    # providing filesystem and process isolation between sessions.
    # Requires Docker (or use local_fallback for testing).
    isolation_mode: Literal["thread_pool", "boxlite"] = "thread_pool"
    boxlite_image: str | None = None
    boxlite_min_pool: int = 1
    boxlite_max_pool: int = 4
    boxlite_cpu_count: int = 2
    boxlite_mem_mib: int = 2048
    boxlite_workspace_mount_mode: Literal["bind", "sync"] = "bind"
    boxlite_local_fallback: bool = True

    def validate_isolation_compatibility(self, backend: str) -> None:
        """Raise ``ValueError`` if isolation settings are incompatible with the backend.

        Boxlite isolation (Phase 3) requires the ``soothe`` host runtime.
        The boxlite worker builds a :class:`SootheAgentAdapter` internally,
        which depends on soothe's host middleware (goal/step guard, decompose,
        ask-user).  When ``FLOWJET_SERVER_BACKEND=nano`` is set, the worker
        would fall back to :class:`NanoAgentAdapter` — which lacks the host
        layer the isolation pool was designed for — producing a confusing
        silent degradation rather than the expected isolated-soothe behaviour.

        This guard makes the misconfiguration a loud, early failure at server
        startup rather than a runtime surprise.
        """
        if self.isolation_mode == "boxlite" and backend != "soothe":
            raise ValueError(
                "isolation_mode='boxlite' requires FLOWJET_SERVER_BACKEND=soothe, "
                f"but got backend='{backend}'. Boxlite isolation only works with "
                "the soothe host runtime, not soothe-nano. Either set "
                "FLOWJET_SERVER_BACKEND=soothe (or unset it for the default) "
                "or set FLOWJET_ISOLATION_MODE=thread_pool to use in-process "
                "isolation with the nano backend."
            )

    def model_ids(self) -> list[str]:
        return [m.strip() for m in self.models.split(",") if m.strip()]

    def home_path(self) -> Path:
        return Path(self.home).expanduser().resolve()

    def pool_settings(self) -> PoolSettings:
        return PoolSettings(
            min_size=max(1, self.thread_pool_min),
            max_size=max(self.thread_pool_min, self.thread_pool_max),
            idle_timeout_seconds=self.thread_pool_idle_timeout,
            max_requests_per_worker=self.max_requests_per_worker,
            reuse_runner=self.reuse_runner,
            request_timeout_seconds=self.request_timeout,
            ready_timeout_seconds=self.ready_timeout,
        )

    def boxlite_pool_settings(self):
        """Build BoxLitePoolSettings from server config (Phase 3)."""
        from flowjet.core.backends.isolation.boxlite_pool import BoxLitePoolSettings

        return BoxLitePoolSettings(
            image=self.boxlite_image,
            min_size=max(1, self.boxlite_min_pool),
            max_size=max(self.boxlite_min_pool, self.boxlite_max_pool),
            cpu_count=self.boxlite_cpu_count,
            mem_mib=self.boxlite_mem_mib,
            workspace_mount_mode=self.boxlite_workspace_mount_mode,
            local_fallback=self.boxlite_local_fallback,
            ready_timeout_seconds=self.ready_timeout,
        )
