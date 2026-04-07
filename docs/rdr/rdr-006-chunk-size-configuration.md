---
title: "File-Size Scoring Penalty for Code Search"
type: feature
status: closed
close_reason: implemented
closed_date: 2026-02-28
priority: P2
author: Hal Hildebrand
date: 2026-02-28
accepted_date: 2026-02-28
reviewed_by: self
related_issues: []
---

# RDR-006: File-Size Scoring Penalty for Code Search

## Problem

`nx index repo` uses a fixed chunk size when splitting source files into embedding units.
Large files (e.g. `main.py`, `doctor.py`) produce broad chunks that score highly on almost
any semantic query, drowning out smaller, more focused files that are the canonical answer.

Observed symptoms when searching `code__arcaneum-2ad2825c` as a single-corpus query
(`nx search "..." --corpus code__arcaneum-2ad2825c`):

- Query `"embedding model GPU acceleration FastEmbed"` returns `doctor.py`, `errors.py`,
  `main.py` — but not `embeddings/client.py`
- Query `"chunk overlap tokenizer source code indexing"` returns `analyze_profile.py`,
  `qdrant-monitor-segments.py` — but not `indexing/markdown/chunker.py`
- Query `"MeiliSearch full text search index documents"` returns `qdrant-restore.sh`,
  `command_wrapper.py` — but not `fulltext/client.py`

**Scope note:** These symptoms are observed and this fix applies to **single-corpus code
searches** (`--corpus code__<repo>`). In the default multi-corpus search (knowledge + code +
docs), the Voyage reranker fires and overwrites all `hybrid_score` values with its own
relevance scores — Track A's penalty has no effect on final ordering in that path (see R8).
For multi-corpus searches, use `--max-file-chunks N` (a hard pre-retrieval filter) instead.

Root cause: large files chunked at the default size produce chunks that span many unrelated
topics, inflating their semantic surface area. Smaller chunks confine each embedding to a
tighter conceptual scope, improving precision.

## Proposed Solution

**Search-time file-size scoring penalty** — no re-indexing required, immediate effect.

Every chunk already stores `chunk_count` (total chunks the source file was split into) as
metadata. Large files produce many chunks; a file with `chunk_count=80` is dominating results
because all 80 of its chunks have broad semantic surface area.

Apply a `file_size_factor` multiplier to `hybrid_score` for **all** `code__` results in
`scoring.py:apply_hybrid_scoring()`, **unconditionally** (regardless of `--hybrid` flag):

```python
_FILE_SIZE_THRESHOLD = 30  # chunks; files ≤ this are not penalised

def _file_size_factor(chunk_count: int) -> float:
    """Return a [0,1] penalty: 1.0 for small files, diminishing for large."""
    return min(1.0, _FILE_SIZE_THRESHOLD / max(1, chunk_count))
```

With this factor:
- `chunk_count ≤ 30` → factor = 1.0 (no penalty)
- `chunk_count = 37` → factor = 0.811 (`main.py`)
- `chunk_count = 50` → factor = 0.60
- `chunk_count = 107` → factor = 0.280 (`embeddings/client.py`)

The penalty is applied **after** computing the vector/frecency score and **before** Voyage
reranking, so the reranker still has final say over ordering but works from a candidate
window that no longer skews toward large files.

The penalty applies to all `code__` results regardless of whether `--hybrid` was passed.
The `--hybrid` flag only gates frecency blending; file-size correction is orthogonal and
must fire on every search path (see R9).

The corrected integration inside `apply_hybrid_scoring()` — complete replacement of the
`for` loop body, including the return statement:

```python
for r in results:
    v_norm = 1.0 - min_max_normalize(r.distance, distances)
    if hybrid and r.collection.startswith("code__"):
        f_score = r.metadata.get("frecency_score", 0.0)
        f_norm = min_max_normalize(f_score, frecencies) if frecencies else 0.0
        r.hybrid_score = hybrid_score(v_norm, f_norm)
    else:
        r.hybrid_score = v_norm
    # File-size penalty: unconditional for all code__ results, outside --hybrid branch.
    # Default of 1 is safe for content indexed before chunk_count was stored.
    if r.collection.startswith("code__"):
        chunk_count = int(r.metadata.get("chunk_count", 1))
        r.hybrid_score *= _file_size_factor(chunk_count)

return sorted(results, key=lambda r: r.hybrid_score, reverse=True)
```

