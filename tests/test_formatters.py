"""Output formatter tests: vimgrep, JSON, plain, plain-with-context."""
import json
import subprocess

import pytest

from nexus.formatters import (
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


# ── format_vimgrep ───────────────────────────────────────────────────────────

def test_vimgrep_basic() -> None:
    lines = format_vimgrep([_result()])
    assert lines == ["src/foo.py:10:0:line one"]


def test_vimgrep_missing_source_path() -> None:
    r = _result()
    r.metadata.pop("source_path")
    lines = format_vimgrep([r])
    assert lines[0].startswith(":10:0:")


def test_vimgrep_empty_content() -> None:
    lines = format_vimgrep([_result(content="")])
    assert lines == ["src/foo.py:10:0:"]


def test_vimgrep_multiple_results() -> None:
    lines = format_vimgrep([_result(), _result(source_path="b.py", line_start=5, content="hello")])
    assert len(lines) == 2
    assert lines[1] == "b.py:5:0:hello"


# ── format_json ──────────────────────────────────────────────────────────────

def test_json_basic() -> None:
    out = format_json([_result()])
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["id"] == "r1"
    assert parsed[0]["collection"] == "code__x"
    assert parsed[0]["source_path"] == "src/foo.py"


def test_json_canonical_fields_win_over_metadata() -> None:
    """If metadata contains 'id' or 'collection', the canonical field wins."""
    r = _result(id="meta-id", collection="meta-col")
    r.metadata["id"] = "meta-id"
    r.metadata["collection"] = "meta-col"
    out = format_json([r])
    parsed = json.loads(out)
    assert parsed[0]["id"] == "r1"
    assert parsed[0]["collection"] == "code__x"


def test_json_empty_results() -> None:
    out = format_json([])
    assert json.loads(out) == []


# ── format_plain ─────────────────────────────────────────────────────────────

def test_plain_basic() -> None:
    lines = format_plain([_result()])
    assert lines[0] == "src/foo.py:10:line one"
    assert lines[1] == "src/foo.py:11:line two"
    assert lines[2] == "src/foo.py:12:line three"


def test_plain_empty_content() -> None:
    lines = format_plain([_result(content="")])
    assert lines == []


def test_plain_single_line() -> None:
    lines = format_plain([_result(content="only line")])
    assert lines == ["src/foo.py:10:only line"]


def test_plain_missing_metadata() -> None:
    """Missing source_path and line_start use defaults."""
    r = SearchResult(id="r1", content="hello", distance=0.1, collection="c", metadata={})
    lines = format_plain([r])
    assert lines == [":0:hello"]


# ── format_plain_with_context ────────────────────────────────────────────────

def test_context_zero_delegates_to_plain() -> None:
    """lines_after=0 produces identical output to format_plain."""
    results = [_result()]
    assert format_plain_with_context(results, lines_after=0) == format_plain(results)


def test_context_limits_lines() -> None:
    """lines_after=1 shows first line + 1 extra = 2 lines total."""
    lines = format_plain_with_context([_result()], lines_after=1)
    assert len(lines) == 2
    assert lines[0] == "src/foo.py:10:line one"
    assert lines[1] == "src/foo.py:11:line two"


def test_context_shows_all_when_large() -> None:
    """lines_after larger than content shows all lines."""
    lines = format_plain_with_context([_result()], lines_after=100)
    assert len(lines) == 3


def test_context_single_line_content() -> None:
    lines = format_plain_with_context([_result(content="solo")], lines_after=5)
    assert lines == ["src/foo.py:10:solo"]


# ── _find_matching_lines ────────────────────────────────────────────────────


def test_find_matching_lines_keyword_match() -> None:
    """Keyword match finds the line containing the query term."""
    chunk = "def foo():\n    return 42\n    pass"
    result = _find_matching_lines(chunk, "return")
    assert result == [1]  # "return 42" is at index 1


def test_find_matching_lines_multiple_matches() -> None:
    """Multiple lines match different query tokens."""
    chunk = "import os\nimport sys\nprint(os.path)\nprint(sys.argv)"
    result = _find_matching_lines(chunk, "os sys")
    assert 0 in result  # "import os"
    assert 1 in result  # "import sys"


def test_find_matching_lines_case_insensitive() -> None:
    """Keyword matching is case-insensitive."""
    chunk = "class MyClass:\n    CONSTANT = True"
    result = _find_matching_lines(chunk, "myclass constant")
    assert 0 in result
    assert 1 in result


def test_find_matching_lines_rg_lines_preferred() -> None:
    """rg_matched_lines take priority over keyword matching."""
    chunk = "line zero\nline one\nline two\nline three"
    # rg says line 12 matched (absolute), chunk starts at line 10
    result = _find_matching_lines(chunk, "zero", rg_matched_lines=[12], chunk_line_start=10)
    assert result == [2]  # absolute 12 - start 10 = index 2


def test_find_matching_lines_rg_out_of_range_falls_back() -> None:
    """rg_matched_lines outside chunk range falls back to keyword match."""
    chunk = "line zero\nline one"
    result = _find_matching_lines(chunk, "zero", rg_matched_lines=[50], chunk_line_start=10)
    assert result == [0]  # keyword match on "zero"


def test_find_matching_lines_no_match_falls_back_to_zero() -> None:
    """When nothing matches, falls back to [0]."""
    chunk = "alpha\nbeta\ngamma"
    result = _find_matching_lines(chunk, "xyznotfound")
    assert result == [0]


def test_find_matching_lines_empty_chunk() -> None:
    """Empty chunk returns [0]."""
    result = _find_matching_lines("", "query")
    assert result == [0]


# ── _extract_context ────────────────────────────────────────────────────────


def test_extract_context_basic_after() -> None:
    """Match at index 2, after=1 shows indices 2-3."""
    lines = ["L0", "L1", "L2", "L3", "L4"]
    result = _extract_context(lines, [2], before=0, after=1)
    assert len(result) == 2
    assert result[0] == (2, "match", "L2")
    assert result[1] == (3, "context", "L3")


def test_extract_context_basic_before() -> None:
    """Match at index 3, before=2 shows indices 1-3."""
    lines = ["L0", "L1", "L2", "L3", "L4"]
    result = _extract_context(lines, [3], before=2, after=0)
    assert len(result) == 3
    assert result[0] == (1, "context", "L1")
    assert result[1] == (2, "context", "L2")
    assert result[2] == (3, "match", "L3")


def test_extract_context_before_and_after() -> None:
    """Match at index 2, before=1, after=1 shows indices 1-3."""
    lines = ["L0", "L1", "L2", "L3", "L4"]
    result = _extract_context(lines, [2], before=1, after=1)
    assert len(result) == 3
    indices = [r[0] for r in result]
    assert indices == [1, 2, 3]


def test_extract_context_bridge_merging() -> None:
    """Two matches 2 lines apart get bridged into one block."""
    lines = [f"L{i}" for i in range(10)]
    # matches at 2 and 5 — gap of 2 lines (3,4) → should bridge
    result = _extract_context(lines, [2, 5], before=0, after=0)
    indices = [r[0] for r in result]
    assert indices == [2, 3, 4, 5]  # bridged


def test_extract_context_no_bridge_large_gap() -> None:
    """Two matches far apart produce separate blocks."""
    lines = [f"L{i}" for i in range(20)]
    result = _extract_context(lines, [2, 15], before=0, after=0)
    indices = [r[0] for r in result]
    assert 2 in indices
    assert 15 in indices
    assert 8 not in indices  # gap not bridged


def test_extract_context_match_at_start_no_before() -> None:
    """Match at index 0, before=3 produces no pre-context (boundary)."""
    lines = ["L0", "L1", "L2", "L3"]
    result = _extract_context(lines, [0], before=3, after=0)
    assert len(result) == 1
    assert result[0] == (0, "match", "L0")


def test_extract_context_match_at_end_no_after() -> None:
    """Match at last index, after=3 stops at chunk boundary."""
    lines = ["L0", "L1", "L2"]
    result = _extract_context(lines, [2], before=0, after=3)
    assert len(result) == 1
    assert result[0] == (2, "match", "L2")


def test_extract_context_empty_matches() -> None:
    """Empty matches list returns empty."""
    result = _extract_context(["L0", "L1"], [], before=1, after=1)
    assert result == []


# ── _merge_line_ranges ──────────────────────────────────────────────────────


def test_merge_line_ranges_overlapping() -> None:
    assert _merge_line_ranges([(1, 5), (3, 8)]) == [(1, 8)]


def test_merge_line_ranges_adjacent() -> None:
    assert _merge_line_ranges([(1, 5), (6, 10)]) == [(1, 10)]


def test_merge_line_ranges_gap() -> None:
    assert _merge_line_ranges([(1, 5), (8, 10)]) == [(1, 5), (8, 10)]


def test_merge_line_ranges_unsorted() -> None:
    assert _merge_line_ranges([(8, 10), (1, 5)]) == [(1, 5), (8, 10)]


def test_merge_line_ranges_empty() -> None:
    assert _merge_line_ranges([]) == []


def test_merge_line_ranges_single() -> None:
    assert _merge_line_ranges([(3, 7)]) == [(3, 7)]


# ── _is_bat_installed ───────────────────────────────────────────────────────


def test_is_bat_installed_not_found(monkeypatch) -> None:
    """Returns False when bat binary is missing."""
    _is_bat_installed.cache_clear()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("bat")),
    )
    assert _is_bat_installed() is False
    _is_bat_installed.cache_clear()


