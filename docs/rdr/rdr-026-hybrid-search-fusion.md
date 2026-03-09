---
title: "Hybrid Search — Exact-Match Score Boosting"
id: RDR-026
type: Feature
status: closed
accepted_date: 2026-03-09
close_date: 2026-03-09
close_reason: implemented
priority: P1
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-006", "RDR-007"]
related_tests: ["tests/test_scoring.py", "tests/test_search_cmd.py", "tests/test_hybrid_boost.py"]
implementation_notes: "PR #84 — Phase 1 complete"
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

1. **Fix `distance=0.0` bug** — in `scoring.py:apply_hybrid_scoring()`, exclude `rg__cache` results from the min-max normalization window and assign a fixed `hybrid_score = RG_FLOOR_SCORE` (e.g., `0.5`) directly. ChromaDB cosine distances (`[0.85, 0.95]` typical) and ripgrep's `distance=0.0` are on incompatible scales; normalizing them together distorts the window. The concrete fix:
   ```python
   distances = [r.distance for r in results if r.collection != "rg__cache"]
   for r in results:
       if r.collection == "rg__cache":
           r.hybrid_score = RG_FLOOR_SCORE  # 0.5
           continue
       v_norm = 1.0 - min_max_normalize(r.distance, distances)
       ...
   ```
2. **Apply exact-match boost AFTER reranking** — a new `apply_exact_match_boost()` function runs after the reranker, using file_path linkage between ripgrep hits and reranked results to promote files with literal matches
3. **Use file_path linkage as primary mechanism** — build a set of file paths from ripgrep hits; for each reranked result whose `source_path` is in that set, apply a score boost. This is more robust than `query in r.content` (which is tautological for rg hits and rarely matches multi-word queries in vector chunks)

### Boost Mechanism

The boost requires **pre-reranker capture** of ripgrep file paths. The reranker's `top_k` cutoff may drop low-scoring rg__cache results from the result set, so the file path set must be built before reranking, then applied after:

```python
# BEFORE reranking — capture rg file paths while all results are present
rg_file_paths = (
    {r.metadata["file_path"] for r in results if r.collection == "rg__cache"}
    if hybrid else set()
)

# Reranking (existing block, unchanged)
if not no_rerank and len(set(r.collection for r in results)) > 1:
    results = rerank_results(results, query=query, model=reranker_model, top_k=n)
else:
    ...

# AFTER reranking — apply boost using pre-captured set
if rg_file_paths:
    for r in results:
        if r.metadata.get("source_path", r.metadata.get("file_path", "")) in rg_file_paths:
            r.hybrid_score = min(1.0, r.hybrid_score + EXACT_MATCH_BOOST)

# Filter rg signals from output, with fallback
vector_results = [r for r in results if r.collection != "rg__cache"]
results = vector_results if vector_results else results
```

Where `EXACT_MATCH_BOOST = 0.15`. This:
- Captures rg file paths **before** the reranker can drop them via `top_k` cutoff
- Applies the boost **after** reranking, so it is NOT overwritten
- Uses file_path linkage (robust for all query types) rather than `query in r.content` (only works for short literal queries)
- Caps at `1.0` to prevent unbounded scores
- Falls back to rg__cache results when no vector results survive (rather than returning empty)
- Fires on both reranked and non-reranked paths (the boost block runs unconditionally after the rerank/no-rerank branch)

**Why additive over multiplicative**: SeaGOAT uses multiplicative (`score / (1 + exact_matches)`) on raw distances before normalization. Nexus applies the boost on post-reranker scores. Additive is preferred because: (a) `+0.15` is independent of the reranker's score magnitude — it provides a fixed promotion for exact matches; (b) multiplicative would couple boost magnitude to reranker score, over-promoting already-high results while under-promoting the low-ranked exact matches we most want to rescue.

### Integration Points

- `commands/search_cmd.py`: Capture `rg_file_paths` pre-reranker; apply post-reranker boost; filter rg results from output with fallback guard; ensure boost fires on both reranked and `--no-rerank` paths
- `scoring.py`: **Required change** — exclude `rg__cache` from distance normalization window in `apply_hybrid_scoring()`, assign fixed `RG_FLOOR_SCORE` directly to rg hits, skip frecency blending for `rg__cache`
- `search_cmd.py`: Scope `_find_rg_cache_paths()` (line 41) to accept an optional corpus filter. Cache files are named `{repo_basename}-{hash8}.cache` (matching the collection suffix pattern — `code__nexus-a1b2c3d4` → `nexus-a1b2c3d4.cache`). The corpus filter strips the `code__`/`docs__`/`rdr__` prefix and globs `{slug}.cache` instead of `*.cache`

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
- Multi-repo cache contamination (P1 fix): `_find_rg_cache_paths()` returns ALL caches regardless of `--corpus`, so ripgrep hits from unrelated repos are injected. This is a correctness failure in multi-repo setups, not just a tuning risk. Fixed in Phase 1 step 5.
- Ripgrep requires a local clone (not applicable for cloud-only collections)