An optional `--max-file-chunks N` flag on `nx search` can add a hard `where` filter:
`{"chunk_count": {"$lte": N}}`, eliminating very large files entirely from candidate set.
Note: `chunk_count` is stored as `int`; the filter operand must also be `int` (not float)
to ensure correct ChromaDB metadata comparison (see R9).

### Threshold Calibration

`_FILE_SIZE_THRESHOLD = 30` is derived from baseline measurements against the
`code__arcaneum-2ad2825c` collection (see R10). The threshold sits between the highest
chunk_count canonical target (`indexing/markdown/chunker.py`, cc=28) and the primary
dominating file (`main.py`, cc=37):

| File | Role | chunk_count | factor @ threshold=30 |
|------|------|-------------|----------------------|
| `errors.py` | dominating (Q1) | 5 | 1.000 |
| `analyze_profile.py` | dominating (Q2) | 5 | 1.000 |
| `doctor.py` | dominating (Q1, Q2) | 10 | 1.000 |
| `qdrant_indexer.py` | dominating (Q3) | 18 | 1.000 (not penalised) |
| `indexing/markdown/chunker.py` | **canonical Q2** | **28** | **1.000** |
| `main.py` | dominating (Q1, Q2) | 37 | 0.811 |
| `embeddings/client.py` | **canonical Q1** | **107** | **0.280** |

`main.py` (cc=37) is pushed below `doctor.py` (cc=10) for query 2 — the primary success
target. `indexing/markdown/chunker.py` (cc=28) retains factor=1.0.

`qdrant_indexer.py` (cc=18) is **not penalised** at threshold=30. To suppress it, the
threshold would need to be < 18, which would simultaneously penalise canonical files with
cc≥18. Q3 precision improvement requires `--max-file-chunks` (hard filter) rather than
the soft scoring penalty.

**Per-repo calibration note:** threshold=30 is calibrated for the arcaneum collection. Other
repos with different file-size distributions need separate calibration. To calibrate for a
new collection: (1) run baseline queries to measure chunk_count of both canonical targets and
dominating false-positives; (2) set threshold between the highest canonical cc and the lowest
dominating cc. If the ranges overlap, use `--max-file-chunks` instead of the soft penalty.

Note: `embeddings/client.py` (cc=107) would receive factor=0.280 if it appeared in results.
Baseline measurements show it is not retrieved in the top 20 for query 1 (a recall problem
distinct from ranking — see R10). The penalty does not worsen recall.

### Implementation Notes

**Touch-points (scoring.py and search_cmd.py only — no indexer changes):**

1. `src/nexus/scoring.py:apply_hybrid_scoring()` — add `_FILE_SIZE_THRESHOLD` constant and
   `_file_size_factor()` helper; apply penalty unconditionally after vector/frecency scoring
   for all `code__` results
2. `src/nexus/commands/search_cmd.py` — add `--max-file-chunks INT` option; pass as
   `where={"chunk_count": {"$lte": N}}` (N must be `int`, not float)

No indexer, chunker, or collection changes required for this track.

**`--max-file-chunks` + `--where` merge:** If the user also passes `--where KEY=VALUE`,
the implementation must merge both filters using ChromaDB's `$and` operator rather than
overwriting the existing filter:
```python
if max_file_chunks is not None:
    size_filter = {"chunk_count": {"$lte": max_file_chunks}}
    if where_filter:
        where_filter = {"$and": [where_filter, size_filter]}
    else:
        where_filter = size_filter
```
A naive `where_filter = size_filter` assignment silently discards the user's `--where`
filter, which is a correctness bug.

**Unit tests required (TDD):** Before implementation, write tests for:
- `_file_size_factor(30)` == 1.0 (at threshold boundary)
- `_file_size_factor(37)` == pytest.approx(0.811, abs=0.001)
- `_file_size_factor(0)` == 1.0 (edge case: default=1 guard via `max(1, chunk_count)`)
- `apply_hybrid_scoring()` with `hybrid=False` applies penalty to `code__` results
- `apply_hybrid_scoring()` does not apply penalty to `docs__` or `knowledge__` results

