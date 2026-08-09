# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wxjr6: pure combined-write payload construction (indexer.py).

``_build_combined_write_payload`` and ``_aggregate_flush_metas`` are
module-level (moved out of ``_run_index``'s nested closures specifically
so they are independently testable — no network, no catalog, no
ChunkBatcher instance required). Design of record: T2
``design-kl2z6-combined-write`` REV 2 §1.1/§1.4/§2.
"""

from __future__ import annotations

import pytest

from nexus.indexer import (
    _CombinedWritePositionZeroViolation,
    _aggregate_flush_metas,
    _build_combined_write_payload,
)


def _file_ctx(
    path: str, doc_id: str, n_chunks: int, *, prefix: str = "c",
    content_hash: str = "h" * 64,
) -> tuple[str, dict]:
    """One ChunkBatcher on_batch_begin/on_batch_complete-shaped
    (path, context) entry — mirrors code_indexer.py's ctx.batcher.add()
    context= payload."""
    ids = [f"{prefix}{i:02d}" + "a" * 62 for i in range(n_chunks)]
    docs = [f"text-{prefix}{i}" for i in range(n_chunks)]
    # chunk_text_hash mirrors production reality (RDR-180: the chash IS
    # the id) — _manifest_chunk_rows reads this field, not "content_hash"
    # (a document-level field, distinct from the per-chunk chash).
    metas = [
        {"content_hash": content_hash, "chunk_text_hash": _cid}
        for _cid in ids
    ]
    return path, {
        "ids": ids,
        "documents": docs,
        "metadatas": metas,
        "catalog_doc_id": doc_id,
    }


class TestAggregateFlushMetas:
    def test_injects_doc_id_and_local_chunk_index(self) -> None:
        fctx = [_file_ctx("a.py", "1.1", 3, prefix="a"), _file_ctx("b.py", "1.2", 2, prefix="b")]
        agg = _aggregate_flush_metas(fctx)
        assert [m["doc_id"] for m in agg] == ["1.1", "1.1", "1.1", "1.2", "1.2"]
        assert [m["chunk_index"] for m in agg] == [0, 1, 2, 0, 1]

    def test_non_dict_context_entries_skipped(self) -> None:
        fctx = [_file_ctx("a.py", "1.1", 2, prefix="a"), ("b.py", None)]
        agg = _aggregate_flush_metas(fctx)
        assert len(agg) == 2

    def test_empty_file_contexts_returns_empty(self) -> None:
        assert _aggregate_flush_metas([]) == []


def _mk_flush(fctx: list) -> tuple[list, list, list]:
    """Build the (ids, docs, metas) a ChunkBatcher flush would carry —
    the flattened union across every file in fctx, in file order."""
    ids: list = []
    docs: list = []
    metas: list = []
    for _path, c in fctx:
        ids.extend(c["ids"])
        docs.extend(c["documents"])
        metas.extend(c["metadatas"])
    return ids, docs, metas


class TestBuildCombinedWritePayload:
    def test_chunks_payload_shape(self) -> None:
        fctx = [_file_ctx("a.py", "1.1", 2, prefix="a")]
        ids, docs, metas = _mk_flush(fctx)
        chunks, full_docs, complete = _build_combined_write_payload(ids, docs, metas, fctx)
        assert chunks == [
            {"chash": ids[0], "text": docs[0], "metadata": metas[0]},
            {"chash": ids[1], "text": docs[1], "metadata": metas[1]},
        ]

    def test_chunks_deduped_by_chash_first_wins(self) -> None:
        # Same chash referenced by two files (identical chunk text) in
        # one flush — design memo §1.1: transmitted ONCE.
        shared_id = "d" * 64
        fctx = [
            ("a.py", {"ids": [shared_id], "documents": ["shared"], "metadatas": [{"content_hash": "h1" * 32}], "catalog_doc_id": "1.1"}),
            ("b.py", {"ids": [shared_id], "documents": ["shared-again"], "metadatas": [{"content_hash": "h2" * 32}], "catalog_doc_id": "1.2"}),
        ]
        ids, docs, metas = _mk_flush(fctx)
        chunks, _full_docs, _complete = _build_combined_write_payload(ids, docs, metas, fctx)
        assert len(chunks) == 1
        assert chunks[0]["chash"] == shared_id
        assert chunks[0]["text"] == "shared"  # first occurrence wins

    def test_full_docs_grouped_by_doc_id_with_position_0(self) -> None:
        fctx = [_file_ctx("a.py", "1.1", 3, prefix="a"), _file_ctx("b.py", "1.2", 1, prefix="b")]
        ids, docs, metas = _mk_flush(fctx)
        _chunks, full_docs, _complete = _build_combined_write_payload(ids, docs, metas, fctx)
        by_id = dict(full_docs)
        assert sorted(by_id) == ["1.1", "1.2"]
        assert [r["position"] for r in by_id["1.1"]] == [0, 1, 2]
        assert [r["position"] for r in by_id["1.2"]] == [0]

    def test_docs_missing_catalog_identity_excluded_from_full_docs(self) -> None:
        fctx = [
            ("a.py", {"ids": ["e" * 64], "documents": ["x"], "metadatas": [{"content_hash": "h" * 64}], "catalog_doc_id": ""}),
        ]
        ids, docs, metas = _mk_flush(fctx)
        chunks, full_docs, complete = _build_combined_write_payload(ids, docs, metas, fctx)
        # Content is still carried in chunks_payload (no doc references
        # it, so the caller's identity-drop path is responsible for
        # deciding what happens — this function has no side effects).
        assert len(chunks) == 1
        assert full_docs == []
        assert complete == {}

    def test_complete_map_restricted_to_full_docs_with_content_hash(self) -> None:
        fctx = [
            _file_ctx("a.py", "1.1", 1, prefix="a", content_hash="h" * 64),
            _file_ctx("b.py", "1.2", 1, prefix="b", content_hash=""),
        ]
        ids, docs, metas = _mk_flush(fctx)
        _chunks, full_docs, complete = _build_combined_write_payload(ids, docs, metas, fctx)
        # 1.1 has a content_hash -> claimed complete; 1.2's content_hash
        # is empty -> NOT claimed (empty string is falsy, matches the old
        # _fire_flush_grain_hooks _manifest_complete construction).
        assert complete == {"1.1": "h" * 64}
        assert {d for d, _ in full_docs} == {"1.1", "1.2"}

    def test_position_zero_violation_raises(self, monkeypatch) -> None:
        # Structurally unreachable via the real ChunkBatcher call path
        # (_aggregate_flush_metas always injects a file-local enumerate
        # starting at 0) — this proves the DEFENDED invariant itself is
        # sound by monkeypatching the aggregation step to violate it, the
        # way a future code change might by accident.
        import nexus.indexer as indexer_mod

        def _bad_aggregate(_fctx):
            return [
                {"doc_id": "1.1", "chunk_index": 1, "chunk_text_hash": "a" * 64},
                {"doc_id": "1.1", "chunk_index": 2, "chunk_text_hash": "b" * 64},
            ]

        monkeypatch.setattr(indexer_mod, "_aggregate_flush_metas", _bad_aggregate)
        fctx = [_file_ctx("a.py", "1.1", 2, prefix="a")]
        ids, docs, metas = _mk_flush(fctx)
        with pytest.raises(_CombinedWritePositionZeroViolation, match="lacks position 0"):
            _build_combined_write_payload(ids, docs, metas, fctx)

    def test_no_content_and_no_chunks_returns_all_empty(self) -> None:
        chunks, full_docs, complete = _build_combined_write_payload([], [], [], [])
        assert chunks == []
        assert full_docs == []
        assert complete == {}
