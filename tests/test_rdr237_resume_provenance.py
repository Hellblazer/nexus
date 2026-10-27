# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.3, nexus-q6nw0 item 6): a resumed run records the buffer's pages, not only its own.

After a kept-buffer exit (the breaker's, an interrupt's) the rerun re-extracts every page but WRITES
only the pages the buffer lacks, so the chunks come from the buffer's text. The rung a page settled
on rides its buffer row (``page_method`` in the page metadata the ladder hands ``on_page``), and
``extractor_loop`` rebuilds the document's ``extraction_pages`` and ``extraction_method`` from those
rows, so the catalog record describes what was indexed. Unit level: the ladder against a fake MinerU,
``extractor_loop`` against a fake buffer. The engine-backed half is
``tests/integration/test_rdr237_catalog_extraction.py``.
"""
from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_extractor import ExtractionResult, PDFExtractor
from nexus.pipeline_stages import _merge_buffered_provenance, _page_rungs, extractor_loop
from tests.test_rdr237_ladder import FakeMineru, make_pdf, run_ladder, worker_exit

_HASH = "h" * 64


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.delenv("NX_MINERU_FAULT_PAGES", raising=False)
    monkeypatch.setattr("nexus.config._LADDER_BREAKER_WARNED", set())


# ── the ladder records each page's rung in the metadata it hands on_page ─────────────────────


def test_each_page_carries_the_rung_it_settled_on(tmp_path: Path) -> None:
    texts = ["alpha one", "bravo two", "charlie three", ""]
    plan = {
        (2, 1): worker_exit(1, "boom"),                                # page 2: rung 2
        (3, 1): worker_exit(1, "boom"), (3, 2): worker_exit(1, "boom"),  # page 3: its text layer
        (4, 1): worker_exit(1, "boom"), (4, 2): worker_exit(1, "boom"),  # page 4: no text layer
    }
    _, delivered, _, _ = run_ladder(tmp_path, texts, plan, breaker=0)
    assert {i: m["page_method"] for i, _t, m in delivered} == {
        0: "mineru", 1: "mineru-no-formula", 2: "pymupdf-text", 3: "unextracted",
    }


def test_a_page_the_breaker_held_back_and_then_delivered_still_names_its_rung(tmp_path: Path) -> None:
    """With the breaker armed, a page failing both rungs is HELD until the window resolves; page 3
    succeeds, which releases it. The held page reaches ``on_page`` late, from ``_flush_held``, and
    must carry the rung it settled on all the same."""
    plan = {(2, 1): worker_exit(1, "boom"), (2, 2): worker_exit(1, "boom")}
    result, delivered, _, _ = run_ladder(tmp_path, ["alpha one", "bravo two", "charlie three"], plan, breaker=3)
    assert [i for i, _t, _m in delivered] == [0, 1, 2], "delivered in page order"
    assert {i: m["page_method"] for i, _t, m in delivered} == {0: "mineru", 1: "pymupdf-text", 2: "mineru"}
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2]}


def test_a_batch_names_the_rung_for_every_page_it_covers(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "doc.pdf", ["a", "b", "c", "d"])
    ext = PDFExtractor()
    delivered: list[tuple[int, str, dict]] = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("nexus.pdf_extractor.do_parse", object()))
        stack.enter_context(patch("nexus.config.get_mineru_page_batch", return_value=2))
        stack.enter_context(patch("nexus.config.get_mineru_page_timeout_s", return_value=60))  # 60 x 2 <= 240 s
        stack.enter_context(patch.object(ext, "_mineru_run_isolated", side_effect=FakeMineru()))
        ext._extract_with_mineru(pdf, on_page=lambda i, t, m: delivered.append((i, t, m)))
    assert [(i, m["page_numbers"], m["page_method"]) for i, _t, m in delivered] == [
        (0, [1, 2], "mineru"), (2, [3, 4], "mineru"),
    ]
    assert _page_rungs(delivered[0][2], 0) == {1: "mineru", 2: "mineru"}


# ── reading the rung back ────────────────────────────────────────────────────


def test_page_rungs_reads_a_dict_a_json_string_and_a_batch() -> None:
    assert _page_rungs({"page_number": 4, "page_method": "pymupdf-text"}, 3) == {4: "pymupdf-text"}
    assert _page_rungs(json.dumps({"page_number": 4, "page_method": "pymupdf-text"}), 3) == {4: "pymupdf-text"}
    assert _page_rungs({"page_numbers": [5, 6], "page_method": "mineru"}, 4) == {5: "mineru", 6: "mineru"}
    assert _page_rungs({"page_method": "mineru"}, 7) == {8: "mineru"}, "no page number: the row's index + 1"


def test_page_rungs_does_not_read_a_bool_as_a_page_number() -> None:
    """``True`` is an ``int`` to Python and would name page 1."""
    assert _page_rungs({"page_numbers": [True, 2], "page_method": "mineru"}, 0) == {2: "mineru"}
    assert _page_rungs({"page_number": True, "page_method": "mineru"}, 0) == {}


@pytest.mark.parametrize("bad", [None, "", "not json", "[1]", {}, {"page_number": 1}, {"page_method": ""},
                                 {"page_number": 1, "page_method": 3}])
def test_page_rungs_of_a_row_without_a_rung_is_empty(bad) -> None:
    assert _page_rungs(bad, 0) == {}


def test_the_merge_lets_the_buffers_rung_win_for_the_pages_it_holds() -> None:
    meta = {"extraction_method": "mineru", "extraction_pages": {}}
    _merge_buffered_provenance(meta, {1: "mineru", 2: "mineru", 3: "mineru"}, {2: "mineru-no-formula"})
    assert meta == {"extraction_method": "mineru+degraded", "extraction_pages": {"mineru-no-formula": [2]}}


def test_the_merge_drops_a_degradation_the_buffer_does_not_hold() -> None:
    """Page 2 degraded in this run, but the buffer holds its clean text from the earlier run."""
    meta = {"extraction_method": "mineru+degraded", "extraction_pages": {"mineru-no-formula": [2]}}
    _merge_buffered_provenance(meta, {1: "mineru", 2: "mineru-no-formula"}, {1: "mineru", 2: "mineru"})
    assert meta == {"extraction_method": "mineru", "extraction_pages": {}}


def test_the_merge_leaves_a_legacy_method_alone_and_does_nothing_without_buffered_rungs() -> None:
    legacy = {"extraction_method": "docling", "extraction_pages": {}}
    _merge_buffered_provenance(legacy, {}, {1: "mineru-no-formula"})
    assert legacy["extraction_method"] == "docling", "only the ladder's own methods are recomputed"
    assert legacy["extraction_pages"] == {"mineru-no-formula": [1]}
    # The explicit pymupdf extractor (bead tr1rj.4) names its own method; a resume must not turn
    # it into a MinerU one, with a known degraded buffered rung or an unknown buffered page.
    for method in ("pymupdf", "pymupdf_normalized", "mineru+docling-degraded"):
        current = {"extraction_method": method, "extraction_pages": {}}
        _merge_buffered_provenance(current, {}, {1: "mineru-no-formula"}, [2])
        assert current["extraction_method"] == method, f"{method} is not the ladder's to rewrite"
        unknown_only = {"extraction_method": method, "extraction_pages": {}}
        _merge_buffered_provenance(unknown_only, {}, {}, [2])
        assert unknown_only == {"extraction_method": method, "extraction_pages": {}}, "nothing to merge for it"
    same = {"extraction_method": "mineru", "extraction_pages": {"unextracted": [4]}}
    _merge_buffered_provenance(same, {4: "unextracted"}, {})
    assert same == {"extraction_method": "mineru", "extraction_pages": {"unextracted": [4]}}, "no rung in the buffer: the run's own map stands"


# ── extractor_loop over a kept buffer ────────────────────────────────────────


def _db(buffer_rungs: list[str | None], *, total_pages=None) -> MagicMock:
    """A pipeline row with ``len(buffer_rungs)`` pages buffered; ``None``: a row an older version wrote."""
    db = MagicMock()
    db.get_pipeline_state.return_value = {
        "pages_extracted": len(buffer_rungs), "total_pages": total_pages, "extraction_meta": None,
    }
    db.read_pages.return_value = [
        {"page_index": i, "page_text": f"text {i}",
         "metadata_json": json.dumps({"page_number": i + 1} | ({"page_method": r} if r else {}))}
        for i, r in enumerate(buffer_rungs)
    ]
    return db


def _resume(monkeypatch, db, run_rungs: list[str], metadata: dict) -> ExtractionResult:
    """Run ``extractor_loop`` with an extractor that re-extracts every page, settling page ``i`` on ``run_rungs[i]``."""

    class Extractor:
        def extract(self, pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None,
                    allow_degraded=False, on_heartbeat=None):
            for i, rung in enumerate(run_rungs):
                on_page(i, f"text {i}", {"page_number": i + 1, "text_length": 6, "page_method": rung})
            return ExtractionResult(text="", metadata=dict(metadata))

    monkeypatch.setattr("nexus.pipeline_stages.PDFExtractor", Extractor)
    return extractor_loop(Path("/tmp/x.pdf"), _HASH, db, threading.Event())


def _warnings(cap) -> list[dict]:
    return [e for e in cap if e.get("event") == "resume_provenance_unknown"]


def _stored(db: MagicMock) -> dict:
    ((_hash, meta),) = [c.args for c in db.store_extraction_metadata.call_args_list]
    return meta


def test_a_resume_records_the_degraded_page_the_buffer_holds_though_the_rerun_got_it_clean(monkeypatch) -> None:
    """Run 1 buffered pages 1-3 with page 2 on rung 2 and was interrupted. The rerun gets all five
    pages from MinerU clean (its own map is empty), but the indexed text of page 2 is rung 2's."""
    db = _db(["mineru", "mineru-no-formula", "mineru"])
    result = _resume(
        monkeypatch, db, ["mineru"] * 5,
        {"extraction_method": "mineru", "extraction_pages": {}, "page_count": 5},
    )
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [2]}
    assert result.metadata["extraction_method"] == "mineru+degraded"
    assert _stored(db) == result.metadata, "the resume buffer's own record carries it too"
    written = [c.args[1] for c in db.write_page.call_args_list]
    assert written == [3, 4], "only the pages the buffer lacked were written"


