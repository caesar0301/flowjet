"""FastAPI routes for /v1/responses, /v1/chat/completions, and /v1/models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from flowjet.server.openai_compat.schemas import ChatCompletionRequest, CreateResponseRequest
from flowjet.server.openai_compat.service import ResponseService


def _service(request: Request) -> ResponseService:
    return request.app.state.service


def create_router(*, auth_dependency: Callable[..., Any] | None = None) -> APIRouter:
    router = APIRouter(prefix="/v1")
    deps = [Depends(auth_dependency)] if auth_dependency is not None else []

    @router.get("/models", dependencies=deps)
    async def list_models(
        service: Annotated[ResponseService, Depends(_service)],
    ) -> dict[str, Any]:
        return await service.list_models()

    @router.post("/responses", dependencies=deps)
    async def create_response(
        body: CreateResponseRequest,
        service: Annotated[ResponseService, Depends(_service)],
    ) -> Any:
        if body.stream:
            return StreamingResponse(
                service.create_stream(body),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        return await service.create(body)

    @router.get("/responses/{response_id}", dependencies=deps)
    async def get_response(
        response_id: str,
        service: Annotated[ResponseService, Depends(_service)],
    ) -> dict[str, Any]:
        return service.get(response_id)

    @router.delete("/responses/{response_id}", dependencies=deps)
    async def delete_response(
        response_id: str,
        service: Annotated[ResponseService, Depends(_service)],
    ) -> dict[str, Any]:
        return await service.delete(response_id)

    @router.post("/chat/completions", dependencies=deps)
    async def create_chat_completion(
        body: ChatCompletionRequest,
        service: Annotated[ResponseService, Depends(_service)],
    ) -> Any:
        if body.stream:
            return StreamingResponse(
                service.chat_completion_stream(body),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        return await service.chat_completion(body)

    return router
