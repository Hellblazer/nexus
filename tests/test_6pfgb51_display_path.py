# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 2 (nexus-6pfgb.51): display-path reads take the catalog, never the chunk row.

``formatters._display_path`` fell back to the row's ``source_path`` (the last writer's file, another
document's after a shared chunk) and then to a ``file_path`` key no writer leaves on a row. A hit
whose document the catalog could not resolve showed that other document's path in every ``nx search``
output format. It now reads ``_display_path``, then ``_display_source_path`` (the owner's absolute
local path), and an orphan chunk shows its title or id.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from nexus import formatters
from nexus.formatters import (
    _display_path,
    _file_line_no,
    format_compact,
    format_plain,
    format_plain_with_context,
    format_vimgrep,
)
from nexus.types import SearchResult


STALE = "/stale/other-documents-file.py"


def _row(id: str = "chunk-aaaa", **meta) -> SearchResult:
    return SearchResult(
        id=id, content="def foo():\n    return 1", distance=0.1, collection="code__test",
        metadata={"line_start": 3, **meta},
    )


def _render(fmt: str, r: SearchResult) -> str:
    if fmt == "plain":
        return "\n".join(format_plain([r]))
    if fmt == "vimgrep":
        return "\n".join(format_vimgrep([r], query="foo"))
    if fmt == "compact":
        return "\n".join(format_compact([r], query="foo"))
    return "\n".join(format_plain_with_context([r], query="foo", lines_before=1, lines_after=1))


# ── .51 ─────────────────────────────────────────────────────────────────────

def test_display_path_reads_the_display_keys_in_order_and_nothing_else() -> None:
    assert _display_path({"_display_path": "rel/a.py", "_display_source_path": "/abs/a.py"}) == "rel/a.py"
    assert _display_path({"_display_source_path": "/abs/a.py", "source_path": STALE}) == "/abs/a.py"
    assert _display_path({"source_path": STALE}) == ""
    assert _display_path({"source_path": STALE, "file_path": STALE}, default="unknown") == "unknown"


@pytest.mark.parametrize("fmt", ["plain", "vimgrep", "compact", "context"])
def test_no_formatter_shows_a_stale_source_path(fmt: str) -> None:
    """An orphan chunk (no catalog path) shows its id, never the last writer's path."""
    out = _render(fmt, _row(source_path=STALE, file_path=STALE))
    assert STALE not in out
    assert "chunk-aaaa" in out, "the hit falls back to its id"


@pytest.mark.parametrize("fmt", ["plain", "vimgrep", "compact", "context"])
def test_formatters_show_the_owners_absolute_path_when_only_that_is_attached(fmt: str) -> None:
    out = _render(fmt, _row(source_path=STALE, _display_source_path="/abs/owner.py"))
    assert "/abs/owner.py" in out
    assert STALE not in out


def test_file_line_no_does_not_open_a_stale_source_path(tmp_path) -> None:
    stale = tmp_path / "stale.txt"
    stale.write_text("one\nthe wanted line\n")
    r = SearchResult(id="n", content="x", distance=0.1, collection="docs__t",
                     metadata={"source_path": str(stale)})
    assert _file_line_no(r, 0, "the wanted line", {}) == 1, "no shown path: the position in the note"


def test_formatters_module_never_reads_the_row_path_keys() -> None:
    """The read-site lint cannot see a read through a parameter named ``meta`` (a ``[receivers]``
    entry would be reported stale once the read is gone), so the module is pinned here."""
    reads = _row_path_key_reads(pathlib.Path(formatters.__file__).read_text())
    assert not reads, f"formatters.py reads a chunk row's source_path or file_path at lines {reads}"


_ROW_PATH_KEYS = {"source_path", "file_path"}


def _row_path_key_reads(source: str) -> list[int]:
    """Lines reading a row path key: ``x.get("source_path"...)`` or ``x["file_path"]``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value in _ROW_PATH_KEYS
        ) or (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant) and node.slice.value in _ROW_PATH_KEYS
        ):
            found.append(node.lineno)
    return found


@pytest.mark.parametrize("read", [
    'meta.get("source_path")', "meta.get('source_path', '')", 'meta.get("file_path")',
    'meta["source_path"]', "meta['file_path']",
])
def test_the_path_key_pin_sees_every_read_spelling(read: str) -> None:
    assert _row_path_key_reads(f"x = {read}\n") == [1]


def test_the_path_key_pin_ignores_other_keys() -> None:
    assert _row_path_key_reads('a = meta.get("_display_path")\nb = meta["title"]\n') == []
