# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text extraction: PyMuPDF4LLM markdown primary, pdfplumber rescue, normalized fallback.

Extraction strategy (three tiers):
1. PyMuPDF4LLM markdown — quality-first, preserves headings/lists/tables.
2. pdfplumber — quality-rescue for PDFs with ruled tables that pymupdf4llm
   misses; invoked only when _markdown_misses_tables() confirms the gap.
3. PyMuPDF normalized — final fallback; raw get_text(sort=True) + whitespace
   normalization. Used when Type3 fonts are detected, a font-related RuntimeError
   is raised, or output is empty.
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


def _format_table(rows: list[list[str | None]]) -> str:
    """Format a pdfplumber table as a Markdown table string.

    Returns an empty string for empty tables.
    None cells are rendered as empty strings.
    """
    if not rows:
        return ""
    # Convert None → ""
    cleaned = [[(cell or "") for cell in row] for row in rows]
    header = cleaned[0]
    body = cleaned[1:]
    separator = "|" + "|".join(" --- " for _ in header) + "|"
    header_row = "|" + "|".join(f" {cell} " for cell in header) + "|"
    data_rows = ["|" + "|".join(f" {cell} " for cell in row) + "|" for row in body]
    return "\n".join([header_row, separator] + data_rows)


@dataclass
class ExtractionResult:
    """Result of PDF text extraction."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PDFExtractor:
    """Extract PDF text as markdown (PyMuPDF4LLM) with pdfplumber rescue and normalized fallback.

    Extraction priority:
    1. PyMuPDF4LLM markdown — quality-first, preserves headings/lists/tables.
    2. pdfplumber — quality-rescue for ruled-table PDFs where pymupdf4llm produces
       garbled or absent table content. Invoked only when _markdown_misses_tables()
       confirms tables are present but missing from markdown output.
    3. PyMuPDF normalized — final fallback for Type3 fonts, font RuntimeErrors,
       or empty output from tier 1.
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
        # Quality-rescue: fall back to pdfplumber if ruled tables are present
        # but missing from the markdown output (e.g. GNN mis-classified cells
        # as body text). pdfplumber is opened only when actually needed.
        if self._markdown_misses_tables(pdf_path, result.text):
            try:
                rescue = self._extract_with_pdfplumber(pdf_path)
                if rescue.text.strip():
                    return rescue
            except Exception:
                _log.debug("pdfplumber rescue failed; using pymupdf4llm output", exc_info=True)
        return result

    # ── internal extraction methods ───────────────────────────────────────────

    def _markdown_misses_tables(self, pdf_path: Path, markdown_text: str) -> bool:
        """Return True if the PDF has ruled tables that are absent from the markdown.

        Uses PyMuPDF's native find_tables() (same algorithm as pdfplumber) to count
        ruled tables on the first five pages, then checks whether the markdown output
        contains a proportional number of pipe characters. Returns False (no fallback)
        when pdfplumber is not installed.

        Note: borderless tables (spacing-only alignment, common in IEEE/ACM papers)
        produce no edges from find_tables() and will not be detected. This is a known
        scope limitation — see RDR-012 Risks.
        """
        try:
            import pdfplumber  # noqa: F401 — availability check only
        except ImportError:
            _log.debug("pdfplumber not installed; table rescue disabled")
            return False
        import pymupdf
        try:
            ruled_tables = 0
            with pymupdf.open(pdf_path) as doc:
                for page in list(doc)[:5]:
                    # TableFinder is always truthy; len() is the correct test.
                    ruled_tables += len(page.find_tables().tables)
            if ruled_tables == 0:
                return False
            pipe_count = markdown_text.count("|")
            # A well-formatted Markdown table produces roughly 2*(N+1) pipes per row
            # plus a separator row. With 3 pipes per table as floor, we allow small
            # single-column pseudo-tables while still catching absent multi-column tables.
            return pipe_count < ruled_tables * 3
        except Exception:
            return False

    def _extract_with_pdfplumber(self, pdf_path: Path) -> ExtractionResult:
        """Extract text and tables via pdfplumber with Markdown table formatting.

        Per page: prose is extracted excluding table bounding boxes (via page.filter())
        to prevent table cell content appearing twice. Tables are formatted as Markdown
        and appended after the prose block for the page.
        """
        import pdfplumber

        page_texts: list[str] = []
        page_boundaries: list[dict] = []
        current_pos = 0
        doc_meta: dict[str, str] = {}

        with pdfplumber.open(pdf_path) as pdf:
            if pdf.metadata:
                raw_meta = pdf.metadata
                doc_meta = {
                    "title": raw_meta.get("Title", "") or "",
                    "author": raw_meta.get("Author", "") or "",
                    "subject": raw_meta.get("Subject", "") or "",
                    "keywords": raw_meta.get("Keywords", "") or "",
                    "creator": raw_meta.get("Creator", "") or "",
                    "producer": raw_meta.get("Producer", "") or "",
                    "creationDate": raw_meta.get("CreationDate", "") or "",
                    "modDate": raw_meta.get("ModDate", "") or "",
                }
            page_count = len(pdf.pages)

            for page in pdf.pages:
                tables = page.find_tables()
                table_bboxes = [t.bbox for t in tables]

                # Extract prose, excluding characters that fall inside table bboxes.
                # page.filter() keeps objects for which the function returns True, so
                # we negate: keep only objects NOT inside any table bbox.
                # This prevents table cell content from appearing in both prose and table
                # markdown. If no tables, fall back to full extract_text().
                if table_bboxes:
                    def _not_in_table(obj: dict, bboxes: list = table_bboxes) -> bool:
                        x0, y0 = obj.get("x0", 0), obj.get("top", 0)
                        x1, y1 = obj.get("x1", 0), obj.get("bottom", 0)
                        return not any(
                            bx0 <= x0 and by0 <= y0 and x1 <= bx1 and y1 <= by1
                            for bx0, by0, bx1, by1 in bboxes
                        )
                    prose = page.filter(_not_in_table).extract_text(layout=True) or ""
                else:
                    prose = page.extract_text(layout=True) or ""

                # Format tables as Markdown, appended after prose.
                table_blocks: list[str] = []
                for table_data in page.extract_tables():
                    if table_data:
                        formatted = _format_table(table_data)
                        if formatted:
                            table_blocks.append(formatted)

                parts = [prose] if prose.strip() else []
                parts.extend(table_blocks)
                page_text = "\n\n".join(parts).strip()

                if page_text:
                    page_boundaries.append(
                        {
                            "page_number": page.page_number,
                            "start_char": current_pos,
                            # +1 includes the \n separator from "\n".join so that
                            # _page_for ranges are contiguous (same convention as
                            # _extract_markdown and _extract_normalized).
                            "page_text_length": len(page_text) + 1,
                        }
                    )
                    page_texts.append(page_text)
                    current_pos += len(page_text) + 1

        return ExtractionResult(
            text="\n".join(page_texts),
            metadata={
                "extraction_method": "pdfplumber",
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
                page_text = _normalize_whitespace_edge_cases(page_text)
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
