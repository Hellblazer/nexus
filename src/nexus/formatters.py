# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output formatters: vimgrep, JSON, plain text, plain text with context."""
from __future__ import annotations

import json
from typing import Any

from nexus.types import SearchResult


def format_vimgrep(results: list[SearchResult]) -> list[str]:
    """Format results as ``path:line:0:content`` for editor integration."""
    lines: list[str] = []
    for r in results:
        source_path = r.metadata.get("source_path", "")
        line_start = r.metadata.get("line_start", 0)
        first_line = r.content.splitlines()[0] if r.content else ""
        lines.append(f"{source_path}:{line_start}:0:{first_line}")
    return lines


def format_json(results: list[SearchResult]) -> str:
    """Format results as a JSON array with id, content, distance, collection, and metadata.

    Metadata fields are spread into the top-level object first, then the canonical
    fields (id, content, distance, collection) are written last so they always win
    over any metadata keys with the same name.
    """
    items: list[dict[str, Any]] = []
    for r in results:
        item: dict[str, Any] = {
            **r.metadata,
            "id": r.id,
            "content": r.content,
            "distance": r.distance,
            "collection": r.collection,
        }
        items.append(item)
    return json.dumps(items, indent=2, default=str)


def format_plain(results: list[SearchResult]) -> list[str]:
    """Default plain-text format: ./path/to/file.py:42:    content."""
    lines: list[str] = []
    for r in results:
        source_path = r.metadata.get("source_path", "")
        line_start = r.metadata.get("line_start", 0)
        for i, content_line in enumerate(r.content.splitlines()):
            line_no = int(line_start) + i
            lines.append(f"{source_path}:{line_no}:{content_line}")
    return lines


def format_plain_with_context(
    results: list[SearchResult],
    lines_after: int = 0,
) -> list[str]:
    """Plain-text format with optional context-line windowing.

    Shows the first line of each chunk, then at most *lines_after* additional
    lines from the chunk.  When *lines_after* is 0, produces identical output
    to :func:`format_plain`.
    """
    if lines_after == 0:
        return format_plain(results)

    output: list[str] = []
    for r in results:
        source_path = r.metadata.get("source_path", "")
        line_start = r.metadata.get("line_start", 0)
        chunk_lines = r.content.splitlines()
        total = len(chunk_lines)
        end_idx = min(total, 1 + lines_after)

        for i, content_line in enumerate(chunk_lines[:end_idx]):
            line_no = int(line_start) + i
            output.append(f"{source_path}:{line_no}:{content_line}")
    return output
