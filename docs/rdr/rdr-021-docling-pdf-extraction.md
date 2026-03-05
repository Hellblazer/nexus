---
id: RDR-021
title: "Replace 3-Tier PDF Extraction Stack with Docling"
type: enhancement
status: accepted
accepted_date: 2026-03-05
reviewed-by: self
priority: P2
created: 2026-03-05
---

# RDR-021: Replace 3-Tier PDF Extraction Stack with Docling

## Problem

The current PDF extraction pipeline in `pdf_extractor.py` uses a 3-tier fallback chain:

1. **pymupdf4llm markdown** — primary; layout extension activated for column detection
2. **pdfplumber rescue** — triggered when ruled tables detected but absent from markdown
3. **pymupdf normalized** — final fallback for Type3 fonts or empty output

Empirical testing against the full `knowledge__delos` corpus (19 academic PDFs) reveals
significant extraction failures that the current stack cannot fix:

- **Multi-column IEEE/ACM papers** (e.g. `fireflies-tocs.pdf`, `Aging Bloom Filter`) fall
  into the pdfplumber rescue path due to having ruled tables, but pdfplumber does not handle
  two-column layout — it merges words across columns, producing `IEEETRANSACTIONSONKNOWLEDGE`
  and `AdditionalKeyWordsandPhrases:Byzantinefailures` (81 merged-alpha-word instances in
  fireflies-tocs alone).
- **Type3 font PDFs** (`pBeeGees.pdf`, `prdts.pdf`, `tc-sql.pdf`) fall to pymupdf_normalized,
  which produces readable but unstructured text — 0 markdown headings, no section hierarchy.
- **PDF metadata titles** are empty for ~14/19 files because PDF XMP/Info dict title fields
  are rarely populated in academic papers. Search results show blank `source_title` fields.

The 3-tier detection logic adds code complexity without reliably solving the underlying
layout problem.

## Research Findings

### Finding 1: Docling layout model fixes multi-column merging

IBM's Docling uses a neural layout model (`docling-ibm-models`) to detect reading order
across text regions before extracting text. This correctly handles two-column academic layouts.

Metric: count of purely-alphabetic tokens ≥ 21 characters (`re.fullmatch(r'[a-zA-Z]{21,}', w)`)
— distinguishes merged prose words from URLs, table separators, and code identifiers.

Full corpus test (19 PDFs, `knowledge__delos`):

| Outcome | Count | Files |
|---------|-------|-------|
| Genuinely fixed | 5 | fireflies-tocs, bft-to-blockchain, distributed-bloom-filter, tc-sql, permission-systems |
| Clean in both stacks | 11 | async-mpc, bft-to-smr, gossip-noise-reduction, hex-bloom, lightweight-smr, mfaz†, pBeeGees, rapid-atc18, self-stabilizing-bft-overlay, virgo, zanzibar |
| False positive — code identifiers | 1 | prdts (`currentRoundHasProposal` in pseudocode listing) |
| Formula artifact | 1 | Aging Bloom Filter (√ radical → `ffiffifi...`; 21→3 instances, remainder math) |
| Minor Docling regression | 1 | aleph-bft (1 instance `Wenowproceedtoproving` in 39K words) |

†mfaz: `transpositionencrypted` appears to be the authors' compound term.

### Finding 2: Docling warm performance is competitive

After one-time HuggingFace model download (~42s, first ever use), subsequent cold starts
(new process, models cached locally) are ~3.5s. **This 3.5s applies to every `nx index pdf`
invocation** — it is not amortised across the process lifetime for CLI use. Within a single
process (batch mode), warm per-file time is ~2.8–3s.

| File | Pages | Current stack | Docling warm |
|------|-------|---------------|-------------|
| Aging Bloom Filter | 5p | 1.9s (pdfplumber) | 3.4s |
| fireflies-tocs | 33p | 8.3s (pdfplumber) | 4.6s |
| zanzibar | 14p | 4.1s (pymupdf4llm) | 2.4s |
| aleph-bft | 34p | 10.6s (pymupdf4llm) | 8.0s |

For batch indexing the 3.5s cold-start is paid once. For individual `nx index pdf` calls it
is paid every invocation — a user-visible change from the current ~0.1s cold start.

Alternatives considered and rejected:
- **Selective quality-gate**: `space_ratio < 0.05` ineffective (bad files: 0.27–0.38);
  max-word-length heuristic works but requires per-file measurement before deciding.
  Single Docling path is simpler and eliminates the detection logic.
- **Persistent service model** (Unix socket daemon): ~0.5s per-call saving (3.5s cold vs
  2.8s warm) does not justify auto-fork, stale socket detection, and idle timeout complexity.

### Finding 3: Docling `export_to_markdown(page_no=N)` enables per-page processing

**Critical for `page_boundaries` contract.** `DoclingDocument.export_to_markdown(page_no=N)`
returns the markdown for a single page. This allows the new `PDFExtractor` to use the
identical per-page character-accumulator loop as the current implementation:

