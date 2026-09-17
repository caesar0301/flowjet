"""Tests for streamed tool-call arg accumulation."""

from __future__ import annotations

from types import SimpleNamespace

from flowjet.cli.tool_stream import (
    ToolCallArgAccumulator,
    _as_chunk_dict,
    _merge_args,
    parse_partial_args,
)


def test_parse_partial_args_complete() -> None:
    assert parse_partial_args('{"file_path": "/tmp/a.py"}') == {"file_path": "/tmp/a.py"}


def test_parse_partial_args_incomplete() -> None:
    partial = '{"file_path": "/Users/chenxm/Workspace/fj-ai/Makefile'
    parsed = parse_partial_args(partial)
    assert parsed["file_path"].startswith("/Users/chenxm/Workspace/fj-ai/Makefile")


def test_parse_partial_args_numbers_and_bools() -> None:
    partial = '{"count": 3, "ratio": 1.5, "ok": true, "missing": null'
    parsed = parse_partial_args(partial)
    assert parsed["count"] == 3
    assert parsed["ratio"] == 1.5
    assert parsed["ok"] is True
    assert parsed["missing"] is None


def test_parse_partial_args_empty() -> None:
    assert parse_partial_args("") == {}
    assert parse_partial_args("[]") == {}


def test_as_chunk_dict_variants() -> None:
    assert _as_chunk_dict({"name": "x", "id": "1"}) == {"name": "x", "id": "1"}
    assert _as_chunk_dict(SimpleNamespace(name="n", args="{}", id="i", index=0)) == {
        "name": "n",
        "args": "{}",
        "id": "i",
        "index": 0,
    }
    assert _as_chunk_dict(SimpleNamespace()) is None

    class Dumpable:
        def model_dump(self) -> dict[str, str]:
            return {"name": "dumped", "id": "x"}

    assert _as_chunk_dict(Dumpable()) == {"name": "dumped", "id": "x"}


def test_merge_args_prefers_longer_strings() -> None:
    assert _merge_args({"a": "hi"}, {"a": "hello"}) == {"a": "hello"}
    assert _merge_args({"a": "hello"}, {"a": "hi"}) == {"a": "hello"}
    assert _merge_args({"n": 1}, {"n": 2}) == {"n": 2}


def test_accumulator_streams_chunked_json() -> None:
    acc = ToolCallArgAccumulator()
    chunks = [
        SimpleNamespace(
            tool_calls=[{"name": "read_file", "args": {}, "id": "call_1", "type": "tool_call"}],
            tool_call_chunks=[
                {
                    "name": "read_file",
                    "args": "",
                    "id": "call_1",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        ),
        SimpleNamespace(
            tool_calls=[
                {"name": "", "args": {"file_path": "/Users/"}, "id": "", "type": "tool_call"}
            ],
            tool_call_chunks=[
                {
                    "name": None,
                    "args": '{"file_path": "/Users/',
                    "id": "",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        ),
        SimpleNamespace(
            tool_calls=[],
            tool_call_chunks=[
                {
                    "name": None,
                    "args": 'chenxm/Workspace/fj-ai/Makefile"}',
                    "id": "",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        ),
    ]

    seen_paths: list[str] = []
    for msg in chunks:
        for _tc_id, name, args in acc.ingest_message(msg):
            assert name == "read_file"
            if "file_path" in args:
                seen_paths.append(args["file_path"])

    assert any("Makefile" in p for p in seen_paths)
    name, args = acc.args_for("call_1")
    assert name == "read_file"
    assert args.get("file_path", "").endswith("Makefile")
    assert acc.args_complete("call_1") is True


def test_args_complete_false_while_json_partial() -> None:
    acc = ToolCallArgAccumulator()
    msg = SimpleNamespace(
        tool_calls=[],
        tool_call_chunks=[
            {
                "name": "ls",
                "args": '{"path": "/Users/',
                "id": "c1",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
    )
    acc.ingest_message(msg)
    assert acc.args_complete("c1") is False
    msg2 = SimpleNamespace(
        tool_calls=[],
        tool_call_chunks=[
            {
                "name": None,
                "args": 'chenxm"}',
                "id": "",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
    )
    acc.ingest_message(msg2)
    assert acc.args_complete("c1") is True
    assert acc.args_complete(None) is False
    assert acc.args_complete("missing") is False


def test_accumulator_pop_and_args_for_missing() -> None:
    acc = ToolCallArgAccumulator()
    assert acc.args_for(None) == (None, {})
    assert acc.args_for("missing") == (None, {})
    assert acc.pop(None) == (None, {})
    assert acc.pop("missing") == (None, {})


def test_accumulator_fallback_path() -> None:
    acc = ToolCallArgAccumulator()
    updated: set[str] = set()
    chunks = [
        {
            "name": "grep",
            "args": '{"pattern": "x"',
            "id": "c1",
            "index": 0,
        },
        {
            "name": None,
            "args": '"}',
            "id": "",
            "index": 0,
        },
    ]
    acc._ingest_chunks_fallback(chunks, updated)
    assert "c1" in updated
    name, args = acc.args_for("c1")
    assert name == "grep"
    assert args.get("pattern") == "x"
    popped_name, popped_args = acc.pop("c1")
    assert popped_name == "grep"
    assert popped_args.get("pattern") == "x"
