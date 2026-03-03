"""AC4–AC5: SemanticMarkdownChunker — headings, frontmatter, naive fallback."""
import nexus.md_chunker as md_mod
from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter


# ── parse_frontmatter ─────────────────────────────────────────────────────────

def test_parse_frontmatter_extracts_key_value_pairs():
    """YAML frontmatter block parsed into metadata dict."""
    text = "---\ntitle: My Doc\nauthor: Alice\n---\n\n# Content\n\nHello."
    fm, rest = parse_frontmatter(text)
    assert fm["title"] == "My Doc"
    assert fm["author"] == "Alice"
    assert "# Content" in rest
    assert "---" not in rest


def test_parse_frontmatter_no_frontmatter_returns_empty():
    """Text without leading --- returns ({}, original_text)."""
    text = "# Just a heading\n\nSome content."
    fm, rest = parse_frontmatter(text)
    assert fm == {}
    assert rest == text


def test_parse_frontmatter_empty_frontmatter():
    """--- ... --- with no keys returns ({}, body)."""
    text = "---\n---\n\n# Body"
    fm, rest = parse_frontmatter(text)
    assert fm == {}
    assert "# Body" in rest


# ── SemanticMarkdownChunker ───────────────────────────────────────────────────

def test_chunk_empty_text_returns_empty():
    chunker = SemanticMarkdownChunker()
    assert chunker.chunk("", {}) == []
    assert chunker.chunk("   ", {}) == []


def test_chunk_headings_create_separate_sections():
    """H1 and H2 headings each start a new section → at least 2 chunks."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "# Introduction\n\nIntro content.\n\n## Background\n\nBG content."
    chunks = chunker.chunk(text, {})
    assert len(chunks) >= 2


def test_chunk_header_path_in_metadata():
    """Sub-section chunks carry header_path metadata for retrieval context."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "# Main\n\nMain content.\n\n## Sub\n\nSub content."
    chunks = chunker.chunk(text, {"source_path": "/doc.md"})
    # At least one chunk should reference the sub-section path
    header_paths = [c.metadata.get("header_path", "") for c in chunks]
    assert any("Sub" in (hp or "") for hp in header_paths)


def test_chunk_naive_fallback_without_markdown_it(monkeypatch):
    """Falls back to naive chunking when markdown-it-py is unavailable."""
    monkeypatch.setattr(md_mod, "MARKDOWN_IT_AVAILABLE", False)
    chunker = SemanticMarkdownChunker(chunk_size=50)
    chunker.md = None
    text = "Some content here.\n\nMore content below.\n\nThird paragraph."
    chunks = chunker.chunk(text, {})
    assert len(chunks) >= 1
    assert all(c.text.strip() for c in chunks)


def test_chunk_preserves_base_metadata():
    """Base metadata dict is merged into every chunk's metadata."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    base = {"source_path": "/my/doc.md", "corpus": "notes"}
    chunks = chunker.chunk("# Hello\n\nWorld.", base)
    for c in chunks:
        assert c.metadata["source_path"] == "/my/doc.md"
        assert c.metadata["corpus"] == "notes"


def test_chunk_multiple_headings_no_index_error():
    """Multiple headings in sequence do not crash the token advance loop.

    Exercises the forward-search for heading_close (guards against the old
    hardcoded i += 3 assumption).
    """
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "# Alpha\n\nContent A.\n\n# Beta\n\nContent B.\n\n# Gamma\n\nContent C."
    chunks = chunker.chunk(text, {})
    titles = [c.metadata.get("header_path", "") for c in chunks]
    assert any("Alpha" in t for t in titles)
    assert any("Gamma" in t for t in titles)


# ── nexus-zmu: pre-heading content must not be silently dropped ───────────────

def test_chunk_pre_heading_content_is_not_dropped():
    """Content before the first heading is preserved as its own chunk."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "Preamble text before any heading.\n\n# First Section\n\nSection content."
    chunks = chunker.chunk(text, {})
    combined = " ".join(c.text for c in chunks)
    assert "Preamble" in combined, "Pre-heading content was silently dropped"


