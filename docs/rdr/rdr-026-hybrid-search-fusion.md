---
title: "Hybrid Search — Exact-Match Score Boosting"
id: RDR-026
type: Feature
status: draft
priority: P1
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-006", "RDR-007", "RDR-014"]
related_tests: ["tests/test_scoring.py", "tests/test_search_cmd.py"]
implementation_notes: ""
---

# RDR-026: Hybrid Search — Exact-Match Score Boosting

## Problem Statement

`nx search --hybrid` fans out to both ChromaDB and ripgrep caches, appending ripgrep hits to the result set. Two bugs and a missing feature prevent this from improving result quality:

1. **Reranker overwrites all scores**: When `--hybrid` injects `rg__cache` results, the collection count exceeds 1, triggering the Voyage reranker (`search_cmd.py:194`). The reranker overwrites every `hybrid_score` with its own relevance score (RDR-006 R8). Any pre-reranker scoring adjustment is discarded.
2. **`distance=0.0` bug**: `_rg_hit_to_result()` (search_cmd.py:53) hardcodes `distance=0.0` for all ripgrep hits. After min-max normalization this yields `v_norm=1.0`, making every ripgrep hit appear maximally similar — regardless of actual relevance.
3. **No post-reranker exact-match signal**: After reranking, there is no mechanism to boost results where the query literally appears in the file. Ripgrep identifies which files contain literal matches, but this signal is lost after the reranker overwrites scores.

SeaGOAT (a sibling project at `/Users/hal.hildebrand/git/SeaGOAT/`) demonstrates that fusing ripgrep keyword hits with vector results produces strictly better results than either alone. Their implementation at `engine.py:125-155` fans out to both sources asynchronously and merges with weighted scoring. Crucially, `result.py:59-62` applies a multiplicative exact-match boost: `score = vector_distance / (1 + exact_matches)`.

## Context

- `ripgrep_cache.py` provides `search_ripgrep(query, cache_path)` — a standalone function (not a class) that runs `rg` against a line cache file and returns parsed hit dicts
- `commands/search_cmd.py` already has the `--hybrid` flag (line 73) and calls `search_ripgrep()` at query time (line 169), appending hits to the vector result set
- `scoring.py` has `apply_hybrid_scoring()` with the formula `0.7 * vector + 0.3 * frecency` — no `exact_match_boost` exists yet
- `search_engine.py` provides `search_cross_corpus()` with no hybrid parameter — hybrid logic (ripgrep fan-out) lives in `search_cmd.py`
- The `--hybrid` flag was a no-op until bead nexus-4qu was resolved. It is now functional: it fans out to ripgrep caches and appends hits to the result set. The missing piece is exact-match score boosting during `apply_hybrid_scoring()`.

## Research Findings

### F1: SeaGOAT Fusion Architecture (Verified — source search)

SeaGOAT queries ChromaDB and ripgrep simultaneously via async fan-out in `engine.py:125-151`. The `query()` method dispatches async fetchers (ripgrep) and sync fetchers (chroma), then merges results. Each source produces `Result` objects with file paths, line numbers, and distances. The merge strategy:
- Vector results contribute semantic relevance scores
- Ripgrep results contribute exact-match signals
- `result.py:get_number_of_exact_matches()` boosts scores when query text literally appears in a result line
- The boost is **multiplicative**: `score = vector_distance / (1 + exact_matches)` (result.py:59-62), effectively halving the distance when one exact match is found

### F2: Nexus Hybrid Infrastructure Is Wired (Verified — source search)

- `ripgrep_cache.py:49` has `search_ripgrep()` with 10s timeout — accepts arbitrary query strings
- `search_cmd.py:167-170` already calls `search_ripgrep()` when `--hybrid` is passed and appends hits to the vector result set
- `scoring.py` has the two-term hybrid formula (`0.7 * vector + 0.3 * frecency`)
- The missing piece is an exact-match boost term in the scoring formula: `apply_hybrid_scoring()` has no mechanism to reward results where the query literally appears in the chunk text

### F3: Ripgrep Availability (Verified — runtime check)

`rg` is a required dependency for nexus (installed by `brew install ripgrep` on macOS, `apt install ripgrep` on Linux). The `nx doctor` command already checks for its presence.

## Proposed Solution

Fix the scoring pipeline in three steps:

1. **Fix `distance=0.0` bug** in `_rg_hit_to_result()` — assign ripgrep hits a fixed `v_norm` floor (e.g., `0.8`) rather than fitting them into the same min-max window as ChromaDB distances, which have a different scale (`[0.85, 0.95]` typical for cosine)
2. **Apply exact-match boost AFTER reranking** — a new `apply_exact_match_boost()` function runs after the reranker, using file_path linkage between ripgrep hits and reranked results to promote files with literal matches
3. **Use file_path linkage as primary mechanism** — build a set of file paths from ripgrep hits; for each reranked result whose `source_path` is in that set, apply a score boost. This is more robust than `query in r.content` (which is tautological for rg hits and rarely matches multi-word queries in vector chunks)

