# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

import nexus.md_chunker as md_mod
from nexus.md_chunker import SemanticMarkdownChunker, classify_section_type, parse_frontmatter


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def chunker():
    return SemanticMarkdownChunker(chunk_size=512)


@pytest.fixture
def small_chunker():
    return SemanticMarkdownChunker(chunk_size=50, chunk_overlap=0)


# ── parse_frontmatter ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, exp_fm, body_contains", [
    ("---\ntitle: My Doc\nauthor: Alice\n---\n\n# Content\nHello.",
     {"title": "My Doc", "author": "Alice"}, "# Content"),
    ("# Just a heading\n\nSome content.", {}, "# Just a heading"),
    ("---\n---\n\n# Body", {}, "# Body"),
    ("---\n: invalid: : yaml: [\n---\nBody text", {}, "Body text"),
    ("---\n- item1\n- item2\n---\nBody text", {}, "Body text"),
    ("---\ntitle: oops\nno closing delimiter", {}, "no closing delimiter"),
])
def test_parse_frontmatter(text, exp_fm, body_contains):
    fm, rest = parse_frontmatter(text)
    assert fm == exp_fm
    assert body_contains in rest


def test_parse_frontmatter_strips_delimiters():
    fm, rest = parse_frontmatter("---\ntitle: X\n---\n\n# Content\nHello.")
    assert "---" not in rest


# ── SemanticMarkdownChunker basics ───────────────────────────────────────────

def test_chunk_empty_text_returns_empty(chunker):
    assert chunker.chunk("", {}) == []
    assert chunker.chunk("   ", {}) == []


def test_chunk_headings_create_separate_sections(chunker):
    chunks = chunker.chunk("# Introduction\n\nIntro content.\n\n## Background\n\nBG content.", {})
    assert len(chunks) >= 2


def test_chunk_header_path_in_metadata(chunker):
    chunks = chunker.chunk("# Main\n\nMain content.\n\n## Sub\n\nSub content.",
                           {"source_path": "/doc.md"})
    header_paths = [c.metadata.get("header_path", "") for c in chunks]
    assert any("Sub" in (hp or "") for hp in header_paths)


def test_chunk_preserves_base_metadata(chunker):
    base = {"source_path": "/my/doc.md", "corpus": "notes"}
    chunks = chunker.chunk("# Hello\n\nWorld.", base)
    for c in chunks:
        assert c.metadata["source_path"] == "/my/doc.md"
        assert c.metadata["corpus"] == "notes"


def test_chunk_multiple_headings_no_index_error(chunker):
    chunks = chunker.chunk(
        "# Alpha\n\nContent A.\n\n# Beta\n\nContent B.\n\n# Gamma\n\nContent C.", {})
    titles = [c.metadata.get("header_path", "") for c in chunks]
    assert any("Alpha" in t for t in titles)
    assert any("Gamma" in t for t in titles)


def test_chunk_pre_heading_content_is_not_dropped(chunker):
    chunks = chunker.chunk("Preamble text before any heading.\n\n# First Section\n\nSection content.", {})
    combined = " ".join(c.text for c in chunks)
    assert "Preamble" in combined, "Pre-heading content was silently dropped"


def test_chunk_only_pre_heading_content(chunker):
    chunks = chunker.chunk("Just some plain content with no headings.\n\nAnother paragraph.", {})
    assert len(chunks) >= 1
    assert "plain content" in " ".join(c.text for c in chunks)


# ── naive fallback ───────────────────────────────────────────────────────────

def test_chunk_naive_fallback_without_markdown_it(monkeypatch):
    monkeypatch.setattr(md_mod, "MARKDOWN_IT_AVAILABLE", False)
    chunker = SemanticMarkdownChunker(chunk_size=50)
    chunker.md = None
    chunks = chunker.chunk("Some content here.\n\nMore content below.\n\nThird paragraph.", {})
    assert len(chunks) >= 1
    assert all(c.text.strip() for c in chunks)


def test_chunk_semantic_exception_falls_back_to_naive():
    chunker = SemanticMarkdownChunker(chunk_size=512)
    chunker.md.parse = lambda text: (_ for _ in ()).throw(RuntimeError("boom"))
    chunks = chunker.chunk("Some plain text content.", {"source": "test"})
    assert len(chunks) >= 1
    assert "plain text" in chunks[0].text


