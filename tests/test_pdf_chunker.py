"""AC4: PDFChunker — sentence-boundary splitting, overlap, page number lookup,
and section-type tagging from heading detection."""
from nexus.pdf_chunker import PDFChunker, TextChunk, _extract_headings


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


# ── Section-type tagging (RDR-089 follow-up — close metadata gap) ────────────


def test_extract_headings_detects_markdown_headings():
    """Markdown ``# Heading`` lines from MinerU/Docling are detected."""
    text = "# Abstract\n\nThis paper does X.\n\n## 1 Introduction\n\nBackground."
    headings = _extract_headings(text)
    titles = [h[1] for h in headings]
    assert "Abstract" in titles
    assert "1 Introduction" in titles


def test_extract_headings_detects_numbered_academic_headings():
    """``1 Introduction`` / ``2.3 Methods`` style headings (PyMuPDF raw text)."""
    text = (
        "Some preamble text.\n"
        "1 Introduction\n\nContent of intro.\n\n"
        "2.3 Method Overview\n\nMethod content."
    )
    headings = _extract_headings(text)
    titles = [h[1] for h in headings]
    assert "1 Introduction" in titles
    assert "2.3 Method Overview" in titles


def test_extract_headings_detects_bare_section_labels():
    """Bare-word section labels (Abstract, References, Acknowledgements)."""
    text = "Abstract\n\nThis is the abstract.\n\nReferences\n\n[1] Smith et al."
    headings = _extract_headings(text)
    titles = [h[1] for h in headings]
    assert "Abstract" in titles
    assert "References" in titles


def test_chunker_tags_section_type_from_markdown():
    """PDFChunker tags every chunk with the section_type derived from the
    most recent heading (markdown style)."""
    text = (
        "# Abstract\n\n" + "Abstract content. " * 30
        + "\n\n# 1 Introduction\n\n" + "Intro content. " * 30
        + "\n\n# 2 Methods\n\n" + "Methods content. " * 30
    )
    chunker = PDFChunker(chunk_chars=200)
    chunks = chunker.chunk(text, {})
    section_types = {c.metadata["section_type"] for c in chunks}
    assert "abstract" in section_types
    assert "introduction" in section_types
    assert "methods" in section_types


def test_chunker_tags_section_title_with_raw_heading_text():
    """section_title carries the raw heading text (for display / citation).
    For top-level headings the chain is just the single heading."""
    text = "# 1 Introduction\n\n" + "Content. " * 100
    chunker = PDFChunker(chunk_chars=200)
    chunks = chunker.chunk(text, {})
    assert all(c.metadata["section_title"] == "1 Introduction" for c in chunks)
    assert all(c.metadata["section_type"] == "introduction" for c in chunks)


def test_chunker_section_title_is_hierarchical_path():
    """section_title carries the full ancestor chain joined with " > "
    (matches SemanticMarkdownChunker convention)."""
    text = (
        "# 3 METHODOLOGY\n\nIntro.\n\n"
        "# 3.1 Chunked Attention\n\n" + "Subsection. " * 80
    )
    chunker = PDFChunker(chunk_chars=200)
    chunks = chunker.chunk(text, {})
    sub_chunks = [c for c in chunks if " > " in c.metadata["section_title"]]
    assert sub_chunks, "Expected at least one chunk under 3.1 with hierarchical title"
    assert sub_chunks[0].metadata["section_title"] == "3 METHODOLOGY > 3.1 Chunked Attention"


def test_chunker_tags_empty_section_when_no_headings():
    """Documents without detectable headings get section_type=''/section_title=''."""
    text = "Just paragraphs. No headings to be found here. " * 50
    chunker = PDFChunker(chunk_chars=200)
    chunks = chunker.chunk(text, {})
    assert all(c.metadata["section_type"] == "" for c in chunks)
    assert all(c.metadata["section_title"] == "" for c in chunks)


def test_subsections_inherit_section_type_from_parent():
    """A 3.1 subsection inherits 'methods' from its 3 METHODOLOGY parent
    rather than falling to 'other'. Dotted-numeral hierarchy walk."""
    text = (
        "# 3 METHODOLOGY\n\nTop-level method intro.\n\n"
        "# 3.1 Chunked Attention\n\n" + "Subsection one. " * 80
        + "\n\n# 3.2 Differential Attention\n\n" + "Subsection two. " * 80
    )
    chunker = PDFChunker(chunk_chars=200)
    chunks = chunker.chunk(text, {})
    types = [c.metadata["section_type"] for c in chunks]
    # Every chunk under 3.x should classify as methods, not 'other'.
    assert "methods" in types
    # The subsections themselves should not show as 'other'.
    # Section titles are now hierarchical ("3 METHODOLOGY > 3.1 ...");
    # match on the leaf segment.
    subsection_types = {
        c.metadata["section_type"]
        for c in chunks
        if c.metadata["section_title"].endswith(" > 3.1 Chunked Attention")
        or c.metadata["section_title"].endswith(" > 3.2 Differential Attention")
    }
    assert subsection_types == {"methods"}, (
        f"Subsections must inherit 'methods'; got {subsection_types}"
    )


def test_deep_hierarchy_walks_to_top_level():
    """3.1.2 walks through 3.1 to find 3 METHODOLOGY → methods."""
    text = (
        "# 3 METHODOLOGY\n\nIntro.\n\n"
        "# 3.1 Approach\n\nSubsection.\n\n"
        "# 3.1.2 Deep Detail\n\n" + "Deep nested content. " * 80
    )
    chunker = PDFChunker(chunk_chars=200)
    chunks = chunker.chunk(text, {})
    deep_chunks = [
        c for c in chunks
        if c.metadata["section_title"].endswith(" > 3.1.2 Deep Detail")
    ]
    assert deep_chunks
    assert all(c.metadata["section_type"] == "methods" for c in deep_chunks)
    # The chain must include all three ancestors.
    assert all(
        c.metadata["section_title"]
        == "3 METHODOLOGY > 3.1 Approach > 3.1.2 Deep Detail"
        for c in deep_chunks
    )


def test_chunker_pre_heading_chunks_get_empty_section():
    """Chunks that fall before the first heading carry empty section
    metadata (the title page of an academic paper, for example)."""
    text = "Title page content. " * 30 + "\n\n# 1 Introduction\n\n" + "Intro. " * 30
    chunker = PDFChunker(chunk_chars=200)
    chunks = chunker.chunk(text, {})
    # First chunk is before the heading
    assert chunks[0].metadata["section_type"] == ""
    # Some later chunk hits the introduction
    intro_types = [c.metadata["section_type"] for c in chunks if c.metadata["section_type"] == "introduction"]
    assert intro_types, "Expected at least one chunk tagged 'introduction'"
