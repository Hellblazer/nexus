# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDFExtractor: backend routing (MinerU by default, PyMuPDF on request), the PyMuPDF paths,
and the failure messages (RDR-021, RDR-044, RDR-237 Phase 1 Step 3)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.errors import UnextractableContentError
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


def _full_metadata(*, method="mineru", formula_count=0, text_len=23, **overrides):
    base = {
        "extraction_method": method,
        "page_count": 1,
        "format": "markdown",
        "formula_count": formula_count,
        "page_boundaries": [{"page_number": 1, "start_char": 0, "page_text_length": text_len}],
        "table_regions": [],
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


# ── _extract_normalized (whole-document PyMuPDF read) ───────────────────────


def test_extract_normalized_tracks_page_boundaries(extractor, dummy_pdf):
    mock_pymupdf = _mock_pymupdf_doc(["Page one text.", ""])
    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._extract_normalized(dummy_pdf)
    boundaries = result.metadata["page_boundaries"]
    assert len(boundaries) == 1
    assert boundaries[0]["page_number"] == 1
    assert result.metadata["extraction_method"] == "pymupdf_normalized"


def test_extract_normalized_reads_the_pdf_title(extractor, dummy_pdf):
    """RDR-237: the title chain starts at ``pdf_title`` (XMP)."""
    mock_pymupdf = _mock_pymupdf_doc(["Some text here."], metadata={"title": "PDF Meta Title"})
    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        result = extractor._extract_normalized(dummy_pdf)
    assert result.metadata["pdf_title"] == "PDF Meta Title"


def test_extract_normalized_raises_on_empty_output(extractor, dummy_pdf):
    """nexus-aold: PyMuPDF fallback used to silently return an
    ExtractionResult with text='' when the PDF was image-only or
    extraction failed page-by-page. Downstream the empty text
    produced 0 chunks and the indexer reported success with no
    chunks indexed (a silent failure mode). Empty output is a hard
    error -- LOUD, per file
    (nexus-aold's original intent), but nexus-deyd5 reclassified it from a
    bare RuntimeError to UnextractableContentError so run_file_loop treats
    it as a per-file skip rather than aborting the whole run."""
    mock_pymupdf = _mock_pymupdf_doc(["", "", ""])
    with patch.dict("sys.modules", {"pymupdf": mock_pymupdf}):
        with pytest.raises(UnextractableContentError, match="empty"):
            extractor._extract_normalized(dummy_pdf)


@pytest.mark.parametrize("input_text, expected", [
    ("hello\tworld", "hello world"),
    ("word  another", "word another"),
    ("a\n\n\n\n\nb", "a\n\n\nb"),
    ("a\n\n\nb", "a\n\n\nb"),
], ids=["tab", "nbsp", "excess_newlines", "three_newlines_preserved"])
def test_normalize_whitespace(input_text, expected):
    assert _normalize_whitespace_edge_cases(input_text) == expected


# ── _pdf_chunks has_formulas propagation ────────────────────────────────────


@pytest.mark.parametrize("formula_count, drop_key", [
    (3, False), (0, False), (0, True),
], ids=["positive_count", "zero_count", "missing_key"])
def test_pdf_chunks_extracts_without_error_on_any_formula_count(formula_count, drop_key):
    """``has_formulas`` is consumed pre-store but is NOT in
    ``ALLOWED_TOP_LEVEL`` — ``normalize()`` drops it before T3 storage,
    so it's intentionally absent from chunk metadata. This test
    verifies the indexer survives all ``formula_count`` shapes without
    asserting on the dropped field."""
    from nexus.doc_indexer import _pdf_chunks
    text = "Content long enough for at least one chunk to be produced by the splitter."
    meta = _full_metadata(formula_count=formula_count, text_len=len(text))
    if drop_key:
        del meta["formula_count"]
    result = ExtractionResult(text=text, metadata=meta)
    with patch("nexus.doc_indexer.PDFExtractor") as M:
        M.return_value.extract.return_value = result
        # Neutral model token on purpose (RDR-109 mode lint): _pdf_chunks
        # is a pure chunking function -- the model string is opaque metadata,
        # never a dispatch predicate; no embedder is constructed.
        chunks = _pdf_chunks(Path("/fake/t.pdf"), "x", "model-ctx", "2026-01-01T00:00:00", "test")
    assert len(chunks) > 0
    for _id, _text, m in chunks:
        assert "has_formulas" not in m


# ── RDR-237: MinerU is the one layout-aware extractor ──────────────────────


class TestMineruIsTheDefault:

    _mineru = ExtractionResult(
        text="MinerU text.", metadata=_full_metadata(method="mineru", formula_count=5, text_len=12),
    )

    def test_extract_with_no_choice_runs_mineru_and_nothing_else(self, extractor, dummy_pdf):
        with (
            patch.object(extractor, "_extract_with_mineru", return_value=self._mineru) as mineru,
            patch.object(extractor, "_extract_pymupdf") as pymupdf,
            patch.object(extractor, "_extract_normalized") as normalized,
        ):
            result = extractor.extract(dummy_pdf)
        assert result.metadata["extraction_method"] == "mineru"
        mineru.assert_called_once()
        assert mineru.call_args.args == (dummy_pdf,)
        assert set(mineru.call_args.kwargs) == {"on_page"}  # no formula count, no formula-OOM choice
        assert callable(mineru.call_args.kwargs["on_page"])  # nexus-i0cwh: the pages_with_text collector
        pymupdf.assert_not_called()
        normalized.assert_not_called()

    def test_the_quick_formula_screen_is_gone(self):
        import nexus.pdf_extractor as module

        for name in ("_has_formulas_quick", "_count_formula_markers", "_FORMULA_ROUTE_THRESHOLD"):
            assert not hasattr(module, name), f"{name} belonged to the removed auto routing"

    def test_pymupdf_runs_the_text_only_extractor_and_never_mineru(self, extractor, dummy_pdf):
        with (
            patch.object(extractor, "_extract_with_mineru") as mineru,
            patch.object(extractor, "_extract_pymupdf", return_value=_make_result("pymupdf")) as pymupdf,
        ):
            result = extractor.extract(dummy_pdf, extractor="pymupdf")
        assert result.metadata["extraction_method"] == "pymupdf"
        pymupdf.assert_called_once()
        mineru.assert_not_called()

    def test_the_older_name_auto_runs_mineru(self, extractor, dummy_pdf):
        with patch.object(extractor, "_extract_with_mineru", return_value=self._mineru) as mineru:
            result = extractor.extract(dummy_pdf, extractor="auto")
        assert result.metadata["extraction_method"] == "mineru"
        mineru.assert_called_once()

    def test_an_unknown_extractor_is_refused(self, extractor, dummy_pdf):
        with pytest.raises(ValueError, match="extractor"):
            extractor.extract(dummy_pdf, extractor="invalid")

    def test_on_formula_oom_is_no_longer_a_parameter(self, extractor, dummy_pdf):
        with pytest.raises(TypeError):
            extractor.extract(dummy_pdf, on_formula_oom="fail")  # type: ignore[call-arg]

    def test_a_mineru_failure_is_not_answered_with_another_extractor(self, extractor, dummy_pdf):
        """nexus-2fyb: no plainer extractor runs behind the user's back; the PDF fails by name."""
        with (
            patch.object(extractor, "_extract_with_mineru", side_effect=RuntimeError("MinerU 409")),
            patch.object(extractor, "_extract_normalized") as normalized,
            patch.object(extractor, "_extract_pymupdf") as pymupdf,
        ):
            with pytest.raises(RuntimeError, match="MinerU extraction failed"):
                extractor.extract(dummy_pdf)
        normalized.assert_not_called()
        pymupdf.assert_not_called()

    def test_a_missing_import_on_an_installed_stack_names_the_reinstall_and_the_text_only_way(
        self, extractor, dummy_pdf,
    ):
        """nexus-2fyb: ImportError branch. MinerU ships with the pdf extra, so with the stack present a
        failed import means a corrupt install: advise reinstalling conexus, not a non-existent
        ``[mineru]`` extra, and name the text-only extractor for a user who cannot repair it now."""
        with patch.object(extractor, "_extract_with_mineru", side_effect=ImportError("No module named 'mineru'")):
            with patch("nexus.pdf_stack.missing_message", return_value=None):
                with pytest.raises(RuntimeError) as excinfo:
                    extractor.extract(dummy_pdf)
        msg = str(excinfo.value)
        assert "uv tool install --reinstall conexus" in msg
        assert "--extractor pymupdf" in msg
        assert "formulas" not in msg  # the removed screen's wording
        assert isinstance(excinfo.value.__cause__, ImportError)

    def test_a_missing_stack_is_reported_with_the_stack_message(self, extractor, dummy_pdf):
        with patch.object(extractor, "_extract_with_mineru", side_effect=ImportError("No module named 'mineru'")):
            with patch("nexus.pdf_stack.missing_message", return_value="PDF support is not installed: run X"):
                with pytest.raises(RuntimeError) as excinfo:
                    extractor.extract(dummy_pdf)
        assert "PDF support is not installed: run X" in str(excinfo.value)

    def test_the_reinstall_hint_follows_the_layout(self, extractor, dummy_pdf, tmp_path, monkeypatch):
        """nexus-utpuw.13. A DISTINCT call site from the one in test_mineru_extractor.py — same helper,
        separate wiring, so it needs its own falsifier or a mutation that unwires just this one passes.
        conftest fences $HOME, so the test above only sees the legacy branch."""
        from tests import _generation_layout

        _generation_layout.build(tmp_path, monkeypatch)
        with patch.object(extractor, "_extract_with_mineru", side_effect=ImportError("No module named 'mineru'")):
            with patch("nexus.pdf_stack.missing_message", return_value=None):
                with pytest.raises(RuntimeError) as excinfo:
                    extractor.extract(dummy_pdf)
        msg = str(excinfo.value)
        assert "nx self install" in msg
        assert "uv tool" not in msg

    def test_a_runtime_failure_does_not_advise_reinstalling_a_healthy_install(self, extractor, dummy_pdf):
        """nexus-2fyb code-review C1: non-import failures (subprocess timeout, OOM kill, mineru-api 5xx,
        httpx errors) MUST NOT advise reinstalling — the install is fine and the failure is operational."""
        with patch.object(
            extractor, "_extract_with_mineru",
            side_effect=RuntimeError("MinerU subprocess timed out after 600s"),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                extractor.extract(dummy_pdf)
        msg = str(excinfo.value)
        assert "MinerU extraction failed" in msg  # operational, not install
        assert "subprocess timed out" in msg  # underlying cause surfaced
        assert "reinstall" not in msg.lower()
        assert "nx doctor" in msg
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_a_failure_does_not_replay_pages_to_the_callback(self, extractor, dummy_pdf):
        """nexus-2fyb: MinerU failing aborts cleanly; no page is delivered from another extractor."""
        received: list[tuple] = []

        def _on_page(page_idx: int, page_text: str, page_meta: dict) -> None:
            received.append((page_idx, page_text, page_meta))

        with patch.object(extractor, "_extract_with_mineru", side_effect=RuntimeError("MinerU 409")):
            with pytest.raises(RuntimeError):
                extractor.extract(dummy_pdf, on_page=_on_page)
        assert received == [], "on_page must not fire when extraction is aborting"

    def test_the_on_page_callback_reaches_mineru_through_the_collector(self, extractor, dummy_pdf):
        cb = lambda *a: None  # noqa: E731
        with patch.object(extractor, "_extract_with_mineru", return_value=self._mineru) as mineru:
            extractor.extract(dummy_pdf, on_page=cb)
        # nexus-i0cwh: the backend receives extract()'s pages_with_text collector, which forwards
        # every call to the caller's callback.
        forwarded = mineru.call_args.kwargs["on_page"]
        assert forwarded is not cb and callable(forwarded)


# ── on_page callback (RDR-048) ──────────────────────────────────────────────


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
