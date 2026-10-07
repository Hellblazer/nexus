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
from nexus.errors import BatchWriteFailedError, EngineOlderThanClientError
from tests._time_seam import module_time

_COLL = "code__fake__bge-base-en-v15-768__v1"
_H = "f" * 64


def _c(n: int) -> str:
    return f"{n:064x}"


class FakeCat:
    """Records every call as ``(op, detail)`` in order. ``prior`` scripts each document's pre-run
    manifest (the begin snapshot); ``fail_docs`` / ``refuse`` / ``omit`` script the failures;
    ``embed`` makes the engine report embedding (the vectors were ignored)."""

    def __init__(self, *, prior=None, fail_docs=(), refuse=(), omit=(), embed=0, fail_fence=False,
                 old_engine=False, sweep_skipped=0):
        self.calls: list[tuple[str, dict]] = []
        self.prior = prior or {}
        self.fail_docs = set(fail_docs)
        self.refuse = set(refuse)
        self.omit = set(omit)
        self.embed = embed
        self.fail_fence = fail_fence
        self.old_engine = old_engine
        self.sweep_skipped = sweep_skipped

    def begin_index_run_many(self, docs, collection, *, snapshot_manifest=False):
        self.calls.append(("begin", {"docs": [d["doc_id"] for d in docs], "snapshot": snapshot_manifest}))
        if self.old_engine and snapshot_manifest:
            # What the real client does when the engine ignored the flag: the engine HAS stamped the
            # documents (begin-many predates the snapshot) and the client raises.
            raise EngineOlderThanClientError("begin_index_run_many: the engine predates the snapshot")
        if "begin" in self.omit:
            return {}
        out = {"docs": len(docs), "failed_doc_ids": []}
        if snapshot_manifest and "snapshots" not in self.omit:
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
                "sweep_skipped": self.sweep_skipped,
                "complete_refused": refused, "complete_refused_count": len(refused)}

    def append_manifest_many(self, docs, *, collection, chunks=None, sweep_chashes=None, complete=None,
                             force_re_embed=False, embedding_model=None, metadata_merge=False, **kw):
        self.calls.append(("append_many", {
            "docs": {d: rows for d, rows in docs}, "chunks": chunks, "sweep_chashes": sweep_chashes,
            "complete": complete, "force": force_re_embed, "merge": metadata_merge}))
        refused = self._refused([(d, n) for d, (h, n) in (complete or {}).items()])
        out = {"failed_doc_ids": [d for d, _ in docs if d in self.fail_docs],
               "chunks_written": len(chunks or ()), "swept": 0, "sweep_skipped": self.sweep_skipped,
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


# ── fix round 3 (nexus-z0o2p.19): old-engine refusals, resumed-document begin, abort bound ───────────


def test_an_engine_too_old_for_the_snapshot_fails_the_run_and_abort_marks_the_documents_its_begin_stamped():
    """begin-many on such an engine stamps every document ``indexing`` and only then does the client
    notice there is no snapshot. The documents must not be left ``indexing`` by the abort."""
    cat = FakeCat(old_engine=True)
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    _doc(w, "1.1.2", 1)
    with pytest.raises(EngineOlderThanClientError):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                     {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    assert "write_many" not in cat.ops() and "append_many" not in cat.ops()
    w.abort("the engine is older")
    assert sorted(c["doc"] for op, c in cat.calls if op == "fail") == ["1.1.1", "1.1.2"]


def test_an_engine_with_no_fence_route_is_refused_with_the_remedy():
    w = _writer(FakeCat(omit={"begin"}))
    _doc(w, "1.1.1", 1)
    with pytest.raises(BatchWriteFailedError) as exc:
        w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    text = str(exc.value)
    assert "older than this client" in text and "Upgrade the local engine" in text and "cloud engine deploy" in text


def test_resumed_documents_begin_in_their_own_call_without_the_snapshot():
    cat = FakeCat(prior={"1.1.1": [_c(90)]})
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    _doc(w, "1.1.2", 1, resume=True)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                 {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    begins = [c for op, c in cat.calls if op == "begin"]
    assert begins == [{"docs": ["1.1.1"], "snapshot": True}, {"docs": ["1.1.2"], "snapshot": False}], (
        "a resumed document ignores its snapshot, so asking for it pulls an uncapped chash list for nothing")


def test_abort_marks_a_bounded_number_of_fences_and_leaves_the_rest_to_the_rerun():
    cat = FakeCat()
    w = _writer(cat)
    n = MultiDocumentImportWriter.ABORT_FENCE_CAP * 3
    for i in range(n):
        _doc(w, f"1.1.{i}", 2)
    w.write_page({f"1.1.{i}": _rows(w, f"1.1.{i}", (0, _c(i))) for i in range(n)},
                 {_c(i): _chunk(_c(i)) for i in range(n)})
    w.abort("boom")
    assert cat.ops().count("fail") == MultiDocumentImportWriter.ABORT_FENCE_CAP


def test_registering_a_document_again_with_other_figures_is_refused_not_silently_ignored():
    w = _writer(FakeCat())
    w.register_document("1.1.1", total_rows=3, max_position=2)
    w.register_document("1.1.1", total_rows=3, max_position=2)        # the same figures: a no-op
    with pytest.raises(ValueError, match="already registered"):
        w.register_document("1.1.1", total_rows=5, max_position=4)


def test_a_finished_or_failed_document_releases_what_only_an_open_document_needs():
    d1, d2 = _c(10), _c(11)
    cat = FakeCat(prior={"1.1.1": [d1, d2]}, fail_docs={"1.1.2"})
    w = _writer(cat)
    _doc(w, "1.1.1", 2)
    _doc(w, "1.1.2", 2)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                 {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    st = w._docs["1.1.1"]          # white-box: nothing else observes retained memory
    assert st.positions and st.prior, "an open document needs both"
    w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(3)))}, {_c(3): _chunk(_c(3))})
    assert st.stamped
    assert not st.positions and not st.prior and not st.wrote and not st.sweep_rest
    assert not w._docs["1.1.2"].positions, "a failed document is released too"


