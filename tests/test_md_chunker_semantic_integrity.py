"""Fix E / nexus-0qnh: SemanticMarkdownChunker structural integrity tests.

All three tests pass after nexus-n72s (Fix C2 — preserve_code_blocks) and
nexus-q25l (Fix 2 — structural token dedup):
- test_code_block_not_split: Fix C2 preserves code blocks intact (no truncation)
- test_list_intact: Fix 2 prevents list_item_open from duplicating inline content
- test_table_pipes_preserved: table inline tokens carry pipe chars; tables preserved
"""
from nexus.md_chunker import SemanticMarkdownChunker


# ── code block integrity ───────────────────────────────────────────────────────

def test_code_block_not_split() -> None:
    """Fenced code block must not be truncated even when larger than chunk_size.

    Current _split_large_section truncates parts > max_chars (≈1689 chars at
    default chunk_size=512).  Fix C2 must preserve code blocks intact.

    Drives nexus-n72s (Fix C2).
    """
    # Build a code block larger than max_chars = chunk_size * 3.3 ≈ 1689 chars
    # 70 lines × ~32 chars = ~2240 chars → exceeds max_chars → gets truncated
    code_lines = "\n".join(
        f"    result_{i:03d} = compute_value(input_{i:03d}, factor={i})"
        for i in range(70)
    )
    marker_first = "result_000"
    marker_last = "result_069"
    code_block = f"```python\n{code_lines}\n```"
    text = f"## Implementation\n\n{code_block}"

    chunks = SemanticMarkdownChunker().chunk(text, {})
    full_text = "\n".join(c.text for c in chunks)
    assert marker_first in full_text, "Code block start was truncated"
    assert marker_last in full_text, "Code block end was truncated (Fix C2 needed)"


# ── list integrity ─────────────────────────────────────────────────────────────

def test_list_intact() -> None:
    """Bulleted list items must appear exactly once (no paragraph_open duplication).

    Verifies nexus-q25l Fix 2 holds: list_item_open is in _STRUCTURAL_TOKEN_TYPES
    so it does not duplicate inline content.
    """
    text = (
        "## Features\n\n"
        "- Alpha: first item\n"
        "- Beta: second item\n"
        "- Gamma: third item\n"
    )
    chunks = SemanticMarkdownChunker().chunk(text, {})
    assert len(chunks) == 1, f"Expected 1 chunk for short list, got {len(chunks)}"
    full_text = chunks[0].text
    assert full_text.count("Alpha: first item") == 1, "List item duplicated"
    assert full_text.count("Beta: second item") == 1, "List item duplicated"
    assert full_text.count("Gamma: third item") == 1, "List item duplicated"


# ── table integrity ────────────────────────────────────────────────────────────

def test_table_pipes_preserved() -> None:
    """Pipe table must include pipe characters in chunked output.

    Table inline tokens (td_open content, inline) carry the pipe-delimited text
    directly.  tr_open/td_open/th_open are in _STRUCTURAL_TOKEN_TYPES so the
    container open/close tokens are silently dropped, but the inline child tokens
    that hold actual cell text (including pipes) are preserved.
    """
    text = (
        "## Results\n\n"
        "| Model | Accuracy | Latency |\n"
        "| ----- | -------- | ------- |\n"
        "| A     | 0.92     | 12ms    |\n"
        "| B     | 0.89     | 8ms     |\n"
    )
    chunks = SemanticMarkdownChunker().chunk(text, {})
    full_text = "\n".join(c.text for c in chunks)
    # Cell content must be present
    assert "Accuracy" in full_text, "Table header content missing"
    assert "0.92" in full_text, "Table cell content missing"
    # Table structure (pipe characters) must be preserved
    assert "|" in full_text, "Pipe characters missing from table output (Fix C2 needed)"
