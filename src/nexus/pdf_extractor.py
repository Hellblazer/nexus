# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text extraction: PyMuPDF4LLM markdown primary, normalized fallback.

Extraction strategy (two tiers implemented):
1. PyMuPDF4LLM markdown — quality-first, preserves headings/lists/tables.
2. PyMuPDF normalized — used when Type3 fonts are detected (pymupdf4llm can
   hang indefinitely on Type3 fonts) or when a font-related RuntimeError is raised.

Note: pdfplumber was considered for a third-tier complex-table fallback
(e.g., multi-column PDFs where pymupdf table detection is unreliable) but is
not currently implemented. The two-tier strategy covers the known failure modes.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re


@dataclass
class ExtractionResult:
    """Result of PDF text extraction."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PDFExtractor:
    """Extract PDF text as markdown (PyMuPDF4LLM) with normalized fallback.

    Extraction priority:
    1. PyMuPDF4LLM markdown — quality-first, preserves headings/lists/tables.
    2. PyMuPDF normalized — used when Type3 fonts are detected (pymupdf4llm
       can hang indefinitely on Type3 fonts) or when a font-related RuntimeError
       is raised by pymupdf4llm.
    """

    def extract(self, pdf_path: Path) -> ExtractionResult:
        """Extract text from *pdf_path*. Returns ExtractionResult."""
        if self._has_type3_fonts(pdf_path):
            return self._extract_normalized(pdf_path)
        try:
            result = self._extract_markdown(pdf_path)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "font" in msg or "code=4" in msg:
                return self._extract_normalized(pdf_path)
            raise
        # Layout mode can produce empty output for minimal or atypical PDFs.
        # Fall back to normalized extraction so callers always get some content.
        if not result.text.strip():
            return self._extract_normalized(pdf_path)
        return result

    # ── internal extraction methods ───────────────────────────────────────────

    def _has_type3_fonts(self, pdf_path: Path) -> bool:
        """Return True if any page uses a Type3 font (can cause pymupdf4llm hangs)."""
        import pymupdf  # lazy — not installed in all environments

        try:
            with pymupdf.open(pdf_path) as doc:
                for page in doc:
                    for font in page.get_fonts():
                        font_type = font[2] if len(font) > 2 else ""
                        if "Type3" in font_type:
                            return True
            return False
        except Exception:
            return False

    def _extract_markdown(self, pdf_path: Path) -> ExtractionResult:
        """Extract per-page markdown via pymupdf4llm."""
        import pymupdf        # lazy
        # Activate layout analysis engine before importing pymupdf4llm so that
        # pymupdf4llm selects layout mode (pymupdf._get_layout is checked at
        # import time).  No-ops if layout was already activated or unavailable.
        try:
            from pymupdf import layout as _layout
            _layout.activate()
        except Exception:
            pass
        import pymupdf4llm    # lazy

        page_texts: list[str] = []
        page_boundaries: list[dict] = []
        current_pos = 0

        with pymupdf.open(pdf_path) as doc:
            page_count = len(doc)
            doc_meta = doc.metadata or {}
            for page_num in range(page_count):
                # Pass the open doc object (not the path) to avoid re-opening
                # the file for every page — O(1) opens instead of O(N_pages).
                page_md: str = pymupdf4llm.to_markdown(
                    doc,
                    pages=[page_num],
                    ignore_images=True,
                    force_text=True,
                ).strip()
                if page_md:
                    page_boundaries.append(
                        {
                            "page_number": page_num + 1,
                            "start_char": current_pos,
                            # +1 includes the \n separator from "\n".join so that
                            # _page_for ranges are contiguous and the separator is
                            # attributed to the preceding page (not left uncovered).
                            # For the last page the range extends 1 char beyond the
                            # text end, which is harmless since no chunk starts there.
                            "page_text_length": len(page_md) + 1,
                        }
                    )
                    page_texts.append(page_md)
                    current_pos += len(page_md) + 1

        return ExtractionResult(
            text="\n".join(page_texts),
            metadata={
                "extraction_method": "pymupdf4llm_markdown",
                "page_count": page_count,
                "format": "markdown",
                "page_boundaries": page_boundaries,
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
                if page_text:
                    page_boundaries.append(
                        {
                            "page_number": page_num + 1,
                            "start_char": current_pos,
                            # +1 includes the \n separator from "\n".join (same
                            # rationale as _extract_markdown: contiguous ranges).
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