def test_a_resume_does_not_record_a_degradation_the_buffer_does_not_hold(monkeypatch) -> None:
    db = _db(["mineru", "mineru", "mineru"])
    result = _resume(
        monkeypatch, db, ["mineru", "mineru-no-formula", "mineru", "mineru", "pymupdf-text"],
        {"extraction_method": "mineru+degraded",
         "extraction_pages": {"mineru-no-formula": [2], "pymupdf-text": [5]}, "page_count": 5},
    )
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [5]}, "page 2's text is the buffer's clean one"
    assert result.metadata["extraction_method"] == "mineru+degraded"


def test_a_resume_over_a_buffer_an_older_version_wrote_lists_no_rung_for_its_pages(monkeypatch) -> None:
    """Pages 1-3 are buffered with no rung: the run's rung for page 2 does not describe its
    indexed (buffered) text, so the map leaves it out and the method stays degraded."""
    db = _db([None, None, None])
    result = _resume(
        monkeypatch, db, ["mineru", "mineru-no-formula", "mineru", "mineru"],
        {"extraction_method": "mineru+degraded", "extraction_pages": {"mineru-no-formula": [2]}, "page_count": 4},
    )
    assert result.metadata["extraction_pages"] == {}
    assert result.metadata["extraction_method"] == "mineru+degraded"


