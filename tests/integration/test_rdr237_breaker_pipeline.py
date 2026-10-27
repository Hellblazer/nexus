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

``test_the_auto_path_trip_keeps_the_buffer_without_the_window_and_a_rerun_completes`` repeats the
trip through the real ``PDFExtractor`` on the default ``extractor="auto"`` path (the first test
fakes ``extract``), and adds that the pages counted toward the trip are not in the kept buffer.
"""
from __future__ import annotations

import contextlib
from unittest.mock import patch

import pymupdf
import pytest

from nexus.db.http_pipeline_client import HttpPipelineDB
from nexus.doc_indexer import _sha256
from nexus.pdf_extractor import MineruSystemicFailure, MineruWorkerError, PDFExtractor
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


# ── the same exit through the REAL extractor on the default extractor=auto path ──────────────


class _FailingMineruExtractor(PDFExtractor):
    """The real ``PDFExtractor`` (ladder, breaker window, ``_route_to_mineru``) over a fake MinerU.

    Pages named in ``failing`` (1-based) fail both rungs with one class; every other page returns
    its line. The earlier test fakes ``extract`` wholesale, so it cannot see what ``extract`` does
    to the exception on its way out.
    """

    lines: list[str] = []
    failing: set[int] = set()

    def _mineru_run_isolated(self, pdf_path, start, end, formula_enable=True):
        if start + 1 in self.failing:
            raise MineruWorkerError(
                "MinerU subprocess exited with code 1", returncode=1, stderr_last_line=_LINE,
            )
        return self.lines[start], [], [{"page_idx": 0, "para_blocks": []}]


def _write_real_pdf(tmp_path, marker: str, lines: list[str]):
    path = tmp_path / f"doc-{marker}.pdf"
    doc = pymupdf.open()
    for line in lines:
        doc.new_page().insert_text((72, 72), line)
    doc.save(str(path))
    doc.close()
    return path.resolve()


def test_the_auto_path_trip_keeps_the_buffer_without_the_window_and_a_rerun_completes(
    tmp_path, monkeypatch,
) -> None:
    marker = "tr1rj-auto"
    lines = _lines(marker, 7)
    ex = type("Extractor", (_FailingMineruExtractor,), {"lines": lines, "failing": {4, 5, 6}})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    path = _write_real_pdf(tmp_path, marker, lines)
    every = [_sha(x) for x in lines]

    with contextlib.ExitStack() as routed:
        # A formula-routed PDF: extract(extractor="auto") takes _route_to_mineru, the hop that
        # used to wrap the breaker in a RuntimeError.
        routed.enter_context(patch("nexus.pdf_extractor._has_formulas_quick", return_value=10))
        routed.enter_context(patch("nexus.pdf_extractor.do_parse", object()))
        routed.enter_context(patch("nexus.pdf_stack.require"))
        routed.enter_context(patch("nexus.config.get_mineru_page_batch", return_value=1))
        routed.enter_context(patch("nexus.config.get_pdf_ladder_breaker", return_value=3))

        with monkeypatch.context() as bounded:
            bound_the_polling(bounded)
            with _traffic() as tripped, pytest.raises(MineruSystemicFailure) as info:
                _index(path, marker, streaming="always")

        assert info.value.failure_class == _CLASS
        row = _row(path)
        assert row["status"] == "failed", row
        assert row["_pages"] == [0, 1, 2], (
            "pages 1 to 3 were kept; pages 4 and 5 counted toward the trip and were held, "
            "so none of the window is in the buffer"
        )
        assert "pipeline:/v1/pipeline/clear_wal" not in tripped.events, (
            "the typed failure reached the pipeline: wrapped in a RuntimeError it wipes the buffer"
        )
        assert tripped.upserts == [] and tripped.data() == []

        ex.failing = set()
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
    assert sorted(written) == [3, 4, 5, 6], "the rerun wrote the window's pages (MinerU text) and the rest"
    doc, _ = _register(path, marker)
    assert _manifest(doc) == [(i, h) for i, h in enumerate(every)]
    assert _present(_COLLECTION, every) == set(every)
    assert _index_state(doc) == "complete"
