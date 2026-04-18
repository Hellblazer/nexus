# SPDX-License-Identifier: AGPL-3.0-or-later
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
    mock_doc = MagicMock()
    mock_doc.num_pages.return_value = len(pages)
    mock_doc.export_to_markdown.side_effect = pages

    mock_result = MagicMock()
    mock_result.document = mock_doc

    mock_converter = MagicMock()
    mock_converter.convert.return_value = mock_result
    return mock_converter, mock_doc


def _make_item(text: str, label: str, page_no: int) -> MagicMock:
    prov = MagicMock()
    prov.page_no = page_no
    item = MagicMock()
    item.text = text
    item.label = label
    item.prov = [prov]
    return item


def _make_typed_item(type_name: str, page_no: int = 1) -> MagicMock:
    item = MagicMock()
    type(item).__name__ = type_name
    item.prov = [MagicMock(page_no=page_no)]
    return item


def _full_metadata(*, method="docling", formula_count=0, text_len=23, **overrides):
    base = {
        "extraction_method": method,
        "page_count": 1,
        "format": "markdown",
        "formula_count": formula_count,
        "page_boundaries": [{"page_number": 1, "start_char": 0, "page_text_length": text_len}],
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
    }
    base.update(overrides)
    return base


def _mock_pymupdf_doc(pages_text: list[str], metadata: dict | None = None):
    mock_pages = []
    for text in pages_text:
        p = MagicMock()
        p.get_text.return_value = text
        mock_pages.append(p)
    mock_doc = MagicMock()
    mock_doc.__enter__ = MagicMock(return_value=mock_doc)
    mock_doc.__exit__ = MagicMock(return_value=False)
    mock_doc.__iter__ = MagicMock(return_value=iter(mock_pages))
    mock_doc.__len__ = MagicMock(return_value=len(pages_text))
    mock_doc.metadata = metadata or {}
    mock_pymupdf = MagicMock()
    mock_pymupdf.open.return_value = mock_doc
    return mock_pymupdf


@pytest.fixture
def docling_sys_modules():
    mock_opts = MagicMock()
    mock_pipeline = MagicMock()
    mock_pipeline.PdfPipelineOptions.return_value = mock_opts
    mock_docling = MagicMock()
    mock_docling.PdfFormatOption = MagicMock()
    modules = {
        "docling": MagicMock(),
        "docling.document_converter": mock_docling,
        "docling.datamodel": MagicMock(),
        "docling.datamodel.pipeline_options": mock_pipeline,
    }
    return modules, mock_opts, mock_docling


# ── extract() primary path ──────────────────────────────────────────────────


def test_extract_uses_docling_by_default(extractor, dummy_pdf):
    expected = _make_result("docling")
    with patch.object(extractor, "_extract_with_docling", return_value=expected) as mock_docling:
        result = extractor.extract(dummy_pdf)
    mock_docling.assert_called_once_with(dummy_pdf, enriched=False)
    assert result.metadata["extraction_method"] == "docling"


def test_extract_falls_back_on_docling_exception(extractor, dummy_pdf):
    expected = _make_result("pymupdf_normalized")
    with patch.object(extractor, "_extract_with_docling", side_effect=RuntimeError("crash")):
        with patch.object(extractor, "_extract_normalized", return_value=expected) as mock_norm:
            result = extractor.extract(dummy_pdf)
    mock_norm.assert_called_once_with(dummy_pdf, on_page=None)
    assert result.metadata["extraction_method"] == "pymupdf_normalized"


# ── _extract_with_docling — page boundaries ─────────────────────────────────


def test_extract_with_docling_page_boundaries(extractor, dummy_pdf):
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
    mock_converter, mock_doc = _make_mock_docling(["", "", ""])
    extractor._converter_enriched = mock_converter
    with pytest.raises(RuntimeError, match="empty output"):
        extractor._extract_with_docling(dummy_pdf)


def test_extract_with_docling_stores_title(extractor, dummy_pdf):
    mock_converter, mock_doc = _make_mock_docling(["# Content"])
    extractor._converter_enriched = mock_converter

    with patch.object(extractor, "_extract_title", return_value="My Paper"):
        result = extractor._extract_with_docling(dummy_pdf)
    assert result.metadata["docling_title"] == "My Paper"


