"""FlowJetAcpAgent — bridges the ACP ``Agent`` protocol to a RuntimeBackend.

Each ACP connection gets a fresh ``FlowJetAcpAgent`` (via the agent factory).
The agent maps ACP session lifecycle + ``session/prompt`` calls onto the same
``RuntimeBackend.stream_run`` used by the OpenAI-compatible Responses API,
streaming progress / tool / output events back as ACP session notifications.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp.helpers import (
    start_tool_call,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
)

from flowjet.core.events import (
    InterruptWaiting,
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


class FlowJetAcpAgent:
    """ACP ``Agent`` backed by a flowjet ``RuntimeBackend``.

    The agent is stateless across connections — each ``session/new`` mints a
    fresh session id and each ``session/prompt`` drives one ``stream_run``.
    Session→thread continuity is delegated to the backend's persistence layer
    (the session id is passed as ``RunRequest.session`` / thread_id).
    """

    def __init__(self, backend: RuntimeBackend, client: Client | None = None) -> None:
        self._backend = backend
        self._client = client
        self._sessions: set[str] = set()
        # Track active prompt tasks so session/cancel can interrupt them.
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}

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

        request = RunRequest(
            model=model,
            input_text=input_text,
            session=session_id,
            run_id=f"acp-{uuid4().hex}",
            metadata={"source": "acp"},
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
                    if client is not None:
                        await client.session_update(
                            session_id,
                            update_agent_thought_text(event.message),
                        )
                elif isinstance(event, ToolStarted):
                    if client is not None:
                        await client.session_update(
                            session_id,
                            start_tool_call(
                                tool_call_id=event.call_id or event.tool,
                                title=event.tool,
                                kind="other",
                                status="in_progress",
                            ),
                        )
                elif isinstance(event, ToolCompleted):
                    if client is not None:
                        await client.session_update(
                            session_id,
                            update_tool_call(
                                tool_call_id=event.call_id or event.tool,
                                status="completed" if event.ok else "failed",
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

        return ResumeSessionResponse()

    async def close_session(self, session_id: str, **kwargs: Any) -> Any:
        from acp.schema import CloseSessionResponse

        task = self._active_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._sessions.discard(session_id)
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
