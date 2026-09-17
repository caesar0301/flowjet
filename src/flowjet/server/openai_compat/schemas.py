"""Request / FlowJet option schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ProjectionMode = Literal["report", "progress", "developer"]
InteractionMode = Literal["agent", "ask"]


class FlowjetOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    projection: ProjectionMode = "report"
    session: str | None = None
    metadata: dict[str, Any] | None = None
    interaction_mode: InteractionMode = Field(
        default="agent",
        description=(
            "soothe-nano interaction mode. ``agent`` allows mutating tools; "
            "``ask`` is hard read-only (inspect + answer only)."
        ),
    )


class CreateResponseRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    input: str | list[Any]
    stream: bool = False
    flowjet: FlowjetOptions | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI Chat Completions request schema.

    Translates the classic ``messages`` array into a single text input
    for the FlowJet backend, then maps the response back to the Chat
    Completions response format.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    temperature: float | None = None
    flowjet: FlowjetOptions | None = None


def normalize_input(value: str | list[Any]) -> str:
    """Flatten OpenAI-style input into a single text string (Phase 1)."""
    if isinstance(value, str):
        return value
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        # message-like: {role, content}
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        elif isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def messages_to_input(messages: list[dict[str, Any]]) -> str:
    """Flatten a Chat Completions ``messages`` array into a single text input.

    Each message is prefixed with its role (``system``, ``user``, ``assistant``,
    ``tool``) so the backend retains conversational context. Multi-part content
    arrays are flattened to their text blocks.
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        text: str
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if isinstance(block, str):
                    chunks.append(block)
                elif isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str):
                        chunks.append(t)
            text = "\n".join(chunks)
        else:
            text = str(content)
        parts.append(f"{role}: {text}")
    return "\n".join(parts)


def merge_flowjet_metadata(opts: FlowjetOptions | None) -> dict[str, Any]:
    """Build RunRequest.metadata from FlowJet options (includes interaction_mode)."""
    if opts is None:
        return {"interaction_mode": "agent"}
    meta: dict[str, Any] = dict(opts.metadata or {})
    meta["interaction_mode"] = opts.interaction_mode
    return meta
