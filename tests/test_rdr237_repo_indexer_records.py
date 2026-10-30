# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.3, fix round 1): ``nx index repo`` records a PDF's extraction on its catalog document.

The repo indexer registers every file in its own catalog pass before it chunks anything, so
``_index_pdf_file`` has the document's tumbler (``doc_id_resolver``) and no row to find. After the
extraction it writes ``extraction_method`` / ``extraction_pages`` onto that document through
``record_pdf_extraction_on_document`` (``extraction_pages`` always, ``{}`` when clean, so a clean
re-extract clears an old map). A run that cannot record it logs ``catalog_pdf_extraction_not_recorded``
naming the file. The engine-backed behaviour of the same ``meta`` merge is pinned by
``tests/integration/test_rdr237_catalog_extraction.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from nexus.indexer import _index_pdf_file
from nexus.pipeline_stages import record_pdf_extraction_on_document

DEGRADED = {"mineru-no-formula": [3], "pymupdf-text": [12]}


class _Writer:
    """Records ``update`` calls; ``fail`` makes them raise."""

    def __init__(self, fail: bool = False) -> None:
        self.updates: list[tuple[str, dict]] = []
        self.closed = False
        self._fail = fail

    def update(self, tumbler, **fields) -> None:
        if self._fail:
            raise RuntimeError("engine said no")
        self.updates.append((str(tumbler), fields))

    def close(self) -> None:
        self.closed = True


def _chunks_filling(method: str, pages: dict):
    """A ``_pdf_chunks`` that fills ``extraction_stats`` the way the real one does."""

    def fake(file, content_hash, model, now_iso, corpus, *, extraction_stats=None, **kw):
        if extraction_stats is not None:
            extraction_stats.update({
                "extraction_method": method, "extraction_pages": pages, "page_count": 12,
                "quality_gate_overridden": False, "extraction_source": "file",
            })
        return [("id1", "Page 1", {"source_title": "T", "page_number": 1, "source_path": str(file),
                                   "corpus": corpus, "embedding_model": model, "store_type": "prose",
                                   "source_agent": "nexus-indexer"})]

    return fake


