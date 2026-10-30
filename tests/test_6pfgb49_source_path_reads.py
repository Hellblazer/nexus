# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 2 (nexus-6pfgb.49): the dead ``source_path`` reads on chunk rows are gone.

A chunk row's ``source_path`` is the last writer's file (and, since RDR-102 D2, no writer sets it
at all); the owning document's path is the catalog entry's, stamped on the hit as ``_display_path``
(and ``_display_source_path``, the absolute local path of its ``file://`` source_uri). Every reader
that took ``source_path`` from the row showed or keyed on a path that may belong to another
document. Each test feeds a row carrying a STALE ``source_path`` and asserts it never appears.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.formatters import _file_line_no
from nexus.mcp.core import _search_render
from nexus.mcp_server import _reset_singletons, query
from nexus.search_engine import apply_file_diversity_cap
from nexus.types import SearchResult

from tests.conftest import patched_mcp_infra_t3
from tests.test_mcp_server import _HYBRID_DEFAULT_ON_CFG, _mock_t3
from tests.test_search_cmd import _mock_t3 as _cmd_t3

@pytest.fixture(autouse=True)
def _reset():
    _reset_singletons()
    yield
    _reset_singletons()


STALE = "/stale/other-documents-file.py"


def _row(id: str, **meta) -> SearchResult:
    return SearchResult(
        id=id, content="body text", distance=0.1, collection="code__test", metadata=dict(meta),
    )


def test_query_does_not_show_or_group_by_a_stale_source_path():
    """The grouping fallback keyed two unrelated chunks that carried one stale path into ONE card
    and titled it with that path; the doc card also carried the path as ``source_path``."""
    _mock_t3([{"name": "code__test", "count": 2}])

    def fake(*_a, **_kw):
        return [_row("chunk-aaaa", source_path=STALE), _row("chunk-bbbb", source_path=STALE)]

    with patch("nexus.search_engine.search_cross_corpus", fake), \
         patch("nexus.config.load_config", return_value=_HYBRID_DEFAULT_ON_CFG):
        out = query(question="stale path probe", corpus="code__test")
    assert STALE not in out
    assert "Found 2 documents" in out, "two unrelated chunks are two cards, not one under the stale path"


def test_search_text_label_never_falls_back_to_a_stale_source_path():
    _mock_t3([{"name": "code__test", "count": 1}])

    def fake(*_a, **_kw):
        return [_row("chunk-dddd", source_path=STALE)]

    # the search trace reaches T1; a session-less fake keeps this test off the tenant
    with patch("nexus.search_engine.search_cross_corpus", fake), \
         patch("nexus.mcp.core._get_t1", return_value=(SimpleNamespace(session_id=""), None)), \
         patch("nexus.config.load_config", return_value=_HYBRID_DEFAULT_ON_CFG):
        out = _search_render(query="stale label probe", corpus="code__test")
    assert STALE not in out
    assert "chunk-dddd" in out, "an unlabelled hit falls back to its id"


def test_the_file_diversity_cap_does_not_group_rows_by_a_stale_source_path():
    """Three rows naming no owner and sharing one stale path were capped as if one file."""
    rows = [_row(f"r{i}", source_path=STALE) for i in range(3)] + [_row("z")]
    assert [r.id for r in apply_file_diversity_cap(rows)] == ["r0", "r1", "r2", "z"]
    owned = [_row(f"o{i}", doc_id="1.1.1", source_path=STALE) for i in range(3)]
    assert [r.id for r in apply_file_diversity_cap(owned)] == ["o0", "o1", "o2"], "doc_id is the file identity"
    assert [r.id for r in apply_file_diversity_cap(owned + [_row("x", doc_id="1.1.2")])] == ["o0", "o1", "x", "o2"]


def test_file_line_no_opens_the_catalog_source_never_the_rows_source_path(tmp_path):
    """The catalog path is often repo-relative and does not open from here; the absolute fallback is
    the owner's source_uri (``_display_source_path``), not the row's last-writer ``source_path``."""
    stale = tmp_path / "stale.txt"
    stale.write_text("one\ntwo\nthe wanted line\n")
    owner = tmp_path / "owner.txt"
    owner.write_text("alpha\nbeta\ngamma\ndelta\nthe wanted line\n")
    base = {"_display_path": "relative/not/openable.txt"}

    cache: dict = {}
    r = _row("n1", source_path=str(stale), **base)
    assert _file_line_no(r, 0, "the wanted line", cache) == 1, "the row's source_path is not read"

    cache = {}
    r = _row("n2", source_path=str(stale), _display_source_path=str(owner), **base)
    assert _file_line_no(r, 0, "the wanted line", cache) == 5, "the owner's absolute path is read"


def test_search_cmd_path_scope_and_files_only_ignore_a_stale_source_path(tmp_path, monkeypatch):
    for k, v in {"CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "v", "CHROMA_TENANT": "t", "CHROMA_DATABASE": "d"}.items():
        monkeypatch.setenv(k, v)
    scope = tmp_path / "scope"
    scope.mkdir()
    inside = str(scope / "in.py")
    rows = [
        _row("keep", _display_path=inside, source_path=STALE),
        _row("drop", source_path=str(scope / "stale.py")),
    ]
    mock_t3 = _cmd_t3(["code__test"])
    cfg = {"embeddings": {"rerankerModel": "rerank-2.5"}}
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), patched_mcp_infra_t3(mock_t3), \
         patch("nexus.catalog.factory.make_catalog_reader", side_effect=RuntimeError("no catalog")), \
         patch("nexus.db.t2.T2Database"), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=rows), \
         patch("nexus.commands.search_cmd.load_config", return_value=cfg):
        scoped = CliRunner().invoke(
            main, ["search", "q", str(scope), "--corpus", "code__test", "--files"],
        )
        unscoped = CliRunner().invoke(main, ["search", "q", "--corpus", "code__test", "--files"])
    assert scoped.exit_code == 0, scoped.output
    assert inside in scoped.output
    assert str(scope / "stale.py") not in scoped.output, "a row's source_path does not satisfy --path"
    assert STALE not in unscoped.output and str(scope / "stale.py") not in unscoped.output


