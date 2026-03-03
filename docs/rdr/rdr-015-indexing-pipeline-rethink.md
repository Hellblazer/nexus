---
title: "Indexing Pipeline Rethink: Align Nexus with Arcaneum's Battle-Tested Implementation"
type: enhancement
status: closed
closed_date: 2026-03-02
close_reason: implemented
priority: P1
author: Hal Hildebrand
date: 2026-03-02
accepted_date: 2026-03-02
reviewed_by: self
supersedes: RDR-014
related_issues: []
---

# RDR-015: Indexing Pipeline Rethink: Align Nexus with Arcaneum's Battle-Tested Implementation

> **Supersedes RDR-014** (Knowledge Base Retrieval Quality: Code Context and Docs
> Deduplication). RDR-014 addressed two specific defects (missing context prefix,
> SemanticMarkdownChunker duplication) and remains the implementation specification
> for those two fixes. This RDR addresses the broader architectural question: why
> does nexus have its own pipeline at all, and what should change at the pipeline
> level? RDR-014 is preserved as the detailed fix record; this RDR governs scope
> and direction for the pipeline layer going forward.

---

## Problem

Nexus built its own chunking and indexing pipeline (`chunker.py`, `md_chunker.py`,
`indexer.py`) without fully auditing arcaneum's existing, battle-tested implementation
(`ast_chunker.py`, `ast_extractor.py`, `source_code_pipeline.py`).

**Root cause (R1):** A conscious "port, not import" decision was made correctly (nexus
and arcaneum have incompatible storage and embedding backends), but the scope of what to
port was assessed incompletely. The `ast_extractor.py` module — which contains
`DEFINITION_TYPES` for 14 languages and a robust `_extract_name()` helper — was added to
arcaneum on 2026-01-15, five weeks before nexus was initialized on 2026-02-21. It existed
and was available, but was not consulted. The absence of the RDR process at the time
(no "alternatives considered" step) left the gap undiscovered.

**What the audit found:**
- `DEFINITION_TYPES` (14 languages, class/method extraction): missing entirely from nexus
- `_extract_name()` (robust field-API name extraction): missing entirely
- AST language coverage: nexus has 16 extensions vs arcaneum's 53
- Internal inconsistency: `chunker.py:AST_EXTENSIONS` has 16 entries, `indexer.py:_EXT_TO_LANGUAGE` has 23 —
  7 languages get metadata tagged with no AST chunking (`.cxx`, `.kts`, `.sc`, `.m`, `.r`, `.php`, `.swift`
  present in `_EXT_TO_LANGUAGE` but absent from `AST_EXTENSIONS`)
- `SemanticMarkdownChunker` is genuinely novel (no arcaneum equivalent)
- Nexus has significant innovations arcaneum lacks (frecency, classification,
  staleness detection, pruning, ChromaDB CCE) — a wholesale replacement would be wrong

**Reverse finding (R5):** Arcaneum's `markdown/chunker.py:202–215` has the **same**
structural token duplication bug as nexus's `md_chunker.py` (RDR-014 Defect 2). The
fix should flow from nexus back to arcaneum.

**Implementation status:** As of this RDR, neither RDR-014 Fix 1 (context prefix) nor
Fix 2 (markdown dedup blocklist) has been applied to `nexus/src/nexus/` — both are
specifications awaiting implementation. All fixes in this RDR are similarly pre-implementation.

---

## Proposed Solutions

### Fix A — Port `DEFINITION_TYPES` + `_extract_name` (P1, prerequisite for RDR-014 Fix 1)

Into `src/nexus/indexer.py`:
- **Copy** `DEFINITION_TYPES` dict from `arcaneum/src/arcaneum/indexing/fulltext/ast_extractor.py:51–136` (14 languages)
- **Copy** `_extract_name()` helper from `arcaneum/src/arcaneum/indexing/fulltext/ast_extractor.py:366–386`
- **Author new** `_extract_context(source, language, start, end)` — this function does **not** exist in arcaneum; it must be written from scratch using the depth-first walk algorithm specified in RDR-014

