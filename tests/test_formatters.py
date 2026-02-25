"""Output formatter tests: vimgrep, JSON, plain, plain-with-context."""
import json

from nexus.formatters import (
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
