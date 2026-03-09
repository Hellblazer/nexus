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

`nx search --hybrid` fans out to both ChromaDB and ripgrep caches, appending ripgrep hits to the result set. However, `apply_hybrid_scoring()` does not apply any exact-match score boost. Ripgrep hits are scored purely by position-based frecency and vector distance normalization — there is no signal for whether the query text literally appears in a matched line. This means:

1. **No exact-match boosting**: Searching for `def compute_frecency` returns semantically similar but non-literal matches ranked equally with exact matches. Ripgrep hits that contain the literal query are not promoted.
2. **Recall gaps**: Canonical implementation files absent from ChromaDB's top-K candidate set (documented in RDR-006/007) are found by ripgrep but not scored higher for containing the exact query.
3. **`distance=0.0` bug**: `_rg_hit_to_result()` (search_cmd.py:53) hardcodes `distance=0.0` for all ripgrep hits. After min-max normalization this yields `v_norm=1.0`, making every ripgrep hit appear maximally similar — regardless of actual relevance.

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

Add exact-match score boosting to the existing hybrid search path:

1. Compute `exact_match_boost` in `apply_hybrid_scoring()` when the query text literally appears in the chunk content
2. Link ripgrep hits to vector results by `file_path` during scoring so that vector results matching ripgrep files receive a boost
3. Fix the `distance=0.0` bug in `_rg_hit_to_result()` (search_cmd.py:53) where all rg hits get `distance=0.0`, causing them to always receive `v_norm=1.0` after min-max normalization

### Scoring Formula

The existing two-term formula:
```
score = 0.7 * vector_norm + 0.3 * frecency_norm
```

becomes a three-term additive formula:
```
final_score = vector_weight * vector_norm + frecency_weight * frecency_norm + exact_match_boost
```

Where:
- `vector_weight = 0.7` (existing)
- `frecency_weight = 0.3` (existing)
- `exact_match_boost = 0.15` when ripgrep finds the query literally in the chunk (new)

**Additive vs. multiplicative justification**: SeaGOAT uses a multiplicative boost (`score = vector_distance / (1 + exact_matches)`) which halves the raw distance. This works well in SeaGOAT because scoring happens on raw distances before normalization. In Nexus, scoring happens on *normalized* values in [0, 1]. Additive is preferred here because: (a) an additive constant is easier to reason about and tune — `+0.15` means "exact match is worth ~21% of the max possible score (0.7)"; (b) multiplicative boost on normalized scores would couple the boost magnitude to the vector score, giving semantically close results a disproportionately large absolute boost while semantically distant exact matches (the case we most want to fix) get minimal benefit. The additive term gives a fixed reward regardless of vector similarity, directly addressing the recall gap.

### Integration Points

- `scoring.py`: Add `exact_match_boost` computation to `apply_hybrid_scoring()`, accepting the query string as a new parameter
- `commands/search_cmd.py`: Pass the query string to `apply_hybrid_scoring()` so it can compute exact-match presence; fix `distance=0.0` in `_rg_hit_to_result()`
- `ripgrep_cache.py`: No changes needed — `search_ripgrep()` already accepts arbitrary queries

## Alternatives Considered

**A. Keyword search via ChromaDB's where_document filter**: ChromaDB supports `$contains` filters, but these are post-filter on the vector results, not a separate search source. This doesn't expand the candidate set.

**B. Full ripgrep-first, vector-rerank**: Run ripgrep first, then use Voyage AI to rerank. Inverts the architecture and loses semantic discovery. Rejected.

**C. BM25 via SQLite FTS5**: Build a full-text index in T2 alongside T3 vectors. Higher engineering cost than ripgrep integration and requires maintaining a parallel index. Deferred as a future enhancement.