~100 LOC. No new dependencies.

### Fix B — Expand AST language coverage (P2)

Expand `chunker.py:AST_EXTENSIONS` from 16 to a curated subset of arcaneum's
`ast_chunker.py:LANGUAGE_MAP` (53 entries). Not all 53 extensions are appropriate —
nexus routes `.md`, `.yaml`, `.json`, `.toml`, `.html` to prose or specialized pipelines;
these must NOT be added to `AST_EXTENSIONS`. Verify each new extension against
`tree-sitter-language-pack.get_parser()` before adding (e.g., Objective-C `.m` is
in `indexer.py:_EXT_TO_LANGUAGE` but has no tree-sitter parser in the pack).

Simultaneously reconcile `indexer.py:_EXT_TO_LANGUAGE` (23 entries, including `.cxx`,
`.kts`, `.sc`, and `.m`) with the curated expansion. Extensions in `_EXT_TO_LANGUAGE`
that have no parser available should be removed or flagged with a `None` language value.
~30–50 LOC depending on curation decisions. Requires a decision table of which extensions
to include/exclude (tracked as a pre-implementation step).

### Fix C — Context prefix injection for prose and PDF chunks (P1)

Apply the same embed-only context prefix pattern from RDR-014 Fix 1 to prose and PDF
pipelines. Both nexus and arcaneum already track the necessary metadata — neither
currently uses it as an embedding-time prefix.

**Markdown chunks:** Both pipelines track `header_path` (e.g., `"Introduction > Background"`).
Inject as embedding prefix — raw chunk text stored in ChromaDB as usual (R9 pattern):

```
## Section: Introduction > Background

<original chunk text>
```

**PDF chunks:** Both pipelines track `page_number` and PDF document `title` metadata.
Inject as embedding prefix:

```
## Document: <title>  Page: <N>

<original chunk text>
```

**Implementation:** Same two-list (`embed_texts` vs `documents`) pattern as RDR-014 Fix 1,
applied in `indexer._index_prose_file()` and `indexer._index_pdf_file()`.

Actual field names at injection points (verified against `doc_indexer.py` and `indexer.py`):
- **Markdown** (`_index_prose_file()`): read `chunk.metadata["header_path"]` (stored as `section_title` in ChromaDB metadata via `indexer.py:391`). No new field needed.
- **PDF** (`_index_pdf_file()` via `_pdf_chunks()`): read `chunk.metadata["page_number"]` and `chunk.metadata["source_title"]` (derived from `pdf_title` in extraction result). The field is `source_title`, not `title` — `indexer.py:510` overwrites `title` with a file-path-based string. Use `source_title` explicitly.

**Re-indexing required:** Yes. All `docs__` and `rdr__` and `pdf__` collections must be
re-indexed with `--force`.

### Fix C2 — `preserve_code_blocks` for SemanticMarkdownChunker (P2)

Add `preserve_code_blocks: bool = True` option to prevent splitting fenced code blocks
mid-content. Add `has_code_blocks: bool` metadata field per chunk. ~20 LOC.
Source: `arcaneum/markdown/chunker.py:274–361`.

### Fix D — Backport markdown dedup fix to arcaneum (P2)

Arcaneum `markdown/chunker.py:202–215` has the identical `_token_content` duplication
bug as nexus. Apply the RDR-014 Fix 2 blocklist approach there as well. Tracked as
arcaneum work, not nexus work.

### Fix E — Add critical test coverage (P1)

Nexus has 1,080 tests across 55 files — more than arcaneum's 708 across 37 files —
but has zero tests for its most critical pipeline paths (R8). The goal is to
**adapt arcaneum's testing patterns** to nexus's constraints (ChromaDB, Voyage AI);
not to copy arcaneum test code wholesale.