def test_is_bat_installed_found(monkeypatch) -> None:
    """Returns True when bat responds."""
    _is_bat_installed.cache_clear()
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(args=["bat"], returncode=0),
    )
    assert _is_bat_installed() is True
    _is_bat_installed.cache_clear()


# ── format_plain_with_context (query-aware) ────────────────────────────────


def test_context_with_query_centers_on_match() -> None:
    """With query, context windows center on matching lines."""
    content = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\nreturn result\nfinal\nend"
    r = _result(content=content, line_start=100)
    lines = format_plain_with_context([r], lines_after=1, query="return")
    # "return result" is at index 7, so line 107 (match) and 108 (after)
    assert any(":107:" in ln for ln in lines)
    assert any(":108:" in ln for ln in lines)


def test_context_with_query_and_before() -> None:
    """lines_before shows lines before the match within the chunk."""
    content = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\nreturn result\nfinal\nend"
    r = _result(content=content, line_start=100)
    lines = format_plain_with_context([r], lines_before=2, lines_after=0, query="return")
    # "return result" is at index 7 → line 107. Before=2 shows 105,106,107
    assert any(":105:" in ln for ln in lines)
    assert any(":106:" in ln for ln in lines)
    assert any(":107:" in ln for ln in lines)


def test_context_with_rg_matched_lines() -> None:
    """rg_matched_lines in metadata are used for line identification."""
    content = "\n".join(f"line {i}" for i in range(10))
    r = _result(content=content, line_start=10, rg_matched_lines=[15])
    lines = format_plain_with_context([r], lines_after=1, query="nomatch")
    # rg says line 15 (absolute) → index 5 in chunk
    assert any(":15:" in ln for ln in lines)
    assert any(":16:" in ln for ln in lines)


