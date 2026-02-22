# Phase 5 — PDF and Markdown Indexing Pipelines

**Bead**: nexus-ejp
**Blocked by**: nexus-odd (Phase 3)
**Blocks**: nexus-683 (Phase 6)
**Parallel with**: Phase 4

**Duration**: 1–2 weeks
**Goal**: Build non-code document indexing — PDF extraction (ported from Arcaneum) and semantic markdown chunking.

## Scope

### Beads

| Bead | Task |
|------|------|
| nexus-20t | Port Arcaneum PDF extraction pipeline (PyMuPDF4LLM + pdfplumber + OCR) |
| nexus-otg | Implement PDF chunking and T3 docs__ upsert with voyage-4 embeddings |
| nexus-aw5 | Port Arcaneum SemanticMarkdownChunker with YAML frontmatter extraction |
| nexus-5ab | Implement markdown indexing with SHA256 incremental sync |

### Technical Decisions

**PDF extraction (Arcaneum port)**:
- Primary: PyMuPDF4LLM → markdown output (best for structured PDFs)
- Fallback 1: pdfplumber (complex tables)
- Fallback 2: Tesseract/EasyOCR (scanned PDFs)
- Type3 font pre-check (port from Arcaneum): detect Type3 fonts before extraction; skip page with warning if detected to avoid hang
- Per-page timeout: 30s (subprocess timeout); on timeout, skip page and log warning
- Fallback chain: markdown → normalized text → skip with warning (not error)

**PDF chunking**:
- ChunkStrategy.chunk(content, source_path, metadata) — source_path required
- Chunk size: ~500 tokens (larger than code chunks; PDFs have more dense prose)
- No line-number attribution (PDFs don't have source lines)
- Metadata: `file_path`, `filename`, `page_number`, `chunk_index`, `chunk_type="pdf"`
- T3 collection: `docs__{corpus-name}` (collection name from `--collection` arg or inferred from filename)
- Embedding: VoyageAIEmbeddingFunction(model_name="voyage-4")
- Incremental sync: SHA256 of extracted text per file; skip re-embedding if unchanged

**Markdown indexing**:
- SemanticMarkdownChunker: heading-aware chunking (H1/H2 boundaries define chunks)
- YAML frontmatter: extracted as metadata fields (title, tags, date, etc.)
- Chunk size: ~300 tokens (smaller than PDF; markdown is dense with structure)
- Metadata: `file_path`, `filename`, `chunk_index`, `chunk_type="markdown"`, `heading`, frontmatter fields
- T3 collection: `docs__{corpus-name}` (same as PDF; mixed corpus OK)
- Embedding: VoyageAIEmbeddingFunction(model_name="voyage-4")
- Incremental sync: SHA256 of raw file; skip if unchanged

**Constructor injection**: indexing pipelines receive VectorStore (protocol) via constructor, not `storage/t3_cloud.py` directly. `cli/index_cmd.py` constructs T3 and injects it.

## Entry Criteria

- Phase 3 complete (T3 CloudClient working, nx store/search working)
- `pymupdf4llm`, `pdfplumber`, `voyageai` packages installed

## Exit Criteria

- [ ] `nx index pdf <path>` extracts, chunks, embeds, upserts to T3
- [ ] Type3 font detection: affected pages skipped with warning (not crash)
- [ ] Per-page extraction timeout: timed-out pages skipped with warning
- [ ] SHA256 incremental sync: unchanged PDFs not re-embedded
- [ ] `nx index md <path>` extracts markdown, chunks at H1/H2 boundaries, upserts
- [ ] YAML frontmatter extracted into metadata fields
- [ ] SemanticMarkdownChunker handles nested headings correctly
- [ ] Both pipelines use VectorStore protocol (injectable for unit test mocking)
- [ ] `nx search "query" --corpus docs` returns results from PDF/markdown collections
- [ ] pytest >85% coverage on indexing/pdf and indexing/markdown modules

## Testing Strategy

**Unit tests** (`tests/unit/indexing/test_pdf_extraction.py`):
- PyMuPDF4LLM extraction on sample PDF (2-3 pages)
- Type3 font detection triggers skip (mock PDF with Type3 metadata)
- Fallback chain: primary fails → pdfplumber
- Chunk boundaries: chunks don't exceed max token count

**Unit tests** (`tests/unit/indexing/test_markdown_chunking.py`):
- H1/H2 boundary detection
- YAML frontmatter extraction
- Chunk size limits
- Nested heading handling

**Integration tests** (`tests/integration/test_pdf_upsert.py`):
- End-to-end: PDF → extract → chunk → embed → upsert to mock VectorStore
- SHA256 incremental: second index call with unchanged file produces no upserts
- SHA256 incremental: changed file triggers re-embed

## Key Files

| File | Purpose |
|------|---------|
| `src/nexus/indexing/pdf/extractor.py` | Arcaneum PDF extraction port |
| `src/nexus/indexing/pdf/chunker.py` | PDFChunker (ChunkStrategy impl) |
| `src/nexus/indexing/pdf/pipeline.py` | IndexPipeline impl; constructor-injected VectorStore |
| `src/nexus/indexing/markdown/chunker.py` | SemanticMarkdownChunker (Arcaneum port) |
| `src/nexus/indexing/markdown/pipeline.py` | Markdown IndexPipeline |
| `tests/unit/indexing/test_pdf_extraction.py` | PDF unit tests |
| `tests/unit/indexing/test_markdown_chunking.py` | Markdown unit tests |
| `tests/integration/test_pdf_upsert.py` | End-to-end with mock VectorStore |

## Arcaneum Port Notes

**What to port**:
- `Arcaneum/arcaneum/pdf/extractor.py` — PyMuPDF4LLM wrapper with Type3 guard
- `Arcaneum/arcaneum/chunking/semantic_markdown.py` — SemanticMarkdownChunker

**What to adapt**:
- Replace Arcaneum's storage layer with Nexus VectorStore protocol
- Replace Arcaneum's config with Nexus config.py dataclasses
- Update ChunkStrategy.chunk() signature to include source_path parameter
- License: Arcaneum is MIT; Nexus is AGPL-3.0. MIT code is compatible with AGPL. Add SPDX header to ported files.

**What NOT to port**:
- Arcaneum's CLI (replaced by Nexus CLI)
- Arcaneum's Mixedbread-specific upload logic (Nexus uses ChromaDB CloudClient)