Five test files to add, in priority order:

| New File | What It Tests | Gap Severity |
|----------|--------------|--------------|
| `test_chunker_ast_languages.py` | AST chunking for Python, JS, Go, Rust, Java correctness | **CRITICAL** |
| `test_md_chunker_semantic_integrity.py` | Code block / list / table preservation in `SemanticMarkdownChunker` | **HIGH** |
| `test_pdf_extractor_normalization.py` | Tab/Unicode/excess-newline normalization edge cases | **HIGH** |
| `test_doc_indexer_hash_sync.py` | Hash-based skip when content unchanged; re-index when changed | **MEDIUM** |
| `test_indexer_chunk_flow.py` | Chunks → embed (Voyage stub) → upsert (ChromaDB stub) end-to-end | **MEDIUM** |

**Pattern to follow:** Arcaneum uses `@pytest.mark.skipif(not TREE_SITTER_AVAILABLE, ...)` guards,
`session`-scoped fixtures for temp dirs, and `unittest.mock.patch` for external services (Qdrant,
MeiliSearch). Nexus should apply the same guards and stub Voyage AI + ChromaDB CloudClient,
not add new production dependencies for testing.

**Sequencing:** `test_chunker_ast_languages.py`, `test_md_chunker_semantic_integrity.py`,
`test_pdf_extractor_normalization.py`, and `test_doc_indexer_hash_sync.py` have no
dependency on other fixes and should be written first per TDD discipline. **Fix A must be
merged before** any `_extract_context()` test cases in `test_indexer_chunk_flow.py` are
written — attempting to test that function before Fix A exists will fail to import.

**Adaptation notes:**
- Replace Qdrant/MeiliSearch mocks with `chromadb.EphemeralClient` or `MagicMock()` stubs
- Voyage AI calls: mock `voyageai.Client.embed` to return `[[0.1] * 1024]` per chunk
- Test `_extract_context(source, language, start, end)` directly — only after Fix A is merged
- `SemanticMarkdownChunker` tests: exercise the `preserve_code_blocks` path added by Fix C2

~200 LOC across 5 files.

### Do NOT port

- ProcessPoolExecutor parallelism (Voyage AI API latency is the bottleneck, not file I/O)
- Qdrant/MeiliSearch dual indexing (architectural incompatibility)
- GPU-accelerated local embedding (nexus uses Voyage AI by design)
- `source_code_pipeline.py` orchestration (nexus's pipeline has superior incremental
  update logic, staleness detection, and pruning)

---

## Alternatives Considered

**Replace nexus pipeline wholesale with arcaneum's (rejected):**
Nexus has 11 genuine innovations absent from arcaneum (frecency scoring, file
classification + dual-collection routing, per-file SHA-256 staleness detection, deleted-file
pruning, misclassification pruning, ChromaDB CCE, `.nexus.yml` per-repo config, HEAD
polling auto-reindex, RDR collection routing, byte-cap enforcement for CloudClient limits,
character offset tracking in markdown chunks). A wholesale replacement would discard all
of this. The correct approach is targeted gap closure, not replacement.

**Import arcaneum as a Python dependency (rejected):**
Would pull in Qdrant client, MeiliSearch client, GPU embedding stack, and dozens of
other transitive dependencies that nexus does not need. The "port, not import" decision
was architecturally correct.

**Java-only context extraction (rejected — R11, RDR-014):**
The original RDR-014 Fix 1 proposal was Java-only regex. Research surfaced arcaneum's
13-language `DEFINITION_TYPES` as prior art, making the regex approach both unnecessary
and inferior.

---

## Research Findings

### R1: Root cause — H3+H4: conscious "port not import" + incomplete scope assessment (confirmed)

**Source:** `nexus/docs/architecture.md:53,58–63`, git log of both repos