def test_the_writer_totals_the_engines_sweep_skips_over_the_run():
    cat = FakeCat(sweep_skipped=1)
    w = _writer(cat)
    _doc(w, "1.1.1", 2)
    _doc(w, "1.1.2", 1)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                 {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(3)))}, {_c(3): _chunk(_c(3))})
    assert w.sweep_skipped == 3          # write_many of the multi group, of the single group, one append


# ── request_may_have_written: every ATTEMPT of a DATA request, nothing else (nexus-z0o2p.34) ───────


class _FlakyCat(FakeCat):
    """A FakeCat whose named operation raises the given exceptions, one per call, before it answers."""

    def __init__(self, op: str, *errors: BaseException, **kw):
        super().__init__(**kw)
        self._op, self._errors = op, list(errors)

    def _maybe(self, op: str) -> None:
        if op == self._op and self._errors:
            raise self._errors.pop(0)

    def begin_index_run_many(self, docs, collection, *, snapshot_manifest=False):
        self._maybe("begin")
        return super().begin_index_run_many(docs, collection, snapshot_manifest=snapshot_manifest)

    def write_manifest_many(self, docs, complete=None, **kw):
        self._maybe("write")
        return super().write_manifest_many(docs, complete, **kw)

    def append_manifest_many(self, docs, **kw):
        self._maybe("append")
        return super().append_manifest_many(docs, **kw)


def _httpx_errors():
    import httpx

    req = httpx.Request("POST", "http://engine.invalid/v1/catalog/manifest/write_many")
    return httpx.ReadError("dropped", request=req), httpx.ConnectError("refused", request=req)


