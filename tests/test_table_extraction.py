# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for structured table extraction (nexus-pjz7).

Tests cover:
- table_regions metadata from MinerU extraction
- chunk_type tagging in PDFChunker (page-level granularity)
- PyMuPDF fallback produces no table_regions
"""
from pathlib import Path
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


# ── ExtractionResult: table_regions from MinerU ──────────────────────────────

def _build(content_list):
    return PDFExtractor._mineru_build_result(
        Path("/fake/test.pdf"), "Page one text.\nPage two text.", content_list, [],
        per_page_lengths=[(0, 14), (1, 14)], total_pages=2,
    )


def test_mineru_result_includes_table_regions_for_tables_rendered_as_text():
    """A table MinerU rendered as text (it has a ``table_body``) is a table region on its page."""
    result = _build([
        {"type": "text", "page_idx": 0},
        {"type": "table", "table_body": "<table><tr><td>A</td></tr></table>", "page_idx": 0, "doc_page_idx": 1},
    ])
    assert result.metadata["table_regions"] == [{"page": 2}]


def test_a_table_mineru_could_not_render_is_not_a_table_region():
    result = _build([{"type": "table", "img_path": "images/x.jpg", "page_idx": 0, "doc_page_idx": 1}])
    assert result.metadata["table_regions"] == []


def test_table_regions_contain_only_page():
    """nexus-dqe86: each table_region entry carries only 'page'.

    An 'html' key used to be written here too but was never read anywhere:
    pdf_chunker.py and pipeline_stages.py, the only two consumers, read only
    ['page']. The dead field was deleted rather than preserved (dead writes get
    deleted unless an accepted RDR plans a reader; a table HTML validator,
    nexus-stkek, reads table HTML out of the extracted TEXT, not out of this
    field). This test pins the field's ABSENCE so it does not silently come back.
    """
    result = _build([{"type": "table", "table_body": "<table/>", "page_idx": 0, "doc_page_idx": 2}])
    assert result.metadata["table_regions"][0] == {"page": 3}


# ── PDFChunker: chunk_type tagging ───────────────────────────────────────────

def test_chunker_tags_table_chunks():
    """Chunks on a table page get chunk_type='table_page'.

    page1_text is 300 chars (not 500, nexus-yrc7q #9 follow-up): with
    chunk_chars=600 the default 20% overlap makes the continuation chunk
    start at char 480; a 500-char page1 (boundary at 501) put that start
    BEFORE the page-2 boundary, so no non-duplicate chunk ever legitimately
    landed on page 2 -- this test's own table_chunks assertion was passing
    only because the now-fixed duplicate-tail-chunk bug (#9) emitted an
    EXTRA chunk starting at char 881 (past the page-2 boundary) purely as
    an artifact of the bug, not because this fixture's boundaries were
    designed to produce one. 300 chars (boundary at 301) puts the real
    continuation chunk's start (480) inside page 2, independent of #9.
    """
    # Page 2 is a table page; build text that fills pages 1 and 2
    page1_text = "A" * 300
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
    """The text-only PyMuPDF extractor produces no table_regions; all chunks tagged as text."""
    import pymupdf

    pdf_path = tmp_path / "test.pdf"
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page().insert_text((72, 72), "Fallback text content. " * 8)
    doc.save(str(pdf_path))
    doc.close()

    result = PDFExtractor()._extract_pymupdf(pdf_path)

    assert result.metadata["extraction_method"] == "pymupdf"
    assert result.metadata["table_regions"] == []

    # When fed to the chunker with no table_regions, all chunks are text
    chunker = PDFChunker(chunk_chars=400)
    chunks = chunker.chunk(result.text, result.metadata)
    assert chunks
    for c in chunks:
        assert c.metadata.get("chunk_type") == "text"
