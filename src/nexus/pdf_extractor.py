# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text extraction with auto-detect math routing.

Extraction backends (selected by ``extractor`` param):
1. Docling — neural layout model for multi-column academic PDFs, Type3 fonts,
   and complex tables.  Enriched mode enables formula detection via FormulaItem.
2. MinerU — math-aware extraction. Default-installed since nexus-2fyb (was
   previously an optional ``[mineru]`` extra; the extras gate produced silent
   formula loss for weeks because fresh installs never picked it up). Used
   when auto mode detects formulas in the Docling probe pass.
3. PyMuPDF normalized — fallback for the explicit ``extractor='docling'``
   path when Docling itself fails.

Auto mode (default): non-enriched Docling probe → if formulas detected, route
to MinerU. If MinerU fails on a formula-bearing PDF, raise ``RuntimeError``
rather than silently returning the formula-stripped probe (the original
silent-corruption bug). Users who explicitly accept stripped extraction can
opt out with ``--extractor docling``.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import re
import subprocess
import sys
import tempfile

import httpx
import structlog

try:
    from mineru.cli.common import do_parse
except ImportError:
    do_parse = None  # type: ignore[assignment]


# Inline script executed in a child Python process for memory isolation.
# Uses os._exit to force-terminate without waiting for daemon threads / worker
# pools that MinerU's pipeline may leave running.
_MINERU_WORKER_SCRIPT = '''
import json, sys, os
from pathlib import Path
from mineru.cli.common import do_parse

pdf_path, result_dir, start, end_str = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
end = None if end_str == "none" else int(end_str)
do_parse(
    result_dir,
    pdf_file_names=[Path(pdf_path).name],
    pdf_bytes_list=[Path(pdf_path).read_bytes()],
    p_lang_list=["en"],
    formula_enable=True,
    table_enable=True,  # Note: server path uses config (default False) — see RDR-046 RF-2
    start_page_id=start,
    end_page_id=end,
)
os._exit(0)
'''

_log = structlog.get_logger(__name__)


# nexus-2fyb code-review R1-I3: progress messages are interactive UX for
# long-running PDF extractions (Docling layout pass, MinerU per-page
# inference). They MUST also go through structlog so non-interactive
# callers (library use, MCP server, batch jobs) capture them in structured
# logs. Setting NEXUS_PDF_PROGRESS_QUIET=1 disables the stderr write
# entirely (e.g. for tests that capture stderr).
import os as _os


def _progress(msg: str) -> None:
    """Emit a progress event via structlog AND optionally to stderr.

    Stderr writes are gated by ``NEXUS_PDF_PROGRESS_QUIET`` env var so
    tests and library callers can suppress the interactive output without
    losing the structured log event. This replaces the prior plain
    ``print()`` which violated the project's no-print-in-library-code
    rule and made tests that captured stderr brittle.
    """
    _log.info("pdf_extractor_progress", message=msg.strip())
    if _os.environ.get("NEXUS_PDF_PROGRESS_QUIET") != "1":
        print(msg, file=sys.stderr, flush=True)


# nexus-2fyb code-review R5-I2: chained exceptions from MinerU/httpx can
# include the configured pdf.mineru_server_url. If a user (mis-)configured
# that URL with embedded credentials (http://user:pass@host/...), those
# credentials would surface in error messages, structlog events, and
# downstream log sinks. Redact userinfo from any URL we surface.
_URL_CREDENTIALS_PATTERN = re.compile(
    r"(https?://)([^/\s@]+)@",  # capture scheme + userinfo segment
)


def _redact_url_credentials(text: str) -> str:
    """Replace ``http://user:pass@host`` with ``http://[redacted]@host`` in *text*.

    Used in error-message construction to avoid leaking credentials that
    a user may have configured into ``pdf.mineru_server_url``.
    """
    return _URL_CREDENTIALS_PATTERN.sub(r"\1[redacted]@", text)


# Block-style formula delimiters — counted once per block.
_FORMULA_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\$.+?\$\$", re.DOTALL),                                  # $$...$$
    re.compile(r"\\\(.+?\\\)", re.DOTALL),                                  # \(...\)
    re.compile(r"\\\[.+?\\\]", re.DOTALL),                                  # \[...\]
    re.compile(r"\\begin\{equation\*?\}.+?\\end\{equation\*?\}", re.DOTALL),
    re.compile(r"\\begin\{align\*?\}.+?\\end\{align\*?\}", re.DOTALL),
)

