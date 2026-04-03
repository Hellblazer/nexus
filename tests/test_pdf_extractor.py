"""RDR-021 + RDR-044: PDFExtractor — extraction backends and auto-detect routing."""
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
    mock_docling.assert_called_once_with(dummy_pdf, enriched=False)
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
    extractor._converter_enriched = mock_converter

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
    extractor._converter_enriched = mock_converter

    with patch.object(extractor, "_extract_title", return_value=""):
        result = extractor._extract_with_docling(dummy_pdf)

    boundaries = result.metadata["page_boundaries"]
    assert boundaries[0]["start_char"] == 0
    assert boundaries[1]["start_char"] == boundaries[0]["page_text_length"]


def test_extract_with_docling_raises_on_empty_output(extractor, dummy_pdf):
    """RuntimeError raised when Docling produces no text (triggers fallback in extract())."""
    mock_converter, mock_doc = _make_mock_docling(["", "", ""])
    extractor._converter_enriched = mock_converter

    with pytest.raises(RuntimeError, match="empty output"):
        extractor._extract_with_docling(dummy_pdf)


def test_extract_with_docling_stores_title(extractor, dummy_pdf):
    """docling_title is stored in metadata from _extract_title()."""
    mock_converter, mock_doc = _make_mock_docling(["# Content"])
    extractor._converter_enriched = mock_converter

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


def test_get_converter_enriched_caches_instance(extractor):
    """_get_converter(enriched=True) returns the same instance on repeated calls."""
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
        c1 = extractor._get_converter(enriched=True)
        c2 = extractor._get_converter(enriched=True)

    assert c1 is c2


def test_get_converter_enriched_and_fast_are_separate(extractor):
    """_get_converter(enriched=True) and _get_converter(enriched=False) return different instances."""
    mock_docling = MagicMock()
    mock_docling.PdfFormatOption = MagicMock()
    mock_docling.DocumentConverter.side_effect = [MagicMock(name="fast"), MagicMock(name="enriched")]

    mock_pipeline = MagicMock()
    mock_pipeline.PdfPipelineOptions.return_value = MagicMock()

    with patch.dict("sys.modules", {
        "docling": MagicMock(),
        "docling.document_converter": mock_docling,
        "docling.datamodel": MagicMock(),
        "docling.datamodel.pipeline_options": mock_pipeline,
    }):
        fast = extractor._get_converter(enriched=False)
        enriched = extractor._get_converter(enriched=True)

    assert fast is not enriched


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


# ── RDR-044 Phase 0: formula detection + enrichment ─────────────────────────

class TestGetConverterFormulaEnrichment:
    """_get_converter(enriched=...) controls do_formula_enrichment on PdfPipelineOptions."""

    def test_enriched_true_enables_formula_enrichment(self, extractor):
        """_get_converter(enriched=True) sets opts.do_formula_enrichment = True."""
        mock_opts = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.PdfPipelineOptions.return_value = mock_opts

        mock_docling = MagicMock()
        mock_docling.PdfFormatOption = MagicMock()

        with patch.dict("sys.modules", {
            "docling": MagicMock(),
            "docling.document_converter": mock_docling,
            "docling.datamodel": MagicMock(),
            "docling.datamodel.pipeline_options": mock_pipeline,
        }):
            extractor._get_converter(enriched=True)

        assert mock_opts.do_formula_enrichment is True

    def test_enriched_false_disables_formula_enrichment(self, extractor):
        """_get_converter(enriched=False) — default fast mode — sets do_formula_enrichment = False."""
        mock_opts = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.PdfPipelineOptions.return_value = mock_opts

        mock_docling = MagicMock()
        mock_docling.PdfFormatOption = MagicMock()

        with patch.dict("sys.modules", {
            "docling": MagicMock(),
            "docling.document_converter": mock_docling,
            "docling.datamodel": MagicMock(),
            "docling.datamodel.pipeline_options": mock_pipeline,
        }):
            extractor._get_converter(enriched=False)

        assert mock_opts.do_formula_enrichment is False

    def test_default_enriched_is_false(self, extractor):
        """_get_converter() without argument defaults to enriched=False."""
        mock_opts = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.PdfPipelineOptions.return_value = mock_opts

        mock_docling = MagicMock()
        mock_docling.PdfFormatOption = MagicMock()

        with patch.dict("sys.modules", {
            "docling": MagicMock(),
            "docling.document_converter": mock_docling,
            "docling.datamodel": MagicMock(),
            "docling.datamodel.pipeline_options": mock_pipeline,
        }):
            extractor._get_converter()

        assert mock_opts.do_formula_enrichment is False