def test_context_backward_compat_no_query() -> None:
    """query=None preserves current behavior (first N lines)."""
    r = _result()
    lines = format_plain_with_context([r], lines_after=1, query=None)
    assert len(lines) == 2
    assert lines[0] == "src/foo.py:10:line one"
    assert lines[1] == "src/foo.py:11:line two"


def test_context_backward_compat_no_flags() -> None:
    """lines_before=0, lines_after=0 -> format_plain output."""
    r = _result()
    assert format_plain_with_context([r], query="one") == format_plain([r])


# ── format_vimgrep (query-aware) ───────────────────────────────────────────


def test_vimgrep_with_query_uses_matching_line() -> None:
    """format_vimgrep with query reports matching line number."""
    content = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\nreturn result"
    r = _result(content=content, line_start=10)
    lines = format_vimgrep([r], query="return")
    assert lines[0].startswith("src/foo.py:17:0:")
    assert "return result" in lines[0]


def test_vimgrep_without_query_uses_line_start() -> None:
    """format_vimgrep with query=None returns line_start (backward compat)."""
    lines = format_vimgrep([_result()], query=None)
    assert lines == ["src/foo.py:10:0:line one"]


# ── format_compact ──────────────────────────────────────────────────────────


def test_compact_basic_format() -> None:
    """format_compact produces path:line:text for each result."""
    lines = format_compact([_result()])
    assert lines == ["src/foo.py:10:line one"]


def test_compact_with_query_best_line() -> None:
    """With query, compact reports best-matching line."""
    content = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\nreturn result"
    r = _result(content=content, line_start=10)
    lines = format_compact([r], query="return")
    assert lines[0] == "src/foo.py:17:return result"


def test_compact_without_query_first_line() -> None:
    """Without query, compact reports first line at line_start."""
    lines = format_compact([_result()], query=None)
    assert lines == ["src/foo.py:10:line one"]
