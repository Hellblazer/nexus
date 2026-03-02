---
title: "pdfplumber Extraction Tier for Complex-Table PDFs"
id: RDR-012
type: architecture
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-01
updated: 2026-03-01
accepted_date: "2026-03-01"
closed_date: "2026-03-01"
close_reason: "implemented"
gate_result: "BLOCKED 2026-03-01 — revised"
gate_date: ""
related_issues: []
---

## RDR-012: pdfplumber Extraction Tier for Complex-Table PDFs

## Summary

Nexus's PDF extractor has two tiers: pymupdf4llm markdown (primary) and pymupdf
normalized (fallback). PDFs with complex tables — multi-column layouts, merged cells,
scientific data tables — produce garbled or flattened output from both tiers.

This RDR adds pdfplumber as a quality-rescue fallback: pymupdf4llm runs first (as
always), and pdfplumber is invoked only when a post-extraction quality check confirms
the markdown output is missing expected table structure. Whitespace normalization
improvements are also included.

## Problem

Current extraction failure modes for table-heavy PDFs:
- **pymupdf4llm**: Even with layout mode (PR #49), the GNN can mis-classify table cells
  as body text, producing merged sentences or skipped rows.
- **pymupdf normalized**: `get_text(sort=True)` flattens multi-column tables into
  interleaved column text.
- **Downstream effect**: Table chunks contain garbled content, poisoning search results
  for PDFs with data tables (research papers, financial reports, technical specs).

## Proposed Solution

Add pdfplumber as a quality-rescue fallback invoked *after* pymupdf4llm has been
attempted and confirmed to produce poor table output:

```
Tier 1: PyMuPDF4LLM markdown    — always attempted first; layout analysis via PR #49
   ↓ Type3 fonts detected → Tier 3 directly
   ↓ font RuntimeError   → Tier 3 directly
   ↓ empty output        → Tier 3 directly
   ↓ success: run _markdown_misses_tables(pdf_path, result.text)
      → True (tables detected, markdown lacks pipe chars) → Tier 2
      → False (no tables, or markdown has table syntax)  → return result

Tier 2: pdfplumber              — table-aware rescue; extract_tables() + Markdown
   ↓ empty output or fails → Tier 3

Tier 3: PyMuPDF normalized      — final fallback; raw get_text(sort=True)
```

Key design properties:
- **Attempt-first**: pymupdf4llm with layout mode always runs first; pdfplumber is a
  rescue path, not a pre-emptive bypass.
- **Single pdfplumber open**: detection uses PyMuPDF native `page.find_tables()`, so
  pdfplumber is opened only when actually needed for extraction.
- **Graceful degradation**: if pdfplumber is not installed, `_markdown_misses_tables`
  returns False and the pdfplumber path is never reached.

## Research Findings

Arcaneum's extractor was reviewed (`arcaneum/src/arcaneum/indexing/pdf/extractor.py`).
It contains `_has_complex_tables()` and `_extract_with_pdfplumber()` methods, but as of
review (2026-03-01) these methods are defined but **never called from `extract()`** —
they have zero callers and zero test coverage. They represent a design sketch, not a
validated reference implementation.

What the review confirmed:
- **`_extract_with_pdfplumber()` approach**: Per-page `extract_text()` for prose +
  `extract_tables()` for tables, tables formatted as Markdown rows. Tables appended
  after prose block. This is a reasonable extraction pattern.
- **`_normalize_whitespace_edge_cases()`**: Handles tabs (`\t`), Unicode non-breaking
  spaces (`\u00a0`), and 4+ consecutive newlines. Not present in the current Nexus
  normalizer.
- **Dependency**: `pdfplumber>=0.11.7` — lightweight, no system binaries required.
- **PyMuPDF native table detection**: PyMuPDF 1.27.1 (installed) exposes
  `Page.find_tables()` with the same algorithm as pdfplumber's table detector. Using
  it for the quality check avoids a pdfplumber open just for detection.

OCR (Tesseract/EasyOCR + pdf2image + cv2) was reviewed and explicitly deferred:
system binary dependency, large model downloads (~200MB), multiprocessing complexity.
Better handled as a separate opt-in RDR.

## Alternatives Considered

**Option A — Full stack parity (4 tiers, includes OCR)**: Rejected — heavy deps.

**Option C — Whitespace normalization only**: Rejected — misses the table problem.

**Pre-emptive routing (original Option B)**: Route to pdfplumber *before* attempting
pymupdf4llm when `_has_complex_tables()` fires. Rejected after gate critique: this
bypasses pymupdf4llm's layout mode (PR #49) even when it would have succeeded, and
uses pdfplumber twice (heuristic + extraction). Attempt-first is correct.

**Option B revised (selected)**: pdfplumber as quality-rescue fallback after
pymupdf4llm attempt. Detection via PyMuPDF native `find_tables()` (no extra dep).
pdfplumber opened only for extraction when needed.

## Implementation Plan

### Phase 1 — Dependency and whitespace normalization

1. Add `pdfplumber>=0.11.7` to `[project.dependencies]` in `pyproject.toml`
2. Run `uv sync` to update `uv.lock`
3. Add `_normalize_whitespace_edge_cases(text: str) -> str` module-level helper in
   `pdf_extractor.py`:
   - Replace `\t` with single space
   - Collapse Unicode whitespace variants (`[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]+`) to single space
   - Collapse 4+ consecutive newlines to `\n\n\n`
4. Apply in `_extract_normalized()` after existing regex normalization

**Success:** `uv run pytest tests/test_pdf_extractor.py -x` still passes; new unit
tests for each normalization case pass.

### Phase 2 — `_markdown_misses_tables()` quality check

Add to `PDFExtractor`. Uses PyMuPDF native `find_tables()` — no pdfplumber dependency:

```python
def _markdown_misses_tables(self, pdf_path: Path, markdown_text: str) -> bool:
    """Return True if the PDF has ruled tables that are absent from the markdown.

    Uses PyMuPDF's native find_tables() (same algorithm as pdfplumber) to count
    ruled tables on the first five pages, then checks whether the markdown output
    contains a proportional number of pipe characters. Returns False (no fallback)
    when pdfplumber is not installed.

    Note: borderless tables (spacing-only alignment, common in IEEE/ACM papers)
    produce no edges from find_tables() and will not be detected. This is a known
    scope limitation — see Risks.
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
        return pipe_count < ruled_tables * 3
    except Exception:
        return False
```

**Threshold rationale**: A well-formatted Markdown table with N columns produces
roughly 2*(N+1) pipe chars per row plus a separator row. With 3 pipes per table as
the floor, we allow for small single-column pseudo-tables while still catching
missing multi-column tables. This threshold is a new design choice, not from Arcaneum.

**5-page cap asymmetry**: Detection scans the first 5 pages only, but `pipe_count` is
measured against the entire document's markdown. This means: if a document has tables
exclusively on pages 6+, `ruled_tables` will be 0 and the function returns False (no
rescue). Tables on later pages are not rescued. This is accepted scope — rescuing late
pages would require scanning all pages, which adds cost proportional to document size.
Accepted cost: single additional `pymupdf.open()` after `_extract_markdown()` has
closed the document. For large documents, this opens and reads up to 5 pages.

**Success:** Unit tests — PDF with ruled table → returns True; plain prose PDF →
returns False; PDF with well-extracted table (pipes present) → returns False.

### Phase 3 — `_extract_with_pdfplumber()` method

Add to `PDFExtractor`:
- Open with pdfplumber; iterate pages
- Per page: collect table bounding boxes via `page.find_tables()` first
- Per page prose: use `page.filter(lambda obj: not _in_table(obj, table_bboxes)).extract_text()`
  to exclude table-region characters from prose extraction, preventing duplication of
  table cell content in both prose and table markdown. If `find_tables()` returns no
  tables, fall back to `page.extract_text(layout=True)` directly. **Note**: Arcaneum's
  `_extract_with_pdfplumber()` does NOT do this exclusion — it calls
  `extract_text(layout=True)` unconditionally, producing duplicated table cell text.
  The `page.filter()` deduplication is a Nexus improvement.
- Per page tables: `page.extract_tables()` → format each table as Markdown using
  `_format_table()`. Separator row uses ` --- ` cells (matching Arcaneum):
  ```
  | col1 | col2 |
  | --- | --- |
  | val  | val  |
  ```
  `None` cells rendered as empty string; empty tables skipped
- `page_boundaries` records `page_text_length: len(page_text) + 1` and advances
  `current_pos` by the same `+1` — following Nexus convention (Arcaneum has an
  inconsistency here: records `len(page_text)` but advances by `len(page_text) + 1`)
- Append formatted tables after prose block for the page (table is placed at end of
  page content; caption severance is a known limitation — see Risks)
- Track `page_boundaries` using the same `start_char` / `page_text_length` pattern
  as other extractors
- Metadata: `extraction_method: "pdfplumber"`, `format: "markdown"`
- If pdfplumber raises on open, propagate (caller handles via empty-output fallback)

**Success:** Unit test on a synthetic table PDF → output contains `|` characters and
`extraction_method == "pdfplumber"`; page boundaries are contiguous and cover all text.

### Phase 4 — Wire into `extract()`

Replace the current routing block in `PDFExtractor.extract()`:

```python
def extract(self, pdf_path: Path) -> ExtractionResult:
    if self._has_type3_fonts(pdf_path):
        return self._extract_normalized(pdf_path)
    try:
        result = self._extract_markdown(pdf_path)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "font" in msg or "code=4" in msg:
            return self._extract_normalized(pdf_path)
        raise
    if not result.text.strip():
        return self._extract_normalized(pdf_path)
    # Quality-rescue: fall back to pdfplumber if tables are present but missing
    # from the markdown output (e.g. GNN mis-classified cells as body text).
    if self._markdown_misses_tables(pdf_path, result.text):
        try:
            rescue = self._extract_with_pdfplumber(pdf_path)
            if rescue.text.strip():
                return rescue
        except Exception:
            pass
    return result
```

**Success:** E2E dry-run on a known ruled-table PDF → chunks contain `|` markers and
metadata shows `extraction_method == "pdfplumber"`. Plain PDF unchanged.

### Phase 5 — Tests

New unit tests in `test_pdf_extractor.py`:
- `test_markdown_misses_tables_detects_gap`: ruled-table PDF + prose-only markdown → True
- `test_markdown_misses_tables_no_tables`: plain PDF → False
- `test_markdown_misses_tables_pipes_present`: table PDF + already-piped markdown → False
- `test_normalize_whitespace_tab`, `test_normalize_whitespace_nbsp`,
  `test_normalize_whitespace_excess_newlines`: one test per normalization case
- `test_extract_with_pdfplumber_produces_pipes`: table PDF → `|` in output
- `test_extract_with_pdfplumber_page_boundaries`: page boundaries contiguous

New subsystem test in `test_pdf_subsystem.py`:
- `test_pdfplumber_tier_fires_for_table_pdf`: table PDF with deficient pymupdf4llm
  output → final metadata has `extraction_method == "pdfplumber"`

Regression guard in `test_pdf_subsystem.py`:
- `test_simple_pdf_stays_on_markdown_tier`: existing simple PDF assertions still pass;
  `extraction_method == "pymupdf4llm_markdown"` unchanged

Note: `test_pdf_subsystem.py` existing assertions against `extraction_method` do not
need updating because they use simple PDFs that will not trigger `_markdown_misses_tables`.

## Risks

**Borderless tables not detected (known scope limitation).**
`find_tables()` in both PyMuPDF and pdfplumber detects tables via explicit ruling
lines or close text-block alignment. Borderless tables — common in IEEE/ACM papers,
many financial PDFs, government reports — produce no edges and are invisible to the
detector. The quality check will return False for these, and pymupdf4llm's (possibly
garbled) output will be used. Scope of this RDR is: "ruled-border tables only."
Borderless table support requires a text-heuristic approach and is deferred.

