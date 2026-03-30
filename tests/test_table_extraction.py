# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for structured table extraction (nexus-pjz7).

Tests cover:
- table_regions metadata from Docling extraction
- chunk_type tagging in PDFChunker (page-level granularity)
- PyMuPDF fallback produces no table_regions
"""
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import ExtractionResult, PDFExtractor


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_page_boundaries(*pages: tuple[int, int, int]) -> list[dict]:
    """Build page_boundaries list from (page_number, start_char, text_length) tuples."""
    return [
        {"page_number": p, "start_char": s, "page_text_length": l}
        for p, s, l in pages
    ]


def _make_table_item(page_no: int, html: str = "<table><tr><td>A</td></tr></table>") -> MagicMock:
    """Return a mock Docling TableItem on the given page."""
    item = MagicMock()
    item.__class__.__name__ = "TableItem"
    prov = MagicMock()
    prov.page_no = page_no
    item.prov = [prov]
    item.export_to_html.return_value = html
    return item


def _make_text_item(page_no: int, text: str = "Some text.") -> MagicMock:
    """Return a mock Docling text item (non-table)."""
    item = MagicMock()
    item.__class__.__name__ = "TextItem"
    prov = MagicMock()
    prov.page_no = page_no
    item.prov = [prov]
    item.text = text
    return item


# ── ExtractionResult: table_regions from Docling ─────────────────────────────

def test_extract_with_docling_includes_table_regions(tmp_path):
    """Docling extraction produces table_regions when TableItems are present."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"fake")

    table_item = _make_table_item(page_no=2)
    text_item = _make_text_item(page_no=1)

    mock_doc = MagicMock()
    mock_doc.num_pages.return_value = 2
    mock_doc.export_to_markdown.side_effect = lambda page_no: (
        "Page one text." if page_no == 1 else "Page two text."
    )
    # iterate_items yields (item, level) tuples
    mock_doc.iterate_items.return_value = [
        (text_item, 0),
        (table_item, 0),
    ]

    mock_result = MagicMock()
    mock_result.document = mock_doc

    extractor = PDFExtractor()
    with patch.object(extractor, "_get_converter") as mock_conv_getter:
        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_conv_getter.return_value = mock_converter

        result = extractor.extract(pdf_path)

    assert "table_regions" in result.metadata
    regions = result.metadata["table_regions"]
    assert len(regions) == 1
    assert regions[0]["page"] == 2


