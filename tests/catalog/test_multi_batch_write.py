# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.0 (nexus-z0o2p.10): the request protocol of the multi-batch combined writer.

Drives :class:`nexus.catalog.multi_batch_write.MultiBatchDocumentWriter` against a recording fake
catalog writer, so the ORDER and SHAPE of the requests are pinned exactly. The behaviour against
the real engine (final state, killed clients, re-index) is in
``tests/integration/test_rdr223_multi_batch_writer_journey.py``.

The protocol (RDR-223 Technical Design 1, bead nexus-z0o2p.10 steps 4 to 6):

* one batch: ONE ``write_manifest_many`` with chunks, sweep on, and the completion stamp riding it;
* several batches: batch 1 is ``write_manifest_many`` with sweep OFF and no ``complete`` (its
  ``dropped_chashes`` are kept), batches 2..N are ``append_manifest_chunks`` with chunks, the LAST
  append carries the kept list as ``sweep_chashes`` (at most 300, the rest in trailing sweep-only
  appends), and only after all of that ``complete_index_run``;
* ``begin_index_run`` before batch 1 whenever a ``content_hash`` is given.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.catalog.multi_batch_write import (
    BatchWriteFailedError,
    MultiBatchDocumentWriter,
    RepeatedPositionError,
    write_document,
)
from nexus.errors import IndexRunVerifyRefused

_COLLECTION = "docs__mbw-unit__bge-base-en-v15-768__v1"
_DOC = "1.1.1"


def _h(i: int) -> str:
    return f"{i:064x}"


def _rows(start: int, n: int, *, offset: int = 0) -> list[dict]:
    return [{"chash": _h(offset + start + i), "position": start + i} for i in range(n)]


def _chunks(start: int, n: int, *, offset: int = 0) -> list[dict]:
    return [
        {"chash": _h(offset + start + i), "text": f"text {offset + start + i}", "metadata": {}}
        for i in range(n)
    ]


def _batch(start: int, n: int, *, offset: int = 0) -> tuple[list[dict], list[dict]]:
    return _rows(start, n, offset=offset), _chunks(start, n, offset=offset)