**Caption severance (known limitation, accepted).**
Tables are appended after the prose block for their page. Captions that appear
immediately above or below the table in the PDF will be in the prose section;
the table content will be at the end of the page block. If the chunker splits at the
page boundary, caption and table land in different chunks. Coordinate-aware
interleaving (using pdfplumber bounding boxes to insert each table inline with the
surrounding prose) is the correct fix but adds significant complexity. Accepted for
now; tracked as a follow-on improvement.

**Staleness guard does not re-index on extraction method change.**
`_index_document` skips re-indexing when `content_hash` and `embedding_model` match.
A PDF already indexed with `pymupdf4llm_markdown` will not be re-indexed after this
change unless the file content changes. Acceptable: new extraction method is an
improvement, and existing indexed content is still semantically correct. Users who
want to force re-extraction can delete the collection or modify the file.

**`extract_tables()` false positives for code/preformatted blocks.**
pdfplumber may identify aligned code blocks or definition lists as tables. Mitigation:
the quality-rescue path only fires when `_markdown_misses_tables` confirms tables
were expected but missing — pdfplumber is not invoked speculatively.

**Residual second PyMuPDF open in `_markdown_misses_tables()`.**
`_extract_markdown()` opens and closes the PDF via pymupdf; the quality check then
opens it a second time. Eliminating this would require threading the open document
handle through from `extract()`. Accepted cost: the detection opens at most 5 pages
and is only invoked when pymupdf4llm succeeds (not on the Type3/error/empty-output
paths). For the vast majority of PDFs (no ruled tables on first 5 pages), `ruled_tables`
is 0 and the function returns immediately after the scan.

## Decision

Adopt Option B revised. pdfplumber as quality-rescue fallback after pymupdf4llm
attempt. Detection via PyMuPDF native `find_tables()`. pdfplumber opened only for
extraction. Add `_normalize_whitespace_edge_cases()` to normalized extractor.
Known limitations: borderless tables out of scope; caption severance accepted.
OCR pipeline deferred to future RDR.
