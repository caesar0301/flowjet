"""Tests for fj CLI logging quieting."""

from __future__ import annotations

import logging
import os
from io import StringIO

import pytest

from flowjet.cli.agent import configure_cli_logging


def test_configure_cli_logging_opts_out_of_browser_use_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BROWSER_USE_SETUP_LOGGING", raising=False)
    configure_cli_logging()
    assert os.environ["BROWSER_USE_SETUP_LOGGING"] == "false"


def test_configure_cli_logging_preserves_explicit_browser_use_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_USE_SETUP_LOGGING", "true")
    configure_cli_logging()
    assert os.environ["BROWSER_USE_SETUP_LOGGING"] == "true"


def test_configure_cli_logging_removes_root_console_handler() -> None:
    root = logging.getLogger()
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)-8s [%(name)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        configure_cli_logging()
        assert handler not in root.handlers
        logging.getLogger("soothe_nano.agent.builder").info("should not reach console")
        assert stream.getvalue() == ""
    finally:
        if handler in root.handlers:
            root.removeHandler(handler)
        handler.close()


def test_configure_cli_logging_suppresses_exception_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_cli_logging(verbose=False)
    try:
        raise TypeError("WizsearchSearchTool._arun() got an unexpected keyword argument 'limit'")
    except TypeError:
        logging.getLogger("soothe_nano.utils.tool_error_handler").exception(
            "wizsearch_search failed: TypeError: boom"
        )
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "unexpected keyword argument" not in captured.err


def test_configure_cli_logging_verbose_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_cli_logging(verbose=True)
    try:
        raise TypeError("unexpected keyword argument 'limit'")
    except TypeError:
        logging.getLogger("soothe_nano.utils.tool_error_handler").exception(
            "wizsearch_search failed: TypeError: unexpected keyword argument 'limit'"
        )
    captured = capsys.readouterr()
    assert "wizsearch_search failed" in captured.err
    assert "Traceback" not in captured.err


def test_silence_after_plugins_reapplies(monkeypatch: pytest.MonkeyPatch) -> None:
    from flowjet.cli.agent import silence_after_plugins

    calls: list[bool] = []
    monkeypatch.setattr(
        "flowjet.cli.agent.configure_cli_logging",
        lambda *, verbose=False: calls.append(verbose),
    )
    silence_after_plugins(verbose=True)
    assert calls == [True]


def test_compact_formatter_drops_traceback() -> None:
    from flowjet.cli.agent import _CompactConsoleFormatter

    formatter = _CompactConsoleFormatter("%(message)s")
    record = logging.LogRecord(
        name="t",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed",
        args=(),
        exc_info=None,
    )
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    assert formatter.format(record) == "failed"
    assert formatter.formatException(record.exc_info) == ""


def test_remove_root_console_preserves_file_handler(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from logging.handlers import RotatingFileHandler

    from flowjet.cli.agent import configure_cli_logging

    root = logging.getLogger()
    path = tmp_path / "app.log"
    file_handler = RotatingFileHandler(path)
    file_handler.set_name("keep-me")
    root.addHandler(file_handler)
    stream = logging.StreamHandler()
    root.addHandler(stream)
    try:
        configure_cli_logging()
        assert file_handler in root.handlers
        assert stream not in root.handlers
    finally:
        root.removeHandler(file_handler)
        file_handler.close()
        if stream in root.handlers:
            root.removeHandler(stream)
            stream.close()
