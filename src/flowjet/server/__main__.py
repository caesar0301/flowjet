"""CLI entry: run uvicorn with dual-stack IPv4/IPv6 support."""

from __future__ import annotations

import socket

import uvicorn

from flowjet.server.config import Settings


def _create_dual_stack_socket(host: str, port: int) -> socket.socket:
    """Create a dual-stack socket that accepts both IPv4 and IPv6.

    On macOS, binding to ``::`` (IPv6 wildcard) only listens on IPv6 by
    default because ``IPV6_V6ONLY`` is 1.  ``localhost`` resolves to ``::1``
    (IPv6) first, so a server bound to ``0.0.0.0`` (IPv4-only) is
    unreachable from browser ``fetch()`` calls — the browser tries IPv6,
    gets "Connection refused", and reports "Failed to fetch" before CORS
    even applies.

    This function creates an IPv6 socket with ``IPV6_V6ONLY=0`` so the
    same listening socket accepts connections on both address families.
    """
    # IPv6 wildcard — accepts IPv4-mapped addresses when V6ONLY=0.
    addr = ("::", port) if host in ("::", "0.0.0.0") else (host, port)
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "IPV6_V6ONLY"):
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(addr)
    sock.listen(2048)
    sock.setblocking(False)
    return sock


def main() -> None:
    # Silence noisy third-party warnings (requests version-mismatch,
    # LangChain beta-API) before the agent stack is imported.
    from flowjet.core._warnings import suppress_known_warnings

    suppress_known_warnings()

    # Pull in a ~/.soothe/config copy before the first agent build. Kept out of
    # create_app(): the ASGI app is built at import time, and importing a module
    # must not write files.
    from flowjet.home import ensure_flowjet_config

    ensure_flowjet_config()

    settings = Settings()
    # When the host is a wildcard (``::`` or ``0.0.0.0``), use a dual-stack
    # socket so both IPv4 and IPv6 clients can connect.  This is essential
    # for browser-based clients (e.g. Airi) where ``localhost`` may resolve
    # to either ``127.0.0.1`` or ``::1`` depending on the OS.
    if settings.host in ("::", "0.0.0.0"):
        sock = _create_dual_stack_socket(settings.host, settings.port)
        config = uvicorn.Config(
            "flowjet.server.http.app:app",
            port=settings.port,
            factory=False,
            reload=False,
            log_level="info",
        )
        server = uvicorn.Server(config)
        config.load()
        server.run(sockets=[sock])
    else:
        uvicorn.run(
            "flowjet.server.http.app:app",
            host=settings.host,
            port=settings.port,
            factory=False,
            reload=False,
        )


if __name__ == "__main__":
    main()
