# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 2 (nexus-6pfgb.53): ``nx search PATH`` scoping matches the owner's absolute path.

The scope matched only ``_display_path`` (the catalog ``file_path``, often repo-relative) or the
legacy ``file_path`` key, so an absolute PATH never selected a hit whose owner lives under it.
``_display_source_path`` (the owner's absolute local path, stamped by the search) is matched too.
"""
from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from nexus.cli import main
from nexus.types import SearchResult

from tests.conftest import patched_mcp_infra_t3
from tests.test_search_cmd import _mock_t3 as _cmd_t3

STALE = "/stale/other-documents-file.py"


def _row(id: str, **meta) -> SearchResult:
    return SearchResult(
        id=id, content="def foo():\n    return 1", distance=0.1, collection="code__test",
        metadata={"line_start": 3, **meta},
    )


def _search_files(args: list[str], rows: list[SearchResult], monkeypatch) -> str:
    for k, v in {"CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "v", "CHROMA_TENANT": "t", "CHROMA_DATABASE": "d"}.items():
        monkeypatch.setenv(k, v)
    mock_t3 = _cmd_t3(["code__test"])
    cfg = {"embeddings": {"rerankerModel": "rerank-2.5"}}
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), patched_mcp_infra_t3(mock_t3), \
         patch("nexus.catalog.factory.make_catalog_reader", side_effect=RuntimeError("no catalog")), \
         patch("nexus.db.t2.T2Database"), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=rows), \
         patch("nexus.commands.search_cmd.load_config", return_value=cfg):
        res = CliRunner().invoke(main, ["search", "q", *args, "--corpus", "code__test", "--files"])
    assert res.exit_code == 0, res.output
    return res.output


def test_an_absolute_path_scope_selects_a_hit_by_its_owners_absolute_path(tmp_path, monkeypatch) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    rows = [
        _row("keep", _display_path="pkg/in.py", _display_source_path=str(scope / "in.py"), source_path=STALE),
        _row("elsewhere", _display_path="pkg/out.py", _display_source_path=str(tmp_path / "other" / "out.py")),
        _row("stale", _display_path="pkg/stale.py", source_path=str(scope / "stale.py")),
    ]
    out = _search_files([str(scope)], rows, monkeypatch)
    assert "pkg/in.py" in out, "the absolute scope matches the owner's absolute path"
    assert "pkg/out.py" not in out
    assert "pkg/stale.py" not in out, "a row's source_path never satisfies the scope"
