# Design: pdfplumber Tier for PDF Extraction

**Date:** 2026-03-01
**Status:** Approved

## Problem

Nexus's PDF extractor has two tiers: pymupdf4llm markdown (primary) and pymupdf
normalized (fallback). PDFs with complex tables (multi-column, merged cells) produce
garbled or flattened output with both tiers. Arcaneum's stack adds pdfplumber as a
table-aware middle tier that handles this case well.

## Approved Approach: Option B — pdfplumber as 3rd tier (3 tiers total, no OCR)

Add pdfplumber as tier 2 (between pymupdf4llm and pymupdf normalized). Trigger it when
`_has_complex_tables()` heuristic fires. Also copy Arcaneum's
`_normalize_whitespace_edge_cases()` helper into the normalized extractor.

OCR (Tesseract/EasyOCR + pdf2image + cv2) is explicitly out of scope — heavy system
dependencies, better handled as a separate opt-in RDR.

## Extraction Tier Order (after this change)

```
1. PyMuPDF4LLM markdown   — quality-first, preserves headings/lists/tables
   ↓ if Type3 fonts detected, font RuntimeError, or empty output
   ↓ if _has_complex_tables() → skip to tier 2 directly

2. pdfplumber             — table-aware, uses extract_tables() + Markdown formatting
   ↓ if extraction fails or produces empty output

3. PyMuPDF normalized     — raw get_text(sort=True) + whitespace normalization
```

## Changes

### `src/nexus/pdf_extractor.py`

1. `_has_complex_tables(pdf_path)` — new method
   - Open with pdfplumber
   - Check first N pages (cap at 5) for tables with >1 column AND >2 rows
   - Return True on first match; return False if pdfplumber unavailable (graceful degradation)

2. `_extract_with_pdfplumber(pdf_path)` — new method
   - Per-page: `page.extract_text()` for prose + `page.extract_tables()` for tables
   - Format each table as Markdown (`| col | col |` with header separator)
   - Interleave prose and tables in reading order (tables inserted after the prose block)
   - Page boundaries tracked same way as other extractors
   - `extraction_method: "pdfplumber"`, `format: "markdown"`

3. `_normalize_whitespace_edge_cases(text)` — new static/module-level helper
   - Replace `\t` with single space
   - Collapse Unicode whitespace variants (non-breaking space `\u00a0`, etc.)
   - Collapse 4+ consecutive newlines to `\n\n\n` (preserve intentional triple breaks)
   - Applied in `_extract_normalized` after existing normalization

4. `extract()` — update routing logic:
   - After Type3 / empty-output checks, add: `if self._has_complex_tables(pdf_path): return self._extract_with_pdfplumber(pdf_path)`
   - Position this check BEFORE the `_extract_markdown` call so complex-table PDFs skip the slower layout analysis

   Final routing:
   ```
   has_type3 → _extract_normalized
   has_complex_tables → _extract_with_pdfplumber
   else → _extract_markdown
     ↳ RuntimeError(font) → _extract_normalized
     ↳ empty output → _extract_normalized (or _extract_with_pdfplumber?)
   ```
   Empty-output fallback goes to `_extract_normalized` (not pdfplumber) — pdfplumber is
   for tables, not for extraction rescue.

### `pyproject.toml`

Add: `pdfplumber>=0.11.7` to `[project.dependencies]`.

### `uv.lock`

Updated automatically by `uv sync` after pyproject.toml change.

## What Is NOT Changed

- `src/nexus/pdf_chunker.py` — no changes
- `src/nexus/doc_indexer.py` — no changes
- OCR pipeline — explicitly out of scope; separate RDR if needed
- `preserve_images` mode — out of scope
- Late chunking / overlap in chunker — out of scope

## pdfplumber Table Markdown Format

Each table from `extract_tables()`:
```python
rows = table  # list[list[str | None]]
# Header = first row; separator row; data rows
```
Output:
```markdown
| col1 | col2 | col3 |
|------|------|------|
| val  | val  | val  |
```
None cells rendered as empty string.

## Graceful Degradation

If `pdfplumber` is not installed (ImportError), `_has_complex_tables()` returns False
and `_extract_with_pdfplumber()` raises RuntimeError. Since `_has_complex_tables()`
gates the call, pdfplumber is never invoked when absent. This means pdfplumber can be
listed as an optional dependency if desired, but for simplicity it is listed as
required in pyproject.toml.

## Test Strategy

- Unit: `_has_complex_tables()` with a synthetic multi-column PDF
- Unit: `_extract_with_pdfplumber()` — table → markdown format, prose passthrough
- Unit: `_normalize_whitespace_edge_cases()` — tab, NBSP, 4+ newlines
- Integration: table PDF → pdfplumber tier fires, chunks include `|` characters
- Regression: existing tests unchanged (pdfplumber path not triggered by simple PDFs)
