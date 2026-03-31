# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-044 Phase 1: MinerU extraction backend tests (fully mocked).

MinerU (mineru package) is an optional dependency — every test mocks at
the import boundary.  No real PDF processing occurs.
"""
import json
import os
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


def _setup_mineru_output(
    output_dir: str,
    stem: str,
    md_text: str = "# Title\n\nSome content.",
    content_list: list | None = None,
    middle: dict | None = None,
) -> None:
    """Write mock MinerU output files to the expected directory structure.

    MinerU writes to: output_dir/{stem}/auto/{stem}.md,
    {stem}_content_list.json, {stem}_middle.json
    """
    auto_dir = Path(output_dir) / stem / "auto"
    auto_dir.mkdir(parents=True, exist_ok=True)

    (auto_dir / f"{stem}.md").write_text(md_text, encoding="utf-8")

    if content_list is None:
        content_list = []
    (auto_dir / f"{stem}_content_list.json").write_text(
        json.dumps(content_list), encoding="utf-8"
    )

    if middle is None:
        middle = {"pdf_info": [{"page_idx": 0, "para_blocks": []}]}
    (auto_dir / f"{stem}_middle.json").write_text(
        json.dumps(middle), encoding="utf-8"
    )


# ── ExtractionResult contract ────────────────────────────────────────────────


class TestMineruExtractionResult:
    """_extract_with_mineru returns well-formed ExtractionResult."""

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_returns_extraction_method_mineru(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """extraction_method is 'mineru' in metadata."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        _setup_mineru_output(work_dir, "paper")

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        assert isinstance(result, ExtractionResult)
        assert result.metadata["extraction_method"] == "mineru"

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_text_from_markdown_output(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """Extracted text comes from the .md output file."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        md_text = "# My Paper\n\nThis is the body text."
        _setup_mineru_output(work_dir, "paper", md_text=md_text)

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        assert result.text == md_text

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_metadata_has_standard_keys(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """Metadata includes all standard keys matching the Docling extraction path."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        _setup_mineru_output(work_dir, "paper")

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        required_keys = {
            "extraction_method", "page_count", "formula_count",
            "page_boundaries", "format",
            "pdf_title", "pdf_author", "pdf_subject", "pdf_keywords",
            "pdf_creator", "pdf_producer", "pdf_creation_date", "pdf_mod_date",
            "docling_title", "table_regions",
        }
        missing = required_keys - result.metadata.keys()
        assert not missing, f"Missing metadata keys: {missing}"


# ── Display equations (content_list.json) ─────────────────────────────────────


class TestMineruDisplayEquations:
    """Display equations from content_list.json are counted."""

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_display_equations_counted(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """Entries with type='equation' in content_list.json are counted as formulas."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        content_list = [
            {"type": "text", "text": "Body text", "page_idx": 0, "bbox": [0, 0, 100, 100]},
            {"type": "equation", "text": "$$E = mc^2$$", "page_idx": 0, "bbox": [0, 100, 100, 150]},
            {"type": "equation", "text": "$$F = ma$$", "page_idx": 1, "bbox": [0, 0, 100, 50]},
        ]
        _setup_mineru_output(work_dir, "paper", content_list=content_list)

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        # Display equations contribute to formula_count
        assert result.metadata["formula_count"] >= 2


# ── Inline equations (middle.json) ────────────────────────────────────────────


class TestMineruInlineEquations:
    """Inline equations from middle.json spans are counted."""

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_inline_equations_counted(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """Spans with type='inline_equation' in middle.json are counted as formulas."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        middle = {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "para_blocks": [
                        {
                            "type": "text",
                            "lines": [
                                {
                                    "spans": [
                                        {"type": "text", "content": "where "},
                                        {"type": "inline_equation", "content": "\\frac{dx}{dt}"},
                                        {"type": "text", "content": " is the derivative"},
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        _setup_mineru_output(work_dir, "paper", middle=middle)

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        assert result.metadata["formula_count"] >= 1

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_combined_display_and_inline_count(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """formula_count equals total of display + inline equations."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        content_list = [
            {"type": "equation", "text": "$$E = mc^2$$", "page_idx": 0, "bbox": [0, 0, 100, 50]},
        ]
        middle = {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "para_blocks": [
                        {
                            "type": "text",
                            "lines": [
                                {
                                    "spans": [
                                        {"type": "inline_equation", "content": "x^2"},
                                        {"type": "inline_equation", "content": "y^2"},
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        _setup_mineru_output(work_dir, "paper", content_list=content_list, middle=middle)

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        # 1 display + 2 inline = 3
        assert result.metadata["formula_count"] == 3

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_zero_formulas_when_none_present(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """formula_count is 0 when no equations found in either file."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        content_list = [
            {"type": "text", "text": "Just text", "page_idx": 0, "bbox": [0, 0, 100, 100]},
        ]
        middle = {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "para_blocks": [
                        {
                            "type": "text",
                            "lines": [{"spans": [{"type": "text", "content": "plain text"}]}],
                        }
                    ],
                }
            ]
        }
        _setup_mineru_output(work_dir, "paper", content_list=content_list, middle=middle)

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        assert result.metadata["formula_count"] == 0


# ── Page boundaries ──────────────────────────────────────────────────────────


class TestMineruPageBoundaries:
    """page_boundaries derived from page_idx fields in middle.json."""

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_page_count_from_middle_json(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """page_count reflects the number of pages in middle.json pdf_info."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        middle = {
            "pdf_info": [
                {"page_idx": 0, "para_blocks": []},
                {"page_idx": 1, "para_blocks": []},
                {"page_idx": 2, "para_blocks": []},
            ]
        }
        _setup_mineru_output(work_dir, "paper", middle=middle)

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        assert result.metadata["page_count"] == 3

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_page_boundaries_have_page_numbers(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """Each page boundary has page_number, start_char, page_text_length."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        middle = {
            "pdf_info": [
                {"page_idx": 0, "para_blocks": []},
                {"page_idx": 1, "para_blocks": []},
            ]
        }
        _setup_mineru_output(work_dir, "paper", middle=middle)

        with patch("nexus.pdf_extractor.do_parse"):
            result = extractor._extract_with_mineru(dummy_pdf)

        boundaries = result.metadata["page_boundaries"]
        assert len(boundaries) >= 1
        for b in boundaries:
            assert "page_number" in b
            assert "start_char" in b
            assert "page_text_length" in b


# ── Tempdir lifecycle ────────────────────────────────────────────────────────


class TestMineruTempdirLifecycle:
    """Temporary directory is created and cleaned up via context manager."""

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_tempdir_used_as_context_manager(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """TemporaryDirectory is used as a context manager (enter + exit)."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        _setup_mineru_output(work_dir, "paper")

        with patch("nexus.pdf_extractor.do_parse"):
            extractor._extract_with_mineru(dummy_pdf)

        mock_tmpdir.__enter__.assert_called_once()
        mock_tmpdir.__exit__.assert_called_once()


# ── Error handling ──────────────────────────────────────────────────────────


class TestMineruErrorHandling:
    """Error paths: missing dependency, do_parse failure."""

    def test_raises_import_error_when_mineru_not_installed(self, extractor, dummy_pdf):
        """ImportError with install instructions when do_parse is None."""
        with patch("nexus.pdf_extractor.do_parse", None):
            with pytest.raises(ImportError, match="MinerU is not installed"):
                extractor._extract_with_mineru(dummy_pdf)

    @patch("nexus.pdf_extractor.tempfile.TemporaryDirectory")
    def test_do_parse_failure_propagates(self, mock_tmpdir_cls, extractor, dummy_pdf, tmp_path):
        """Exceptions from do_parse propagate to caller (for fallback handling)."""
        work_dir = str(tmp_path / "mineru_work")
        mock_tmpdir = MagicMock()
        mock_tmpdir.__enter__ = MagicMock(return_value=work_dir)
        mock_tmpdir.__exit__ = MagicMock(return_value=False)
        mock_tmpdir_cls.return_value = mock_tmpdir

        with patch("nexus.pdf_extractor.do_parse", side_effect=RuntimeError("model download failed")):
            with pytest.raises(RuntimeError, match="model download failed"):
                extractor._extract_with_mineru(dummy_pdf)