def test_every_attempt_of_a_data_request_is_judged_not_only_the_last(monkeypatch):
    """A dropped connection followed by refused reconnects is one request that may have reached the
    engine: the retry wrapper re-raises only the LAST error (a connect error, 'never made'), so the
    writer must have seen the first attempt's in-flight error itself."""
    import pytest as _pytest

    module_time(monkeypatch, "nexus.retry").sleep = lambda s: None
    read_error, connect_error = _httpx_errors()
    cat = _FlakyCat("write", read_error, connect_error, connect_error, connect_error, connect_error)
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    with _pytest.raises(Exception):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    assert w.request_may_have_written() is True


def test_a_request_that_never_left_does_not_count_as_in_flight(monkeypatch):
    import pytest as _pytest

    module_time(monkeypatch, "nexus.retry").sleep = lambda s: None
    _read_error, connect_error = _httpx_errors()
    cat = _FlakyCat("write", *[connect_error] * 8)
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    with _pytest.raises(Exception):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    assert w.request_may_have_written() is False


def test_a_begin_the_old_engine_failed_after_answering_wrote_nothing_so_phantoms_may_go():
    """EngineOlderThanClientError from begin_index_run_many is raised after the engine answered, which
    the classifier calls in flight; but a begin carries no rows or chunks, so nothing can have been
    written and the documents the import registered must be removable."""
    import pytest as _pytest

    cat = _FlakyCat("begin", EngineOlderThanClientError("begin_index_run_many: the engine predates the snapshot"))
    w = _writer(cat)
    _doc(w, "1.1.1", 1)
    with _pytest.raises(EngineOlderThanClientError):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    assert w.request_may_have_written() is False


def test_a_failed_stamp_only_request_does_not_make_the_run_look_in_flight(monkeypatch):
    """A stamp-only append_many carries no rows, so it cannot have created a phantom document."""
    import pytest as _pytest

    module_time(monkeypatch, "nexus.retry").sleep = lambda s: None
    read_error, _connect = _httpx_errors()
    cat = _FlakyCat("append", *[read_error] * 8)
    w = _writer(cat, defer_completion=True)
    _doc(w, "1.1.1", 1)
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    with _pytest.raises(Exception):
        w.complete_documents(res.landed)
    assert w.request_may_have_written() is False


# ── defer_completion (nexus-z0o2p.34): the stamp is the caller's, after its hooks ─────────────────


def test_a_deferred_writer_sends_no_stamp_with_a_data_request_and_lists_the_documents_it_owes():
    cat = FakeCat(prior={"1.1.1": [_c(50)]})
    w = _writer(cat, defer_completion=True)
    _doc(w, "1.1.1", 2)
    _doc(w, "1.1.2", 1)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2))), "1.1.2": _rows(w, "1.1.2", (0, _c(3)))},
                       {_c(2): _chunk(_c(2)), _c(3): _chunk(_c(3))})
    # Neither the single-request document (write_many, sweep on) nor the last append stamped.
    assert [c["complete"] for op, c in cat.calls if op in ("write_many", "append_many")] == [None, None, None]
    assert sorted(res.landed) == ["1.1.1", "1.1.2"] and res.finished == []
    # The last append still carried the deferred sweep; only the stamp waits.
    assert [c for op, c in cat.calls if op == "append_many"][-1]["sweep_chashes"] == {"1.1.1": [_c(50)]}
    assert w.progress() == (0, 2)


def test_complete_documents_stamps_with_one_stamp_only_append_many_and_the_row_counts():
    cat = FakeCat()
    w = _writer(cat, defer_completion=True)
    _doc(w, "1.1.1", 2)
    _doc(w, "1.1.2", 1)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2))), "1.1.2": _rows(w, "1.1.2", (0, _c(3)))},
                       {_c(2): _chunk(_c(2)), _c(3): _chunk(_c(3))})
    before = len(cat.calls)
    done = w.complete_documents(res.landed)
    assert [op for op, _ in cat.calls[before:]] == ["append_many"], "one stamp request for the page"
    stamp = cat.calls[-1][1]
    assert stamp["docs"] == {"1.1.1": [], "1.1.2": []} and not stamp["chunks"] and not stamp["sweep_chashes"]
    assert stamp["complete"] == {"1.1.1": (_H, 2), "1.1.2": (_H, 1)}
    assert sorted(done.finished) == ["1.1.1", "1.1.2"] and not done.failed
    assert w.progress() == (2, 2)
    assert sorted(w.finish().completed) == ["1.1.1", "1.1.2"]


