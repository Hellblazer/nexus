# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for PDFExtractor's text-only extractor using real PDF fixture files.

No patching of pymupdf: every test exercises the actual extraction against
programmatically-generated fixture PDFs. The default extractor (MinerU) needs the PDF stack and
its models, so its end-to-end coverage lives in the ``slow`` tests and the release shakedown
(nexus-jy4hd); this file runs everywhere.

RDR-237 Phase 1 Step 3: these tests asserted a removed extractor's output; they now
assert the explicit ``pymupdf`` extractor's, which reads each page's own text layer.
"""
from pathlib import Path

from nexus.pdf_extractor import PDFExtractor


def _extract(path: Path):
    return PDFExtractor().extract(path, extractor="pymupdf")


class TestExtractSimple:
    """Happy-path extraction of a single-page TrueType PDF."""

    def test_extraction_method_is_pymupdf(self, simple_pdf: Path) -> None:
        assert _extract(simple_pdf).metadata["extraction_method"] == "pymupdf"

    def test_text_is_nonempty(self, simple_pdf: Path) -> None:
        assert _extract(simple_pdf).text.strip()

    def test_page_count_is_one(self, simple_pdf: Path) -> None:
        assert _extract(simple_pdf).metadata["page_count"] == 1

    def test_format_is_normalized(self, simple_pdf: Path) -> None:
        assert _extract(simple_pdf).metadata["format"] == "normalized"


class TestExtractMultipage:
    """Page boundary tracking across a 3-page PDF."""

    def test_page_count_is_three(self, multipage_pdf: Path) -> None:
        assert _extract(multipage_pdf).metadata["page_count"] == 3

    def test_page_boundaries_has_three_entries(self, multipage_pdf: Path) -> None:
        assert len(_extract(multipage_pdf).metadata["page_boundaries"]) == 3

    def test_page_boundaries_have_correct_page_numbers(self, multipage_pdf: Path) -> None:
        numbers = [b["page_number"] for b in _extract(multipage_pdf).metadata["page_boundaries"]]
        assert numbers == [1, 2, 3]

    def test_page_boundaries_tile_the_text_exactly(self, multipage_pdf: Path) -> None:
        result = _extract(multipage_pdf)
        position = 0
        for boundary in result.metadata["page_boundaries"]:
            assert boundary["start_char"] == position
            position += boundary["page_text_length"]
        assert position == len(result.text), "each page length counts its join newline except the last"


class TestType3Fonts:
    """A Type3-font PDF is read from its text layer like any other."""

    def test_type3_pdf_is_extracted_by_pymupdf(self, type3_pdf: Path) -> None:
        result = _extract(type3_pdf)
        assert result.metadata["extraction_method"] == "pymupdf"
        assert result.metadata["format"] == "normalized"


class TestDocumentMetadata:
    """PDF document metadata keys are present, as strings."""

    KEYS = (
        "pdf_title", "pdf_author", "pdf_subject", "pdf_keywords",
        "pdf_creator", "pdf_producer", "pdf_creation_date", "pdf_mod_date",
    )

    def test_all_pdf_meta_keys_present_and_string(self, simple_pdf: Path) -> None:
        meta = _extract(simple_pdf).metadata
        for key in self.KEYS:
            assert key in meta, f"Missing key: {key!r}"
            assert isinstance(meta[key], str), f"Key {key!r} is not a str"

    def test_type3_pdf_has_all_metadata_keys(self, type3_pdf: Path) -> None:
        meta = _extract(type3_pdf).metadata
        for key in self.KEYS:
            assert key in meta, f"pymupdf path missing key: {key!r}"
