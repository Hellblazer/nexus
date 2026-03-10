---
title: "Indexer Module Decomposition and Configuration Externalization"
id: RDR-032
type: Technical Debt
status: closed
accepted_date: 2026-03-09
closed_date: 2026-03-09
close_reason: implemented
priority: P3
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-014", "RDR-015", "RDR-016", "RDR-017", "RDR-025", "RDR-028"]
related_tests: []
implementation_notes: ""
---

# RDR-032: Indexer Module Decomposition and Configuration Externalization

## Problem Statement

Two related technical debt issues are creating maintenance friction:

### 1. indexer.py Monolith (1,325 lines)

`src/nexus/indexer.py` mixes:
- File classification dispatch
- Code file indexing (`_index_code_file`, 12 parameters)
- Prose file indexing (`_index_prose_file`, 12 parameters)
- PDF file indexing (`_index_pdf_file`)
- RDR file indexing
- Frecency score computation
- Misclassification pruning
- Deleted file pruning
- Context extraction (`_extract_context`)

The `_index_code_file` and `_index_prose_file` functions each take 12 parameters including `col: object` and `db: object` — the `object` type annotations hide the actual protocol expected. Worse, the parameter types are asymmetric: `_index_code_file` (line 546) takes `voyage_client: object` (a pre-constructed `voyageai.Client` instance), while `_index_prose_file` (line 675) and `_index_pdf_file` (line 849) take `voyage_key: str` (a raw API key string). The prose and PDF paths delegate to `doc_indexer._embed_with_fallback`, which constructs its own client internally from the key. This means the caller must know which form to pass depending on the file type — a leaky abstraction that an `IndexContext` must resolve.

### 2. Hard-Coded Tuning Constants

8 constants are scattered across 6 files with no configuration override:

| Constant | File | Value | Impact |
|----------|------|-------|--------|
| Vector/frecency weights | scoring.py:48 | 0.7/0.3 | Search ranking |
| File-size threshold | scoring.py:16 | 30 chunks | Large file penalty |
| Frecency decay rate | frecency.py:53 | 0.01 | Recency bias |
| Chunk size | chunker.py:14 | 150 lines | Chunk granularity |
| PDF chunk size | pdf_chunker.py:11 | 1500 chars | PDF chunk granularity |
| Git log timeout | frecency.py:21 | 30s/60s | Frecency reliability |
| Ripgrep timeout | ripgrep_cache.py:91 | 10s | Hybrid search speed |
| CCE token estimate | doc_indexer.py:67 | len/3 | Batch boundary accuracy |

## Context

- The indexer is the most-changed module (touched by RDR-014, 015, 016, 017, 025, 028)
- `.nexus.yml` already exists as a per-project config mechanism but only controls `exclude_patterns`
- Duplicated patterns: credential checking (3 locations), staleness check (3 locations)

## Research Findings

### F1: Dependency Graph is Acyclic (Verified — source inspection)

The proposed module split will not create circular imports. `indexer.py` (orchestrator) imports `code_indexer` and `prose_indexer`. Both import from `indexer_utils`. `code_indexer` imports from `nexus.chunker` and `nexus.languages`. `prose_indexer` imports from `nexus.doc_indexer` and `nexus.md_chunker`. No reverse dependencies exist — `chunker`, `doc_indexer`, `md_chunker`, and `languages` do not import from `indexer`.

### F2: 12-Parameter Functions are Primary Friction (Verified — code audit)

The `_index_code_file` (line 546) and `_index_prose_file` (line 675) functions take 12 parameters including 4 untyped `object` parameters (`col`, `db`, `voyage_client`/`voyage_key`) that hide protocols. The parameter types are asymmetric: `_index_code_file` takes a pre-constructed `voyageai.Client`, while `_index_prose_file` and `_index_pdf_file` take a raw `voyage_key: str`. `IndexContext` eliminates this positional coupling.

### F3: LANGUAGE_REGISTRY Stability (Verified — RDR-025, commit 32803fb)

`LANGUAGE_REGISTRY` is a module-level dict defined in `nexus/languages.py` with no runtime state. Used at one call site in `_index_code_file` (line 576) to map file extensions to tree-sitter language names. This is a clean, stable dependency for the extracted `code_indexer.py`.

## Proposed Solution

### Track A: Module Split

Extract three focused modules from `indexer.py`:

```
src/nexus/
  indexer.py          # Orchestrator: classify, dispatch, coordinate (remains, ~300 lines)
  code_indexer.py     # _index_code_file + _extract_context (~250 lines)
  prose_indexer.py    # _index_prose_file (~200 lines)
```

