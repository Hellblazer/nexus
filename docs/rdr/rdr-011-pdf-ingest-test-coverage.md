---
title: "PDF Ingest Test Coverage: Unit, Subsystem, and E2E with Local ChromaDB"
id: RDR-011
type: testing
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-01
updated: 2026-03-01
accepted_date: 2026-03-01
gate_result: PASSED
gate_date: 2026-03-01
related_issues: []
---


## RDR-011: PDF Ingest Test Coverage: Unit, Subsystem, and E2E with Local ChromaDB

## Summary

The PDF ingest pipeline (`PDFExtractor` → `PDFChunker` → `_pdf_chunks` →
`_index_pdf_file`) has zero test coverage against real PDF bytes. Every existing
test mocks out the pymupdf/pymupdf4llm layer or writes `b"fake pdf bytes"` as
the fixture. This means:

- The actual markdown extraction format from pymupdf4llm has never been exercised
- Page boundary computation against real multi-page content is untested
- Type3 font detection with real font tables is untested
- The `_index_pdf_file` path in `indexer.py` (which adds git metadata) has no
  tests at all
- The E2E path — real PDF bytes → embeddings → ChromaDB — has never run
- **PDF document metadata (`title`, `author`, `subject`, `creationDate`, etc.)
  is silently discarded** — `PDFExtractor` never reads `doc.metadata`, and
  `_pdf_chunks` hardcodes `source_title`, `source_author`, `source_date` as
  empty strings (production bug found during research)

This RDR designs a three-tier test strategy that covers all layers without
requiring cloud credentials (no Voyage AI, no ChromaDB Cloud), and includes
the production fix for PDF metadata extraction.

## Motivation

1. **1.0.0 shipped with untested PDF ingest.** The primary advertised use case
   for `nx index pdf` and `nx index repo` (PDF routing) has no validation that
   the pipeline actually works.

2. **Mocked-only tests give false confidence.** `test_pdf_extractor.py` passes
   because it mocks `pymupdf4llm.to_markdown` — it says nothing about whether
   the real output can be parsed, whether page boundaries are correct, or whether
   the chunker handles pymupdf4llm's actual markdown dialect.

3. **Git metadata is untested in the PDF path.** `_index_pdf_file` merges
   `git_meta` (branch, commit hash, remote URL, project name) into every chunk's
   metadata. This field set is tested for code files but not for PDFs.

4. **PDF document metadata is silently lost.** PyMuPDF's `doc.metadata` provides
   title, author, subject, keywords, creator, producer, creationDate, and
   modDate. None of these are extracted. `_pdf_chunks` has `source_title`,
   `source_author`, `source_date` hardcoded to `""` — the fields exist in the
   schema but are never filled from the actual PDF.

5. **Local ChromaDB E2E is feasible without credentials.** ChromaDB
   `EphemeralClient` + `DefaultEmbeddingFunction` (all-MiniLM-L6-v2 via ONNX,
   no API key) is already used in tests. A real PDF → embed → store → search
   cycle should run in CI without any secrets.

## Scope

### In scope

- **Production fix**: `PDFExtractor` must extract `doc.metadata` (title, author,
  subject, keywords, creation date, etc.) and propagate through to chunk metadata
- **Unit tests** (no ChromaDB, no Voyage, no network) using real PDF fixture files
- **Subsystem tests** (real extraction + chunking, mocked embed + T3)
- **E2E tests** (real extraction + chunking + local embedding + EphemeralClient)
- PDF document metadata (`source_title`, `source_author`, `source_date`, etc.) populated from `doc.metadata`
- Git metadata correctness in `_index_pdf_file` (repo indexer PDF path)
- Type3 font fallback validation with a real Type3 PDF
- Staleness / incremental-sync skip behaviour with real content hashes
- Fixture PDF generation strategy (programmatic with embedded metadata, no copyrighted content)

### Out of scope

- Cloud ChromaDB / Voyage AI embedding (already tested in `test_doc_indexer.py`
  via mocks; cloud E2E is a separate concern)
- `nx index pdf` CLI output formatting (already covered in `test_index_cmd.py`)
- PDF search result quality / ranking

## Design

### Fixture PDF Strategy

