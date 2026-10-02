# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 (nexus-z0o2p.11): the streaming PDF run that finds every chunk already sent.

A run whose last request landed and whose completion stamp did not (a post-pass that failed, a kill
between the last request and the stamp) leaves a buffer with every chunk flagged uploaded. The
retry has nothing to send, only the stamp. Two rules:

* the stamp is FAIL-CLOSED. A missing fence route (a ``None`` answer) or a transport failure is an
  error and leaves the document's fence ``indexing``; the run never returns success over an
  unstamped document. (The old tail went through ``_fence_begin``/``_fence_complete``, which
  swallow every error but a refusal and returned success with the fence still ``indexing``.)
* the stamp is only sent over a manifest this buffer wrote. The buffer's chunks are not readable
  once flagged, so the proof is the document's fence: ``indexing`` for THIS content hash is what
  the run that sent the last request left. If the document was indexed at other bytes since (the
  fence carries that hash), the manifest is not this buffer's and stamping over it would mark the
  wrong version complete whenever the row counts happen to agree; the buffer is discarded and the
  document runs again.

The fake engine stands in for the pipeline buffer; the writer is the recorder used by
``test_pipeline_stages.py``. The real-engine behavior of the writer is in
``tests/integration/test_rdr223_pdf_journey.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from nexus.db.t3 import T3Database
from nexus.errors import BatchWriteFailedError, IndexRunVerifyRefused
from nexus.pdf_chunker import TextChunk
from nexus.pdf_extractor import ExtractionResult
from nexus.pipeline_stages import pipeline_index_pdf
from tests._owner_write_double import install_streaming_writer
from tests.pipeline_fake_engine import make_fake_engine_db

_P_EXT = "nexus.pipeline_stages.PDFExtractor"
_P_CHK = "nexus.pipeline_stages.PDFChunker"
_H = "abc123"


@pytest.fixture()
def db():
    store, _engine = make_fake_engine_db()
    return store


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.pipeline_stages._POLL_INTERVAL", 0.01)


@pytest.fixture()
def writer(monkeypatch):
    return install_streaming_writer(monkeypatch)


def _extract(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, allow_degraded=False):
    if on_page:
        on_page(0, "Page 0 content.", {"page_number": 1, "text_length": 15})
    return ExtractionResult(
        text="Page 0 content.",
        metadata={"extraction_method": "docling", "page_count": 1,
                  "page_boundaries": [{"page_number": 1, "start_char": 0, "page_text_length": 16}],
                  "table_regions": [], "format": "markdown"})


def _t3(*, update_side_effect):
    col = MagicMock()
    col.get.return_value = {"ids": ["abc123_0"], "metadatas": [
        {"page_number": 1, "chunk_type": "text", "content_hash": _H}]}
    t3 = create_autospec(T3Database, instance=True)
    t3.get_or_create_collection.return_value = col
    t3.update_chunks.side_effect = update_side_effect
    return t3


def _run(db, t3, path="/tail.pdf"):
    with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
        ME.return_value.extract.side_effect = _extract
        MC.return_value.chunk.return_value = [TextChunk(text="chunk 0 text", chunk_index=0,
                                                        metadata={"page_number": 1})]
        return pipeline_index_pdf(Path(path), _H, "docs__test", t3, db=db,
                                  embed_fn=lambda t, m: ([[0.1] * 4 for _ in t], m), corpus="test")


def _leave_a_finished_upload(db, writer) -> None:
    """Run 1: every chunk is sent and flagged, the post-pass fails, so nothing is stamped."""
    _run(db, _t3(update_side_effect=Exception("quota exceeded")))
    assert len(writer.instances) == 1 and writer.instances[0].finished
    assert ("complete",) not in writer.events, "non-vacuity: run 1 stamped nothing"


@pytest.fixture()
def fence(monkeypatch):
    """Pin the document's fence as the retry reads it: ``fence.set(state, hash)``."""
    box = {"v": ("indexing", _H)}
    monkeypatch.setattr("nexus.doc_indexer._index_fence_state", lambda doc_id: box["v"])

    class _F:
        @staticmethod
        def set(state, h):
            box["v"] = (state, h)

    return _F


