"""Server settings from environment."""

from __future__ import annotations

from pathlib import Path

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