# ── chunk offset metadata ───────────────────────────────────────────────────

def test_semantic_chunk_offsets(chunker):
    chunks = chunker.chunk("# Alpha\n\nContent for alpha.\n\n# Beta\n\nContent for beta.", {})
    assert len(chunks) >= 2
    for c in chunks:
        assert "chunk_start_char" in c.metadata
        assert "chunk_end_char" in c.metadata
        assert c.metadata["chunk_end_char"] >= c.metadata["chunk_start_char"]

    starts = [c.metadata["chunk_start_char"] for c in chunks]
    for a, b in zip(starts, starts[1:]):
        assert a <= b, f"chunk_start_char decreased: {a} -> {b}"


# ── _split_large_section ────────────────────────────────────────────────────

def test_split_large_section_truncates_oversized_part():
    chunker = SemanticMarkdownChunker(chunk_size=10, chunk_overlap=0)
    section = {
        "level": 1, "header": "Big Section", "header_path": ["Big Section"],
        "content_parts": [{"type": "text", "content": "x" * 10000, "is_code_block": False}],
    }
    chunks = chunker._split_large_section(section, {}, start_index=0)
    assert len(chunks) >= 1
    for chunk in chunks:
        assert len(chunk.text) <= chunker.max_chars + len("# Big Section") + 4


def test_split_large_section_truncation_with_overlap():
    chunker = SemanticMarkdownChunker(chunk_size=10, chunk_overlap=3)
    section = {
        "level": 1, "header": "Big", "header_path": ["Big"],
        "content_parts": [{"type": "text", "content": "x" * 10000, "is_code_block": False}],
    }
    chunks = chunker._split_large_section(section, {}, start_index=0)
    assert len(chunks) >= 1
    budget = chunker.max_chars + len("# Big") + chunker.overlap_chars + 8
    for chunk in chunks:
        assert len(chunk.text) <= budget


# ── dedup blocklist ──────────────────────────────────────────────────────────

def test_no_duplicate_content_in_chunk():
    chunks = SemanticMarkdownChunker().chunk(
        "### Section\n\nA paragraph with content.\n\n- bullet one\n- bullet two\n", {})
    assert len(chunks) == 1
    assert chunks[0].text.count("A paragraph with content.") == 1
    assert chunks[0].text.count("bullet one") == 1


def test_structural_token_not_duplicated():
    chunks = SemanticMarkdownChunker().chunk("Paragraph text.\n\nSecond paragraph.\n", {})
    full_text = "\n".join(c.text for c in chunks)
    assert full_text.count("Paragraph text.") == 1
    assert full_text.count("Second paragraph.") == 1


def test_spurious_split_resolved():
    sentence = "The quick brown fox jumps over the lazy dog and continues running. "
    chunks = SemanticMarkdownChunker().chunk(f"### Section\n\n{sentence * 17}", {})
    assert len(chunks) == 1, f"Expected 1 chunk after dedup fix, got {len(chunks)}"


# ── byte cap post-pass ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "# Section\n\n```python\n{code}\n```\n",
    "```python\n{code}\n```",
], ids=["with-heading", "no-heading"])
def test_md_chunker_byte_cap_enforced(text):
    from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
    text = text.format(code="x" * 15_000)
    chunks = SemanticMarkdownChunker(preserve_code_blocks=True).chunk(text, {})
    assert len(chunks) >= 1
    for c in chunks:
        assert len(c.text.encode()) <= SAFE_CHUNK_BYTES


# ── overlap in _split_large_section ─────────────────────────────────────────

def _assert_overlap_at_start(chunks, overlap_chars):
    for i in range(len(chunks) - 1):
        tail = chunks[i].text[-overlap_chars:]
        next_text = chunks[i + 1].text
        body_start = next_text.find("\n\n")
        next_body = next_text[body_start + 2:] if body_start != -1 else next_text
        assert next_body.startswith(tail), (
            f"Chunk {i} tail not at START of chunk {i+1} body.\n"
            f"  tail ({overlap_chars} chars): {tail!r}\n"
            f"  next body: {next_body!r}")