```python
page_texts = []
current_pos = 0
for p in range(1, doc.num_pages() + 1):
    page_md = result.document.export_to_markdown(page_no=p).strip()
    if page_md:
        page_boundaries.append({
            "page_number": p,
            "start_char": current_pos,
            "page_text_length": len(page_md) + 1,
        })
        page_texts.append(page_md)
        current_pos += len(page_md) + 1
text = "\n".join(page_texts)
```

`PDFChunker._page_for()` is unchanged. All existing chunk-to-page attribution logic works
without modification. Verified: `doc.export_to_markdown(page_no=1)` returns 4,439 chars
for zanzibar.pdf page 1 with correct content.

### Finding 4: Docling extracts titles from document content

Refined title extraction heuristic (verified on all 19 corpus PDFs):

1. Iterate `doc.iterate_items()` collecting page-1 items with text ≥ 10 chars
2. Skip items whose text matches `{'abstract', 'introduction', '1 introduction', 'keywords'}`
   or starts with "abstract" and is longer than 100 chars
3. Return first item with `label` containing `'title'` or `'section_header'`
4. Fallback: first `text`-labelled item on page 1 with `len < 120`

Results on 19 corpus PDFs:

| Result | Count | Notes |
|--------|-------|-------|
| Correct paper title | 17 | Including distributed-bloom-filter and zanzibar (fixed vs earlier test) |
| Wrong (section heading) | 1 | Aging Bloom Filter → "Concise Papers" (IEEE journal section) |
| Partial (institution name) | 1 | tc-sql → "Wright State University CORE Scholar" |

17/19 vs ~14/19 blank with current stack. Known non-title results are still more useful
than blank for search result display.

Title source priority in `_pdf_chunks`:
```python
source_title = (
    result.metadata.get("docling_title", "")          # from Docling content analysis
    or result.metadata.get("pdf_title", "")           # from PDF XMP/Info dict
    or pdf_path.stem.replace("_", " ").replace("-", " ")  # filename fallback
)
```

The new `PDFExtractor` writes the Docling-extracted title to `result.metadata["docling_title"]`.
`_pdf_chunks` reads `result.metadata.get("docling_title", "")` first. Both sides use
`"docling_title"` — no ambiguity.

### Finding 5: Heading structure improves significantly for formerly unstructured PDFs

Files that currently produce 0 headings (pdfplumber and pymupdf_normalized paths) gain
full markdown structure under Docling:

| File | Current headings | Docling headings |
|------|-----------------|-----------------|
| Aging Bloom Filter | 0 | 17 |
| distributed-bloom-filter | 0 | 20 |
| fireflies-tocs | 0 | 38 |
| pBeeGees | 0 | 34 |
| prdts | 0 | 32 |
| permission-systems | 0 | 14 |
| tc-sql | 0 | 3 |

Minor heading count regressions in 2 files (aleph-bft: 51→41, hex-bloom: 34→28) where
Docling is slightly more conservative in heading detection. These are not quality regressions —
the heading content is still present as body text.

### Finding 6: Chunk count and character output

Corpus-wide totals (same `PDFChunker` applied to both):

| Metric | Current stack | Docling |
|--------|---------------|---------|
| Total raw chars | 1,638,672 | 1,441,945 (−12%) |
| Total chunks (current) | 1,397 | ~similar (same chunker, same thresholds) |

The 12% character reduction is primarily from pdfplumber's spatial-layout whitespace padding
being eliminated. Chunk counts will be comparable. Re-indexing `knowledge__delos` (1,397
chunks) costs approximately $0.17 in Voyage AI CCE embeddings — negligible.

### Finding 7: Dependency analysis

`docling>=2.76` adds: `docling-ibm-models`, `docling-parse`, `docling-core`, `pypdfium2`,
`huggingface_hub`, `scipy`, `accelerate`, `pandas`, `python-docx`, `python-pptx`, and others.

**Model download size**: ~1,060 MB total (HuggingFace cache):
- `docling-models`: 716.5 MB (layout + TableFormer)
- `docling-layout-heron`: 343.5 MB

This is a one-time download on first use. Subsequent runs load from local cache (~3.5s).

**Removable after adoption**: `pymupdf4llm` and `pdfplumber` are imported only in
`pdf_extractor.py` (confirmed: `grep -r pymupdf4llm src/nexus --include=*.py` → one file).
Both can be removed from `pyproject.toml` once Docling is confirmed stable. `pymupdf`
(the base library, without `4llm`) must be retained for the `pymupdf_normalized` fallback.

### Finding 8: Docling pipeline configuration (verified)

```python
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions

opts = PdfPipelineOptions()
opts.do_ocr = False               # digital PDFs have embedded text
opts.do_table_structure = True    # TableFormer for table detection/extraction
opts.generate_page_images = False
opts.generate_picture_images = False

converter = DocumentConverter(
    format_options={"pdf": PdfFormatOption(pipeline_options=opts)}
)
result = converter.convert(str(pdf_path))
```

## Decision

**Replace the 3-tier extraction stack with a single Docling tier, retaining `pymupdf_normalized`
as a fallback for Docling failures.**

### What changes

