# SPDX-License-Identifier: AGPL-3.0-or-later
"""MinerU extraction backend tests (fully mocked).

Tests are structured around the new batched-subprocess architecture:
- _mineru_build_result: static method, tested directly (no mocking)
- _mineru_run_isolated: subprocess call, mocked at method level
- _extract_with_mineru: orchestrator, mocked at pymupdf + _mineru_run_isolated
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_extractor import ExtractionResult, PDFExtractor


@pytest.fixture
def extractor():
    return PDFExtractor()


@pytest.fixture
def dummy_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"dummy pdf bytes")
    return p


# ── _mineru_build_result (static, no mocking needed) ───────────────────────


class TestMineruBuildResult:
    """_mineru_build_result assembles ExtractionResult from raw MinerU outputs."""

    def test_extraction_method_is_mineru(self, dummy_pdf):
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, "# Title", [], [{"page_idx": 0, "para_blocks": []}],
        )
        assert isinstance(result, ExtractionResult)
        assert result.metadata["extraction_method"] == "mineru"

    def test_text_passed_through(self, dummy_pdf):
        md = "# My Paper\n\nBody text."
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, md, [], [{"page_idx": 0, "para_blocks": []}],
        )
        assert result.text == md

    def test_metadata_has_standard_keys(self, dummy_pdf):
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, "text", [], [{"page_idx": 0, "para_blocks": []}],
        )
        required_keys = {
            "extraction_method", "page_count", "formula_count",
            "page_boundaries", "format",
            "pdf_title", "pdf_author", "pdf_subject", "pdf_keywords",
            "pdf_creator", "pdf_producer", "pdf_creation_date", "pdf_mod_date",
            "docling_title", "table_regions",
        }
        missing = required_keys - result.metadata.keys()
        assert not missing, f"Missing metadata keys: {missing}"

    def test_display_equations_counted(self, dummy_pdf):
        content_list = [
            {"type": "text", "text": "Body"},
            {"type": "equation", "text": "$$E = mc^2$$"},
            {"type": "equation", "text": "$$F = ma$$"},
        ]
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, "text", content_list, [{"page_idx": 0, "para_blocks": []}],
        )
        assert result.metadata["formula_count"] >= 2

    def test_inline_equations_counted(self, dummy_pdf):
        pdf_info = [{
            "page_idx": 0,
            "para_blocks": [{
                "type": "text",
                "lines": [{"spans": [
                    {"type": "text", "content": "where "},
                    {"type": "inline_equation", "content": "\\frac{dx}{dt}"},
                ]}],
            }],
        }]
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, "text", [], pdf_info,
        )
        assert result.metadata["formula_count"] >= 1

    def test_combined_display_and_inline(self, dummy_pdf):
        content_list = [{"type": "equation", "text": "$$E = mc^2$$"}]
        pdf_info = [{
            "page_idx": 0,
            "para_blocks": [{
                "type": "text",
                "lines": [{"spans": [
                    {"type": "inline_equation", "content": "x^2"},
                    {"type": "inline_equation", "content": "y^2"},
                ]}],
            }],
        }]
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, "text", content_list, pdf_info,
        )
        assert result.metadata["formula_count"] == 3

    def test_zero_formulas(self, dummy_pdf):
        pdf_info = [{
            "page_idx": 0,
            "para_blocks": [{
                "type": "text",
                "lines": [{"spans": [{"type": "text", "content": "plain"}]}],
            }],
        }]
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, "text", [{"type": "text", "text": "x"}], pdf_info,
        )
        assert result.metadata["formula_count"] == 0

    def test_page_count_from_pdf_info(self, dummy_pdf):
        pdf_info = [
            {"page_idx": 0, "para_blocks": []},
            {"page_idx": 1, "para_blocks": []},
            {"page_idx": 2, "para_blocks": []},
        ]
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, "some text", [], pdf_info,
        )
        assert result.metadata["page_count"] == 3

    def test_page_boundaries_structure(self, dummy_pdf):
        pdf_info = [
            {"page_idx": 0, "para_blocks": []},
            {"page_idx": 1, "para_blocks": []},
        ]
        result = PDFExtractor._mineru_build_result(
            dummy_pdf, "some text here", [], pdf_info,
        )
        boundaries = result.metadata["page_boundaries"]
        assert len(boundaries) >= 1
        for b in boundaries:
            assert "page_number" in b
            assert "start_char" in b
            assert "page_text_length" in b


# ── _extract_with_mineru orchestration ──────────────────────────────────────


class TestMineruOrchestration:
    """_extract_with_mineru batches pages and delegates to _mineru_run_isolated."""

    def _mock_pymupdf(self, page_count: int):
        """Return a patch context for pymupdf.open that reports page_count."""
        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=page_count)
        mock_doc.__enter__ = MagicMock(return_value=mock_doc)
        mock_doc.__exit__ = MagicMock(return_value=False)
        mock_pymupdf = MagicMock()
        mock_pymupdf.open = MagicMock(return_value=mock_doc)
        return patch.dict("sys.modules", {"pymupdf": mock_pymupdf})

    def _mock_do_parse(self):
        """Patch do_parse to non-None so the import guard passes."""
        return patch("nexus.pdf_extractor.do_parse", MagicMock())

    def test_small_pdf_single_batch(self, extractor, dummy_pdf):
        """PDF with <= MINERU_PAGE_BATCH pages runs one batch with end=None."""
        pdf_info = [{"page_idx": 0, "para_blocks": []}]
        isolated_return = ("# Title", [], pdf_info)

        with self._mock_pymupdf(1), self._mock_do_parse(), \
             patch.object(extractor, "_mineru_run_isolated", return_value=isolated_return) as mock_iso:
            result = extractor._extract_with_mineru(dummy_pdf)

        mock_iso.assert_called_once_with(dummy_pdf, 0, None)
        assert result.metadata["extraction_method"] == "mineru"

    def test_large_pdf_splits_into_batches(self, extractor, dummy_pdf):
        """PDF with > MINERU_PAGE_BATCH pages splits into multiple batches."""
        extractor.MINERU_PAGE_BATCH = 5
        pdf_info = [{"page_idx": 0, "para_blocks": []}]
        isolated_return = ("batch text", [], pdf_info)

        with self._mock_pymupdf(12), self._mock_do_parse(), \
             patch.object(extractor, "_mineru_run_isolated", return_value=isolated_return) as mock_iso:
            result = extractor._extract_with_mineru(dummy_pdf)

        # 12 pages / 5 per batch = 3 batches: (0,5), (5,10), (10,12)
        assert mock_iso.call_count == 3
        calls = [c.args for c in mock_iso.call_args_list]
        assert calls[0] == (dummy_pdf, 0, 5)
        assert calls[1] == (dummy_pdf, 5, 10)
        assert calls[2] == (dummy_pdf, 10, 12)

    def test_batch_results_merged(self, extractor, dummy_pdf):
        """Markdown from batches is joined; content_list and pdf_info are concatenated."""
        extractor.MINERU_PAGE_BATCH = 2
        batch1 = ("# Page 1", [{"type": "equation", "text": "E=mc2"}],
                   [{"page_idx": 0, "para_blocks": []}])
        batch2 = ("# Page 2", [{"type": "text", "text": "plain"}],
                   [{"page_idx": 1, "para_blocks": []}])

        with self._mock_pymupdf(4), self._mock_do_parse(), \
             patch.object(extractor, "_mineru_run_isolated", side_effect=[batch1, batch2]):
            result = extractor._extract_with_mineru(dummy_pdf)

        assert "# Page 1" in result.text
        assert "# Page 2" in result.text
        assert result.metadata["page_count"] == 2
        assert result.metadata["formula_count"] == 1


# ── _mineru_run_isolated subprocess handling ────────────────────────────────


class TestMineruRunIsolated:
    """_mineru_run_isolated shells out to a subprocess and reads results."""

    @patch("nexus.pdf_extractor.subprocess.run")
    @patch("nexus.pdf_extractor.tempfile.mkdtemp")
    def test_subprocess_failure_raises(self, mock_mkdtemp, mock_run, extractor, dummy_pdf, tmp_path):
        """Non-zero exit code raises RuntimeError."""
        mock_mkdtemp.return_value = str(tmp_path / "work")
        (tmp_path / "work").mkdir()
        mock_run.return_value = MagicMock(returncode=1)

        with pytest.raises(RuntimeError, match="MinerU subprocess exited with code 1"):
            extractor._mineru_run_isolated(dummy_pdf, 0, None)


# ── Error handling ──────────────────────────────────────────────────────────


class TestMineruErrorHandling:
    """Error paths: missing dependency."""

    def test_raises_import_error_when_mineru_not_installed(self, extractor, dummy_pdf):
        """ImportError with install instructions when do_parse is None."""
        with patch("nexus.pdf_extractor.do_parse", None):
            with pytest.raises(ImportError, match="MinerU is not installed"):
                extractor._extract_with_mineru(dummy_pdf)
