# SPDX-License-Identifier: AGPL-3.0-or-later
"""AST-based code chunking with line-based fallback."""
from pathlib import Path
from typing import Any

import structlog

from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES

_log = structlog.get_logger()

from nexus.languages import LANGUAGE_REGISTRY

_CHUNK_LINES = 150
_OVERLAP = 0.15
# Alias for backward-compat with existing tests that import _CHUNK_MAX_BYTES.
_CHUNK_MAX_BYTES = SAFE_CHUNK_BYTES
# Lines longer than this threshold (bytes) are split at natural break points
# before line-based chunking. Prevents minified JS/CSS from producing a single
# oversized chunk that gets truncated.
_LONG_LINE_THRESHOLD = SAFE_CHUNK_BYTES
_BREAK_CHARS = (";", ",", "}", ")", "]")


def _make_code_splitter(language: str, content: str, chunk_lines: int = _CHUNK_LINES) -> list:
    """Chunk *content* via CodeSplitter for *language*; returns list of nodes.

    All llama-index imports live here so tests can patch this single function.
    Uses tree-sitter-language-pack to obtain the parser, passing it explicitly
    to CodeSplitter (which otherwise tries the deprecated tree_sitter_languages).
    """
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*validate_default.*")
        from llama_index.core import Document  # type: ignore[import]
        from llama_index.core.node_parser import CodeSplitter  # type: ignore[import]
    from tree_sitter_language_pack import get_parser  # type: ignore[import]

    # tree-sitter-language-pack uses "csharp" not "c_sharp".
    parser_name = "csharp" if language == "c_sharp" else language
    parser = get_parser(parser_name)
    splitter = CodeSplitter(
        language=language,
        parser=parser,
        chunk_lines=chunk_lines,
        chunk_lines_overlap=int(chunk_lines * _OVERLAP),
    )
    doc = Document(text=content)
    return splitter.get_nodes_from_documents([doc])


def _split_long_line(line: str, max_chars: int) -> list[str]:
    """Split a very long line into smaller segments at natural break points.

    Used for minified code where a single line can be the entire file.
    Prefers splitting at semicolons, commas, and closing brackets within
    the last 20% of each segment.  Falls back to a hard cut at *max_chars*
    when no natural break point is found.
    """
    if len(line) <= max_chars:
        return [line]

    segments: list[str] = []
    pos = 0
    while pos < len(line):
        end = min(pos + max_chars, len(line))
        if end < len(line):
            # Search for a natural break in the last 20% of the segment
            search_start = pos + int(max_chars * 0.8)
            best_break = -1
            for ch in _BREAK_CHARS:
                bp = line.rfind(ch, search_start, end)
                if bp > best_break:
                    best_break = bp
            if best_break > search_start:
                end = best_break + 1  # include the break character
        segments.append(line[pos:end])
        pos = end
    return segments


def _expand_long_lines(content: str, max_bytes: int = _LONG_LINE_THRESHOLD) -> str:
    """Pre-process content by splitting lines that exceed *max_bytes*.

    Returns the content with long lines broken into shorter segments,
    each ending at a natural code delimiter when possible.  This ensures
    the downstream line-based chunker has reasonable-length lines to work with.
    """
    lines = content.splitlines()
    has_long = any(len(ln.encode()) > max_bytes for ln in lines)
    if not has_long:
        return content
    # Conservative char estimate: UTF-8 chars are ≤4 bytes, but code is mostly ASCII.
    max_chars = max_bytes  # 1:1 for ASCII; will overshoot for multi-byte but that's safe
    expanded: list[str] = []
    for ln in lines:
        if len(ln.encode()) > max_bytes:
            expanded.extend(_split_long_line(ln, max_chars))
        else:
            expanded.append(ln)
    return "\n".join(expanded)


def _line_chunk(
    content: str,
    chunk_lines: int = _CHUNK_LINES,
    overlap: float = _OVERLAP,
    max_bytes: int = _CHUNK_MAX_BYTES,
) -> list[tuple[int, int, str]]:
    """Split *content* into overlapping line-based chunks.

    Each chunk contains at most *chunk_lines* lines and at most *max_bytes*
    bytes (UTF-8 encoded).  When a window of *chunk_lines* would exceed
    *max_bytes*, the window is shrunk via binary search until it fits.
    A single line that is itself larger than *max_bytes* is emitted as-is
    (the byte limit cannot be honoured without splitting the line).

    The step between chunk starts is derived from the *actual* chunk size
    written, preserving the overlap ratio regardless of byte-capping.

    Returns list of (line_start, line_end, text) tuples (1-indexed).
    """
    # Pre-split long lines (minified code) so the line-based chunker
    # has reasonable-length lines to work with.
    content = _expand_long_lines(content, max_bytes=max_bytes)
    lines = content.splitlines()
    n = len(lines)
    if n == 0:
        return []

    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < n:
        end = min(start + chunk_lines, n)
        chunk_text = "\n".join(lines[start:end])

        # Byte cap: binary-search for the largest window that fits.
        if len(chunk_text.encode()) > max_bytes:
            lo, hi = 1, end - start
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len("\n".join(lines[start : start + mid]).encode()) <= max_bytes:
                    lo = mid
                else:
                    hi = mid - 1
            # Emit at least 1 line; if that single line still exceeds max_bytes,
            # truncate at a UTF-8 boundary rather than emitting an oversized chunk.
            end = start + max(1, lo)
            chunk_text = "\n".join(lines[start:end])
            if len(chunk_text.encode()) > max_bytes:
                chunk_text = chunk_text.encode()[:max_bytes].decode("utf-8", errors="ignore")

        chunks.append((start + 1, end, chunk_text))  # 1-indexed
        if end == n:
            break
        # Step is relative to actual chunk size to preserve overlap ratio.
        actual_lines = end - start
        start += max(1, int(actual_lines * (1 - overlap)))
    return chunks