@pytest.fixture()
def stamp(monkeypatch):
    """Record the tail's stamp instead of sending it."""
    calls: list[tuple] = []
    monkeypatch.setattr("nexus.doc_indexer._stamp_finished_upload", lambda *a: calls.append(a))
    return calls


def test_a_finished_upload_under_its_own_fence_is_stamped_without_a_writer(db, writer, fence, stamp) -> None:
    _leave_a_finished_upload(db, writer)

    n = _run(db, _t3(update_side_effect=[None, None]))

    assert n == 1
    assert len(writer.instances) == 1, "the retry had nothing to send and made no writer"
    assert len(stamp) == 1 and stamp[0][1:] == (_H, 1), "one stamp, over this content and its row count"
    assert db.get_pipeline_state(_H) is None, "the buffer is cleaned up after the stamp"


@pytest.mark.parametrize(
    "state,fence_hash",
    [("complete", "other-bytes"), ("indexing", "other-bytes"), ("failed", _H), (None, "")],
    ids=["indexed-since", "another-run-in-flight", "failed", "no-fence"],
)
def test_a_finished_upload_whose_fence_is_not_its_own_is_discarded_and_rerun(
    db, writer, fence, stamp, state, fence_hash,
) -> None:
    """The buffer's chunks are gone (flagged), so nothing proves the manifest is theirs: the
    document runs again through one writer, from scratch, and stamps through it."""
    _leave_a_finished_upload(db, writer)
    fence.set(state, fence_hash)

    n = _run(db, _t3(update_side_effect=[None, None]))

    assert n == 1
    assert len(writer.instances) == 2, "a second writer sent the document again"
    assert [r["position"] for rows, _ in writer.instances[1].batches for r in rows] == [0]
    assert writer.events[-2:] == [("finish",), ("complete",)]
    assert stamp == [], "the writer-less stamp was not used"


# ── the stamp helper ──────────────────────────────────────────────────────────


class _Cat:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls, self.closed = result, raises, [], False

    def complete_index_run(self, *args):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.result

    def close(self):
        self.closed = True


def _stamp_with(monkeypatch, cat):
    from nexus.doc_indexer import _stamp_finished_upload

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **k: cat)
    return _stamp_finished_upload


def test_the_stamp_helper_sends_the_row_count(monkeypatch) -> None:
    cat = _Cat(result={"referenced": 3})
    _stamp_with(monkeypatch, cat)("1.1.1", _H, 3)
    assert cat.calls == [("1.1.1", _H, 3)] and cat.closed


def test_a_none_stamp_is_an_error_not_a_pre_fence_engine_success(monkeypatch) -> None:
    """``None`` is the client's answer for an engine with no fence route. The document was NOT
    stamped, and must not be reported as done."""
    cat = _Cat(result=None)
    with pytest.raises(BatchWriteFailedError, match="NOT stamped"):
        _stamp_with(monkeypatch, cat)("1.1.1", _H, 3)
    assert cat.closed


def test_a_transport_failure_propagates_and_the_fence_is_not_failed(monkeypatch) -> None:
    cat = _Cat(raises=OSError("connection reset"))
    fail = MagicMock()
    monkeypatch.setattr("nexus.doc_indexer._fence_fail", fail)
    stamp = _stamp_with(monkeypatch, cat)
    monkeypatch.setattr("nexus.retry._manifest_write_with_retry", lambda fn, *a, **k: fn(*a, **k))
    with pytest.raises(OSError, match="connection reset"):
        stamp("1.1.1", _H, 3)
    fail.assert_not_called()


def test_a_refused_stamp_is_recorded_and_raised(monkeypatch) -> None:
    from nexus.mcp_infra import get_complete_refusals, reset_complete_refusals

    reset_complete_refusals()
    refusal = IndexRunVerifyRefused(doc_id="1.1.1", referenced=3, present=2, missing=1, chunk_count=3)
    with pytest.raises(IndexRunVerifyRefused):
        _stamp_with(monkeypatch, _Cat(raises=refusal))("1.1.1", _H, 3)
    assert "1.1.1" in str(get_complete_refusals())
