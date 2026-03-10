# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PDF text normalization and chunking."""
from nexus.pdf_extractor import _normalize_whitespace_edge_cases
from nexus.pdf_chunker import PDFChunker


# ── _normalize_whitespace_edge_cases ────────────────────────────────────────

def test_normalize_tabs_to_spaces():
    """Tabs are normalized to single spaces."""
    assert "hello world" in _normalize_whitespace_edge_cases("hello\tworld")


def test_normalize_preserves_single_spaces():
    """Regular spaces between words are preserved."""
    result = _normalize_whitespace_edge_cases("hello world")
    assert "hello" in result and "world" in result


def test_normalize_unicode_whitespace():
    """Unicode whitespace characters (non-breaking space, etc.) are normalized."""
    # \u00a0 = non-breaking space, \u2003 = em space
    result = _normalize_whitespace_edge_cases("hello\u00a0world")
    assert "hello" in result and "world" in result


def test_normalize_excessive_newlines():
    """More than 2 consecutive newlines are collapsed."""
    result = _normalize_whitespace_edge_cases("para1\n\n\n\n\npara2")
    assert result.count("\n") <= 3  # at most 2 consecutive newlines + surrounding


def test_normalize_preserves_content():
    """Normalization preserves actual text content."""
    text = "The quick brown fox jumps over the lazy dog."
    result = _normalize_whitespace_edge_cases(text)
    assert result.strip() == text


def test_normalize_empty_string():
    """Empty string normalizes to empty string."""
    assert _normalize_whitespace_edge_cases("") == ""


# ── PDFChunker ──────────────────────────────────────────────────────────────

def test_pdf_chunker_splits_text():
    """PDFChunker splits long text into multiple chunks."""
    long_text = "This is a sentence. " * 200  # ~4000 chars
    chunker = PDFChunker(chunk_chars=500)
    chunks = chunker.chunk(long_text, {"source": "test.pdf"})
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.text) <= 600  # allow some overlap margin


def test_pdf_chunker_single_chunk():
    """Short text produces a single chunk."""
    short_text = "A short PDF content."
    chunker = PDFChunker(chunk_chars=500)
    chunks = chunker.chunk(short_text, {"source": "test.pdf"})
    assert len(chunks) == 1
    assert chunks[0].text.strip() == short_text


def test_pdf_chunker_metadata_includes_chunk_info():
    """Each chunk includes chunk position metadata."""
    text = "Content. " * 50
    chunker = PDFChunker(chunk_chars=100)
    chunks = chunker.chunk(text, {})
    assert len(chunks) >= 1
    for c in chunks:
        assert "chunk_index" in c.metadata
        assert "chunk_start_char" in c.metadata


def test_pdf_chunker_chunk_index_sequential():
    """Chunk indices are sequential starting from 0."""
    text = "Sentence number one. " * 100
    chunker = PDFChunker(chunk_chars=200)
    chunks = chunker.chunk(text, {})
    indices = [c.chunk_index for c in chunks]
    assert indices == list(range(len(chunks)))


def test_pdf_chunker_empty_text():
    """Empty text produces no chunks or a single empty chunk."""
    chunker = PDFChunker(chunk_chars=500)
    chunks = chunker.chunk("", {})
    assert isinstance(chunks, list)
    assert len(chunks) <= 1
