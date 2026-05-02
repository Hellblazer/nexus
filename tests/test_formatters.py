# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import subprocess

import pytest

from nexus.formatters import (
    _display_path,
    _extract_context,
    _find_matching_lines,
    _is_bat_installed,
    _merge_line_ranges,
    format_compact,
    format_json,
    format_plain,
    format_plain_with_context,
    format_vimgrep,
)
from nexus.types import SearchResult


def _result(
    content: str = "line one\nline two\nline three",
    source_path: str = "src/foo.py",
    line_start: int = 10,
    **extra_meta: object,
) -> SearchResult:
    meta = {"source_path": source_path, "line_start": line_start, **extra_meta}
    return SearchResult(id="r1", content=content, distance=0.1, collection="code__x", metadata=meta)


# ── format_vimgrep ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs,expected", [
    ({}, ["src/foo.py:10:0:line one"]),
    ({"content": ""}, ["src/foo.py:10:0:"]),
])
def test_vimgrep_basic(kwargs, expected) -> None:
    assert format_vimgrep([_result(**kwargs)]) == expected


def test_vimgrep_missing_source_path() -> None:
    r = _result()
    r.metadata.pop("source_path")
    assert format_vimgrep([r])[0].startswith(":10:0:")


# ── nexus-1qed: _display_path priority + formatter integration ──────────────


class TestDisplayPathPriority:
    """nexus-1qed: ``_display_path`` (catalog-resolved) wins over
    ``source_path`` and ``file_path`` so chunks that no longer carry
    ``source_path`` (post-prune) still render via the catalog projection.
    """

    def test_display_path_wins_over_source_path(self) -> None:
        meta = {
            "_display_path": "/abs/from/catalog.py",
            "source_path": "src/legacy.py",
        }
        # WITH TEETH: a regression that drops the _display_path branch
        # and reads source_path directly fails this assertion.
        assert _display_path(meta) == "/abs/from/catalog.py"

    def test_falls_back_to_source_path_when_display_path_absent(self) -> None:
        assert _display_path({"source_path": "src/legacy.py"}) == "src/legacy.py"

    def test_falls_back_to_file_path_when_neither_present(self) -> None:
        assert _display_path({"file_path": "older/shape.md"}) == "older/shape.md"

    def test_default_when_no_path_keys(self) -> None:
        assert _display_path({}, default="unknown") == "unknown"


def test_formatters_use_display_path_when_attached() -> None:
    """nexus-1qed end-to-end: format_plain reads ``_display_path`` so a
    chunk without ``source_path`` (post-prune shape) renders the
    catalog-resolved path. Pre-fix code keys on source_path and renders
    the fallback "[distance] title" form instead.
    """
    r = SearchResult(
        id="r1",
        content="def foo(): pass",
        distance=0.1,
        collection="code__x",
        metadata={
            # post-prune chunk shape: no source_path, only doc_id +
            # _display_path attached by search_engine._attach_display_paths.
            "_display_path": "/abs/path/to/foo.py",
            "doc_id": "ART-deadbeef",
            "line_start": 1,
        },
    )
    out = format_plain([r])
    assert out == ["/abs/path/to/foo.py:1:def foo(): pass"]


def test_format_compact_prefers_display_path() -> None:
    r = SearchResult(
        id="r1",
        content="def foo(): pass",
        distance=0.1,
        collection="code__x",
        metadata={
            "_display_path": "/abs/from/catalog.py",
            "source_path": "stale/legacy.py",
            "line_start": 5,
        },
    )
    assert format_compact([r]) == ["/abs/from/catalog.py:5:def foo(): pass"]


def test_vimgrep_multiple_results() -> None:
    lines = format_vimgrep([_result(), _result(source_path="b.py", line_start=5, content="hello")])
    assert len(lines) == 2
    assert lines[1] == "b.py:5:0:hello"


# ── format_json ─────────────────────────────────────────────────────────────


def test_json_basic() -> None:
    parsed = json.loads(format_json([_result()]))
    assert len(parsed) == 1
    assert parsed[0]["id"] == "r1"
    assert parsed[0]["collection"] == "code__x"
    assert parsed[0]["source_path"] == "src/foo.py"


