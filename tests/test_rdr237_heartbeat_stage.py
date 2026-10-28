# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.2): the streaming pipeline's heartbeat, unit level.

``extractor_loop`` passes ``PDFExtractor.extract`` an ``on_heartbeat`` that re-sends
``pages_extracted = max(the row's value when the run began, the last settled page + 1)`` through
``update_progress``. Against the fake engine, with the extractor faked. The engine-backed half (the row's
``updated_at`` moves) is ``tests/integration/test_rdr237_heartbeat_pipeline.py``.
"""
from __future__ import annotations

import ast
import inspect
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus import doc_indexer
from nexus.db.http_pipeline_client import PipelineRunFenced
from nexus.pdf_extractor import ExtractionResult
from nexus.pipeline_stages import PipelineCancelled, extractor_loop

_HASH = "h" * 64


def _loop(monkeypatch, db, script, *, cancel: threading.Event | None = None) -> ExtractionResult:
    class Extractor:
        def extract(self, pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None,
                    allow_degraded=False, on_heartbeat=None):
            assert on_heartbeat is not None
            script(on_page, on_heartbeat)
            return ExtractionResult(text="", metadata={"page_count": 0, "table_regions": []})

    monkeypatch.setattr("nexus.pipeline_stages.PDFExtractor", Extractor)
    return extractor_loop(Path("/tmp/x.pdf"), _HASH, db, cancel or threading.Event())


def _db(*, row_pages: int, buffer_pages: int, total_pages=None) -> MagicMock:
    db = MagicMock()
    db.get_pipeline_state.return_value = {
        "pages_extracted": row_pages, "total_pages": total_pages, "extraction_meta": None,
    }
    db.read_pages.return_value = [{"page_index": i} for i in range(buffer_pages)]
    return db


def _sent(db: MagicMock) -> list[int]:
    """The ``pages_extracted`` values sent, in order."""
    return [
        c.kwargs["pages_extracted"] for c in db.update_progress.call_args_list if "pages_extracted" in c.kwargs
    ]


def test_a_heartbeat_re_sends_the_counter_the_row_already_has(monkeypatch) -> None:
    db = _db(row_pages=0, buffer_pages=0)

    def script(on_page, on_heartbeat) -> None:
        on_page(0, "one", {"page_number": 1})
        on_page(1, "two", {"page_number": 2})
        on_heartbeat()

    _loop(monkeypatch, db, script)
    assert _sent(db) == [1, 2, 2]


def test_a_heartbeat_before_the_first_page_sends_zero_on_a_fresh_row(monkeypatch) -> None:
    db = _db(row_pages=0, buffer_pages=0)
    _loop(monkeypatch, db, lambda on_page, on_heartbeat: on_heartbeat())
    assert _sent(db) == [0]


def test_a_resumed_runs_heartbeat_never_sends_less_than_the_rows_counter(monkeypatch) -> None:
    """The row says 3 pages and the buffer holds 3: the run skips them, and its heartbeat sends 3."""
    db = _db(row_pages=3, buffer_pages=3)
    _loop(monkeypatch, db, lambda on_page, on_heartbeat: on_heartbeat())
    assert _sent(db) == [3]


def test_a_buffer_that_is_shorter_than_the_rows_counter_does_not_lower_it(monkeypatch) -> None:
    """The row says 5 and the buffer is empty (the resume checks put the run's start at 0): the
    heartbeat, called before any page, sends the row's 5 and not the run's 0."""
    db = _db(row_pages=5, buffer_pages=0)

    def script(on_page, on_heartbeat) -> None:
        on_heartbeat()
        on_page(0, "one", {"page_number": 1})  # the run re-extracts from page 1, as on_page always has
        on_heartbeat()

    _loop(monkeypatch, db, script)
    sent = _sent(db)
    assert sent[0] == 5, "the first heartbeat did not lower the counter"
    assert sent[2] == 5, "and neither did the next"


def test_a_heartbeat_after_a_cancel_raises_the_pipelines_own_cancel(monkeypatch) -> None:
    db = _db(row_pages=0, buffer_pages=0)
    cancel = threading.Event()
    cancel.set()
    seen: list[BaseException] = []

    def script(on_page, on_heartbeat) -> None:
        try:
            on_heartbeat()
        except BaseException as exc:  # noqa: BLE001 — the test reads what the callback raised
            seen.append(exc)
            raise

    result = _loop(monkeypatch, db, script, cancel=cancel)
    assert isinstance(seen[0], PipelineCancelled)
    assert result.metadata["page_count"] == 0, "extractor_loop turns the cancel into an empty result"
    assert _sent(db) == [], "a cancelled run writes nothing"


def test_a_fenced_flush_inside_the_heartbeat_reaches_the_caller(monkeypatch) -> None:
    db = _db(row_pages=0, buffer_pages=0)
    fenced = PipelineRunFenced("stale", pipeline_id=1, content_hash=_HASH, run_epoch=1, current_epoch=2, remedy="")
    db.update_progress.side_effect = fenced
    with pytest.raises(PipelineRunFenced) as info:
        _loop(monkeypatch, db, lambda on_page, on_heartbeat: on_heartbeat())
    assert info.value is fenced


def test_the_batch_path_passes_no_heartbeat() -> None:
    """``index_pdf``'s all-in-memory path has no pipeline row to stamp (the bead decided; the RDR's
    Progress section says "the same", which the bead overrides)."""
    tree = ast.parse(inspect.getsource(doc_indexer))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "extract"
        and isinstance(n.func.value, ast.Call) and getattr(n.func.value.func, "id", "") == "PDFExtractor"
    ]
    assert calls, "non-vacuity: the batch path's PDFExtractor().extract call was found"
    for call in calls:
        assert "on_heartbeat" not in {kw.arg for kw in call.keywords}
