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

# Host (soothe) executor emits this as a ``mode=custom`` chunk carrying
# pre-parsed tool kwargs — a structured wire event that bypasses the fragile
# ``tool_call_chunks`` partial-JSON accumulation. Handled in
# ``iter_nano_runtime_events`` (not ``map_custom``) so it can yield a real
# ``ToolStarted`` and feed the accumulator's arg overlays.
_STREAM_TOOL_CALL_UPDATE = "soothe.stream.tool_call.update"

# Host (soothe) middleware surfaces as ``<Middleware>.<phase>`` graph updates.
# The CLI renders these through ``friendly_progress``, so the synthetic payloads
# below stay in the same ``soothe.*`` vocabulary as nano's custom events.
_HOST_PHASE_EVENTS: dict[str, str] = {
    "GoalStepGuardMiddleware": "soothe.step.planned",
    "DecomposeTaskMiddleware": "soothe.step.decomposed",
    "EvalStepMiddleware": "soothe.step.evaluated",
    "WestWorldMiddleware": "soothe.step.fanned_out",
    "AskUserPromptMiddleware": "soothe.ask.requested",
    "IntakeOnlyTaskGuardMiddleware": "soothe.intake.routed",
}

# Plan bookkeeping: the todo list already drives the step line.
_QUIET_TOOLS = frozenset({"write_todos", "todo_write", "write_todo_list"})

# Fires on every turn with no user-visible content.
_SKIP_UPDATE_NODES = frozenset(
    {
        "PatchToolCallsMiddleware",
        "ProgressiveToolMiddleware",
        "WorkspaceContextMiddleware",
        "TodoListMiddleware",
        "model",
        "tools",
    }
)


def todo_step_payload(todos: list[Any]) -> dict[str, Any] | None:
    """Turn a soothe todo list into a step-progress payload.

    The agent writes its plan as todos (``write_todos``); each ``in_progress``
    item is the step the user is waiting on, which is far more informative than
    a generic "Working" line.
    """
    items = [item for item in todos if isinstance(item, dict)]
    if not items:
        return None
    total = len(items)
    done = sum(1 for item in items if str(item.get("status")) == "completed")
    current = next(
        (item for item in items if str(item.get("status")) == "in_progress"),
        None,
    )
    if current is None:
        if done == 0:
            return None
        return {
            "type": "soothe.step.progress",
            "step": done,
            "steps": total,
            "done": done,
            "status": "completed",
            "todo": "",
        }
    return {
        "type": "soothe.step.progress",
        "step": items.index(current) + 1,
        "steps": total,
        "done": done,
        "status": "in_progress",
        "todo": str(current.get("content") or ""),
    }