class TestFormulaItemCounting:
    """_extract_with_docling counts FormulaItem objects in the iterate_items loop."""

    def _make_formula_item(self, page_no: int = 1) -> MagicMock:
        """Build a mock FormulaItem (duck-typed by __name__)."""
        item = MagicMock()
        type(item).__name__ = "FormulaItem"
        prov = MagicMock()
        prov.page_no = page_no
        item.prov = [prov]
        return item

    def _make_text_item(self, page_no: int = 1) -> MagicMock:
        """Build a mock TextItem (not a FormulaItem)."""
        item = MagicMock()
        type(item).__name__ = "TextItem"
        prov = MagicMock()
        prov.page_no = page_no
        item.prov = [prov]
        return item

    def test_formula_count_in_metadata(self, extractor, dummy_pdf):
        """formula_count in metadata reflects FormulaItem objects found during iteration."""
        formula1 = self._make_formula_item(page_no=1)
        formula2 = self._make_formula_item(page_no=2)
        text_item = self._make_text_item(page_no=1)

        mock_converter, mock_doc = _make_mock_docling(["# Page 1", "## Page 2"])
        mock_doc.iterate_items.return_value = iter([
            (formula1, None),
            (text_item, None),
            (formula2, None),
        ])
        extractor._converter_enriched = mock_converter

        with patch.object(extractor, "_extract_title", return_value=""):
            result = extractor._extract_with_docling(dummy_pdf)

        assert result.metadata["formula_count"] == 2

    def test_zero_formula_count_when_none_present(self, extractor, dummy_pdf):
        """formula_count is 0 when no FormulaItem objects exist."""
        text_item = self._make_text_item(page_no=1)

        mock_converter, mock_doc = _make_mock_docling(["# Content"])
        mock_doc.iterate_items.return_value = iter([
            (text_item, None),
        ])
        extractor._converter_enriched = mock_converter

        with patch.object(extractor, "_extract_title", return_value=""):
            result = extractor._extract_with_docling(dummy_pdf)

        assert result.metadata["formula_count"] == 0


class TestFormulaStructlogWarning:
    """structlog warning emitted when formulas are detected."""

    def test_warning_emitted_when_formulas_detected(self, extractor, dummy_pdf):
        """'formula_content_detected' warning logged when formula_count > 0."""
        formula = MagicMock()
        type(formula).__name__ = "FormulaItem"
        formula.prov = [MagicMock(page_no=1)]

        mock_converter, mock_doc = _make_mock_docling(["# Content"])
        mock_doc.iterate_items.return_value = iter([(formula, None)])
        extractor._converter_enriched = mock_converter

        with patch.object(extractor, "_extract_title", return_value=""):
            with patch("nexus.pdf_extractor._log") as mock_log:
                extractor._extract_with_docling(dummy_pdf)

        mock_log.warning.assert_any_call(
            "formula_content_detected",
            formula_count=1,
            path=str(dummy_pdf),
        )

    def test_no_warning_when_zero_formulas(self, extractor, dummy_pdf):
        """No warning logged when formula_count == 0."""
        text_item = MagicMock()
        type(text_item).__name__ = "TextItem"
        text_item.prov = [MagicMock(page_no=1)]

        mock_converter, mock_doc = _make_mock_docling(["# Content"])
        mock_doc.iterate_items.return_value = iter([(text_item, None)])
        extractor._converter_enriched = mock_converter

        with patch.object(extractor, "_extract_title", return_value=""):
            with patch("nexus.pdf_extractor._log") as mock_log:
                extractor._extract_with_docling(dummy_pdf)

        # No call with "formula_content_detected"
        for call in mock_log.warning.call_args_list:
            assert call[0][0] != "formula_content_detected"


