"""AC4: PDFChunker — sentence-boundary splitting, overlap, page number lookup."""
from nexus.pdf_chunker import PDFChunker, TextChunk


def test_chunk_short_text_produces_single_chunk():
    """Text under chunk_chars returns exactly one chunk."""
    chunker = PDFChunker(chunk_chars=500)
    chunks = chunker.chunk("Hello world.", {})
    assert len(chunks) == 1
    assert chunks[0].text == "Hello world."
    assert chunks[0].chunk_index == 0


def test_chunk_sentence_boundary_respected():
    """Chunker breaks at '. ' when it falls within the last-20% search window."""
    # ". " at position 36 lands in the [32, 40] search window for chunk_chars=40
    chunker = PDFChunker(chunk_chars=40, overlap_percent=0.1)
    text = "Longer sentence text here more text. Short."
    chunks = chunker.chunk(text, {})
    # First chunk should end at the sentence boundary (period), not mid-word
    assert chunks[0].text.endswith(".")


def test_chunk_returns_textchunk_instances():
    """chunk() returns list of TextChunk dataclass objects."""
    chunker = PDFChunker(chunk_chars=500)
    chunks = chunker.chunk("content", {})
    assert all(isinstance(c, TextChunk) for c in chunks)


# ── Phase 2c: byte cap post-pass ──────────────────────────────────────────────

def test_pdf_chunker_byte_cap_enforced() -> None:
    """Chunks exceeding SAFE_CHUNK_BYTES must be truncated in the post-pass."""
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
    # chunk_chars > SAFE_CHUNK_BYTES forces chunks that exceed the byte cap.
    big_text = "a" * 20_000  # 20 KB ASCII
    chunker = PDFChunker(chunk_chars=15_000)  # 15 KB per chunk > 12 288
    chunks = chunker.chunk(big_text, {})

    assert len(chunks) >= 1
    for c in chunks:
        assert len(c.text.encode()) <= SAFE_CHUNK_BYTES, (
            f"PDF chunk exceeds SAFE_CHUNK_BYTES: {len(c.text.encode())} bytes (limit {SAFE_CHUNK_BYTES})"
        )


# ── nexus-nd3e: default overlap must be 300 chars (20% of 1500) ─────────────


def test_pdf_chunker_default_overlap_is_300_chars():
    """Default PDFChunker overlap must be 300 chars (20% of 1500-char default)."""
    chunker = PDFChunker()
    assert chunker.overlap_chars == 300, (
        f"Expected overlap_chars=300 (0.20 * 1500), got {chunker.overlap_chars}"
    )
