# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for PDFExtractor using real PDF fixture files.

No patching of pymupdf or pymupdf4llm — every test exercises the actual
extraction pipeline against programmatically-generated fixture PDFs.

AC-U1 through AC-U8 from RDR-011.
"""
from pathlib import Path

from nexus.pdf_extractor import PDFExtractor


class TestExtractSimple:
    """AC-U1: Happy-path extraction of a single-page TrueType PDF."""

    def test_extraction_method_is_markdown(self, simple_pdf: Path) -> None:
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["extraction_method"] == "pymupdf4llm_markdown"

    def test_text_is_nonempty(self, simple_pdf: Path) -> None:
        result = PDFExtractor().extract(simple_pdf)
        assert result.text.strip()

    def test_page_count_is_one(self, simple_pdf: Path) -> None:
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["page_count"] == 1

    def test_format_is_markdown(self, simple_pdf: Path) -> None:
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["format"] == "markdown"


class TestExtractMultipage:
    """AC-U2: Page boundary tracking across a 3-page PDF."""

    def test_page_count_is_three(self, multipage_pdf: Path) -> None:
        result = PDFExtractor().extract(multipage_pdf)
        assert result.metadata["page_count"] == 3

    def test_page_boundaries_has_three_entries(self, multipage_pdf: Path) -> None:
        result = PDFExtractor().extract(multipage_pdf)
        assert len(result.metadata["page_boundaries"]) == 3

    def test_page_boundaries_have_correct_page_numbers(self, multipage_pdf: Path) -> None:
        result = PDFExtractor().extract(multipage_pdf)
        numbers = [b["page_number"] for b in result.metadata["page_boundaries"]]
        assert numbers == [1, 2, 3]


class TestType3Detection:
    """AC-U3 / AC-U4: Type3 font detection."""

    def test_simple_pdf_has_no_type3_fonts(self, simple_pdf: Path) -> None:
        assert PDFExtractor()._has_type3_fonts(simple_pdf) is False

    def test_type3_pdf_has_type3_fonts(self, type3_pdf: Path) -> None:
        assert PDFExtractor()._has_type3_fonts(type3_pdf) is True


class TestType3Fallback:
    """AC-U5: Type3 PDFs use normalized fallback extraction path."""

    def test_extraction_method_is_normalized(self, type3_pdf: Path) -> None:
        result = PDFExtractor().extract(type3_pdf)
        assert result.metadata["extraction_method"] == "pymupdf_normalized"

    def test_format_is_normalized(self, type3_pdf: Path) -> None:
        result = PDFExtractor().extract(type3_pdf)
        assert result.metadata["format"] == "normalized"


class TestDocumentMetadata:
    """AC-U6 / AC-U7 / AC-U8: PDF document metadata propagation."""

    def test_simple_pdf_title(self, simple_pdf: Path) -> None:
        """AC-U6: pdf_title extracted from simple.pdf."""
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["pdf_title"] == "Test Document"

    def test_simple_pdf_author(self, simple_pdf: Path) -> None:
        """AC-U6: pdf_author extracted from simple.pdf."""
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["pdf_author"] == "Test Author"

    def test_simple_pdf_creation_date_value(self, simple_pdf: Path) -> None:
        """AC-U6: pdf_creation_date round-trips the exact fixture value."""
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["pdf_creation_date"] == "D:20260301000000"

    def test_simple_pdf_subject_and_keywords(self, simple_pdf: Path) -> None:
        """AC-U6: pdf_subject and pdf_keywords extracted from simple.pdf."""
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["pdf_subject"] == "PDF Ingest Testing"
        assert result.metadata["pdf_keywords"] == "test, pdf, nexus"

    def test_multipage_pdf_title(self, multipage_pdf: Path) -> None:
        """AC-U7: pdf_title extracted from multipage.pdf."""
        result = PDFExtractor().extract(multipage_pdf)
        assert result.metadata["pdf_title"] == "Multipage Test"

    def test_all_pdf_meta_keys_present_and_string(self, multipage_pdf: Path) -> None:
        """AC-U7: All pdf_* keys exist, are str, and are not None."""
        result = PDFExtractor().extract(multipage_pdf)
        meta = result.metadata
        for key in (
            "pdf_title", "pdf_author", "pdf_subject", "pdf_keywords",
            "pdf_creator", "pdf_producer", "pdf_creation_date", "pdf_mod_date",
        ):
            assert key in meta, f"Missing key: {key!r}"
            assert meta[key] is not None, f"Key {key!r} is None"
            assert isinstance(meta[key], str), f"Key {key!r} is not a str"

    def test_type3_pdf_has_all_metadata_keys(self, type3_pdf: Path) -> None:
        """AC-U8: Normalized extraction path also emits all pdf_* keys."""
        result = PDFExtractor().extract(type3_pdf)
        meta = result.metadata
        for key in ("pdf_title", "pdf_author", "pdf_subject", "pdf_keywords",
                    "pdf_creator", "pdf_producer", "pdf_creation_date", "pdf_mod_date"):
            assert key in meta, f"Normalized path missing key: {key!r}"
