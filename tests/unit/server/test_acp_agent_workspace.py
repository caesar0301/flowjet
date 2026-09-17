"""FlowJetAcpAgent carries the client's cwd into the run's workspace.

ACP puts the working directory on the session-setup calls (`session/new`,
`load`, `resume`, `fork`) but not on `session/prompt`, so the agent has to
remember it per session. Without that the backend falls back to a hashed
workspace under ``FLOWJET_HOME`` and a desktop client's project directory never
reaches the agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from flowjet.core.events import ModelInfo, RunCompleted, RunRequest, RuntimeEvent
from flowjet.server.acp.agent import FlowJetAcpAgent
from flowjet.server.config import Settings


@dataclass
class _TextBlock:
    """Stand-in for an ACP text content block (the agent reads `.text`)."""

    text: str


class RecordingBackend:
    """Minimal RuntimeBackend that records every RunRequest it receives."""

    def __init__(self) -> None:
        self.requests: list[RunRequest] = []

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="default")]

    async def delete_run(self, run_id: str) -> None:
        return None

    async def stream_run(self, request: RunRequest) -> AsyncIterator[RuntimeEvent]:
        self.requests.append(request)
        yield RunCompleted(output_text="ok")


async def _prompt(agent: FlowJetAcpAgent, session_id: str) -> None:
    await agent.prompt(session_id, [_TextBlock("hello")])


def _last_metadata(backend: RecordingBackend) -> dict:
    assert backend.requests, "the backend never received a run"
    return dict(backend.requests[-1].metadata or {})


def test_allow_external_workspace_defaults_to_true():
    """Session workspaces follow the caller unless an operator opts out.

    Asserted against the declared field default rather than a constructed
    Settings so a developer's FLOWJET_ALLOW_EXTERNAL_WORKSPACE cannot mask it.
    """
    default = Settings.model_fields["allow_external_workspace"].default

    assert default is True


@pytest.mark.asyncio
async def test_prompt_forwards_session_new_cwd_as_workspace():
    backend = RecordingBackend()
    agent = FlowJetAcpAgent(backend=backend)

    created = await agent.new_session(cwd="/home/dev/project")
    await _prompt(agent, created.sessionId)

    assert _last_metadata(backend)["workspace"] == "/home/dev/project"
    # `source` still marks the request as ACP-originated.
    assert _last_metadata(backend)["source"] == "acp"


@pytest.mark.asyncio
async def test_prompt_omits_workspace_when_session_has_no_cwd():
    """No cwd means "let the backend choose" — never a fabricated path."""
    backend = RecordingBackend()
    agent = FlowJetAcpAgent(backend=backend)

    session_id = "acp-no-cwd"
    await _prompt(agent, session_id)

    assert "workspace" not in _last_metadata(backend)


@pytest.mark.asyncio
async def test_blank_cwd_is_ignored_rather_than_used_as_a_path():
    backend = RecordingBackend()
    agent = FlowJetAcpAgent(backend=backend)

    created = await agent.new_session(cwd="   ")
    await _prompt(agent, created.sessionId)

    assert "workspace" not in _last_metadata(backend)


@pytest.mark.asyncio
async def test_load_and_resume_record_the_cwd():
    """Reopening a session must restore its workspace, not lose it."""
    backend = RecordingBackend()
    agent = FlowJetAcpAgent(backend=backend)

    await agent.load_session(cwd="/home/dev/loaded", session_id="acp-load")
    await _prompt(agent, "acp-load")
    assert _last_metadata(backend)["workspace"] == "/home/dev/loaded"

    await agent.resume_session(session_id="acp-resume", cwd="/home/dev/resumed")
    await _prompt(agent, "acp-resume")
    assert _last_metadata(backend)["workspace"] == "/home/dev/resumed"


@pytest.mark.asyncio
async def test_fork_records_the_cwd_for_the_new_session():
    backend = RecordingBackend()
    agent = FlowJetAcpAgent(backend=backend)

    parent = await agent.new_session(cwd="/home/dev/parent")
    forked = await agent.fork_session(session_id=parent.sessionId, cwd="/home/dev/forked")
    await _prompt(agent, forked.sessionId)

    assert _last_metadata(backend)["workspace"] == "/home/dev/forked"


@pytest.mark.asyncio
async def test_close_session_forgets_the_cwd():
    """A closed session must not leak its workspace into a later prompt."""
    backend = RecordingBackend()
    agent = FlowJetAcpAgent(backend=backend)

    created = await agent.new_session(cwd="/home/dev/project")
    await agent.close_session(created.sessionId)
    await _prompt(agent, created.sessionId)

    assert "workspace" not in _last_metadata(backend)