Generate minimal test PDFs programmatically using **pymupdf** itself (already a
dependency). This avoids bundling binary blobs, keeps fixtures reproducible, and
produces PDFs whose structure we fully control.

Create `tests/fixtures/` directory with a `conftest.py`-level or module-level
generator that produces:

| Fixture name | Pages | Font type | Purpose |
|---|---|---|---|
| `simple.pdf` | 1 | TrueType (Helvetica) | Happy path: markdown extraction |
| `multipage.pdf` | 3 | TrueType | Page boundary tracking |
| `type3_font.pdf` | 1 | Type3 (synthetic) | Fallback path |

Generation approach:
```python
import pymupdf

# Page content per topic — semantically distinct for reliable semantic search tests
_PAGE_TOPICS = [
    "Apple orchards produce fruit in autumn harvests.",
    "Database transactions ensure ACID consistency in storage systems.",
    "Network protocols define communication rules between distributed nodes.",
]

def make_simple_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Hello World. This is a test document for PDF ingest.", fontsize=12)
    doc.set_metadata({
        "title": "Test Document",
        "author": "Test Author",
        "subject": "PDF Ingest Testing",
        "keywords": "test, pdf, nexus",
        "creationDate": "D:20260301000000",
    })
    doc.save(str(path))
    doc.close()

def make_multipage_pdf(path: Path) -> None:
    """Generate a 3-page PDF with semantically distinct content per page.

    Content per page must exceed chunk_chars=30 threshold used in AC-U9/U10
    tests. Each page uses a distinct topic to make semantic search reliable
    in AC-E2.
    """
    doc = pymupdf.open()
    for i, topic in enumerate(_PAGE_TOPICS, start=1):
        page = doc.new_page()
        # ~300 chars per page ensures PDFChunker(chunk_chars=100) splits across pages
        text = f"{topic} " * 10  # repeat to get sufficient length
        page.insert_text((72, 100), text.strip(), fontsize=12)
    doc.set_metadata({"title": "Multipage Test", "author": "Test Author"})
    doc.save(str(path))
    doc.close()
```

**Critical note on fixture text length**: `PDFChunker._DEFAULT_CHUNK_CHARS = 1500`
(`pdf_chunker.py:9`). The `multipage.pdf` fixture must generate enough text per
page to force multi-chunk output. AC-U9 and AC-U10 tests **must** instantiate
`PDFChunker(chunk_chars=100)` explicitly — do not use the default — to guarantee
multi-chunk output from the fixture without needing enormous generated files.
This is intentional test isolation: the default chunk size is exercised by
unit tests using synthetic strings; the integration tests focus on correctness
of page boundary attribution, not chunk-size behaviour.

Note: `insert_text` writes plain text — pymupdf4llm may or may not produce
markdown headings. Tests assert on content presence, not exact heading syntax.

**pymupdf date format**: `doc.metadata["creationDate"]` returns `D:20260301000000`
(PDF date format). Store this value as-is in `pdf_creation_date` — do not strip
the `D:` prefix. Tests asserting `source_date` is non-empty should use
`assert result["source_date"].startswith("D:")` to confirm the raw format.

For Type3 fonts: embed a minimal hand-crafted PDF with a Type3 font dictionary
as a bytes fixture (`tests/fixtures/type3_font.pdf` as a static binary). A
~500-byte hand-crafted PDF with one Type3 glyph is sufficient — this cannot be
easily generated via pymupdf's Python API.

### Tier 0 — Production Fix: PDF Document Metadata Extraction

`PDFExtractor._extract_markdown` and `_extract_normalized` both open
`doc = pymupdf.open(pdf_path)` — add `doc.metadata` extraction in both paths.
Include it in `ExtractionResult.metadata` under the keys:
`pdf_title`, `pdf_author`, `pdf_subject`, `pdf_keywords`,
`pdf_creator`, `pdf_producer`, `pdf_creation_date`, `pdf_mod_date`.
All values default to `""` if absent.

Update `doc_indexer._pdf_chunks` to populate:
- `source_title` ← `pdf_title`
- `source_author` ← `pdf_author`
- `source_date` ← `pdf_creation_date`

Add new chunk metadata fields (PDF-only, absent from markdown chunks):
- `pdf_subject` ← `pdf_subject`
- `pdf_keywords` ← `pdf_keywords`

