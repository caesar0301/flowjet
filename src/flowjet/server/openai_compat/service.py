"""Orchestrate create / get / delete / list models."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from flowjet.core.backends.isolation.errors import IsolationError
from flowjet.core.events import RunFailed, RunRequest
from flowjet.core.protocol import RuntimeBackend
from flowjet.server.openai_compat.errors import OpenAIError
from flowjet.server.openai_compat.projection import ProjectionEngine
from flowjet.server.openai_compat.schemas import (
    ChatCompletionRequest,
    CreateResponseRequest,
    merge_flowjet_metadata,
    messages_to_input,
    normalize_input,
)
from flowjet.server.openai_compat.store import InMemoryRunStore


def format_sse(payload: dict[str, Any]) -> str:
    event_type = payload.get("type", "message")
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_chat_sse(payload: dict[str, Any]) -> str:
    """Format a Chat Completions SSE chunk."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ResponseService:
    def __init__(self, backend: RuntimeBackend, store: InMemoryRunStore | None = None) -> None:
        self.backend = backend
        self.store = store or InMemoryRunStore()

    async def list_models(self) -> dict[str, Any]:
        models = await self.backend.list_models()
        return {
            "object": "list",
            "data": [
                {
                    "id": m.id,
                    "object": "model",
                    "created": 0,
                    "owned_by": m.owned_by,
                }
                for m in models
            ],
        }

    def get(self, response_id: str) -> dict[str, Any]:
        body = self.store.get(response_id)
        if body is None:
            raise OpenAIError(
                f"No response found with id '{response_id}'.",
                code="response_not_found",
                param="id",
                status_code=404,
            )
        return body

    async def delete(self, response_id: str) -> dict[str, Any]:
        if not self.store.delete(response_id):
            raise OpenAIError(
                f"No response found with id '{response_id}'.",
                code="response_not_found",
                param="id",
                status_code=404,
            )
        await self.backend.delete_run(response_id)
        return {"id": response_id, "object": "response", "deleted": True}

    async def create(self, body: CreateResponseRequest) -> dict[str, Any]:
        response_id, engine, event_iter = await self._prepare(body)
        async for _payload in event_iter:
            pass
        final = engine.final_response()
        self.store.put(response_id, final)
        return final

    async def create_stream(self, body: CreateResponseRequest) -> AsyncIterator[str]:
        response_id, engine, event_iter = await self._prepare(body)
        async for payload in event_iter:
            yield format_sse(payload)
            if payload.get("type") in ("response.completed", "response.failed"):
                self.store.put(response_id, engine.final_response())

    async def _prepare(
        self, body: CreateResponseRequest
    ) -> tuple[str, ProjectionEngine, AsyncIterator[dict[str, Any]]]:
        models = {m.id for m in await self.backend.list_models()}
        if body.model not in models:
            raise OpenAIError(
                f"Model '{body.model}' not found.",
                code="model_not_found",
                param="model",
                status_code=404,
            )

        opts = body.flowjet
        projection = opts.projection if opts else "report"
        session = opts.session if opts else None
        metadata = merge_flowjet_metadata(opts)
        response_id = f"resp_{uuid4().hex}"
        engine = ProjectionEngine(projection, response_id, body.model)

        request = RunRequest(
            model=body.model,
            input_text=normalize_input(body.input),
            session=session,
            metadata=metadata,
            run_id=response_id,
        )

        self.store.put(
            response_id,
            {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "model": body.model,
                "output": [],
                "usage": None,
            },
        )

        async def gen() -> AsyncIterator[dict[str, Any]]:
            try:
                async for runtime_event in self.backend.stream_run(request):
                    for payload in engine.handle(runtime_event):
                        yield payload
            except IsolationError as exc:
                raise OpenAIError(
                    exc.message,
                    code=exc.code,
                    status_code=400,
                ) from exc
            except Exception as exc:  # noqa: BLE001
                for payload in engine.handle(RunFailed(message=str(exc) or type(exc).__name__)):
                    yield payload
                self.store.put(response_id, engine.final_response())

        return response_id, engine, gen()

    # ------------------------------------------------------------------
    # Chat Completions API  (POST /v1/chat/completions)
    # ------------------------------------------------------------------

    async def chat_completion(self, body: ChatCompletionRequest) -> dict[str, Any]:
        """Non-streaming Chat Completions response."""
        response_id, engine, event_iter = await self._prepare_chat(body)
        async for _payload in event_iter:
            pass
        return self._chat_response(body.model, response_id, engine)

    async def chat_completion_stream(self, body: ChatCompletionRequest) -> AsyncIterator[str]:
        """Streaming Chat Completions response (SSE)."""
        response_id, engine, event_iter = await self._prepare_chat(body)
        created = int(time.time())

        # initial role chunk
        yield format_chat_sse(
            self._chat_chunk(body.model, response_id, created, role="assistant", content="")
        )

        async for payload in event_iter:
            etype = payload.get("type", "")
            if etype == "response.output_text.delta":
                delta = payload.get("delta", "")
                if delta:
                    yield format_chat_sse(
                        self._chat_chunk(body.model, response_id, created, content=delta)
                    )
            elif etype in ("response.completed", "response.failed"):
                final = engine.final_response()
                finish = "stop" if final.get("status") == "completed" else "stop"
                yield format_chat_sse(
                    self._chat_chunk(
                        body.model,
                        response_id,
                        created,
                        content="",
                        finish_reason=finish,
                    )
                )
                yield "data: [DONE]\n\n"

    async def _prepare_chat(
        self, body: ChatCompletionRequest
    ) -> tuple[str, ProjectionEngine, AsyncIterator[dict[str, Any]]]:
        models = {m.id for m in await self.backend.list_models()}
        if body.model not in models:
            raise OpenAIError(
                f"Model '{body.model}' not found.",
                code="model_not_found",
                param="model",
                status_code=404,
            )

        opts = body.flowjet
        projection = opts.projection if opts else "report"
        session = opts.session if opts else None
        metadata = merge_flowjet_metadata(opts)
        response_id = f"chatcmpl_{uuid4().hex}"
        engine = ProjectionEngine(projection, response_id, body.model)

        request = RunRequest(
            model=body.model,
            input_text=messages_to_input(body.messages),
            session=session,
            metadata=metadata,
            run_id=response_id,
        )

        async def gen() -> AsyncIterator[dict[str, Any]]:
            try:
                async for runtime_event in self.backend.stream_run(request):
                    for payload in engine.handle(runtime_event):
                        yield payload
            except IsolationError as exc:
                raise OpenAIError(
                    exc.message,
                    code=exc.code,
                    status_code=400,
                ) from exc
            except Exception as exc:  # noqa: BLE001
                for payload in engine.handle(RunFailed(message=str(exc) or type(exc).__name__)):
                    yield payload

        return response_id, engine, gen()

    @staticmethod
    def _chat_chunk(
        model: str,
        chunk_id: str,
        created: int,
        *,
        role: str | None = None,
        content: str = "",
        finish_reason: str | None = None,
    ) -> dict[str, Any]:
        delta: dict[str, Any] = {}
        if role is not None:
            delta["role"] = role
        if content:
            delta["content"] = content
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

    @staticmethod
    def _chat_response(model: str, response_id: str, engine: ProjectionEngine) -> dict[str, Any]:
        final = engine.final_response()
        text = ""
        output = final.get("output") or []
        for item in output:
            if isinstance(item, dict):
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text += part.get("text", "")
        usage = final.get("usage") or {}
        prompt_tokens = usage.get("input_tokens", 0) if usage else 0
        completion_tokens = usage.get("output_tokens", 0) if usage else 0
        return {
            "id": response_id,
            "object": "chat.completion",
            "created": engine.created_at,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
