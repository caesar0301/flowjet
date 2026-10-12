"""FlowJetAcpAgent — bridges the ACP ``Agent`` protocol to a RuntimeBackend.

Each ACP connection gets a fresh ``FlowJetAcpAgent`` (via the agent factory).
The agent maps ACP session lifecycle + ``session/prompt`` calls onto the same
``RuntimeBackend.stream_run`` used by the OpenAI-compatible Responses API,
streaming progress / tool / output events back as ACP session notifications.

The client's working directory is part of the session setup, so it is captured
on ``session/new`` / ``load`` / ``resume`` / ``fork`` and replayed as
``RunRequest.metadata["workspace"]`` on each turn — that is what makes an ACP
session run in the client's project rather than in a hashed workspace.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp.helpers import (
    plan_entry,
    start_tool_call,
    text_block,
    update_agent_message_text,
    update_agent_thought,
    update_agent_thought_text,
    update_plan,
    update_tool_call,
)

from flowjet.core.events import (
    InterruptWaiting,
    Narration,
    OutputTextDelta,
    Progress,
    RunCompleted,
    RunFailed,
    RunRequest,
    RunStarted,
    ToolCompleted,
    ToolStarted,
    UsageInfo,
)
from flowjet.core.protocol import RuntimeBackend
from flowjet.server import __version__ as flowjet_version

if TYPE_CHECKING:
    from acp import Client

__all__ = ["FlowJetAcpAgent", "build_acp_agent_factory"]


def _extract_prompt_text(prompt: list[Any]) -> str:
    """Flatten ACP content blocks into a single text string."""
    parts: list[str] = []
    for block in prompt:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool-kind inference (Enhancement 4)
# ---------------------------------------------------------------------------

_READ_TOOLS = frozenset(
    {
        "read_file",
        "read",
        "cat",
        "grep",
        "glob",
        "ls",
        "list",
        "find",
        "search",
        "head",
        "tail",
    }
)
_EDIT_TOOLS = frozenset(
    {
        "write_file",
        "write",
        "edit_file",
        "edit",
        "create_file",
        "create",
        "delete_file",
        "delete",
        "remove",
        "move_file",
        "move",
        "rename",
        "mkdir",
        "rmdir",
    }
)
_EXECUTE_TOOLS = frozenset(
    {
        "run_command",
        "run",
        "execute",
        "exec",
        "bash",
        "shell",
        "terminal",
        "subprocess",
    }
)
_SEARCH_TOOLS = frozenset(
    {
        "web_search",
        "search_web",
        "fetch",
        "http_get",
        "http_post",
        "curl",
        "wget",
    }
)


def _infer_tool_kind(tool_name: str) -> str:
    """Map a tool name to an ACP ``ToolKind``.

    ACP supports ``read``, ``edit``, ``delete``, ``move``, ``search``,
    ``execute``, ``think``, ``fetch``, ``switch_mode``, ``other``.  We infer
    the kind from the tool name so ACP clients can distinguish read tools
    (e.g. ``read_file``, ``grep``) from edit tools (e.g. ``write_file``,
    ``edit_file``) and execute tools (e.g. ``run_command``).
    """
    name = tool_name.lower().strip()
    if name in _READ_TOOLS:
        return "read"
    if name in _EDIT_TOOLS:
        return "edit"
    if name in _EXECUTE_TOOLS:
        return "execute"
    if name in _SEARCH_TOOLS:
        return "fetch"
    return "other"


# ---------------------------------------------------------------------------
# Plan projection (Enhancement 5)
# ---------------------------------------------------------------------------


def _raw_to_plan_entries(raw: dict[str, Any] | None) -> list[Any] | None:
    """Convert a ``soothe.step.progress`` raw payload to ACP plan entries.

    The soothe runtime emits todo/step progress as ``Progress`` events with
    ``raw`` payloads containing ``soothe.step.progress`` data (step number,
    total steps, current todo text).  This projects that structured data
    into ACP ``AgentPlanUpdate`` entries so ACP clients can render a
    structured plan/todo list.

    Returns ``None`` when the raw payload is not a step-progress event.
    """
    if not raw:
        return None
    raw_type = str(raw.get("type") or "").strip()
    if raw_type != "soothe.step.progress":
        return None
    steps = raw.get("steps")
    todo = str(raw.get("todo") or "").strip()
    step = raw.get("step")
    done = raw.get("done")
    status = str(raw.get("status") or "").strip()
    if not steps or not isinstance(steps, int):
        return None
    # Build a list of plan entries representing the todo list state.
    entries: list[Any] = []
    for i in range(1, steps + 1):
        if i == step:
            entry_status = (
                status if status in ("pending", "in_progress", "completed") else "in_progress"
            )
            content = todo or f"Step {i}"
        elif done is not None and isinstance(done, int) and i <= done:
            entry_status = "completed"
            content = f"Step {i}"
        else:
            entry_status = "pending"
            content = f"Step {i}"
        entries.append(
            plan_entry(
                content=content,
                priority="medium",
                status=entry_status,  # type: ignore[arg-type]
            )
        )
    return entries if entries else None


# ---------------------------------------------------------------------------
# Tool content helpers (Enhancement 3)
# ---------------------------------------------------------------------------


def _tool_locations(args: dict[str, Any] | None) -> list[Any] | None:
    """Extract ACP ``ToolCallLocation`` entries from tool args.

    Looks for common path-like keys (``file_path``, ``path``,
    ``target_file``) and converts them to ``ToolCallLocation`` objects.
    """
    if not args:
        return None
    from acp.schema import ToolCallLocation

    locations: list[Any] = []
    for key in ("file_path", "path", "target_file", "directory", "dir"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            locations.append(ToolCallLocation(path=value))
    return locations if locations else None


def _tool_raw_input(args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the tool args as ``raw_input`` for ACP tool calls."""
    if not args:
        return None
    return args