`architecture.md:53` explicitly states: *"Ported, not imported — SeaGOAT and Arcaneum
patterns rewritten in Nexus module structure."* The porting decision was conscious and
correct (incompatible backends). However, arcaneum's `ast_extractor.py` — which contained
`DEFINITION_TYPES` (14 languages) and `_extract_name()` — was committed on 2026-01-15,
five weeks before nexus was initialised (2026-02-21). It was available and was not consulted.
Absence of the RDR process meant no "alternatives considered" step was ever performed.
H2 (nexus built first) is fully refuted — arcaneum is 4 months older.

### R2: DEFINITION_TYPES gap discovered via RDR-014 (confirmed)

See RDR-014 R7 and R11. Direct trigger for RDR-015.

### R3: Code pipeline gap inventory (confirmed)

**Source:** Code comparison, `chunker.py:11–28`, `indexer.py:_EXT_TO_LANGUAGE`, `arcaneum/ast_chunker.py:40–95`

| Gap | Arcaneum | Nexus | Priority |
|-----|---------|-------|----------|
| G1 DEFINITION_TYPES (14 lang) | `ast_extractor.py:51–136` | Missing | P1 |
| G2 `_extract_name()` | `ast_extractor.py:366–386` | Missing | P1 |
| G3 AST language coverage | 53 extensions | 16 extensions | P2 |
| G4 Internal lang map inconsistency | n/a | `AST_EXTENSIONS` 16 ≠ `_EXT_TO_LANGUAGE` 23 | P2 |
| G5 Minified code handling | `_split_long_line` | Missing | P3 |

### R7: Prose and PDF pipeline gap inventory (confirmed)

**Source:** `arcaneum/markdown/chunker.py`, `arcaneum/indexing/fulltext/pdf_extractor.py`,
`nexus/src/nexus/md_chunker.py`, `nexus/src/nexus/pdf_extractor.py`

**PDF extraction (largely equivalent):** Both pipelines implement the same 3-tier strategy
(PyMuPDF4LLM → pdfplumber rescue → normalized fallback), same page boundary tracking, same
table detection, same 8-field PDF metadata. No porting needed for extraction logic.

**Markdown chunking (mostly equivalent):** Both use markdown-it-py AST, heading hierarchy
with `header_path`, character offsets. Key gaps:

| Gap | Arcaneum | Nexus | Priority |
|-----|---------|-------|----------|
| G7 Context prefix for prose embedding | Not present | Not present | **P1 (new Fix C)** |
| G8 Context prefix for PDF embedding | Not present | Not present | **P1 (new Fix C)** |
| G9 `preserve_code_blocks` option | `markdown/chunker.py:274–361` | Missing | P2 |
| G10 `has_code_blocks` metadata field | Present | Missing | P2 |
| G11 OCR (Tesseract/EasyOCR) | `ocr.py:154–462` | Missing | P3 (optional) |
| G12 Markdown dedup fix (same bug) | Bug present (Fix D target) | Bug present (RDR-014 Fix 2) | P1 |

**Key insight (R6 G7/G8):** Both nexus and arcaneum already track `header_path` in markdown
chunks and `page_number` + document `title` in PDF chunks. Neither uses these as
embedding-time context prefixes. This is the same gap as code chunks (RDR-014 Defect 1)
applied to prose and PDF — and the same embed-only prefix solution applies.

### R4: Full novelty inventory — do not replace (confirmed)

**Source:** `indexer.py`, `frecency.py`, `classifier.py`, `md_chunker.py`, `db/t3.py`

Nexus has 11 genuine innovations absent from arcaneum: frecency scoring, file
classification + dual-collection routing, per-file SHA-256 staleness detection,
deleted-file pruning, misclassification pruning, ChromaDB CCE (`voyage-context-3`
for prose), `.nexus.yml` per-repo config, HEAD polling auto-reindex, RDR collection
routing, 16KB byte-cap enforcement for ChromaDB CloudClient, character offset tracking
in markdown chunks. A wholesale replacement would discard all of this.