# ── _extract_title ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("items, iterate_side_effect, expected", [
    pytest.param(
        [("Some text block", "text", 1), ("My Great Paper", "title", 1), ("Other", "section_header", 1)],
        None, "My Great Paper",
        id="first_title_label",
    ),
    pytest.param(
        [("Abstract", "section_header", 1), ("My Real Title", "title", 1)],
        None, "My Real Title",
        id="skips_abstract_keyword",
    ),
    pytest.param(
        [("Page 2 Title", "title", 2), ("Short Title Here", "title", 1)],
        None, "Short Title Here",
        id="skips_page2_items",
    ),
    pytest.param(
        [("Short title text", "text", 1)],
        "two_passes", "Short title text",
        id="fallback_short_text",
    ),
    pytest.param([], None, "", id="empty_returns_empty"),
])
def test_extract_title(extractor, items, iterate_side_effect, expected):
    doc = MagicMock()
    mock_items = [(_make_item(t, l, p), None) for t, l, p in items]
    if iterate_side_effect == "two_passes":
        doc.iterate_items.side_effect = [iter(mock_items), iter(mock_items)]
    else:
        doc.iterate_items.return_value = iter(mock_items)
    assert extractor._extract_title(doc) == expected


# ── _extract_normalized (fallback) ──────────────────────────────────────────


def test_extract_normalized_tracks_page_boundaries(extractor, dummy_pdf):
    mock_pymupdf = _mock_pymupdf_doc(["Page one text.", ""])
    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._extract_normalized(dummy_pdf)
    boundaries = result.metadata["page_boundaries"]
    assert len(boundaries) == 1
    assert boundaries[0]["page_number"] == 1
    assert result.metadata["extraction_method"] == "pymupdf_normalized"


def test_extract_normalized_includes_docling_title_empty(extractor, dummy_pdf):
    mock_pymupdf = _mock_pymupdf_doc(["Some text here."], metadata={"title": "PDF Meta Title"})
    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._extract_normalized(dummy_pdf)
    assert result.metadata["docling_title"] == ""
    assert result.metadata["pdf_title"] == "PDF Meta Title"


# ── _get_converter lazy init ────────────────────────────────────────────────


def test_get_converter_is_lazy(extractor):
    assert extractor._converter is None


@pytest.mark.parametrize("enriched", [False, True], ids=["fast", "enriched"])
def test_get_converter_caches_instance(extractor, docling_sys_modules, enriched):
    modules, _, _ = docling_sys_modules
    with patch.dict("sys.modules", modules):
        c1 = extractor._get_converter(enriched=enriched)
        c2 = extractor._get_converter(enriched=enriched)
    assert c1 is c2


def test_get_converter_enriched_and_fast_are_separate(extractor, docling_sys_modules):
    modules, _, mock_docling = docling_sys_modules
    mock_docling.DocumentConverter.side_effect = [MagicMock(name="fast"), MagicMock(name="enriched")]
    with patch.dict("sys.modules", modules):
        fast = extractor._get_converter(enriched=False)
        enriched = extractor._get_converter(enriched=True)
    assert fast is not enriched


# ── _normalize_whitespace_edge_cases ────────────────────────────────────────


@pytest.mark.parametrize("input_text, expected", [
    ("hello\tworld", "hello world"),
    ("word\u00a0\u00a0another", "word another"),
    ("a\n\n\n\n\nb", "a\n\n\nb"),
    ("a\n\n\nb", "a\n\n\nb"),
], ids=["tab", "nbsp", "excess_newlines", "three_newlines_preserved"])
def test_normalize_whitespace(input_text, expected):
    assert _normalize_whitespace_edge_cases(input_text) == expected


# ── RDR-044 Phase 0: formula detection + enrichment ────────────────────────


@pytest.mark.parametrize("enriched, expected_flag", [
    (True, True),
    (False, False),
    (None, False),
], ids=["enriched_true", "enriched_false", "default_none"])
def test_get_converter_formula_enrichment(extractor, docling_sys_modules, enriched, expected_flag):
    modules, mock_opts, _ = docling_sys_modules
    with patch.dict("sys.modules", modules):
        if enriched is None:
            extractor._get_converter()
        else:
            extractor._get_converter(enriched=enriched)
    assert mock_opts.do_formula_enrichment is expected_flag


