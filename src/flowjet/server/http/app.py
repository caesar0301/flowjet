"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from flowjet import __version__
from flowjet.core.backend import Backend, resolve_backend
from flowjet.core.bridges.nano import build_isolating_nano_backend
from flowjet.core.bridges.soothe import build_isolating_soothe_backend
from flowjet.core.modes import Surface
from flowjet.core.protocol import RuntimeBackend
from flowjet.server.acp import build_acp_agent_factory, create_acp_router
from flowjet.server.config import Settings
from flowjet.server.http.auth import require_api_key
from flowjet.server.openai_compat.errors import OpenAIError, error_body
from flowjet.server.openai_compat.routes import create_router
from flowjet.server.openai_compat.service import ResponseService
from flowjet.server.openai_compat.store import InMemoryRunStore


def build_backend(settings: Settings) -> RuntimeBackend:
    """Build the runtime backend based on the resolved server backend.

    Defaults to ``soothe`` (host runtime via ``ModeRoutedSootheAgent``);
    falls back to ``nano`` when ``FLOWJET_SERVER_BACKEND=nano`` is set.

    When ``settings.workspace_sync_source`` is set, a
    :class:`SyncAwareWorkspaceResolver` is constructed and injected —
    enabling fsspec workspace sync (Phase 2). Otherwise the default
    local-only :class:`WorkspaceResolver` is used (Phase 1).

    When ``settings.isolation_mode`` is ``"boxlite"``, a
    :class:`BoxLiteIsolationPool` is constructed and injected — enabling
    Docker-container-based session isolation (Phase 3). Otherwise the
    default in-process :class:`ThreadPool` is used (Phase 1/2).
    """
    backend = resolve_backend(Surface.SERVER)
    workspace_resolver = _build_workspace_resolver(settings)
    isolation_pool = _build_isolation_pool(settings)
    common = dict(
        models=settings.model_ids(),
        config_path=settings.nano_config,
        home=settings.home_path(),
        pool_settings=settings.pool_settings(),
        allow_external_workspace=settings.allow_external_workspace,
        workspace_resolver=workspace_resolver,
        pool=isolation_pool,
    )
    if backend is Backend.SOOTHE:
        return build_isolating_soothe_backend(**common)
    return build_isolating_nano_backend(**common)


def _build_workspace_resolver(settings: Settings) -> Any:
    """Construct the workspace resolver based on sync settings.

    When ``workspace_sync_source`` is set, build a
    :class:`SyncAwareWorkspaceResolver` backed by an fsspec sync backend.
    Otherwise return ``None`` so the backend uses the default
    :class:`WorkspaceResolver` (local-only, Phase 1 behavior).
    """
    sync_source = settings.workspace_sync_source
    if not sync_source or not sync_source.strip():
        return None
    from flowjet.core.backends.isolation.workspace import (
        SyncAwareWorkspaceResolver,
    )

    return SyncAwareWorkspaceResolver.from_settings(
        home=settings.home_path(),
        cas_root=settings.workspace_cas_root,
        sync_source=sync_source,
        sync_config_path=settings.workspace_sync_config,
        allow_external_workspace=settings.allow_external_workspace,
    )


def _build_isolation_pool(settings: Settings) -> Any:
    """Construct the isolation pool based on isolation mode settings.

    When ``isolation_mode`` is ``"boxlite"``, build a
    :class:`BoxLiteIsolationPool` for Docker-container-based session
    isolation (Phase 3). Otherwise return ``None`` so the backend uses
    the default in-process :class:`ThreadPool` (Phase 1/2 behavior).
    """
    if settings.isolation_mode != "boxlite":
        return None
    from flowjet.core.backends.isolation.boxlite_pool import (
        BoxLiteIsolationPool,
    )

    return BoxLiteIsolationPool(
        adapter_factory=_boxlite_adapter_factory(settings),
        settings=settings.boxlite_pool_settings(),
    )


def _boxlite_adapter_factory(settings: Settings) -> Any:
    """Return a zero-arg adapter factory for the boxlite pool.

    The adapter is constructed inside the container worker, not on the host.
    This factory is a placeholder — the pool passes it to satisfy the
    IsolationPool protocol but the worker builds its own adapter from the
    resolved backend setting.
    """
    from flowjet.core.backends.isolation.adapter import FakeAgentAdapter

    return FakeAgentAdapter


def create_app(
    settings: Settings | None = None,
    backend: RuntimeBackend | None = None,
) -> FastAPI:
    settings = settings or Settings()
    backend = backend or build_backend(settings)
    store = InMemoryRunStore()
    service = ResponseService(backend=backend, store=store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Tear down the ACP server's connection registry.
        acp_server = getattr(app.state, "acp_server", None)
        if acp_server is not None:
            await acp_server.registry.close_all()
        shutdown = getattr(app.state.backend, "shutdown", None)
        if shutdown is not None:
            result = shutdown()
            if hasattr(result, "__await__"):
                await result

    app = FastAPI(title="flowjet-server", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.api_key = settings.api_key
    app.state.backend = backend
    app.state.service = service

    # CORS — browser-based OpenAI-compatible clients (e.g. Airi) issue
    # cross-origin fetch() calls to /v1/models, /v1/chat/completions, and
    # /v1/responses. Without permissive CORS headers the browser blocks
    # every request, surfacing as "Failed to fetch" in the client.
    #
    # When ``allow_credentials`` is True the CORS spec forbids ``"*"`` for
    # ``Access-Control-Allow-Origin`` and ``Access-Control-Expose-Headers``.
    # Starlette reflects the request Origin for credentials, but a literal
    # ``"*"`` in ``expose_headers`` is emitted verbatim — browsers ignore
    # it and hide response headers from JS, which can break clients that
    # read streaming headers.  Listing the headers explicitly is the safe
    # pattern that works with credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "content-type",
            "x-request-id",
            "x-accel-buffering",
            "cache-control",
            "connection",
        ],
    )

    auth = require_api_key if settings.api_key else None
    app.include_router(create_router(auth_dependency=auth))

    # ACP (Agent Client Protocol) over WebSocket at /acp.
    from acp.http.server import AcpServer

    acp_agent_factory = build_acp_agent_factory(backend)
    acp_server = AcpServer(acp_agent_factory)
    app.state.acp_server = acp_server
    app.include_router(create_acp_router(acp_server))

    @app.get("/")
    async def root() -> dict:
        return {"service": "flowjet-server", "version": __version__}

    @app.get("/health")
    async def health() -> dict:
        body: dict = {"status": "ok"}
        metrics = getattr(backend, "pool_metrics", None)
        if callable(metrics):
            body["pool"] = metrics()
        return body

    @app.exception_handler(OpenAIError)
    async def openai_error_handler(_request: Request, exc: OpenAIError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=error_body(exc))

    return app


# Default ASGI app for `uvicorn flowjet.server.http.app:app`
app = create_app()