| Component | Change |
|-----------|--------|
| `src/nexus/pdf_extractor.py` | Replace `PDFExtractor` internals with Docling; retain `ExtractionResult` dataclass and `extract(path) -> ExtractionResult` public API |
| `pyproject.toml` | Add `docling>=2.76` to `dependencies`; remove `pymupdf4llm` and `pymupdf-layout` and `pdfplumber` once adoption confirmed |
| `src/nexus/doc_indexer.py` | Title fallback: `result.metadata["docling_title"]` → `result.metadata["pdf_title"]` → filename stem |
| `tests/test_pdf_extractor.py` | Replace 3-tier behaviour tests with Docling path tests |

### What stays the same

- `ExtractionResult` dataclass (`text: str`, `metadata: dict`) — unchanged
- `extraction_method` metadata field — new value: `"docling"`
- `page_boundaries` list structure (`page_number`, `start_char`, `page_text_length`)
- `PDFChunker` — no changes needed
- `chunk_start_char`, `chunk_end_char`, `page_number` in chunk metadata
- All T3 indexing, search, and CLI commands

### New `PDFExtractor` structure

```python
class PDFExtractor:
    def __init__(self):
        self._converter = None  # lazy init — avoid 3.5s cost for non-PDF operations

    def _get_converter(self):
        if self._converter is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            opts = PdfPipelineOptions()
            opts.do_ocr = False
            opts.do_table_structure = True
            opts.generate_page_images = False
            opts.generate_picture_images = False
            self._converter = DocumentConverter(
                format_options={"pdf": PdfFormatOption(pipeline_options=opts)}
            )
        return self._converter

    def extract(self, pdf_path: Path) -> ExtractionResult:
        try:
            return self._extract_with_docling(pdf_path)
        except Exception:
            _log.warning("docling extraction failed; falling back to pymupdf_normalized",
                         path=str(pdf_path), exc_info=True)
            return self._extract_normalized(pdf_path)

    def _extract_with_docling(self, pdf_path: Path) -> ExtractionResult:
        result = self._get_converter().convert(str(pdf_path))
        doc = result.document
        page_count = doc.num_pages()
        page_texts, page_boundaries, current_pos = [], [], 0
        for p in range(1, page_count + 1):
            page_md = doc.export_to_markdown(page_no=p).strip()
            if page_md:
                page_boundaries.append({
                    "page_number": p,
                    "start_char": current_pos,
                    "page_text_length": len(page_md) + 1,
                })
                page_texts.append(page_md)
                current_pos += len(page_md) + 1
        text = "\n".join(page_texts)
        if not text.strip():
            raise RuntimeError("docling produced empty output")
        return ExtractionResult(
            text=text,
            metadata={
                "extraction_method": "docling",
                "page_count": page_count,
                "format": "markdown",
                "page_boundaries": page_boundaries,
                "docling_title": self._extract_title(doc),
                "pdf_title": "",  # XMP metadata not exposed by Docling
                ...
            },
        )
```

The empty-output guard (`if not text.strip(): raise RuntimeError(...)`) triggers the fallback
to `pymupdf_normalized` for corrupt or blank PDFs.

### Title extraction algorithm

```python
def _extract_title(self, doc) -> str:
    for item, _ in doc.iterate_items():
        prov = getattr(item, 'prov', [])
        if not prov or prov[0].page_no != 1:
            continue
        text = (getattr(item, 'text', '') or '').strip()
        if not text or len(text) < 10:
            continue
        lower = text.lower()
        if lower in ('abstract', 'introduction', '1 introduction', 'keywords'):
            continue
        if lower.startswith('abstract') and len(text) > 100:
            continue
        label = str(getattr(item, 'label', ''))
        if 'title' in label or 'section_header' in label:
            return text
    # fallback: first short text block on page 1
    for item, _ in doc.iterate_items():
        prov = getattr(item, 'prov', [])
        if not prov or prov[0].page_no != 1:
            continue
        text = (getattr(item, 'text', '') or '').strip()
        if text and 10 <= len(text) < 120:
            return text
    return ''
```

Known non-title results: Aging Bloom Filter → "Concise Papers" (IEEE section label),
tc-sql → "Wright State University CORE Scholar". Accepted — both are more informative
than blank.

## Risks

- **Model download**: ~1 GB from HuggingFace on first ever use. Document in `nx index pdf`
  help text and in `docs/quickstart.md`. `nx doctor` should warn if models not yet cached.
- **Per-invocation cold start**: 3.5s added to every `nx index pdf` CLI call (not just the
  first). This is user-visible. Current stack cold start is ~0.1s.
- **Formula rendering**: Docling renders some math symbols as ligature sequences
  (`ffiffifi...`). 3 instances in Aging Bloom Filter. Chunks remain semantically useful.
- **Minor regression in aleph-bft**: 1 merged word in 39K — negligible.
- **Re-indexing required**: Accepting this RDR triggers a forced re-index of all PDF
  collections (`knowledge__delos`: 1,397 chunks ≈ $0.17 Voyage AI cost).
- **Dependency bloat**: scipy, accelerate, huggingface_hub add substantial installed size.
  Offset by eventual removal of pymupdf4llm, pymupdf-layout, pdfplumber.