def test_json_canonical_fields_win() -> None:
    r = _result()
    r.metadata["id"] = "meta-id"
    r.metadata["collection"] = "meta-col"
    parsed = json.loads(format_json([r]))
    assert parsed[0]["id"] == "r1"
    assert parsed[0]["collection"] == "code__x"


def test_json_empty() -> None:
    assert json.loads(format_json([])) == []


# ── format_plain ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("content,expected", [
    ("line one\nline two\nline three", ["src/foo.py:10:line one", "src/foo.py:11:line two", "src/foo.py:12:line three"]),
    ("", []),
    ("only line", ["src/foo.py:10:only line"]),
])
def test_plain_variants(content, expected) -> None:
    assert format_plain([_result(content=content)]) == expected


def test_plain_missing_metadata() -> None:
    """Without source_path, fall back to MCP-style [distance] title\\n  snippet."""
    r = SearchResult(id="r1", content="hello", distance=0.1, collection="c", metadata={})
    assert format_plain([r]) == ["[0.1000] r1", "  hello"]


def test_plain_uses_title_when_present() -> None:
    """Source-path-less results prefer metadata title over the bare id."""
    r = SearchResult(
        id="abc123",
        content="line one\nline two",
        distance=0.5,
        collection="knowledge__demo",
        metadata={"title": "Demo Note"},
    )
    assert format_plain([r]) == ["[0.5000] Demo Note", "  line one"]


# ── format_plain_with_context ───────────────────────────────────────────────


@pytest.mark.parametrize("lines_after,expected_count", [
    (0, 3),  # all lines (delegates to format_plain)
    (1, 2),
    (100, 3),
])
def test_context_line_counts(lines_after, expected_count) -> None:
    lines = format_plain_with_context([_result()], lines_after=lines_after)
    assert len(lines) == expected_count


def test_context_zero_delegates_to_plain() -> None:
    results = [_result()]
    assert format_plain_with_context(results, lines_after=0) == format_plain(results)


def test_context_single_line() -> None:
    assert format_plain_with_context([_result(content="solo")], lines_after=5) == ["src/foo.py:10:solo"]


# ── _find_matching_lines ────────────────────────────────────────────────────


@pytest.mark.parametrize("chunk,query,rg_lines,start,expected", [
    ("def foo():\n    return 42\n    pass", "return", [], 0, [1]),
    ("import os\nimport sys\nprint(os.path)\nprint(sys.argv)", "os sys", [], 0, [0, 1]),
    ("class MyClass:\n    CONSTANT = True", "myclass constant", [], 0, [0, 1]),
    ("line zero\nline one\nline two\nline three", "zero", [12], 10, [2]),
    ("alpha\nbeta\ngamma", "xyznotfound", [], 0, [0]),
    ("", "query", [], 0, [0]),
])
def test_find_matching_lines(chunk, query, rg_lines, start, expected) -> None:
    result = _find_matching_lines(chunk, query, rg_matched_lines=rg_lines or None, chunk_line_start=start or 0)
    if len(expected) == 1:
        assert result == expected
    else:
        for e in expected:
            assert e in result


def test_find_matching_lines_rg_out_of_range_falls_back() -> None:
    result = _find_matching_lines("line zero\nline one", "zero", rg_matched_lines=[50], chunk_line_start=10)
    assert result == [0]


# ── _extract_context ────────────────────────────────────────────────────────


@pytest.mark.parametrize("matches,before,after,expected_indices,expected_types", [
    ([2], 0, 1, [2, 3], ["match", "context"]),
    ([3], 2, 0, [1, 2, 3], ["context", "context", "match"]),
    ([2], 1, 1, [1, 2, 3], None),
    ([0], 3, 0, [0], ["match"]),
    ([2], 0, 3, [2], ["match"]),  # last index, after=3 stops at boundary (3 items)
    ([], 1, 1, [], None),
])
def test_extract_context(matches, before, after, expected_indices, expected_types) -> None:
    lines = ["L0", "L1", "L2", "L3", "L4"][:max(max(expected_indices, default=-1) + 1, 3)]
    result = _extract_context(lines, matches, before=before, after=after)
    assert [r[0] for r in result] == expected_indices
    if expected_types:
        assert [r[1] for r in result] == expected_types