### Deferred: Chunk-Size Presets (Track B)

Re-indexing with smaller chunk sizes (e.g. 60-line `small` preset) was considered but
deferred pending validation of the scoring penalty. If Track A alone produces acceptable
precision improvements on the failing queries, Track B may be unnecessary. Track B
requires changes to `chunker.py`, `indexer.py`, and CLI; involves irreversible collection
deletion via `--force`; and needs empirical calibration of preset sizes. It is scoped
to a separate RDR if needed after Track A validation.

## Alternatives Considered

### Re-index with `--force` at identical chunk size (rejected)
Re-embedding at the same 150-line size produces identical embeddings and identical precision.
No improvement.

### `--hybrid` search mode (insufficient)
Tested: hybrid search (semantic + ripgrep) returned the same files as semantic-only for the
failing queries. The problem is that broad chunks score high semantically; ripgrep re-ranking
does not overcome a dominant vector score.

### Voyage reranker as sole fix (insufficient)
The reranker operates on the top-K candidates returned by ChromaDB. If the top-K is already
dominated by large-file chunks (because ChromaDB returns the highest-similarity results
regardless of source size), the reranker has no small-file candidates to surface. The scoring
penalty must be applied before or instead of relying on the reranker.

### Accept current behaviour (rejected)
Workaround is to write more specific, term-rich queries. This shifts the burden to users and
doesn't fix the underlying structural issue.

## Research Findings

### R1: Current chunking implementation (Confirmed)

**Source:** `src/nexus/chunker.py`

Code files use `chunk_file()` (line 162) which dispatches to:
- **AST path** (supported languages): llama-index `CodeSplitter` via `_make_code_splitter()`
  with `chunk_lines=_CHUNK_LINES` (150) and overlap computed as `150 × _OVERLAP` (0.15) = 22 lines
- **Fallback path**: `_line_chunk(content, chunk_lines=150, overlap=0.15)` (line 63)

Both paths enforce `_CHUNK_MAX_BYTES = 16_000` via `_enforce_byte_cap()`. Note:
`_enforce_byte_cap()` re-splits oversized AST nodes and renumbers `chunk_count`
accordingly. A file with very long functions can have an elevated `chunk_count` due to
byte-cap splitting independent of total file length. For the files in this RDR (main.py
cc=37, chunker.py cc=28), byte-cap splitting is unlikely to be a significant factor, but
`chunk_count` is not a pure proxy for file line-count.

Module-level constants (lines 30–34):
```python
_CHUNK_LINES = 150
_OVERLAP     = 0.15   # 15 % overlap → 22-line windows
_CHUNK_MAX_BYTES = 16_000
```

**`chunk_file()` currently accepts no chunk_size parameter** — it reads the module constants
directly. Track B (deferred) would require adding a 6th touch-point: `_make_code_splitter()`
which is where `CodeSplitter(chunk_lines=_CHUNK_LINES, ...)` is actually called.

### R2: Call chain from CLI → chunker (Confirmed)

```
nx index repo PATH
  commands/index.py:index_repo_cmd()           # no chunk params today
    indexer.py:index_repository()              # no chunk params today
      indexer.py:_run_index()
        indexer.py:_index_code_file()          # no chunk params today
          chunker.py:chunk_file(file, content) # reads module constants
            chunker.py:_make_code_splitter()   # actual CodeSplitter call site
```

Markdown prose uses `SemanticMarkdownChunker(chunk_size=512, chunk_overlap=50)` (tokens).
PDFs use `PDFChunker(chunk_chars=1500, overlap_percent=0.20)`.
Both are out of scope — this RDR addresses code files only.

### R3: Scope of change (Confirmed)

Track A (this RDR) touches **2 files only**:
- `src/nexus/scoring.py` — penalty logic
- `src/nexus/commands/search_cmd.py` — optional `--max-file-chunks` flag

Track B (deferred) would require 6 touch-points spanning indexer, chunker, and CLI.

### R4: Byte-limit guard (Confirmed)

At 4 bytes/token and typical prose, 150 lines ≈ 600–900 tokens ≈ 2,400–3,600 bytes — well
under the 16 KB ChromaDB Cloud document limit (RDR-005). Track A does not change chunk sizes,
so this limit is unaffected.

