# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.3): the extraction record on the catalog document, through the REAL engine.

Driven through ``doc_indexer.index_pdf`` against the engine substrate
``tests/integration/test_rdr223_pdf_journey.py`` boots (only the extractor and the chunker are fake). The
fake extractor settles each page on a rung the way the ladder does, puts it in the page metadata
(``page_method``) and reports the document's ``extraction_method`` / ``extraction_pages``.

* the streaming path (``streaming="always"``) and the all-at-once path (``streaming="never"``), the hook's
  two callers, each leave the map on the catalog document's ``meta``, and the chunks carry
  ``mineru+degraded``;
* ``nx store get`` and the MCP ``store_get`` print the degraded pages from a real catalog document;
* a document re-extracted clean after a degraded extraction has an EMPTY map (the service merges ``meta``
  key by key and never removes one; the hook overwrites the key whole);
* a kept-buffer resume records the pages the buffer holds, not only the rerun's own;
* ``on_extraction`` fires for an extraction and not for a staleness skip;
* a swallowed catalog error in the all-at-once path is logged, naming the document.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import http_vector_client as hvc
from nexus.db.http_pipeline_client import HttpPipelineDB
from nexus.mcp import core as mcp_core
from nexus.pdf_extractor import ExtractionResult, MineruSystemicFailure
from tests._bounded_polling import bound_the_polling
from tests.integration.test_rdr223_pdf_journey import (
    _COLLECTION,
    _FakeChunker,
    _index,
    _lines,
    _reader,
    _register,
    _sha,
    _traffic,
    _write_pdf,
)

pytestmark = [pytest.mark.integration]

_DEGRADED_RUNGS = ("mineru-no-formula", "pymupdf-text", "unextracted")


class _LadderExtractor:
    """One page per line; page ``i`` settles on ``rungs[i]`` (``mineru`` is rung 1)."""

    lines: list[str] = []
    rungs: list[str] = []
    #: when set, deliver only this many pages and then trip the breaker (the kept-buffer exit)
    trip_after: int | None = None
    calls: list[str] = []

    def extract(self, pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None,
                allow_degraded=False, on_heartbeat=None):
        self.calls.append(str(pdf_path))
        pos, bounds = 0, []
        pages: dict[str, list[int]] = {}
        for i, line in enumerate(self.lines):
            if self.trip_after is not None and i >= self.trip_after:
                raise MineruSystemicFailure("exit:1:boom", "boom", [i, i + 1, i + 2], "doc.pdf")
            rung = self.rungs[i]
            if rung != "mineru":
                pages.setdefault(rung, []).append(i + 1)
            if on_page:
                on_page(i, line, {"page_number": i + 1, "text_length": len(line), "page_method": rung})
            bounds.append({"page_number": i + 1, "start_char": pos, "page_text_length": len(line) + 1})
            pos += len(line) + 1
        return ExtractionResult(
            text="\n".join(self.lines),
            metadata={"extraction_method": "mineru+degraded" if pages else "mineru", "extraction_pages": pages,
                      "page_count": len(self.lines), "page_boundaries": bounds, "table_regions": [],
                      "format": "markdown"})


@pytest.fixture()
def ladder(monkeypatch):
    """Route both indexers' extraction through a fake ladder; returns the extractor class to script."""
    ex = type("Extractor", (_LadderExtractor,), {"lines": [], "rungs": [], "calls": [], "trip_after": None})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    return ex


def _meta(path, marker) -> dict:
    doc, _created = _register(path, marker)
    entry = _reader().resolve(doc)
    assert entry is not None
    return dict(entry.meta)


def _chunk_method(line: str) -> str:
    entry = hvc.HttpVectorClient().get_by_id(_COLLECTION, _sha(line))
    assert entry is not None
    return entry.get("extraction_method", "")


_BOTH = pytest.mark.parametrize("streaming", ["always", "never"], ids=["streaming", "all-at-once"])
_RUNGS = ["mineru", "mineru-no-formula", "mineru", "pymupdf-text"]
_MAP = {"mineru-no-formula": [2], "pymupdf-text": [4]}


@_BOTH
def test_a_degraded_document_carries_its_map_on_the_catalog_and_its_chunks_the_method(
    tmp_path, ladder, streaming,
) -> None:
    marker = f"tr1rj3-degraded-{streaming}"
    lines = _lines(marker, 4)
    ladder.lines, ladder.rungs = lines, list(_RUNGS)
    path = _write_pdf(tmp_path, marker)

    assert _index(path, marker, streaming=streaming) == 4

    meta = _meta(path, marker)
    assert meta["extraction_pages"] == _MAP
    assert meta["extraction_method"] == "mineru+degraded"
    assert {_chunk_method(x) for x in lines} == {"mineru+degraded"}


