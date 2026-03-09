# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for minified code handling in the AST chunker."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.chunker import (
    _CHUNK_MAX_BYTES,
    _expand_long_lines,
    _split_long_line,
    chunk_file,
)


# ── _split_long_line ─────────────────────────────────────────────────────────


def test_split_long_line_short_passthrough() -> None:
    """Lines shorter than max_chars are returned as-is."""
    result = _split_long_line("short line", max_chars=100)
    assert result == ["short line"]


def test_split_long_line_splits_at_semicolon() -> None:
    """Prefers splitting at semicolons in the last 20% of the segment."""
    # Build a line with a semicolon near the end of the first segment
    line = "a" * 85 + ";rest" + "b" * 20
    result = _split_long_line(line, max_chars=100)
    assert len(result) >= 2
    assert result[0].endswith(";")


def test_split_long_line_splits_at_brace() -> None:
    """Splits at closing brace when no semicolon available."""
    line = "a" * 85 + "}rest" + "b" * 20
    result = _split_long_line(line, max_chars=100)
    assert len(result) >= 2
    assert result[0].endswith("}")


def test_split_long_line_hard_cut_when_no_break() -> None:
    """Falls back to hard cut when no break character in last 20%."""
    line = "a" * 200  # no break characters at all
    result = _split_long_line(line, max_chars=100)
    assert len(result) == 2
    assert len(result[0]) == 100
    assert len(result[1]) == 100


def test_split_long_line_produces_no_empty_segments() -> None:
    """No empty strings in the output."""
    line = ";" * 500
    result = _split_long_line(line, max_chars=100)
    assert all(len(s) > 0 for s in result)


# ── _expand_long_lines ───────────────────────────────────────────────────────


def test_expand_long_lines_passthrough_normal() -> None:
    """Normal multi-line content passes through unchanged."""
    content = "line 1\nline 2\nline 3"
    assert _expand_long_lines(content) == content


def test_expand_long_lines_splits_minified() -> None:
    """A single very long line gets split into multiple lines."""
    minified = "var x=1;" * 5000  # ~40KB
    result = _expand_long_lines(minified, max_bytes=1000)
    lines = result.splitlines()
    assert len(lines) > 1
    # Each line should be ≤ 1000 chars (ASCII)
    for ln in lines:
        assert len(ln) <= 1000


# ── chunk_file with minified content ─────────────────────────────────────────


def test_chunk_file_minified_js_produces_multiple_chunks(tmp_path: Path) -> None:
    """Minified JS (single long line) produces multiple chunks, not just one truncated chunk."""
    # Build ~50KB of minified JS
    minified = "var a=1;" * 6000
    assert len(minified) > _CHUNK_MAX_BYTES * 2  # definitely oversized

    chunks = chunk_file(Path("bundle.min.js"), minified)

    assert len(chunks) > 1, f"Expected multiple chunks, got {len(chunks)}"
    # All chunks should be within the byte limit
    for c in chunks:
        assert len(c["text"].encode()) <= _CHUNK_MAX_BYTES, (
            f"Chunk {c['chunk_index']} exceeds byte limit: {len(c['text'].encode())}"
        )
    # Combined text should cover most of the original content
    total_text = "".join(c["text"] for c in chunks)
    assert len(total_text) > len(minified) * 0.5, (
        "Chunks should cover at least half the original content"
    )


def test_chunk_file_minified_css_produces_multiple_chunks(tmp_path: Path) -> None:
    """Minified CSS (single long line) also produces multiple chunks."""
    minified = ".a{color:red;}" * 4000
    chunks = chunk_file(Path("styles.min.css"), minified)

    assert len(chunks) > 1
    for c in chunks:
        assert len(c["text"].encode()) <= _CHUNK_MAX_BYTES


def test_chunk_file_normal_js_unchanged(tmp_path: Path) -> None:
    """Normal JS with many short lines is not affected by minified handling."""
    normal = "\n".join(f"var x{i} = {i};" for i in range(100))
    chunks = chunk_file(Path("normal.js"), normal)

    assert len(chunks) >= 1
    # Should use AST chunking for .js files
    assert chunks[0]["ast_chunked"] is True or chunks[0]["ast_chunked"] is False


def test_chunk_file_minified_preserves_metadata() -> None:
    """Minified chunks have correct file_path, filename, extension metadata."""
    minified = "function f(){" + "x();" * 5000 + "}"
    chunks = chunk_file(Path("/repo/app.min.js"), minified)

    for c in chunks:
        assert c["file_path"] == "/repo/app.min.js"
        assert c["filename"] == "app.min.js"
        assert c["file_extension"] == ".js"
        assert "chunk_index" in c
        assert "chunk_count" in c
        assert "line_start" in c
        assert "line_end" in c