def _enforce_byte_cap(
    chunks: list[dict[str, Any]],
    max_bytes: int = _CHUNK_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Post-process a chunk list and split any entry that exceeds *max_bytes*.

    Used for AST-produced chunks where CodeSplitter may emit a single node
    (e.g. a 400-line function body) that is larger than the storage limit.
    Renumbers chunk_index and chunk_count across the returned list.
    """
    result: list[dict[str, Any]] = []
    for chunk in chunks:
        text: str = chunk["text"]
        if len(text.encode()) <= max_bytes:
            result.append(chunk)
            continue

        # Oversized node: expand long lines (minified code), then re-split
        # line-by-line with binary-search byte cap.
        text = _expand_long_lines(text, max_bytes=max_bytes)
        base_ls: int = chunk.get("line_start", 1)
        lines = text.splitlines()
        pos = 0
        while pos < len(lines):
            lo, hi = 1, len(lines) - pos
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len("\n".join(lines[pos : pos + mid]).encode()) <= max_bytes:
                    lo = mid
                else:
                    hi = mid - 1
            take = max(1, lo)
            sub_text = "\n".join(lines[pos : pos + take])
            if len(sub_text.encode()) > max_bytes:
                sub_text = sub_text.encode()[:max_bytes].decode("utf-8", errors="ignore")
            result.append({
                **chunk,
                "text": sub_text,
                "line_start": base_ls + pos,
                "line_end": base_ls + pos + take - 1,
            })
            pos += take

    # Renumber indices across the (possibly expanded) list.
    total = len(result)
    for i, c in enumerate(result):
        c["chunk_index"] = i
        c["chunk_count"] = total
    return result


def chunk_file(file: Path, content: str, chunk_lines: int | None = None) -> list[dict[str, Any]]:
    """Chunk *file* content; use AST splitter for known extensions, else lines.

    Each returned dict contains:
        file_path, filename, file_extension, ast_chunked,
        chunk_index, chunk_count, line_start, line_end, text

    *chunk_lines* overrides the module default (_CHUNK_LINES = 150) when set.
    """
    effective_chunk_lines = chunk_lines if chunk_lines is not None else _CHUNK_LINES
    ext = file.suffix.lower()
    language = LANGUAGE_REGISTRY.get(ext)

    # ``filename`` / ``file_extension`` / ``ast_chunked`` were dropped
    # in nexus-59j0: they're not in metadata_schema.ALLOWED_TOP_LEVEL,
    # so the indexer factory ignores them. ``file_path`` stays as the
    # only chunker-level identifier the code_indexer reads (it's
    # promoted to ``source_path`` at factory time).
    base_meta = {
        "file_path": str(file),
    }

    if language:
        try:
            nodes = _make_code_splitter(language, content, chunk_lines=effective_chunk_lines)
            if nodes:
                count = len(nodes)
                result = []
                for i, node in enumerate(nodes):
                    meta = {**base_meta, **node.metadata, "chunk_index": i, "chunk_count": count}
                    # CodeSplitter.get_nodes_from_documents() always returns metadata={} —
                    # no line_start/line_end. Derive per-chunk line numbers from the
                    # TextNode character offsets (start_char_idx is populated and accurate).
                    if node.start_char_idx is not None:
                        line_start = content[:node.start_char_idx].count("\n") + 1
                    else:
                        line_start = 1  # defensive fallback; None not observed empirically
                    # max() guard: str.splitlines() returns [] for empty text, giving -1 delta
                    line_end = max(line_start, line_start + len(node.text.splitlines()) - 1)
                    meta["line_start"] = line_start
                    meta["line_end"] = line_end
                    meta["text"] = node.text
                    result.append(meta)
                # Post-process: split any AST node that exceeds the byte cap
                # (e.g. a single function body longer than _CHUNK_MAX_BYTES).
                return _enforce_byte_cap(result)
        except Exception:
            _log.debug("AST chunking failed, falling back to line chunks", file=str(file), exc_info=True)

    # Line-based fallback
    raw_chunks = _line_chunk(content, chunk_lines=effective_chunk_lines)
    if not raw_chunks:
        if not content.strip():
            return []
        raw_chunks = [(1, 1, content)]
    count = len(raw_chunks)
    result = []
    for i, (ls, le, text) in enumerate(raw_chunks):
        result.append(
            {
                **base_meta,
                "chunk_index": i,
                "chunk_count": count,
                "line_start": ls,
                "line_end": le,
                "text": text,
            }
        )
    return result
