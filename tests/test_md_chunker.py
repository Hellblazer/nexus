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