@pytest.mark.parametrize("items, expected_count", [
    ([("FormulaItem", 1), ("TextItem", 1), ("FormulaItem", 2)], 2),
    ([("TextItem", 1)], 0),
], ids=["two_formulas", "zero_formulas"])
def test_formula_count_in_metadata(extractor, dummy_pdf, items, expected_count):
    pages = ["# Page 1"] + (["## Page 2"] if any(p > 1 for _, p in items) else [])
    mock_converter, mock_doc = _make_mock_docling(pages)
    mock_doc.iterate_items.return_value = iter([
        (_make_typed_item(name, page), None) for name, page in items
    ])
    extractor._converter_enriched = mock_converter

    with patch.object(extractor, "_extract_title", return_value=""):
        result = extractor._extract_with_docling(dummy_pdf)
    assert result.metadata["formula_count"] == expected_count


@pytest.mark.parametrize("item_type, expect_warning", [
    ("FormulaItem", True),
    ("TextItem", False),
], ids=["warning_emitted", "no_warning"])
def test_formula_structlog_warning(extractor, dummy_pdf, item_type, expect_warning):
    mock_converter, mock_doc = _make_mock_docling(["# Content"])
    mock_doc.iterate_items.return_value = iter([(_make_typed_item(item_type), None)])
    extractor._converter_enriched = mock_converter

    with patch.object(extractor, "_extract_title", return_value=""):
        with patch("nexus.pdf_extractor._log") as mock_log:
            extractor._extract_with_docling(dummy_pdf)

    if expect_warning:
        mock_log.warning.assert_any_call(
            "formula_content_detected", formula_count=1, path=str(dummy_pdf),
        )
    else:
        for call in mock_log.warning.call_args_list:
            assert call[0][0] != "formula_content_detected"


# ── _pdf_chunks has_formulas propagation ────────────────────────────────────


@pytest.mark.parametrize("formula_count, drop_key, expected", [
    (3, False, True), (0, False, False), (0, True, False),
], ids=["positive_count", "zero_count", "missing_key"])
def test_pdf_chunks_has_formulas(formula_count, drop_key, expected):
    from nexus.doc_indexer import _pdf_chunks
    text = "Content long enough for at least one chunk to be produced by the splitter."
    meta = _full_metadata(formula_count=formula_count, text_len=len(text))
    if drop_key:
        del meta["formula_count"]
    result = ExtractionResult(text=text, metadata=meta)
    with patch("nexus.doc_indexer.PDFExtractor") as M:
        M.return_value.extract.return_value = result
        chunks = _pdf_chunks(Path("/fake/t.pdf"), "x", "voyage-context-3", "2026-01-01T00:00:00", "test")
    assert len(chunks) > 0
    for _id, _text, m in chunks:
        assert m["has_formulas"] is expected


# ── RDR-044 Phase 2: auto-detect routing ───────────────────────────────────