**Schema test requirement**: The existing `test_docs_metadata_schema_complete`
in `test_doc_indexer.py:142-148` validates markdown schema only. A new parallel
test `test_pdf_metadata_schema_complete` must assert the full PDF chunk key set:
all 19 existing keys **plus** `pdf_subject` and `pdf_keywords`. The existing
markdown schema test must remain unchanged (these fields are PDF-only).

### Tier 1 — Unit Tests (no mocks, no network)

File: `tests/test_pdf_extractor_integration.py`

All tests use real fixture PDFs. No patching of pymupdf or pymupdf4llm.

**AC-U1**: `PDFExtractor.extract(simple.pdf)` returns `ExtractionResult` with
`extraction_method == "pymupdf4llm_markdown"`, non-empty text, `page_count == 1`,
`format == "markdown"`.

**AC-U2**: `PDFExtractor.extract(multipage.pdf)` returns `page_count == 3` and
`page_boundaries` list with exactly 3 entries, each with correct `page_number`.

**AC-U3**: `PDFExtractor._has_type3_fonts(simple.pdf)` returns `False`.

**AC-U4**: `PDFExtractor._has_type3_fonts(type3_font.pdf)` returns `True`.

**AC-U5**: `PDFExtractor.extract(type3_font.pdf)` returns
`extraction_method == "pymupdf_normalized"`, `format == "normalized"` (Type3 fallback taken).

**AC-U6 (metadata)**: `PDFExtractor.extract(simple.pdf).metadata` contains
`pdf_title == "Test Document"`, `pdf_author == "Test Author"`,
`pdf_creation_date` non-empty.

**AC-U7 (metadata fallback)**: `PDFExtractor.extract(multipage.pdf).metadata`
contains `pdf_title == "Multipage Test"`. For a PDF with no metadata set,
all `pdf_*` fields are `""` (not `None`, not missing keys).

**AC-U8 (normalized metadata)**: `PDFExtractor.extract(type3_font.pdf).metadata`
still contains `pdf_title`, `pdf_author` keys (even if empty — normalized path
also extracts doc metadata).

File: `tests/test_pdf_chunker_integration.py`

**AC-U9**: `PDFChunker(chunk_chars=100).chunk(real_extracted_text, real_metadata)`
— text extracted from `multipage.pdf` (which has ~300 chars per page) — produces
`len(chunks) > 1`. Every chunk's `metadata["page_number"]` is ≥ 1 and the
union of `[chunk_start_char, chunk_end_char)` across all chunks spans the full
text without gaps. **Must use `chunk_chars=100` explicitly** — the default 1500
would produce a single chunk from the fixture, making the test vacuous.

**AC-U10 (overlap)**: For `PDFChunker(chunk_chars=100, overlap_percent=0.1)`,
adjacent chunks share at least 1 character of overlap (the last char of chunk N
appears in chunk N+1).

**AC-U11 (char range metadata)**: Every chunk has `chunk_start_char` and
`chunk_end_char` in its metadata, with `chunk_end_char > chunk_start_char`.

### Tier 2 — Subsystem Tests (real extract + chunk; mocked embed + T3)

File: `tests/test_pdf_subsystem.py`

**AC-S1**: `_pdf_chunks(simple.pdf, hash, model, now, corpus)` with real
extraction returns a non-empty list; every tuple `(id, text, meta)` has:
- `meta["store_type"] == "pdf"`
- `meta["content_hash"] == sha256(simple.pdf.read_bytes()).hexdigest()`
- `meta["page_count"] == 1`
- `meta["extraction_method"] == "pymupdf4llm_markdown"`
- `meta["chunk_count"] == len(result)`
- `meta["source_title"] == "Test Document"`
- `meta["source_author"] == "Test Author"`
- `meta["source_date"]` is non-empty

**AC-S2**: `_pdf_chunks(multipage.pdf, ...)` — `meta["page_number"]` values are
drawn from `{1, 2, 3}` (no zeros when boundaries are present).

**AC-S2b**: `_pdf_chunks` on a PDF with no embedded metadata — `source_title`,
`source_author`, `source_date` are all `""` (empty string, not absent).

**AC-S3**: `doc_indexer.index_pdf(simple.pdf, corpus="test")` with mocked
`_embed_with_fallback` and mocked T3 client — upserts chunks and returns
`count > 0`.

