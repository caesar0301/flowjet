"""ACP (Agent Client Protocol) support for flowjet-server.

Exposes the RuntimeBackend as an ACP ``Agent`` reachable over WebSocket at
``/acp``.  Other ACP clients connect via ``ws://host:port/acp`` and drive the
same nano-backed runtime used by the OpenAI-compatible Responses API.
"""

from flowjet.server.acp.agent import FlowJetAcpAgent, build_acp_agent_factory
from flowjet.server.acp.routes import create_acp_router

__all__ = ["FlowJetAcpAgent", "build_acp_agent_factory", "create_acp_router"]