### R5: Arcaneum has the same markdown dedup bug (confirmed)

**Source:** `arcaneum/src/arcaneum/indexing/markdown/chunker.py:202–215`

The `_extract_token_content` method has the identical `token.map` fallback that causes
`paragraph_open` to duplicate `inline` content — the same root cause as RDR-014 Defect 2.
The RDR-014 Fix 2 blocklist should be backported to arcaneum.

### R6: Scope estimate — "copy 3 functions", not "rewrite the indexer" (confirmed)

P1+P2 fixes: ~130–160 LOC across 2–3 files (Fix B scope now 30–50 LOC with curation work). No new dependencies.
Fix E: ~200 LOC across 5 test files.
Prose/PDF audit (R7): complete — no new extraction work needed.

### R8: Testing audit — nexus has more tests overall but critical pipeline paths have zero coverage (confirmed)

**Source:** Full audit of `arcaneum/tests/` (37 files, 708 functions) vs `nexus/tests/` (55 files, 1,080 functions)

Nexus has 53% more tests than arcaneum overall, but is missing coverage in the most
important areas:

| Test Area | Arcaneum | Nexus | Gap |
|-----------|----------|-------|-----|
| AST language coverage (10+ languages) | 32 tests | **0 tests** | **CRITICAL** |
| AST function extraction (qualified names, nested, decorated) | 47 tests | **0 tests** | **CRITICAL** |
| Markdown code block / list / table preservation | 6 tests | **0 tests** | HIGH |
| PDF whitespace normalization (tabs, Unicode, excess newlines) | 11 tests | **0 tests** | HIGH |
| PDF hash-based change detection | 7 tests | 5 tests (doc_indexer only) | MEDIUM |
| PDF page metadata accuracy | 8 tests | 3 tests (basic) | MEDIUM |
| Indexer chunk→embed→upsert E2E | 10 tests | 3 tests (status only) | MEDIUM |
| Markdown header path (full hierarchy) | 6 tests | 4 tests (basic) | LOW |

**Key finding:** Nexus `test_chunker.py` tests the line-based fallback path only. The AST
code path through `tree-sitter-language-pack` is completely untested despite being the
primary path for all supported languages. Similarly, `test_md_chunker.py` tests frontmatter
and basic heading hierarchy but not code-block / table / list structural preservation.

**Adaptation principle confirmed:** Arcaneum's test patterns (skip guards, session fixtures,
service mocks) are directly adaptable to nexus's constraints (ChromaDB EphemeralClient,
Voyage AI stub, no Qdrant/MeiliSearch). No new test dependencies needed.

---

## Open Questions

1. ~~**Full gap inventory?**~~ **Resolved (R3):** See gap table. G1–G5 for code pipeline.
   G6 (prose/PDF) is the remaining open investigation.

2. ~~**What is genuinely novel in nexus?**~~ **Resolved (R4):** 11 innovations documented.
   `SemanticMarkdownChunker` is genuinely novel — keep and fix (RDR-014).

3. ~~**Storage layer orthogonality?**~~ **Resolved (R1):** Storage divergence is real but
   orthogonal to chunking. Chunking/extraction layers are fully separable.

4. ~~**Scope of change?**~~ **Resolved (R6):** "Copy 3 functions" fix, ~130 LOC, 3–5 hours.

5. ~~**RDR-014 sequencing?**~~ **Resolved:** RDR-014 is fully compatible. It specifies the
   correct implementation of Fix A (context prefix). Implement RDR-014 first, then Fix B
   (language expansion).

6. ~~**Prose and PDF pipeline audit?**~~ **Resolved (R7):** PDF extraction is already
   equivalent. Markdown chunking is mostly equivalent with two gaps: `preserve_code_blocks`
   (P2) and the shared markdown dedup bug (Fix D). The primary new work is context prefix
   injection for prose and PDF embeddings (Fix C) — both pipelines already carry the
   required metadata (`header_path`, `page_number`, document `title`); neither injects
   it as an embedding prefix.