@_BOTH
def test_store_get_prints_the_degraded_pages_of_a_real_catalog_document(tmp_path, ladder, streaming) -> None:
    marker = f"tr1rj3-print-{streaming}"
    lines = _lines(marker, 4)
    ladder.lines, ladder.rungs = lines, list(_RUNGS)
    path = _write_pdf(tmp_path, marker)
    _index(path, marker, streaming=streaming)

    expected = ["Extractor:  mineru+degraded", "Degraded:   mineru-no-formula 2; pymupdf-text 4"]
    cli = CliRunner().invoke(main, ["store", "get", _sha(lines[0]), "--collection", _COLLECTION])
    assert cli.exit_code == 0, cli.output
    out = cli.output.splitlines()
    assert out[out.index(expected[0]) + 1] == expected[1], cli.output
    mcp = mcp_core.store_get(doc_id=_sha(lines[0]), collection=_COLLECTION).splitlines()
    assert mcp[mcp.index(expected[0]) + 1] == expected[1]


@_BOTH
def test_a_document_re_extracted_clean_has_an_empty_map(tmp_path, ladder, streaming) -> None:
    marker = f"tr1rj3-clear-{streaming}"
    lines = _lines(marker, 4)
    ladder.lines, ladder.rungs = lines, list(_RUNGS)
    path = _write_pdf(tmp_path, marker)
    _index(path, marker, streaming=streaming)
    assert _meta(path, marker)["extraction_pages"] == _MAP

    ladder.rungs = ["mineru"] * 4  # the same document, extracted clean this time
    _index(path, marker, streaming=streaming, force=True)

    meta = _meta(path, marker)
    assert meta["extraction_pages"] == {}, "the old map is replaced, not kept"
    assert meta["extraction_method"] == "mineru"
    cli = CliRunner().invoke(main, ["store", "get", _sha(lines[0]), "--collection", _COLLECTION])
    assert "Extractor:  mineru" in cli.output and "Degraded:" not in cli.output


def test_a_resumed_run_records_the_pages_the_buffer_holds(tmp_path, ladder, monkeypatch) -> None:
    """Run 1 buffers pages 1-3 (page 2 on its text layer) and trips the breaker, which keeps the buffer.
    The rerun re-extracts all seven pages, every one clean this time (its own map is empty), but writes
    only pages 4-7: the indexed text of page 2 is run 1's text-layer text, and the catalog says so."""
    marker = "tr1rj3-resume"
    lines = _lines(marker, 7)
    ladder.lines = lines
    ladder.rungs = ["mineru", "pymupdf-text", "mineru"] + ["mineru"] * 4
    ladder.trip_after = 3
    path = _write_pdf(tmp_path, marker)

    with monkeypatch.context() as bounded:
        bound_the_polling(bounded)
        with _traffic(), pytest.raises(MineruSystemicFailure):
            _index(path, marker, streaming="always")

    ladder.trip_after = None
    ladder.rungs = ["mineru"] * 7
    written: list[int] = []
    real_write_page = HttpPipelineDB.write_page

    def _record(self, content_hash, page_index, page_text, metadata=None):
        written.append(page_index)
        return real_write_page(self, content_hash, page_index, page_text, metadata=metadata)

    with monkeypatch.context() as rerun:
        bound_the_polling(rerun)
        rerun.setattr(HttpPipelineDB, "write_page", _record)
        assert _index(path, marker, streaming="always") == 7

    assert sorted(written) == [3, 4, 5, 6], "pages 1-3 came from the kept buffer"
    meta = _meta(path, marker)
    assert meta["extraction_pages"] == {"pymupdf-text": [2]}, "the buffer's page 2 is in the record"
    assert meta["extraction_method"] == "mineru+degraded"
    assert {_chunk_method(x) for x in lines} == {"mineru+degraded"}


def _index_reporting(path, marker, *, streaming, force=False) -> list[dict]:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_pdf

    reports: list[dict] = []
    index_pdf(
        path, f"z0o2p11-{marker}", t3=HttpVectorClient(), collection_name=_COLLECTION,
        streaming=streaming, force=force, on_extraction=reports.append)
    return reports