**AC-S4**: Calling `index_pdf` twice with the same file (same hash) returns `0`
on the second call. The mock `col.get` must be configured to return matching
`content_hash` and `embedding_model` on the second call:
```python
from nexus.doc_indexer import _sha256
from nexus.corpus import index_model_for_collection
content_hash = _sha256(simple_pdf)
model = index_model_for_collection("docs__test")
mock_col.get.return_value = {
    "ids": ["x"], "metadatas": [{"content_hash": content_hash, "embedding_model": model}]
}
```
This ensures the staleness guard is triggered by value match, not by accident.

**AC-S5**: `_index_pdf_file(simple.pdf, repo=tmp_path, ...)` with real git repo
(using `git init -b main` + repo-local `user.email`/`user.name` + initial
commit, per the pattern in `test_indexer_e2e.py:82-86`), mocked
`_embed_with_fallback`, mocked T3 — metadata on every upserted chunk:
- `assert meta["git_commit_hash"]` — non-empty (truthiness check, not just key presence)
- `assert meta["git_branch"] == "main"` — set by `git init -b main`
- `assert meta["git_project_name"]` — non-empty (equals repo dir name)
- `assert meta["tags"] == "pdf"`
- `assert meta["category"] == "prose"`
- `assert isinstance(meta["frecency_score"], float)`

**AC-S6**: `_index_pdf_file` on a repo with no git (`tmp_path` not a git repo)
stores empty strings for git metadata fields rather than raising.

### Tier 3 — E2E Tests (real extract + chunk + local embed + EphemeralClient)

File: `tests/test_pdf_e2e.py`

Uses `chromadb.EphemeralClient()` + `DefaultEmbeddingFunction()` (no API keys).
These already work in `test_t1.py` and similar tests — no new dependencies.

**AC-E1**: End-to-end happy path using collection `docs__pdf-e2e-simple`:
```
simple.pdf → PDFExtractor → PDFChunker → local embedding → EphemeralClient.upsert
                                                         → EphemeralClient.query("hello world")
```
Query returns at least one result with `distance < 1.0` and
`metadata["store_type"] == "pdf"`.

**AC-E2**: Multipage E2E — `multipage.pdf` indexed into EphemeralClient (using
collection name `docs__pdf-e2e-test` to avoid singleton collision with other
tests), query for `"database transactions"` with `n_results=3` — at least one
result has `metadata["page_number"] == 2`. Use semantically distinct fixture
content (page 1 = apple orchards, page 2 = database transactions, page 3 =
network protocols) for reliable disambiguation by MiniLM-L6-v2.

**AC-E3**: Re-indexing the same PDF (staleness guard) — after indexing
`simple.pdf`, call `index_pdf` again with the same `t3` instance: returns `0`
and the collection's document count is unchanged.

**AC-E4**: `_index_pdf_file` E2E with git repo — `simple.pdf` in a `git init`
repo (using `git init -b main` + repo-local `user.email`/`user.name` config),
indexed via `_index_pdf_file` with EphemeralClient and local embeddings —
querying by content returns a hit where `result["metadatas"][0]["git_project_name"]`
equals the repo directory name.

**AC-E5**: `index_repository()` PDF routing — `rich_repo` fixture in
`test_indexer_e2e.py` augmented with a `simple.pdf`; after `index_repository()`
runs, the `docs__` collection contains at least one chunk where
`metadata["store_type"] == "pdf"` and `metadata["source_title"] == "Test Document"`.

### Local Embedding in E2E Tests

`DefaultEmbeddingFunction` produces 384-dim vectors (all-MiniLM-L6-v2 via ONNX).
`voyage-context-3` / `voyage-4` produce 1024-dim. The E2E tests must use a
consistent embedding function throughout each test (no mixing). Two options:

**Option A (Recommended)**: Parameterise `_index_pdf_file` / `index_pdf` to
accept an optional `embed_fn` callable for testing. Default is `_embed_with_fallback`.
In E2E tests, inject a local function that wraps `DefaultEmbeddingFunction`.

**Option B**: Create a thin wrapper that calls `DefaultEmbeddingFunction` and
returns `(embeddings, "test-local")` to match the `(list[list[float]], str)`
return type of `_embed_with_fallback`.

