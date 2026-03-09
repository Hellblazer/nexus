---
title: "Hybrid Search — Vector + Ripgrep Query Fusion"
id: RDR-026
type: Feature
status: draft
priority: P1
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-006", "RDR-007", "RDR-014"]
related_tests: []
implementation_notes: ""
---

# RDR-026: Hybrid Search — Vector + Ripgrep Query Fusion

## Problem Statement

`nx search` queries ChromaDB vector collections only. The `ripgrep_cache.py` module exists but is wired only into the indexing pipeline — it is never used at query time. This means:

1. **No exact-match boosting**: Searching for `def compute_frecency` returns semantically similar but non-literal matches ranked equally with exact matches
2. **Recall gaps**: Canonical implementation files absent from ChromaDB's top-K candidate set (documented in RDR-006/007) could be found trivially by ripgrep
3. **No keyword fallback**: When semantic search fails on domain-specific jargon, ripgrep would catch literal occurrences

SeaGOAT (a sibling project at `/Users/hal.hildebrand/git/SeaGOAT/`) demonstrates that fusing ripgrep keyword hits with vector results produces strictly better results than either alone. Their implementation at `seagoat/engine.py:125-155` fans out to both sources asynchronously and merges with weighted scoring.

## Context

- `ripgrep_cache.py` already provides `RipgrepCache.search(query, repo_path)` → list of file matches with line numbers
- `search_engine.py` handles cross-corpus vector queries serially
- `scoring.py` implements `apply_hybrid_scoring()` with weights `0.7 * vector + 0.3 * frecency`
- SeaGOAT's scoring: `0.7 * vector + 0.3 * frecency`, plus exact-match boost (halves distance when regex matches found)
- The `--hybrid` flag existed historically but was a no-op (bead nexus-4qu, since removed in RDR-009)

## Research Findings

### F1: SeaGOAT Fusion Architecture (Verified — source search)

SeaGOAT queries ChromaDB and ripgrep simultaneously via async (`engine.py:65-72`), then merges results. Each source produces `Result` objects with file paths, line numbers, and distances. The merge strategy:
- Vector results contribute semantic relevance scores
- Ripgrep results contribute exact-match signals
- `result.py:get_number_of_exact_matches()` boosts scores when query text literally appears in a result line

### F2: Nexus Infrastructure Already Exists (Verified — source search)

- `ripgrep_cache.py:91` has `search()` with 10s timeout
- `scoring.py` already has the hybrid scoring formula
- The missing piece is the query-time fan-out: call ripgrep alongside ChromaDB, merge result sets, and boost exact matches

### F3: Ripgrep Availability (Verified — runtime check)

`rg` is a required dependency for nexus (installed by `brew install ripgrep` on macOS, `apt install ripgrep` on Linux). The `nx doctor` command already checks for its presence.

## Proposed Solution

Add a `--hybrid` flag to `nx search` that:

1. Runs the vector query against ChromaDB (existing path)
2. Simultaneously runs `rg --json "{query}" {repo_path}` via `ripgrep_cache.search()`
3. Merges results: vector results are primary, ripgrep results boost scores for files/lines with exact matches
4. Optionally: ripgrep-only results (files not in vector top-K) are appended with a configurable penalty

### Scoring Formula

```
final_score = vector_weight * vector_norm + frecency_weight * frecency_norm + exact_match_boost
```

Where:
- `vector_weight = 0.7` (existing)
- `frecency_weight = 0.3` (existing)
- `exact_match_boost = 0.15` when ripgrep finds the query literally in the chunk (new)

### Integration Points

- `search_engine.py`: Add `hybrid: bool` parameter to `search()`, fan out to ripgrep
- `scoring.py`: Add `exact_match_boost` to `apply_hybrid_scoring()`
- `commands/search.py`: Add `--hybrid` CLI flag
- `ripgrep_cache.py`: Ensure `search()` works for arbitrary queries (not just cached patterns)

## Alternatives Considered

**A. Keyword search via ChromaDB's where_document filter**: ChromaDB supports `$contains` filters, but these are post-filter on the vector results, not a separate search source. This doesn't expand the candidate set.

**B. Full ripgrep-first, vector-rerank**: Run ripgrep first, then use Voyage AI to rerank. Inverts the architecture and loses semantic discovery. Rejected.

**C. BM25 via SQLite FTS5**: Build a full-text index in T2 alongside T3 vectors. Higher engineering cost than ripgrep integration and requires maintaining a parallel index. Deferred as a future enhancement.

## Trade-offs

**Benefits**:
- Directly addresses the code search recall gap (RDR-006/007)
- Leverages existing infrastructure (ripgrep_cache.py, scoring.py)
- Opt-in via `--hybrid` flag — no regression risk for existing workflows

**Risks**:
- Ripgrep adds ~100ms latency per query (mitigated: run in parallel with vector query)
- Ripgrep requires a local clone (not applicable for cloud-only collections)
- Score calibration between vector distances and exact-match boosts needs tuning

**Failure modes**:
- Ripgrep not installed → degrade gracefully to vector-only (existing `nx doctor` check warns)
- Ripgrep timeout → return vector-only results with warning
- No local repo path available → skip ripgrep, log debug message

## Implementation Plan

### Phase 1: Core Fusion
1. Extend `RipgrepCache.search()` to accept arbitrary query strings
2. Add `hybrid: bool` parameter to `SearchEngine.search()`
3. Implement async fan-out: vector query + ripgrep query in parallel
4. Merge results: boost vector results that have ripgrep matches
5. Add `--hybrid` flag to `nx search` CLI

### Phase 2: Tuning & Testing
6. Add integration tests with a fixture repo
7. Tune exact_match_boost weight via A/B comparison on known queries
8. Add `search.hybrid_default` config option for per-project defaults

### Phase 3: Refinements
9. Handle ripgrep-only results (files not in vector top-K) — append with penalty
10. Line-level match tracking for future context-line display (see RDR-027)

## Test Plan

- Unit: mock ripgrep output, verify score boosting math
- Unit: ripgrep timeout → graceful fallback to vector-only
- Unit: ripgrep not installed → graceful fallback
- Integration: search a fixture repo with known exact matches, verify they rank higher with --hybrid
- Regression: existing non-hybrid searches produce identical results

## References

- SeaGOAT engine.py: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/engine.py`
- SeaGOAT result.py exact match: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/result.py:14-28`
- nexus ripgrep_cache.py: `src/nexus/ripgrep_cache.py`
- nexus scoring.py: `src/nexus/scoring.py`
- RDR-006: File-Size Scoring Penalty
- RDR-007: Claude Adoption Search Guidance