@_BOTH
def test_on_extraction_reports_an_extraction_and_not_a_staleness_skip(tmp_path, ladder, streaming) -> None:
    marker = f"tr1rj3-report-{streaming}"
    ladder.lines, ladder.rungs = _lines(marker, 4), list(_RUNGS)
    path = _write_pdf(tmp_path, marker)

    assert _index_reporting(path, marker, streaming=streaming) == [
        {"extraction_method": "mineru+degraded", "extraction_pages": _MAP},
    ]
    assert _index_reporting(path, marker, streaming=streaming) == [], "unchanged: nothing was extracted"


def test_a_swallowed_catalog_error_in_the_all_at_once_path_names_the_document(
    tmp_path, ladder, monkeypatch,
) -> None:
    from structlog.testing import capture_logs

    marker = "tr1rj3-swallow"
    lines = _lines(marker, 4)
    ladder.lines, ladder.rungs = lines, list(_RUNGS)
    path = _write_pdf(tmp_path, marker)

    def boom(*_a, **_kw):
        raise RuntimeError("catalog hook exploded")

    monkeypatch.setattr("nexus.pipeline_stages._catalog_pdf_hook", boom)
    with capture_logs() as cap:
        assert _index(path, marker, streaming="never") == 4, "indexing still succeeds"
    (warning,) = [e for e in cap if e.get("event") == "register_in_catalog_raised"]
    assert warning["file_path"] == str(path) and warning["log_level"] == "warning"
    assert "extraction_pages" not in _meta(path, marker)


def test_the_incremental_path_records_the_map_and_reports_the_extraction(tmp_path, ladder) -> None:
    """The third route of ``index_pdf`` (more than ``_INCREMENTAL_THRESHOLD`` chunks, not streamed)
    registers through the same wrapper and reports through the same callback."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD

    marker = "tr1rj3-incremental"
    lines = _lines(marker, _INCREMENTAL_THRESHOLD + 12)
    ladder.lines = lines
    ladder.rungs = ["mineru"] * len(lines)
    ladder.rungs[1] = "mineru-no-formula"
    ladder.rungs[100] = "unextracted"
    path = _write_pdf(tmp_path, marker)

    reports = _index_reporting(path, marker, streaming="never")

    expected = {"mineru-no-formula": [2], "unextracted": [101]}
    assert reports == [{"extraction_method": "mineru+degraded", "extraction_pages": expected}]
    assert _meta(path, marker)["extraction_pages"] == expected


def test_a_streaming_run_that_filled_no_extraction_stats_reports_nothing(tmp_path, ladder, monkeypatch) -> None:
    """``_report_extraction`` fires only for a run whose extraction filled the stats. Here the
    pipeline completes but hands back no extraction record (the stats dict is dropped): the caller
    must hear nothing, never a half-empty report. The guard is live, not dead code."""
    import nexus.pipeline_stages as stages

    name = "tr1rj3-nostats"
    ladder.lines, ladder.rungs = _lines(name, 4), list(_RUNGS)
    path = _write_pdf(tmp_path, name)
    real = stages.pipeline_index_pdf

    def without_stats(*args, **kwargs):
        kwargs["extraction_stats"] = None
        return real(*args, **kwargs)

    monkeypatch.setattr(stages, "pipeline_index_pdf", without_stats)
    assert _index_reporting(path, name, streaming="always") == []


def test_the_repo_indexer_s_record_replaces_the_map_on_a_registered_document(tmp_path, ladder) -> None:
    """``nx index repo`` writes through ``record_pdf_extraction_on_document`` onto the document its
    catalog pass registered. Against the real engine: a degraded record lands, then a clean one
    replaces the map whole (the engine's meta merge never removes a key)."""
    from nexus.pipeline_stages import record_pdf_extraction_on_document

    name = "tr1rj3-repo-record"
    path = _write_pdf(tmp_path, name)
    doc, _created = _register(path, name)

    record_pdf_extraction_on_document(
        str(doc), path, _COLLECTION, {"extraction_method": "mineru+degraded", "extraction_pages": _MAP})
    meta = _meta(path, name)
    assert meta["extraction_pages"] == _MAP and meta["extraction_method"] == "mineru+degraded"

    record_pdf_extraction_on_document(
        str(doc), path, _COLLECTION, {"extraction_method": "mineru", "extraction_pages": {}})
    meta = _meta(path, name)
    assert meta["extraction_pages"] == {} and meta["extraction_method"] == "mineru"