def test_chunk_only_pre_heading_content_returns_one_chunk():
    """A document with no headings at all returns its content as a single chunk."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "Just some plain content with no headings.\n\nAnother paragraph."
    chunks = chunker.chunk(text, {})
    assert len(chunks) >= 1
    combined = " ".join(c.text for c in chunks)
    assert "plain content" in combined


# ── nexus-9ar: semantic chunker must write chunk_start_char/chunk_end_char ────

def test_semantic_chunk_start_char_is_not_all_zero():
    """Every semantic chunk must have chunk_start_char set in its metadata."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "# Alpha\n\nContent for alpha.\n\n# Beta\n\nContent for beta."
    chunks = chunker.chunk(text, {})
    assert len(chunks) >= 2, "Expected at least 2 chunks"
    for c in chunks:
        assert "chunk_start_char" in c.metadata, "chunk_start_char missing from metadata"
        assert "chunk_end_char" in c.metadata, "chunk_end_char missing from metadata"


def test_semantic_chunk_offsets_are_monotonically_increasing():
    """chunk_start_char offsets across sections should be non-decreasing."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "# Section One\n\nContent one.\n\n# Section Two\n\nContent two.\n\n# Section Three\n\nContent three."
    chunks = chunker.chunk(text, {})
    starts = [c.metadata.get("chunk_start_char", 0) for c in chunks]
    for a, b in zip(starts, starts[1:]):
        assert a <= b, f"chunk_start_char decreased: {a} → {b}"


def test_semantic_chunk_end_char_greater_than_start():
    """For non-empty sections, chunk_end_char should be > chunk_start_char."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    text = "# Alpha\n\nSome alpha content here.\n\n# Beta\n\nSome beta content here."
    chunks = chunker.chunk(text, {})
    for c in chunks:
        start = c.metadata.get("chunk_start_char", 0)
        end = c.metadata.get("chunk_end_char", 0)
        assert end >= start, f"chunk_end_char ({end}) < chunk_start_char ({start})"


# ── nexus-9vp: oversized part is truncated in _split_large_section ───────────

def test_split_large_section_truncates_oversized_part():
    """A content part larger than max_chars is truncated to max_chars."""
    chunker = SemanticMarkdownChunker(chunk_size=10)  # max_chars ≈ 33 chars

    # Build a section with one part that vastly exceeds max_chars
    oversized = "x" * 10000  # 10000 chars >> max_chars (≈33)
    section = {
        "level": 1,
        "header": "Big Section",
        "header_path": ["Big Section"],
        "content_parts": [{"type": "text", "content": oversized, "is_code_block": False}],
    }
    chunks = chunker._split_large_section(section, {}, start_index=0)

    assert len(chunks) >= 1
    for chunk in chunks:
        assert len(chunk.text) <= chunker.max_chars + len("# Big Section") + 4, (
            f"Chunk text exceeds max_chars: {len(chunk.text)} chars"
        )


# ── frontmatter edge cases ──────────────────────────────────────────────────

def test_parse_frontmatter_invalid_yaml_returns_empty():
    """Malformed YAML in frontmatter returns ({}, body)."""
    text = "---\n: invalid: : yaml: [\n---\nBody text"
    fm, rest = parse_frontmatter(text)
    assert fm == {}
    assert "Body text" in rest


def test_parse_frontmatter_non_dict_returns_empty():
    """YAML parsing to a non-dict (e.g. list) is treated as empty."""
    text = "---\n- item1\n- item2\n---\nBody text"
    fm, rest = parse_frontmatter(text)
    assert fm == {}
    assert "Body text" in rest


def test_parse_frontmatter_unclosed_block():
    """No closing --- means no frontmatter detected."""
    text = "---\ntitle: oops\nno closing delimiter"
    fm, rest = parse_frontmatter(text)
    assert fm == {}
    assert rest == text


