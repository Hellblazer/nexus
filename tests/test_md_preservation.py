# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for markdown frontmatter parsing and semantic chunking."""
from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter


# ── parse_frontmatter ────────────────────────────────────────────────────────

def test_frontmatter_basic_extraction():
    """parse_frontmatter extracts YAML frontmatter and returns body separately."""
    md = "---\ntitle: My Doc\nauthor: Test\n---\n\n# Body\n\nText here.\n"
    fm, body = parse_frontmatter(md)
    assert fm["title"] == "My Doc"
    assert fm["author"] == "Test"
    assert "# Body" in body
    assert "---" not in body


def test_frontmatter_missing():
    """parse_frontmatter returns empty dict when no frontmatter exists."""
    md = "# Just a heading\n\nSome text.\n"
    fm, body = parse_frontmatter(md)
    assert fm == {}
    assert "# Just a heading" in body


def test_frontmatter_empty_block():
    """parse_frontmatter handles empty frontmatter block."""
    md = "---\n---\n\nBody text.\n"
    fm, body = parse_frontmatter(md)
    assert fm == {} or fm is None or isinstance(fm, dict)
    assert "Body text" in body


def test_frontmatter_with_list_values():
    """parse_frontmatter handles YAML lists in frontmatter."""
    md = "---\ntitle: Doc\ntags:\n  - alpha\n  - beta\n---\n\nContent.\n"
    fm, body = parse_frontmatter(md)
    assert fm["title"] == "Doc"
    assert isinstance(fm["tags"], list)
    assert "alpha" in fm["tags"]


# ── SemanticMarkdownChunker ──────────────────────────────────────────────────

def test_heading_based_chunking():
    """SemanticMarkdownChunker splits on ## headings."""
    md = (
        "## Introduction\n\nThis is the intro section with some content.\n\n"
        "## Methods\n\nThis describes the methodology used.\n\n"
        "## Results\n\nHere are the results of our study.\n"
    )
    chunker = SemanticMarkdownChunker(chunk_size=2048)
    chunks = chunker.chunk(md, {})
    assert len(chunks) >= 2
    texts = [c.text for c in chunks]
    assert any("Introduction" in t for t in texts)
    assert any("Methods" in t or "Results" in t for t in texts)


def test_nested_heading_chunking():
    """SemanticMarkdownChunker handles ### subsections."""
    md = (
        "## Chapter 1\n\nIntro.\n\n"
        "### Section 1.1\n\nDetails of 1.1.\n\n"
        "### Section 1.2\n\nDetails of 1.2.\n\n"
        "## Chapter 2\n\nSecond chapter content.\n"
    )
    chunker = SemanticMarkdownChunker(chunk_size=2048)
    chunks = chunker.chunk(md, {})
    assert len(chunks) >= 2


def test_code_block_preservation():
    """Code blocks within chunks are preserved intact."""
    md = (
        "## Setup\n\nInstall with:\n\n"
        "```bash\npip install nexus\nnx --version\n```\n\n"
        "Then configure.\n"
    )
    chunker = SemanticMarkdownChunker(chunk_size=2048)
    chunks = chunker.chunk(md, {})
    assert len(chunks) >= 1
    combined = " ".join(c.text for c in chunks)
    assert "pip install nexus" in combined
    assert "nx --version" in combined


def test_no_heading_document():
    """A document with no headings produces at least one chunk."""
    md = "Just plain text without any headings or structure.\n\nAnother paragraph.\n"
    chunker = SemanticMarkdownChunker(chunk_size=2048)
    chunks = chunker.chunk(md, {})
    assert len(chunks) >= 1
    assert "plain text" in chunks[0].text


def test_chunk_metadata_includes_header_path():
    """Chunks carry header_path metadata showing their heading hierarchy."""
    md = "## Parent\n\n### Child\n\nContent under child heading.\n"
    chunker = SemanticMarkdownChunker(chunk_size=2048)
    chunks = chunker.chunk(md, {})
    assert len(chunks) >= 1
    # At least one chunk should have a header_path
    assert any(len(c.header_path) > 0 for c in chunks)


def test_chunk_index_sequential():
    """Chunk indices are sequential starting from 0."""
    md = "## A\n\nText A.\n\n## B\n\nText B.\n\n## C\n\nText C.\n"
    chunker = SemanticMarkdownChunker(chunk_size=2048)
    chunks = chunker.chunk(md, {})
    indices = [c.chunk_index for c in chunks]
    assert indices == list(range(len(chunks)))