### R5: `--chunk-size` semantics clarification (Confirmed)

The original RDR draft used `--chunk-size 150` (the current default) as a "fix" — which
would produce identical embeddings and no improvement. The correct fix for large-file
dominance is either smaller chunks (Track B, deferred) or a scoring penalty (Track A, this
RDR). The Validation section uses Track A only.

### R6: `--force` / collection-deletion mechanism (Deferred with Track B)

Track B requires full collection re-index. The mechanism is: delete-and-recreate the
ChromaDB collection via `client.delete_collection(name)` before indexing. This is
irreversible (no built-in backup) and requires explicit `--force` from the user to avoid
accidental data loss. Since Track B is deferred, this is not in scope for this RDR.

### R7: `chunk_count` metadata available for filtering and scoring (Confirmed)

**Source:** `src/nexus/indexer.py:269`, `src/nexus/db/t3.py:306–366`, `src/nexus/scoring.py`

`chunk_count` is stored as `int` on every code chunk at index time. The `search()` method
spreads all metadata into results, so `chunk_count` is available in every
`SearchResult.metadata`.

`where` filters are fully supported and passed directly to ChromaDB:
```python
# Hard filter: exclude files with > 20 chunks (operand must be int, not float)
t3.search(query, collections, where={"chunk_count": {"$lte": 20}})
```

The `apply_hybrid_scoring()` function in `scoring.py` already receives full `SearchResult`
objects including metadata — adding a `file_size_factor` requires only the penalty formula
and one multiply per result.

### R8: Scoring pipeline integration point (Confirmed)

**Source:** `src/nexus/scoring.py:40–80`

The `apply_hybrid_scoring()` loop (lines 70–78) computes `r.hybrid_score` for each result.
The penalty must be applied **after** the vector/frecency score is set, **before** returning
the sorted list. The Voyage reranker (`rerank_results()`, lines 100–134) runs downstream and
overwrites `hybrid_score` with `relevance_score`.

The reranker fires when `len(set(r.collection for r in results)) > 1` — the condition
is on the **result** collection set, not the input collection list. In single-corpus code
searches run **without `--hybrid`**, all results share one collection, so the reranker
does not fire and the penalty has full effect on final ordering.

**Important:** when `--hybrid` is passed, ripgrep appends results with
`collection="rg__cache"`, making the result-collection set size 2 and triggering the
reranker. A single-corpus `--corpus code__<repo> --hybrid` search therefore **does** invoke
the Voyage reranker, which overwrites all `hybrid_score` values. Track A's penalty is a
no-op in that path. Validation must be run **without `--hybrid`** (see Protocol).

In multi-corpus default searches (multiple `code__`/`docs__`/`knowledge__` collections),
the reranker always fires and overwrites `hybrid_score`. Track A's primary benefit is
confined to single-corpus code searches run without `--hybrid`.

### R9: `--hybrid` flag scope — penalty must be unconditional (Confirmed)

**Source:** `src/nexus/scoring.py:73`

The `hybrid` flag at line 73 gates **frecency blending only**:
```python
if hybrid and r.collection.startswith("code__"):
    # blend vector + frecency
else:
    r.hybrid_score = v_norm  # vector only
```

The failing queries (Problem section) are run without `--hybrid`. If `file_size_factor`
were placed inside the `if hybrid` branch, it would be a no-op for standard searches — the
exact opposite of the intended fix. The penalty must be applied in a **separate block** after
the vector/frecency assignment, unconditionally for all `code__` results.

Type note: `r.metadata.get("chunk_count", 1)` returns whatever Python type ChromaDB
deserialised. Cast to `int` before use to guard against float round-trip:
```python
chunk_count = int(r.metadata.get("chunk_count", 1))
```

### R10: Baseline query measurements — actual chunk_counts (Confirmed)

**Source:** `nx search ... --corpus code__arcaneum-2ad2825c --n 20 --json`, run 2026-02-28

Baseline queries run against `code__arcaneum-2ad2825c` before any Track A changes.
Results shown are raw ChromaDB results (no penalty applied).

**Query 1: "embedding model GPU acceleration FastEmbed"** (canonical: `embeddings/client.py`)