# Command tokens — counted independently of containing blocks. Each
# occurrence inside or outside a block is one marker. This is intentional:
# the original nexus-2fyb bug shape was the alternation pattern below,
# which used a single re.findall and let `\$\$.+?\$\$` consume the whole
# block — `\frac` instances inside were never separately counted (4 markers
# returned for a paper with 12 \frac calls). Counting commands independently
# avoids that undercount and gives the routing decision a true signal.
#
# Patterns use \b (word boundary) rather than requiring an immediate `{`
# because MinerU emits LaTeX with whitespace between the command and its
# argument: ``\\frac { 1 } { m }`` (note spaces), so `\\frac\{` would
# match zero of those. \b matches between the word `\\frac` and the
# subsequent non-word character (space, `{`, `(`, etc.). This was an
# adjacent regression to the C1 bug — the regex assumed Docling-shaped
# LaTeX and silently undercounted on MinerU output.
_FORMULA_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\\frac\b"),       # fraction
    re.compile(r"\\sum\b"),        # summation
    re.compile(r"\\int\b"),        # integral
    re.compile(r"\\prod\b"),       # product
    re.compile(r"\\partial\b"),    # partial derivative
    re.compile(r"\\nabla\b"),      # nabla/gradient
    re.compile(r"\\mathbb\b"),     # blackboard bold
    re.compile(r"\\mathcal\b"),    # calligraphic
)

# Unicode math symbols that indicate formula content in raw PDF text.
# These are present in the PDF's embedded text even without enrichment.
_MATH_UNICODE = frozenset("∑∫∏∀∃∈∉∪∩⊆⊇⊂⊃→←↔∧∨¬⇒⇔⇐∂∇≤≥≠±×÷√∞≈≡∝∅⊕⊗⊥∥")


def _count_formula_markers(text: str) -> int:
    """Count LaTeX formula markers in *text*.

    Used as the routing heuristic in auto-mode extraction; ``count >= 5``
    escalates to MinerU. The count is the sum of two independent measures:

    1. **Block delimiters** (``$$..$$``, ``\\(..\\)``, ``\\[..\\]``, equation
       and align environments) — each delimited block contributes 1.
    2. **Command tokens** (``\\frac``, ``\\sum``, ``\\int``, etc.) — each
       occurrence contributes 1, *including* occurrences inside delimited
       blocks. A ``$$..$$`` block with three ``\\frac`` calls inside
       therefore contributes 4 (1 block + 3 fracs).

    The deliberate double-counting of commands inside blocks is the fix for
    nexus-2fyb's adjacent bug shape: prior versions used one alternation
    pattern with ``re.findall``, which would consume the whole block as a
    single match and skip the commands inside, undercounting by an order
    of magnitude on math papers.
    """
    count = 0
    for pat in _FORMULA_BLOCK_PATTERNS:
        count += len(pat.findall(text))
    for pat in _FORMULA_COMMAND_PATTERNS:
        count += len(pat.findall(text))
    return count


def _has_formulas_quick(pdf_path: Path) -> int:
    """Quick formula detection via raw PDF text Unicode math symbols.

    Uses pymupdf to extract raw text (~0.1s) and counts Unicode math symbols.
    Returns the count. A threshold of >=5 indicates a formula-containing paper.
    """
    try:
        import pymupdf
        with pymupdf.open(pdf_path) as doc:
            count = 0
            for page in doc:
                text = page.get_text()
                count += sum(1 for c in text if c in _MATH_UNICODE)
                if count >= 5:
                    return count  # early exit
            return count
    except Exception:
        return 0


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


# Known operator names that MinerU/UniMERNet may emit as spaced single chars
# inside \operatorname{...} or \operatorname*{...} -- e.g. ``{ m a x }`` -> max.
# The rejoin is scoped to this allowlist so genuinely separate tokens are not
# silently merged via the explicit rejoin path.  Unknown content still has its
# surrounding whitespace collapsed by Rule 3 (all-whitespace strip within formula
# context).
_KNOWN_OPERATOR_NAMES: frozenset[str] = frozenset({
    "arg", "argmax", "argmin", "cos", "deg", "det", "diag",
    "exp", "inf", "lim", "ln", "log",
    "max", "min", "rank", "sign", "sin", "softmax", "sup", "tan", "tr",
})