### Boost Mechanism

The boost is applied as a **post-reranker adjustment** in `search_cmd.py`, between reranking (line 196) and output formatting (line 211):

```python
# After reranking
if hybrid:
    rg_file_paths = {r.metadata["file_path"] for r in results if r.collection == "rg__cache"}
    for r in results:
        if r.metadata.get("source_path", r.metadata.get("file_path", "")) in rg_file_paths:
            r.hybrid_score += EXACT_MATCH_BOOST  # 0.15
```

Where `EXACT_MATCH_BOOST = 0.15`. This:
- Operates on reranker output, so it is NOT overwritten
- Uses file_path linkage (robust for all query types) rather than `query in r.content` (only works for short literal queries)
- Applies to both vector results and rg results that share a file path with a ripgrep match
- Is additive: a fixed `+0.15` reward for files containing the literal query, regardless of semantic score

**Why additive over multiplicative**: SeaGOAT uses multiplicative (`score / (1 + exact_matches)`) on raw distances before normalization. Nexus applies the boost on post-reranker scores. Additive is preferred because: (a) `+0.15` is independent of the reranker's score magnitude — it provides a fixed promotion for exact matches; (b) multiplicative would couple boost magnitude to reranker score, over-promoting already-high results while under-promoting the low-ranked exact matches we most want to rescue.

### Integration Points

- `commands/search_cmd.py`: Apply post-reranker boost using rg file_path set; fix `distance=0.0` in `_rg_hit_to_result()` with fixed `v_norm` floor; filter rg results from final output (they served as signals, vector results carry the content)
- `scoring.py`: Minor — `apply_hybrid_scoring()` may need `rg__cache` collection handling for frecency blending
- `ripgrep_cache.py`: No changes needed

## Alternatives Considered

**A. Keyword search via ChromaDB's where_document filter**: ChromaDB supports `$contains` filters, but these are post-filter on the vector results, not a separate search source. This doesn't expand the candidate set.

**B. Full ripgrep-first, vector-rerank**: Run ripgrep first, then use Voyage AI to rerank. Inverts the architecture and loses semantic discovery. Rejected.

**C. BM25 via SQLite FTS5**: Build a full-text index in T2 alongside T3 vectors. Higher engineering cost than ripgrep integration and requires maintaining a parallel index. Deferred as a future enhancement.

**D. Multiplicative boost (SeaGOAT-style)**: Use `score = score / (1 + exact_matches)` instead of additive `+0.15`. Rejected for Nexus because the boost operates on post-reranker scores, not raw distances. Could be revisited if tuning shows additive is insufficient.

**E. Pre-reranker boost in `apply_hybrid_scoring()`**: Add exact-match boost before the reranker. Rejected because the Voyage reranker overwrites all `hybrid_score` values (RDR-006 R8, search_cmd.py:194-196), making pre-reranker score adjustments invisible. The boost must be post-reranker.

## Trade-offs

**Benefits**:
- Directly addresses the code search recall gap (RDR-006/007)
- Leverages existing infrastructure (ripgrep_cache.py, scoring.py, search_cmd.py hybrid wiring)
- Contained change — only modifies scoring logic, not the fan-out or retrieval path

**Risks**:
- Score calibration between vector distances and exact-match boosts needs tuning (0.15 is an initial estimate)
- Multi-repo cache contamination: `_find_rg_cache_paths()` returns ALL caches regardless of `--corpus`, so ripgrep hits from unrelated repos may be injected
- Ripgrep requires a local clone (not applicable for cloud-only collections)

**Failure modes**:
- Ripgrep not installed: degrade gracefully to vector-only (existing `nx doctor` check warns)
- Ripgrep timeout: return vector-only results with warning (existing 10s timeout in `search_ripgrep()`)
- No local repo path available: skip ripgrep, log debug message

## Implementation Plan

### Phase 1: Bug Fixes and Core Boosting

Already done:
- `search_ripgrep()` accepts arbitrary query strings
- `--hybrid` flag exists in CLI (search_cmd.py:73)
- Fan-out to ripgrep in search_cmd.py:167-170

TODO:
1. Fix `distance=0.0` bug in `_rg_hit_to_result()` — assign a fixed `v_norm` floor (e.g., `0.8`) instead of hardcoding `distance=0.0` which distorts min-max normalization
2. Add post-reranker `apply_exact_match_boost()` in `search_cmd.py` — build `rg_file_paths` set from ripgrep results, boost reranked results whose `source_path` is in that set by `+0.15`
3. Filter `rg__cache` results from final output — they serve as file_path signals for boosting but should not appear in user-facing results (vector chunks carry the actual content)
4. Address multi-repo cache contamination: `_find_rg_cache_paths()` returns ALL caches regardless of `--corpus`. Scope cache selection to match the target corpus.