| Rank | chunk_count | filename | distance |
|------|-------------|----------|----------|
| 1 | 37 | main.py | 0.8862 |
| 2 | 10 | doctor.py (via test_client.py) | 0.8919 |
| 3 | 7 | cpu_stats.py | 0.8920 |
| 4 | 5 | errors.py | 0.8986 |
| 5 | 10 | doctor.py | 0.9007 |
| 6 | 20 | memory.py | 0.9011 |
| 7 | 65 | uploader.py | 0.9034 |

Canonical `embeddings/client.py` (cc=107): **not in top 20** — recall failure.
Chunk_count of dominating files spans 5–65; files with cc≤10 would not be penalized at
threshold=30 but represent the majority of top results. This is a precision/recall split:
the dominating files are not irrelevant because of their chunk_count but because of semantic
surface area from different causes (broad CLI code in main.py; ML-adjacent terminology in
smaller files).

**Query 2: "chunk overlap tokenizer source code indexing"** (canonical: `indexing/markdown/chunker.py`)

| Rank | chunk_count | filename | distance |
|------|-------------|----------|----------|
| 1 | 37 | main.py | 0.9260 |
| 2 | 37 | main.py | 0.9287 |
| 3 | 10 | doctor.py | 0.9292 |
| 4 | 5 | analyze_profile.py | 0.9297 |
| 5 | 6 | qdrant-monitor-segments.py | 0.9299 |
| 7 | 37 | main.py | 0.9332 |

Canonical `indexing/markdown/chunker.py` (cc=28): **not in top 20** — recall failure.
`main.py` (cc=37) holds 4 of the top 20 slots.

**Simulated penalty arithmetic for Q2 at threshold=30:**

Let `max_d` = the maximum distance in the 20-result window (rank 20 distance).
`min_d` = 0.9260 (rank 1, main.py). The normalization range = `max_d − 0.9260`.

Post-penalty scores (penalty only, no frecency — validation runs without `--hybrid`):
- rank-1 main.py (d=0.9260): `v_norm = 1.0`; after penalty: `1.0 × 0.811 = 0.811`
- rank-3 doctor.py (d=0.9292, cc=10): `v_norm = 1 − 0.0032 / (max_d − 0.9260)`;
  penalty factor = 1.0 (cc≤30)

For doctor.py to overtake penalized main.py:
```
1 − 0.0032 / (max_d − 0.9260) > 0.811
0.0032 / (max_d − 0.9260) < 0.189
max_d − 0.9260 > 0.0169
max_d > 0.9429
```

Rank 7 (the last shown) is already at 0.9332; ranks 8–20 extend the window further.
A 20-result window with `max_d > 0.9429` is essentially certain. The claim holds as long
as the full baseline data is confirmed to include at least one result with distance > 0.9429.
Validation Step 1 should record the rank-20 distance for Q2 to confirm this bound.

**Query 3: "MeiliSearch full text search index documents"** (canonical: `fulltext/client.py`)

| Rank | chunk_count | filename | distance |
|------|-------------|----------|----------|
| 1 | 18 | qdrant_indexer.py | 0.9221 |
| 2 | 6 | qdrant-restore.sh | 0.9256 |
| 3–4 | 18 | qdrant_indexer.py | 0.9286–0.9342 |
| 6 | 77 | collections.py | 0.9354 |
| 10 | 77 | collections.py | 0.9375 |

Canonical `fulltext/client.py` (cc=unknown): **not retrievable** — not found in top 30
across multiple queries specific to its content. May require re-indexing or investigation
of the collection's current state.

**Recall vs. precision distinction:**

Track A can improve precision (reranking files that are retrieved). It cannot improve recall
(getting canonical files into the candidate set when they are absent). Queries 1–3 all have
recall failures. The penalty helps query 2 by suppressing `main.py`; it has limited effect
on queries 1 and 3 because the canonical files are not in the candidate pool regardless of
scoring. Recall improvement, if needed, requires a different mechanism (larger K from
ChromaDB, Track B smaller chunks, or query reformulation).

## Open Questions (Resolved)

1. **What chunk size fixes the failing queries?** *(Deferred to Track B.)* Track A
   addresses precision within the single-corpus search path without re-indexing.
