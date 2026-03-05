# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for PDFExtractor using real PDF fixture files.

No patching of Docling or pymupdf — every test exercises the actual
extraction pipeline against programmatically-generated fixture PDFs.

RDR-021: replaces 3-tier stack with Docling primary + pymupdf_normalized fallback.
"""
from pathlib import Path

from nexus.pdf_extractor import PDFExtractor


class TestExtractSimple:
    """Happy-path extraction of a single-page TrueType PDF."""

    def test_extraction_method_is_docling(self, simple_pdf: Path) -> None:
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["extraction_method"] == "docling"

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
    """Page boundary tracking across a 3-page PDF."""

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


class TestType3WithDocling:
    """Type3 font PDFs are now handled by Docling (not routed to normalized fallback).

    RDR-021: Docling's neural layout model extracts text regardless of font type;
    the pymupdf4llm Type3 detection and fallback logic has been removed.
    """

    def test_type3_pdf_uses_docling(self, type3_pdf: Path) -> None:
        """Type3 fixture PDF is extracted by Docling (extraction_method = 'docling')."""
        result = PDFExtractor().extract(type3_pdf)
        assert result.metadata["extraction_method"] == "docling"

    def test_type3_pdf_format_is_markdown(self, type3_pdf: Path) -> None:
        result = PDFExtractor().extract(type3_pdf)
        assert result.metadata["format"] == "markdown"


class TestDocumentMetadata:
    """PDF document metadata keys are present; Docling extracts titles from content."""

    def test_all_pdf_meta_keys_present_and_string(self, simple_pdf: Path) -> None:
        """All pdf_* and docling_* keys exist, are str, and are not None."""
        result = PDFExtractor().extract(simple_pdf)
        meta = result.metadata
        for key in (
            "pdf_title", "pdf_author", "pdf_subject", "pdf_keywords",
            "pdf_creator", "pdf_producer", "pdf_creation_date", "pdf_mod_date",
            "docling_title",
        ):
            assert key in meta, f"Missing key: {key!r}"
            assert meta[key] is not None, f"Key {key!r} is None"
            assert isinstance(meta[key], str), f"Key {key!r} is not a str"

    def test_docling_does_not_expose_xmp_metadata(self, simple_pdf: Path) -> None:
        """Docling path sets pdf_title/author/etc to '' (XMP not exposed by Docling)."""
        result = PDFExtractor().extract(simple_pdf)
        # Docling does not parse XMP/Info dict metadata; these are always empty.
        assert result.metadata["pdf_title"] == ""
        assert result.metadata["pdf_author"] == ""

    def test_docling_title_key_present(self, simple_pdf: Path) -> None:
        """docling_title key is always present in Docling output (may be empty string)."""
        result = PDFExtractor().extract(simple_pdf)
        assert "docling_title" in result.metadata
        assert isinstance(result.metadata["docling_title"], str)

    def test_type3_pdf_has_all_metadata_keys(self, type3_pdf: Path) -> None:
        """Docling extraction path emits all expected metadata keys."""
        result = PDFExtractor().extract(type3_pdf)
        meta = result.metadata
        for key in ("pdf_title", "pdf_author", "pdf_subject", "pdf_keywords",
                    "pdf_creator", "pdf_producer", "pdf_creation_date", "pdf_mod_date",
                    "docling_title"):
            assert key in meta, f"Docling path missing key: {key!r}"