def test_a_second_complete_documents_call_has_nothing_owed():
    cat = FakeCat()
    w = _writer(cat, defer_completion=True)
    _doc(w, "1.1.1", 1)
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    assert w.complete_documents(res.landed).finished == ["1.1.1"]
    before = len(cat.calls)
    assert w.complete_documents(res.landed).finished == []
    assert len(cat.calls) == before


def test_a_deferred_stamp_the_engine_refuses_is_reported_and_leaves_the_fence_alone():
    cat = FakeCat(refuse={"1.1.1"})
    w = _writer(cat, defer_completion=True)
    _doc(w, "1.1.1", 1)
    _doc(w, "1.1.2", 1)
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                       {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    done = w.complete_documents(res.landed)
    assert done.finished == ["1.1.2"] and "refused" in done.failed["1.1.1"]
    verdict = w.finish()
    assert verdict.completed == ["1.1.2"] and "refused" in verdict.failed["1.1.1"]
    assert not [c for op, c in cat.calls if op == "fail"]


def test_a_deferred_stamp_the_engine_fails_in_place_leaves_the_document_indexing():
    cat = FakeCat()
    w = _writer(cat, defer_completion=True)
    _doc(w, "1.1.1", 1)
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    cat.fail_docs.add("1.1.1")                       # the stamp request fails for it, the write did not
    done = w.complete_documents(res.landed)
    assert done.finished == [] and "stays indexing" in done.failed["1.1.1"]
    assert "stays indexing" in w.finish().failed["1.1.1"]
    assert not [c for op, c in cat.calls if op == "fail"]


def test_a_document_whose_stamp_is_owed_is_reported_by_finish_and_never_failed_by_abort():
    """A stamp that may have committed and lost its ack must not be flipped to failed: abort() leaves
    a document whose last request landed (stamp owed) ``indexing``, and still fails an open one."""
    cat = FakeCat()
    w = _writer(cat, defer_completion=True)
    _doc(w, "1.1.1", 1)
    _doc(w, "1.1.2", 2)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                 {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    w.abort("boom")                       # 1.1.1 landed fully (stamp owed); 1.1.2 is still open
    assert [c["doc"] for op, c in cat.calls if op == "fail"] == ["1.1.2"]
    cat2 = FakeCat()
    w2 = _writer(cat2, defer_completion=True)
    _doc(w2, "1.1.1", 1)
    w2.write_page({"1.1.1": _rows(w2, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    assert "completion stamp was never sent" in w2.finish().failed["1.1.1"]


def test_complete_documents_is_for_a_deferred_writer_and_only_for_registered_documents():
    plain = _writer(FakeCat())
    with pytest.raises(ValueError, match="defer_completion"):
        plain.complete_documents([])
    w = _writer(FakeCat(), defer_completion=True)
    with pytest.raises(ValueError, match="not registered"):
        w.complete_documents(["9.9.9"])


def test_a_deferred_trailing_sweep_chain_parks_the_document_after_its_last_sweep_without_a_stamp():
    dropped = [_c(1000 + i) for i in range(650)]
    cat = FakeCat(prior={"1.1.1": dropped})
    w = _writer(cat, defer_completion=True)
    _doc(w, "1.1.1", 2)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2)))}, {_c(2): _chunk(_c(2))})
    appends = [c for op, c in cat.calls if op == "append_many"]
    assert [len(a["sweep_chashes"]["1.1.1"]) for a in appends] == [300, 300, 50]
    assert [a["complete"] for a in appends] == [None, None, None]
    assert res.landed == ["1.1.1"] and res.finished == []
    assert w.complete_documents(res.landed).finished == ["1.1.1"]
    assert cat.calls[-1][1]["complete"] == {"1.1.1": (_H, 2)}
