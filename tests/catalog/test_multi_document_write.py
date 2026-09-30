# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z0o2p.19 (RDR-223 P2.9): :class:`MultiDocumentImportWriter`, the page-by-page multi-document
writer the ``.nxexp`` import drives.

A scripted fake stands in for the catalog client HERE because the properties under test are ones the
engine only exhibits rarely: the order and shape of the requests (which request carries a document's
sweep and stamp, what a resumed document is sent as), what is sent (a drop list minus what the run
wrote, capped at 300), and what the writer refuses when an answer lacks a field (no old-engine
degrade). Every property that lives on the engine side of the wire (a chunk never lands without an
owner, vectors byte-identical, ``complete`` after an import) is pinned against the real engine in
``tests/test_z0o2p19_nxexp_import_combined_write.py``.
"""
from __future__ import annotations

import pytest

from nexus.catalog.multi_document_write import MultiDocumentImportWriter
from nexus.errors import BatchWriteFailedError

_COLL = "code__fake__bge-base-en-v15-768__v1"
_H = "f" * 64


def _c(n: int) -> str:
    return f"{n:064x}"


class FakeCat:
    """Records every call as ``(op, detail)`` in order. ``prior`` scripts each document's pre-run
    manifest (the begin snapshot); ``fail_docs`` / ``refuse`` / ``omit`` script the failures;
    ``embed`` makes the engine report embedding (the vectors were ignored)."""

    def __init__(self, *, prior=None, fail_docs=(), refuse=(), omit=(), embed=0, fail_fence=False):
        self.calls: list[tuple[str, dict]] = []
        self.prior = prior or {}
        self.fail_docs = set(fail_docs)
        self.refuse = set(refuse)
        self.omit = set(omit)
        self.embed = embed
        self.fail_fence = fail_fence

    def begin_index_run_many(self, docs, collection, *, snapshot_manifest=False):
        self.calls.append(("begin", {"docs": [d["doc_id"] for d in docs], "snapshot": snapshot_manifest}))
        if "begin" in self.omit:
            return {}
        out = {"docs": len(docs), "failed_doc_ids": []}
        if "snapshots" not in self.omit:
            out["snapshots"] = {
                d["doc_id"]: {"prior_chashes": list(self.prior.get(d["doc_id"], [])),
                              "prior_count": len(self.prior.get(d["doc_id"], []))}
                for d in docs}
        return out

    def _refused(self, stamped):
        return [{"doc_id": d, "referenced": 1, "missing": 0, "chunk_count": n}
                for d, n in stamped if d in self.refuse]

    def write_manifest_many(self, docs, complete=None, *, sweep=False, chunks=None, collection,
                            force_re_embed=False, embedding_model=None, metadata_merge=False, **kw):
        self.calls.append(("write_many", {
            "docs": {d: rows for d, rows in docs}, "sweep": sweep, "complete": complete,
            "chunks": chunks, "embedding_model": embedding_model, "force": force_re_embed,
            "merge": metadata_merge}))
        refused = self._refused([(d, len(rows)) for d, rows in docs if complete and d in complete])
        return {"failed_doc_ids": [d for d, _ in docs if d in self.fail_docs],
                "chunks_written": len(chunks or ()), "vectors_supplied": len(chunks or ()),
                "embed_embedded": self.embed, "vector_mismatches": 2 if chunks else 0,
                "complete_refused": refused, "complete_refused_count": len(refused)}

    def append_manifest_many(self, docs, *, collection, chunks=None, sweep_chashes=None, complete=None,
                             force_re_embed=False, embedding_model=None, metadata_merge=False, **kw):
        self.calls.append(("append_many", {
            "docs": {d: rows for d, rows in docs}, "chunks": chunks, "sweep_chashes": sweep_chashes,
            "complete": complete, "force": force_re_embed, "merge": metadata_merge}))
        refused = self._refused([(d, n) for d, (h, n) in (complete or {}).items()])
        out = {"failed_doc_ids": [d for d, _ in docs if d in self.fail_docs],
               "chunks_written": len(chunks or ()), "swept": 0, "sweep_skipped": 0,
               "embed_embedded": self.embed, "complete_refused": refused,
               "complete_refused_count": len(refused)}
        if chunks and "unreferenced" not in self.omit:
            out["chunks_unreferenced"] = 0
        return out

    def fail_index_run(self, doc_id, error):
        self.calls.append(("fail", {"doc": doc_id, "error": error}))
        if self.fail_fence:
            raise RuntimeError("engine down")

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _writer(cat: FakeCat, **kw) -> MultiDocumentImportWriter:
    return MultiDocumentImportWriter(
        cat, collection=_COLL, content_hash=_H, embedding_model="bge-base-en-v15-768",
        force_re_embed=True, metadata_merge=True, **kw)


def _doc(w: MultiDocumentImportWriter, doc: str, total: int, *, maxpos: int | None = None, resume=False):
    w.register_document(doc, total_rows=total, max_position=total - 1 if maxpos is None else maxpos,
                        resume=resume)


def _rows(w: MultiDocumentImportWriter, doc: str, *pairs: tuple[int, str]) -> list[dict]:
    return [{"chash": c, "position": w.claim_position(doc, p)} for p, c in pairs]


def _chunk(c: str) -> dict:
    return {"chash": c, "text": f"text {c[-4:]}", "metadata": {}, "embedding": [0.5, 0.25]}


def test_a_multi_page_document_is_replaced_appended_and_stamped_on_its_own_last_append():
    cat = FakeCat()
    w = _writer(cat)
    _doc(w, "1.1.1", 3)
    _doc(w, "1.1.2", 1)
    a1, a2, a3, b1 = _c(1), _c(2), _c(3), _c(4)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, a1))}, {a1: _chunk(a1)})
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (1, a2)), "1.1.2": _rows(w, "1.1.2", (0, b1))},
                       {a2: _chunk(a2), b1: _chunk(b1)})
    # Doc 1.1.2 is a single-request document: written with sweep on and its stamp; nothing else stamps yet.
    assert cat.ops() == ["begin", "write_many", "begin", "write_many", "append_many"]
    writes = [c for op, c in cat.calls if op == "write_many"]
    assert writes[0]["sweep"] is False and writes[0]["complete"] is None       # 1.1.1's first page
    assert writes[0]["embedding_model"] == "bge-base-en-v15-768" and writes[0]["force"] and writes[0]["merge"]
    assert writes[0]["chunks"] == [_chunk(a1)]                                  # the vector rides with it
    assert writes[-1]["sweep"] is True and writes[-1]["complete"] == {"1.1.2": _H}
    assert res.finished == ["1.1.2"]
    app = [c for op, c in cat.calls if op == "append_many"]
    assert list(app[0]["docs"]) == ["1.1.1"] and app[0]["complete"] is None     # not its last page
    # Its last page: the append carries the stamp with the ROW count.
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (2, a3))}, {a3: _chunk(a3)})
    app = [c for op, c in cat.calls if op == "append_many"]
    assert app[-1]["complete"] == {"1.1.1": (_H, 3)}
    assert res.finished == ["1.1.1"]
    done = w.finish()
    assert sorted(done.completed) == ["1.1.1", "1.1.2"] and not done.failed
    assert w.rows_landed == 4


def test_a_documents_sweep_comes_from_the_begin_snapshot_minus_what_the_run_wrote():
    d1, d2, keep = _c(10), _c(11), _c(12)
    cat = FakeCat(prior={"1.1.1": [d1, d2, keep]})
    w = _writer(cat)
    _doc(w, "1.1.1", 2)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    assert [c for op, c in cat.calls if op == "begin"][0]["snapshot"] is True
    w.write_page({"1.1.1": _rows(w, "1.1.1", (1, keep))}, {keep: _chunk(keep)})   # re-added on the last page
    last = [c for op, c in cat.calls if op == "append_many"][-1]
    assert last["sweep_chashes"] == {"1.1.1": [d1, d2]}
    assert last["complete"] == {"1.1.1": (_H, 2)}                # sweep and stamp ride ONE request
    assert cat.ops().count("append_many") == 1


def test_a_drop_list_over_the_cap_continues_in_trailing_sweeps_and_the_stamp_rides_the_last():
    dropped = [_c(1000 + i) for i in range(650)]
    cat = FakeCat(prior={"1.1.1": dropped})
    w = _writer(cat)
    _doc(w, "1.1.1", 2)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2)))}, {_c(2): _chunk(_c(2))})
    appends = [c for op, c in cat.calls if op == "append_many"]
    assert [len(a["sweep_chashes"]["1.1.1"]) for a in appends] == [300, 300, 50]
    assert [x for a in appends for x in a["sweep_chashes"]["1.1.1"]] == dropped
    assert [a["complete"] for a in appends] == [None, None, {"1.1.1": (_H, 2)}]
    assert appends[1]["docs"] == {"1.1.1": []}                   # sweep-only
    assert res.finished == ["1.1.1"]


def test_a_single_request_document_with_a_prior_manifest_is_swept_by_the_engine_not_the_client():
    cat = FakeCat(prior={"1.1.1": [_c(50)]})
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    (only,) = [c for op, c in cat.calls if op == "write_many"]
    assert only["sweep"] is True and only["complete"] == {"1.1.1": _H}
    assert "append_many" not in cat.ops()


def test_a_resumed_document_is_appended_from_its_first_page_and_never_replaced_or_swept():
    cat = FakeCat(prior={"1.1.1": [_c(90), _c(91)]})     # the dead run's rows: the snapshot is ignored
    w = _writer(cat)
    _doc(w, "1.1.1", 2, resume=True)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2)))}, {_c(2): _chunk(_c(2))})
    assert "write_many" not in cat.ops()
    appends = [c for op, c in cat.calls if op == "append_many"]
    assert [a["sweep_chashes"] for a in appends] == [None, None]
    assert [a["complete"] for a in appends] == [None, {"1.1.1": (_H, 2)}]
    assert res.finished == ["1.1.1"]


def test_claim_position_keeps_a_free_position_and_sends_a_collider_past_the_highest_one():
    w = _writer(FakeCat())
    _doc(w, "d", 5, maxpos=4)
    assert [w.claim_position("d", p) for p in (2, 0, 1)] == [2, 0, 1]   # legitimate rows never move
    assert w.claim_position("d", 1) == 5                                # the collider goes to the tail
    assert w.claim_position("d", 0) == 6
    _doc(w, "other", 1, maxpos=0)
    assert w.claim_position("other", 0) == 0                            # positions are per document


def test_a_row_whose_position_was_not_claimed_is_refused_before_anything_is_sent():
    cat = FakeCat()
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    with pytest.raises(ValueError, match="claim_position"):
        w.write_page({"1.1.1": [{"chash": _c(1), "position": 3}]}, {_c(1): _chunk(_c(1))})
    assert cat.calls == []


def test_a_document_the_engine_fails_is_marked_and_skipped_while_its_sibling_goes_on():
    cat = FakeCat(fail_docs={"1.1.2"})
    w = _writer(cat)
    _doc(w, "1.1.1", 2)
    _doc(w, "1.1.2", 2)
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                       {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    assert res.written == ["1.1.1"] and "1.1.2" in res.failed
    assert ("fail", {"doc": "1.1.2", "error": res.failed["1.1.2"]}) in cat.calls
    again = w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(3))), "1.1.2": _rows(w, "1.1.2", (1, _c(4)))},
                         {_c(3): _chunk(_c(3)), _c(4): _chunk(_c(4))})
    assert again.written == ["1.1.1"] and "1.1.2" in again.failed and again.finished == ["1.1.1"]
    done = w.finish()
    assert done.completed == ["1.1.1"] and "1.1.2" in done.failed


def test_a_refused_stamp_is_reported_and_leaves_the_fence_alone():
    cat = FakeCat(refuse={"1.1.1"})
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    _doc(w, "1.1.2", 1)
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                       {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    assert res.finished == ["1.1.2"]
    done = w.finish()
    assert done.completed == ["1.1.2"] and "refused" in done.failed["1.1.1"]
    assert not [c for op, c in cat.calls if op == "fail"]
    w.abort("later")                                   # finished: a no-op
    assert not [c for op, c in cat.calls if op == "fail"]


def test_a_document_that_never_reached_its_last_page_is_reported_and_abort_marks_it_failed():
    cat = FakeCat()
    w = _writer(cat)
    _doc(w, "1.1.1", 3)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    w.abort("boom")
    assert [c["doc"] for op, c in cat.calls if op == "fail"] == ["1.1.1"]
    w.abort("boom")
    assert cat.ops().count("fail") == 1


def test_abort_stops_after_the_first_fence_call_that_fails():
    cat = FakeCat(fail_fence=True)
    w = _writer(cat)
    for i in range(5):
        _doc(w, f"1.1.{i}", 2)
    w.write_page({f"1.1.{i}": _rows(w, f"1.1.{i}", (0, _c(i))) for i in range(5)},
                 {_c(i): _chunk(_c(i)) for i in range(5)})
    w.abort("boom")
    assert cat.ops().count("fail") == 1, "one un-retried call per document against a down engine is a storm"


def test_an_unfinished_document_is_reported_by_finish():
    w = _writer(FakeCat())
    _doc(w, "1.1.1", 3)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    done = w.finish()
    assert "never reached its last page" in done.failed["1.1.1"]


def test_rows_arriving_after_a_documents_last_page_fail_that_document():
    cat = FakeCat()
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2)))}, {_c(2): _chunk(_c(2))})
    assert "after its last page" in res.failed["1.1.1"]


@pytest.mark.parametrize("omit,match", [
    ({"begin"}, "failed_doc_ids"),
    ({"snapshots"}, "snapshots"),
])
def test_a_begin_answer_missing_a_field_the_protocol_needs_is_an_error_not_a_degrade(omit, match):
    w = _writer(FakeCat(omit=omit))
    _doc(w, "1.1.1", 1)
    with pytest.raises(BatchWriteFailedError, match=match):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})


def test_an_append_answer_without_chunks_unreferenced_is_an_error():
    w = _writer(FakeCat(omit={"unreferenced"}))
    _doc(w, "1.1.1", 2)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    with pytest.raises(BatchWriteFailedError, match="chunks_unreferenced"):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2)))}, {_c(2): _chunk(_c(2))})


def test_an_engine_that_embedded_although_vectors_were_supplied_fails_the_import():
    w = _writer(FakeCat(embed=3))
    _doc(w, "1.1.1", 2)
    with pytest.raises(BatchWriteFailedError, match="embedded 3 chunk"):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})


def test_an_append_that_embedded_although_vectors_were_supplied_fails_the_import():
    cat = FakeCat()
    w = _writer(cat)
    _doc(w, "1.1.1", 2)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    cat.embed = 1
    with pytest.raises(BatchWriteFailedError, match="embedded 1 chunk"):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2)))}, {_c(2): _chunk(_c(2))})


def test_the_page_result_carries_the_engines_vector_mismatch_count():
    w = _writer(FakeCat())
    _doc(w, "1.1.1", 1)
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    assert res.vector_mismatches == 2


def test_content_hash_is_required_and_an_unregistered_document_is_refused():
    with pytest.raises(ValueError, match="content_hash"):
        MultiDocumentImportWriter(FakeCat(), collection=_COLL, content_hash="")
    w = _writer(FakeCat())
    with pytest.raises(ValueError, match="not registered"):
        w.write_page({"9.9.9": [{"chash": _c(1), "position": 0}]}, {})
