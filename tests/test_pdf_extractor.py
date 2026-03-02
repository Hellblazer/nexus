"""AC1–AC2 + RDR-012: PDFExtractor — markdown extraction, pdfplumber rescue, fallbacks."""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from nexus.pdf_extractor import PDFExtractor, _format_table, _normalize_whitespace_edge_cases


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


# ── _format_table ─────────────────────────────────────────────────────────────

def test_format_table_basic():
    """Header + separator + data row formatted as Markdown."""
    rows = [["Name", "Value"], ["foo", "bar"]]
    result = _format_table(rows)
    assert "| Name |" in result
    assert "| --- |" in result
    assert "| foo |" in result


def test_format_table_none_cells():
    """None cells are rendered as empty strings."""
    rows = [[None, "B"], ["x", None]]
    result = _format_table(rows)
    assert result  # non-empty
    assert "None" not in result


def test_format_table_empty():
    """Empty rows list returns empty string."""
    assert _format_table([]) == ""


# ── _markdown_misses_tables ───────────────────────────────────────────────────

def _mock_pymupdf_with_tables(n_tables: int):
    """Return a mock pymupdf module with n_tables ruled tables on page 0."""
    mock_finder = MagicMock()
    mock_finder.tables = [MagicMock()] * n_tables
    mock_page = MagicMock()
    mock_page.find_tables.return_value = mock_finder

    mock_doc = MagicMock()
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)
    mock_doc.__iter__ = MagicMock(side_effect=lambda: iter([mock_page]))

    mock_pymupdf = MagicMock()
    mock_pymupdf.open.return_value = mock_doc
    return mock_pymupdf


def test_markdown_misses_tables_detects_gap(extractor, dummy_pdf):
    """PDF has ruled tables; markdown has no pipes → returns True (rescue needed)."""
    mock_pymupdf = _mock_pymupdf_with_tables(n_tables=2)
    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._markdown_misses_tables(dummy_pdf, "prose only, no pipes here")
    assert result is True


def test_markdown_misses_tables_no_tables(extractor, dummy_pdf):
    """PDF has no ruled tables → returns False."""
    mock_pymupdf = _mock_pymupdf_with_tables(n_tables=0)
    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._markdown_misses_tables(dummy_pdf, "prose only")
    assert result is False


def test_markdown_misses_tables_pipes_present(extractor, dummy_pdf):
    """PDF has ruled tables; markdown already has sufficient pipes → returns False."""
    mock_pymupdf = _mock_pymupdf_with_tables(n_tables=1)
    # 1 table × 3 = threshold of 3 pipes; supply 5 to ensure False
    markdown_with_pipes = "| col1 | col2 | col3 | col4 | col5 |"
    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._markdown_misses_tables(dummy_pdf, markdown_with_pipes)
    assert result is False


def test_markdown_misses_tables_no_pdfplumber(extractor, dummy_pdf):
    """pdfplumber not installed → returns False (graceful degradation)."""
    import sys
    # Temporarily remove pdfplumber from sys.modules to simulate ImportError
    saved = sys.modules.pop("pdfplumber", None)
    try:
        with patch.dict("sys.modules", {"pdfplumber": None}):
            result = extractor._markdown_misses_tables(dummy_pdf, "prose")
        assert result is False
    finally:
        if saved is not None:
            sys.modules["pdfplumber"] = saved


# ── _extract_with_pdfplumber ──────────────────────────────────────────────────

def _make_mock_pdfplumber(pages_data: list[dict]):
    """Build a mock pdfplumber module.

    pages_data: list of dicts with keys:
        prose (str): text from extract_text()
        tables (list[list[list[str|None]]]): from extract_tables()
        page_number (int): 1-based
    """
    mock_pages = []
    for pd in pages_data:
        mock_page = MagicMock()
        mock_page.page_number = pd["page_number"]
        mock_page.find_tables.return_value = []  # no bboxes → prose path
        mock_page.extract_text.return_value = pd.get("prose", "")
        mock_page.extract_tables.return_value = pd.get("tables", [])
        mock_pages.append(mock_page)

    mock_pdf = MagicMock()
    mock_pdf.metadata = {"Title": "Test", "Author": "A"}
    mock_pdf.pages = mock_pages
    mock_pdf.__len__ = MagicMock(return_value=len(mock_pages))
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)

    mock_pdfplumber = MagicMock()
    mock_pdfplumber.open.return_value = mock_pdf
    return mock_pdfplumber


def test_extract_with_pdfplumber_produces_pipes(extractor, dummy_pdf):
    """_extract_with_pdfplumber includes | characters when tables are present."""
    pages_data = [
        {
            "page_number": 1,
            "prose": "Some prose text.",
            "tables": [[["Header1", "Header2"], ["val1", "val2"]]],
        }
    ]
    mock_pdfplumber = _make_mock_pdfplumber(pages_data)
    with patch.dict("sys.modules", {"pdfplumber": mock_pdfplumber}):
        result = extractor._extract_with_pdfplumber(dummy_pdf)

    assert result.metadata["extraction_method"] == "pdfplumber"
    assert "|" in result.text


