# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 fix round 3 (nexus-rfvyy.6.20): ``nx collection reindex`` reads the sensitivity flags BEFORE it
deletes the collection.

The verb deletes the collection and then indexes every source again. The indexers skip or refuse a document the
catalog flags sensitive when its collection embeds with Voyage, so a flagged document's chunks used to be gone by the
time the gate spoke. Now a Voyage (or unknown-embedder) target holding a flagged document refuses first, naming the
documents and ``--ack-external-embedder``; with the flag the rebuild goes ahead and the indexers are told. The rdr branch
also counts what it indexed, not what it was handed.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.cli import main
from nexus.db.collection_purge import CascadeCounts
from tests.test_collection_cmd import _setup_reindex_mock, env_creds, mock_db, runner  # noqa: F401 — fixtures
from tests.test_sensitive_external_rules import _register_in

pytestmark = pytest.mark.t2_tenant

_VOYAGE = "docs__rfvyy-reindex__voyage-context-3__v1"
_BGE = "docs__rfvyy-reindex__bge-base-en-v15-768__v1"


def _flag(tumbler: str) -> None:
    from nexus.catalog.factory import make_catalog_writer

    out = make_catalog_writer().set_sensitivity([tumbler], True, reason="test: flagged before the reindex")
    assert out["updated"] == [1], out


def _setup(mock_db, tmp_path: Path, collection: str, *, flagged: bool) -> tuple[Path, str]:
    doc = tmp_path / "memo.md"
    doc.write_text("# Memo\nthe harbour lease, second draft\n")
    vr = _setup_reindex_mock(mock_db, [{"source_path": str(doc)}], [{"source_path": str(doc)}])
    tumbler = _register_in(doc, collection)
    if flagged:
        _flag(tumbler)
    mock_db._vr = vr
    return doc, tumbler


def _run(runner, mock_db, name: str, *extra: str):
    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.db.collection_purge.purge_collection_cascade", return_value=CascadeCounts()) as purge, \
         patch("nexus.doc_indexer.index_markdown", return_value=1) as index_md, \
         patch("nexus.db.t3.verify_collection_deep", return_value=mock_db._vr):
        result = runner.invoke(main, ["collection", "reindex", name, *extra])
    return result, purge, index_md


def test_a_flagged_document_in_a_voyage_collection_refuses_before_the_delete(runner, env_creds, mock_db, tmp_path):
    doc, tumbler = _setup(mock_db, tmp_path, _VOYAGE, flagged=True)

    result, purge, index_md = _run(runner, mock_db, _VOYAGE)

    assert result.exit_code != 0, result.output
    assert "--ack-external-embedder" in result.output and tumbler in result.output, result.output
    assert "Nothing was deleted" in result.output
    purge.assert_not_called()
    index_md.assert_not_called()


def test_the_ack_lets_the_rebuild_run_and_tells_the_indexer(runner, env_creds, mock_db, tmp_path):
    doc, _ = _setup(mock_db, tmp_path, _VOYAGE, flagged=True)

    result, purge, index_md = _run(runner, mock_db, _VOYAGE, "--ack-external-embedder")

    assert result.exit_code == 0, result.output
    assert "acknowledged" in result.output
    purge.assert_called_once()
    assert index_md.call_args.kwargs.get("ack_external_embedder") is True


def test_an_unflagged_voyage_collection_and_a_flagged_bge_collection_rebuild_without_the_flag(
    runner, env_creds, mock_db, tmp_path,
):
    _setup(mock_db, tmp_path, _VOYAGE, flagged=False)
    result, purge, _ = _run(runner, mock_db, _VOYAGE)
    assert result.exit_code == 0, result.output
    purge.assert_called_once()

    (tmp_path / "bge").mkdir()
    _setup(mock_db, tmp_path / "bge", _BGE, flagged=True)
    result, purge, _ = _run(runner, mock_db, _BGE)
    assert result.exit_code == 0, result.output
    purge.assert_called_once()


def test_a_catalog_that_cannot_list_the_flags_refuses_before_the_delete(
    runner, env_creds, mock_db, tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    _setup(mock_db, tmp_path, _VOYAGE, flagged=False)

    def boom(self, *a, **k):
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient.list_by_collection", boom)

    result, purge, index_md = _run(runner, mock_db, _VOYAGE)

    assert result.exit_code != 0, result.output
    assert "could not read" in result.output and "Nothing was deleted" in result.output, result.output
    purge.assert_not_called()


def test_the_rdr_branch_counts_the_sources_it_indexed_not_the_ones_it_was_handed(
    runner, env_creds, mock_db, tmp_path,
):
    name = "rdr__rfvyy-reindex__bge-base-en-v15-768__v1"
    doc, _ = _setup(mock_db, tmp_path, name, flagged=False)
    with patch("nexus.commands.collection._t3", return_value=mock_db), \
         patch("nexus.db.collection_purge.purge_collection_cascade", return_value=CascadeCounts()), \
         patch("nexus.doc_indexer.batch_index_markdowns", return_value={str(doc): "skipped"}), \
         patch("nexus.db.t3.verify_collection_deep", return_value=mock_db._vr):
        result = runner.invoke(main, ["collection", "reindex", name])
    assert result.exit_code == 0, result.output
    assert "(0 sources processed)" in result.output, result.output
    assert "1 source(s) were skipped" in result.output, result.output
