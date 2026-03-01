# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for PDFChunker using real extracted PDF text.

Uses PDFChunker(chunk_chars=100) explicitly — the default 1500 would yield
a single chunk from small fixture files, making multi-chunk assertions vacuous.

AC-U9 through AC-U11 from RDR-011.
"""
from pathlib import Path

import pytest

from nexus.doc_indexer import _pdf_chunks, _sha256
from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import PDFExtractor


@pytest.fixture(scope="module")
def multipage_result(multipage_pdf: Path):
    """Extract multipage.pdf once for all chunker tests in this module."""
    return PDFExtractor().extract(multipage_pdf)


class TestMultipleChunks:
    """AC-U9: chunk_chars=100 produces > 1 chunk from multipage.pdf."""

    def test_produces_multiple_chunks(self, multipage_result) -> None:
        chunks = PDFChunker(chunk_chars=100).chunk(
            multipage_result.text, multipage_result.metadata
        )
        assert len(chunks) > 1

    def test_all_page_numbers_are_positive(self, multipage_result) -> None:
        chunks = PDFChunker(chunk_chars=100).chunk(
            multipage_result.text, multipage_result.metadata
        )
        for chunk in chunks:
            assert chunk.metadata["page_number"] >= 1, (
                f"chunk {chunk.chunk_index} has page_number "
                f"{chunk.metadata['page_number']}"
            )

    def test_chunk_ranges_cover_full_text(self, multipage_result) -> None:
        """Union of [chunk_start_char, chunk_end_char) spans entire text."""
        text = multipage_result.text
        chunks = PDFChunker(chunk_chars=100).chunk(text, multipage_result.metadata)
        sorted_chunks = sorted(chunks, key=lambda c: c.metadata["chunk_start_char"])

        assert sorted_chunks[0].metadata["chunk_start_char"] == 0
        # With overlap each chunk starts before the previous one ends — no gaps.
        for prev, curr in zip(sorted_chunks, sorted_chunks[1:]):
            assert curr.metadata["chunk_start_char"] <= prev.metadata["chunk_end_char"]
        # PDFChunker always sets end = min(start + chunk_chars, len(text)) for the
        # final iteration, and sentence-boundary search only runs when end < len(text),
        # so the last chunk always ends exactly at len(text).
        assert sorted_chunks[-1].metadata["chunk_end_char"] == len(text)


class TestOverlap:
    """AC-U10: overlap_percent=0.1 produces overlapping raw-position windows."""

    def test_adjacent_chunks_overlap_in_raw_positions(self, multipage_result) -> None:
        chunks = PDFChunker(chunk_chars=100, overlap_percent=0.1).chunk(
            multipage_result.text, multipage_result.metadata
        )
        assert len(chunks) > 1, "Need ≥ 2 chunks to verify overlap"
        for prev, curr in zip(chunks, chunks[1:]):
            # next chunk starts before current chunk ends → raw-position overlap
            assert curr.metadata["chunk_start_char"] < prev.metadata["chunk_end_char"], (
                f"No overlap: chunk {prev.chunk_index} ends at "
                f"{prev.metadata['chunk_end_char']}, "
                f"chunk {curr.chunk_index} starts at "
                f"{curr.metadata['chunk_start_char']}"
            )


class TestIsImagePdf:
    """is_image_pdf metadata field correctness (per-page heuristic)."""

    def test_real_text_pdf_is_not_image(self, simple_pdf: Path) -> None:
        """TrueType PDF with real extractable text → is_image_pdf False."""
        content_hash = _sha256(simple_pdf)
        chunks = _pdf_chunks(simple_pdf, content_hash, "test-model", "2026-01-01", "test")
        assert chunks, "Expected at least one chunk from simple.pdf"
        assert all(chunk[2]["is_image_pdf"] is False for chunk in chunks), (
            "simple.pdf should not be flagged as image-only"
        )

    def test_multipage_real_text_is_not_image(self, multipage_pdf: Path) -> None:
        """3-page TrueType PDF → is_image_pdf False on all chunks."""
        content_hash = _sha256(multipage_pdf)
        chunks = _pdf_chunks(multipage_pdf, content_hash, "test-model", "2026-01-01", "test")
        assert chunks
        assert all(chunk[2]["is_image_pdf"] is False for chunk in chunks)

    def test_type3_pdf_is_image_when_no_text(self, type3_pdf: Path) -> None:
        """Type3 PDF whose glyph renders as empty text → is_image_pdf True."""
        from nexus.pdf_extractor import PDFExtractor
        result = PDFExtractor().extract(type3_pdf)
        page_count = result.metadata.get("page_count", 1) or 1
        chars_per_page = len(result.text) / page_count
        # Only assert is_image_pdf when text extraction actually returns empty;
        # pymupdf may or may not decode the single Type3 glyph depending on version.
        if chars_per_page < 20:
            content_hash = _sha256(type3_pdf)
            chunks = _pdf_chunks(type3_pdf, content_hash, "test-model", "2026-01-01", "test")
            if chunks:
                assert all(chunk[2]["is_image_pdf"] is True for chunk in chunks)


class TestCharRangeMetadata:
    """AC-U11: Every chunk carries chunk_start_char / chunk_end_char with end > start."""

    def test_char_range_keys_present(self, multipage_result) -> None:
        chunks = PDFChunker(chunk_chars=100).chunk(
            multipage_result.text, multipage_result.metadata
        )
        for chunk in chunks:
            assert "chunk_start_char" in chunk.metadata
            assert "chunk_end_char" in chunk.metadata

    def test_end_greater_than_start(self, multipage_result) -> None:
        chunks = PDFChunker(chunk_chars=100).chunk(
            multipage_result.text, multipage_result.metadata
        )
        for chunk in chunks:
            assert chunk.metadata["chunk_end_char"] > chunk.metadata["chunk_start_char"], (
                f"chunk {chunk.chunk_index}: "
                f"end={chunk.metadata['chunk_end_char']} "
                f"<= start={chunk.metadata['chunk_start_char']}"
            )