def test_a_clean_rerun_over_an_older_buffer_does_not_claim_a_clean_method(monkeypatch) -> None:
    """The buffer's pages came from a build that recorded no rung, so the record cannot prove them
    clean even though this run got every page from MinerU: the method stays ``mineru+degraded``, the
    map lists only what is known (here nothing), and the run says why."""
    from structlog.testing import capture_logs

    db = _db([None, None, None])
    with capture_logs() as cap:
        result = _resume(
            monkeypatch, db, ["mineru"] * 4,
            {"extraction_method": "mineru", "extraction_pages": {}, "page_count": 4},
        )
    assert result.metadata["extraction_method"] == "mineru+degraded"
    assert result.metadata["extraction_pages"] == {}
    assert _stored(db) == result.metadata
    (warning,) = _warnings(cap)
    assert warning["file_path"] == "/tmp/x.pdf" and warning["content_hash"] == _HASH
    assert warning["buffered_rows_without_a_rung"] == 3 and warning["buffered_rows_with_a_rung"] == 0
    assert warning["log_level"] == "warning"


def test_a_buffer_that_names_a_rung_for_some_pages_only_is_unproven_for_the_rest(monkeypatch) -> None:
    """Mixed buffer: page 1 names rung 2, page 2 names none, page 3 names rung 1. The known rung
    is recorded; page 2 is unknown, so the method is still not a clean ``mineru``."""
    from structlog.testing import capture_logs

    db = _db(["mineru-no-formula", None, "mineru"])
    with capture_logs() as cap:
        result = _resume(
            monkeypatch, db, ["mineru"] * 5,
            {"extraction_method": "mineru", "extraction_pages": {}, "page_count": 5},
        )
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [1]}
    assert result.metadata["extraction_method"] == "mineru+degraded"
    (warning,) = _warnings(cap)
    assert warning["buffered_rows_without_a_rung"] == 1 and warning["buffered_rows_with_a_rung"] == 2