**Failure modes**:
- Ripgrep not installed: degrade gracefully to vector-only (existing `nx doctor` check warns)
- Ripgrep timeout: return vector-only results with warning (existing 10s timeout in `search_ripgrep()`)
- No local repo path available: skip ripgrep, log debug message
- No vector results survive filtering: fallback to rg__cache results rather than empty (guarded filter in Boost Mechanism)

## Implementation Plan

### Phase 1: Bug Fixes and Core Boosting

Already done:
- `search_ripgrep()` accepts arbitrary query strings
- `--hybrid` flag exists in CLI (search_cmd.py:73)
- Fan-out to ripgrep in search_cmd.py:167-170

TODO:
1. Fix `distance=0.0` bug in `scoring.py:apply_hybrid_scoring()` — exclude `rg__cache` results from the min-max normalization distance window; assign `hybrid_score = RG_FLOOR_SCORE` (0.5) directly to rg hits; skip frecency blending for `rg__cache` collection
2. Capture `rg_file_paths` **pre-reranker** in `search_cmd.py` — the reranker's `top_k` may drop rg hits, so file paths must be captured before reranking. Apply post-reranker boost of `min(1.0, hybrid_score + 0.15)` to results whose `source_path` is in the set
3. Filter `rg__cache` results from final output with fallback guard — `vector_results = [r for r in results if r.collection != "rg__cache"]; results = vector_results if vector_results else results`. This preserves rg hits when no vector results survive, rather than returning empty
4. Ensure boost fires on both `--hybrid` (with reranker) and `--hybrid --no-rerank` paths — the boost block must run unconditionally after the rerank/no-rerank branch, not inside the reranker-only branch
5. Fix multi-repo cache contamination (P1): `_find_rg_cache_paths()` in `search_cmd.py:41` returns ALL caches (`*.cache`) regardless of `--corpus`, injecting hits from unrelated repos. Add an optional `corpus: str` parameter — when provided, strip the `code__`/`docs__`/`rdr__` prefix and glob `{slug}.cache` instead of `*.cache`. Cache files are named `{repo_basename}-{hash8}.cache` by `indexer.py:1185`, matching the collection suffix (e.g., `code__nexus-a1b2c3d4` → `nexus-a1b2c3d4.cache`)

### Phase 2: Tuning & Testing
6. Add unit tests for exact-match boost scoring math in `test_scoring.py`
7. Add unit test: ripgrep-only results (no vector overlap) receive boost correctly
8. Tune `exact_match_boost` weight via A/B comparison on known queries
9. Add `search.hybrid_default` config option for per-project defaults

### Phase 3: Refinements
10. Handle ripgrep-only results (files not in vector top-K) — append with penalty
11. Line-level match tracking for future context-line display (see RDR-027)

## Test Plan

- Unit: post-reranker boost — mock reranked results + pre-captured rg file_path set, verify `min(1.0, score + 0.15)` applied to matching source_paths
- Unit: boost fires when rg hits dropped by reranker — mock reranker that returns top_k without rg hits, verify pre-captured `rg_file_paths` still enables boost on surviving vector results
- Unit: verify `distance=0.0` fix — rg hits excluded from normalization window, assigned `RG_FLOOR_SCORE` directly
- Unit: rg results filtered from final output — only vector results with boost appear
- Unit: zero vector results fallback — when all results are `rg__cache`, filter guard preserves them rather than returning empty
- Unit: `--hybrid --no-rerank` applies boost correctly — boost fires on the non-reranked path
- Unit: ripgrep timeout → graceful fallback to vector-only
- Unit: ripgrep not installed → graceful fallback
- Unit: file_path linkage — vector result boosted when its source_path matches an rg hit file_path
- Unit: multi-repo cache scoping — searching `--corpus code__nexus --hybrid` only searches nexus-related caches
- Integration: search a fixture repo with known exact matches, verify they rank higher with --hybrid
- Regression: existing non-hybrid searches produce identical results

## Finalization Gate

