# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text extraction: Docling primary, PyMuPDF normalized fallback.

Extraction strategy (two tiers):
1. Docling — neural layout model handles multi-column academic PDFs, Type3 fonts,
   and complex tables. Extracts title from document content.
2. PyMuPDF normalized — final fallback for Docling failures or empty output.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import logging
import re

_log = logging.getLogger(__name__)


def _normalize_whitespace_edge_cases(text: str) -> str:
    """Normalize whitespace variants not covered by basic normalization.

    - Replace tab characters with a single space.
    - Collapse Unicode non-breaking and exotic whitespace to a single space.
    - Collapse 4+ consecutive newlines to three (preserving intentional breaks).
    """
    text = text.replace("\t", " ")
    text = re.sub(r"[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


@dataclass
class ExtractionResult:
    """Result of PDF text extraction."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PDFExtractor:
    """Extract PDF text via Docling with PyMuPDF normalized fallback.

    Docling uses a neural layout model to handle multi-column academic PDFs,
    producing structured markdown with headings and correct reading order.
    Falls back to PyMuPDF normalized extraction on any Docling failure.
    """

    def __init__(self) -> None:
        self._converter = None  # lazy init — avoid 3.5s cost for non-PDF operations

    def extract(self, pdf_path: Path) -> ExtractionResult:
        """Extract text from *pdf_path*. Returns ExtractionResult."""
        try:
            return self._extract_with_docling(pdf_path)
        except Exception:
            _log.warning(
                "docling extraction failed; falling back to pymupdf_normalized",
                exc_info=True,
            )
            return self._extract_normalized(pdf_path)

    # ── internal extraction methods ───────────────────────────────────────────

    def _get_converter(self):
        """Lazily initialise the Docling DocumentConverter."""
        if self._converter is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions

            opts = PdfPipelineOptions()
            opts.do_ocr = False                 # digital PDFs have embedded text
            opts.do_table_structure = True      # TableFormer for table detection
            opts.generate_page_images = False
            opts.generate_picture_images = False
            self._converter = DocumentConverter(
                format_options={"pdf": PdfFormatOption(pipeline_options=opts)}
            )
        return self._converter

    def _extract_with_docling(self, pdf_path: Path) -> ExtractionResult:
        """Extract per-page markdown via Docling."""
        result = self._get_converter().convert(str(pdf_path))
        doc = result.document
        page_count = doc.num_pages()

        page_texts: list[str] = []
        page_boundaries: list[dict] = []
        current_pos = 0

        for p in range(1, page_count + 1):
            page_md = doc.export_to_markdown(page_no=p).strip()
            if page_md:
                page_boundaries.append(
                    {
                        "page_number": p,
                        "start_char": current_pos,
                        # +1 includes the \n separator from "\n".join so that
                        # _page_for ranges are contiguous (same convention as the
                        # former _extract_markdown implementation).
                        "page_text_length": len(page_md) + 1,
                    }
                )
                page_texts.append(page_md)
                current_pos += len(page_md) + 1

        text = "\n".join(page_texts)
        if not text.strip():
            raise RuntimeError("docling produced empty output")

        # Second pass: collect TableItem regions (duck-typed, import-path safe)
        table_regions: list[dict] = []
        for item, _ in doc.iterate_items():
            if type(item).__name__ != "TableItem":
                continue
            prov = getattr(item, "prov", [])
            page_no = prov[0].page_no if prov else 0
            html = ""
            if callable(getattr(item, "export_to_html", None)):
                try:
                    html = item.export_to_html()
                except Exception as exc:
                    _log.debug("table_html_export_failed", page=page_no, error=str(exc))
                    html = ""
            table_regions.append({"page": page_no, "html": html})

        return ExtractionResult(
            text=text,
            metadata={
                "extraction_method": "docling",
                "page_count": page_count,
                "format": "markdown",
                "page_boundaries": page_boundaries,
                "table_regions": table_regions,
                "docling_title": self._extract_title(doc),
                "pdf_title": "",  # XMP metadata not exposed by Docling
                "pdf_author": "",
                "pdf_subject": "",
                "pdf_keywords": "",
                "pdf_creator": "",
                "pdf_producer": "",
                "pdf_creation_date": "",
                "pdf_mod_date": "",
            },
        )

    def _extract_title(self, doc) -> str:
        """Extract a paper title from Docling document items on page 1.

        Algorithm (verified on 19 corpus PDFs, 17/19 correct):
        1. Iterate page-1 items, skip section labels (abstract, introduction, keywords).
        2. Return first item with label containing 'title' or 'section_header'.
        3. Fallback: first text-labelled item on page 1 with 10 ≤ len < 120.
        """
        _SKIP = {"abstract", "introduction", "1 introduction", "keywords"}

        for item, _ in doc.iterate_items():
            prov = getattr(item, "prov", [])
            if not prov or prov[0].page_no != 1:
                continue
            text = (getattr(item, "text", "") or "").strip()
            if not text or len(text) < 10:
                continue
            lower = text.lower()
            if lower in _SKIP:
                continue
            if lower.startswith("abstract") and len(text) > 100:
                continue
            label = str(getattr(item, "label", ""))
            if "title" in label or "section_header" in label:
                return text

        # Fallback: first short text block on page 1
        for item, _ in doc.iterate_items():
            prov = getattr(item, "prov", [])
            if not prov or prov[0].page_no != 1:
                continue
            text = (getattr(item, "text", "") or "").strip()
            if text and 10 <= len(text) < 120:
                return text

        return ""

    def _extract_normalized(self, pdf_path: Path) -> ExtractionResult:
        """Extract via raw PyMuPDF with whitespace normalization."""
        import pymupdf  # lazy

        text_parts: list[str] = []
        page_boundaries: list[dict] = []
        current_pos = 0

        with pymupdf.open(pdf_path) as doc:
            page_count = len(doc)
            doc_meta = doc.metadata or {}
            for page_num, page in enumerate(doc):
                raw: str = page.get_text(sort=True)
                # Normalize per-page so page_boundaries match character positions
                # in the final joined text (global normalization after the fact
                # would shift boundaries unpredictably).
                page_text = re.sub(r" +", " ", raw)
                page_text = re.sub(r"\n{3,}", "\n\n", page_text)
                page_text = "\n".join(line.rstrip() for line in page_text.split("\n")).strip()
                page_text = _normalize_whitespace_edge_cases(page_text)
                if page_text:
                    page_boundaries.append(
                        {
                            "page_number": page_num + 1,
                            "start_char": current_pos,
                            # +1 includes the \n separator from "\n".join (same
                            # rationale as _extract_with_docling: contiguous ranges).
                            "page_text_length": len(page_text) + 1,
                        }
                    )
                    text_parts.append(page_text)
                    current_pos += len(page_text) + 1

        text = "\n".join(text_parts)

        return ExtractionResult(
            text=text,
            metadata={
                "extraction_method": "pymupdf_normalized",
                "page_count": page_count,
                "format": "normalized",
                "page_boundaries": page_boundaries,
                "docling_title": "",
                "pdf_title": doc_meta.get("title", ""),
                "pdf_author": doc_meta.get("author", ""),
                "pdf_subject": doc_meta.get("subject", ""),
                "pdf_keywords": doc_meta.get("keywords", ""),
                "pdf_creator": doc_meta.get("creator", ""),
                "pdf_producer": doc_meta.get("producer", ""),
                "pdf_creation_date": doc_meta.get("creationDate", ""),
                "pdf_mod_date": doc_meta.get("modDate", ""),
            },
        )