2. **Scope for prose/docs:** *(Resolved)* **Code only.** Markdown and PDF chunkers out of scope.
3. **Collection invalidation:** *(Resolved — deferred with Track B.)* Require explicit
   `--force` when Track B is implemented. Not needed for Track A.
4. **`--hybrid` flag scope:** *(Resolved — R9.)* Penalty is unconditional for `code__`
   results, applied outside the `if hybrid` branch.
5. **Multi-corpus / reranker path:** *(Resolved — R8, Problem scope note.)* Track A is
   scoped to single-corpus code searches **run without `--hybrid`**. The reranker fires
   whenever result-collection diversity > 1: in multi-corpus mode and in any single-corpus
   search where `--hybrid` is passed (which injects `rg__cache` results). Use
   `--max-file-chunks` for multi-corpus or `--hybrid` searches.

## Validation

All validation runs single-corpus against `code__arcaneum-2ad2825c` (`--corpus
code__arcaneum-2ad2825c`) and **without `--hybrid`**. Using `--hybrid` injects ripgrep
results under `rg__cache`, triggering the Voyage reranker and masking the penalty effect.
Multi-corpus default search is also out of scope (see R8). No re-index required.

### Success criteria

Track A is validated as **effective** if `main.py` (cc=37) is no longer rank 1 for query 2
after the penalty is applied, without demoting small focused files (cc≤10) that were
correctly ranked. This is achievable given the measured chunk_counts (R10).

Track A is validated as **sufficient** for a query if the canonical file appears in top-3.
Given the recall failures for Q1–Q3 (canonical files absent from the top-20 candidate set —
R10), Track A is **not expected to be sufficient for Q1–Q3**. Q4 and Q5 use name-specific
queries where the canonical file is likely retrievable, making them the primary sufficiency
tests.

**Q4/Q5 failure decision tree:** Both canonical targets have large chunk counts (Q4:
`embeddings/client.py` cc=107 → factor=0.280; Q5: `indexing/source_code_pipeline.py`
cc=unknown). The penalty may suppress them even when they are retrievable. If Q4 or Q5
fails top-3, diagnose before escalating to Track B:

1. **Is the canonical file retrievable?** Run `--n 50` for the same query. If the file
   appears at rank 4–50, the penalty is suppressing it.
2. **If retrievable but penalized into low rank:** The threshold constraint is fundamental —
   `_FILE_SIZE_THRESHOLD` cannot be raised above 36 without also removing the penalty from
   `main.py` (cc=37), which would break the Q2 primary criterion:
   - threshold ≥ 37 → `main.py` factor = 1.0 (Q2 criterion fails simultaneously)
   - threshold ∈ [28, 36] → `main.py` penalized; `chunker.py` (cc=28) unpenalized
   - For `embeddings/client.py` (cc=107) to reach factor=1.0 requires threshold ≥ 107

   If Q4's canonical file is retrievable but penalized into low rank, the soft penalty
   has a **fundamental target conflict** between Q2 (demote cc=37) and Q4 (preserve
   cc=107). A single global threshold cannot satisfy both. Accept that Q4 sufficiency
   is not achievable with Track A, and escalate to Track B for smaller chunk sizes.
3. **If not retrievable at rank 1–50:** recall failure — escalate to Track B.
   The penalty cannot help files absent from the ChromaDB candidate pool.

### Protocol

**Step 1 — Baseline** (done — see R10 for Q1–Q3)

Run Q4, Q5, and two regression queries before deploying Track A. All queries **without
`--hybrid`** (see R8 — `--hybrid` injects `rg__cache` results and triggers the reranker,
masking the penalty):
```
nx search "class EmbeddingClient" --corpus code__arcaneum-2ad2825c --n 20
nx search "class SourceCodePipeline" --corpus code__arcaneum-2ad2825c --n 20
```
For Q2 also record the rank-20 distance to confirm `max_d > 0.9429` (see R10 calculation).

**Regression queries (select two before deployment and record here):** Choose queries from
a different domain (e.g., git operations, network/socket code, CLI argument parsing) where
none of the target files should be related to embedding or chunking. Record for each:
top-3 files and their chunk_counts. These are the reference for Step 3.

