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
    # Dual-source sync (Phase 3): content-root URI template with a
    # ``{workspace}`` placeholder (e.g. "s3://triarch/workspaces/{workspace}").
    # When set, workspace files (materials + mirrored agent outputs) are
    # materialized from the content root at session open and dirty writes /
    # deletions are mirrored back at checkpoint — giving non-engine readers
    # (backend / frontend) a plain-file view of every workspace. Empty = disabled.
    workspace_content_source: str | None = None

    # Isolation mode — "thread_pool" (default, in-process) or "boxlite"
    # (microVM-per-workspace sandboxing via the external boxlite runtime,
    # https://github.com/boxlite-ai/boxlite). "boxlite" requires the boxlite
    # SDK (bundled in the `server` extra) and host virtualization (/dev/kvm on
    # Linux, Hypervisor.framework on macOS).
    isolation_mode: Literal["thread_pool", "boxlite"] = "thread_pool"
    # OCI image for the in-VM worker container (typically the flowjet-server
    # image — it carries the flowjet venv + worker entrypoint).
    boxlite_image: str | None = None
    # Max concurrently live worker VMs (across all workspaces). VMs are
    # spawned on demand per workspace; there is no baseline pre-warm.
    boxlite_max_pool: int = 4
    # Per-VM resource limits. Note: guest memory is mmap'd by the server
    # process, so it counts against the server container's cgroup memory
    # limit — size the container as concurrent VMs × (mem + ~512MiB) + server.
    boxlite_vcpus: int = 2
    boxlite_mem_mib: int = 2048
    # Fixed in-VM mount point for the (single) workspace volume.
    boxlite_workspace_container_path: str = "/workspace"
    # Egress allow-list for worker VMs (comma-separated hosts). Empty = SDK
    # default network (no egress filtering).
    boxlite_allow_net: str | None = None
    # Comma-separated env var names copied from the server environment into
    # the worker VM (e.g. FLOWJET_NANO_CONFIG, FLOWJET_SERVER_BACKEND).
    boxlite_pass_env: str | None = None
    # Private OCI registry credentials for pulling the worker image
    # (env-only; never in config files).
    boxlite_registry_host: str | None = None
    boxlite_registry_username: str | None = None
    boxlite_registry_password: str | None = None
    # Boxlite runtime data dir (image cache, VM state). None = SDK default.
    # On the triarch deploy this should live on the flowjet-home volume
    # (e.g. /flowjet/data/boxlite).
    boxlite_home_dir: str | None = None
    boxlite_idle_timeout: float = 300.0
    boxlite_max_requests: int = 100

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
        """Build BoxLiteVMSettings from server config."""
        from flowjet.core.backends.isolation.boxlite_vm_pool import BoxLiteVMSettings

        allow_net: list[str] | None = None
        if self.boxlite_allow_net and self.boxlite_allow_net.strip():
            allow_net = [h.strip() for h in self.boxlite_allow_net.split(",") if h.strip()]
        pass_env: list[str] | None = None
        if self.boxlite_pass_env and self.boxlite_pass_env.strip():
            pass_env = [e.strip() for e in self.boxlite_pass_env.split(",") if e.strip()]

        return BoxLiteVMSettings(
            image=self.boxlite_image,
            max_size=max(1, self.boxlite_max_pool),
            vcpus=self.boxlite_vcpus,
            mem_mib=self.boxlite_mem_mib,
            workspace_container_path=self.boxlite_workspace_container_path,
            allow_net=allow_net,
            pass_env=pass_env,
            registry_host=self.boxlite_registry_host,
            registry_username=self.boxlite_registry_username,
            registry_password=self.boxlite_registry_password,
            home_dir=self.boxlite_home_dir,
            idle_timeout_seconds=self.boxlite_idle_timeout,
            max_requests_per_worker=self.boxlite_max_requests,
            ready_timeout_seconds=self.ready_timeout,
        )
