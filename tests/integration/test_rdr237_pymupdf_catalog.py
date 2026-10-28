# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.4): a document indexed with the explicit ``pymupdf`` extractor, through the REAL engine.

Driven through ``doc_indexer.index_pdf`` against the engine substrate ``test_rdr223_pdf_journey.py``
boots. Unlike ``test_rdr237_catalog_extraction.py`` the extractor is REAL (PyMuPDF over a real PDF with
two text pages and an image-only page), with the PDF stack hidden and every install or MinerU touch a
trip wire; only the chunker is the journey's fake (one chunk per page line, so each chunk's text is known).

* both routes of ``index_pdf`` that extract per document (the streaming pipeline and the all-at-once
  path) leave ``extraction_method: pymupdf`` and the page map (``pymupdf-text`` and ``unextracted``) on the
  catalog document, and the chunks carry ``pymupdf``;
* ``nx store get`` and the MCP ``store_get`` print ``Extractor:  pymupdf`` and a ``Degraded:`` line for
  the page with no text layer only (the text-layer pages are the extractor's normal output);
* a ``pymupdf`` rerun over the kept buffer of a MinerU run that tripped its breaker writes only the pages
  the buffer lacked and records the document as the mixture it is, ``mineru+degraded``.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from click.testing import CliRunner

from nexus import pdf_stack
from nexus.cli import main
from nexus.db import http_vector_client as hvc
from nexus.db.http_pipeline_client import HttpPipelineDB
from nexus.mcp import core as mcp_core
from nexus.pdf_extractor import ExtractionResult, MineruSystemicFailure, PDFExtractor
from tests._bounded_polling import bound_the_polling
from tests.integration.test_rdr223_pdf_journey import (
    _COLLECTION,
    _FakeChunker,
    _index,
    _register,
    _sha,
    _traffic,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture()
def hidden_stack(monkeypatch):
    """The PDF stack absent, the real chunker faked, and every install or MinerU touch a failure."""
    pdf_stack.reset_cache()
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("absent", "gone"))
    calls: list[str] = []

    def forbid(name):
        def _raise(*_a, **_k):
            calls.append(name)
            raise AssertionError(f"{name} must not be called for the pymupdf extractor")
        return _raise

    monkeypatch.setattr(pdf_stack, "ensure", forbid("ensure"))
    monkeypatch.setattr(pdf_stack, "_install_generation", forbid("_install_generation"))
    monkeypatch.setattr(PDFExtractor, "_mineru_server_available", forbid("_mineru_server_available"))
    monkeypatch.setattr(PDFExtractor, "_extract_with_mineru", forbid("_extract_with_mineru"))
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    return calls


def _lines(marker: str, n: int) -> list[str]:
    return [f"{marker} page {i + 1} of the harbour survey, text layer sentence for the pymupdf extractor." for i in range(n)]


def _write_real_pdf(tmp_path: Path, marker: str, pages: list[str | None]) -> Path:
    path = (tmp_path / f"doc-{marker}.pdf").resolve()
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text is None:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
            pix.clear_with(120)
            page.insert_image(pymupdf.Rect(50, 50, 250, 250), pixmap=pix)
        else:
            page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def _index_pymupdf(path: Path, marker: str, *, streaming: str, force: bool = False):
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_pdf

    return index_pdf(
        path, f"z0o2p11-{marker}", t3=HttpVectorClient(), collection_name=_COLLECTION,
        streaming=streaming, force=force, extractor="pymupdf",
    )


def _meta(path: Path, marker: str) -> dict:
    from nexus.catalog.factory import make_catalog_reader

    doc, _created = _register(path, marker)
    entry = make_catalog_reader().resolve(doc)
    assert entry is not None
    return dict(entry.meta)


def _chunk_method(line: str) -> str:
    entry = hvc.HttpVectorClient().get_by_id(_COLLECTION, _sha(line))
    assert entry is not None
    return entry.get("extraction_method", "")


_BOTH = pytest.mark.parametrize("streaming", ["always", "never"], ids=["streaming", "all-at-once"])


@_BOTH
def test_a_pymupdf_document_records_its_method_and_page_map_on_the_catalog_and_its_chunks(
    tmp_path, hidden_stack, streaming,
) -> None:
    marker = f"tr1rj4-record-{streaming}"
    l1, l3 = _lines(marker, 3)[0], _lines(marker, 3)[2]
    path = _write_real_pdf(tmp_path, marker, [l1, None, l3])

    assert _index_pymupdf(path, marker, streaming=streaming) == 2, "one chunk per text page"

    meta = _meta(path, marker)
    assert meta["extraction_method"] == "pymupdf"
    assert meta["extraction_pages"] == {"pymupdf-text": [1, 3], "unextracted": [2]}
    assert {_chunk_method(x) for x in (l1, l3)} == {"pymupdf"}
    assert hidden_stack == [], "no install attempted, MinerU never probed or started"


@_BOTH
def test_store_get_calls_only_the_page_with_no_text_layer_degraded(tmp_path, hidden_stack, streaming) -> None:
    marker = f"tr1rj4-print-{streaming}"
    l1, l3 = _lines(marker, 3)[0], _lines(marker, 3)[2]
    path = _write_real_pdf(tmp_path, marker, [l1, None, l3])
    _index_pymupdf(path, marker, streaming=streaming)

    expected = ["Extractor:  pymupdf", "Degraded:   unextracted 2"]
    cli = CliRunner().invoke(main, ["store", "get", _sha(l1), "--collection", _COLLECTION])
    assert cli.exit_code == 0, cli.output
    out = cli.output.splitlines()
    assert out[out.index(expected[0]) + 1] == expected[1], cli.output
    mcp = mcp_core.store_get(doc_id=_sha(l1), collection=_COLLECTION).splitlines()
    assert mcp[mcp.index(expected[0]) + 1] == expected[1]


def test_a_pymupdf_rerun_over_a_tripped_mineru_run_records_the_mixture(tmp_path, hidden_stack, monkeypatch) -> None:
    """Run 1 is a MinerU ladder that buffers pages 1 and 2 and trips its breaker, which keeps the buffer.
    The rerun is ``--extractor pymupdf``: it writes only pages 3 to 5,
    and the catalog says the document is MinerU's pages 1-2 plus the text layer for the rest."""
    marker = "tr1rj4-resume"
    texts = _lines(marker, 5)
    path = _write_real_pdf(tmp_path, marker, [texts[0], texts[1], texts[2], None, texts[4]])

    class _TrippingMineru:
        def extract(self, pdf_path, *, extractor="mineru", on_page=None,
                    allow_degraded=False, on_heartbeat=None):
            for i in (0, 1):
                on_page(i, texts[i], {"page_number": i + 1, "text_length": len(texts[i]), "page_method": "mineru"})
            raise MineruSystemicFailure("exit:1:boom", "boom", [3, 4, 5], "doc.pdf")

    with monkeypatch.context() as run1:
        # Run 1 is the MinerU run: the host it ran on had the stack.
        run1.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("present", ""))
        pdf_stack.reset_cache()
        run1.setattr("nexus.pipeline_stages.PDFExtractor", _TrippingMineru)
        run1.setattr("nexus.doc_indexer.PDFExtractor", _TrippingMineru)
        bound_the_polling(run1)
        with _traffic(), pytest.raises(MineruSystemicFailure):
            _index(path, marker, streaming="always")
    pdf_stack.reset_cache()  # run 1 cached "present"; the rerun's host has no stack

    written: list[int] = []
    real_write_page = HttpPipelineDB.write_page

    def _record(self, content_hash, page_index, page_text, metadata=None):
        written.append(page_index)
        return real_write_page(self, content_hash, page_index, page_text, metadata=metadata)

    with monkeypatch.context() as rerun:
        bound_the_polling(rerun)
        rerun.setattr(HttpPipelineDB, "write_page", _record)
        assert _index_pymupdf(path, marker, streaming="always") == 4

    assert sorted(written) == [2, 3, 4], "pages 1 and 2 came from the kept buffer"
    meta = _meta(path, marker)
    assert meta["extraction_method"] == "mineru+degraded"
    assert meta["extraction_pages"] == {"pymupdf-text": [3, 5], "unextracted": [4]}
    assert {_chunk_method(x) for x in (texts[0], texts[1], texts[2], texts[4])} == {"mineru+degraded"}
    assert hidden_stack == []
