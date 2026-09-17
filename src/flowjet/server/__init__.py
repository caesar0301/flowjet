"""FlowJet server — ACP over WebSocket + OpenAI-compatible HTTP API.

Importing this package must not pull in FastAPI: the CLI imports it lazily so
that ``pip install flowjet`` (without the ``server`` extra) keeps working.
"""

from __future__ import annotations

from flowjet import __version__

__all__ = ["__version__"]