**PDF indexing**: `pdf_chunker.py` and `pdf_extractor.py` already exist as separate modules, and `_index_pdf_file` already delegates to `doc_indexer._pdf_chunks` and `doc_indexer._embed_with_fallback`. Rather than creating a fourth PDF-named module (`pdf_indexer.py`) alongside these two, `_index_pdf_file` stays in the orchestrator (`indexer.py`) — it is thin glue (~60 lines of logic beyond delegation) and its extraction would fragment the PDF surface area across three modules without reducing complexity. If future growth warrants extraction, it should consolidate into `doc_indexer.py` rather than proliferate a new file.

Shared utilities extracted to `indexer_utils.py`:
- `check_staleness(col, source_file, content_hash, embedding_model)` → bool
- `check_credentials(voyage_key, chroma_key)` → raises CredentialsMissingError
- `build_context_prefix(filename, definition_name, line_start, line_end)` → str

**Implementation note on `check_staleness`**: The current inline staleness pattern performs a `col.get()` call wrapped in `_chroma_with_retry` (imported from `nexus.retry`). The extracted `check_staleness` utility must import and use `_chroma_with_retry` internally rather than expecting the caller to wrap the call — the retry logic is part of the staleness check's contract, not an optional decoration.

Replace 12-parameter functions with an `IndexContext` dataclass:
```python
@dataclass
class IndexContext:
    col: Collection
    db: T3Database
    voyage_key: str              # raw API key — single source of truth
    voyage_client: voyageai.Client  # pre-constructed for code path
    repo_path: Path
    corpus: str
    # ... etc
```

`IndexContext` carries **both** the key and a pre-constructed client. `code_indexer` uses `ctx.voyage_client` directly (as today); `prose_indexer` and `_index_pdf_file` pass `ctx.voyage_key` to `doc_indexer._embed_with_fallback` (which constructs its own client internally). This preserves existing behavior while making the asymmetry explicit and centralized.

**Note on RDR-025 dependency**: `code_indexer.py` will carry a dependency on `nexus.languages.LANGUAGE_REGISTRY` (introduced by RDR-025, commit 32803fb). The import is used in `_index_code_file` to resolve file extensions to tree-sitter languages. This is a clean, stable dependency — `LANGUAGE_REGISTRY` is a module-level dict, not a runtime service.

### Track B: Configuration Externalization

Add a `[tuning]` section to `.nexus.yml`:

```yaml
tuning:
  scoring:
    vector_weight: 0.7
    frecency_weight: 0.3
    file_size_threshold: 30
  frecency:
    decay_rate: 0.01
  chunking:
    code_chunk_lines: 150
    pdf_chunk_chars: 1500
  timeouts:
    git_log: 30
    ripgrep: 10
```

Load via existing `config.py` with fallback to current hard-coded defaults.

## Alternatives Considered

**A. Keep indexer.py as-is**: It works. But every RDR that touches indexing (RDR-014, 015, 016, 017, 025, 028) requires understanding 1,325 lines. Maintenance cost is growing.

**B. Full plugin architecture**: Over-engineered. The four file types (code, prose, PDF, RDR) are stable and unlikely to grow. Simple module extraction is sufficient.

## Trade-offs

**Benefits**:
- Each module is independently testable and understandable
- Type-safe `IndexContext` replaces 12 untyped parameters
- Users can tune scoring for their repos without code changes
- Reduced merge conflicts when multiple RDRs touch the indexer

**Risks**:
- Module extraction is a large diff (high merge conflict risk if done alongside other work)
- Config externalization adds validation and migration burden
- Defaults must remain backward-compatible

## Implementation Plan

### Phase 1: Module Extraction
1. Create `IndexContext` dataclass (carries both `voyage_key` and `voyage_client`)
2. Extract `code_indexer.py` with `index_code_file(ctx, file_path)` (carries `LANGUAGE_REGISTRY` dependency)
3. Extract `prose_indexer.py` with `index_prose_file(ctx, file_path)`
4. Extract shared utilities to `indexer_utils.py` (including `check_staleness` with internal `_chroma_with_retry`)
5. Keep `_index_pdf_file` in `indexer.py` orchestrator (thin delegation to existing `doc_indexer`/`pdf_chunker`/`pdf_extractor`)
6. Reduce `indexer.py` to orchestrator role (~300 lines)
7. Verify all existing tests pass without modification

