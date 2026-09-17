"""``fj serve`` — run the FlowJet server from the unified CLI.

Thin wrapper: it maps a small set of flags onto the ``FLOWJET_*`` settings and
then delegates to :func:`flowjet.server.__main__.main`, which is the same entry
point as the ``flowjet-server`` console script. The import is lazy so a plain
``pip install flowjet`` (without the ``server`` extra) still runs every other
``fj`` command.
"""

from __future__ import annotations

import argparse
import os
import sys

_ENV_BY_FLAG = {
    "host": "FLOWJET_HOST",
    "port": "FLOWJET_PORT",
    "api_key": "FLOWJET_API_KEY",
    "nano_config": "FLOWJET_NANO_CONFIG",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowjet serve",
        description="Run the FlowJet server (ACP over WebSocket + OpenAI-compatible HTTP).",
    )
    parser.add_argument("--host", help="Bind address (FLOWJET_HOST, default '::').")
    parser.add_argument("-p", "--port", type=int, help="Bind port (FLOWJET_PORT, default 8618).")
    parser.add_argument("--api-key", help="Require 'Authorization: Bearer <key>' on /v1.")
    parser.add_argument("--nano-config", help="Path to nano.yml for the agent backend.")
    return parser


def run_serve(argv: list[str] | None = None) -> int:
    """Start the HTTP service (ACP + OpenAI-compatible) and block until exit."""
    args = _build_parser().parse_args(list(argv or []))
    for flag, env_name in _ENV_BY_FLAG.items():
        value = getattr(args, flag, None)
        if value is not None:
            os.environ[env_name] = str(value)

    try:
        from flowjet.server.__main__ import main
    except ImportError as exc:  # pragma: no cover - depends on install extras
        sys.stderr.write(
            "flowjet serve requires the server extra:\n"
            "    pip install 'flowjet[server]'\n"
            f"(missing dependency: {exc})\n"
        )
        return 2

    main()
    return 0