def test_extract_with_pdfplumber_page_boundaries(extractor, dummy_pdf):
    """Page boundaries are contiguous and cover all pages with content."""
    pages_data = [
        {"page_number": 1, "prose": "Page one content.", "tables": []},
        {"page_number": 2, "prose": "Page two content.", "tables": []},
    ]
    mock_pdfplumber = _make_mock_pdfplumber(pages_data)
    with patch.dict("sys.modules", {"pdfplumber": mock_pdfplumber}):
        result = extractor._extract_with_pdfplumber(dummy_pdf)

    boundaries = result.metadata["page_boundaries"]
    assert len(boundaries) == 2
    assert boundaries[0]["start_char"] == 0
    # second boundary starts after first page text + 1 (join separator)
    first_len = boundaries[0]["page_text_length"]
    assert boundaries[1]["start_char"] == first_len


def test_extract_with_pdfplumber_none_cells(extractor, dummy_pdf):
    """None table cells are rendered as empty strings, not the word 'None'."""
    pages_data = [
        {
            "page_number": 1,
            "prose": "",
            "tables": [[[None, "B"], ["x", None]]],
        }
    ]
    mock_pdfplumber = _make_mock_pdfplumber(pages_data)
    with patch.dict("sys.modules", {"pdfplumber": mock_pdfplumber}):
        result = extractor._extract_with_pdfplumber(dummy_pdf)

    assert "None" not in result.text


# ── extract() pdfplumber rescue path ─────────────────────────────────────────

def test_extract_invokes_pdfplumber_rescue_when_tables_missing(extractor, dummy_pdf):
    """extract() calls _extract_with_pdfplumber when _markdown_misses_tables returns True."""
    md_result = _make_result("pymupdf4llm_markdown")
    plumber_result = _make_result("pdfplumber")

    with patch.object(extractor, "_has_type3_fonts", return_value=False):
        with patch.object(extractor, "_extract_markdown", return_value=md_result):
            with patch.object(extractor, "_markdown_misses_tables", return_value=True):
                with patch.object(extractor, "_extract_with_pdfplumber",
                                  return_value=plumber_result) as mock_plumber:
                    result = extractor.extract(dummy_pdf)

    mock_plumber.assert_called_once_with(dummy_pdf)
    assert result.metadata["extraction_method"] == "pdfplumber"


def test_extract_skips_pdfplumber_when_no_tables_missing(extractor, dummy_pdf):
    """extract() does NOT call pdfplumber when _markdown_misses_tables returns False."""
    md_result = _make_result("pymupdf4llm_markdown")

    with patch.object(extractor, "_has_type3_fonts", return_value=False):
        with patch.object(extractor, "_extract_markdown", return_value=md_result):
            with patch.object(extractor, "_markdown_misses_tables", return_value=False):
                with patch.object(extractor, "_extract_with_pdfplumber") as mock_plumber:
                    result = extractor.extract(dummy_pdf)

    mock_plumber.assert_not_called()
    assert result.metadata["extraction_method"] == "pymupdf4llm_markdown"


def test_extract_falls_back_to_markdown_if_pdfplumber_empty(extractor, dummy_pdf):
    """extract() returns markdown result if pdfplumber returns empty text."""
    from nexus.pdf_extractor import ExtractionResult
    md_result = _make_result("pymupdf4llm_markdown")
    empty_plumber = ExtractionResult(text="   ", metadata={"extraction_method": "pdfplumber"})

    with patch.object(extractor, "_has_type3_fonts", return_value=False):
        with patch.object(extractor, "_extract_markdown", return_value=md_result):
            with patch.object(extractor, "_markdown_misses_tables", return_value=True):
                with patch.object(extractor, "_extract_with_pdfplumber",
                                  return_value=empty_plumber):
                    result = extractor.extract(dummy_pdf)

    assert result.metadata["extraction_method"] == "pymupdf4llm_markdown"


def test_extract_falls_back_to_markdown_if_pdfplumber_raises(extractor, dummy_pdf):
    """extract() returns markdown result if pdfplumber raises an exception."""
    md_result = _make_result("pymupdf4llm_markdown")

    with patch.object(extractor, "_has_type3_fonts", return_value=False):
        with patch.object(extractor, "_extract_markdown", return_value=md_result):
            with patch.object(extractor, "_markdown_misses_tables", return_value=True):
                with patch.object(extractor, "_extract_with_pdfplumber",
                                  side_effect=Exception("pdfplumber crash")):
                    result = extractor.extract(dummy_pdf)

    assert result.metadata["extraction_method"] == "pymupdf4llm_markdown"
