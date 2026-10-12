"""Tests for the backend-isolation compatibility guard.

Boxlite isolation (Phase 3) requires the ``soothe`` host runtime.
When ``FLOWJET_SERVER_BACKEND=nano`` is combined with
``FLOWJET_ISOLATION_MODE=boxlite``, the server must fail fast at startup
rather than silently degrading to a nano adapter inside the container.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Settings.validate_isolation_compatibility
# ---------------------------------------------------------------------------


def test_validate_soothe_with_boxlite_is_okay():
    """soothe + boxlite is the valid combination — no error."""
    from flowjet.server.config import Settings

    s = Settings()
    s.isolation_mode = "boxlite"
    # Should not raise.
    s.validate_isolation_compatibility("soothe")


def test_validate_soothe_with_thread_pool_is_okay():
    """soothe + thread_pool is the default — no error."""
    from flowjet.server.config import Settings

    s = Settings()
    s.isolation_mode = "thread_pool"
    s.validate_isolation_compatibility("soothe")  # should not raise


def test_validate_nano_with_thread_pool_is_okay():
    """nano + thread_pool is valid — isolation is in-process."""
    from flowjet.server.config import Settings

    s = Settings()
    s.isolation_mode = "thread_pool"
    s.validate_isolation_compatibility("nano")  # should not raise


def test_validate_nano_with_boxlite_raises():
    """nano + boxlite is a misconfiguration — must raise."""
    from flowjet.server.config import Settings

    s = Settings()
    s.isolation_mode = "boxlite"
    with pytest.raises(ValueError, match="boxlite.*requires.*soothe"):
        s.validate_isolation_compatibility("nano")


def test_validate_error_message_mentions_both_fixes():
    """Error message tells the user how to fix it."""
    from flowjet.server.config import Settings

    s = Settings()
    s.isolation_mode = "boxlite"
    with pytest.raises(ValueError) as exc_info:
        s.validate_isolation_compatibility("nano")
    msg = str(exc_info.value)
    assert "FLOWJET_SERVER_BACKEND=soothe" in msg
    assert "FLOWJET_ISOLATION_MODE=thread_pool" in msg


# ---------------------------------------------------------------------------
# build_backend — integration guard
# ---------------------------------------------------------------------------


def test_build_backend_raises_on_nano_with_boxlite(monkeypatch, tmp_path):
    """build_backend() raises ValueError when nano + boxlite are combined."""
    from flowjet.server.config import Settings
    from flowjet.server.http.app import build_backend

    monkeypatch.setenv("FLOWJET_SERVER_BACKEND", "nano")
    monkeypatch.setenv("FLOWJET_ISOLATION_MODE", "boxlite")
    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))
    monkeypatch.setenv("FLOWJET_BOXLITE_LOCAL_FALLBACK", "true")

    s = Settings()
    with pytest.raises(ValueError, match="boxlite.*requires.*soothe"):
        build_backend(s)


def test_build_backend_succeeds_on_soothe_with_boxlite(monkeypatch, tmp_path):
    """build_backend() works when soothe + boxlite are configured."""
    from flowjet.server.config import Settings
    from flowjet.server.http.app import build_backend

    monkeypatch.setenv("FLOWJET_SERVER_BACKEND", "soothe")
    monkeypatch.setenv("FLOWJET_ISOLATION_MODE", "boxlite")
    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))
    monkeypatch.setenv("FLOWJET_BOXLITE_LOCAL_FALLBACK", "true")

    s = Settings()
    backend = build_backend(s)  # should not raise
    assert backend is not None


def test_build_backend_succeeds_on_nano_with_thread_pool(monkeypatch, tmp_path):
    """build_backend() works when nano + thread_pool are configured."""
    from flowjet.server.config import Settings
    from flowjet.server.http.app import build_backend

    monkeypatch.setenv("FLOWJET_SERVER_BACKEND", "nano")
    monkeypatch.setenv("FLOWJET_ISOLATION_MODE", "thread_pool")
    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))

    s = Settings()
    backend = build_backend(s)  # should not raise
    assert backend is not None


def test_build_backend_succeeds_on_defaults(monkeypatch, tmp_path):
    """build_backend() works with default settings (soothe + thread_pool)."""
    from flowjet.server.config import Settings
    from flowjet.server.http.app import build_backend

    monkeypatch.setenv("FLOWJET_HOME", str(tmp_path))

    s = Settings()
    backend = build_backend(s)
    assert backend is not None


# ---------------------------------------------------------------------------
# Default backend is soothe (not nano)
# ---------------------------------------------------------------------------


def test_default_backend_is_soothe():
    """Default server backend is soothe — isolation is safe by default."""
    from flowjet.core.backend import DEFAULT_SERVER_BACKEND, Backend

    assert DEFAULT_SERVER_BACKEND is Backend.SOOTHE