7. ~~**What does arcaneum's test suite teach us? What's missing?**~~ **Resolved (R8):**
   Nexus has zero tests for AST chunking correctness (CRITICAL) and zero tests for
   markdown structural preservation (HIGH). Fix E adds 5 targeted test files (~200 LOC)
   adapting arcaneum's testing patterns to nexus's ChromaDB/Voyage constraints.

---

## Validation

### Fix A (DEFINITION_TYPES + context prefix)
Per RDR-014 Fix 1 success criteria — three failing ART queries must resolve to correct
files in top-3 after `--force` re-index of `code__ART-8c2e74c0`.

### Fix B (language expansion)
- Fix E must be merged before Fix B; the AST language tests are the regression baseline
- `chunker.py:AST_EXTENSIONS` and `indexer.py:_EXT_TO_LANGUAGE` have identical key sets after reconciliation
- Kotlin (`.kt`), Swift (`.swift`), Scala (`.scala`), PHP (`.php`) files are AST-chunked
  (not line-based) in a test index pass
- `pytest tests/test_chunker_ast_languages.py` (from Fix E) passes for Python, Java, Go — Fix B must not break these

### Fix C (prose context prefix)
- Re-index `docs__ART-8c2e74c0` and `rdr__nexus-*` with `--force`
- **Baseline step (before re-indexing):** Record current top-3 results for a prose-domain
  query against `docs__ART-8c2e74c0` — choose a concept that appears in one specific section
  of the ART documentation but whose vocabulary co-occurs incidentally in other sections.
  Example: `nx search "weight update learning rule" --corpus docs__ART-8c2e74c0 -m 3`.
  After re-indexing, the same query should surface the documented weight-update section as
  top-1, not a section that merely mentions "weight" in a different context.
- **Deduplication regression check:** `nx search "cognitive pipeline log-polar" --corpus docs__ART-8c2e74c0 -m 1 --json`
  — `content` field contains no repeated sentences (Fix 2 dedup must remain clean after Fix C prefix is added)
- **RDR collection check:** `nx search "embed-only prefix" --corpus rdr__nexus-*` — top
  result should be one of the RDR-014 or RDR-015 sections that document this pattern
- **PDF:** `nx search "classification algorithm" --corpus pdf__* -m 3` — top results should
  include document title and page number in stored metadata (unchanged) and embedding
  quality should improve for title-heavy queries

**Note:** `nx search "vigilance match criterion"` is a *code* collection query (RDR-014
Fix 1 validation) — it must be run against `code__ART-8c2e74c0`, not `docs__ART-8c2e74c0`.

### Fix C2 (preserve_code_blocks)
- Markdown chunks containing fenced code blocks are not split mid-block
- `has_code_blocks: True` present on chunks that contain code fences

### Fix D (arcaneum backport)
- Arcaneum `markdown/chunker.py` dedup fix applied and tested
- Arcaneum test suite passes

### Fix E (test coverage)
- `pytest tests/test_chunker_ast_languages.py` passes: Python, JS, Go produce ≥2 AST chunks each
- `pytest tests/test_md_chunker_semantic_integrity.py` passes: code block not split, list intact, table pipes preserved
- `pytest tests/test_pdf_extractor_normalization.py` passes: tabs, U+00A0, excess newlines, combined edge cases all normalized
- `pytest tests/test_doc_indexer_hash_sync.py` passes: unchanged file skips extraction; modified file re-embeds
- `pytest tests/test_indexer_chunk_flow.py` passes: Python + markdown files produce chunks, Voyage stub called, ChromaDB upsert called
- `pytest tests/` — full suite passes with no regressions
- All new tests skip cleanly when `tree-sitter-language-pack` unavailable (via `pytest.mark.skipif`)
