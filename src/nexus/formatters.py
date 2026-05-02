# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output formatters: vimgrep, JSON, plain text, plain text with context."""
from __future__ import annotations

import json
import re
import subprocess
from functools import cache
from typing import Any

import structlog

from nexus.types import SearchResult

_log = structlog.get_logger()


def _display_path(meta: dict, default: str = "") -> str:
    """nexus-1qed: return the best display path for a SearchResult's metadata.

    Priority order:

    1. ``_display_path`` (catalog-resolved path attached by
       ``search_engine._attach_display_paths`` when a catalog is in scope).
    2. ``source_path`` (legacy chunk metadata; survives until the prune
       verb in nexus-o6aa.10.3 lands).
    3. ``file_path`` (older chunk shape, used by some MCP-promoted entries).
    4. *default* (caller-provided fallback string).

    Formatters use this in place of direct ``meta.get("source_path", ...)``
    reads so a single fallback chain serves catalog-resolved + legacy chunks.
    """
    return (
        meta.get("_display_path")
        or meta.get("source_path")
        or meta.get("file_path")
        or default
    )


def _find_matching_lines(
    chunk_text: str,
    query: str,
    rg_matched_lines: list[int] | None = None,
    chunk_line_start: int = 0,
) -> list[int]:
    """Return 0-based line indices within *chunk_text* that best match *query*.

    Priority order:
    1. *rg_matched_lines* (absolute line numbers from ripgrep) translated to
       chunk-relative indices.
    2. Keyword match: split *query* on ``\\W+``, case-insensitive substring
       match — a line matches if any token appears.
    3. Fallback to ``[0]`` (first line of chunk).
    """
    lines = chunk_text.splitlines()
    n = len(lines)
    if n == 0:
        return [0]

    # 1. rg_matched_lines: translate absolute → chunk-relative
    if rg_matched_lines:
        relative = []
        for abs_ln in rg_matched_lines:
            idx = abs_ln - chunk_line_start
            if 0 <= idx < n:
                relative.append(idx)
        if relative:
            return sorted(set(relative))

    # 2. Keyword match
    tokens = [t.lower() for t in re.split(r"\W+", query) if t]
    if tokens:
        matches = []
        for i, line in enumerate(lines):
            lower = line.lower()
            if any(tok in lower for tok in tokens):
                matches.append(i)
        if matches:
            return matches

    # 3. Fallback
    return [0]