# ── semantic exception fallback ──────────────────────────────────────────────

def test_chunk_semantic_exception_falls_back_to_naive():
    """When markdown-it parse() raises, chunker falls back to naive splitting."""
    chunker = SemanticMarkdownChunker(chunk_size=512)
    original_parse = chunker.md.parse
    chunker.md.parse = lambda text: (_ for _ in ()).throw(RuntimeError("boom"))

    chunks = chunker.chunk("Some plain text content.", {"source": "test"})
    assert len(chunks) >= 1
    assert "plain text" in chunks[0].text

    chunker.md.parse = original_parse


# ── nexus-q25l: structural-token dedup blocklist ──────────────────────────────

def test_no_duplicate_content_in_chunk():
    """paragraph_open and list_item_open must not duplicate inline content."""
    text = "### Section\n\nA paragraph with content.\n\n- bullet one\n- bullet two\n"
    chunks = SemanticMarkdownChunker().chunk(text, {})
    assert len(chunks) == 1
    assert chunks[0].text.count("A paragraph with content.") == 1
    assert chunks[0].text.count("bullet one") == 1


def test_structural_token_not_duplicated():
    """Plain paragraphs must not appear twice due to paragraph_open .map fallback."""
    text = "Paragraph text.\n\nSecond paragraph.\n"
    chunks = SemanticMarkdownChunker().chunk(text, {})
    full_text = "\n".join(c.text for c in chunks)
    assert full_text.count("Paragraph text.") == 1
    assert full_text.count("Second paragraph.") == 1


def test_spurious_split_resolved():
    """Single paragraph ~345 tokens fits in 512-token chunk after dedup fix.

    sentence × 17 ≈ 1139 chars ÷ 3.3 ≈ 345 tokens (clean).
    With paragraph_open duplication: ≈ 690 tokens → spurious split.
    After fix: only inline token contributes → 345 tokens → one chunk.
    """
    sentence = "The quick brown fox jumps over the lazy dog and continues running. "
    paragraph = sentence * 17
    text = f"### Section\n\n{paragraph}"
    chunks = SemanticMarkdownChunker().chunk(text, {})
    assert len(chunks) == 1, (
        f"Expected 1 chunk after dedup fix, got {len(chunks)}"
    )


# ── Phase 2b: byte cap post-pass ──────────────────────────────────────────────

def test_md_chunker_byte_cap_enforced_on_large_code_fence() -> None:
    """Code fence larger than SAFE_CHUNK_BYTES must be truncated in the post-pass."""
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
    big_code = "x" * 15_000  # 15 KB > SAFE_CHUNK_BYTES (12 288)
    text = f"# Section\n\n```python\n{big_code}\n```\n"

    chunker = SemanticMarkdownChunker(preserve_code_blocks=True)
    chunks = chunker.chunk(text, {})

    assert len(chunks) >= 1
    for c in chunks:
        assert len(c.text.encode()) <= SAFE_CHUNK_BYTES, (
            f"Chunk exceeds SAFE_CHUNK_BYTES: {len(c.text.encode())} bytes (limit {SAFE_CHUNK_BYTES})"
        )


def test_md_chunker_byte_cap_no_code_fence() -> None:
    """Non-code content larger than SAFE_CHUNK_BYTES is also capped in the post-pass."""
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
    big_code = "y" * 15_000  # 15 KB single-line fence (no heading → naive path likely)
    text = f"```python\n{big_code}\n```"

    chunker = SemanticMarkdownChunker(preserve_code_blocks=True)
    chunks = chunker.chunk(text, {})

    assert len(chunks) >= 1
    for c in chunks:
        assert len(c.text.encode()) <= SAFE_CHUNK_BYTES, (
            f"Chunk exceeds SAFE_CHUNK_BYTES: {len(c.text.encode())} bytes (limit {SAFE_CHUNK_BYTES})"
        )
