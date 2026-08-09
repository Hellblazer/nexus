# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wxjr6: pure combined-write payload construction (indexer.py).

``_build_combined_write_payload`` and ``_aggregate_flush_metas`` are
module-level (moved out of ``_run_index``'s nested closures specifically
so they are independently testable — no network, no catalog, no
ChunkBatcher instance required). Design of record: T2
``design-kl2z6-combined-write`` REV 2 §1.1/§1.4/§2.

TestMixedIdentityBatch (code review Critical C1, 2026-08-09, review T2
[22014]) covers the fix for a REAL regression: the engine's
``upsertManifestChunkVectors`` (``CatalogRepository.java`` ~3966-3984)
only persists chashes referenced by a doc's OWN manifest ``rows`` — the
``resolved`` chash map it receives is the FULL flush-wide ``chunks``
payload, a strict superset. Verified directly against the Java source
(``chashes`` is built by iterating ``rows``, never ``resolved``'s own
keys) before writing this fix. Pre-fix, ``_build_combined_write_payload``
put EVERY chunk in ``chunks_payload`` regardless of catalog identity
while ``full_docs`` only ever covered identity-having docs — confirmed
by inspecting the pre-fix committed version (git show a3b115d0) directly:
a MIXED flush (some files with catalog identity, some without — routine,
since ``code_indexer.py`` resolves ``catalog_doc_id`` per FILE and stages
the file into the batcher regardless of resolution success) silently lost
identity-less content: embedded (Voyage cost paid), then discarded
server-side with no log, no counter, no error.
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
    context= payload. doc_id="" mirrors a file whose catalog registration
    did not resolve (still staged into the batcher regardless — the
    condition TestMixedIdentityBatch exercises)."""
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
        chunks, full_docs, complete, orphan_ids, orphan_docs, orphan_metas = (
            _build_combined_write_payload(ids, docs, metas, fctx)
        )
        assert chunks == [
            {"chash": ids[0], "text": docs[0], "metadata": metas[0]},
            {"chash": ids[1], "text": docs[1], "metadata": metas[1]},
        ]
        assert orphan_ids == orphan_docs == orphan_metas == []

    def test_chunks_deduped_by_chash_first_wins(self) -> None:
        # Same chash referenced by two files (identical chunk text) in
        # one flush — design memo §1.1: transmitted ONCE.
        shared_id = "d" * 64
        fctx = [
            ("a.py", {"ids": [shared_id], "documents": ["shared"], "metadatas": [{"content_hash": "h1" * 32}], "catalog_doc_id": "1.1"}),
            ("b.py", {"ids": [shared_id], "documents": ["shared-again"], "metadatas": [{"content_hash": "h2" * 32}], "catalog_doc_id": "1.2"}),
        ]
        ids, docs, metas = _mk_flush(fctx)
        chunks, _full_docs, _complete, _oi, _od, _om = _build_combined_write_payload(ids, docs, metas, fctx)
        assert len(chunks) == 1
        assert chunks[0]["chash"] == shared_id
        assert chunks[0]["text"] == "shared"  # first occurrence wins

    def test_full_docs_grouped_by_doc_id_with_position_0(self) -> None:
        fctx = [_file_ctx("a.py", "1.1", 3, prefix="a"), _file_ctx("b.py", "1.2", 1, prefix="b")]
        ids, docs, metas = _mk_flush(fctx)
        _chunks, full_docs, _complete, _oi, _od, _om = _build_combined_write_payload(ids, docs, metas, fctx)
        by_id = dict(full_docs)
        assert sorted(by_id) == ["1.1", "1.2"]
        assert [r["position"] for r in by_id["1.1"]] == [0, 1, 2]
        assert [r["position"] for r in by_id["1.2"]] == [0]

    def test_all_orphan_batch_excluded_from_full_docs_and_chunks(self) -> None:
        # An ALL-orphan flush (every file lacks catalog identity): no
        # content is silently lost — everything routes to the orphan
        # return values, chunks_payload/full_docs are both empty. The
        # caller (_batch_flush) sends orphan_ids through the legacy
        # upsert-chunks call AND still logs combined_write_batch_missing_
        # doc_identity for the (now-empty) combined-write side.
        fctx = [
            ("a.py", {"ids": ["e" * 64], "documents": ["x"], "metadatas": [{"content_hash": "h" * 64}], "catalog_doc_id": ""}),
        ]
        ids, docs, metas = _mk_flush(fctx)
        chunks, full_docs, complete, orphan_ids, orphan_docs, orphan_metas = (
            _build_combined_write_payload(ids, docs, metas, fctx)
        )
        assert chunks == []
        assert full_docs == []
        assert complete == {}
        assert orphan_ids == ["e" * 64]
        assert orphan_docs == ["x"]
        assert orphan_metas == [{"content_hash": "h" * 64}]

    def test_complete_map_restricted_to_full_docs_with_content_hash(self) -> None:
        fctx = [
            _file_ctx("a.py", "1.1", 1, prefix="a", content_hash="h" * 64),
            _file_ctx("b.py", "1.2", 1, prefix="b", content_hash=""),
        ]
        ids, docs, metas = _mk_flush(fctx)
        _chunks, full_docs, complete, _oi, _od, _om = _build_combined_write_payload(ids, docs, metas, fctx)
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
        chunks, full_docs, complete, orphan_ids, orphan_docs, orphan_metas = (
            _build_combined_write_payload([], [], [], [])
        )
        assert chunks == []
        assert full_docs == []
        assert complete == {}
        assert orphan_ids == []
        assert orphan_docs == []
        assert orphan_metas == []


class TestMixedIdentityBatch:
    """Code review Critical C1 (2026-08-09, review T2 [22014]): a flush
    with SOME identity-having files and SOME identity-less files. Before
    the fix, identity-less content silently landed in ``chunks_payload``
    (sent to the engine, embedded, then discarded — no doc's ``rows``
    ever referenced it). The fix splits the flush BEFORE building
    ``chunks_payload`` so identity-less content never enters it at all.
    """

    def test_falsifies_against_the_pre_fix_shape(self) -> None:
        """Reconstructs the PRE-FIX function body (git show a3b115d0)
        against this exact fixture and asserts it WOULD have put the
        orphan chash in chunks_payload — proving the bug was real before
        asserting the fix below removes it. Not testing production code;
        this is the falsification record the review demanded, kept
        in-repo rather than only in session scrollback.
        """
        fctx = [
            _file_ctx("has_id.py", "1.1", 1, prefix="a"),
            _file_ctx("no_id.py", "", 1, prefix="b"),
        ]
        ids, docs, metas = _mk_flush(fctx)
        orphan_chash = ids[1]  # "no_id.py"'s single chunk

        # Pre-fix logic, verbatim (chunks_payload built from the FULL
        # flush, no identity split):
        pre_fix_chunks_payload = []
        seen = set()
        for cid, cdoc, cmeta in zip(ids, docs, metas):
            if cid in seen:
                continue
            seen.add(cid)
            pre_fix_chunks_payload.append({"chash": cid, "text": cdoc, "metadata": cmeta})
        pre_fix_chunk_ids = {c["chash"] for c in pre_fix_chunks_payload}

        assert orphan_chash in pre_fix_chunk_ids, (
            "pre-fix reconstruction did not reproduce the bug — fixture "
            "or reconstruction drifted from the reviewed commit"
        )

    def test_identity_less_chunks_excluded_from_chunks_payload(self) -> None:
        fctx = [
            _file_ctx("has_id.py", "1.1", 1, prefix="a"),
            _file_ctx("no_id.py", "", 1, prefix="b"),
        ]
        ids, docs, metas = _mk_flush(fctx)
        orphan_chash = ids[1]

        chunks, full_docs, complete, orphan_ids, orphan_docs, orphan_metas = (
            _build_combined_write_payload(ids, docs, metas, fctx)
        )
        chunk_payload_ids = {c["chash"] for c in chunks}
        assert orphan_chash not in chunk_payload_ids, (
            "identity-less chunk leaked into chunks_payload — the engine "
            "will embed it and silently discard it (no doc references it)"
        )
        assert chunk_payload_ids == {ids[0]}

    def test_identity_less_chunks_durably_accounted_via_orphan_return(self) -> None:
        fctx = [
            _file_ctx("has_id.py", "1.1", 1, prefix="a"),
            _file_ctx("no_id.py", "", 1, prefix="b"),
        ]
        ids, docs, metas = _mk_flush(fctx)
        chunks, full_docs, complete, orphan_ids, orphan_docs, orphan_metas = (
            _build_combined_write_payload(ids, docs, metas, fctx)
        )
        assert orphan_ids == [ids[1]]
        assert orphan_docs == [docs[1]]
        assert orphan_metas == [metas[1]]
        # The identity-having file's doc is unaffected.
        assert dict(full_docs)["1.1"][0]["chash"] == ids[0]

    def test_shared_chash_across_identity_and_orphan_file_stays_in_combined_payload(self) -> None:
        # A chash claimed by BOTH an identity file and an identity-less
        # file (duplicate chunk text across files, routine boilerplate)
        # is SAFELY referenceable via the identity file's manifest row —
        # it must stay in chunks_payload, not be treated as orphan.
        shared_id = "f" * 64
        fctx = [
            ("has_id.py", {"ids": [shared_id], "documents": ["shared"], "metadatas": [{"content_hash": "h1" * 32, "chunk_text_hash": shared_id}], "catalog_doc_id": "1.1"}),
            ("no_id.py", {"ids": [shared_id], "documents": ["shared"], "metadatas": [{"content_hash": "h2" * 32, "chunk_text_hash": shared_id}], "catalog_doc_id": ""}),
        ]
        ids, docs, metas = _mk_flush(fctx)
        chunks, full_docs, complete, orphan_ids, orphan_docs, orphan_metas = (
            _build_combined_write_payload(ids, docs, metas, fctx)
        )
        assert {c["chash"] for c in chunks} == {shared_id}
        assert orphan_ids == []
        assert dict(full_docs)["1.1"][0]["chash"] == shared_id

    def test_multiple_orphan_files_all_accounted(self) -> None:
        fctx = [
            _file_ctx("has_id.py", "1.1", 2, prefix="a"),
            _file_ctx("no_id_1.py", "", 1, prefix="b"),
            _file_ctx("no_id_2.py", "", 1, prefix="c"),
        ]
        ids, docs, metas = _mk_flush(fctx)
        chunks, full_docs, complete, orphan_ids, orphan_docs, orphan_metas = (
            _build_combined_write_payload(ids, docs, metas, fctx)
        )
        assert len(chunks) == 2  # only has_id.py's 2 chunks
        assert sorted(orphan_ids) == sorted(ids[2:4])  # both no_id files' chunks
        assert len(orphan_docs) == 2
        assert len(orphan_metas) == 2