### Contradiction Check
No internal contradictions. The reranker interaction (RDR-006 R8) is explicitly addressed: `rg_file_paths` is captured pre-reranker (before `top_k` can drop rg hits), and the boost is applied post-reranker (avoiding the overwrite problem). The `distance=0.0` fix (exclude rg__cache from normalization window in `scoring.py`) and the post-reranker boost (file_path linkage in `search_cmd.py`) target different pipeline stages and compose correctly. Alternative E documents why pre-reranker boosting was rejected. The rg filter includes a fallback guard to prevent zero results when no vector results survive.

### Assumption Verification
- **A1**: The Voyage reranker overwrites `hybrid_score` for all results when collection count > 1 — verified in `search_cmd.py:194-196` and documented in RDR-006 R8. This is why the boost MUST be post-reranker.
- **A2**: Ripgrep hits carry `file_path` in metadata (`search_cmd.py:56`) and vector results carry `source_path` — verified. File_path linkage between the two is the primary boost mechanism.
- **A3**: The `0.15` boost value is meaningful relative to reranker output scores. Tuning in Phase 2 may adjust this. The additive approach ensures exact matches get a fixed promotion regardless of reranker score.
- **A4**: `rg__cache` results should not appear in final output — they serve as signals for file_path linkage. Filtering them out (Implementation Plan step 3) prevents raw ripgrep lines from appearing alongside chunk content.

### Scope Verification
The change touches `search_cmd.py` (pre-reranker capture + post-reranker boost + rg filtering with fallback + corpus filter on `_find_rg_cache_paths()`) and `scoring.py` (exclude `rg__cache` from normalization window, assign `RG_FLOOR_SCORE`). No changes to `search_engine.py`, `ripgrep_cache.py`, CLI flags, or the indexing pipeline.

### Cross-Cutting Concerns
- **Reranker interaction**: `rg_file_paths` captured pre-reranker (before `top_k` drops rg hits). Boost applied post-reranker. The reranker still operates on the full result set (vector + rg), using rg hits to inform its relevance judgments.
- **`--hybrid --no-rerank` path**: The boost block runs unconditionally after the rerank/no-rerank branch. On the no-rerank path, `hybrid_score` values from `apply_hybrid_scoring()` are the ordering basis — the `distance=0.0` fix in `scoring.py` (exclude rg__cache from normalization) ensures these are not distorted.
- **Performance**: Post-reranker boost is a set lookup (`O(1)` per result) on file paths already in memory. Negligible overhead.
- **Score bounds**: `hybrid_score` capped at `1.0` via `min(1.0, score + EXACT_MATCH_BOOST)`.
- **Backward compatibility**: Non-hybrid searches are unaffected. The boost only fires when `hybrid=True` and rg results are present.
- **Multi-repo contamination**: P1 fix in Phase 1 step 5. `_find_rg_cache_paths()` in `search_cmd.py` accepts a corpus filter; cache naming convention `{basename}-{hash8}.cache` matches collection suffix pattern.
- **`--hybrid --no-rerank` sub-n results**: The round-robin interleave may include rg__cache entries in the top n; after filtering, fewer than n vector results may remain. This is acceptable — the user requested hybrid results, which include rg signals.
- **Frecency on rg hits**: `apply_hybrid_scoring()` gates frecency on `r.collection.startswith("code__")`. Ripgrep hits (`rg__cache`) bypass frecency blending and receive `RG_FLOOR_SCORE` directly — acceptable since rg hits are signals, not final results.
- **Zero-result guard**: rg filter uses `vector_results if vector_results else results` to prevent returning empty when all results are rg__cache.

### Proportionality
The implementation touches 2 production files (`search_cmd.py`, `scoring.py`) and adds ~30 lines of boost/scoring logic plus ~80 lines of tests. This is proportional to the problem: a well-scoped scoring enhancement that leverages existing infrastructure. No new dependencies, no architectural changes, no new CLI flags.

## References

- SeaGOAT engine.py (async fan-out): `/Users/hal.hildebrand/git/SeaGOAT/seagoat/engine.py:125-155`
- SeaGOAT result.py (multiplicative exact-match boost): `/Users/hal.hildebrand/git/SeaGOAT/seagoat/result.py:59-62`
- nexus ripgrep_cache.py: `src/nexus/ripgrep_cache.py`
- nexus scoring.py: `src/nexus/scoring.py`
- nexus search_cmd.py: `src/nexus/commands/search_cmd.py`
- RDR-006: File-Size Scoring Penalty
- RDR-007: Claude Adoption Search Guidance