def normalize_latex_spacing(s: str) -> str:
    """Normalize MinerU/UniMERNet spaced-token LaTeX formula string.

    MinerU/UniMERNet emits formula LaTeX with spurious whitespace between
    commands and their tokens -- e.g. ``\\operatorname* { m a x }``,
    ``\\mathbf { s }``, ``Q _ { \\phi }``.  This normalizer collapses those
    spaces so stored chunks render correctly.

    Conservative rules (closes #1049):
    - ``{ \\bf X }`` -> ``\\mathbf{X}``
    - ``\\operatorname*{ m a x }`` / ``\\operatorname{ m i n }`` -- rejoin
      spaced single-char tokens when the joined result is in
      ``_KNOWN_OPERATOR_NAMES`` (e.g. ``max``, ``min``, ``argmax``).
      Unknown operatorname content is left to the whitespace-collapse step.
    - Collapse all remaining whitespace within the formula.
    - ``\\text{...}`` groups are protected: internal spaces are preserved.

    Idempotent: running twice produces the same result as running once.

    Designed for formula strings, not prose.  At the wiring level
    (``_normalize_mineru_latex``) this is called only within ``$...$`` and
    ``$$...$$`` delimiters so prose chunks are never touched.
    """
    # Rule 1: { \\bf token } -> \\mathbf{token}
    # Handles MinerU's {\\bf X} legacy-font group notation.
    s = re.sub(r"\{\s*\\bf\s+(\S+)\s*\}", r"\\mathbf{\1}", s)

    # Rule 2: \\operatorname*{ m a x } -> \\operatorname*{max}
    # Rejoin scoped to \\operatorname / \\operatorname* with an allowlist so that
    # genuinely separate single-char tokens are not silently merged.
    def _rejoin_opname(m: re.Match) -> str:
        cmd = m.group(1)
        content = m.group(2).strip()
        parts = content.split()
        if parts and all(len(p) == 1 for p in parts):
            joined = "".join(parts)
            if joined in _KNOWN_OPERATOR_NAMES:
                return f"{cmd}{{{joined}}}"
        return m.group(0)

    s = re.sub(r"(\\operatorname\*?)\s*\{([^}]+)\}", _rejoin_opname, s)

    # Protect \text{...} groups: save them as placeholders so whitespace
    # stripping below does not collapse spaces inside \text{some words}.
    _placeholders: list[str] = []

    def _save_text(m: re.Match) -> str:
        _placeholders.append(m.group(0))
        return f"\x00T{len(_placeholders) - 1}\x00"

    s = re.sub(r"\\text\{[^}]*\}", _save_text, s)

    # Rule 3: collapse all whitespace in the formula string.
    # Safe because this function is only called on formula content, not prose.
    s = re.sub(r"\s+", "", s)

    # Restore \text{...} groups with their original internal spacing.
    for i, t in enumerate(_placeholders):
        s = s.replace(f"\x00T{i}\x00", t)

    return s