def _extract_context(
    lines: list[str],
    matches: list[int],
    before: int = 0,
    after: int = 0,
) -> list[tuple[int, str, str]]:
    """Extract context-windowed line blocks around *matches*.

    Returns list of ``(line_index, line_type, text)`` tuples where
    *line_type* is one of ``"match"``, ``"context"``, or ``"bridge"``.

    Adjacent or overlapping blocks (gap <= 2 lines) are merged with
    bridge lines filling the gap.
    """
    n = len(lines)
    if not matches or n == 0:
        return []

    # Build raw blocks: each match expands to [match - before, match + after]
    blocks: list[tuple[int, int]] = []
    for m in matches:
        start = max(0, m - before)
        end = min(n - 1, m + after)
        blocks.append((start, end))

    # Merge overlapping / adjacent blocks (gap <= 2 → bridge)
    merged: list[tuple[int, int]] = [blocks[0]]
    for start, end in blocks[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 3:  # gap of <=2 lines between blocks
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    # Build output with line types
    match_set = set(matches)
    result: list[tuple[int, str, str]] = []
    for block_start, block_end in merged:
        for i in range(block_start, block_end + 1):
            if i in match_set:
                result.append((i, "match", lines[i]))
            elif any(block_start <= m <= block_end and abs(i - m) <= max(before, after) for m in match_set):
                result.append((i, "context", lines[i]))
            else:
                result.append((i, "bridge", lines[i]))

    return result


@cache
def _is_bat_installed() -> bool:
    """Check if ``bat`` is available on PATH. Result is cached for the session."""
    try:
        subprocess.run(
            ["bat", "--version"],
            capture_output=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and merge overlapping or adjacent line ranges.

    ``[(1, 5), (3, 8)]`` → ``[(1, 8)]``
    ``[(1, 5), (6, 10)]`` → ``[(1, 10)]``
    ``[(1, 5), (8, 10)]`` → ``[(1, 5), (8, 10)]``
    """
    if not ranges:
        return []
    sorted_ranges = sorted(ranges)
    merged = [sorted_ranges[0]]
    for start, end in sorted_ranges[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _format_with_bat(
    results: list[SearchResult],
    context_blocks: dict[str, list[tuple[int, int]]] | None = None,
) -> str:
    """Format results with ``bat`` syntax highlighting.

    Groups results by ``source_path``, merges overlapping line ranges,
    calls ``bat`` once per file.  Falls back to plain formatting per file
    on any subprocess error.
    """
    from collections import defaultdict

    # Group results by display path (nexus-1qed: prefer catalog-resolved
    # _display_path, fall back to source_path / file_path for legacy chunks).
    groups: dict[str, list[SearchResult]] = defaultdict(list)
    for r in results:
        src = _display_path(r.metadata, default="unknown")
        groups[src].append(r)

    output_parts: list[str] = []

    for src_path, file_results in groups.items():
        # Determine line ranges
        if context_blocks and src_path in context_blocks:
            ranges = context_blocks[src_path]
        else:
            ranges = []
            for r in file_results:
                ls = int(r.metadata.get("line_start", 1))
                le = int(r.metadata.get("line_end", 0))
                if le < ls:
                    le = ls + max(0, len(r.content.splitlines()) - 1)
                ranges.append((ls, le))

        merged = _merge_line_ranges(ranges)
        if not merged:
            continue

        # Build bat command
        cmd = [
            "bat",
            "--file-name", src_path,
            "--paging", "never",
            "--style=plain",
        ]
        for start, end in merged:
            cmd.extend(["--line-range", f"{start}:{end}"])

        # Feed chunk content via stdin (source file may not exist locally)
        # Reconstruct full content from all chunks for this file
        all_content_lines: dict[int, str] = {}
        for r in file_results:
            ls = int(r.metadata.get("line_start", 1))
            for i, line in enumerate(r.content.splitlines()):
                all_content_lines[ls + i] = line

        if all_content_lines:
            min_line = min(all_content_lines)
            max_line = max(all_content_lines)
            stdin_lines = []
            for ln in range(min_line, max_line + 1):
                stdin_lines.append(all_content_lines.get(ln, ""))
            stdin_text = "\n".join(stdin_lines)
        else:
            stdin_text = "\n".join(r.content for r in file_results)

        # Use stdin mode: bat reads from stdin with --file-name for language detection
        # Replace the file arg with "-" for stdin
        cmd.append("-")

        try:
            proc = subprocess.run(
                cmd,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0 and proc.stdout:
                output_parts.append(proc.stdout.rstrip("\n"))
            else:
                # bat failed silently — fall back to plain
                _log.debug("bat returned non-zero", returncode=proc.returncode, file=src_path)
                output_parts.append(_plain_fallback(file_results))
        except (FileNotFoundError, subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            _log.debug("bat invocation failed, falling back to plain", file=src_path, exc=str(exc))
            output_parts.append(_plain_fallback(file_results))

    return "\n".join(output_parts)


def _plain_fallback(results: list[SearchResult]) -> str:
    """Format results as plain text (fallback when bat fails)."""
    lines = format_plain(results)
    return "\n".join(lines)


def format_compact(
    results: list[SearchResult],
    query: str | None = None,
) -> list[str]:
    """One line per result: ``source_path:line_no:best_matching_line``.

    When *query* is provided, reports the best-matching line within the chunk.
    Otherwise uses the first line at ``line_start``.
    """
    output: list[str] = []
    for r in results:
        source_path = _display_path(r.metadata)
        line_start = int(r.metadata.get("line_start", 0))
        chunk_lines = r.content.splitlines()
        if not chunk_lines:
            output.append(f"{source_path}:{line_start}:")
            continue

        if query:
            matches = _find_matching_lines(
                r.content, query,
                rg_matched_lines=r.metadata.get("rg_matched_lines"),
                chunk_line_start=line_start,
            )
            best_idx = matches[0]
        else:
            best_idx = 0

        best_idx = min(best_idx, len(chunk_lines) - 1)
        line_no = line_start + best_idx
        output.append(f"{source_path}:{line_no}:{chunk_lines[best_idx]}")
    return output


def format_vimgrep(results: list[SearchResult], query: str | None = None) -> list[str]:
    """Format results as ``path:line:0:content`` for editor integration.

    When *query* is provided, reports the best-matching line within the chunk
    rather than always using the first line at ``line_start``.
    """
    lines: list[str] = []
    for r in results:
        source_path = _display_path(r.metadata)
        line_start = int(r.metadata.get("line_start", 0))
        chunk_lines = r.content.splitlines() if r.content else [""]

        if query and chunk_lines:
            matches = _find_matching_lines(
                r.content, query,
                rg_matched_lines=r.metadata.get("rg_matched_lines"),
                chunk_line_start=line_start,
            )
            best_idx = min(matches[0], len(chunk_lines) - 1)
            line_no = line_start + best_idx
            text = chunk_lines[best_idx]
        else:
            line_no = line_start
            text = chunk_lines[0]

        lines.append(f"{source_path}:{line_no}:0:{text}")
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
    """Default plain-text format: ./path/to/file.py:42:    content.

    For results without a ``source_path`` (knowledge/docs entries from
    ``store put``), falls back to the doc-style ``[distance] title\\n  snippet``
    format that the MCP surface uses.
    """
    lines: list[str] = []
    for r in results:
        source_path = _display_path(r.metadata)
        if not source_path:
            title = r.metadata.get("title") or r.id
            snippet = r.content.splitlines()[0] if r.content else ""
            lines.append(f"[{r.distance:.4f}] {title}")
            if snippet:
                lines.append(f"  {snippet}")
            continue
        line_start = r.metadata.get("line_start", 0)
        for i, content_line in enumerate(r.content.splitlines()):
            line_no = int(line_start) + i
            lines.append(f"{source_path}:{line_no}:{content_line}")
    return lines


def format_plain_with_context(
    results: list[SearchResult],
    lines_after: int = 0,
    lines_before: int = 0,
    query: str | None = None,
) -> list[str]:
    """Plain-text format with optional context-line windowing.

    When *query* is provided and context flags are active, windows are
    centered on matching lines (keyword or rg_matched_lines) rather than
    the chunk start.  When *query* is ``None``, falls back to showing the
    first N lines from the chunk start (current behavior).

    When both *lines_after* and *lines_before* are 0, delegates to
    :func:`format_plain` for identical output.
    """
    if lines_after == 0 and lines_before == 0:
        return format_plain(results)

    output: list[str] = []
    for r in results:
        source_path = _display_path(r.metadata)
        line_start = int(r.metadata.get("line_start", 0))
        chunk_lines = r.content.splitlines()
        total = len(chunk_lines)

        if query and (lines_before > 0 or lines_after > 0):
            # Smart windowing: center on matching lines
            matches = _find_matching_lines(
                r.content, query,
                rg_matched_lines=r.metadata.get("rg_matched_lines"),
                chunk_line_start=line_start,
            )
            context = _extract_context(chunk_lines, matches, lines_before, lines_after)
            for idx, _line_type, text in context:
                line_no = line_start + idx
                output.append(f"{source_path}:{line_no}:{text}")
        else:
            # Legacy behavior: first N lines from chunk start
            end_idx = min(total, 1 + lines_after)
            for i, content_line in enumerate(chunk_lines[:end_idx]):
                line_no = line_start + i
                output.append(f"{source_path}:{line_no}:{content_line}")
    return output