class _Entry:
    def __init__(self, file_path: str, source_uri: str) -> None:
        self.file_path, self.source_uri = file_path, source_uri
        self.title, self.physical_collection = "t", "code__test"
        self.meta: dict = {}
        self.indexed_at = ""
        self.bib_year = self.bib_citation_count = 0
        self.bib_authors = self.bib_venue = ""


class _Catalog:
    def __init__(self, entries: dict) -> None:
        self.entries, self.calls = entries, 0

    def resolve_many(self, ids):
        self.calls += 1
        return {i: self.entries[i] for i in ids if i in self.entries}


def test_the_owners_local_source_is_stamped_from_the_entry_the_page_already_resolved():
    from nexus.context_annotations import DISPLAY_SOURCE_PATH_KEY
    from nexus.formatters import format_json
    from nexus.search_engine import _attach_display_paths

    cat = _Catalog({
        "1.1.1": _Entry("rel/a.py", "file:///abs/repo/a.py"),
        "1.1.2": _Entry("", "nx-note://n"),
    })
    rows = [_row("a", doc_id="1.1.1"), _row("b", doc_id="1.1.2"), _row("c")]
    _attach_display_paths(rows, cat)
    assert cat.calls == 1, "one lookup for the page: the stamp adds no catalog call"
    assert rows[0].metadata[DISPLAY_SOURCE_PATH_KEY] == "/abs/repo/a.py"
    assert rows[0].metadata["_display_path"] == "rel/a.py"
    assert DISPLAY_SOURCE_PATH_KEY not in rows[1].metadata, "only a file:// source is a local path"
    assert DISPLAY_SOURCE_PATH_KEY not in rows[2].metadata
    assert DISPLAY_SOURCE_PATH_KEY not in format_json(rows[:1]), "a working key stays off the JSON wire"