class FlowJetAcpAgent:
    """ACP ``Agent`` backed by a flowjet ``RuntimeBackend``.

    The agent is stateless across connections — each ``session/new`` mints a
    fresh session id and each ``session/prompt`` drives one ``stream_run``.
    Session→thread continuity is delegated to the backend's persistence layer
    (the session id is passed as ``RunRequest.session`` / thread_id).

    The one piece of per-session state kept here is the workspace: ACP carries
    the client's working directory on the session-setup calls (``session/new``,
    ``load``, ``resume``, ``fork``) but not on ``session/prompt``, so the cwd is
    recorded when the session is established and replayed as
    ``RunRequest.metadata["workspace"]`` on every turn.
    """

    def __init__(self, backend: RuntimeBackend, client: Client | None = None) -> None:
        self._backend = backend
        self._client = client
        self._sessions: set[str] = set()
        # Session id → the cwd the client asked that session to run in.
        self._session_cwd: dict[str, str] = {}
        # Track active prompt tasks so session/cancel can interrupt them.
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}

    def _remember_cwd(self, session_id: str, cwd: str | None) -> None:
        """Record the session's workspace, ignoring an absent or blank cwd."""
        if isinstance(cwd, str) and cwd.strip():
            self._session_cwd[session_id] = cwd.strip()

    # -- ACP Agent protocol ------------------------------------------------

    def on_connect(self, conn: Client) -> None:
        """Called by the ACP SDK when the client connection is ready."""
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp.meta import PROTOCOL_VERSION
        from acp.schema import AgentCapabilities, Implementation, InitializeResponse

        version = protocol_version if protocol_version <= PROTOCOL_VERSION else PROTOCOL_VERSION
        return InitializeResponse(
            protocolVersion=version,
            agentCapabilities=AgentCapabilities(
                loadSession=True,
            ),
            agentInfo=Implementation(
                name="flowjet-server",
                title="FlowJet Server",
                version=flowjet_version,
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp.schema import NewSessionResponse

        session_id = f"acp-{uuid4().hex}"
        self._sessions.add(session_id)
        self._remember_cwd(session_id, cwd)
        return NewSessionResponse(sessionId=session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp.schema import LoadSessionResponse

        self._sessions.add(session_id)
        self._remember_cwd(session_id, cwd)
        return LoadSessionResponse()

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> Any:
        from acp.schema import ListSessionsResponse, SessionInfo

        sessions = [SessionInfo(sessionId=sid, cwd=cwd or ".") for sid in sorted(self._sessions)]
        return ListSessionsResponse(sessions=sessions)

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> Any:
        from acp.schema import SetSessionModeResponse

        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> Any:
        from acp.schema import SetSessionConfigOptionResponse

        # flowjet-server does not expose configurable session options yet;
        # return an empty list so the response validates against the schema.
        return SetSessionConfigOptionResponse(configOptions=[])

    async def authenticate(self, method_id: str, **kwargs: Any) -> Any:
        return None

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> Any:
        from acp.schema import PromptResponse, Usage

        client = self._client
        input_text = _extract_prompt_text(prompt)
        models = await self._backend.list_models()
        model = models[0].id if models else "default"

        # The cwd arrives on the session-setup calls, not on session/prompt, so
        # replay what this session was created with. Without it the backend
        # falls back to a hashed workspace under FLOWJET_HOME and the client's
        # project directory never reaches the agent.
        metadata: dict[str, Any] = {"source": "acp"}
        session_cwd = self._session_cwd.get(session_id)
        if session_cwd:
            metadata["workspace"] = session_cwd
        # Pass through emit_tools so the fake adapter emits tool-call events
        # during E2E testing (the real nano adapter ignores this flag).
        if kwargs.get("emit_tools"):
            metadata["emit_tools"] = True

        request = RunRequest(
            model=model,
            input_text=input_text,
            session=session_id,
            run_id=f"acp-{uuid4().hex}",
            metadata=metadata,
        )

        output_text = ""
        stop_reason = "end_turn"
        usage_info: UsageInfo | None = None

        async def _drive() -> None:
            nonlocal output_text, usage_info, stop_reason
            async for event in self._backend.stream_run(request):
                if isinstance(event, RunStarted):
                    if client is not None:
                        await client.session_update(
                            session_id,
                            update_agent_thought_text(
                                f"Run {event.run_id} started on {event.model}"
                            ),
                        )
                elif isinstance(event, Progress):
                    # Enhancement 5: project step-progress raw payloads into
                    # ACP AgentPlanUpdate so clients can render a structured
                    # plan/todo list.
                    plan_entries = _raw_to_plan_entries(event.raw)
                    if client is not None and plan_entries is not None:
                        await client.session_update(
                            session_id,
                            update_plan(plan_entries),
                        )
                    # Enhancement 6: pass the raw payload through to the
                    # agent_thought_chunk _meta so ACP clients that understand
                    # the soothe vocabulary can render richer status lines.
                    if client is not None:
                        thought = update_agent_thought(text_block(event.message))
                        if event.raw:
                            thought.field_meta = event.raw
                        await client.session_update(session_id, thought)
                elif isinstance(event, Narration):
                    # Enhancement 1: stream live narration (uncommitted text
                    # between tool calls) as agent_thought_chunk so ACP clients
                    # see the agent's intermediate reasoning ("Let me look that
                    # up.") instead of silence until the final answer.
                    if client is not None and event.text:
                        await client.session_update(
                            session_id,
                            update_agent_thought_text(event.text),
                        )
                elif isinstance(event, ToolStarted):
                    # Enhancement 3: pass tool args as raw_input and extract
                    # file-path locations so ACP clients can show rich tool
                    # metadata (file paths, diffs, terminal refs).
                    # Enhancement 4: infer tool kind (read/edit/execute/search)
                    # from the tool name instead of always "other".
                    if client is not None:
                        await client.session_update(
                            session_id,
                            start_tool_call(
                                tool_call_id=event.call_id or event.tool,
                                title=event.tool,
                                kind=_infer_tool_kind(event.tool),  # type: ignore[arg-type]
                                status="in_progress",
                                locations=_tool_locations(event.args),
                                raw_input=_tool_raw_input(event.args),
                            ),
                        )
                elif isinstance(event, ToolCompleted):
                    # Enhancement 3: pass tool result detail/preview as
                    # raw_output so ACP clients can show tool results inline.
                    if client is not None:
                        raw_output = None
                        if event.detail:
                            raw_output = {"detail": event.detail, "preview": event.preview}
                        elif event.preview:
                            raw_output = {"preview": event.preview}
                        await client.session_update(
                            session_id,
                            update_tool_call(
                                tool_call_id=event.call_id or event.tool,
                                status="completed" if event.ok else "failed",
                                raw_output=raw_output,
                            ),
                        )
                elif isinstance(event, OutputTextDelta):
                    if client is not None:
                        await client.session_update(
                            session_id,
                            update_agent_message_text(event.delta),
                        )
                    output_text += event.delta
                elif isinstance(event, InterruptWaiting):
                    stop_reason = "end_turn"
                    if client is not None:
                        await client.session_update(
                            session_id,
                            update_agent_thought_text(event.message or "Waiting for input"),
                        )
                elif isinstance(event, RunCompleted):
                    output_text = event.output_text or output_text
                    usage_info = event.usage
                    stop_reason = "end_turn"
                elif isinstance(event, RunFailed):
                    stop_reason = "end_turn"
                    if client is not None:
                        await client.session_update(
                            session_id,
                            update_agent_message_text(f"Error: {event.message}"),
                        )

        task = asyncio.ensure_future(_drive())
        self._active_tasks[session_id] = task
        try:
            await task
        except asyncio.CancelledError:
            stop_reason = "cancelled"
        finally:
            self._active_tasks.pop(session_id, None)

        usage = None
        if usage_info is not None:
            usage = Usage(
                totalTokens=usage_info.total_tokens,
                inputTokens=usage_info.input_tokens,
                outputTokens=usage_info.output_tokens,
            )

        return PromptResponse(stopReason=stop_reason, usage=usage)

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp.schema import ForkSessionResponse

        new_id = f"acp-{uuid4().hex}"
        self._sessions.add(new_id)
        self._remember_cwd(new_id, cwd)
        return ForkSessionResponse(sessionId=new_id)

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        from acp.schema import ResumeSessionResponse

        self._sessions.add(session_id)
        self._remember_cwd(session_id, cwd)
        return ResumeSessionResponse()

    async def close_session(self, session_id: str, **kwargs: Any) -> Any:
        from acp.schema import CloseSessionResponse

        task = self._active_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._sessions.discard(session_id)
        self._session_cwd.pop(session_id, None)
        return CloseSessionResponse()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        task = self._active_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        try:
            await self._backend.delete_run(session_id)
        except Exception:  # noqa: BLE001
            pass

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        pass


def build_acp_agent_factory(backend: RuntimeBackend):
    """Return an ``AgentFactory``: ``Client → FlowJetAcpAgent``."""

    def factory(client: Client) -> FlowJetAcpAgent:
        return FlowJetAcpAgent(backend=backend, client=client)

    return factory