Option B requires no API changes and is simpler for an initial pass.

### Fixture Type3 PDF

The minimal hand-crafted Type3 PDF (stored as `tests/fixtures/type3_font.pdf`):

```
%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
           /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj
4 0 obj << /Type /Font /Subtype /Type3
           /FontBBox [0 0 500 700] /FontMatrix [0.001 0 0 0.001 0 0]
           /CharProcs << /A 6 0 R >> /Encoding << /Type /Encoding
           /Differences [65 /A] >> /FirstChar 65 /LastChar 65
           /Widths [500] >> endobj
5 0 obj << /Length 20 >> stream
BT /F1 12 Tf 100 700 Td (A) Tj ET
endstream endobj
6 0 obj << /Length 6 >> stream
0 0 m
endstream endobj
xref ...  trailer ... %%EOF
```

This is about 600 bytes and can be committed as-is (public domain, no content).

## Implementation Plan

### Phase 0 — Production Fix: PDF metadata extraction (prerequisite for AC-S1 metadata assertions)
1. Update `PDFExtractor._extract_markdown`: read `doc.metadata` inside the
   `with pymupdf.open(pdf_path) as doc:` block; add `pdf_*` keys to returned metadata dict
2. Update `PDFExtractor._extract_normalized`: same change
3. Update `doc_indexer._pdf_chunks`: map `pdf_title` → `source_title`,
   `pdf_author` → `source_author`, `pdf_creation_date` → `source_date`;
   add `pdf_subject`, `pdf_keywords` as new fields
4. Add docstring to `_pdf_chunks` documenting the intentional absence of
   `git_meta` (standalone use vs repo indexer asymmetry — see F7)

### Phase 1 — Fixtures (prerequisite for all tests)
5. Create `tests/fixtures/` directory
6. Add session-scoped fixture generators in `tests/conftest.py` using pymupdf:
   `simple_pdf` (with title/author metadata), `multipage_pdf` (semantically
   distinct page content per `_PAGE_TOPICS` constants)
7. Commit hand-crafted `tests/fixtures/type3_font.pdf` binary (~600 bytes).
   **Before committing**, validate with:
   ```bash
   python -c "
   import pymupdf
   doc = pymupdf.open('tests/fixtures/type3_font.pdf')
   fonts = doc[0].get_fonts()
   print(fonts)
   assert any(f[2] == 'Type3' for f in fonts), 'No Type3 font found!'
   print('Type3 validated OK')
   "
   ```
   If the binary is malformed (incorrect xref offsets), pymupdf will fail to
   open it or return an empty font list — either way the validation script
   catches it before the binary is committed.

### Phase 2 — Unit Tests
8. Write `tests/test_pdf_extractor_integration.py` (AC-U1 through AC-U8)
9. Write `tests/test_pdf_chunker_integration.py` (AC-U9 through AC-U11);
   all chunker tests use `PDFChunker(chunk_chars=100)` explicitly
10. Add `test_pdf_metadata_schema_complete` to `test_doc_indexer.py` asserting
    the full PDF chunk key set (19 existing fields + `pdf_subject` + `pdf_keywords`)
11. Verify all pass without mocks

### Phase 3 — Subsystem Tests
11. Write `tests/test_pdf_subsystem.py` (AC-S1 through AC-S6, AC-S2b)
12. `_index_pdf_file` git metadata test: `git init` + `git commit --allow-empty` in tmp_path

### Phase 4 — E2E Tests
13. Implement Option B local embed wrapper (no API changes needed):
    ```python
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    _local_ef = DefaultEmbeddingFunction()
    def _local_embed(chunks, model, api_key, input_type="document"):
        return [list(v) for v in _local_ef(chunks)], "test-local"
    ```
14. Write `tests/test_pdf_e2e.py` (AC-E1 through AC-E5)
15. Verify CI runs without any env vars set

