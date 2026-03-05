"""RDR-021: PDFExtractor — Docling primary extraction, pymupdf_normalized fallback."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_extractor import PDFExtractor, ExtractionResult, _normalize_whitespace_edge_cases


@pytest.fixture
def extractor():
    return PDFExtractor()


@pytest.fixture
def dummy_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"dummy pdf bytes")
    return p


def _make_result(method: str, text: str = "some text") -> ExtractionResult:
    return ExtractionResult(
        text=text,
        metadata={"extraction_method": method, "page_count": 1, "page_boundaries": [], "format": ""},
    )


def _make_mock_docling(pages: list[str], title: str = ""):
    """Build a mock Docling converter result.

    pages: per-page markdown strings (empty string → page skipped).
    title: returned by _extract_title via the doc mock.
    """
    mock_doc = MagicMock()
    mock_doc.num_pages.return_value = len(pages)
    mock_doc.export_to_markdown.side_effect = pages

    mock_result = MagicMock()
    mock_result.document = mock_doc

    mock_converter = MagicMock()
    mock_converter.convert.return_value = mock_result

    return mock_converter, mock_doc


# ── extract() primary path ────────────────────────────────────────────────────

def test_extract_uses_docling_by_default(extractor, dummy_pdf):
    """Primary path: _extract_with_docling is called for a normal PDF."""
    expected = _make_result("docling")
    with patch.object(extractor, "_extract_with_docling", return_value=expected) as mock_docling:
        result = extractor.extract(dummy_pdf)
    mock_docling.assert_called_once_with(dummy_pdf)
    assert result.metadata["extraction_method"] == "docling"


def test_extract_falls_back_on_docling_exception(extractor, dummy_pdf):
    """Any Docling exception triggers pymupdf_normalized fallback."""
    expected = _make_result("pymupdf_normalized")
    with patch.object(extractor, "_extract_with_docling", side_effect=RuntimeError("crash")):
        with patch.object(extractor, "_extract_normalized", return_value=expected) as mock_norm:
            result = extractor.extract(dummy_pdf)
    mock_norm.assert_called_once_with(dummy_pdf)
    assert result.metadata["extraction_method"] == "pymupdf_normalized"


# ── _extract_with_docling — page boundaries ───────────────────────────────────

def test_extract_with_docling_page_boundaries(extractor, dummy_pdf):
    """Per-page boundaries are recorded for non-empty pages only."""
    mock_converter, mock_doc = _make_mock_docling(["# Page 1", "", "## Page 3"])
    extractor._converter = mock_converter

    with patch.object(extractor, "_extract_title", return_value="Test Title"):
        result = extractor._extract_with_docling(dummy_pdf)

    boundaries = result.metadata["page_boundaries"]
    assert len(boundaries) == 2
    assert boundaries[0]["page_number"] == 1
    assert boundaries[1]["page_number"] == 3
    assert result.metadata["extraction_method"] == "docling"


def test_extract_with_docling_boundary_positions(extractor, dummy_pdf):
    """start_char of page N+1 == start_char + page_text_length of page N."""
    page1 = "# Title\n\nBody text."   # 20 chars
    page2 = "## Section\n\nMore."     # 18 chars
    mock_converter, mock_doc = _make_mock_docling([page1, page2])
    extractor._converter = mock_converter

    with patch.object(extractor, "_extract_title", return_value=""):
        result = extractor._extract_with_docling(dummy_pdf)

    boundaries = result.metadata["page_boundaries"]
    assert boundaries[0]["start_char"] == 0
    assert boundaries[1]["start_char"] == boundaries[0]["page_text_length"]


def test_extract_with_docling_raises_on_empty_output(extractor, dummy_pdf):
    """RuntimeError raised when Docling produces no text (triggers fallback in extract())."""
    mock_converter, mock_doc = _make_mock_docling(["", "", ""])
    extractor._converter = mock_converter

    with pytest.raises(RuntimeError, match="empty output"):
        extractor._extract_with_docling(dummy_pdf)


def test_extract_with_docling_stores_title(extractor, dummy_pdf):
    """docling_title is stored in metadata from _extract_title()."""
    mock_converter, mock_doc = _make_mock_docling(["# Content"])
    extractor._converter = mock_converter

    with patch.object(extractor, "_extract_title", return_value="My Paper"):
        result = extractor._extract_with_docling(dummy_pdf)

    assert result.metadata["docling_title"] == "My Paper"


# ── _extract_title ────────────────────────────────────────────────────────────

def _make_item(text: str, label: str, page_no: int) -> MagicMock:
    """Build a mock Docling document item."""
    prov = MagicMock()
    prov.page_no = page_no

    item = MagicMock()
    item.text = text
    item.label = label
    item.prov = [prov]
    return item


def test_extract_title_returns_first_title_label(extractor):
    """First page-1 item with 'title' in label is returned; plain 'text' label items are skipped."""
    doc = MagicMock()
    items = [
        (_make_item("Some text block", "text", 1), None),       # label 'text' — skip in pass 1
        (_make_item("My Great Paper", "title", 1), None),       # label 'title' — return this
        (_make_item("Other content", "section_header", 1), None),
    ]
    # Pass 1 (label scan) runs; pass 2 (fallback) not needed
    doc.iterate_items.return_value = iter(items)
    assert extractor._extract_title(doc) == "My Great Paper"


def test_extract_title_skips_abstract_keyword(extractor):
    """Items whose text is exactly 'abstract' (case-insensitive) are skipped."""
    doc = MagicMock()
    items = [
        (_make_item("Abstract", "section_header", 1), None),
        (_make_item("My Real Title", "title", 1), None),
    ]
    doc.iterate_items.return_value = iter(items)
    # "Abstract" is 8 chars < 10 → skipped by length check; title returned
    result = extractor._extract_title(doc)
    assert result == "My Real Title"


def test_extract_title_skips_page2_items(extractor):
    """Items on page 2+ are not considered for title extraction."""
    doc = MagicMock()
    items = [
        (_make_item("Page 2 Title", "title", 2), None),
        (_make_item("Short Title Here", "title", 1), None),
    ]
    doc.iterate_items.return_value = iter(items)
    result = extractor._extract_title(doc)
    assert result == "Short Title Here"


def test_extract_title_falls_back_to_short_text(extractor):
    """Fallback: returns first page-1 text with 10 ≤ len < 120 when no title label found."""
    doc = MagicMock()
    # No 'title' or 'section_header' labels
    items = [
        (_make_item("Short title text", "text", 1), None),
    ]
    # Both passes iterate the same items — use side_effect to supply twice
    doc.iterate_items.side_effect = [iter(items), iter(items)]
    result = extractor._extract_title(doc)
    assert result == "Short title text"


def test_extract_title_returns_empty_when_nothing_found(extractor):
    """Returns '' when no suitable items exist on page 1."""
    doc = MagicMock()
    doc.iterate_items.return_value = iter([])
    assert extractor._extract_title(doc) == ""


# ── _extract_normalized (fallback) ────────────────────────────────────────────

def test_extract_normalized_tracks_page_boundaries(extractor, dummy_pdf):
    """_extract_normalized records a boundary for each non-empty page."""
    mock_page0 = MagicMock()
    mock_page0.get_text.return_value = "Page one text."
    mock_page1 = MagicMock()
    mock_page1.get_text.return_value = ""

    mock_doc = MagicMock()
    mock_doc.__len__ = MagicMock(return_value=2)
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page0, mock_page1]))
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)
    mock_doc.metadata = {}

    mock_pymupdf = MagicMock()
    mock_pymupdf.open.return_value = mock_doc

    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._extract_normalized(dummy_pdf)

    boundaries = result.metadata["page_boundaries"]
    assert len(boundaries) == 1
    assert boundaries[0]["page_number"] == 1
    assert result.metadata["extraction_method"] == "pymupdf_normalized"


def test_extract_normalized_includes_docling_title_empty(extractor, dummy_pdf):
    """_extract_normalized always sets docling_title to '' (not populated by pymupdf)."""
    mock_page = MagicMock()
    mock_page.get_text.return_value = "Some text here."

    mock_doc = MagicMock()
    mock_doc.__len__ = MagicMock(return_value=1)
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)
    mock_doc.metadata = {"title": "PDF Meta Title"}

    mock_pymupdf = MagicMock()
    mock_pymupdf.open.return_value = mock_doc

    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._extract_normalized(dummy_pdf)

    assert result.metadata["docling_title"] == ""
    assert result.metadata["pdf_title"] == "PDF Meta Title"


# ── _get_converter lazy init ──────────────────────────────────────────────────

def test_get_converter_is_lazy(extractor):
    """Converter is None before first call."""
    assert extractor._converter is None


def test_get_converter_caches_instance(extractor):
    """_get_converter returns the same instance on repeated calls."""
    mock_converter = MagicMock()
    mock_docling = MagicMock()
    mock_docling.DocumentConverter.return_value = mock_converter
    mock_docling.PdfFormatOption = MagicMock()

    mock_pipeline = MagicMock()
    mock_pipeline.PdfPipelineOptions.return_value = MagicMock()

    with patch.dict("sys.modules", {
        "docling": MagicMock(),
        "docling.document_converter": mock_docling,
        "docling.datamodel": MagicMock(),
        "docling.datamodel.pipeline_options": mock_pipeline,
    }):
        c1 = extractor._get_converter()
        c2 = extractor._get_converter()

    assert c1 is c2


# ── _normalize_whitespace_edge_cases ──────────────────────────────────────────

def test_normalize_whitespace_tab():
    """Tab characters are replaced with a single space."""
    assert _normalize_whitespace_edge_cases("hello\tworld") == "hello world"


def test_normalize_whitespace_nbsp():
    """Unicode non-breaking spaces are collapsed to a single space."""
    text = "word\u00a0\u00a0another"
    result = _normalize_whitespace_edge_cases(text)
    assert "\u00a0" not in result
    assert "word another" == result


def test_normalize_whitespace_excess_newlines():
    """Four or more consecutive newlines are collapsed to three."""
    assert _normalize_whitespace_edge_cases("a\n\n\n\n\nb") == "a\n\n\nb"
    # Three newlines are preserved as-is.
    assert _normalize_whitespace_edge_cases("a\n\n\nb") == "a\n\n\nb"
