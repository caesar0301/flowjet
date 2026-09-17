"""FlowJet — unified agent CLI and server (ACP + OpenAI-compatible HTTP).

The distribution ships three packages:

* ``flowjet.core``   — surface-agnostic runtime (events, backends, nano bridge)
* ``flowjet.cli``    — terminal one-shot agent (``fj``)
* ``flowjet.server`` — HTTP service (``flowjet-server`` / ``fj serve``)
"""

from __future__ import annotations

import importlib.metadata

try:
    __version__ = importlib.metadata.version("flowjet")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = ["__version__"]
