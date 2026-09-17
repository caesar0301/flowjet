"""Tool-arg accumulation now lives in ``flowjet.core.tool_stream``.

Kept as a re-export so the CLI renderer and its tests keep their import paths.
"""

from __future__ import annotations

from flowjet.core.tool_stream import (
    ToolCallArgAccumulator,
    _as_chunk_dict,
    _merge_args,
    parse_partial_args,
)

__all__ = [
    "ToolCallArgAccumulator",
    "_as_chunk_dict",
    "_merge_args",
    "parse_partial_args",
]