**D. Multiplicative boost (SeaGOAT-style)**: Use `score = score / (1 + exact_matches)` instead of additive `+0.15`. Rejected for Nexus because scoring operates on normalized [0,1] values, not raw distances (see justification above). Could be revisited if tuning shows additive is insufficient.

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
1. Fix `distance=0.0` scoring bug in `_rg_hit_to_result()` — assign a synthetic distance (e.g., based on inverse frecency) instead of hardcoding 0.0, which distorts min-max normalization
2. Add `exact_match_boost` to `apply_hybrid_scoring()` — accept `query: str` parameter, check whether `query` appears in `r.content`, add `+0.15` when found
3. Link ripgrep hits to vector results by `file_path` for boosting — when a vector result's file_path matches a ripgrep hit, boost the vector result even though it was retrieved from ChromaDB
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

- Unit: mock ripgrep output, verify `exact_match_boost` adds +0.15 when query text appears in chunk content
- Unit: verify `distance=0.0` fix — rg hits should not all get `v_norm=1.0`
- Unit: ripgrep timeout → graceful fallback to vector-only
- Unit: ripgrep not installed → graceful fallback
- Unit: file_path linkage — vector result boosted when matching rg hit exists
- Integration: search a fixture repo with known exact matches, verify they rank higher with --hybrid
- Regression: existing non-hybrid searches produce identical results

## Finalization Gate

### Contradiction Check
No internal contradictions found. The RDR correctly identifies that hybrid infrastructure (flag, fan-out, result appending) is already wired, and scopes the work to the scoring gap. The additive boost approach is explicitly justified against the multiplicative alternative. The `distance=0.0` bug is identified as both a problem (Problem Statement point 3) and a fix target (Implementation Plan step 1).

### Assumption Verification
- **A1**: `search_ripgrep()` returns hits with `line_content` containing the raw source line — verified in `ripgrep_cache.py:113-122`. A simple `query in r.content` substring check is sufficient for exact-match detection.
- **A2**: `apply_hybrid_scoring()` receives all results (vector + ripgrep) in a single list — verified in `search_cmd.py:191`. The function can match by `file_path` metadata to link rg hits to vector results.
- **A3**: The `0.15` boost value is meaningful relative to the `[0, 1]` score range — `0.15` is ~21% of the vector weight (0.7). This is large enough to promote exact matches but not so large that irrelevant exact matches dominate semantic results. Tuning in Phase 2 may adjust this.

### Scope Verification
The change is contained to three files: `scoring.py` (add boost logic), `search_cmd.py` (pass query to scoring, fix distance bug), and tests. No changes to `search_engine.py`, `ripgrep_cache.py`, CLI flags, or the indexing pipeline. This is proportional to a P1 scoring enhancement.

### Cross-Cutting Concerns
- **Performance**: No additional subprocess calls or API requests. The exact-match check is a Python `in` operator on strings already in memory. Negligible overhead.
- **Backward compatibility**: Non-hybrid searches are unaffected — `exact_match_boost` is only computed when `hybrid=True`. The `apply_hybrid_scoring()` signature gains a `query` parameter; callers must be updated (only `search_cmd.py:191`).
- **Multi-repo contamination**: Identified as a known risk (Implementation Plan step 4). Not blocking for Phase 1 but must be addressed before the feature is considered complete.

### Proportionality
The implementation touches 2 production files and adds ~30 lines of scoring logic plus ~50 lines of tests. This is proportional to the problem: a well-scoped scoring enhancement that leverages existing infrastructure. No new dependencies, no architectural changes, no new CLI flags.

## References

- SeaGOAT engine.py (async fan-out): `/Users/hal.hildebrand/git/SeaGOAT/seagoat/engine.py:125-155`
- SeaGOAT result.py (multiplicative exact-match boost): `/Users/hal.hildebrand/git/SeaGOAT/seagoat/result.py:59-62`
- nexus ripgrep_cache.py: `src/nexus/ripgrep_cache.py`
- nexus scoring.py: `src/nexus/scoring.py`
- nexus search_cmd.py: `src/nexus/commands/search_cmd.py`
- RDR-006: File-Size Scoring Penalty
- RDR-007: Claude Adoption Search Guidance