### Phase 2: Tuning & Testing
5. Add unit tests for exact-match boost scoring math in `test_scoring.py`
6. Add unit test: ripgrep-only results (no vector overlap) receive boost correctly
7. Tune `exact_match_boost` weight via A/B comparison on known queries
8. Add `search.hybrid_default` config option for per-project defaults

### Phase 3: Refinements
9. Handle ripgrep-only results (files not in vector top-K) — append with penalty
10. Line-level match tracking for future context-line display (see RDR-027)

## Test Plan

- Unit: post-reranker boost — mock reranked results + rg file_path set, verify `+0.15` applied to matching source_paths
- Unit: boost survives reranking — mock reranker that overwrites scores, verify post-reranker boost is still visible
- Unit: verify `distance=0.0` fix — rg hits should not all get `v_norm=1.0`
- Unit: rg results filtered from final output — only vector results with boost appear
- Unit: ripgrep timeout → graceful fallback to vector-only
- Unit: ripgrep not installed → graceful fallback
- Unit: file_path linkage — vector result boosted when its source_path matches an rg hit file_path
- Integration: search a fixture repo with known exact matches, verify they rank higher with --hybrid
- Regression: existing non-hybrid searches produce identical results

## Finalization Gate

### Contradiction Check
No internal contradictions. The reranker interaction (RDR-006 R8) is now explicitly addressed: the boost is applied post-reranker, avoiding the overwrite problem. The `distance=0.0` bug and the post-reranker boost are complementary fixes targeting different pipeline stages. Alternative E documents why pre-reranker boosting was rejected.

### Assumption Verification
- **A1**: The Voyage reranker overwrites `hybrid_score` for all results when collection count > 1 — verified in `search_cmd.py:194-196` and documented in RDR-006 R8. This is why the boost MUST be post-reranker.
- **A2**: Ripgrep hits carry `file_path` in metadata (`search_cmd.py:56`) and vector results carry `source_path` — verified. File_path linkage between the two is the primary boost mechanism.
- **A3**: The `0.15` boost value is meaningful relative to reranker output scores. Tuning in Phase 2 may adjust this. The additive approach ensures exact matches get a fixed promotion regardless of reranker score.
- **A4**: `rg__cache` results should not appear in final output — they serve as signals for file_path linkage. Filtering them out (Implementation Plan step 3) prevents raw ripgrep lines from appearing alongside chunk content.

### Scope Verification
The change is contained to `search_cmd.py` (post-reranker boost + distance fix + rg filtering) and tests. `scoring.py` may need minor `rg__cache` handling. No changes to `search_engine.py`, `ripgrep_cache.py`, CLI flags, or the indexing pipeline.

### Cross-Cutting Concerns
- **Reranker interaction**: Explicitly handled — boost is post-reranker, not pre-reranker. The reranker still operates on the full result set (vector + rg), which means it can use rg hits to inform its relevance judgments before the boost is applied.
- **Performance**: Post-reranker boost is a set lookup (`O(1)` per result) on file paths already in memory. Negligible overhead.
- **Score bounds**: `hybrid_score` may exceed `1.0` after boost (`reranker_score + 0.15`). No downstream code enforces `[0, 1]` bounds. If needed, apply `min(1.0, score)` cap.
- **Backward compatibility**: Non-hybrid searches are unaffected. The boost only fires when `hybrid=True` and rg results are present.
- **Multi-repo contamination**: Identified as known risk (Implementation Plan step 4). Not blocking for Phase 1 but must be addressed.
- **Frecency on rg hits**: `apply_hybrid_scoring()` gates frecency on `r.collection.startswith("code__")`. Ripgrep hits (`rg__cache`) bypass frecency blending — this is acceptable since rg hits are signals, not final results.

### Proportionality
The implementation touches 1-2 production files and adds ~20 lines of post-reranker boost logic plus ~60 lines of tests. This is proportional to the problem: a well-scoped scoring enhancement that leverages existing infrastructure. No new dependencies, no architectural changes, no new CLI flags.

## References

- SeaGOAT engine.py (async fan-out): `/Users/hal.hildebrand/git/SeaGOAT/seagoat/engine.py:125-155`
- SeaGOAT result.py (multiplicative exact-match boost): `/Users/hal.hildebrand/git/SeaGOAT/seagoat/result.py:59-62`
- nexus ripgrep_cache.py: `src/nexus/ripgrep_cache.py`
- nexus scoring.py: `src/nexus/scoring.py`
- nexus search_cmd.py: `src/nexus/commands/search_cmd.py`
- RDR-006: File-Size Scoring Penalty
- RDR-007: Claude Adoption Search Guidance