### Phase 2: Configuration
8. Add `TuningConfig` to `config.py`
9. Add `[tuning]` section to `.nexus.yml` schema
10. Replace hard-coded constants with config lookups
11. Add defaults matching current values
12. Add `nx config show tuning` command

## Test Plan

- Unit: each extracted module independently testable
- Unit: IndexContext creation and validation
- Unit: TuningConfig loading with defaults, overrides, and invalid values
- Regression: full test suite passes unchanged after extraction
- Integration: `.nexus.yml` tuning overrides affect search/index behavior. Specific tests:
  - Set `chunking.code_chunk_lines: 50` in `.nexus.yml`, index a file >50 lines, verify all chunks are ≤50 lines (vs. default 150)
  - Set `scoring.vector_weight: 1.0, frecency_weight: 0.0`, verify frecency scores have zero effect on ranking
  - Set `timeouts.ripgrep: 0.001`, verify hybrid search gracefully degrades (returns vector-only results)

## Finalization Gate

### Contradiction Check

The original draft proposed a `pdf_indexer.py` module while `pdf_chunker.py` and `pdf_extractor.py` already exist. This has been resolved: `_index_pdf_file` remains in the orchestrator since it is thin glue over existing `doc_indexer` functions. No new PDF-named module is introduced.

The `IndexContext` originally showed only `voyage_client` but `_index_prose_file` and `_index_pdf_file` take `voyage_key: str`. Resolved: `IndexContext` carries both `voyage_key` and `voyage_client`, preserving existing call semantics for each path.

### Assumption Verification

- **Assumption**: Extracted modules will not create circular imports.
  **Verified**: The dependency graph is acyclic. `indexer.py` (orchestrator) imports `code_indexer` and `prose_indexer`. Both import from `indexer_utils`. `code_indexer` imports from `nexus.chunker` and `nexus.languages`. `prose_indexer` imports from `nexus.doc_indexer` and `nexus.md_chunker`. No reverse dependencies exist — `chunker`, `doc_indexer`, `md_chunker`, and `languages` do not import from `indexer`.

- **Assumption**: The 12-parameter functions are the primary source of friction.
  **Verified**: The parameter lists include 4 untyped `object` parameters (`col`, `db`, `voyage_client`/`voyage_key`) that hide protocols, plus 6 value parameters that are identical across all three file-type functions. `IndexContext` eliminates the positional coupling.

- **Assumption**: `LANGUAGE_REGISTRY` (RDR-025) is a stable dependency for `code_indexer.py`.
  **Verified**: It is a module-level dict defined in `nexus/languages.py` with no runtime state. It is used at one call site in `_index_code_file` (line 576) to map file extensions to tree-sitter language names.

### Scope Verification

The RDR is scoped to two tracks: module extraction (Track A) and configuration externalization (Track B). It does not attempt to change indexing semantics, modify the chunking pipeline, or alter embedding model selection. The `.nexus.yml` tuning section adds read-only config for existing constants — no new behavioral features.

### Cross-Cutting Concerns

- **Retry logic**: `check_staleness` extraction must internalize `_chroma_with_retry` (from `nexus.retry`) rather than leaving retry responsibility to callers. This is documented in the implementation notes above.

- **PDF module surface area**: Three existing PDF-related modules (`pdf_chunker.py`, `pdf_extractor.py`, `doc_indexer.py`) already handle PDF processing. The decision to keep `_index_pdf_file` in the orchestrator avoids adding a fourth module and fragmenting the PDF code path further. If PDF indexing grows in complexity, consolidation into `doc_indexer.py` is the preferred path.

- **Config migration**: Track B must ensure that repos without a `[tuning]` section in `.nexus.yml` behave identically to current behavior. The `TuningConfig` defaults are the current hard-coded values — no migration needed for existing users.

### Proportionality

Track A and Track B are **independently deliverable and parallelizable**. Track A (module extraction) is a pure refactor with no behavioral change — it can land as a single PR with no config changes. Track B (config externalization) can proceed independently by replacing hard-coded constants with config lookups in the existing monolithic `indexer.py` if needed. However, the cleanest sequencing is Track A first (to establish module boundaries), then Track B (to thread config through the new `IndexContext`). Neither track is blocked on the other.

The scope is proportional to the problem: 6 RDRs have modified `indexer.py`, each requiring understanding of 1,325 lines. The extraction reduces the cognitive load per module to 200-300 lines while preserving all existing tests unchanged.

## References

- Current indexer: `src/nexus/indexer.py` (1,325 lines)
- Config system: `src/nexus/config.py`
- `.nexus.yml` schema: `src/nexus/config.py`
