# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 (nexus-z0o2p.13): the side effects ``_index_document`` keeps when its manifest hook is
dropped.

The manifest hook used to feed the run-summary collectors (a refused completion stamp, the sweep
counters); the combined chunk+owner write replaced it, so ``_write_chunks_with_owner_rows`` and
``_index_document`` feed them. The engine round trip has real coverage in
``tests/integration/test_rdr223_index_document_journey.py``; these tests pin the wiring around it
with a recording catalog writer, because the engine cannot be made to refuse a stamp on demand.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.errors import IndexRunVerifyRefused

COLLECTION = "docs__wcor-test__bge-base-en-v15-768__v1"
DOC_ID = "1.7.1"


class _Cat:
    """A catalog writer that answers a one-request write; *resp* overrides fields of the answer."""

    def __init__(self, **resp) -> None:
        self.resp = resp
        self.calls: list[str] = []
        self.failed: list[str] = []

    def begin_index_run(self, doc_id, content_hash, run_id, collection, **kw):
        self.calls.append("begin")
        return {"prior_chashes": [], "prior_count": 0}

    def write_manifest_many(self, docs, complete=None, *, sweep=False, chunks=None, collection,
                            force_re_embed=False, embedding_model=None, metadata_merge=False,
                            metadata_delete_keys=None):
        self.calls.append("write")
        self.merge = (metadata_merge, metadata_delete_keys)
        doc = docs[0][0]
        out = {"chunks_written": len(chunks or []), "failed_doc_ids": [], "complete_refused": [],
               "complete_refused_count": 0, "dropped_chashes": {doc: []}, "dropped_count": {doc: 0}}
        out.update(self.resp)
        return out

    def fail_index_run(self, doc_id, error):
        self.failed.append(doc_id)

    def close(self) -> None:
        pass


def _write(cat: _Cat):
    from nexus.doc_indexer import _write_chunks_with_owner_rows

    text = "wcor chunk"
    chash = hashlib.sha256(text.encode()).hexdigest()
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=cat):
        return _write_chunks_with_owner_rows(
            COLLECTION, DOC_ID, "h" * 64, [chash], [text], [{"chunk_text_hash": chash}])


@pytest.fixture(autouse=True)
def _fresh_collectors():
    from nexus.mcp_infra import reset_complete_refusals, reset_superseded_sweep_stats

    reset_superseded_sweep_stats()
    reset_complete_refusals()
    yield
    reset_superseded_sweep_stats()
    reset_complete_refusals()


def test_the_engines_sweep_counts_reach_the_run_summary() -> None:
    from nexus.mcp_infra import get_superseded_sweep_stats

    result = _write(_Cat(swept=3, sweep_skipped=1))

    assert result.swept == 3 and result.sweep_skipped == 1
    stats = get_superseded_sweep_stats()
    assert stats["swept"] == 3
    assert stats["skipped"] == [
        {"doc_id": DOC_ID, "collection": COLLECTION, "reason": "sweep_failed"}]


def test_the_engines_sweep_skip_reasons_are_kept() -> None:
    """A skipped sweep keeps the engine's own reason (gate_timeout, statement_timeout, ...), not a
    generic one, so the run summary can tell an operator what to do about it."""
    from nexus.mcp_infra import get_superseded_sweep_stats

    _write(_Cat(sweep_skipped=1, sweep_detail=[
        {"doc_id": DOC_ID, "errored": True, "reason": "gate_timeout"},
        {"doc_id": DOC_ID, "errored": False}]))

    assert get_superseded_sweep_stats()["skipped"] == [
        {"doc_id": DOC_ID, "collection": COLLECTION, "reason": "gate_timeout"}]


def test_chunk_metadata_is_merged_naming_the_keys_the_document_dropped() -> None:
    """The combined write merges (stored - delete_keys) || incoming, like the old upsert did."""
    from nexus.doc_indexer import _write_chunks_with_owner_rows
    from nexus.metadata_schema import REWRITE_OWNED_KEYS

    text = "wcor chunk"
    chash = hashlib.sha256(text.encode()).hexdigest()
    cat = _Cat()
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=cat):
        _write_chunks_with_owner_rows(
            COLLECTION, DOC_ID, "h" * 64, [chash], [text],
            [{"chunk_text_hash": chash, "content_hash": "h" * 64}])

    merge, keys = cat.merge
    assert merge is True
    # Every owned key the row dropped is named, and the keys it carries are not.
    assert set(keys) == REWRITE_OWNED_KEYS - {"chunk_text_hash", "content_hash"}
    assert not any(k.startswith("bib_") for k in keys)


def test_a_clean_write_records_no_skip_and_no_refusal() -> None:
    from nexus.mcp_infra import get_complete_refusals, get_superseded_sweep_stats

    _write(_Cat())

    assert get_superseded_sweep_stats()["skipped"] == []
    assert get_complete_refusals() == []


def test_a_refused_stamp_is_recorded_and_propagates_without_marking_the_run_failed(
    tmp_path: Path,
) -> None:
    """The engine refuses the completion stamp: the document is NOT fully indexed. The refusal
    must reach the run summary (nexus-5xn3k.6) and propagate. ``_index_document`` does not call
    ``_fence_fail`` for it (the writer's own abort has already marked the fence)."""
    from nexus.doc_indexer import _index_document
    from nexus.mcp_infra import get_complete_refusals

    f = tmp_path / "doc.md"
    f.write_text("hello wcor")
    text = "wcor chunk"
    chash = hashlib.sha256(text.encode()).hexdigest()

    def chunks(file_path, content_hash, target_model, now_iso, corpus):
        return [(chash, text, {"content_hash": content_hash, "embedding_model": target_model,
                               "chunk_text_hash": chash, "content_type": "prose"})]

    cat = _Cat(complete_refused=[{"doc_id": DOC_ID, "referenced": 1, "missing": 1, "chunk_count": 1}],
               complete_refused_count=1)

    class _Col:
        def get(self, **kw):
            return {"ids": [], "metadatas": []}

    class _T3:
        def get_or_create_collection(self, name):
            return _Col()

    with patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value=DOC_ID), \
            patch("nexus.doc_indexer._register_before_read"), \
            patch("nexus.catalog.factory.make_catalog_writer", return_value=cat), \
            patch("nexus.doc_indexer._fence_fail") as fence_fail:
        with pytest.raises(IndexRunVerifyRefused) as exc:
            _index_document(f, "wcor", chunks, _T3(), collection_name=COLLECTION)

    assert exc.value.doc_id == DOC_ID
    assert get_complete_refusals() == [DOC_ID]
    fence_fail.assert_not_called()
    assert cat.calls == ["begin", "write"]
    # The writer leaves the fence 'indexing' after a refusal: nothing marks it failed.
    assert cat.failed == []
