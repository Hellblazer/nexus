---
title: "CCE Query Model Mismatch: voyage-context-3 Collections Unsearchable Since rc1"
date: 2026-02-28
severity: high
introduced: "053f54d — 2026-02-22"
discovered: "2026-02-28 (post rc5 release)"
affected_releases: [rc1, rc2, rc3, rc4, rc5]
affected_collections: [docs__, knowledge__, rdr__]
status: resolved
fix_pr: "https://github.com/Hellblazer/nexus/pull/33"
---

# Post-Mortem: CCE Query Model Mismatch

## Summary

Every `docs__`, `knowledge__`, and `rdr__` collection indexed since rc1 has been
effectively unsearchable. Documents in these collections use `voyage-context-3`
(Contextualized Chunk Embedding, CCE) at index time, but the search path queries
with `voyage-4`. These two models produce vectors in **incompatible geometric spaces**:
cosine similarity between any `voyage-4` query and any CCE-indexed chunk is ~0.05 —
effectively random noise. Results are not ranked by semantic relevance; they are ranked
by volume (collections with more chunks win purely by probability).

Only `code__` collections (voyage-code-3 at index, voyage-4 at query) have been
working as intended.

---

## Timeline

| Date | Event |
|------|-------|
| 2026-02-22 | `1c23ec5` — CCE implementation landed. `doc_indexer.py` indexes `docs__` and `knowledge__` with `voyage-context-3` via the `/v1/contextualizedembeddings` endpoint. `corpus.py` initially returned `voyage-code-3` for code and `voyage-4` for everything else — consistent with CCE intent. |
| 2026-02-22 | `053f54d` — "Fix voyage-4 as universal query model". Commit message argues: *"Code collections are indexed with voyage-code-3 but queried with voyage-4 — the semantic spaces are compatible enough for effective retrieval, and a single query model simplifies cross-corpus search."* The same reasoning was **incorrectly extended** to CCE collections. `embedding_model_for_collection()` was changed to return `"voyage-4"` for all collections unconditionally. |
| 2026-02-22 | rc1–rc4 released. No search tests against live CCE collections. Bug undetected. |
| 2026-02-28 | rc5 released. Post-release `nx search "four store t3 architecture" --corpus rdr` returns only RDR-001/RDR-002 for every query, never RDR-004. |
| 2026-02-28 | Investigation reveals cosine similarity of ~0.05 across all CCE chunks for all queries. Deep research confirms root cause: voyage-4 and voyage-context-3 are incompatible vector spaces. |

---

## Root Cause

### The Assumption

Commit `053f54d` made this architectural decision:

> "voyage-4 is the universal query model for all collection types."

This was based on an observed pattern: `voyage-code-3` (code index model) and `voyage-4`
(query model) work together acceptably for `code__` collections. The commit assumed
this cross-model compatibility generalised to `voyage-context-3`.

### Why the Assumption Was Wrong

`voyage-code-3` and `voyage-4` are members of overlapping model families that share
vector space geometry (unconfirmed but empirically tolerable).

`voyage-context-3` is architecturally different:

1. **Different API endpoint**: CCE uses `/v1/contextualizedembeddings`, not `/v1/embeddings`.
   The model name `voyage-context-3` is rejected entirely by `client.embed()`.

2. **Different training objective**: CCE trains on cross-chunk context propagation,
   not point-in-space retrieval. The resulting embedding space has different geometry.

3. **Incompatible vector spaces**: Cosine similarity between `voyage-4` query vectors
   and `voyage-context-3` document vectors is ~0.05 — indistinguishable from random
   orthogonal vectors. There is no semantic signal.

4. **Official documentation**: Voyage AI's Voyage 4 family cross-model compatibility
   (voyage-4-large ↔ voyage-4-lite ↔ voyage-4-nano) is explicitly scoped to the
   Voyage 4 family. The CCE docs specify `voyage-context-3` must be used at both
   index and query time via `contextualized_embed()`.

### The Compounding Bug

Even if `corpus.py` had returned `"voyage-context-3"`, the query path would still
be broken. ChromaDB's `VoyageAIEmbeddingFunction` always calls `client.embed()`,
which rejects `voyage-context-3`. The fix requires bypassing the ChromaDB EF
entirely for CCE collections and calling `contextualized_embed([[query]], input_type="query")`
directly.

---

## Impact