class FakeCat:
    """Records every writer call in order; answers like a Phase 1 engine."""

    def __init__(self, *, dropped: list[str] | None = None, dropped_unknown: bool = False,
                 omit_dropped: bool = False, complete_refused: bool = False,
                 failed: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._dropped = dropped or []
        self._dropped_unknown = dropped_unknown
        self._omit_dropped = omit_dropped
        self._complete_refused = complete_refused
        self._failed = failed

    def _rec(self, name: str, **kw: Any) -> None:
        self.calls.append((name, kw))

    def begin_index_run(self, doc_id, content_hash, run_id, collection):
        self._rec("begin_index_run", doc_id=doc_id, content_hash=content_hash,
                  run_id=run_id, collection=collection)

    def write_manifest_many(self, docs, complete=None, *, sweep=False, chunks=None,
                            collection, force_re_embed=False, embedding_model=None):
        self._rec("write_manifest_many", docs=docs, complete=complete, sweep=sweep,
                  chunks=chunks, collection=collection, force_re_embed=force_re_embed,
                  embedding_model=embedding_model)
        (doc_id, rows), = docs
        out: dict = {
            "failed_doc_ids": [doc_id] if self._failed else [],
            "complete_refused": [], "complete_refused_count": 0,
            "swept": 0, "sweep_skipped": 0, "sweep_detail": [],
        }
        if complete and self._complete_refused:
            out["complete_refused"] = [{"doc_id": doc_id, "referenced": len(rows),
                                        "missing": 1, "chunk_count": len(rows)}]
            out["complete_refused_count"] = 1
        if chunks is not None:
            out.update(chunks_written=len(chunks), embed_embedded=len(chunks),
                       embed_skipped=0, chunks_deduped=0)
        if self._dropped_unknown:
            out["dropped_unknown"] = [doc_id]
        elif not self._omit_dropped and not self._failed:
            out["dropped_chashes"] = {doc_id: list(self._dropped)}
            out["dropped_count"] = {doc_id: len(self._dropped)}
        return out

    def append_manifest_chunks(self, doc_id, chunks, *, collection, chunk_payload=None,
                               sweep_chashes=None, force_re_embed=False, embedding_model=None):
        self._rec("append_manifest_chunks", doc_id=doc_id, rows=chunks, collection=collection,
                  chunk_payload=chunk_payload, sweep_chashes=sweep_chashes,
                  force_re_embed=force_re_embed, embedding_model=embedding_model)
        out: dict = {"ok": True, "count": len(chunks)}
        if chunk_payload is not None:
            out.update(chunks_written=len(chunk_payload), embed_embedded=len(chunk_payload),
                       embed_skipped=0, chunks_deduped=0)
        if sweep_chashes:
            out.update(swept=len(sweep_chashes), sweep_skipped=0, sweep_detail={})
        return out

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        self._rec("complete_index_run", doc_id=doc_id, content_hash=content_hash,
                  chunk_count=chunk_count)
        return {"referenced": chunk_count, "present": chunk_count, "missing": 0}

    def fail_index_run(self, doc_id, error):
        self._rec("fail_index_run", doc_id=doc_id, error=error)

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def of(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


def _writer(cat: FakeCat, **kw: Any) -> MultiBatchDocumentWriter:
    return MultiBatchDocumentWriter(cat, doc_id=_DOC, collection=_COLLECTION, **kw)


# ── one batch ─────────────────────────────────────────────────────────────────


def test_one_batch_is_one_write_many_with_sweep_on_and_complete_riding_it() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", run_id="run1")
    w.add_batch(*_batch(0, 3))
    res = w.finish()
    assert cat.names() == ["begin_index_run", "write_manifest_many"]
    wm = cat.of("write_manifest_many")[0]
    assert wm["sweep"] is True
    assert wm["complete"] == {_DOC: "hash1"}
    assert len(wm["chunks"]) == 3 and wm["collection"] == _COLLECTION
    assert cat.of("begin_index_run")[0]["run_id"] == "run1"
    assert res.completed and res.requests == 1 and res.batches == 1


def test_one_batch_without_a_content_hash_has_no_fence_and_no_complete() -> None:
    cat = FakeCat()
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    res = w.finish()
    assert cat.names() == ["write_manifest_many"]
    assert cat.of("write_manifest_many")[0]["complete"] in (None, {})
    assert not res.completed


def test_one_batch_complete_refused_raises() -> None:
    cat = FakeCat(complete_refused=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    with pytest.raises(IndexRunVerifyRefused):
        w.finish()


def test_write_many_failure_of_the_document_raises() -> None:
    cat = FakeCat(failed=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    with pytest.raises(BatchWriteFailedError) as ei:
        w.finish()
    assert ei.value.doc_id == _DOC


# ── several batches ───────────────────────────────────────────────────────────


def test_three_batches_request_sequence() -> None:
    cat = FakeCat(dropped=[_h(900), _h(901)])
    w = _writer(cat, content_hash="hash1", run_id="run1")
    for b in (_batch(0, 2), _batch(2, 2), _batch(4, 2)):
        w.add_batch(*b)
    res = w.finish()
    assert cat.names() == [
        "begin_index_run", "write_manifest_many", "append_manifest_chunks",
        "append_manifest_chunks", "complete_index_run",
    ]
    wm = cat.of("write_manifest_many")[0]
    # Batch 1 must neither sweep (it would delete the previous run's chunks before the later
    # batches land) nor carry the completion stamp (write_many stamps against ITS OWN row count).
    assert wm["sweep"] is False
    assert wm["complete"] in (None, {})
    assert [r["position"] for r in wm["docs"][0][1]] == [0, 1]
    a1, a2 = cat.of("append_manifest_chunks")
    assert a1["sweep_chashes"] in (None, [])
    assert a2["sweep_chashes"] == [_h(900), _h(901)]          # the LAST append carries the sweep
    assert [r["position"] for r in a2["rows"]] == [4, 5]
    assert len(a2["chunk_payload"]) == 2
    done = cat.of("complete_index_run")[0]
    assert done == {"doc_id": _DOC, "content_hash": "hash1", "chunk_count": 6}
    assert res.completed and res.requests == 3 and res.batches == 3 and res.distinct_chashes == 6


def test_completion_stamp_is_the_very_last_call() -> None:
    cat = FakeCat(dropped=[_h(i) for i in range(900, 1000)])
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2)):
        w.add_batch(*b)
    w.finish()
    assert cat.names()[-1] == "complete_index_run"
    assert "complete_index_run" not in cat.names()[:-1]


def test_dropped_chashes_this_run_re_added_are_not_swept() -> None:
    """batch 1's dropped list is the previous manifest minus batch 1 only, so for an unchanged
    document it holds nearly every chash. Anything a later batch (or batch 1) wrote is subtracted
    client-side first: no sweep request for an unchanged re-index."""
    cat = FakeCat(dropped=[_h(2), _h(3), _h(4), _h(5)])   # exactly what batches 2 and 3 write
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2), _batch(4, 2)):
        w.add_batch(*b)
    res = w.finish()
    for a in cat.of("append_manifest_chunks"):
        assert not a["sweep_chashes"]
    assert res.swept == 0
    assert len(cat.of("append_manifest_chunks")) == 2          # no sweep-only append either