def test_a_mixed_buffer_whose_known_pages_are_clean_is_still_unproven(monkeypatch) -> None:
    from structlog.testing import capture_logs

    db = _db(["mineru", None])
    with capture_logs() as cap:
        result = _resume(
            monkeypatch, db, ["mineru"] * 3,
            {"extraction_method": "mineru", "extraction_pages": {}, "page_count": 3},
        )
    assert result.metadata["extraction_pages"] == {}
    assert result.metadata["extraction_method"] == "mineru+degraded", "page 2's rung is unknown"
    assert len(_warnings(cap)) == 1


def test_a_buffer_whose_rows_all_name_a_rung_logs_nothing(monkeypatch) -> None:
    from structlog.testing import capture_logs

    db = _db(["mineru", "mineru"])
    with capture_logs() as cap:
        result = _resume(
            monkeypatch, db, ["mineru"] * 3,
            {"extraction_method": "mineru", "extraction_pages": {}, "page_count": 3},
        )
    assert result.metadata["extraction_method"] == "mineru"
    assert _warnings(cap) == []


def test_a_first_run_is_untouched(monkeypatch) -> None:
    db = _db([])
    result = _resume(
        monkeypatch, db, ["mineru", "mineru-no-formula"],
        {"extraction_method": "mineru+degraded", "extraction_pages": {"mineru-no-formula": [2]}, "page_count": 2},
    )
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [2]}


def test_a_non_ladder_extractor_on_a_resume_is_untouched(monkeypatch) -> None:
    """No ``page_method`` anywhere (docling, pymupdf): nothing to merge, the method stays as reported."""

    class Extractor:
        def extract(self, pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None,
                    allow_degraded=False, on_heartbeat=None):
            for i in range(3):
                on_page(i, f"text {i}", {"page_number": i + 1, "text_length": 6})
            return ExtractionResult(text="", metadata={"extraction_method": "pymupdf_normalized", "page_count": 3})

    from structlog.testing import capture_logs

    monkeypatch.setattr("nexus.pipeline_stages.PDFExtractor", Extractor)
    db = _db([None, None])
    with capture_logs() as cap:
        result = extractor_loop(Path("/tmp/x.pdf"), _HASH, db, threading.Event())
    assert result.metadata == {"extraction_method": "pymupdf_normalized", "page_count": 3}
    assert _warnings(cap) == [], "rows with no rung are the norm for a non-ladder extractor"