class TestAutoDetectRouting:

    _docling = ExtractionResult(text="Docling text.", metadata=_full_metadata(formula_count=0))
    _docling_f = ExtractionResult(text="Docling text.", metadata=_full_metadata(formula_count=10))
    _mineru = ExtractionResult(text="MinerU text.", metadata=_full_metadata(method="mineru", formula_count=5, text_len=12))

    def test_auto_uses_docling_when_no_formulas(self, extractor, dummy_pdf):
        with (
            patch.object(extractor, "_extract_with_docling", return_value=self._docling),
            patch.object(extractor, "_extract_with_mineru") as mock_mineru,
        ):
            result = extractor.extract(dummy_pdf, extractor="auto")
        assert result.metadata["extraction_method"] == "docling"
        mock_mineru.assert_not_called()

    def test_auto_uses_mineru_when_formulas_detected(self, extractor, dummy_pdf):
        with (
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=10),
            patch.object(extractor, "_extract_with_docling", return_value=self._docling_f),
            patch.object(extractor, "_extract_with_mineru", return_value=self._mineru) as m,
        ):
            result = extractor.extract(dummy_pdf, extractor="auto")
        assert result.metadata["extraction_method"] == "mineru"
        m.assert_called_once_with(dummy_pdf, formula_count=10, on_page=None)

    def test_auto_falls_back_when_mineru_fails(self, extractor, dummy_pdf):
        with (
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=10),
            patch.object(extractor, "_extract_with_docling", return_value=self._docling_f),
            patch.object(extractor, "_extract_with_mineru", side_effect=RuntimeError("download failed")),
            patch("nexus.pdf_extractor._log") as mock_log,
        ):
            result = extractor.extract(dummy_pdf, extractor="auto")
        assert result is self._docling_f
        mock_log.debug.assert_any_call(
            "mineru_extraction_failed", error="download failed", path=str(dummy_pdf),
        )

    def test_auto_fallback_replays_on_page_after_mineru_fails(self, extractor, dummy_pdf):
        """nexus-7ne1 regression: the MinerU-failed fallback must replay on_page
        from fast_result.page_boundaries, mirroring the formula_count < 5 happy
        path. Without this replay, the streaming pipeline never sees pages and
        the chunker emits 0 chunks for the entire document — the bug that
        masqueraded as MinerU brokenness during the 2026-04-17 Delos re-index.
        """
        # Three pages joined with "\n" → text = "Page A\nPage B\nPage C" (20 chars).
        # page_text_length includes the +1 for the joining "\n" except the final page.
        page_a = "Page A"   # 6 chars, boundary length = 7 (+1 for \n)
        page_b = "Page B"   # 6 chars, boundary length = 7
        page_c = "Page C"   # 6 chars, boundary length = 7 (final, but convention preserves +1)
        full_text = "\n".join([page_a, page_b, page_c])
        fast_result = ExtractionResult(
            text=full_text,
            metadata={
                "extraction_method": "docling",
                "page_count": 3,
                "page_boundaries": [
                    {"page_number": 1, "start_char": 0,  "page_text_length": 7},
                    {"page_number": 2, "start_char": 7,  "page_text_length": 7},
                    {"page_number": 3, "start_char": 14, "page_text_length": 7},
                ],
                "format": "markdown",
                "formula_count": 10,
            },
        )
        received: list[tuple] = []
        def _on_page(page_idx: int, page_text: str, page_meta: dict) -> None:
            received.append((page_idx, page_text, page_meta))

        with (
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=10),
            patch.object(extractor, "_extract_with_docling", return_value=fast_result),
            patch.object(extractor, "_extract_with_mineru", side_effect=RuntimeError("MinerU 409")),
        ):
            result = extractor.extract(dummy_pdf, extractor="auto", on_page=_on_page)

        # Returned the fast_result (existing contract — preserved).
        assert result is fast_result
        # AND now the on_page callback fired once per page, with the
        # right text for each page (the pre-fix behavior produced 0 callbacks).
        assert len(received) == 3, f"expected 3 callbacks, got {len(received)}"
        # 0-indexed page positions per RDR-048 callback contract.
        assert [r[0] for r in received] == [0, 1, 2]
        assert [r[1] for r in received] == [page_a, page_b, page_c]
        # Page metadata uses 1-based page_number.
        assert [r[2]["page_number"] for r in received] == [1, 2, 3]
        # text_length matches the page text (length-1 from page_text_length).
        assert [r[2]["text_length"] for r in received] == [6, 6, 6]

    def test_auto_fallback_no_callback_when_on_page_is_none(self, extractor, dummy_pdf):
        """nexus-7ne1: ensure the replay loop is gated on on_page being non-None,
        so callers that don't supply a callback still get the existing fast_result
        return without errors.
        """
        with (
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=10),
            patch.object(extractor, "_extract_with_docling", return_value=self._docling_f),
            patch.object(extractor, "_extract_with_mineru", side_effect=RuntimeError("MinerU 409")),
        ):
            result = extractor.extract(dummy_pdf, extractor="auto", on_page=None)
        assert result is self._docling_f  # contract preserved when no callback

    def test_forced_docling_skips_mineru(self, extractor, dummy_pdf):
        with (
            patch.object(extractor, "_extract_with_docling", return_value=self._docling_f),
            patch.object(extractor, "_extract_with_mineru") as mock_mineru,
        ):
            result = extractor.extract(dummy_pdf, extractor="docling")
        assert result.metadata["extraction_method"] == "docling"
        mock_mineru.assert_not_called()

    def test_forced_mineru_skips_docling(self, extractor, dummy_pdf):
        with (
            patch.object(extractor, "_extract_with_docling") as mock_docling,
            patch.object(extractor, "_extract_with_mineru", return_value=self._mineru),
        ):
            result = extractor.extract(dummy_pdf, extractor="mineru")
        assert result.metadata["extraction_method"] == "mineru"
        mock_docling.assert_not_called()

    def test_invalid_extractor_raises_value_error(self, extractor, dummy_pdf):
        with pytest.raises(ValueError, match="extractor"):
            extractor.extract(dummy_pdf, extractor="invalid")


# ── on_page callback (RDR-048) ──────────────────────────────────────────────