def test_table_regions_contain_required_fields(tmp_path):
    """Each table_region entry must have 'page' and 'html' keys."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"fake")

    html_str = "<table><tr><td>Data</td></tr></table>"
    table_item = _make_table_item(page_no=3, html=html_str)
    text_item = _make_text_item(page_no=1)

    mock_doc = MagicMock()
    mock_doc.num_pages.return_value = 3
    mock_doc.export_to_markdown.return_value = "Some text on the page."
    mock_doc.iterate_items.return_value = [
        (text_item, 0),
        (table_item, 0),
    ]

    mock_result = MagicMock()
    mock_result.document = mock_doc

    extractor = PDFExtractor()
    with patch.object(extractor, "_get_converter") as mock_conv_getter:
        mock_converter = MagicMock()
        mock_converter.convert.return_value = mock_result
        mock_conv_getter.return_value = mock_converter

        result = extractor.extract(pdf_path)

    region = result.metadata["table_regions"][0]
    assert "page" in region
    assert "html" in region
    assert region["page"] == 3
    assert region["html"] == html_str


# ── PDFChunker: chunk_type tagging ───────────────────────────────────────────

def test_chunker_tags_table_chunks():
    """Chunks on a table page get chunk_type='table_page'."""
    # Page 2 is a table page; build text that fills pages 1 and 2
    page1_text = "A" * 500
    page2_text = "B" * 500
    text = page1_text + "\n" + page2_text

    extraction_metadata = {
        "page_boundaries": _make_page_boundaries(
            (1, 0, len(page1_text) + 1),
            (2, len(page1_text) + 1, len(page2_text) + 1),
        ),
        "table_regions": [{"page": 2, "html": "<table/>"}],
    }

    chunker = PDFChunker(chunk_chars=600)
    chunks = chunker.chunk(text, extraction_metadata)

    # Some chunks should be on page 2 → tagged as table
    table_chunks = [c for c in chunks if c.metadata.get("chunk_type") == "table_page"]
    assert len(table_chunks) > 0, "Expected at least one chunk tagged as table_page"
    for c in table_chunks:
        assert c.metadata["page_number"] == 2


def test_chunker_tags_text_chunks():
    """Chunks NOT on a table page get chunk_type='text'."""
    page1_text = "A" * 500
    page2_text = "B" * 500
    text = page1_text + "\n" + page2_text

    extraction_metadata = {
        "page_boundaries": _make_page_boundaries(
            (1, 0, len(page1_text) + 1),
            (2, len(page1_text) + 1, len(page2_text) + 1),
        ),
        "table_regions": [{"page": 2, "html": "<table/>"}],
    }

    chunker = PDFChunker(chunk_chars=600)
    chunks = chunker.chunk(text, extraction_metadata)

    # Page 1 chunks should be text
    page1_chunks = [c for c in chunks if c.metadata.get("page_number") == 1]
    assert len(page1_chunks) > 0, "Expected at least one chunk on page 1"
    for c in page1_chunks:
        assert c.metadata.get("chunk_type") == "text", (
            f"Page 1 chunk should be 'text', got {c.metadata.get('chunk_type')!r}"
        )


def test_mixed_document():
    """Page 1 → text, page 2 → table, page 3 → text."""
    p1 = "First page content. " * 30     # ~600 chars
    p2 = "Second page content. " * 30    # ~630 chars
    p3 = "Third page content. " * 30     # ~600 chars

    # Build text with \n separators, matching page_boundaries convention
    text = p1 + "\n" + p2 + "\n" + p3
    s1 = 0
    l1 = len(p1) + 1
    s2 = s1 + l1
    l2 = len(p2) + 1
    s3 = s2 + l2
    l3 = len(p3) + 1

    extraction_metadata = {
        "page_boundaries": [
            {"page_number": 1, "start_char": s1, "page_text_length": l1},
            {"page_number": 2, "start_char": s2, "page_text_length": l2},
            {"page_number": 3, "start_char": s3, "page_text_length": l3},
        ],
        "table_regions": [{"page": 2, "html": "<table/>"}],
    }

    chunker = PDFChunker(chunk_chars=400, overlap_percent=0.0)
    chunks = chunker.chunk(text, extraction_metadata)

    # Every chunk must have chunk_type set
    for c in chunks:
        assert "chunk_type" in c.metadata, f"Missing chunk_type on chunk {c.chunk_index}"

    page_types: dict[int, set[str]] = {}
    for c in chunks:
        pg = c.metadata["page_number"]
        ct = c.metadata["chunk_type"]
        page_types.setdefault(pg, set()).add(ct)

    # Pages 1 and 3 must only have text chunks; page 2 must only have table_page chunks
    assert page_types.get(1) == {"text"}, f"Page 1: expected {{'text'}}, got {page_types.get(1)}"
    assert page_types.get(2) == {"table_page"}, f"Page 2: expected {{'table_page'}}, got {page_types.get(2)}"
    assert page_types.get(3) == {"text"}, f"Page 3: expected {{'text'}}, got {page_types.get(3)}"


def test_no_tables_all_text():
    """Document with no table_regions: all chunks get chunk_type='text'."""
    text = "No tables here. " * 100

    extraction_metadata = {
        "page_boundaries": [
            {"page_number": 1, "start_char": 0, "page_text_length": len(text) + 1},
        ],
        "table_regions": [],
    }

    chunker = PDFChunker(chunk_chars=400)
    chunks = chunker.chunk(text, extraction_metadata)

    assert chunks, "Expected at least one chunk"
    for c in chunks:
        assert c.metadata.get("chunk_type") == "text"


def test_pymupdf_fallback_all_text(tmp_path):
    """PyMuPDF fallback path produces no table_regions; all chunks tagged as text."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"fake pdf")

    # Mock pymupdf so we don't need a real PDF
    page_text = "Fallback text content. " * 50

    mock_page = MagicMock()
    mock_page.get_text.return_value = page_text

    mock_doc_ctx = MagicMock()
    mock_doc_ctx.__enter__ = MagicMock(return_value=mock_doc_ctx)
    mock_doc_ctx.__exit__ = MagicMock(return_value=False)
    mock_doc_ctx.__len__ = MagicMock(return_value=1)
    mock_doc_ctx.__iter__ = MagicMock(return_value=iter([mock_page]))
    mock_doc_ctx.metadata = {}

    extractor = PDFExtractor()
    with patch("pymupdf.open", return_value=mock_doc_ctx):
        result = extractor._extract_normalized(pdf_path)

    assert result.metadata["extraction_method"] == "pymupdf_normalized"
    assert "table_regions" not in result.metadata

    # When fed to the chunker with no table_regions, all chunks are text
    chunker = PDFChunker(chunk_chars=400)
    chunks = chunker.chunk(result.text, result.metadata)
    for c in chunks:
        assert c.metadata.get("chunk_type") == "text"