Record rank and chunk_count of canonical file and top-5 results for Q4/Q5. These provide
the pre-Track-A reference for the sufficiency test.

**Step 2 — Deploy Track A and re-run all queries**

Apply `_FILE_SIZE_THRESHOLD = 30` and `_file_size_factor` in `scoring.py`. Re-run Q1–Q5
and the two regression queries **without `--hybrid`** (identical parameters to Step 1).
Using `--hybrid` injects `rg__cache` results and triggers the Voyage reranker, which
overwrites all `hybrid_score` values and masks the penalty effect. For each query record:
- New rank of any file that was in top-5 at baseline
- Whether the canonical file appears in top-3 (sufficiency check for Q4/Q5)
- For Q4/Q5: if canonical file is retrievable but ranked 4–20, apply the failure
  decision tree from the Success Criteria section above

Primary assertion for Q2: `main.py` (cc=37) is no longer rank 1.

**Step 3 — Confirm no regression on unrelated queries**

Re-run the two regression queries recorded in Step 1 against the same corpus (without
`--hybrid`). The penalty intentionally demotes large files; large files moving down is
**expected behavior, not a regression**. The regression criterion is:

> No file with cc≤30 that appeared in the baseline top-3 for a regression query has been
> displaced below rank 3 after Track A is applied.

If a cc≤30 file drops from the top-3 in the regression queries, diagnose whether the
displacement is caused by a bug in the unconditional penalty block (e.g., the guard
`r.collection.startswith("code__")` is mis-scoped) rather than the intended penalty effect.

### Query table

| # | Query | Canonical file | Canonical cc | Canonical penalty factor | Baseline status | Track A target |
|---|-------|---------------|-------------|--------------------------|-----------------|----------------|
| Q1 | `"embedding model GPU acceleration FastEmbed"` | `embeddings/client.py` | 107 | 0.280 | recall failure (R10) | suppress main.py (cc=37) |
| Q2 | `"chunk overlap tokenizer source code indexing"` | `indexing/markdown/chunker.py` | 28 | 1.000 | recall failure (R10) | suppress main.py (cc=37) — **primary criterion** |
| Q3 | `"MeiliSearch full text search index documents"` | `fulltext/client.py` | unknown | unknown | recall failure (R10) | no penalty (qdrant_indexer.py cc=18 < threshold); use `--max-file-chunks 17` to suppress it |
| Q4 | `"class EmbeddingClient"` | `embeddings/client.py` | 107 | **0.280** | baseline in Step 1 | top-3 canonical; if fails, see failure decision tree |
| Q5 | `"class SourceCodePipeline"` | `indexing/source_code_pipeline.py` | unknown | unknown | baseline in Step 1 | top-3 canonical; if fails, see failure decision tree |

### Threshold adjustment

If `main.py` (cc=37) is still rank 1 after Track A at threshold=30:
- Valid penalty range for `main.py`: threshold must be **< 37** (at threshold ≥ 37,
  `min(1.0, threshold/37) = 1.0` — no penalty applied)
- Valid non-penalty range for Q2 canonical `chunker.py` (cc=28): threshold must be **≥ 28**
- **Usable range: threshold ∈ [28, 36]**; current value 30 sits safely in this range
- Try threshold=33: `main.py` factor = 33/37 = 0.892 (lighter penalty, may be sufficient)
- Try threshold=28: `main.py` factor = 28/37 = 0.757; `chunker.py` factor = 28/28 = 1.000
  (minimum safe value — no safety margin between canonical and dominating files)
- If threshold=28 still does not suppress `main.py` as rank 1, the soft penalty approach
  is insufficient for Q2 without Track B

If Q4/Q5 canonical files are not in top-3: see the Q4/Q5 failure decision tree in Success
Criteria above — the constraint conflict between Q2 and Q4 means Track A cannot satisfy both.
Escalate to Track B if Q4 sufficiency is required.

For Q3 precision improvement: `qdrant_indexer.py` has cc=18, so `--max-file-chunks 30`
does not suppress it (18 ≤ 30 passes the filter). To exclude `qdrant_indexer.py` specifically
requires `--max-file-chunks 17`. This hard filter works in multi-corpus mode since it
applies at the retrieval layer, before the reranker sees any results.