def test_long_dropped_list_is_split_into_300s_with_trailing_sweep_only_appends() -> None:
    dropped = [_h(10_000 + i) for i in range(650)]
    cat = FakeCat(dropped=dropped)
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2)):
        w.add_batch(*b)
    res = w.finish()
    appends = cat.of("append_manifest_chunks")
    assert len(appends) == 1 + 2
    data, *sweeps = appends
    assert len(data["chunk_payload"]) == 2 and data["sweep_chashes"] == dropped[:300]
    # Trailing sweep-only appends: no rows, no chunks, the rest of the list in order.
    assert [(s["rows"], s["chunk_payload"]) for s in sweeps] == [([], None)] * 2
    assert [s["sweep_chashes"] for s in sweeps] == [dropped[300:600], dropped[600:]]
    assert res.swept == 650


def test_sweep_only_appends_follow_the_last_data_append_and_precede_complete() -> None:
    cat = FakeCat(dropped=[_h(10_000 + i) for i in range(301)])
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2)):
        w.add_batch(*b)
    w.finish()
    kinds = [
        "data" if kw.get("chunk_payload") is not None else "sweep-only"
        for n, kw in cat.calls if n == "append_manifest_chunks"
    ]
    assert kinds == ["data", "sweep-only"]
    assert cat.names().index("complete_index_run") > max(
        i for i, n in enumerate(cat.names()) if n == "append_manifest_chunks")


def test_dropped_unknown_omits_the_sweep_and_says_so() -> None:
    cat = FakeCat(dropped_unknown=True)
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 2), _batch(2, 2)):
        w.add_batch(*b)
    res = w.finish()
    assert all(not a["sweep_chashes"] for a in cat.of("append_manifest_chunks"))
    assert res.sweep_deferred_unknown is True
    assert res.completed        # the write itself is whole; only the sweep is left to the reaper


def test_multi_batch_response_without_dropped_chashes_is_a_hard_error() -> None:
    """The client and engine ship as a pair: batch 1's response with neither an entry in
    ``dropped_chashes`` nor a ``dropped_unknown`` marker for the document is a broken engine, not a
    reason to guess (guessing 'empty' would silently strand the previous run's chunks; guessing
    'sweep now' would delete them under the later batches)."""
    cat = FakeCat(omit_dropped=True)
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    with pytest.raises(BatchWriteFailedError, match="dropped_chashes"):
        w.add_batch(*_batch(2, 2))       # batch 1 is sent once batch 2 shows it is not the last


def test_response_without_dropped_count_is_a_hard_error() -> None:
    cat = FakeCat(dropped=[_h(900)])
    real = cat.write_manifest_many

    def no_count(*a, **kw):
        out = real(*a, **kw)
        out.pop("dropped_count")
        return out

    cat.write_manifest_many = no_count  # type: ignore[method-assign]
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    with pytest.raises(BatchWriteFailedError, match="dropped_count"):
        w.add_batch(*_batch(2, 2))


