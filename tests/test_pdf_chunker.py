"""AC4: PDFChunker — sentence-boundary splitting, overlap, page number lookup."""
from nexus.pdf_chunker import PDFChunker, TextChunk


def test_chunk_short_text_produces_single_chunk():
    """Text under chunk_chars returns exactly one chunk."""
    chunker = PDFChunker(chunk_chars=500)
    chunks = chunker.chunk("Hello world.", {})
    assert len(chunks) == 1
    assert chunks[0].text == "Hello world."
    assert chunks[0].chunk_index == 0


def test_chunk_long_text_produces_multiple_chunks():
    """Text longer than chunk_chars splits into multiple chunks."""
    chunker = PDFChunker(chunk_chars=20, overlap_percent=0.1)
    text = "A" * 200
    chunks = chunker.chunk(text, {})
    assert len(chunks) > 1
    for i, c in enumerate(chunks):
        assert c.chunk_index == i


def test_chunk_sentence_boundary_respected():
    """Chunker breaks at '. ' when it falls within the last-20% search window."""
    # ". " at position 36 lands in the [32, 40] search window for chunk_chars=40
    chunker = PDFChunker(chunk_chars=40, overlap_percent=0.1)
    text = "Longer sentence text here more text. Short."
    chunks = chunker.chunk(text, {})
    # First chunk should end at the sentence boundary (period), not mid-word
    assert chunks[0].text.endswith(".")


def test_chunk_page_number_from_boundaries():
    """page_number in chunk metadata is derived from page_boundaries."""
    chunker = PDFChunker(chunk_chars=500)
    extraction_meta = {
        "page_boundaries": [
            {"page_number": 2, "start_char": 0, "page_text_length": 200}
        ]
    }
    chunks = chunker.chunk("Hello world.", extraction_meta)
    assert chunks[0].metadata["page_number"] == 2


def test_chunk_page_number_zero_without_boundaries():
    """Without page_boundaries metadata, page_number defaults to 0."""
    chunker = PDFChunker(chunk_chars=500)
    chunks = chunker.chunk("Hello world.", {})
    assert chunks[0].metadata["page_number"] == 0


def test_chunk_returns_textchunk_instances():
    """chunk() returns list of TextChunk dataclass objects."""
    chunker = PDFChunker(chunk_chars=500)
    chunks = chunker.chunk("content", {})
    assert all(isinstance(c, TextChunk) for c in chunks)
