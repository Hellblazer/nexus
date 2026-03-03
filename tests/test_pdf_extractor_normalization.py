"""Fix E / nexus-0qnh: PDF text normalization edge cases.

Tests _normalize_whitespace_edge_cases from nexus.pdf_extractor:
- Tab characters → single space
- U+00A0 and exotic Unicode whitespace → single space
- 4+ consecutive newlines → 3 newlines (implementation collapses to \\n\\n\\n)
- Combined edge cases
"""
from nexus.pdf_extractor import _normalize_whitespace_edge_cases


# ── tab normalization ──────────────────────────────────────────────────────────

def test_tab_normalization() -> None:
    """Tab characters are replaced with a single space."""
    result = _normalize_whitespace_edge_cases("word1\tword2\t\tword3")
    assert "\t" not in result, "Tab characters must be removed"
    assert "word1" in result
    assert "word2" in result
    assert "word3" in result


def test_tab_at_line_start() -> None:
    """Indentation tabs (common in copied PDF text) become spaces."""
    result = _normalize_whitespace_edge_cases("\tindented line")
    assert "\t" not in result
    assert "indented line" in result


# ── non-breaking space normalization ──────────────────────────────────────────

def test_nbsp_normalization() -> None:
    """U+00A0 (non-breaking space) is replaced with a regular space."""
    nbsp = "\u00A0"
    result = _normalize_whitespace_edge_cases(f"word1{nbsp}word2")
    assert nbsp not in result, "U+00A0 must be normalized"
    assert "word1" in result
    assert "word2" in result


def test_exotic_unicode_whitespace_normalization() -> None:
    """Exotic Unicode whitespace variants are collapsed to a single space."""
    # U+2003 EM SPACE, U+205F MEDIUM MATHEMATICAL SPACE, U+3000 IDEOGRAPHIC SPACE
    exotic = "\u2003text\u205F\u3000between"
    result = _normalize_whitespace_edge_cases(exotic)
    assert "\u2003" not in result
    assert "\u205F" not in result
    assert "\u3000" not in result
    assert "text" in result
    assert "between" in result


# ── excess newline normalization ───────────────────────────────────────────────

def test_four_newlines_collapsed_to_three() -> None:
    """4+ consecutive newlines are collapsed to 3 (implementation behaviour)."""
    result = _normalize_whitespace_edge_cases("para1\n\n\n\npara2")
    assert "\n\n\n\n" not in result, "4+ newlines must be collapsed"
    assert "para1" in result
    assert "para2" in result
    # Exactly 3 newlines remain (or fewer)
    assert result.count("\n\n\n\n") == 0


def test_three_newlines_preserved() -> None:
    """3 consecutive newlines are below the collapse threshold and stay as-is."""
    result = _normalize_whitespace_edge_cases("para1\n\n\npara2")
    # 3 newlines = below 4+ threshold; implementation does not collapse these
    assert "para1" in result
    assert "para2" in result
    # The 3-newline sequence should NOT be further collapsed
    assert "\n\n\n" in result


# ── combined edge cases ────────────────────────────────────────────────────────

def test_combined_edge_cases() -> None:
    """All normalization types applied together in one string."""
    nbsp = "\u00A0"
    em_space = "\u2003"
    text = f"Title\t\tAuthor\n{nbsp}Abstract{em_space}text\n\n\n\nConclusion"
    result = _normalize_whitespace_edge_cases(text)

    assert "\t" not in result, "Tabs must be removed"
    assert nbsp not in result, "NBSP must be normalized"
    assert em_space not in result, "EM SPACE must be normalized"
    assert "\n\n\n\n" not in result, "4+ newlines must be collapsed"
    assert "Title" in result
    assert "Abstract" in result
    assert "Conclusion" in result