def test_chunks_the_engine_reports_unreferenced_are_a_hard_error() -> None:
    cat = FakeCat()
    real = cat.append_manifest_chunks

    def dropping(*a, **kw):
        out = real(*a, **kw)
        out["chunks_unreferenced"] = 1
        return out

    cat.append_manifest_chunks = dropping  # type: ignore[method-assign]
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    w.add_batch(*_batch(2, 2))
    with pytest.raises(BatchWriteFailedError, match="unreferenced|referenced by no row"):
        w.finish()


def test_dropped_count_disagreeing_with_the_list_is_a_hard_error() -> None:
    cat = FakeCat(dropped=[_h(900), _h(901)])
    real = cat.write_manifest_many

    def lying(*a, **kw):
        out = real(*a, **kw)
        out["dropped_count"] = {_DOC: 5}
        return out

    cat.write_manifest_many = lying  # type: ignore[method-assign]
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 2))
    with pytest.raises(BatchWriteFailedError, match="dropped_count"):
        w.add_batch(*_batch(2, 2))


# ── rows and positions ────────────────────────────────────────────────────────


def test_a_repeated_position_within_a_batch_fails() -> None:
    cat = FakeCat()
    w = _writer(cat)
    rows = [{"chash": _h(1), "position": 0}, {"chash": _h(2), "position": 0}]
    with pytest.raises(RepeatedPositionError):
        w.add_batch(rows, _chunks(1, 2))
    assert cat.calls == []


def test_a_repeated_position_across_batches_fails_before_it_is_sent() -> None:
    cat = FakeCat()
    w = _writer(cat)
    w.add_batch(*_batch(0, 2))
    with pytest.raises(RepeatedPositionError):
        w.add_batch(*_batch(1, 2, offset=100))    # position 1 again, a different chash
    assert cat.calls == []                          # nothing was sent for the bad batch


def test_a_row_without_an_integer_position_fails() -> None:
    w = _writer(FakeCat())
    with pytest.raises(ValueError, match="position"):
        w.add_batch([{"chash": _h(1)}], _chunks(1, 1))


def test_a_payload_chunk_no_row_references_fails() -> None:
    """A chunk the batch's own rows do not reference would be an ownerless chunk by construction."""
    w = _writer(FakeCat())
    with pytest.raises(ValueError, match="no row"):
        w.add_batch(_rows(0, 1), _chunks(0, 2))


def test_a_batch_over_the_chunk_cap_is_split_into_requests_no_larger_than_it() -> None:
    cat = FakeCat(dropped=[_h(900)])
    w = _writer(cat, content_hash="hash1", chunk_cap=4)
    w.add_batch(*_batch(0, 10))
    res = w.finish()
    sizes = [len(c["chunks"]) for c in cat.of("write_manifest_many")] + [
        len(a["chunk_payload"]) for a in cat.of("append_manifest_chunks")]
    assert sizes == [4, 4, 2]
    assert res.batches == 3 and res.distinct_chashes == 10
    # One document across the slices: positions contiguous, the sweep on the LAST slice only.
    appends = cat.of("append_manifest_chunks")
    assert [r["position"] for r in cat.of("write_manifest_many")[0]["docs"][0][1]] == [0, 1, 2, 3]
    assert [r["position"] for a in appends for r in a["rows"]] == [4, 5, 6, 7, 8, 9]
    assert appends[0]["sweep_chashes"] in (None, []) and appends[1]["sweep_chashes"] == [_h(900)]
    assert cat.of("write_manifest_many")[0]["sweep"] is False


@pytest.mark.parametrize("configured", [301, 1000, 100_000])
def test_a_chunk_cap_env_override_above_300_is_clamped_to_300(monkeypatch, configured) -> None:
    """NX_ONNX_LOCAL_UPSERT_CHUNK_CAP has no upper bound, but the engine caps append_many at 300 and
    the 300-record write cap holds everywhere: no request may carry more, whatever the operator
    set."""
    import nexus.db.http_vector_client as hvc

    monkeypatch.setattr(hvc, "_ONNX_LOCAL_UPSERT_CHUNK_CAP", configured)
    monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda *a, **k: "onnx-local")
    assert hvc.per_collection_chunk_cap(_COLLECTION) == configured      # the override is live
    cat = FakeCat()
    w = _writer(cat)                                                    # no explicit chunk_cap
    w.add_batch(*_batch(0, 700))
    w.finish()
    sizes = [len(c["chunks"]) for c in cat.of("write_manifest_many")] + [
        len(a["chunk_payload"]) for a in cat.of("append_manifest_chunks")]
    assert sizes == [300, 300, 100]
    assert max(sizes) <= 300