def test_a_complete_row_whose_buffer_was_wiped_falls_through_and_still_merges(monkeypatch) -> None:
    """The fast path trusts the counters only when the buffer backs them. Here it holds two of the
    three pages the row claims: the run re-extracts (the nexus-gl99l fall-through) over those two,
    and their rungs still reach the record."""
    db = MagicMock()
    db.get_pipeline_state.return_value = {"pages_extracted": 3, "total_pages": 3, "extraction_meta": "{}"}
    db.read_pages.return_value = [
        {"page_index": 0, "page_text": "t", "metadata_json": json.dumps({"page_number": 1, "page_method": "mineru"})},
        {"page_index": 1, "page_text": "t",
         "metadata_json": json.dumps({"page_number": 2, "page_method": "unextracted"})},
    ]
    result = _resume(
        monkeypatch, db, ["mineru"] * 3,
        {"extraction_method": "mineru", "extraction_pages": {}, "page_count": 3},
    )
    assert [c.args[1] for c in db.write_page.call_args_list] == [2], "only the page the buffer lacked"
    assert result.metadata["extraction_pages"] == {"unextracted": [2]}
    assert result.metadata["extraction_method"] == "mineru+degraded"


class _PageText(str):
    """A page's text that can be weakly referenced, to see whether anything keeps it alive."""


@pytest.mark.parametrize("state", [
    {"pages_extracted": 3, "total_pages": None, "extraction_meta": None},
    {"pages_extracted": 4, "total_pages": 4, "extraction_meta": "{}"},
], ids=["interrupted-run", "complete-row-with-a-wiped-buffer"])
def test_the_buffered_page_text_is_not_kept_alive_while_the_extraction_runs(monkeypatch, state) -> None:
    """A resume reads the whole buffer, text included, but needs only each row's rung. Holding
    the rows would keep every buffered page's text in memory for the whole (long) extraction."""
    import gc
    import weakref

    refs: list[weakref.ref] = []

    def read_pages(_hash):
        rows = []
        for i in range(3):
            text = _PageText(f"page {i} " * 1000)
            refs.append(weakref.ref(text))
            rows.append({"page_index": i, "page_text": text,
                         "metadata_json": json.dumps({"page_number": i + 1, "page_method": "mineru"})})
        return rows

    db = _db([None, None, None])
    db.get_pipeline_state.return_value = state
    db.read_pages.side_effect = read_pages
    alive_during_extraction: list[bool] = []

    class Extractor:
        def extract(self, pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None,
                    allow_degraded=False, on_heartbeat=None):
            gc.collect()
            alive_during_extraction.extend(r() is not None for r in refs)
            return ExtractionResult(text="", metadata={"extraction_method": "mineru", "extraction_pages": {},
                                                       "page_count": 3})

    monkeypatch.setattr("nexus.pipeline_stages.PDFExtractor", Extractor)
    extractor_loop(Path("/tmp/x.pdf"), _HASH, db, threading.Event())
    assert len(alive_during_extraction) == 3, "the buffer was read"
    assert not any(alive_during_extraction), "the rows were dropped once their rungs were read"


def test_an_unknown_buffered_page_is_never_given_the_reruns_rung(monkeypatch) -> None:
    """Pages 1-2 are buffered with no rung; the rerun degraded page 2 and page 3. Page 2's indexed
    text is the buffer's, so the run's rung does not describe it: only page 3 is listed, and the
    warning names the unknown pages."""
    from structlog.testing import capture_logs

    db = _db([None, None])
    with capture_logs() as cap:
        result = _resume(
            monkeypatch, db, ["mineru", "pymupdf-text", "pymupdf-text"],
            {"extraction_method": "mineru+degraded", "extraction_pages": {"pymupdf-text": [2, 3]},
             "page_count": 3},
        )
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [3]}
    assert result.metadata["extraction_method"] == "mineru+degraded"
    (warning,) = _warnings(cap)
    assert warning["first_pages_without_a_rung"] == [1, 2]
