---
title: "Indexer Module Decomposition and Configuration Externalization"
id: RDR-032
type: Technical Debt
status: draft
priority: P3
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-015"]
related_tests: []
implementation_notes: ""
---

# RDR-032: Indexer Module Decomposition and Configuration Externalization

## Problem Statement

Two related technical debt issues are creating maintenance friction:

### 1. indexer.py Monolith (1,236 lines)

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

The `_index_code_file` and `_index_prose_file` functions each take 12 positional parameters including `col: object`, `db: object`, and `voyage_client: object` — the `object` type annotations hide the actual protocol expected.

### 2. Hard-Coded Tuning Constants

7+ constants are scattered across 5 files with no configuration override:

| Constant | File | Value | Impact |
|----------|------|-------|--------|
| Vector/frecency weights | scoring.py:48 | 0.7/0.3 | Search ranking |
| File-size threshold | scoring.py:16 | 30 chunks | Large file penalty |
| Frecency decay rate | frecency.py:53 | 0.01 | Recency bias |
| Chunk size | chunker.py:53 | 150 lines | Chunk granularity |
| PDF chunk size | pdf_chunker.py:11 | 1500 chars | PDF chunk granularity |
| Git log timeout | frecency.py:21 | 30s/60s | Frecency reliability |
| Ripgrep timeout | ripgrep_cache.py:91 | 10s | Hybrid search speed |
| CCE token estimate | doc_indexer.py:67 | len/3 | Batch boundary accuracy |

## Context

- The indexer is the most-changed module (touched by RDR-014, 015, 016, 017, 028)
- `.nexus.yml` already exists as a per-project config mechanism but only controls `exclude_patterns`
- Duplicated patterns: credential checking (3 locations), staleness check (3 locations)

## Proposed Solution

### Track A: Module Split

Extract four focused modules from `indexer.py`:

```
src/nexus/
  indexer.py          # Orchestrator: classify, dispatch, coordinate (remains, ~300 lines)
  code_indexer.py     # _index_code_file + _extract_context (~250 lines)
  prose_indexer.py    # _index_prose_file (~200 lines)
  pdf_indexer.py      # _index_pdf_file (moves from indexer.py, ~150 lines)
```

Shared utilities extracted to `indexer_utils.py`:
- `check_staleness(col, source_file, content_hash, embedding_model)` → bool
- `check_credentials(voyage_key, chroma_key)` → raises CredentialsMissingError
- `build_context_prefix(filename, definition_name, line_start, line_end)` → str

Replace 12-parameter functions with a `IndexContext` dataclass:
```python
@dataclass
class IndexContext:
    col: Collection
    db: T3Database
    voyage_client: voyageai.Client
    repo_path: Path
    corpus: str
    # ... etc
```

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

**A. Keep indexer.py as-is**: It works. But every RDR that touches indexing (RDR-014, 015, 016, 017, 028) requires understanding 1,236 lines. Maintenance cost is growing.

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
1. Create `IndexContext` dataclass
2. Extract `code_indexer.py` with `index_code_file(ctx, file_path)`
3. Extract `prose_indexer.py` with `index_prose_file(ctx, file_path)`
4. Move PDF indexing to `pdf_indexer.py`
5. Extract shared utilities to `indexer_utils.py`
6. Reduce `indexer.py` to orchestrator role
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
- Integration: `.nexus.yml` tuning overrides affect search/index behavior

## References

- Current indexer: `src/nexus/indexer.py` (1,236 lines)
- Config system: `src/nexus/config.py`
- `.nexus.yml` schema: `src/nexus/config.py`