| Collection type | Index model | Query model (actual) | Searchable? |
|----------------|-------------|----------------------|-------------|
| `code__*` | voyage-code-3 | voyage-4 | Partially (cross-model compat unconfirmed but tolerable) |
| `docs__*` | voyage-context-3 | voyage-4 | **No** — cosine sim ~0.05 |
| `knowledge__*` | voyage-context-3 | voyage-4 | **No** — cosine sim ~0.05 |
| `rdr__*` | voyage-context-3 | voyage-4 | **No** — cosine sim ~0.05 |

All `nx search` queries that include `docs`, `knowledge`, or `rdr` corpora (the default
includes `knowledge`, `code`, `docs`) have been returning semantically meaningless
results from these corpora since rc1. The `code__` results within the same query were
correct; they were mixed with noise from the other corpora.

`nx store`, `nx memory promote`, and any other operation that writes to `docs__` or
`knowledge__` has been producing data that cannot be effectively retrieved.

---

## What Was Not Caught

1. **No integration test for CCE retrieval quality.** Tests verified that `upsert_chunks()`
   accepted CCE embeddings and that `search()` returned rows — not that the *right*
   rows were returned.

2. **No cross-model compatibility validation.** The assumption that voyage-4 queries
   would work against CCE documents was never tested against a live collection.

3. **Misleading symptom.** Searches did return results — they just returned the wrong
   ones. Collections with more chunks (RDR-001: 48, RDR-002: 67) consistently
   out-ranked collections with fewer chunks (RDR-004: 18) because with near-uniform
   distances, volume is the only differentiator.

4. **No post-index verification step.** `nx index` reported success, `nx collection info`
   showed correct document counts. There was no health check confirming that indexed
   content was actually retrievable.

---

## Lessons Learned

1. **Cross-model embedding compatibility must be verified empirically, not assumed.**
   Same dimension ≠ same vector space. The CCE architecture is intentionally different
   from standard retrieval embedding.

2. **Index model ≠ query model is a footgun.** The decision to support different models
   at index and query time (while sometimes useful) creates a class of bugs that are
   silent at the API level but catastrophic for retrieval quality.

3. **Retrieval quality tests must assert semantic correctness, not just row count.**
   A test that asserts `len(results) > 0` will pass even when all results are noise.

4. **New embedding strategies need a retrieval smoke test before release.** A single
   `assert known_document in top_k_results(known_query)` would have caught this on day one.

---

## Fix (Resolved)

The original CCE query path fix was deployed in PR #33. RDR-040 (v2.4.0, PR #118)
then closed the systemic gaps that allowed the bug to ship undetected:

**Original fix (PR #33):**

- `corpus.py` — `embedding_model_for_collection()` returns `"voyage-context-3"` for CCE collections
- `db/t3.py` — `search()` and `put()` bypass VoyageAIEmbeddingFunction, call `contextualized_embed()` directly

**RDR-040 gap closure (PR #118, v2.4.0):**

- **C1**: Single-chunk CCE documents now use `contextualized_embed()` — the `len(chunks) < 2` fallback to voyage-4 was a recurrence of this exact bug class
- **C4**: Partial CCE batch failure re-embeds entire document consistently — prevents mixed-model vectors
- **C2/C3**: Paginated all unbounded `col.get()` calls (ChromaDB 300-record hard cap)
- **C5**: MCP collection cache race eliminated
- **A1**: Retrieval quality tests assert semantic rank ordering, not just row count
- **A2**: `verify --deep` uses known-document probe with distance reporting
- **A3**: Cross-model invariant regression test — fails if CCE index/query models diverge
- **A4**: `nx collection reindex` command for recovery
- **B1–B4**: MCP server enhanced with multi-corpus search + collection management tools

## Additional Bug Discovered During Validation

A round-trip test of the v2.4.0 release reproduced the *collection naming* variant of
this failure:

`nx index pdf --collection knowledge` passed the bare name `"knowledge"` directly to
ChromaDB (bypassing `t3_collection_name()`), creating a collection that `nx search`
could not find (search uses `resolve_corpus()` which matches `knowledge__*`). Worse,
`index_model_for_collection("knowledge")` returns `voyage-4` (not CCE), so all chunks
were embedded with the wrong model — the same cascading failure as the original bug.

**Fix**: PR #119 normalizes `--collection` through `t3_collection_name()` so bare names
like `"knowledge"` become `"knowledge__knowledge"`.

**Cascading failure chain** (identical pattern to the original):
```
--collection knowledge (user input)
  → Missing t3_collection_name() call
    → Collection "knowledge" created (wrong name)
      → index_model_for_collection("knowledge") = voyage-4 (wrong model)
        → Chunks embedded with voyage-4 (wrong space)
          → Chunks invisible to search (wrong collection name)
```

See: `docs/postmortem/2026-03-23-pdf-index-collection-mismatch.md`