### Phase 4b — Repo Indexer E2E (PDF routing via `index_repository`)
16. Add PDF bytes to `rich_repo` fixture in `test_indexer_e2e.py` using
    pymupdf (already a test dependency) to generate bytes in-memory:
    ```python
    # In rich_repo fixture, after creating text files:
    import pymupdf as _fitz
    pdf_doc = _fitz.open()
    pdf_page = pdf_doc.new_page()
    pdf_page.insert_text((72, 100), "Test Document for repo indexer.", fontsize=12)
    pdf_doc.set_metadata({"title": "Test Document", "author": "Test Author"})
    pdf_bytes = pdf_doc.tobytes()
    pdf_doc.close()
    (repo / "docs" / "test.pdf").parent.mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "test.pdf").write_bytes(pdf_bytes)
    ```
    This uses `write_bytes()` (not `write_text()`) and requires no external tools.
17. Add assertions to an existing or new E2E test confirming that `index_repository()`
    routes the PDF to `docs__` collection with correct metadata (AC-E5)

### Phase 5 — CI Validation
18. Run full test suite; confirm no new env-var requirements
19. Verify `tests/fixtures/*.pdf` committed correctly (binary files, not text-tracked)

## Acceptance Criteria

- [ ] **A0**: `PDFExtractor` extracts `doc.metadata` fields (`pdf_title`,
  `pdf_author`, `pdf_subject`, `pdf_keywords`, `pdf_creator`, `pdf_producer`,
  `pdf_creation_date`, `pdf_mod_date`) in both `_extract_markdown` and
  `_extract_normalized` paths; `_pdf_chunks` populates `source_title`,
  `source_author`, `source_date`, `pdf_subject`, `pdf_keywords` from them
- [ ] **A1**: `tests/fixtures/` exists with `simple.pdf` (with metadata),
  `multipage.pdf`, `type3_font.pdf`
- [ ] **A2**: All AC-U1 through AC-U11 tests pass with no mocks for the PDF
  extraction layer; metadata fields verified from real `doc.metadata`
- [ ] **A3**: All AC-S1 through AC-S6 (plus AC-S2b) tests pass; git metadata
  fields and PDF document metadata fields validated on chunks from `_index_pdf_file`
- [ ] **A4**: All AC-E1 through AC-E5 E2E tests pass using only
  `chromadb.EphemeralClient` + `DefaultEmbeddingFunction` (no API keys);
  `rich_repo` fixture includes a PDF and `index_repository()` routes it correctly
- [ ] **A5**: `pytest -x tests/` passes with no `VOYAGE_API_KEY` or
  `CHROMA_API_KEY` set in environment
- [ ] **A6**: New tests do not slow CI by more than 30 seconds total (pymupdf
  and DefaultEmbeddingFunction are fast; model download is cached in CI)

## Research Findings

### F1 — ONNX model already cached in CI ✓

`ci.yml:25-29` caches `~/.cache/chroma` with key `chromadb-onnx-${{ runner.os }}`.
The ONNX MiniLM-L6-v2 model is already warm across CI runs from existing T1 tests.
**Resolution**: Open question 1 is answered — no additional CI setup needed.

### F2 — conftest.py `local_t3` is function-scoped

`tests/conftest.py` defines a **function-scoped** `local_t3` fixture (not
session-scoped). `tests/fixtures/` directory does not exist yet. For E2E PDF
tests the function scope is acceptable (model is already loaded after first test
in the session), but adding a session-scoped `pdf_fixtures_dir` fixture to
generate the simple/multipage PDFs once is preferable for test speed.

### F3 — pymupdf cannot generate Type3 PDFs via Python API

PyMuPDF exposes no explicit API for creating Type3 font dictionaries.
`type3_font.pdf` must be a committed binary in `tests/fixtures/`.
The ~600-byte hand-crafted PDF in the Design section is confirmed viable.
**Resolution**: Open question 2 is answered — commit binary.

### F4 — `_index_pdf_file` has zero direct tests

`indexer.py:459-534` (`_index_pdf_file`) is completely untested. The E2E tests
in `test_indexer_e2e.py` call `index_repository()` but never assert on PDF
chunk metadata, git fields, or the docs__ collection PDF path. Confirmed gap.

### F5 — Additional unit test gaps beyond original AC list

`test_pdf_chunker.py` is missing: overlap calculation, `chunk_start_char` /
`chunk_end_char` metadata presence, and edge cases with empty pages.
`test_pdf_extractor.py` is missing: `_extract_normalized` direct test,
`page_count` in metadata, `format` field value (`"markdown"` vs `"normalized"`).
These are added to the AC list below.

### F6 — PDF document metadata not extracted ⚠ BUG