def _normalize_mineru_latex(md: str) -> str:
    """Apply ``normalize_latex_spacing`` within LaTeX formula blocks in markdown.

    Scopes the normalizer to ``$$...$$`` (display math) and ``$...$`` (inline
    math) delimiters so that prose text is never modified.  Idempotent.

    Called from ``PDFExtractor._extract_with_mineru`` on each per-page
    markdown fragment (batch loop and OOM-retry path) before the length
    is measured for ``per_page_lengths``/``page_boundaries``, so the
    stored offsets are consistent with the normalized text.  Existing
    indexed chunks need a re-index to pick up clean LaTeX.
    """
    # Display math first (greedy match would eat $...$).
    md = re.sub(
        r"\$\$(.*?)\$\$",
        lambda m: f"$${normalize_latex_spacing(m.group(1))}$$",
        md,
        flags=re.DOTALL,
    )
    # Inline math \u2014 [^$] avoids matching across $$ boundaries.
    md = re.sub(
        r"\$([^$]+?)\$",
        lambda m: f"${normalize_latex_spacing(m.group(1))}$",
        md,
    )
    return md


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
        self._mineru_server_checked: bool = False
        self._mineru_server_up: bool = False
        self._mineru_server_restarts: int = 0

    def extract(
        self,
        pdf_path: Path,
        *,
        extractor: str = "auto",
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
        """Extract text from *pdf_path*. Returns ExtractionResult.

        *extractor* selects the backend:
        - ``"auto"`` — Docling pass (enriched, to detect formulas); if
          formulas found, try MinerU then fall back to PyMuPDF normalized.
        - ``"docling"`` — Docling with PyMuPDF normalized fallback.
        - ``"mineru"`` — MinerU directly (no fallback).

        *on_page* — optional streaming callback fired per extracted page (or
        per MinerU batch when ``mineru_page_batch > 1``):
        ``on_page(page_index, page_text, page_metadata)``.
        ``page_metadata`` contains ``"page_number"`` (1-based) and
        ``"text_length"``.
        """
        if extractor not in ("auto", "docling", "mineru"):
            raise ValueError(
                f"extractor must be 'auto', 'docling', or 'mineru'; got {extractor!r}"
            )

        # nexus-2fyb code-review R1-I2: validate the path is readable before
        # dispatching. Without this, a directory or dangling symlink reaches
        # pymupdf/Docling and produces an opaque internal error that leaks
        # library paths through the message.
        if not pdf_path.is_file():
            raise FileNotFoundError(
                f"PDF not found or not a regular file: {pdf_path}"
            )

        if extractor == "docling":
            _progress(f"  Docling: extracting {pdf_path.name}…")
            try:
                return self._extract_with_docling(pdf_path, on_page=on_page)
            except Exception as exc:
                _progress(f"  Docling failed ({type(exc).__name__}), falling back to PyMuPDF: {pdf_path.name}")
                _log.debug("docling_extraction_failed", error=str(exc), path=str(pdf_path))
                return self._extract_normalized(pdf_path, on_page=on_page)

        if extractor == "mineru":
            _progress(f"  MinerU: extracting {pdf_path.name}…")
            return self._extract_with_mineru(pdf_path, on_page=on_page)

        # extractor == "auto"
        # Step 1: Quick formula pre-screen via raw PDF text (~0.1s)
        formula_count = _has_formulas_quick(pdf_path)

        # Step 2: Extract with non-enriched Docling (probe — no on_page callback
        # to avoid double-firing if MinerU takes over for formula PDFs)
        _progress(f"  Docling: extracting {pdf_path.name}…")
        try:
            fast_result = self._extract_with_docling(pdf_path, enriched=False)
        except Exception as exc:
            _progress(f"  Docling failed ({type(exc).__name__}), falling back to PyMuPDF: {pdf_path.name}")
            _log.debug("docling_auto_pass_failed", error=str(exc), path=str(pdf_path))
            return self._extract_normalized(pdf_path, on_page=on_page)

        # Also check the Docling markdown for LaTeX markers (catches formulas
        # that Docling renders as LaTeX even without enrichment)
        text_markers = _count_formula_markers(fast_result.text)
        formula_count = max(formula_count, text_markers)

        if formula_count < 5:
            # Docling wins — replay on_page from page_boundaries since the
            # probe pass didn't fire the callback.
            if on_page is not None:
                for boundary in fast_result.metadata.get("page_boundaries", []):
                    page_num = boundary["page_number"]
                    start = boundary["start_char"]
                    length = boundary["page_text_length"] - 1  # -1 for \n separator
                    page_text = fast_result.text[start : start + length]
                    on_page(page_num - 1, page_text, {"page_number": page_num, "text_length": length})
            return fast_result

        # Math paper detected — switch to MinerU for formula-aware extraction.
        # nexus-2fyb: previously, a MinerU failure here silently returned the
        # non-enriched Docling probe (formulas already stripped). That hid
        # extraction corruption from every caller — the result was
        # indistinguishable from a paper that legitimately had no math. Auto
        # mode now fails loudly so the user installs MinerU or explicitly opts
        # into formula-stripped extraction with --extractor docling.
        _progress(f"  Formulas detected ({formula_count}) — switching to MinerU: {pdf_path.name}")
        try:
            return self._extract_with_mineru(pdf_path, formula_count=formula_count, on_page=on_page)
        except ImportError as exc:
            # do_parse is None — mineru is a default dep since nexus-2fyb so a
            # missing import means the conexus install itself is corrupt.
            _log.error(
                "mineru_import_failed",
                error=str(exc),
                formula_count=formula_count,
                path=str(pdf_path),
            )
            raise RuntimeError(
                f"PDF {pdf_path.name} contains formulas (detected {formula_count}) "
                f"but MinerU is not importable: {exc}. "
                f"MinerU is a required dependency since nexus-2fyb; if it is "
                f"missing your conexus install is corrupt — reinstall with "
                f"`uv tool install --reinstall conexus`. To bypass formula "
                f"extraction entirely, rerun with `--extractor docling`."
            ) from exc
        except Exception as exc:
            # MinerU is installed but extraction failed — subprocess timeout,
            # OOM kill, mineru-api server error, etc. Do NOT advise reinstall;
            # the install is fine and the failure is operational.
            sanitized_msg = _redact_url_credentials(str(exc))
            _log.error(
                "mineru_extraction_failed",
                error=sanitized_msg,
                error_type=type(exc).__name__,
                formula_count=formula_count,
                path=str(pdf_path),
            )
            raise RuntimeError(
                f"PDF {pdf_path.name} contains formulas (detected {formula_count}) "
                f"but MinerU extraction failed: {type(exc).__name__}: {sanitized_msg}. "
                f"To bypass formula extraction and accept formula-stripped "
                f"output for this PDF, rerun with `--extractor docling`."
            ) from exc

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

    def _extract_with_docling(
        self,
        pdf_path: Path,
        *,
        enriched: bool = True,
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
        """Extract per-page markdown via Docling."""
        result = self._get_converter(enriched=enriched).convert(str(pdf_path))
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
                if on_page is not None:
                    on_page(p - 1, page_md, {"page_number": p, "text_length": len(page_md)})
                page_texts.append(page_md)
                current_pos += len(page_md) + 1

        text = "\n".join(page_texts)
        if not text.strip():
            raise RuntimeError("docling produced empty output")

        # Collect TableItem regions and count formulas
        table_regions: list[dict] = []
        formula_count = 0
        if enriched:
            # Enriched mode: count FormulaItem objects (duck-typed, single pass)
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
        else:
            # Non-enriched mode: scan text for LaTeX formula patterns
            # This is 100x faster than running the enrichment pipeline
            formula_count = _count_formula_markers(text)
            for item, _ in doc.iterate_items():
                if type(item).__name__ == "TableItem":
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

    # Page batch size is read from config via get_mineru_page_batch() (default 1).
    # Formula-dense PDFs OOM during MFR prediction at larger batch sizes.

    def _extract_with_mineru(
        self,
        pdf_path: Path,
        *,
        formula_count: int = 0,
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
        """Extract text via MinerU (math-aware, optional dependency).

        Each page-range batch runs in a **subprocess** so that MinerU's
        GPU/model memory is fully reclaimed between batches.  Without this,
        memory accumulates across in-process ``do_parse`` calls and large
        formula-dense PDFs get OOM-killed.

        OOM retry: if a multi-page batch fails, retries at 1-page granularity.
        Single-page failures propagate immediately (no infinite retry).

        *on_page* fires once per batch (default batch size is 1 page via
        ``mineru_page_batch`` config).  The callback receives the batch start
        page index, the batch markdown, and metadata.
        """
        if do_parse is None:
            raise ImportError(
                "MinerU is not importable but is a required dependency since "
                "nexus-2fyb. Reinstall conexus: `uv tool install --reinstall conexus`."
            )

        import pymupdf  # lightweight — only used for page count

        with pymupdf.open(pdf_path) as doc:
            total_pages = len(doc)

        from nexus.config import get_mineru_page_batch
        batch_size = get_mineru_page_batch()

        batches: list[tuple[int, int | None]] = []
        if total_pages <= batch_size:
            batches.append((0, None))
        else:
            _log.info(
                "mineru_splitting_large_pdf",
                total_pages=total_pages,
                batch_size=batch_size,
                path=str(pdf_path),
            )
            for start in range(0, total_pages, batch_size):
                batches.append((start, min(start + batch_size, total_pages)))

        md_parts: list[str] = []
        all_content_list: list[dict] = []
        all_pdf_info: list[dict] = []
        # nexus-2fyb code-review C2: track real per-page markdown lengths
        # so page_boundaries reflect actual content distribution, not a
        # uniform char/page average. Each entry: (page_index_0based, length).
        # OOM retry loop appends per-page entries directly; batch-mode
        # appends one entry covering the batch span.
        per_page_lengths: list[tuple[int, int]] = []

        fname = pdf_path.name
        for batch_idx, (start, end) in enumerate(batches):
            label = f"{start + 1}–{end}" if end is not None else f"{start + 1}–{total_pages}"
            _progress(
                f"  MinerU: page {start + 1}/{total_pages} ({fname})",
            )
            _log.info("mineru_batch", pages=label, path=str(pdf_path))
            try:
                md, content_list, pdf_info = self._mineru_run_isolated(pdf_path, start, end)
            except RuntimeError:
                # OOM or subprocess failure — retry at 1-page granularity.
                # This catches both subprocess OOM (exit code -9) and server-crash
                # fallback failures. Single-page retries that also fail propagate
                # immediately (span <= 1 → re-raise), so the document fails cleanly.
                span = (end or total_pages) - start
                if span <= 1:
                    raise  # already single-page, no retry possible
                _log.warning(
                    "mineru_oom_retry",
                    pages=label,
                    path=str(pdf_path),
                    original_batch=span,
                )
                for page in range(start, end or total_pages):
                    _progress(
                        f"  MinerU: page {page + 1}/{total_pages} (retry, {fname})",
                    )
                    md, content_list, pdf_info = self._mineru_run_isolated(
                        pdf_path, page, page + 1,
                    )
                    # Normalize before measuring length so per_page_lengths
                    # is consistent with the stored normalized text.
                    md = _normalize_mineru_latex(md)
                    if on_page is not None:
                        on_page(page, md, {"page_number": page + 1, "text_length": len(md)})
                    md_parts.append(md)
                    all_content_list.extend(content_list)
                    all_pdf_info.extend(pdf_info)
                    per_page_lengths.append((page, len(md)))
                continue
            # Normalize before measuring length so per_page_lengths
            # is consistent with the stored normalized text.
            md = _normalize_mineru_latex(md)
            if on_page is not None:
                # Note: for batch_size > 1, fires once per batch with the batch's
                # combined markdown and start page index. Page-number metadata is
                # only accurate when batch_size=1 (the default). The streaming
                # pipeline relies on this default for correct page attribution.
                on_page(start, md, {"page_number": start + 1, "text_length": len(md)})
            md_parts.append(md)
            all_content_list.extend(content_list)
            all_pdf_info.extend(pdf_info)
            # For batch_size==1 (the default), end == start+1 and this is
            # exact. For batch_size>1, the batch md covers `batch_pages`
            # pages and we distribute it uniformly within the batch (the
            # only resolution available without per-page md from MinerU).
            batch_end = end if end is not None else total_pages
            batch_pages = batch_end - start
            if batch_pages == 1:
                per_page_lengths.append((start, len(md)))
            elif batch_pages > 1:
                per_page = len(md) // batch_pages
                remainder = len(md) % batch_pages
                for offset in range(batch_pages):
                    extra = 1 if offset < remainder else 0
                    per_page_lengths.append((start + offset, per_page + extra))

        if batches:
            _progress(f"  MinerU: {total_pages}/{total_pages} done ({fname})")

        md_text = "\n".join(md_parts)
        return self._mineru_build_result(
            pdf_path, md_text, all_content_list, all_pdf_info,
            per_page_lengths=per_page_lengths,
            formula_count_floor=formula_count,
        )

    def _mineru_server_available(self) -> bool:
        """Check if the MinerU API server is reachable.

        Result cached for the lifetime of this PDFExtractor instance —
        a False result is never retried. Create a new instance to re-check.

        nexus-h1jk: when the configured URL is unreachable, emit a
        structured warning + ``_progress`` line so the operator knows
        the run is silently degrading to the in-process subprocess
        path (where math-heavy / large PDFs OOM-kill the worker).
        The auto-restart machinery writes the live port back to
        ``~/.config/nexus/config.yml``, so a stale URL persists across
        sessions when the server is later killed.
        """
        if self._mineru_server_checked:
            return self._mineru_server_up

        from nexus.config import get_mineru_server_url
        url = f"{get_mineru_server_url()}/health"
        try:
            resp = httpx.get(url, timeout=2)
            self._mineru_server_up = resp.status_code == 200
            if not self._mineru_server_up:
                _log.warning(
                    "mineru_server_unhealthy",
                    url=url, http_status=resp.status_code,
                )
                _progress(
                    f"  warn: MinerU server at {url} returned HTTP "
                    f"{resp.status_code}; falling back to in-process subprocess. "
                    f"Run `nx mineru start` to enable server mode."
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            self._mineru_server_up = False
            _log.warning(
                "mineru_server_unreachable",
                url=url, error=f"{type(exc).__name__}: {exc}",
            )
            _progress(
                f"  warn: MinerU server at {url} unreachable; falling back "
                f"to in-process subprocess (slower, OOM-risk on large math PDFs). "
                f"Run `nx mineru start` to enable server mode."
            )

        self._mineru_server_checked = True
        return self._mineru_server_up

    def _mineru_run_via_server(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Extract via MinerU HTTP server (POST /file_parse)."""
        from nexus.config import get_mineru_server_url, get_mineru_table_enable

        url = f"{get_mineru_server_url()}/file_parse"
        with pdf_path.open("rb") as f:
            resp = httpx.post(
                url,
                files=[("files", (pdf_path.name, f, "application/pdf"))],
                data={
                    "backend": "pipeline",
                    "start_page_id": str(start),
                    "end_page_id": str(end if end is not None else 99999),
                    "formula_enable": "true",
                    "table_enable": str(get_mineru_table_enable()).lower(),
                    "return_md": "true",
                    "return_middle_json": "true",
                    "return_content_list": "true",
                    "parse_method": "auto",
                    "lang_list": "en",
                },
                timeout=300,
            )
        resp.raise_for_status()
        data = resp.json()

        all_results = data.get("results", {})
        stem = pdf_path.stem
        results = all_results.get(stem)
        if results is None:
            if len(all_results) == 1:
                results = next(iter(all_results.values()))
            else:
                raise RuntimeError(
                    f"Server results missing key {stem!r}; "
                    f"available keys: {list(all_results.keys())}"
                )

        md = results.get("md_content", "")
        if not md:
            # Empty page (image-only, blank, or figure plate) — not an error
            _log.debug("mineru_empty_page", path=str(pdf_path), start=start, end=end)
            md = ""

        raw_cl = results.get("content_list")
        raw_mj = results.get("middle_json")
        if raw_mj is None:
            _log.warning("mineru_server_no_middle_json", path=str(pdf_path))
        content_list = json.loads(raw_cl) if raw_cl else []
        middle = json.loads(raw_mj) if raw_mj else {}
        return md, content_list, middle.get("pdf_info", [])

    _MINERU_MAX_RESTARTS: int = 2

    def _restart_mineru_server(self) -> bool:
        """Attempt to restart the MinerU server after a crash.

        Returns True if the server was restarted and is healthy.
        Limited to _MINERU_MAX_RESTARTS per PDFExtractor instance.
        """
        if self._mineru_server_restarts >= self._MINERU_MAX_RESTARTS:
            _log.warning("mineru_restart_budget_exhausted",
                         restarts=self._mineru_server_restarts)
            return False

        self._mineru_server_restarts += 1
        _log.info("mineru_server_restarting",
                  attempt=self._mineru_server_restarts)

        # nexus-8g79.10 (V4): import process primitives from the
        # lower-layer module. ``_pid_file_path`` is still in commands/
        # because ``nx mineru start/stop`` owns the lifecycle; the
        # library only reads. Path is also available via
        # ``nexus._mineru_pid._pid_file_path``.
        from nexus._mineru_pid import (
            _pid_file_path,
            is_process_alive,
            read_pid_file,
        )

        # Clean up stale PID file if the server is dead
        info = read_pid_file()
        if info is not None and not is_process_alive(info["pid"]):
            _pid_file_path().unlink(missing_ok=True)

        # Start a new server via the same logic as `nx mineru start`
        import subprocess as _sp
        import time as _time
        from nexus.commands.mineru import (
            _HEALTH_POLL_INTERVAL,
            _find_free_port,
            _mineru_output_root,
            _resolve_mineru_api_bin,
            _server_env,
        )
        # GH #1059: resolve mineru-api from the venv bin first, then PATH.
        mineru_bin = _resolve_mineru_api_bin()
        if mineru_bin is None:
            _log.warning("mineru_restart_failed", reason="mineru-api not found")
            return False
        port = _find_free_port()
        cmd = [mineru_bin, "--host", "127.0.0.1", "--port", str(port)]
        # nexus-2fyb code-review C-sec-1: previously called _server_env() with
        # no arguments, but the function signature requires output_root. This
        # was a TypeError waiting to fire on the first server crash during a
        # multi-PDF run, AND a security bug — without MINERU_API_OUTPUT_ROOT
        # set, MinerU falls back to its default world-writable /tmp/mineru-
        # output instead of the per-user 0o700 directory.
        output_root = _mineru_output_root()
        try:
            proc = _sp.Popen(
                cmd, env=_server_env(output_root),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError):
            _log.warning("mineru_restart_failed", reason="mineru-api not found")
            return False

        # Poll health for up to 60s (models already cached in memory by OS)
        url = f"http://127.0.0.1:{port}/health"
        deadline = _time.monotonic() + 60
        while _time.monotonic() < deadline:
            if proc.poll() is not None:
                _log.warning("mineru_restart_failed", reason="process exited")
                return False
            try:
                resp = httpx.get(url, timeout=2)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            _time.sleep(_HEALTH_POLL_INTERVAL)
        else:
            _log.warning("mineru_restart_failed", reason="health timeout")
            return False

        # Write PID file only (canonical source of truth). nexus-oa7r:
        # do NOT write the port to persistent config — the PID-file
        # lookup in ``get_mineru_server_url`` discovers the live port
        # at every call. Persisting ephemeral ports drifted across
        # reboots.
        import json as _json
        from datetime import datetime, timezone
        pid_path = _pid_file_path()
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(_json.dumps({
            "pid": proc.pid, "port": port,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }))

        # Reset availability cache
        self._mineru_server_checked = True
        self._mineru_server_up = True
        _log.info("mineru_server_restarted", pid=proc.pid, port=port)
        return True

    def _mineru_run_isolated(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Dispatch to server or subprocess based on server availability."""
        if self._mineru_server_available():
            try:
                return self._mineru_run_via_server(pdf_path, start, end)
            except (httpx.ConnectError, httpx.TimeoutException,
                    httpx.RemoteProtocolError) as exc:
                # Server crashed — invalidate cache, try restart
                self._mineru_server_checked = True
                self._mineru_server_up = False
                _log.warning("mineru_server_lost", path=str(pdf_path),
                             pages=f"{start}–{end}", error=str(exc))
                if self._restart_mineru_server():
                    # Retry this page on the new server
                    try:
                        return self._mineru_run_via_server(pdf_path, start, end)
                    except Exception:
                        pass  # fall through to subprocess
                return self._mineru_run_subprocess(pdf_path, start, end)
            except httpx.HTTPStatusError as exc:
                _log.warning("mineru_server_error", path=str(pdf_path),
                             pages=f"{start}–{end}", error=str(exc))
                return self._mineru_run_subprocess(pdf_path, start, end)
        return self._mineru_run_subprocess(pdf_path, start, end)

    def _mineru_run_subprocess(
        self, pdf_path: Path, start: int, end: int | None,
    ) -> tuple[str, list[dict], list[dict]]:
        """Run MinerU in a fresh OS process for full memory isolation.

        Uses ``subprocess.run`` with an inline Python script so the child
        loads MinerU models independently.  When the child exits, all
        GPU/model memory is reclaimed by the OS — no leaks across batches.
        """
        result_dir = tempfile.mkdtemp()
        try:
            import os as _os
            import signal

            proc = subprocess.Popen(
                [
                    sys.executable, "-c", _MINERU_WORKER_SCRIPT,
                    str(pdf_path), result_dir,
                    str(start), "none" if end is None else str(end),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # own process group
            )
            # Use killpg(getpgid(pid)) rather than killpg(pid) directly —
            # with start_new_session=True the pgid equals pid at spawn time,
            # but by the time we kill the child may be dead and the PID
            # recycled by the kernel. getpgid() resolves the current pgid
            # from the live PID slot (raises ProcessLookupError if the
            # process is already gone, which we swallow). Matches the
            # session.py:301 idiom (indexing review C1).
            def _killpg_safe() -> None:
                # Delegated to nexus.util.process_group.safe_killpg so
                # the mock-guard + error-swallow contract is consistent
                # across every subprocess cleanup site in the codebase.
                from nexus.util.process_group import safe_killpg

                safe_killpg(proc, signal.SIGKILL)

            try:
                returncode = proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                _killpg_safe()
                proc.wait()
                raise RuntimeError(
                    f"MinerU subprocess timed out after 180s "
                    f"(pages {start}–{end}, path={pdf_path})"
                )
            if returncode != 0:
                # Clean up any orphaned children in the process group
                _killpg_safe()
                _log.error(
                    "mineru_subprocess_failed",
                    returncode=returncode,
                    pages=f"{start}–{end}",
                    path=str(pdf_path),
                )
                raise RuntimeError(
                    f"MinerU subprocess exited with code {returncode} "
                    f"(pages {start}–{end}, path={pdf_path})"
                )
            # Kill any lingering workers in the process group
            _killpg_safe()

            pdf_name = pdf_path.name
            base = Path(result_dir) / pdf_name / "auto"
            # Indexing review I2: assume MinerU's output layout but fail
            # loudly with a useful message when it diverges (e.g. version
            # upgrade changes the "auto" directory name). The subprocess
            # already exited 0, so a missing output file is an unexpected
            # state not a runtime error.
            md_file = base / f"{pdf_name}.md"
            if not md_file.exists():
                raise RuntimeError(
                    f"MinerU produced no output at {md_file} "
                    f"(subprocess exited 0; layout may have changed). "
                    f"Pages {start}–{end}, path={pdf_path}"
                )
            md = md_file.read_text(encoding="utf-8")
            content_list: list[dict] = json.loads(
                (base / f"{pdf_name}_content_list.json").read_text(encoding="utf-8")
            )
            middle: dict = json.loads(
                (base / f"{pdf_name}_middle.json").read_text(encoding="utf-8")
            )
            return md, content_list, middle.get("pdf_info", [])
        finally:
            import shutil
            shutil.rmtree(result_dir, ignore_errors=True)

    @staticmethod
    def _mineru_build_result(
        pdf_path: Path, md_text: str,
        content_list: list[dict], pdf_info: list[dict],
        *,
        per_page_lengths: list[tuple[int, int]] | None = None,
        formula_count_floor: int = 0,
    ) -> ExtractionResult:
        """Assemble an ExtractionResult from (merged) MinerU outputs.

        *per_page_lengths* (nexus-2fyb code-review C2): list of
        ``(page_index_0based, markdown_char_length)`` tuples captured from
        the batch loop. Used to build accurate ``page_boundaries`` so
        chunks get correct ``page_number`` attribution. When ``None``
        (legacy callers, defensive), falls back to uniform char/page
        distribution — but logs a warning because that path produces
        wrong page_number metadata for any non-uniform document.

        *formula_count_floor* (nexus-2fyb code-review R1): the count
        produced by the auto-mode probe, used as a lower bound. If
        MinerU's structured response is missing or empty (e.g. server
        returned content_list=[] under degraded conditions), the
        recomputed formula_count would otherwise be 0, breaking the
        ``has_formulas`` flag downstream for confirmed math papers.
        """
        display_count = sum(1 for e in content_list if e.get("type") == "equation")

        inline_count = 0
        for page in pdf_info:
            for block in page.get("para_blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("type") == "inline_equation":
                            inline_count += 1

        formula_count = max(display_count + inline_count, formula_count_floor)
        page_count = len(pdf_info)

        # md_text is already normalized: _extract_with_mineru applies
        # _normalize_mineru_latex per-page so per_page_lengths and
        # page_boundaries are consistent with the stored text.
        total_len = len(md_text)

        page_boundaries: list[dict] = []
        if per_page_lengths is not None and page_count > 0:
            # Use the real per-page lengths captured from the batch loop.
            # md_text is "\n".join(md_parts), so each per-page segment has
            # +1 separator except the last. start_char accumulates.
            page_lengths_by_idx = {idx: length for idx, length in per_page_lengths}
            pos = 0
            for i in range(page_count):
                length = page_lengths_by_idx.get(i, 0)
                # Add +1 for the "\n" separator (matches the join), except final.
                stored_length = length + (1 if i < page_count - 1 else 0)
                page_boundaries.append({
                    "page_number": i + 1,
                    "start_char": pos,
                    "page_text_length": stored_length,
                })
                pos += stored_length
        elif page_count > 0 and total_len > 0:
            # Fallback (legacy callers, no per-batch tracking). Uniform
            # distribution gives wrong page_number for non-uniform docs.
            _log.warning(
                "mineru_uniform_page_boundaries",
                path=str(pdf_path),
                page_count=page_count,
                reason="per_page_lengths not provided",
            )
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

    def _extract_normalized(
        self,
        pdf_path: Path,
        *,
        on_page: Callable[[int, str, dict], None] | None = None,
    ) -> ExtractionResult:
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
                    if on_page is not None:
                        on_page(page_num, page_text, {"page_number": page_num + 1, "text_length": len(page_text)})
                    text_parts.append(page_text)
                    current_pos += len(page_text) + 1

        text = "\n".join(text_parts)
        if not text.strip():
            # nexus-aold: silent zero-chunk indexing was the failure
            # mode of large-PDF Docling crashes that cascaded into
            # the PyMuPDF fallback returning an empty result. Make
            # it a hard error here too, mirroring the equivalent
            # guard in _extract_with_docling. The indexer's outer
            # error path will surface this as a non-zero exit with
            # a named failure mode (was: silent 0 chunks indexed).
            raise RuntimeError(
                f"pymupdf produced empty output for {pdf_path.name} "
                f"(page_count={page_count}); the PDF may be image-only "
                "or have a damaged text layer. Try --extractor mineru "
                "or rerun OCR before indexing."
            )

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
