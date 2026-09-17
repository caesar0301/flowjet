"""Tests for CLI error formatting."""

from __future__ import annotations

from io import StringIO

from flowjet.cli.stream import (
    format_cli_error,
    write_cli_error,
)
from flowjet.core.tool_results import simplify_tool_error, tool_result_error_detail


def test_tool_result_error_detail_from_string() -> None:
    detail = tool_result_error_detail(
        "Error: TypeError: WizsearchSearchTool._arun() got an unexpected keyword argument 'limit'"
    )
    assert detail is not None
    assert "unexpected keyword argument" in detail


def test_tool_result_error_detail_from_dict() -> None:
    assert tool_result_error_detail({"error": "boom"}) == "boom"
    assert tool_result_error_detail({"error": ""}) is None
    assert tool_result_error_detail({"ok": True}) is None
    assert tool_result_error_detail("ok results") is None


def test_tool_result_error_detail_non_string() -> None:
    assert tool_result_error_detail(None) is None
    assert tool_result_error_detail(42) is None
    assert tool_result_error_detail(["error"]) is None


def test_tool_result_error_detail_empty_and_prefixes() -> None:
    assert tool_result_error_detail("   ") is None
    assert tool_result_error_detail("error: not found") == "not found"
    assert tool_result_error_detail("ERROR: boom") == "boom"
    # Trailing space is stripped before prefix match, so bare "Error:" is not an error payload.
    assert tool_result_error_detail("Error:") is None


def test_tool_result_error_detail_json_string() -> None:
    assert tool_result_error_detail('{"error": "disk full"}') == "disk full"
    assert tool_result_error_detail('{"error": ""}') is None
    assert tool_result_error_detail('{"ok": true}') is None
    assert tool_result_error_detail('{"error": "broken"') is None  # invalid JSON


def test_simplify_unexpected_keyword() -> None:
    raw = "TypeError: WizsearchSearchTool._arun() got an unexpected keyword argument 'limit'"
    assert simplify_tool_error(raw) == "unexpected argument 'limit'"


def test_simplify_missing_argument() -> None:
    raw = "missing 1 required positional argument: 'query'"
    assert simplify_tool_error(raw) == "missing argument 'query'"
    raw2 = "TypeError: missing 2 required positional arguments: 'a' and 'b'"
    assert simplify_tool_error(raw2) == "missing argument 'a' and 'b'"


def test_simplify_strips_exception_prefix_and_method() -> None:
    assert simplify_tool_error("ValueError: bad path") == "bad path"
    assert simplify_tool_error("Foo.bar() exploded") == "exploded"
    assert simplify_tool_error("   ") == ""


def test_format_cli_error_one_line() -> None:
    exc = RuntimeError("provider unreachable\nmore detail")
    assert format_cli_error(exc) == "error: RuntimeError: provider unreachable"


def test_format_cli_error_empty_and_prefixed() -> None:
    assert format_cli_error(RuntimeError("")) == "error: RuntimeError"
    assert format_cli_error(ValueError("ValueError: already prefixed")) == (
        "error: ValueError: already prefixed"
    )
    assert format_cli_error(RuntimeError("RuntimeError something")) == (
        "error: RuntimeError something"
    )


def test_write_cli_error_verbose_includes_traceback() -> None:
    err = StringIO()

    def boom() -> None:
        raise ValueError("bad")

    try:
        boom()
    except ValueError as exc:
        write_cli_error(exc, verbose=False, err=err)
        quiet = err.getvalue()
        err.seek(0)
        err.truncate()
        write_cli_error(exc, verbose=True, err=err)
        noisy = err.getvalue()

    assert quiet == "error: ValueError: bad\n"
    assert "Traceback" in noisy
    assert "ValueError: bad" in noisy