`PDFExtractor._extract_markdown` and `_extract_normalized` never call
`doc.metadata`. PyMuPDF exposes a `doc.metadata` dict with keys: `title`,
`author`, `subject`, `keywords`, `creator`, `producer`, `creationDate`,
`modDate`. Meanwhile, `doc_indexer._pdf_chunks` has hardcoded empty strings for
`source_title`, `source_author`, `source_date` (lines 237-239). **This is a
production bug** — PDF document metadata is silently discarded.

**Fix**: `PDFExtractor` must extract `doc.metadata` and include it as
`pdf_title`, `pdf_author`, `pdf_subject`, `pdf_keywords`, `pdf_creator`,
`pdf_producer`, `pdf_creation_date`, `pdf_mod_date` in `ExtractionResult.metadata`.
`_pdf_chunks` must then populate `source_title`, `source_author`, `source_date`
from those fields. Both extraction methods (`_extract_markdown` and
`_extract_normalized`) share the same `pymupdf.open()` context and can read
`doc.metadata` at no extra cost.

**`metadata` fixture PDF for tests**: When generating `simple.pdf`, embed
realistic metadata via `doc.set_metadata({"title": "Test Document",
"author": "Test Author", "subject": "Testing", "creationDate": "D:20260301000000"})`.

### F7 — `doc_indexer.index_pdf` vs `indexer._index_pdf_file` metadata asymmetry (intentional)

Confirmed intentional by design: standalone `index_pdf` has no repo context so
git fields are absent; `_index_pdf_file` (repo indexer) merges `git_meta`.
Action: document this in the `_pdf_chunks` docstring only — no code change.

### F8 — `doc.metadata` always returns `str`, never `None`

`pymupdf` Document always returns all standard metadata keys with `""` for
unset fields (never `None`, never missing). Confirmed by `_getMetadata()` which
returns `""` on any exception. This means all `pdf_*` fields in
`ExtractionResult.metadata` will always be `str` — no sanitization needed before
ChromaDB upsert. `doc.set_metadata(dict)` is the correct API for fixture generation.

### F9 — `PDFChunker` metadata keys confirmed; embed patch path confirmed

`TextChunk.metadata` has exactly 4 keys: `chunk_index`, `chunk_start_char`,
`chunk_end_char`, `page_number` (`pdf_chunker.py:60-65`). `doc_indexer._pdf_chunks`
reads these with `.get(..., 0)` fallback (`doc_indexer.py:246-247`).

Single patch target for all E2E tests: `patch("nexus.doc_indexer._embed_with_fallback")`
covers all three call sites — `doc_indexer.py:200`, `indexer.py:444`,
`indexer.py:522` — because `indexer.py` imports the function from `doc_indexer`.

### F10 — git repo setup pattern for subsystem tests

From `test_indexer_e2e.py:82-86`:
```python
subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "test@nexus"], cwd=repo, ...)
subprocess.run(["git", "config", "user.name", "Nexus Test"], cwd=repo, ...)
subprocess.run(["git", "add", "."], cwd=repo, ...)
subprocess.run(["git", "commit", "-m", "Initial corpus commit"], cwd=repo, ...)
```
No `--global` flags — all config is repo-local. Safe for CI.

### F11 — No PDF files in `test_indexer_e2e.py` fixtures; `upsert_chunks_with_embeddings` does zero sanitization

`rich_repo` and `mini_repo` contain only `.py`, `.md`, `.yaml` files — no PDFs.
The `_run_index()` PDF routing path is completely untested in the E2E suite.
A PDF file must be added to the `rich_repo` fixture to cover `_index_pdf_file`
being called from `index_repository()`.

`upsert_chunks_with_embeddings` (`t3.py:353-354`) explicitly opts out of
per-record validation with a comment. `chroma_quotas.py` validates byte sizes
only — no type checks. Since `doc.metadata` always returns `str` (F8), no
extra sanitization is needed.

## Open Questions

*(All resolved by research — see F1–F11 above)*

1. ~~DefaultEmbeddingFunction model download in CI~~ → **Resolved: F1 — already cached**
2. ~~Type3 PDF binary in git~~ → **Resolved: F3 — commit as binary**
3. ~~metadata asymmetry~~ → **Resolved: F7 — intentional, add docstring**
