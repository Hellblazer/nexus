# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.2): the extractor's heartbeat reaches the engine's pipeline row.

``extractor_loop`` hands ``PDFExtractor.extract`` an ``on_heartbeat`` that re-sends the row's
``pages_extracted`` through ``update_progress``. Against the REAL engine substrate of
``tests/integration/test_rdr223_pdf_journey.py``, with only the extractor faked (it calls the
callbacks the way the real one does):

* with a page batch pending, ``update_progress`` does not flush at once; the write rides the next flush,
  which the chunker's poll triggers about every 0.5 s through ``read_pages_from``; the row's
  ``updated_at`` moves within 2 s of the heartbeat;
* with nothing pending, the heartbeat flushes at once and moves ``updated_at`` by itself;
* a run that resumes a row never lowers its ``pages_extracted`` with a heartbeat (the buffer may have
  been wiped while the counter survived).
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from nexus.db.http_pipeline_client import HttpPipelineDB
from nexus.pdf_extractor import ExtractionResult
from nexus.pipeline_stages import extractor_loop
from tests.integration.test_rdr223_pdf_journey import _COLLECTION

pytestmark = [pytest.mark.integration]


def _sha(marker: str) -> str:
    return hashlib.sha256(marker.encode()).hexdigest()


def _row(db: HttpPipelineDB, content_hash: str) -> dict:
    """The row as the engine holds it, read WITHOUT flushing this client's buffers (a state read
    flushes first, which would hide what the heartbeat alone did)."""
    pipeline_id = db.pipeline_id_for(content_hash)
    (row,) = [r for r in db._get("/v1/pipeline/list")["pipelines"] if int(r["pipeline_id"]) == pipeline_id]
    return row


def _stamp(row: dict) -> datetime:
    return datetime.fromisoformat(row["updated_at"])


class _Chunker(threading.Thread):
    """What the real chunker does to the buffer: read the pages from the row about every 0.5 s,
    which flushes whatever the extractor buffered."""

    def __init__(self, db: HttpPipelineDB, content_hash: str) -> None:
        super().__init__(daemon=True)
        self.db, self.content_hash = db, content_hash
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(0.5):
            self.db.read_pages_from(self.content_hash, 0)


def _extractor_class(script):
    """A ``PDFExtractor`` stand-in: ``script(on_page, on_heartbeat)`` plays the extraction."""
    class Extractor:
        def extract(self, pdf_path, *, extractor="mineru", on_page=None,
                    allow_degraded=False, on_heartbeat=None):
            assert on_heartbeat is not None, "extractor_loop must hand the extractor a heartbeat"
            script(on_page, on_heartbeat)
            return ExtractionResult(text="", metadata={"page_count": 0, "table_regions": []})

    return Extractor


def _run_loop(monkeypatch, db, content_hash, path, script) -> None:
    monkeypatch.setattr("nexus.pipeline_stages.PDFExtractor", _extractor_class(script))
    extractor_loop(path, content_hash, db, threading.Event(), extractor="auto")


def _page(n: int) -> dict:
    return {"page_number": n + 1, "text_length": 3}