class TestPdfChunksHasFormulas:
    """_pdf_chunks propagates has_formulas to chunk metadata."""

    def test_has_formulas_true_when_formula_count_positive(self):
        """Chunk metadata includes has_formulas=True when result has formula_count > 0."""
        from nexus.doc_indexer import _pdf_chunks

        result = ExtractionResult(
            text="Some math content here that is long enough to chunk.",
            metadata={
                "extraction_method": "docling",
                "page_count": 1,
                "format": "markdown",
                "page_boundaries": [{"page_number": 1, "start_char": 0, "page_text_length": 52}],
                "table_regions": [],
                "docling_title": "Math Paper",
                "pdf_title": "",
                "pdf_author": "",
                "pdf_subject": "",
                "pdf_keywords": "",
                "pdf_creator": "",
                "pdf_producer": "",
                "pdf_creation_date": "",
                "pdf_mod_date": "",
                "formula_count": 3,
            },
        )

        with patch("nexus.doc_indexer.PDFExtractor") as MockExtractor:
            MockExtractor.return_value.extract.return_value = result
            chunks = _pdf_chunks(
                Path("/fake/math.pdf"), "abc123", "voyage-context-3",
                "2026-01-01T00:00:00", "test",
            )

        assert len(chunks) > 0
        for _id, _text, meta in chunks:
            assert meta["has_formulas"] is True

    def test_has_formulas_false_when_formula_count_zero(self):
        """Chunk metadata includes has_formulas=False when formula_count == 0."""
        from nexus.doc_indexer import _pdf_chunks

        result = ExtractionResult(
            text="Plain text content that is long enough to produce at least one chunk.",
            metadata={
                "extraction_method": "docling",
                "page_count": 1,
                "format": "markdown",
                "page_boundaries": [{"page_number": 1, "start_char": 0, "page_text_length": 70}],
                "table_regions": [],
                "docling_title": "Plain Paper",
                "pdf_title": "",
                "pdf_author": "",
                "pdf_subject": "",
                "pdf_keywords": "",
                "pdf_creator": "",
                "pdf_producer": "",
                "pdf_creation_date": "",
                "pdf_mod_date": "",
                "formula_count": 0,
            },
        )

        with patch("nexus.doc_indexer.PDFExtractor") as MockExtractor:
            MockExtractor.return_value.extract.return_value = result
            chunks = _pdf_chunks(
                Path("/fake/plain.pdf"), "def456", "voyage-context-3",
                "2026-01-01T00:00:00", "test",
            )

        assert len(chunks) > 0
        for _id, _text, meta in chunks:
            assert meta["has_formulas"] is False

    def test_has_formulas_false_when_key_missing(self):
        """Chunk metadata defaults has_formulas=False when formula_count not in metadata."""
        from nexus.doc_indexer import _pdf_chunks

        result = ExtractionResult(
            text="Legacy content without formula detection metadata in the result.",
            metadata={
                "extraction_method": "pymupdf_normalized",
                "page_count": 1,
                "format": "normalized",
                "page_boundaries": [{"page_number": 1, "start_char": 0, "page_text_length": 63}],
                "table_regions": [],
                "docling_title": "",
                "pdf_title": "",
                "pdf_author": "",
                "pdf_subject": "",
                "pdf_keywords": "",
                "pdf_creator": "",
                "pdf_producer": "",
                "pdf_creation_date": "",
                "pdf_mod_date": "",
            },
        )

        with patch("nexus.doc_indexer.PDFExtractor") as MockExtractor:
            MockExtractor.return_value.extract.return_value = result
            chunks = _pdf_chunks(
                Path("/fake/old.pdf"), "ghi789", "voyage-context-3",
                "2026-01-01T00:00:00", "test",
            )

        assert len(chunks) > 0
        for _id, _text, meta in chunks:
            assert meta["has_formulas"] is False


# ── RDR-044 Phase 2: auto-detect routing ────────────────────────────────────


def _make_docling_result(formula_count: int = 0) -> ExtractionResult:
    """Build a mock Docling ExtractionResult with configurable formula_count."""
    return ExtractionResult(
        text="Docling extracted text.",
        metadata={
            "extraction_method": "docling",
            "page_count": 1,
            "format": "markdown",
            "formula_count": formula_count,
            "page_boundaries": [{"page_number": 1, "start_char": 0, "page_text_length": 23}],
            "table_regions": [],
            "docling_title": "",
            "pdf_title": "",
            "pdf_author": "",
            "pdf_subject": "",
            "pdf_keywords": "",
            "pdf_creator": "",
            "pdf_producer": "",
            "pdf_creation_date": "",
            "pdf_mod_date": "",
        },
    )


