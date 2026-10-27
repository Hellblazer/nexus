# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.1): the MinerU breaker's exit through the REAL streaming pipeline.

``MineruSystemicFailure`` says the install or the machine is failing, not the document. The
pipeline therefore gives the row back as ``failed`` with its buffer KEPT, the way an interrupted run
does (``_release_interrupted_run``), and not through ``_mark_failed_and_reset_wal``, which would
wipe the extracted pages and restart a 300-page book at page 1. Driven through
``doc_indexer.index_pdf`` against the engine substrate ``tests/integration/test_rdr223_pdf_journey.py``
boots; only the extractor (which trips after a few pages) and the chunker are faked.

* the run fails with the breaker's own exception, naming the class and the stderr line;
* the pipeline row is ``failed`` and its pages are still in the buffer; nothing wiped it;
* no catalog manifest is written and no chunk reaches the vector store;
* a rerun after the "repair" writes only the pages the first run did not extract, and completes.
"""
from __future__ import annotations

import pytest

from nexus.db.http_pipeline_client import HttpPipelineDB
from nexus.doc_indexer import _sha256
from nexus.pdf_extractor import MineruSystemicFailure
from tests._bounded_polling import bound_the_polling
from tests.integration.test_rdr223_pdf_journey import (
    _COLLECTION,
    _FakeChunker,
    _FakeExtractor,
    _index,
    _index_state,
    _lines,
    _manifest,
    _present,
    _register,
    _sha,
    _traffic,
    _write_pdf,
)

pytestmark = [pytest.mark.integration]

_CLASS = "exit:1:ImportError: libGL.so.1: cannot open shared object file"
_LINE = "ImportError: libGL.so.1: cannot open shared object file"


class _TrippingExtractor(_FakeExtractor):
    """Delivers the first ``trip_after`` pages, then the breaker trips (``None``: a healthy run)."""

    trip_after: int | None = None

    def extract(self, pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None,
                allow_degraded=False):
        if self.trip_after is None:
            return super().extract(pdf_path, extractor=extractor, on_page=on_page)
        self.calls.append(str(pdf_path))
        for i, line in enumerate(self.lines[: self.trip_after]):
            on_page(i, line, {"page_number": i + 1, "text_length": len(line)})
        raise MineruSystemicFailure(
            _CLASS, _LINE, [self.trip_after - 1, self.trip_after, self.trip_after + 1], "doc.pdf",
        )


def _row(path) -> dict:
    """The pipeline row of *path*, read through a fresh client the way the next process finds it."""
    content_hash = _sha256(path)
    db = HttpPipelineDB()
    (row,) = [r for r in db._get("/v1/pipeline/list")["pipelines"]
              if r["content_hash"] == content_hash and r["pdf_path"] == str(path)]
    db._pipeline_ids[content_hash] = int(row["pipeline_id"])
    row["_pages"] = [p["page_index"] for p in db.read_pages(content_hash)]
    return row


def test_a_breaker_trip_fails_the_row_keeps_the_buffer_and_a_rerun_resumes(
    tmp_path, monkeypatch,
) -> None:
    marker = "tr1rj-breaker"
    lines = _lines(marker, 7)
    ex = type("Extractor", (_TrippingExtractor,), {"lines": lines, "calls": [], "trip_after": 3})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    path = _write_pdf(tmp_path, marker)
    every = [_sha(x) for x in lines]

    with monkeypatch.context() as bounded:
        bound_the_polling(bounded)
        with _traffic() as tripped, pytest.raises(MineruSystemicFailure) as info:
            _index(path, marker, streaming="always")

    # The failure names the class and the worker's line, end to end.
    assert info.value.failure_class == _CLASS
    assert _LINE in str(info.value)

    # The row is failed and its buffer is KEPT: the pages the run extracted are still there, and
    # the engine was never asked to wipe the buffer.
    row = _row(path)
    assert row["status"] == "failed", row
    assert row["_pages"] == [0, 1, 2], "the three extracted pages survive the failure"
    assert "pipeline:/v1/pipeline/clear_wal" not in tripped.events
    assert _LINE in str(row.get("error", "")), "the failure message with the stderr line is the audit record"

    # Nothing of the document was written: no manifest request, no chunk upload, no owner row.
    assert tripped.data() == [], "no catalog manifest was written"
    assert tripped.upserts == [], "no chunk was uploaded"
    doc, created = _register(path, marker)
    assert created, "the failed run left no catalog document behind (its fresh registration rolled back)"
    assert _manifest(doc) == []
    assert _present(_COLLECTION, every) == set()

    # The install is "repaired"; the rerun resumes: only the pages after the kept ones are written.
    ex.trip_after = None
    written: list[int] = []
    real_write_page = HttpPipelineDB.write_page

    def _record(self, content_hash, page_index, page_text, metadata=None):
        written.append(page_index)
        return real_write_page(self, content_hash, page_index, page_text, metadata=metadata)

    with monkeypatch.context() as rerun_ctx:
        bound_the_polling(rerun_ctx)
        rerun_ctx.setattr(HttpPipelineDB, "write_page", _record)
        n = _index(path, marker, streaming="always")

    assert n == len(every)
    assert sorted(written) == [3, 4, 5, 6], "pages 1-3 were kept; the rerun wrote only the rest"
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
    assert _present(_COLLECTION, every) == set(every)
    assert _index_state(doc) == "complete"
