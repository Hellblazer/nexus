# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-i0cwh: ``PDFExtractor.extract`` reports ``pages_with_text``.

The DEVONthink route's page-coverage check needs every page the extractor
produced text for. Chunk ``page_number`` values only mark where chunks
start (a 20-page slide deck chunked into 7 chunks reads as 13 missing
pages), so the extractor itself records the set from the per-page
callback every backend already fires, and index_pdf surfaces it in its
``return_metadata`` dict.
"""
from __future__ import annotations

from pathlib import Path

from nexus.pdf_extractor import ExtractionResult, PDFExtractor


def _fake_dispatch(pages: list[tuple[int, str]]):
    def dispatch(self, pdf_path, *, extractor, on_formula_oom, on_page):
        for number, text in pages:
            if on_page is not None:
                on_page(number - 1, text, {"page_number": number, "text_length": len(text)})
        return ExtractionResult(text="\n".join(t for _, t in pages), metadata={"page_count": len(pages)})
    return dispatch


def test_pages_with_text_excludes_empty_pages(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(PDFExtractor, "_extract_dispatch", _fake_dispatch([(1, "hello " * 20), (2, ""), (3, "world " * 20)]))
    monkeypatch.setattr("nexus.pdf_extractor._enforce_extraction_quality", lambda *a, **k: None)
    result = PDFExtractor().extract(pdf, extractor="docling")
    assert result.metadata["pages_with_text"] == [1, 3]
    assert result.metadata["page_count"] == 3


def test_caller_on_page_still_fires(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(PDFExtractor, "_extract_dispatch", _fake_dispatch([(1, "a" * 30), (2, "b" * 30)]))
    monkeypatch.setattr("nexus.pdf_extractor._enforce_extraction_quality", lambda *a, **k: None)
    seen: list[int] = []
    PDFExtractor().extract(pdf, extractor="docling", on_page=lambda i, t, m: seen.append(m["page_number"]))
    assert seen == [1, 2]
