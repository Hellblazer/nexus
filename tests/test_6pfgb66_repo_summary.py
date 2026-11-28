# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.66 fix round 1, item 2: the ``nx index repo`` summary for files the engine refused for chunk keys.

The summary text is built by ``chunk_key_refusal_report`` from the run stats; ``index_repo_cmd`` echoes it and
takes these files out of the generic failed-flush block ("retried automatically", "see the WARNING line above"),
which is wrong for them. The command itself needs a tenant to run in a test, so the text is pinned here at its
source and the wiring by reading the command.
"""
from __future__ import annotations

import ast
from pathlib import Path

from nexus.collection_errors import MAX_LISTED_KEYS, chunk_key_refusal_report, chunk_key_refusal_stats

_STATS = {
    "chunk_flush_refused_files": 3,
    "chunk_flush_refused_paths": ["a.py", "b.py", "c.py"],
    "chunk_flush_refused_keys": [
        {"key": "title", "class": "DOCUMENT", "home": "the catalog document"},
        {"key": "line_start", "class": "POSITION", "home": "the manifest row"},
    ],
}


def test_no_refused_files_no_report() -> None:
    assert chunk_key_refusal_report({}, 5) is None
    assert chunk_key_refusal_report({"chunk_flush_refused_files": 0}, 5) is None


def test_the_report_names_paths_keys_and_homes() -> None:
    warning, error = chunk_key_refusal_report(_STATS, 10)  # type: ignore[misc]
    assert warning.startswith("Warning: 3/10 file(s) refused by the engine")
    assert "a.py, b.py, c.py" in warning
    assert "'title' (DOCUMENT, belongs in: the catalog document)" in warning
    assert "'line_start' (POSITION, belongs in: the manifest row)" in warning
    assert "3 file(s) refused by the engine" in error and "Upgrade nx" in error


def test_the_report_caps_keys_and_paths() -> None:
    many_keys = [{"key": f"k{i:02d}", "class": "UNKNOWN", "home": "nowhere"} for i in range(MAX_LISTED_KEYS + 4)]
    paths = [f"f{i}.py" for i in range(13)]
    warning, _ = chunk_key_refusal_report(
        {**_STATS, "chunk_flush_refused_files": 13, "chunk_flush_refused_paths": paths,
         "chunk_flush_refused_keys": many_keys}, 20)  # type: ignore[misc]
    assert "'k09'" in warning and "'k10'" not in warning and "and 4 more" in warning
    assert "f9.py" in warning and "f10.py" not in warning and "and 3 more" in warning


def test_the_report_claims_nothing_about_what_was_written_and_promises_no_retry() -> None:
    warning, error = chunk_key_refusal_report(_STATS, 3)  # type: ignore[misc]
    for text in (warning, error):
        low = text.lower()
        for bad in ("retried automatically", "zero chunks landed", "nothing was indexed", "nothing was written",
                    "warning line above"):
            assert bad not in low
    assert "A re-run is refused the same way" in warning
    assert "The engine reported these requests refused" in warning


def test_stats_dedupe_the_keys_over_all_files() -> None:
    from nexus.collection_errors import chunk_key_refused_error_from_body as mk  # noqa: PLC0415 — test-local import

    def err(*keys: str):
        return mk(422, {"reason": "chunk_metadata_key_refused",
                        "refused": [{"key": k, "class": "DOCUMENT", "home": "h"} for k in keys]})

    stats = chunk_key_refusal_stats({"b.py": err("title", "x"), "a.py": err("title")})
    assert stats["chunk_flush_refused_files"] == 2
    assert stats["chunk_flush_refused_paths"] == ["a.py", "b.py"]
    assert [k["key"] for k in stats["chunk_flush_refused_keys"]] == ["title", "x"]
    assert chunk_key_refusal_stats({}) == {
        "chunk_flush_refused_files": 0, "chunk_flush_refused_paths": [], "chunk_flush_refused_keys": []}


def test_index_repo_cmd_prints_the_report_and_leaves_the_refused_files_out_of_the_failed_flush_block() -> None:
    """The command's wiring, read from its source: it builds the report from the run stats, echoes it, and
    subtracts the refused count from the generic failed-flush block (a refused file is also a failed file)."""
    src = (Path(__file__).resolve().parents[1] / "src" / "nexus" / "commands" / "index.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "index_repo_cmd")
    body = ast.get_source_segment(src, fn) or ""
    assert "chunk_key_refusal_report(stats or {}, n)" in body
    assert "click.echo(_refused_report[0])" in body
    assert '.get("chunk_flush_failed_files", 0) - _refused_files' in body
    assert "if chunk_flush_failed_files > 0:" in body
