"""AC1–AC2: PDFExtractor — markdown extraction, Type3 fallback, font error fallback."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_extractor import PDFExtractor


@pytest.fixture
def extractor():
    return PDFExtractor()


@pytest.fixture
def dummy_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"dummy pdf bytes")
    return p


def _make_result(method: str):
    from nexus.pdf_extractor import ExtractionResult
    return ExtractionResult(
        text="some text",
        metadata={"extraction_method": method, "page_count": 1, "page_boundaries": [], "format": ""},
    )


# ── extract() branching ────────────────────────────────────────────────────────

def test_extract_uses_markdown_path_by_default(extractor, dummy_pdf):
    """Primary path: _extract_markdown called when no Type3 fonts."""
    expected = _make_result("pymupdf4llm_markdown")
    with patch.object(extractor, "_has_type3_fonts", return_value=False):
        with patch.object(extractor, "_extract_markdown", return_value=expected) as mock_md:
            result = extractor.extract(dummy_pdf)
    mock_md.assert_called_once_with(dummy_pdf)
    assert result.metadata["extraction_method"] == "pymupdf4llm_markdown"


def test_extract_falls_back_to_normalized_for_type3_fonts(extractor, dummy_pdf):
    """Type3 fonts detected → _extract_normalized, _extract_markdown never called."""
    expected = _make_result("pymupdf_normalized")
    with patch.object(extractor, "_has_type3_fonts", return_value=True):
        with patch.object(extractor, "_extract_normalized", return_value=expected) as mock_norm:
            with patch.object(extractor, "_extract_markdown") as mock_md:
                result = extractor.extract(dummy_pdf)
    mock_md.assert_not_called()
    assert result.metadata["extraction_method"] == "pymupdf_normalized"


def test_extract_falls_back_on_font_runtime_error(extractor, dummy_pdf):
    """RuntimeError with 'font' → normalized fallback, error not re-raised."""
    expected = _make_result("pymupdf_normalized")
    with patch.object(extractor, "_has_type3_fonts", return_value=False):
        with patch.object(extractor, "_extract_markdown",
                          side_effect=RuntimeError("code=4: no font file for digest")):
            with patch.object(extractor, "_extract_normalized", return_value=expected):
                result = extractor.extract(dummy_pdf)
    assert result.metadata["extraction_method"] == "pymupdf_normalized"


def test_extract_reraises_non_font_runtime_error(extractor, dummy_pdf):
    """Non-font RuntimeError propagates to caller."""
    with patch.object(extractor, "_has_type3_fonts", return_value=False):
        with patch.object(extractor, "_extract_markdown",
                          side_effect=RuntimeError("unrelated crash")):
            with pytest.raises(RuntimeError, match="unrelated crash"):
                extractor.extract(dummy_pdf)


# ── _has_type3_fonts ──────────────────────────────────────────────────────────

def test_has_type3_fonts_returns_true_when_present(extractor, dummy_pdf):
    """Returns True when any page uses a Type3 font."""
    type3_page = MagicMock()
    type3_page.get_fonts.return_value = [[None, None, "Type3", None, None, None]]

    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(side_effect=lambda: iter([type3_page]))
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)

    mock_pymupdf = MagicMock()
    mock_pymupdf.open.return_value = mock_doc

    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._has_type3_fonts(dummy_pdf)

    assert result is True


def test_has_type3_fonts_returns_false_with_no_type3(extractor, dummy_pdf):
    """Returns False when no Type3 fonts found."""
    normal_page = MagicMock()
    normal_page.get_fonts.return_value = [[None, None, "TrueType", None, None, None]]

    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(side_effect=lambda: iter([normal_page]))
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)

    mock_pymupdf = MagicMock()
    mock_pymupdf.open.return_value = mock_doc

    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._has_type3_fonts(dummy_pdf)

    assert result is False


# ── _extract_markdown page boundaries ─────────────────────────────────────────

def test_extract_markdown_tracks_page_boundaries(extractor, dummy_pdf):
    """_extract_markdown records a boundary entry for each non-empty page."""
    mock_doc = MagicMock()
    mock_doc.__len__ = MagicMock(return_value=3)
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)

    mock_pymupdf = MagicMock()
    mock_pymupdf.open.return_value = mock_doc

    mock_pymupdf4llm = MagicMock()
    # Page 2 returns empty → no boundary entry for page 2
    mock_pymupdf4llm.to_markdown.side_effect = ["# Page 1", "", "## Page 3"]

    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf, "pymupdf4llm": mock_pymupdf4llm}):
        result = extractor._extract_markdown(dummy_pdf)

    boundaries = result.metadata["page_boundaries"]
    assert len(boundaries) == 2
    assert boundaries[0]["page_number"] == 1
    assert boundaries[1]["page_number"] == 3
    assert result.metadata["extraction_method"] == "pymupdf4llm_markdown"