def _make_mineru_result() -> ExtractionResult:
    """Build a mock MinerU ExtractionResult."""
    return ExtractionResult(
        text="MinerU extracted text with math.",
        metadata={
            "extraction_method": "mineru",
            "page_count": 1,
            "format": "markdown",
            "formula_count": 5,
            "page_boundaries": [{"page_number": 1, "start_char": 0, "page_text_length": 31}],
            "table_regions": [],
            "docling_title": "",
            "pdf_title": "",
            "pdf_author": "",
            "pdf_subject": "",
            "pdf_keywords": "",
            "pdf_creator": "",
            "pdf_producer": "",
            "pdf_creation_date": "",
            "pdf_mod_date": "",
        },
    )


class TestAutoDetectRouting:
    """RDR-044 Phase 2: extract(extractor=...) routing logic.

    Tests the new extract(pdf_path, *, extractor="auto") signature.
    These tests will FAIL until nexus-gl4q implements the routing.
    """

    def test_auto_uses_docling_when_no_formulas(self, extractor, dummy_pdf):
        """extractor='auto' returns Docling result directly when formula_count==0."""
        docling_result = _make_docling_result(formula_count=0)

        with (
            patch.object(extractor, "_extract_with_docling", return_value=docling_result) as mock_docling,
            patch.object(extractor, "_extract_with_mineru") as mock_mineru,
        ):
            result = extractor.extract(dummy_pdf, extractor="auto")

        assert result.metadata["extraction_method"] == "docling"
        mock_mineru.assert_not_called()

    def test_auto_uses_mineru_when_formulas_detected(self, extractor, dummy_pdf):
        """extractor='auto' routes to MinerU when quick scan finds formulas."""
        docling_result = _make_docling_result(formula_count=10)
        mineru_result = _make_mineru_result()

        with (
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=10),
            patch.object(extractor, "_extract_with_docling", return_value=docling_result),
            patch.object(extractor, "_extract_with_mineru", return_value=mineru_result) as mock_mineru,
        ):
            result = extractor.extract(dummy_pdf, extractor="auto")

        assert result.metadata["extraction_method"] == "mineru"
        mock_mineru.assert_called_once_with(dummy_pdf, formula_count=10)

    def test_auto_falls_back_to_fast_result_when_mineru_fails(self, extractor, dummy_pdf):
        """extractor='auto' returns the initial Docling result when MinerU raises."""
        fast_result = _make_docling_result(formula_count=10)

        with (
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=10),
            patch.object(extractor, "_extract_with_docling", return_value=fast_result),
            patch.object(
                extractor, "_extract_with_mineru",
                side_effect=RuntimeError("MinerU model download failed"),
            ),
            patch("nexus.pdf_extractor._log") as mock_log,
        ):
            result = extractor.extract(dummy_pdf, extractor="auto")

        assert result is fast_result
        mock_log.debug.assert_any_call(
            "mineru_extraction_failed",
            error="MinerU model download failed",
            path=str(dummy_pdf),
        )

    def test_forced_docling_skips_mineru(self, extractor, dummy_pdf):
        """extractor='docling' always uses Docling, never calls MinerU."""
        docling_result = _make_docling_result(formula_count=10)

        with (
            patch.object(extractor, "_extract_with_docling", return_value=docling_result),
            patch.object(extractor, "_extract_with_mineru") as mock_mineru,
        ):
            result = extractor.extract(dummy_pdf, extractor="docling")

        assert result.metadata["extraction_method"] == "docling"
        mock_mineru.assert_not_called()

    def test_forced_mineru_skips_docling(self, extractor, dummy_pdf):
        """extractor='mineru' calls MinerU directly, no Docling fast pass."""
        mineru_result = _make_mineru_result()

        with (
            patch.object(extractor, "_extract_with_docling") as mock_docling,
            patch.object(extractor, "_extract_with_mineru", return_value=mineru_result),
        ):
            result = extractor.extract(dummy_pdf, extractor="mineru")

        assert result.metadata["extraction_method"] == "mineru"
        mock_docling.assert_not_called()

    def test_invalid_extractor_raises_value_error(self, extractor, dummy_pdf):
        """Unknown extractor value raises ValueError."""
        with pytest.raises(ValueError, match="extractor"):
            extractor.extract(dummy_pdf, extractor="invalid")
