"""Accumulate streamed tool-call argument chunks for progress display."""

from __future__ import annotations

import json
import re
from typing import Any


def _as_chunk_dict(chunk: Any) -> dict[str, Any] | None:
    if isinstance(chunk, dict):
        return chunk
    model_dump = getattr(chunk, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            return None
    name = getattr(chunk, "name", None)
    args = getattr(chunk, "args", None)
    tc_id = getattr(chunk, "id", None)
    index = getattr(chunk, "index", None)
    if name is None and args is None and tc_id is None:
        return None
    return {"name": name, "args": args, "id": tc_id, "index": index}


def parse_partial_args(args_str: str) -> dict[str, Any]:
    """Parse complete or incomplete tool-arg JSON into a dict for display."""
    text = (args_str or "").strip()
    if not text:
        return {}
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    # Recover key/value pairs from a still-open JSON object stream.
    out: dict[str, Any] = {}
    for match in re.finditer(r'"([^"\\]+)"\s*:\s*"((?:[^"\\]|\\.)*)(?:"|$)', text):
        key, raw_val = match.group(1), match.group(2)
        try:
            out[key] = json.loads(f'"{raw_val}"')
        except (TypeError, ValueError, json.JSONDecodeError):
            out[key] = raw_val.replace('\\"', '"').replace("\\\\", "\\")
    for match in re.finditer(r'"([^"\\]+)"\s*:\s*(-?\d+(?:\.\d+)?)', text):
        key = match.group(1)
        if key in out:
            continue
        num = match.group(2)
        out[key] = float(num) if "." in num else int(num)
    for match in re.finditer(r'"([^"\\]+)"\s*:\s*(true|false|null)', text):
        key = match.group(1)
        if key in out:
            continue
        token = match.group(2)
        out[key] = {"true": True, "false": False, "null": None}[token]
    return out


def _merge_args(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge arg dicts, keeping the longer string value for each key."""
    merged = dict(base)
    for key, value in overlay.items():
        if key not in merged or merged[key] in (None, ""):
            merged[key] = value
            continue
        cur = merged[key]
        if isinstance(value, str) and isinstance(cur, str) and len(value) > len(cur):
            merged[key] = value
        elif not isinstance(cur, str) and value not in (None, ""):
            merged[key] = value
    return merged


class ToolCallArgAccumulator:
    """Track tool names + streamed arg JSON across ``AIMessageChunk`` events."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._last_active_id = ""
        self._by_index: dict[int, str] = {}
        self._overlays: dict[str, dict[str, Any]] = {}

    def _ensure(self, tc_id: str) -> dict[str, Any]:
        return self._pending.setdefault(
            tc_id,
            {
                "name": "",
                "args_str": "",
                "is_complete_json": False,
                "emitted": False,
                "is_main": True,
            },
        )

    def _resolved_args(self, tc_id: str) -> dict[str, Any]:
        state = self._pending.get(tc_id) or {}
        parsed = parse_partial_args(str(state.get("args_str") or ""))
        return _merge_args(parsed, self._overlays.get(tc_id) or {})

    def ingest_message(self, message: Any) -> list[tuple[str, str, dict[str, Any]]]:
        """Ingest one AI message/chunk; return updated ``(id, name, args)`` entries."""
        updated_ids: set[str] = set()

        raw_chunks = getattr(message, "tool_call_chunks", None) or []
        chunks: list[dict[str, Any]] = []
        for raw in raw_chunks:
            as_dict = _as_chunk_dict(raw)
            if as_dict is not None:
                chunks.append(as_dict)

        if chunks:
            try:
                from soothe_sdk.display.message_processing import accumulate_tool_call_chunks

                before = {k: (v.get("name"), v.get("args_str")) for k, v in self._pending.items()}
                self._last_active_id = accumulate_tool_call_chunks(
                    self._pending,
                    chunks,
                    last_active_id=self._last_active_id,
                )
                for key, state in self._pending.items():
                    prev = before.get(key)
                    if prev is None or prev != (state.get("name"), state.get("args_str")):
                        updated_ids.add(key)
            except Exception:
                self._ingest_chunks_fallback(chunks, updated_ids)

            for chunk in chunks:
                idx = chunk.get("index")
                tc_id = str(chunk.get("id") or "").strip()
                if isinstance(idx, int) and tc_id:
                    self._by_index[idx] = tc_id

        # Register names / seed overlays from tool_calls — never clobber args_str.
        for tc in getattr(message, "tool_calls", None) or []:
            entry = _as_chunk_dict(tc) or (tc if isinstance(tc, dict) else None)
            if not entry:
                continue
            tc_id = str(entry.get("id") or "").strip() or self._last_active_id
            if not tc_id and isinstance(entry.get("index"), int):
                tc_id = self._by_index.get(entry["index"], "")
            if not tc_id:
                continue
            state = self._ensure(tc_id)
            name = entry.get("name") or ""
            if isinstance(name, str) and name.strip():
                if state.get("name") != name:
                    state["name"] = name
                    updated_ids.add(tc_id)
                self._last_active_id = tc_id
            args = entry.get("args")
            if isinstance(args, dict) and args:
                prev_overlay = self._overlays.get(tc_id) or {}
                merged_overlay = _merge_args(prev_overlay, args)
                if merged_overlay != prev_overlay:
                    self._overlays[tc_id] = merged_overlay
                    updated_ids.add(tc_id)
            elif isinstance(args, str) and args.strip() and not state.get("args_str"):
                state["args_str"] = args
                updated_ids.add(tc_id)

        out: list[tuple[str, str, dict[str, Any]]] = []
        for tc_id in updated_ids:
            state = self._pending.get(tc_id)
            if not state:
                continue
            name = str(state.get("name") or "") or "tool"
            out.append((tc_id, name, self._resolved_args(tc_id)))
        return out

    def _ingest_chunks_fallback(self, chunks: list[dict[str, Any]], updated_ids: set[str]) -> None:
        """Minimal accumulator if soothe-sdk helper is unavailable."""
        for chunk in chunks:
            tc_id = str(chunk.get("id") or "").strip()
            name = chunk.get("name")
            args = chunk.get("args", "")
            idx = chunk.get("index")
            if not tc_id and isinstance(idx, int):
                tc_id = self._by_index.get(idx, "")
            if not tc_id:
                tc_id = self._last_active_id
            if isinstance(name, str) and name.strip() and tc_id:
                state = self._ensure(tc_id)
                state["name"] = name
                self._last_active_id = tc_id
                if isinstance(idx, int):
                    self._by_index[idx] = tc_id
                updated_ids.add(tc_id)
            if not tc_id or tc_id not in self._pending:
                continue
            state = self._pending[tc_id]
            if isinstance(args, dict) and args:
                self._overlays[tc_id] = _merge_args(self._overlays.get(tc_id) or {}, args)
                updated_ids.add(tc_id)
            elif isinstance(args, str) and args:
                state["args_str"] = str(state.get("args_str") or "") + args
                updated_ids.add(tc_id)
                self._last_active_id = tc_id

    def args_complete(self, tool_call_id: str | None) -> bool:
        """True when arg JSON is finished (not a mid-stream partial object)."""
        if not tool_call_id:
            return False
        tc_id = str(tool_call_id)
        state = self._pending.get(tc_id)
        if not state:
            return False
        if state.get("is_complete_json"):
            return True
        args_str = str(state.get("args_str") or "").strip()
        if args_str:
            try:
                loaded = json.loads(args_str)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            return isinstance(loaded, dict)
        # Dict args arrived via ``tool_calls`` with no streamed JSON yet.
        return bool(self._overlays.get(tc_id))

    def args_for(self, tool_call_id: str | None) -> tuple[str | None, dict[str, Any]]:
        if not tool_call_id:
            return None, {}
        tc_id = str(tool_call_id)
        state = self._pending.get(tc_id)
        if not state:
            return None, {}
        name = str(state.get("name") or "") or None
        return name, self._resolved_args(tc_id)

    def pop(self, tool_call_id: str | None) -> tuple[str | None, dict[str, Any]]:
        if not tool_call_id:
            return None, {}
        tc_id = str(tool_call_id)
        state = self._pending.pop(tc_id, None)
        overlay = self._overlays.pop(tc_id, None) or {}
        if not state:
            return None, overlay
        name = str(state.get("name") or "") or None
        args = _merge_args(parse_partial_args(str(state.get("args_str") or "")), overlay)
        return name, args