def _skill_names(payload: dict[str, Any]) -> list[str]:
    """Best-effort skill names from a ``SkillActivationMiddleware`` payload."""
    raw = payload.get("skill_activation")
    if isinstance(raw, dict):
        raw = raw.get("skills") or raw.get("activated") or raw.get("names") or []
    if not isinstance(raw, (list, tuple)):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            name = item.get("name") or item.get("skill")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def update_progress(data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Map one soothe ``updates`` chunk to ``(dedupe_key, event_payload)``.

    Returns ``None`` for the plumbing updates that carry no user-visible step.
    """
    todos = data.get("todos")
    if isinstance(todos, list):
        payload = todo_step_payload(todos)
        if payload is not None:
            key = f"todo:{payload['step']}/{payload['steps']}:{payload['todo']}:{payload['done']}"
            return key, payload

    for node, node_payload in data.items():
        name = node.split(".")[0]
        # The todo list rides along inside the ``tools`` node update.
        if isinstance(node_payload, dict) and isinstance(node_payload.get("todos"), list):
            payload = todo_step_payload(node_payload["todos"])
            if payload is not None:
                key = (
                    f"todo:{payload['step']}/{payload['steps']}:{payload['todo']}:{payload['done']}"
                )
                return key, payload
        if name in _SKIP_UPDATE_NODES:
            continue
        event = _HOST_PHASE_EVENTS.get(name)
        if event is not None:
            phase = node.split(".", 1)[1] if "." in node else ""
            return node, {"type": event, "middleware": name, "phase": phase}
        if name == "SkillActivationMiddleware" and isinstance(node_payload, dict):
            names = _skill_names(node_payload)
            if names:
                return node, {"type": "soothe.skill.activated", "skill": ", ".join(names)}
    return None


# Arg keys worth echoing in a one-line tool summary (first match wins).
_TOOL_SUMMARY_KEYS = (
    "file_path",
    "path",
    "target_file",
    "command",
    "cmd",
    "script",
    "query",
    "pattern",
    "url",
    "skill",
)


def tool_call_summary(name: str, args: dict[str, Any]) -> str:
    """``write_file a.txt``-style summary for renderers without tool metadata."""
    for key in _TOOL_SUMMARY_KEYS:
        value = args.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            text = " ".join(str(value).split())
            return f"{name} {text[:60]}"
    return str(name)


def step_message(payload: dict[str, Any]) -> str:
    """Human-readable one-liner for a step payload (used by non-CLI renderers)."""
    step = payload.get("step")
    steps = payload.get("steps")
    todo = str(payload.get("todo") or "").strip()
    head = f"{step}/{steps}" if step and steps else "step"
    if todo:
        return f"Step {head} · {todo}"
    if payload.get("status") == "completed":
        return f"Steps complete · {head}"
    return f"Step {head}"


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
    if event_type == _STREAM_TOOL_CALL_UPDATE:
        # Handled in ``iter_nano_runtime_events`` — yields a ToolStarted /
        # Progress refresh with the pre-parsed args, never a generic Progress.
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


def _wire_tool_call_update(
    data: dict[str, Any],
    *,
    emitted_calls: set[str],
    refreshed_calls: set[str],
    tool_args: ToolCallArgAccumulator,
) -> list[RuntimeEvent]:
    """Convert a ``soothe.stream.tool_call.update`` into ToolStarted / Progress.

    The host (soothe) executor pre-parses tool kwargs and emits them as a
    structured wire event — an efficient transit path that does not depend on
    ``tool_call_chunks`` partial-JSON accumulation. This makes that path
    first-class: a new call id yields ``ToolStarted`` (same as the
    ``messages`` path), and a repeat yields a ``Progress`` refresh so the
    status line picks up the parsed args.

    Args are also fed into ``tool_args`` overlays so the eventual
    ``ToolCompleted`` event carries them even if the ``messages``-mode chunks
    never produced parseable JSON.
    """
    tc_id = str(data.get("tool_call_id") or "").strip()
    name = str(data.get("name") or "").strip()
    args = data.get("args")
    if not isinstance(args, dict):
        args = {}
    if not tc_id:
        return []
    # Seed the accumulator's overlay so ToolCompleted (messages path) sees
    # the parsed args even if tool_call_chunks were empty or partial.
    tool_args.seed_overlay(tc_id, name=name, args=args)
    # Quiet tools (write_todos) are plan bookkeeping that already surfaces as
    # step progress — never emit a ToolStarted for them.
    if name in _QUIET_TOOLS:
        return []
    if tc_id in emitted_calls:
        # Already started via the messages path; refresh the status line with
        # the parsed args (Writing a.txt, not bare Writing).
        if args and tc_id not in refreshed_calls:
            refreshed_calls.add(tc_id)
            return [
                Progress(
                    stage="Tool",
                    message=tool_call_summary(name or "tool", args),
                    raw={
                        "type": "soothe.tool.started",
                        "tool": name,
                        "args": args,
                    },
                )
            ]
        return []
    emitted_calls.add(tc_id)
    return [ToolStarted(tool=name or "tool", call_id=tc_id, args=args or None)]


def _extract_ask_user_interrupts(
    raw: Any,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Pull ``ask_user`` questions and interrupt ids from a ``__interrupt__`` payload.

    LangGraph emits ``__interrupt__`` as a tuple of ``Interrupt`` objects (or
    dicts). Each carries a ``value`` with the interrupt payload; soothe's
    ``ask_user`` tool tags it with ``{"type": "ask_user", "questions": [...]}``.

    Returns ``(questions, interrupt_ids)`` — only questions from ``ask_user``
    interrupts are surfaced; other interrupt kinds (tool approval, etc.) yield
    an empty questions tuple but still carry their interrupt id so the CLI can
    render a generic "paused for input" message.
    """
    if not isinstance(raw, (list, tuple)):
        raw = (raw,) if raw is not None else ()
    questions: list[dict[str, Any]] = []
    interrupt_ids: list[str] = []
    for entry in raw:
        value: Any = None
        int_id: str = ""
        # LangGraph Interrupt objects have ``.value`` / ``.id``; serialized
        # dicts use the same keys.
        if hasattr(entry, "value"):
            value = getattr(entry, "value", None)
            int_id = str(getattr(entry, "id", "") or "")
        elif isinstance(entry, dict):
            value = entry.get("value")
            int_id = str(entry.get("id", "") or "")
        if int_id:
            interrupt_ids.append(int_id)
        if isinstance(value, dict) and value.get("type") == "ask_user":
            qs = value.get("questions")
            if isinstance(qs, list):
                for q in qs:
                    if isinstance(q, dict):
                        questions.append(q)
    return tuple(questions), tuple(interrupt_ids)


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
    resume_value: Any = None,
) -> AsyncIterator[RuntimeEvent]:
    """Drive ``agent.astream`` and yield sanitized runtime events.

    When ``resume_value`` is set, the run resumes a paused graph (an
    ``ask_user`` interrupt) with that value via ``Command(resume=...)``
    instead of starting a new turn from ``input_text``.
    """
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

    # Resume a paused graph (ask_user interrupt) rather than starting a new
    # turn. LangGraph's ``Command(resume=...)`` delivers the value to the
    # ``interrupt()`` call that suspended the graph.
    if resume_value is not None:
        from langgraph.types import Command

        payload: Any = Command(resume=resume_value)
    else:
        payload = {"messages": [HumanMessage(content=input_text)]}
    config = {"configurable": configurable}
    answer = ""
    composing = False
    open_tools: dict[str, str] = {}
    open_args: dict[str, dict[str, Any]] = {}
    tool_args = ToolCallArgAccumulator()
    emitted_calls: set[str] = set()
    refreshed_calls: set[str] = set()
    last_step_key = ""

    try:
        async for chunk in agent.astream(
            payload,
            config=config,
            stream_mode=["messages", "updates", "custom"],
            subgraphs=True,
        ):
            if not isinstance(chunk, tuple) or len(chunk) != 3:
                continue
            _ns, mode, data = chunk

            if mode == "custom" and isinstance(data, dict):
                # Host (soothe) wire tool-call update: pre-parsed args that
                # bypass tool_call_chunks partial-JSON accumulation. Handle
                # before map_custom so it yields a real ToolStarted.
                if str(data.get("type") or "").strip() == _STREAM_TOOL_CALL_UPDATE:
                    for event in _wire_tool_call_update(
                        data,
                        emitted_calls=emitted_calls,
                        refreshed_calls=refreshed_calls,
                        tool_args=tool_args,
                    ):
                        yield event
                    continue
                mapped = map_custom(data)
                if mapped:
                    yield mapped
                continue

            if mode == "updates" and isinstance(data, dict):
                if "__interrupt__" in data:
                    questions, interrupt_ids = _extract_ask_user_interrupts(data["__interrupt__"])
                    yield InterruptWaiting(
                        message="Waiting for input…",
                        questions=questions,
                        interrupt_ids=interrupt_ids,
                    )
                    continue
                # Plan steps (todos) and host middleware phases are the only
                # signal soothe gives us about *what* it is doing between tool
                # calls; surface each change once.
                step = update_progress(data)
                if step is not None:
                    key, payload = step
                    if key != last_step_key:
                        last_step_key = key
                        yield Progress(
                            stage="Step",
                            message=step_message(payload),
                            raw=payload,
                        )
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
                            # Tool args stream as partial JSON, so ToolStarted
                            # usually carries none — refresh the status line
                            # once they parse (Writing b1.txt, not Writing).
                            # Once per call: partial JSON grows on every token,
                            # and re-emitting it would flood the event stream.
                            if args and name not in _QUIET_TOOLS and tc_id not in refreshed_calls:
                                refreshed_calls.add(tc_id)
                                yield Progress(
                                    stage="Tool",
                                    message=tool_call_summary(str(name), args),
                                    raw={
                                        "type": "soothe.tool.started",
                                        "tool": name,
                                        "args": args,
                                    },
                                )
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
                if str(name) in _QUIET_TOOLS and ok:
                    # Plan bookkeeping already surfaced as a step event.
                    continue
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