def test_split_large_section_overlap():
    chunker = SemanticMarkdownChunker(chunk_size=50, chunk_overlap=10)
    para = "Alpha bravo charlie delta echo foxtrot golf hotel. "
    section = {
        "level": 2, "header": "Test", "header_path": ["Test"],
        "content_parts": [
            {"type": "text", "content": para + suffix, "is_code_block": False}
            for suffix in ("One.", "Two.", "Three.", "Four.", "Five.")
        ],
    }
    chunks = chunker._split_large_section(section, {}, start_index=0)
    assert len(chunks) >= 3
    _assert_overlap_at_start(chunks, chunker.overlap_chars)


def test_split_large_section_overlap_no_header_duplication():
    chunker = SemanticMarkdownChunker(chunk_size=20, chunk_overlap=15)
    section = {
        "level": 2, "header": "Hdr", "header_path": ["Hdr"],
        "content_parts": [
            {"type": "text", "content": t, "is_code_block": False}
            for t in ("Short.", "Second part is here.", "Third part follows.")
        ],
    }
    chunks = chunker._split_large_section(section, {}, start_index=0)
    for chunk in chunks:
        assert chunk.text.count("## Hdr") <= 1


# ── classify_section_type ────────────────────────────────────────────────────

@pytest.mark.parametrize("header_path, expected", [
    ([], ""),
    (["Abstract"], "abstract"),
    (["ABSTRACT"], "abstract"),
    (["abstract"], "abstract"),
    (["1. Introduction"], "introduction"),
    (["Introduction"], "introduction"),
    (["Methods"], "methods"),
    (["Materials and Methods"], "methods"),
    (["Methodology"], "methods"),
    (["3. Methods"], "methods"),
    (["Results"], "results"),
    (["Results and Discussion"], "results"),
    (["4. Results"], "results"),
    (["Discussion"], "discussion"),
    (["5. Discussion"], "discussion"),
    (["Conclusion"], "conclusion"),
    (["Conclusions"], "conclusion"),
    (["6. Conclusion"], "conclusion"),
    (["References"], "references"),
    (["Reference"], "references"),
    (["Acknowledgements"], "acknowledgements"),
    (["Acknowledgments"], "acknowledgements"),
    (["Appendix A"], "appendix"),
    (["Appendices"], "appendix"),
    (["Related Work"], "related_work"),
    (["Background"], "related_work"),
    (["Prior Work"], "related_work"),
    (["Evaluation"], "results"),
    (["Experiments"], "results"),
    (["4 Evaluation"], "results"),
    (["Experimental Results"], "results"),
    (["Empirical Study"], "results"),
    (["Findings"], "results"),
    (["Approach"], "methods"),
    (["Algorithm"], "methods"),
    (["System Design"], "methods"),
    (["Architecture"], "methods"),
    (["Future Work"], "conclusion"),
    (["Summary"], "conclusion"),
    (["Introduction", "Background"], "related_work"),
    (["References", "Cited Works"], "references"),
])
def test_classify_section_type(header_path, expected):
    assert classify_section_type(header_path) == expected


# ── section_type in metadata ─────────────────────────────────────────────────

@pytest.mark.parametrize("header_path, expected", [
    (["Abstract"], "abstract"),
    ([], ""),
    (["Related Work"], "related_work"),
])
def test_make_chunk_section_type(header_path, expected):
    chunker = SemanticMarkdownChunker(chunk_size=512)
    chunk = chunker._make_chunk("text", 0, {}, header_path)
    assert chunk.metadata["section_type"] == expected


def test_full_chunk_pipeline_has_section_type(chunker):
    chunks = chunker.chunk("# Abstract\n\nThis paper presents...\n\n# References\n\n[1] Foo et al.", {})
    for c in chunks:
        assert "section_type" in c.metadata


def test_naive_fallback_has_section_type(monkeypatch):
    monkeypatch.setattr(md_mod, "MARKDOWN_IT_AVAILABLE", False)
    chunker = SemanticMarkdownChunker(chunk_size=50)
    chunker.md = None
    chunks = chunker.chunk("Some content here.\n\nMore content.", {})
    for c in chunks:
        assert c.metadata.get("section_type") == ""