def test_a_heartbeat_with_a_page_batch_pending_rides_the_next_flush_and_moves_updated_at_within_2_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_hash = _sha("tr1rj2-pending")
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    db = HttpPipelineDB()
    db.create_pipeline(content_hash, str(path), _COLLECTION)
    observed: dict = {}

    def script(on_page, on_heartbeat) -> None:
        on_page(0, "page one", _page(0))
        db.flush(content_hash)  # the chunker's first poll: page 1 and its counter are on the row
        observed["after_page_1"] = _row(db, content_hash)
        time.sleep(1.2)  # long enough that a later stamp differs at any clock resolution
        # A second page is buffered and has not been flushed yet: a batch is PENDING.
        db.write_page(content_hash, 1, "page two", metadata=_page(1))
        assert db._page_buffer.get(content_hash), "non-vacuity: a page batch is pending"
        observed["stamp_before"] = _stamp(_row(db, content_hash))
        on_heartbeat()
        # The heartbeat did not flush: update_progress sees the pending batch and waits for the
        # chunker's flush (the comment at the heartbeat site says so).
        observed["stamp_right_after"] = _stamp(_row(db, content_hash))
        chunker = _Chunker(db, content_hash)
        chunker.start()
        began = time.monotonic()
        try:
            while time.monotonic() - began < 2.0:
                observed["stamp_later"] = _stamp(_row(db, content_hash))
                if observed["stamp_later"] > observed["stamp_right_after"]:
                    break
                time.sleep(0.1)
        finally:
            chunker.stop.set()
            chunker.join(5)
        observed["moved_in"] = time.monotonic() - began

    _run_loop(monkeypatch, db, content_hash, path, script)

    assert observed["stamp_right_after"] == observed["stamp_before"], (
        "with a batch pending the heartbeat is buffered, not sent: it rides the next flush"
    )
    assert observed["stamp_later"] > observed["stamp_before"], "the chunker's flush carried it to the row"
    assert observed["moved_in"] < 2.0, f"updated_at moved after {observed['moved_in']:.2f} s, not within 2 s"


def test_a_heartbeat_with_nothing_pending_flushes_at_once_and_moves_updated_at_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_hash = _sha("tr1rj2-idle")
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    db = HttpPipelineDB()
    db.create_pipeline(content_hash, str(path), _COLLECTION)
    observed: dict = {}

    def script(on_page, on_heartbeat) -> None:
        on_page(0, "page one", _page(0))
        db.flush(content_hash)
        assert not db._page_buffer.get(content_hash) and not db._chunk_buffer.get(content_hash)
        observed["stamp_before"] = _stamp(_row(db, content_hash))
        time.sleep(1.2)
        on_heartbeat()  # a long attempt: no page since the last flush
        observed["stamp_after"] = _stamp(_row(db, content_hash))
        observed["pages_extracted"] = _row(db, content_hash)["pages_extracted"]

    _run_loop(monkeypatch, db, content_hash, path, script)

    assert observed["stamp_after"] > observed["stamp_before"], "the heartbeat alone refreshed the row"
    assert observed["pages_extracted"] == 1, "and re-sent the counter it already had"


def test_a_resumed_runs_heartbeat_never_lowers_pages_extracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first run extracted three pages and died; the resumed run finds its buffer intact and skips
    them, so its first page is the fourth. A heartbeat before that page must re-send the row's 3, not
    a counter that starts at 0. (The row's counter and the buffer disagree only in a unit test of
    ``extractor_loop``: the engine zeroes the counter in the same transaction that wipes the buffer.)"""
    content_hash = _sha("tr1rj2-resume")
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    first = HttpPipelineDB()
    first.create_pipeline(content_hash, str(path), _COLLECTION)
    for i in range(3):
        first.write_page(content_hash, i, f"page {i + 1}", metadata=_page(i))
    first.update_progress(content_hash, pages_extracted=3)
    first.mark_failed(content_hash, "the first run died")  # keeps the buffer, as an interrupted run does
    assert len(first.read_pages(content_hash)) == 3, "non-vacuity: the buffer holds the three pages"
    assert _row(first, content_hash)["pages_extracted"] == 3

    resumed = HttpPipelineDB()
    resumed.create_pipeline(content_hash, str(path), _COLLECTION)
    observed: dict = {}

    def script(on_page, on_heartbeat) -> None:
        on_heartbeat()
        resumed.flush(content_hash)
        observed["after_heartbeat"] = _row(resumed, content_hash)["pages_extracted"]
        on_page(3, "page four", _page(3))
        on_heartbeat()
        resumed.flush(content_hash)
        observed["after_page_four"] = _row(resumed, content_hash)["pages_extracted"]

    _run_loop(monkeypatch, resumed, content_hash, path, script)

    assert observed["after_heartbeat"] == 3, "the heartbeat re-sent the row's counter, not 0"
    assert observed["after_page_four"] == 4
