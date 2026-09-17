"""FastAPI WebSocket route exposing the ACP server at ``/acp``.

Mounts an :class:`acp.http.server.AcpServer` whose agent factory bridges to the
flowjet ``RuntimeBackend``.  Other ACP clients connect via
``ws://host:port/acp`` and drive the same nano-backed runtime used by the
OpenAI-compatible Responses API.

The route is a thin ASGI bridge: on WebSocket upgrade it delegates to
:func:`acp.ws.server.handle_asgi_websocket`, which pumps JSON-RPC text frames
both directions and tears the connection down on disconnect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket

if TYPE_CHECKING:
    from acp.http.server import AcpServer

__all__ = ["create_acp_router"]


def create_acp_router(acp_server: AcpServer) -> APIRouter:
    """Build a FastAPI router with the ``/acp`` WebSocket endpoint."""

    router = APIRouter(tags=["acp"])

    @router.websocket("/acp")
    async def acp_websocket(websocket: WebSocket) -> None:
        """Bridge a WebSocket connection to the ACP server.

        The ACP WebSocket handler is a raw ASGI callable; we pass through the
        Starlette/FastAPI scope, receive, and send callables so the ACP SDK
        owns the full JSON-RPC frame pump.
        """
        from acp.ws.server import handle_asgi_websocket

        await handle_asgi_websocket(
            acp_server,
            websocket.scope,
            websocket.receive,
            websocket.send,
        )

    return router