def test_extract_context_bridge_merging() -> None:
    lines = [f"L{i}" for i in range(10)]
    result = _extract_context(lines, [2, 5], before=0, after=0)
    assert [r[0] for r in result] == [2, 3, 4, 5]


def test_extract_context_no_bridge_large_gap() -> None:
    lines = [f"L{i}" for i in range(20)]
    indices = [r[0] for r in _extract_context(lines, [2, 15], before=0, after=0)]
    assert 2 in indices and 15 in indices and 8 not in indices


# ── _merge_line_ranges ──────────────────────────────────────────────────────


@pytest.mark.parametrize("ranges,expected", [
    ([(1, 5), (3, 8)], [(1, 8)]),
    ([(1, 5), (6, 10)], [(1, 10)]),
    ([(1, 5), (8, 10)], [(1, 5), (8, 10)]),
    ([(8, 10), (1, 5)], [(1, 5), (8, 10)]),
    ([], []),
    ([(3, 7)], [(3, 7)]),
])
def test_merge_line_ranges(ranges, expected) -> None:
    assert _merge_line_ranges(ranges) == expected


# ── _is_bat_installed ───────────────────────────────────────────────────────


@pytest.mark.parametrize("side_effect,expected", [
    (FileNotFoundError("bat"), False),
    (None, True),
])
def test_is_bat_installed(monkeypatch, side_effect, expected) -> None:
    _is_bat_installed.cache_clear()
    if side_effect:
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: (_ for _ in ()).throw(side_effect),
        )
    else:
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=["bat"], returncode=0),
        )
    assert _is_bat_installed() is expected
    _is_bat_installed.cache_clear()


# ── format_plain_with_context (query-aware) ─────────────────────────────────


_LONG_CONTENT = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\nreturn result\nfinal\nend"


def test_context_with_query_centers_on_match() -> None:
    r = _result(content=_LONG_CONTENT, line_start=100)
    lines = format_plain_with_context([r], lines_after=1, query="return")
    assert any(":107:" in ln for ln in lines)
    assert any(":108:" in ln for ln in lines)


def test_context_with_query_and_before() -> None:
    r = _result(content=_LONG_CONTENT, line_start=100)
    lines = format_plain_with_context([r], lines_before=2, lines_after=0, query="return")
    assert any(":105:" in ln for ln in lines)
    assert any(":106:" in ln for ln in lines)
    assert any(":107:" in ln for ln in lines)


def test_context_with_rg_matched_lines() -> None:
    content = "\n".join(f"line {i}" for i in range(10))
    r = _result(content=content, line_start=10, rg_matched_lines=[15])
    lines = format_plain_with_context([r], lines_after=1, query="nomatch")
    assert any(":15:" in ln for ln in lines)
    assert any(":16:" in ln for ln in lines)


@pytest.mark.parametrize("query,lines_after,expected_len,first_line", [
    (None, 1, 2, "src/foo.py:10:line one"),
    ("one", 0, 3, None),  # backward compat: lines_before=0, lines_after=0 -> format_plain
])
def test_context_backward_compat(query, lines_after, expected_len, first_line) -> None:
    r = _result()
    lines = format_plain_with_context([r], lines_after=lines_after, query=query)
    if expected_len == 3:
        assert lines == format_plain([r])
    else:
        assert len(lines) == expected_len
    if first_line:
        assert lines[0] == first_line


# ── format_vimgrep (query-aware) ───────────────────────────────────────────


def test_vimgrep_with_query_uses_matching_line() -> None:
    content = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\nreturn result"
    lines = format_vimgrep([_result(content=content, line_start=10)], query="return")
    assert lines[0].startswith("src/foo.py:17:0:")
    assert "return result" in lines[0]


def test_vimgrep_without_query_uses_line_start() -> None:
    assert format_vimgrep([_result()], query=None) == ["src/foo.py:10:0:line one"]


# ── format_compact ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("query,content,expected", [
    (None, "line one\nline two\nline three", "src/foo.py:10:line one"),
    ("return", "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\nreturn result", "src/foo.py:17:return result"),
])
def test_compact(query, content, expected) -> None:
    lines = format_compact([_result(content=content)], query=query)
    assert lines[0] == expected