def test_a_chunk_referenced_by_two_slices_is_sent_once() -> None:
    """Rows repeating a chash (the same text at two positions) share one chunk: the second slice's
    payload does not resend it, and the completion stamp counts it once."""
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1", chunk_cap=2)
    rows = [{"chash": _h(1), "position": 0}, {"chash": _h(2), "position": 1},
            {"chash": _h(1), "position": 2}, {"chash": _h(3), "position": 3}]
    chunks = [{"chash": _h(i), "text": f"t{i}", "metadata": {}} for i in (1, 2, 3)]
    w.add_batch(rows, chunks)
    w.finish()
    sent = [c["chash"] for c in cat.of("write_manifest_many")[0]["chunks"]] + [
        c["chash"] for c in cat.of("append_manifest_chunks")[0]["chunk_payload"]]
    assert sent == [_h(1), _h(2), _h(3)]
    assert cat.of("complete_index_run")[0]["chunk_count"] == 3


def test_an_empty_batch_fails() -> None:
    w = _writer(FakeCat())
    with pytest.raises(ValueError):
        w.add_batch([], [])


def test_finish_with_no_batch_fails() -> None:
    with pytest.raises(ValueError, match="no batch"):
        _writer(FakeCat()).finish()


def test_a_writer_that_failed_refuses_more_work() -> None:
    cat = FakeCat(failed=True)
    w = _writer(cat)
    w.add_batch(*_batch(0, 1))
    with pytest.raises(BatchWriteFailedError):
        w.add_batch(*_batch(1, 1))        # sends batch 1, which the engine failed
    with pytest.raises(BatchWriteFailedError, match="earlier"):
        w.add_batch(*_batch(2, 1))
    with pytest.raises(BatchWriteFailedError, match="earlier"):
        w.finish()


# ── the fence and the run ─────────────────────────────────────────────────────


def test_begin_index_run_precedes_the_first_request_and_uses_a_generated_run_id() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    for b in (_batch(0, 1), _batch(1, 1)):
        w.add_batch(*b)
    w.finish()
    assert cat.names()[0] == "begin_index_run"
    assert cat.of("begin_index_run")[0]["run_id"]


def test_abort_marks_the_run_failed_when_a_fence_was_begun() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.add_batch(*_batch(1, 1))            # sends batch 1 (fence begun)
    w.abort("boom")
    assert cat.of("fail_index_run") == [{"doc_id": _DOC, "error": "boom"}]


def test_abort_before_anything_was_sent_is_a_no_op() -> None:
    cat = FakeCat()
    w = _writer(cat, content_hash="hash1")
    w.add_batch(*_batch(0, 1))
    w.abort("boom")
    assert cat.calls == []


# ── vectors and the convenience form ──────────────────────────────────────────


def test_embedding_model_and_force_re_embed_ride_every_chunk_carrying_request() -> None:
    cat = FakeCat()
    w = _writer(cat, embedding_model="m", force_re_embed=True)
    for b in (_batch(0, 1), _batch(1, 1)):
        w.add_batch(*b)
    w.finish()
    wm = cat.of("write_manifest_many")[0]
    ap = cat.of("append_manifest_chunks")[0]
    assert wm["embedding_model"] == "m" and wm["force_re_embed"] is True
    assert ap["embedding_model"] == "m" and ap["force_re_embed"] is True


def test_write_document_is_the_batches_convenience_form() -> None:
    cat = FakeCat()
    res = write_document(
        cat, [_batch(0, 2), _batch(2, 2)], doc_id=_DOC, collection=_COLLECTION,
        content_hash="hash1")
    assert res.completed and cat.names()[-1] == "complete_index_run"