class TestOnPageCallbackDocling:

    def test_callback_receives_pages_in_order(self, extractor, dummy_pdf):
        pages = ["# Page One", "## Page Two", "### Page Three"]
        mock_converter, _ = _make_mock_docling(pages)
        extractor._converter_enriched = mock_converter
        received: list[tuple] = []

        with patch.object(extractor, "_extract_title", return_value=""):
            extractor._extract_with_docling(
                dummy_pdf, on_page=lambda idx, text, meta: received.append((idx, text, meta)),
            )

        assert len(received) == 3
        assert [r[0] for r in received] == [0, 1, 2]
        assert received[0][1] == "# Page One"

    def test_callback_metadata_has_page_number_and_length(self, extractor, dummy_pdf):
        mock_converter, _ = _make_mock_docling(["Hello world"])
        extractor._converter_enriched = mock_converter
        received: list[dict] = []

        with patch.object(extractor, "_extract_title", return_value=""):
            extractor._extract_with_docling(
                dummy_pdf, on_page=lambda idx, text, meta: received.append(meta),
            )
        assert received[0]["page_number"] == 1
        assert received[0]["text_length"] == len("Hello world")

    def test_callback_skips_empty_pages(self, extractor, dummy_pdf):
        mock_converter, _ = _make_mock_docling(["# Page 1", "", "# Page 3"])
        extractor._converter_enriched = mock_converter
        received: list[int] = []

        with patch.object(extractor, "_extract_title", return_value=""):
            extractor._extract_with_docling(
                dummy_pdf, on_page=lambda idx, text, meta: received.append(idx),
            )
        assert received == [0, 2]

    @pytest.mark.parametrize("on_page", [None, lambda *a: None], ids=["none", "noop"])
    def test_result_complete(self, extractor, dummy_pdf, on_page):
        mock_converter, _ = _make_mock_docling(["# Page 1", "## Page 2"])
        extractor._converter_enriched = mock_converter

        with patch.object(extractor, "_extract_title", return_value="Title"):
            result = extractor._extract_with_docling(dummy_pdf, on_page=on_page)

        assert result.text == "# Page 1\n## Page 2"
        assert len(result.metadata["page_boundaries"]) == 2
        assert result.metadata["extraction_method"] == "docling"


class TestOnPageCallbackPyMuPDF:

    def test_callback_receives_pages_in_order(self, extractor, dummy_pdf):
        mock_pymupdf = _mock_pymupdf_doc(["Page zero text", "Page one text"])
        received: list[tuple] = []

        with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
            extractor._extract_normalized(
                dummy_pdf, on_page=lambda idx, text, meta: received.append((idx, text, meta)),
            )

        assert len(received) == 2
        assert received[0][0] == 0
        assert "Page zero text" in received[0][1]
        assert received[0][2]["page_number"] == 1
        assert received[1][2]["page_number"] == 2

    def test_result_complete_with_callback(self, extractor, dummy_pdf):
        mock_pymupdf = _mock_pymupdf_doc(["Some content"])

        with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
            result = extractor._extract_normalized(dummy_pdf, on_page=lambda *a: None)

        assert result.text
        assert result.metadata["page_boundaries"]


class TestOnPageCallbackExtract:

    def test_extract_passes_on_page_to_docling(self, extractor, dummy_pdf):
        expected = _make_result("docling")
        cb = lambda *a: None
        with patch.object(extractor, "_extract_with_docling", return_value=expected) as mock_d:
            extractor.extract(dummy_pdf, extractor="docling", on_page=cb)
        mock_d.assert_called_once_with(dummy_pdf, on_page=cb)

    def test_extract_passes_on_page_to_normalized_fallback(self, extractor, dummy_pdf):
        expected = _make_result("pymupdf_normalized")
        cb = lambda *a: None
        with patch.object(extractor, "_extract_with_docling", side_effect=RuntimeError("fail")):
            with patch.object(extractor, "_extract_normalized", return_value=expected) as mock_n:
                extractor.extract(dummy_pdf, extractor="docling", on_page=cb)
        mock_n.assert_called_once_with(dummy_pdf, on_page=cb)

    def test_extract_auto_replays_on_page_for_docling_win(self, extractor, dummy_pdf):
        pages = ["# Page One", "## Page Two"]
        mock_converter, _ = _make_mock_docling(pages)
        extractor._converter = mock_converter
        received: list[tuple] = []

        with (
            patch("nexus.pdf_extractor._has_formulas_quick", return_value=0),
            patch.object(extractor, "_extract_title", return_value=""),
        ):
            extractor.extract(
                dummy_pdf, on_page=lambda idx, text, meta: received.append((idx, text)),
            )

        assert len(received) == 2
        assert received[0][0] == 0
        assert received[1][0] == 1
