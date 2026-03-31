# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text extraction with auto-detect math routing.

Extraction backends (three tiers, selected by ``extractor`` param):
1. Docling — neural layout model for multi-column academic PDFs, Type3 fonts,
   and complex tables.  Enriched mode enables formula detection via FormulaItem.
2. MinerU — math-aware extraction (optional ``mineru`` extra).  Used when auto
   mode detects formulas in the Docling pass.
3. PyMuPDF normalized — final fallback for all extraction failures.

Auto mode (default): Docling pass → if formulas detected → try MinerU → fallback
to Docling → fallback to PyMuPDF normalized.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import re
import tempfile

import structlog

try:
    from mineru.cli.common import do_parse
except ImportError:
    do_parse = None  # type: ignore[assignment]

_log = structlog.get_logger(__name__)


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
        self._converter = None  # lazy init — fast mode (no formula enrichment)
        self._converter_enriched = None  # lazy init — enriched mode (formula enrichment)

    def extract(self, pdf_path: Path, *, extractor: str = "auto") -> ExtractionResult:
        """Extract text from *pdf_path*. Returns ExtractionResult.

        *extractor* selects the backend:
        - ``"auto"`` — Docling pass (enriched, to detect formulas); if
          formulas found, try MinerU then fall back to PyMuPDF normalized.
        - ``"docling"`` — Docling with PyMuPDF normalized fallback.
        - ``"mineru"`` — MinerU directly (no fallback).
        """
        if extractor not in ("auto", "docling", "mineru"):
            raise ValueError(
                f"extractor must be 'auto', 'docling', or 'mineru'; got {extractor!r}"
            )

        if extractor == "docling":
            try:
                return self._extract_with_docling(pdf_path)
            except Exception:
                _log.warning(
                    "docling extraction failed; falling back to pymupdf_normalized",
                    exc_info=True,
                )
                return self._extract_normalized(pdf_path)

        if extractor == "mineru":
            return self._extract_with_mineru(pdf_path)

        # extractor == "auto"
        try:
            fast_result = self._extract_with_docling(pdf_path)
        except Exception:
            _log.warning(
                "docling fast pass failed; falling back to pymupdf_normalized",
                exc_info=True,
            )
            return self._extract_normalized(pdf_path)

        formula_count = fast_result.metadata.get("formula_count", 0)
        if formula_count == 0:
            return fast_result

        # Math paper detected — try MinerU
        try:
            return self._extract_with_mineru(pdf_path)
        except Exception:
            _log.warning(
                "mineru_extraction_failed; falling back to docling_enriched",
                exc_info=True,
            )
            try:
                return self._extract_with_docling(pdf_path)
            except Exception:
                _log.warning(
                    "docling enriched fallback failed; falling back to pymupdf_normalized",
                    exc_info=True,
                )
                return self._extract_normalized(pdf_path)

    # ── internal extraction methods ───────────────────────────────────────────

    def _get_converter(self, enriched: bool = False):
        """Lazily initialise the Docling DocumentConverter.

        *enriched* enables ``do_formula_enrichment`` for LaTeX extraction.
        Two converters are cached independently so callers can switch modes
        without re-creating the converter each time.
        """
        attr = "_converter_enriched" if enriched else "_converter"
        converter = getattr(self, attr)
        if converter is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions

            opts = PdfPipelineOptions()
            opts.do_ocr = False                 # digital PDFs have embedded text
            opts.do_table_structure = True      # TableFormer for table detection
            opts.generate_page_images = False
            opts.generate_picture_images = False
            opts.do_formula_enrichment = enriched
            converter = DocumentConverter(
                format_options={"pdf": PdfFormatOption(pipeline_options=opts)}
            )
            setattr(self, attr, converter)
        return converter

    def _extract_with_docling(self, pdf_path: Path) -> ExtractionResult:
        """Extract per-page markdown via Docling."""
        result = self._get_converter(enriched=True).convert(str(pdf_path))
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

        # Collect TableItem regions and count FormulaItem (duck-typed, single pass)
        table_regions: list[dict] = []
        formula_count = 0
        for item, _ in doc.iterate_items():
            item_type = type(item).__name__
            if item_type == "FormulaItem":
                formula_count += 1
            elif item_type == "TableItem":
                prov = getattr(item, "prov", [])
                page_no = prov[0].page_no if prov else 0
                html = ""
                if callable(getattr(item, "export_to_html", None)):
                    try:
                        html = item.export_to_html(doc=doc)
                    except Exception as exc:
                        _log.debug("table_html_export_failed", page=page_no, error=str(exc))
                        html = ""
                table_regions.append({"page": page_no, "html": html})

        if formula_count > 0:
            _log.warning("formula_content_detected", formula_count=formula_count, path=str(pdf_path))

        return ExtractionResult(
            text=text,
            metadata={
                "extraction_method": "docling",
                "page_count": page_count,
                "format": "markdown",
                "page_boundaries": page_boundaries,
                "table_regions": table_regions,
                "formula_count": formula_count,
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

    def _extract_with_mineru(self, pdf_path: Path) -> ExtractionResult:
        """Extract text via MinerU (math-aware, optional dependency).

        NOTE: Page boundaries are approximated by dividing total text length
        evenly across pages.  MinerU writes a single markdown file (not
        per-page), so exact character-level boundaries are unavailable.
        A future phase may refine this using ``middle.json`` paragraph page_idx.
        """
        if do_parse is None:
            raise ImportError(
                "MinerU is not installed. Install with: uv pip install 'conexus[mineru]'"
            )

        with tempfile.TemporaryDirectory() as tmp_str:
            output_dir = Path(tmp_str)
            do_parse(
                input_file_names=[str(pdf_path)],
                output_dir=str(output_dir),
                formula_enable=True,
                table_enable=True,
            )

            pdf_stem = pdf_path.stem
            base = output_dir / pdf_stem / "auto"
            md_text = (base / f"{pdf_stem}.md").read_text(encoding="utf-8")
            content_list_data: list[dict] = json.loads(
                (base / f"{pdf_stem}_content_list.json").read_text(encoding="utf-8")
            )
            middle_data: dict = json.loads(
                (base / f"{pdf_stem}_middle.json").read_text(encoding="utf-8")
            )

        # Display equations from content_list.json
        display_count = sum(1 for e in content_list_data if e.get("type") == "equation")

        # Inline equations from middle.json spans
        inline_count = 0
        for page in middle_data.get("pdf_info", []):
            for block in page.get("para_blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("type") == "inline_equation":
                            inline_count += 1

        formula_count = display_count + inline_count

        # Page boundaries — approximate from text length fraction
        page_count = len(middle_data.get("pdf_info", []))
        total_len = len(md_text)
        page_boundaries: list[dict] = []
        if page_count > 0 and total_len > 0:
            chars_per_page = total_len / page_count
            for i in range(page_count):
                start = int(i * chars_per_page)
                length = int(chars_per_page) + (1 if i < page_count - 1 else 0)
                page_boundaries.append({
                    "page_number": i + 1,
                    "start_char": start,
                    "page_text_length": length,
                })

        if formula_count > 0:
            _log.info(
                "mineru_formulas_extracted",
                formula_count=formula_count,
                path=str(pdf_path),
            )

        return ExtractionResult(
            text=md_text,
            metadata={
                "extraction_method": "mineru",
                "page_count": page_count,
                "format": "markdown",
                "formula_count": formula_count,
                "page_boundaries": page_boundaries,
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
                "formula_count": 0,
            },
        )