def _index(tmp_path: Path, method: str, pages: dict, *, doc_id: str = "1.1.7", writer=None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    pdf = repo / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    col = MagicMock()
    col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock()
    db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    writer = writer if writer is not None else _Writer()
    with patch("nexus.doc_indexer._pdf_chunks", side_effect=_chunks_filling(method, pages)), \
            patch("nexus.doc_indexer._fence_begin"), \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
            patch("nexus.catalog.factory.make_catalog_writer", return_value=writer):
        count = _index_pdf_file(
            pdf, repo, "docs__repo", "voyage-context-3", col, db, "fake-key", git_meta={},
            now_iso="2026-01-01T00:00:00", score=1.0, embed_fn=lambda texts: [[0.1] * 3 for _ in texts],
            doc_id_resolver=lambda p: doc_id, hooks=MagicMock(),
        )
    return count, writer, pdf


def test_a_degraded_pdf_indexed_by_the_repo_indexer_records_its_map(tmp_path) -> None:
    count, writer, _pdf = _index(tmp_path, "mineru+degraded", DEGRADED)
    assert count == 1
    assert writer.updates == [
        ("1.1.7", {"meta": {"extraction_method": "mineru+degraded", "extraction_pages": DEGRADED,
                           "quality_gate_overridden": False, "extraction_source": "file"}}),
    ]
    assert writer.closed


def test_a_clean_re_extract_through_the_repo_indexer_writes_an_empty_map(tmp_path) -> None:
    """The service merges ``meta`` key by key and never removes one, so a clean run clears an old
    map only by writing ``extraction_pages`` whole, ``{}`` included."""
    _count, writer, _pdf = _index(tmp_path, "mineru", {})
    assert writer.updates == [("1.1.7", {"meta": {
        "extraction_method": "mineru", "extraction_pages": {},
        "quality_gate_overridden": False, "extraction_source": "file"}})]


def test_a_file_with_no_catalog_document_logs_that_nothing_was_recorded(tmp_path) -> None:
    with capture_logs() as cap:
        count, writer, pdf = _index(tmp_path, "mineru+degraded", DEGRADED, doc_id="")
    assert writer.updates == []
    (warning,) = [e for e in cap if e.get("event") == "catalog_pdf_extraction_not_recorded"]
    assert warning["reason"] == "repo_indexer_no_document"
    assert warning["file_path"] == str(pdf) and warning["log_level"] == "warning"
    assert warning["extraction_pages"] == DEGRADED and warning["collection"] == "docs__repo"


def test_a_failed_write_is_logged_and_does_not_stop_the_file(tmp_path) -> None:
    writer = _Writer(fail=True)
    with capture_logs() as cap:
        count, _w, pdf = _index(tmp_path, "mineru+degraded", DEGRADED, writer=writer)
    assert count == 1, "the PDF is indexed either way"
    (warning,) = [e for e in cap if e.get("event") == "catalog_pdf_extraction_not_recorded"]
    assert warning["reason"] == "repo_indexer_update_failed" and "engine said no" in warning["error"]
    assert warning["file_path"] == str(pdf) and warning["doc_id"] == "1.1.7"
    assert writer.closed, "the writer is closed after a failed write"


def test_no_catalog_writer_is_named(tmp_path) -> None:
    pdf = tmp_path / "paper.pdf"
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=None), capture_logs() as cap:
        record_pdf_extraction_on_document("1.1.7", pdf, "docs__repo", {"extraction_method": "mineru", "extraction_pages": {}})
    (warning,) = [e for e in cap if e.get("event") == "catalog_pdf_extraction_not_recorded"]
    assert warning["reason"] == "no_catalog_writer" and warning["file_path"] == str(pdf)


@pytest.mark.parametrize("extraction", [{"extraction_method": "", "extraction_pages": {}}, {}])
def test_an_unnamed_method_is_not_written_but_the_map_is(extraction) -> None:
    """An unnamed method must not overwrite a recorded one; the map is always written."""
    writer = _Writer()
    with patch("nexus.catalog.factory.make_catalog_writer", return_value=writer):
        record_pdf_extraction_on_document("1.1.7", Path("/x/p.pdf"), "docs__repo", extraction)
    assert writer.updates == [("1.1.7", {"meta": {"extraction_pages": {}}})]


def test_the_record_lands_before_the_chunks_are_embedded_and_uploaded(tmp_path) -> None:
    """Recorded after extraction and before embed and upload: a failed upload leaves the file
    unindexed (the next run re-extracts and re-records); a record after a landed upload could fail
    and never be retried, since staleness would then call the file current."""
    events: list[str] = []

    class _OrderedWriter(_Writer):
        def update(self, tumbler, **fields) -> None:
            events.append("record")
            super().update(tumbler, **fields)

    repo = tmp_path / "repo"
    repo.mkdir()
    pdf = repo / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    col = MagicMock()
    col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock()
    db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col

    def embed(texts):
        events.append("embed")
        return [[0.1] * 3 for _ in texts]

    with patch("nexus.doc_indexer._pdf_chunks", side_effect=_chunks_filling("mineru+degraded", DEGRADED)), \
            patch("nexus.doc_indexer._fence_begin"), \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
            patch("nexus.catalog.factory.make_catalog_writer", return_value=_OrderedWriter()):
        _index_pdf_file(
            pdf, repo, "docs__repo", "bge-base-en-v1.5", col, db, "fake-key", git_meta={},
            now_iso="2026-01-01T00:00:00", score=1.0, embed_fn=embed,
            doc_id_resolver=lambda p: "1.1.7", hooks=MagicMock(),
        )
    assert "embed" in events, "non-vacuity: the chunks were embedded"
    assert events.index("record") < events.index("embed")
