# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z0o2p.19 (RDR-223 P2.9): :class:`MultiDocumentImportWriter`, the page-by-page multi-document
writer the ``.nxexp`` import drives.

A scripted fake stands in for the catalog client HERE because the properties under test are ones the
engine only exhibits rarely: the order of the requests (sweep before stamp, stamp after the last
append), what is sent (a drop list minus what the run wrote, capped at 300), and what the writer
refuses when an answer lacks a field (no old-engine degrade). Every property that lives on the engine
side of the wire (a chunk never lands without an owner, vectors byte-identical, ``complete`` after an
import) is pinned against the real engine in ``tests/test_z0o2p19_nxexp_import_combined_write.py``.
"""
from __future__ import annotations

import pytest

from nexus.catalog.multi_document_write import MultiDocumentImportWriter
from nexus.errors import BatchWriteFailedError, IndexRunVerifyRefused

_COLL = "code__fake__bge-base-en-v15-768__v1"
_H = "f" * 64


def _c(n: int) -> str:
    return f"{n:064x}"


class FakeCat:
    """Records every call as ``(op, detail)`` in order; ``dropped`` scripts the replace's drop list
    per document; ``fail_docs`` / ``refuse`` / ``omit`` script the failures."""

    def __init__(self, *, dropped=None, fail_docs=(), refuse=(), omit=()):
        self.calls: list[tuple[str, dict]] = []
        self.dropped = dropped or {}
        self.fail_docs = set(fail_docs)
        self.refuse = set(refuse)
        self.omit = set(omit)

    def begin_index_run_many(self, docs, collection):
        self.calls.append(("begin", {"docs": [d["doc_id"] for d in docs]}))
        if "begin" in self.omit:
            return {}
        return {"docs": len(docs), "failed_doc_ids": []}

    def write_manifest_many(self, docs, complete=None, *, sweep=False, chunks=None, collection,
                            force_re_embed=False, embedding_model=None, **kw):
        self.calls.append(("write_many", {
            "docs": {d: rows for d, rows in docs}, "sweep": sweep, "complete": complete,
            "chunks": chunks, "embedding_model": embedding_model}))
        out = {"failed_doc_ids": [d for d, _ in docs if d in self.fail_docs],
               "chunks_written": len(chunks or ()), "vectors_supplied": len(chunks or ()),
               "embed_embedded": 0}
        if "dropped" not in self.omit:
            ok = [d for d, _ in docs if d not in self.fail_docs]
            out["dropped_chashes"] = {d: list(self.dropped.get(d, [])) for d in ok}
            out["dropped_count"] = {d: len(self.dropped.get(d, [])) for d in ok}
            out["dropped_unknown"] = []
        return out

    def append_manifest_many(self, docs, *, collection, chunks=None, sweep_chashes=None,
                             force_re_embed=False, embedding_model=None, **kw):
        self.calls.append(("append_many", {
            "docs": {d: rows for d, rows in docs}, "chunks": chunks,
            "sweep_chashes": sweep_chashes}))
        out = {"failed_doc_ids": [d for d, _ in docs if d in self.fail_docs],
               "chunks_written": len(chunks or ()), "swept": 0, "sweep_skipped": 0}
        if chunks and "unreferenced" not in self.omit:
            out["chunks_unreferenced"] = 0
        return out

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        self.calls.append(("complete", {"doc": doc_id, "count": chunk_count, "hash": content_hash}))
        if doc_id in self.refuse:
            raise IndexRunVerifyRefused(doc_id=doc_id, referenced=chunk_count, present=0,
                                        missing=1, chunk_count=chunk_count)
        return {"referenced": chunk_count, "present": chunk_count, "missing": 0}

    def fail_index_run(self, doc_id, error):
        self.calls.append(("fail", {"doc": doc_id, "error": error}))

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _writer(cat: FakeCat) -> MultiDocumentImportWriter:
    return MultiDocumentImportWriter(
        cat, collection=_COLL, content_hash=_H, embedding_model="bge-base-en-v15-768")


def _rows(w: MultiDocumentImportWriter, doc: str, *pairs: tuple[int, str]) -> list[dict]:
    return [{"chash": c, "position": w.claim_position(doc, p)} for p, c in pairs]


def _chunk(c: str) -> dict:
    return {"chash": c, "text": f"text {c[-4:]}", "metadata": {}, "embedding": [0.5, 0.25]}


def test_pages_are_a_replace_then_appends_and_the_stamp_waits_for_finish():
    cat = FakeCat()
    w = _writer(cat)
    a1, a2, b1 = _c(1), _c(2), _c(3)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, a1))}, {a1: _chunk(a1)})
    w.write_page({"1.1.1": _rows(w, "1.1.1", (5, a2)), "1.1.2": _rows(w, "1.1.2", (0, b1))},
                 {a2: _chunk(a2), b1: _chunk(b1)})
    assert cat.ops() == ["begin", "write_many", "begin", "write_many", "append_many"]
    _, first = cat.calls[1]
    assert first["sweep"] is False and first["complete"] is None
    assert first["embedding_model"] == "bge-base-en-v15-768"
    assert first["chunks"] == [_chunk(a1)]                    # vectors ride with the chunk
    _, second = cat.calls[3]
    assert list(second["docs"]) == ["1.1.2"]                  # only the document not yet open
    _, app = cat.calls[4]
    assert list(app["docs"]) == ["1.1.1"] and app["chunks"] == [_chunk(a2)]
    done = w.finish()
    assert cat.ops()[5:] == ["complete", "complete"]
    assert {c["doc"]: c["count"] for op, c in cat.calls if op == "complete"} == {"1.1.1": 2, "1.1.2": 1}
    assert sorted(done.completed) == ["1.1.1", "1.1.2"] and not done.failed
    assert w.rows_landed == 3


def test_the_drop_list_minus_what_the_run_wrote_is_swept_after_the_last_append_and_before_the_stamp():
    d1, d2, keep = _c(10), _c(11), _c(12)
    cat = FakeCat(dropped={"1.1.1": [d1, d2, keep]})
    w = _writer(cat)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    w.write_page({"1.1.1": _rows(w, "1.1.1", (1, keep))}, {keep: _chunk(keep)})   # re-added later
    w.finish()
    ops = cat.ops()
    sweep_at = next(i for i, (op, c) in enumerate(cat.calls) if op == "append_many" and c["sweep_chashes"])
    assert cat.calls[sweep_at][1]["sweep_chashes"] == {"1.1.1": [d1, d2]}
    assert cat.calls[sweep_at][1]["docs"] == {"1.1.1": []}    # sweep-only
    assert sweep_at == len(ops) - 2 and ops[-1] == "complete"


def test_no_sweep_request_when_the_run_rewrote_everything_it_dropped():
    cat = FakeCat(dropped={"1.1.1": [_c(1)]})
    w = _writer(cat)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(2)))}, {_c(2): _chunk(_c(2))})
    w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(1)))}, {_c(1): _chunk(_c(1))})
    w.finish()
    assert all(not c.get("sweep_chashes") for op, c in cat.calls if op == "append_many")


def test_a_drop_list_over_the_cap_continues_in_further_requests():
    dropped = [_c(1000 + i) for i in range(650)]
    cat = FakeCat(dropped={"1.1.1": dropped})
    w = _writer(cat)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    w.finish()
    sweeps = [c["sweep_chashes"]["1.1.1"] for op, c in cat.calls if op == "append_many"]
    assert [len(s) for s in sweeps] == [300, 300, 50]
    assert [x for s in sweeps for x in s] == dropped


def test_claim_position_keeps_a_free_position_and_moves_a_taken_one_past_the_highest():
    w = _writer(FakeCat())
    assert w.claim_position("d", 4) == 4
    assert w.claim_position("d", 0) == 0
    assert w.claim_position("d", 4) == 5          # taken: one past the highest claimed
    assert w.claim_position("d", 4) == 6
    assert w.claim_position("other", 4) == 4      # positions are per document


def test_a_row_whose_position_was_not_claimed_is_refused_before_anything_is_sent():
    cat = FakeCat()
    w = _writer(cat)
    with pytest.raises(ValueError, match="claim_position"):
        w.write_page({"1.1.1": [{"chash": _c(1), "position": 3}]}, {_c(1): _chunk(_c(1))})
    assert cat.calls == []


def test_a_document_the_engine_fails_is_marked_and_gets_no_more_rows_or_stamp():
    cat = FakeCat(fail_docs={"1.1.2"})
    w = _writer(cat)
    res = w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                       {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    assert res.written == ["1.1.1"] and "1.1.2" in res.failed
    assert ("fail", {"doc": "1.1.2", "error": res.failed["1.1.2"]}) in cat.calls
    again = w.write_page({"1.1.2": _rows(w, "1.1.2", (1, _c(3)))}, {_c(3): _chunk(_c(3))})
    assert again.written == [] and "1.1.2" in again.failed
    assert cat.ops().count("write_many") == 1 and cat.ops().count("append_many") == 0
    done = w.finish()
    assert done.completed == ["1.1.1"]
    assert [c["doc"] for op, c in cat.calls if op == "complete"] == ["1.1.1"]


def test_a_refused_stamp_is_reported_and_leaves_the_fence_alone():
    cat = FakeCat(refuse={"1.1.1"})
    w = _writer(cat)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1))), "1.1.2": _rows(w, "1.1.2", (0, _c(2)))},
                 {_c(1): _chunk(_c(1)), _c(2): _chunk(_c(2))})
    done = w.finish()
    assert done.completed == ["1.1.2"] and "1.1.1" in done.failed
    assert "refused" in done.failed["1.1.1"]
    assert not [c for op, c in cat.calls if op == "fail"]
    w.abort("later")                                   # finished: a no-op
    assert not [c for op, c in cat.calls if op == "fail"]


def test_abort_marks_only_documents_begun_and_not_stamped_or_failed():
    cat = FakeCat()
    w = _writer(cat)
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    w.abort("boom")
    assert [c["doc"] for op, c in cat.calls if op == "fail"] == ["1.1.1"]
    w.abort("boom")
    assert [op for op, _ in cat.calls].count("fail") == 1


@pytest.mark.parametrize("omit,match", [
    ({"begin"}, "failed_doc_ids"),
    ({"dropped"}, "dropped_chashes"),
])
def test_a_response_missing_a_field_the_protocol_needs_is_an_error_not_a_degrade(omit, match):
    w = _writer(FakeCat(omit=omit))
    with pytest.raises(BatchWriteFailedError, match=match):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})


def test_an_append_answer_without_chunks_unreferenced_is_an_error():
    w = _writer(FakeCat(omit={"unreferenced"}))
    w.write_page({"1.1.1": _rows(w, "1.1.1", (0, _c(1)))}, {_c(1): _chunk(_c(1))})
    with pytest.raises(BatchWriteFailedError, match="chunks_unreferenced"):
        w.write_page({"1.1.1": _rows(w, "1.1.1", (1, _c(2)))}, {_c(2): _chunk(_c(2))})


def test_content_hash_is_required():
    with pytest.raises(ValueError, match="content_hash"):
        MultiDocumentImportWriter(FakeCat(), collection=_COLL, content_hash="")
