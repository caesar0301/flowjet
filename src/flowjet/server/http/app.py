"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from flowjet import __version__
from flowjet.core.bridges.nano import build_isolating_nano_backend
from flowjet.core.protocol import RuntimeBackend
from flowjet.server.acp import build_acp_agent_factory, create_acp_router
from flowjet.server.config import Settings
from flowjet.server.http.auth import require_api_key
from flowjet.server.openai_compat.errors import OpenAIError, error_body
from flowjet.server.openai_compat.routes import create_router
from flowjet.server.openai_compat.service import ResponseService
from flowjet.server.openai_compat.store import InMemoryRunStore


def build_backend(settings: Settings) -> RuntimeBackend:
    return build_isolating_nano_backend(
        models=settings.model_ids(),
        config_path=settings.nano_config,
        home=settings.home_path(),
        pool_settings=settings.pool_settings(),
        allow_external_workspace=settings.allow_external_workspace,
    )


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
