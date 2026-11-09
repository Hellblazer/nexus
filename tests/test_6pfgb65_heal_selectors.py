# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 4 Step 1 (nexus-6pfgb.65): the manifest heal's ``content_hash`` selector is blind to a chunk row
written since Step 1 (a row carries only its CHUNK keys), so "no chunk carries its content hash" is no evidence
that a manifest-less document lost its chunks. The heal says so and leaves the fence alone.

The engine-backed behaviour of the rest of the heal is pinned by ``tests/integration/test_8o06q_manifest_heal_keeps_rows.py``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from nexus.catalog.manifest_heal import heal_manifest_gaps

_COLL = "docs__heal65__bge-base-en-v15-768__v1"
_CH = "ab" * 32


def _entry(tumbler: str = "1.1.9", chunk_count: int = 2):
    return SimpleNamespace(
        tumbler=tumbler, chunk_count=chunk_count, meta={"content_hash": "f" * 64},
        physical_collection=_COLL, index_state="complete", index_content_hash="")


class _Reader:
    def get_manifests(self, doc_ids):
        return {}

    def by_doc_id(self, tumbler):
        return _entry(str(tumbler))


def _run(monkeypatch, fetched: dict):
    writer = MagicMock()
    monkeypatch.setattr("nexus.indexer._paginated_get", lambda col, **kw: fetched)
    t3 = MagicMock()
    result = heal_manifest_gaps([_entry()], _Reader(), lambda: t3, lambda: writer)
    return result, writer


def test_a_manifestless_document_with_no_chunk_under_its_content_hash_is_reported_not_marked(monkeypatch) -> None:
    result, writer = _run(monkeypatch, {"ids": [], "metadatas": []})
    assert [str(e.tumbler) for e in result.lost] == ["1.1.9"], "non-vacuity: the document is reported"
    writer.fail_index_run.assert_not_called()
    assert result.reindex == []
    why = result.lost_unmarked_why["1.1.9"]
    assert "content hash" in why and "RDR-236" in why and "re-index" in why, why


def test_a_manifestless_document_with_legacy_rows_is_still_rebuilt_from_them(monkeypatch) -> None:
    fetched = {
        "ids": [_CH, "cd" * 32],
        "metadatas": [
            {"content_hash": "f" * 64, "chunk_text_hash": _CH, "chunk_start_char": 0, "chunk_end_char": 5},
            {"content_hash": "f" * 64, "chunk_text_hash": "cd" * 32, "chunk_start_char": 5, "chunk_end_char": 9},
        ],
    }
    result, writer = _run(monkeypatch, fetched)
    assert result.reconciled == 1 and result.lost == []
    writer.append_manifest_chunks.assert_called_once()
    writer.fail_index_run.assert_not_called()
