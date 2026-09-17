"""Map soothe-nano astream chunks to Agent Runtime events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from flowjet.core.events import (
    InterruptWaiting,
    Narration,
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunFailed,
    RunStarted,
    RuntimeEvent,
    ToolCompleted,
    ToolStarted,
)
from flowjet.core.tool_results import (
    preview_text,
    simplify_tool_error,
    tool_result_error_detail,
)
from flowjet.core.tool_stream import ToolCallArgAccumulator

_SKIP_CUSTOM_TYPES = frozenset(
    {
        "soothe.stream.end",
        "soothe.protocol.message.received",
        "soothe.internal.policy.checked",
        "soothe.internal.plugin.health_checked",
    }
)


def resolve_interaction_mode(metadata: dict[str, Any] | None) -> str:
    """Return ``agent`` or ``ask`` from request metadata (default ``agent``)."""
    raw = (metadata or {}).get("interaction_mode", "agent")
    return raw if raw in ("agent", "ask") else "agent"


def ai_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def map_custom(data: dict[str, Any]) -> Progress | None:
    event_type = str(data.get("type") or "").strip()
    if not event_type or event_type in _SKIP_CUSTOM_TYPES:
        return None
    if event_type.startswith("soothe.output."):
        return None
    short = event_type[7:] if event_type.startswith("soothe.") else event_type
    parts = short.split(".")
    domain = parts[0] if parts else "agent"
    action = parts[-1] if parts else ""
    if any(k in data for k in ("prompt", "arguments", "args", "system_prompt")):
        tool = data.get("tool") or data.get("name")
        if isinstance(tool, str) and domain in {"tool", "mcp"}:
            return Progress(stage=tool, message=f"{action or 'Running'} {tool}".strip())
        return Progress(stage=domain.title(), message=action.replace("_", " ").title() or "Working")
    message = data.get("message") or data.get("action_preview") or action.replace("_", " ")
    stage = str(data.get("tool") or data.get("name") or domain).title()
    return Progress(stage=str(stage), message=str(message or "Working"), raw=data)


async def iter_nano_runtime_events(
    agent: Any,
    *,
    run_id: str,
    model: str,
    session: str,
    input_text: str,
    workspace: str | None = None,
    thread_id: str | None = None,
    interaction_mode: str | None = "agent",
) -> AsyncIterator[RuntimeEvent]:
    """Drive ``agent.astream`` and yield sanitized runtime events."""
    try:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("langchain-core is required for the nano bridge") from exc

    tid = thread_id or session
    yield RunStarted(run_id=run_id, model=model, session=session)

    # ``None`` lets soothe-nano resolve the mode itself (CLI: the mode was
    # already chosen when the agent was built).
    mode = interaction_mode if interaction_mode in ("agent", "ask") else None
    configurable: dict[str, Any] = {"thread_id": tid}
    if mode:
        configurable["interaction_mode"] = mode
    if workspace:
        configurable["workspace"] = workspace

    messages = [HumanMessage(content=input_text)]
    config = {"configurable": configurable}
    answer = ""
    composing = False
    open_tools: dict[str, str] = {}
    open_args: dict[str, dict[str, Any]] = {}
    tool_args = ToolCallArgAccumulator()
    emitted_calls: set[str] = set()

    try:
        async for chunk in agent.astream(
            {"messages": messages},
            config=config,
            stream_mode=["messages", "updates", "custom"],
            subgraphs=True,
        ):
            if not isinstance(chunk, tuple) or len(chunk) != 3:
                continue
            _ns, mode, data = chunk

            if mode == "custom" and isinstance(data, dict):
                mapped = map_custom(data)
                if mapped:
                    yield mapped
                continue

            if mode == "updates" and isinstance(data, dict) and "__interrupt__" in data:
                yield InterruptWaiting(message="Waiting for input…")
                continue

            if mode != "messages":
                continue
            if not isinstance(data, tuple) or len(data) != 2:
                continue
            message_obj, _meta = data

            if isinstance(message_obj, AIMessage):
                # Tool args stream as tool_call_chunks (partial JSON); the
                # accumulator resolves them into a dict for both renderers.
                updates = tool_args.ingest_message(message_obj)
                if updates:
                    answer = ""
                    composing = False
                    for tc_id, name, args in updates:
                        if tc_id in emitted_calls:
                            # Later updates refresh args; the call already started.
                            open_args[tc_id] = args
                            continue
                        emitted_calls.add(tc_id)
                        open_tools[tc_id] = str(name)
                        open_args[tc_id] = args
                        yield ToolStarted(tool=str(name), call_id=tc_id, args=args or None)
                    continue
                if getattr(message_obj, "tool_calls", None) or getattr(
                    message_obj, "tool_call_chunks", None
                ):
                    continue
                text = ai_text(message_obj)
                if text:
                    if text.startswith(answer):
                        delta = text[len(answer) :]
                    elif answer.startswith(text):
                        delta = ""
                    else:
                        delta = text
                    if delta:
                        answer = text if text.startswith(answer) else answer + delta
                        if not composing:
                            composing = True
                            yield Progress(stage="Working", message="Composing response…")
                        # Uncommitted: a later tool call discards this text.
                        yield Narration(text=answer)

            elif isinstance(message_obj, ToolMessage):
                tc_id = getattr(message_obj, "tool_call_id", None)
                call = str(tc_id) if tc_id else None
                name, tc_args = tool_args.pop(tc_id)
                if not tc_args:
                    tc_args = open_args.pop(call or "", None) or {}
                else:
                    open_args.pop(call or "", None)
                name = (
                    name
                    or open_tools.pop(call or "", None)
                    or getattr(message_obj, "name", None)
                    or "tool"
                )
                status = getattr(message_obj, "status", None)
                err_detail = tool_result_error_detail(message_obj.content)
                ok = status != "error" and err_detail is None
                detail = simplify_tool_error(err_detail) if err_detail else None
                yield ToolCompleted(
                    tool=str(name),
                    ok=ok,
                    call_id=call,
                    args=tc_args or None,
                    detail=detail,
                    preview=detail or preview_text(message_obj.content),
                )

        if answer:
            yield OutputTextDelta(delta=answer)
        yield RunCompleted(output_text=answer)
    except Exception as exc:  # noqa: BLE001
        yield RunFailed(message=str(exc) or type(exc).__name__)
