---
title: "CCE Post-Mortem Gap Closure & MCP Server Enhancement"
id: RDR-040
type: Quality / Feature
status: accepted
accepted_date: 2026-03-23
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-23
related_issues: []
---

# RDR-040: CCE Post-Mortem Gap Closure & MCP Server Enhancement

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The CCE query model mismatch (post-mortem: `cce-query-model-mismatch`) was fixed in PR #33, but the post-mortem identified four systemic gaps that allowed the bug to ship undetected through five release candidates. These gaps remain open. Additionally, the MCP server lacks tools that agents need for self-service collection management and multi-corpus search.

This RDR addresses both: closing the post-mortem gaps to prevent recurrence, and enhancing the MCP server to give agents the same capabilities the CLI provides.

## Context

### Background

The CCE fix (PR #33) corrected the query path so `docs__`, `knowledge__`, and `rdr__` collections use `voyage-context-3` at both index and query time. The code is correct now. What's missing is the **test and tooling infrastructure** that would have caught the bug before release:

1. Tests verified mechanism (correct API called) but not ranking quality
2. No post-index verification that indexed content is actually retrievable
3. No regression guard for the cross-model invariant
4. No re-indexing command for corrupted collections

Separately, the MCP server exposes `search` with a single-corpus default (`"knowledge"`), while the CLI defaults to `("knowledge", "code", "docs")`. Agents using the MCP tool get a narrow view unless they know to call search three times. The server also lacks `collection_list`, `collection_info`, and `collection_verify` tools.

### Prior Art

- **Post-mortem**: `docs/rdr/post-mortem/cce-query-model-mismatch.md` — root cause analysis and fix description
- **RDR-034**: MCP server design — established the current tool surface
- **RDR-017**: Indexing progress reporting — established `--monitor` pattern with tqdm

### Scope

Two tracks, nine deliverables:

**Track A — Post-Mortem Gap Closure:**
- A1: Retrieval quality unit tests (semantic ranking, not just row count)
- A2: Enhanced `collection verify --deep` (known-document retrieval check)
- A3: Cross-model invariant regression test
- A4: `nx collection reindex <name>` command
- A5: Per-chunk progress for pdf/md indexing

**Track B — MCP Server Enhancement:**
- B1: Multi-corpus search (match CLI default behavior)
- B2: `collection_list` MCP tool
- B3: `collection_info` MCP tool
- B4: `collection_verify` MCP tool

**Track C — Bug Fixes (from codebase audit):**
- C1: Single-chunk CCE fallback to voyage-4 creates model mismatch at query time (H1)
- C2: Unpaginated `col.get()` in `_prune_deleted_files` truncates at 300 records (H2)
- C3: Same pagination bug in `_prune_misclassified` and `_run_index_frecency_only`
- C4: Mixed-model CCE batches on partial failure — vectors from two spaces stored together
- C5: MCP `_get_collection_names()` cache race condition (no lock)

**Track D — Documentation & Plugin Updates:**
- D1: `nx/skills/nexus/reference.md` — add new MCP tools (collection_list, collection_info, collection_verify, multi-corpus search)
- D2: `docs/cli-reference.md` — add `nx collection reindex`, update `verify --deep` docs
- D3: `docs/architecture.md` — update MCP server tool surface
- D4: `nx/agents/pdf-chromadb-processor.md` — update for per-chunk progress
- D5: `CLAUDE.md` — note single-chunk CCE caveat in collection conventions
- D6: `nx/CHANGELOG.md` — release notes for all changes

## Design

### A1: Retrieval Quality Unit Tests

**Location:** `tests/test_t3.py`

Add tests using the existing `local_t3` fixture (`EphemeralClient` + `DefaultEmbeddingFunction` / ONNX MiniLM-L6-v2, 384-dim). No API keys needed; MiniLM produces semantically meaningful embeddings:

- **`test_search_returns_closest_document_first`** — Index 3 semantically distinct documents (e.g., "Python web framework", "quantum physics", "Italian cooking"). Query with a related term (e.g., "Django REST API"). Assert the most semantically relevant document is the top result, not just that results are non-empty.
- **`test_search_unrelated_query_produces_high_distance`** — Index domain-specific documents, query with unrelated terms. Assert distances are significantly higher than for relevant queries — this is the failure mode of the original bug (cross-space vectors behave like random noise).

These tests use the `local_t3` fixture (which injects `DefaultEmbeddingFunction` via `_ef_override`), matching the existing 20+ test pattern in the suite.

### A2: Enhanced `collection verify --deep`

**Location:** `src/nexus/commands/collection.py`

Current `verify --deep` fires a generic `"health check probe"` query. Enhance to:

1. Fetch the first stored document from the collection via `col.peek(limit=1)` (deterministic — returns first inserted document, not random; any real document works for the probe)
2. Extract the first 50 words of its content as a query
3. Run `db.search()` with that query against the collection
4. Assert the original document appears in top-k results
5. Report the raw distance value and health status

**Distance metric note:** ChromaDB uses L2 (squared Euclidean) by default unless the collection is created with `hnsw:space=cosine`. The health thresholds must be calibrated during implementation by testing against a known-healthy collection and a known-broken one. The design defers specific threshold values — implementation should:
- Query `col.metadata` to determine the distance space
- Use relative comparison (probe distance vs. collection median) rather than absolute thresholds
- Report the raw distance and metric so the user can interpret

This catches the exact failure mode: CCE-indexed docs queried with the wrong model produce distances indistinguishable from noise (cosine sim ≈ 0.05 per post-mortem).

**Shared function location:** Extract the verify-deep logic to `src/nexus/db/t3.py` (or a new `src/nexus/health.py`) — NOT `commands/collection.py` — so the MCP tool (B4) can call it without importing from the commands layer.

### A3: Cross-Model Invariant Regression Test

**Location:** `tests/test_corpus.py`

Strengthen the existing `test_embedding_model_for_collection_regression` (line 142 of `test_corpus.py`) which already asserts individual model values. Add `test_cce_index_query_model_invariant` for the **cross-function** invariant that was actually violated:

```python
for prefix in ("docs__x", "knowledge__x", "rdr__x"):
    idx = index_model_for_collection(prefix)
    qry = embedding_model_for_collection(prefix)
    if idx == "voyage-context-3":
        assert qry == "voyage-context-3", (
            f"{prefix}: CCE index model requires CCE query model, "
            f"got query={qry}. See post-mortem: cce-query-model-mismatch"
        )
```

The existing regression test checks individual return values; this test checks the **joint invariant** (both functions must agree for CCE prefixes). The original bug had `index_model` returning `voyage-context-3` while `embedding_model` returned `voyage-4`.

### A4: `nx collection reindex <name>`

**Location:** `src/nexus/commands/collection.py`

New command that:

1. Looks up collection metadata (`embedding_model`, source paths from stored chunk metadata)
2. **Pre-delete safety check**: Count entries without `source_path` metadata. If any exist (e.g., `nx store put` entries in `knowledge__` collections), abort with error unless `--force` is passed. This prevents silent data loss of manual entries.
3. Deletes the collection
4. Re-indexes from source:
   - `code__*` → re-run `index_repo` for the registered repo (lookup via `RepoRegistry`)
   - `docs__*` → re-run `index_markdown` or `index_pdf` for each distinct `source_path` from chunk metadata
   - `rdr__*` → extract `source_path` values from chunk metadata, derive the RDR directory as `parent.parent`, call `batch_index_markdowns()` directly (NOT `index_rdr`, which requires a repo root and uses lossy hash-based collection naming)
   - `knowledge__*` → re-index only entries that have `source_path`; skip manual entries (warned in step 2)
5. Runs `verify --deep` automatically after re-indexing
6. Reports before/after document counts and any skipped/missing sources

**Limitations:**
- `knowledge__` manual entries (from `nx store put`) have no `source_path` and cannot be auto-reindexed. These are warned about in the pre-delete check.
- If source files have been moved or deleted, reindex warns per missing source rather than silently dropping them.
- Absolute paths in `source_path` are tied to the indexing machine; reindex must run on the same filesystem.

### A5: Per-Chunk Progress for PDF/MD Indexing

**Location:** `src/nexus/doc_indexer.py`, `src/nexus/commands/index.py`

**Justification:** While not one of the four post-mortem gaps, per-chunk progress addresses operational observability of CCE indexing. CCE batching via `_embed_with_fallback` can stall on large documents (30+ seconds for multi-page PDFs); without per-chunk progress, the failure mode is indistinguishable from a hang. This was a contributing factor in the delayed discovery of the original bug — indexing appeared to "work" with no visible anomalies.

Add an optional `on_progress: Callable[[int, int], None] | None` callback to `_embed_with_fallback()`:
- Emitted as `on_progress(embedded_count, total_chunks)` after each Voyage API call (per-batch, not per-chunk — minimal overhead since it fires only after network I/O)
- Thread through: `index_pdf`/`index_markdown` → `_index_document` → `_embed_with_fallback`

Wire in the CLI commands (`index_pdf_cmd`, `index_md_cmd`):
- When `--monitor` or non-TTY: create a tqdm bar over chunks (matching `index_repo`'s pattern)
- Single-file operations get a chunk-level progress bar instead of just a post-hoc metadata dump

### B1: Multi-Corpus Search

**Location:** `src/nexus/mcp_server.py`

Change the `search` tool signature:

```python
@mcp.tool()
def search(query: str, corpus: str = "knowledge,code,docs", n: int = 10) -> str:
```

- Accept comma-separated corpus values: `"knowledge,code,docs"` (new default matching CLI)
- Split on `,`, resolve each prefix, merge into a single target list
- Backward compatible: `corpus="knowledge"` still works as before
- `corpus="all"` as a convenience alias — expand to `"knowledge,code,docs,rdr"` before splitting (explicit substitution, not passed to `resolve_corpus` directly which would look for `all__*` and find nothing)

### B2: `collection_list` MCP Tool

**Location:** `src/nexus/mcp_server.py`

```python
@mcp.tool()
def collection_list() -> str:
    """List all T3 collections with document counts."""
```

Delegates to `db.list_collections()`. Returns formatted table of name, count, embedding model.

### B3: `collection_info` MCP Tool

**Location:** `src/nexus/mcp_server.py`

```python
@mcp.tool()
def collection_info(name: str) -> str:
    """Get detailed information about a T3 collection."""
```

Delegates to `db.collection_info()`. Returns count, embedding model, index model, sample metadata.

### B4: `collection_verify` MCP Tool

**Location:** `src/nexus/mcp_server.py`

```python
@mcp.tool()
def collection_verify(name: str) -> str:
    """Verify a collection's retrieval health via known-document probe."""
```

Runs the enhanced verify-deep logic from A2 (extracted to a shared function in `db/t3.py` or `commands/collection.py`). Returns health status, distance, and document count.

### C1: Single-Chunk CCE Fallback Fix

**Location:** `src/nexus/doc_indexer.py:117-121`

**Bug:** When `_embed_with_fallback()` receives a CCE-target model but the document has < 2 chunks, it silently falls back to `voyage-4`. But `T3Database.search()` always uses `_cce_embed()` (voyage-context-3) for the collection. This recreates the original CCE mismatch for short documents.

**Fix:** Two options (choose during implementation):
1. **Option A**: For single-chunk CCE, use `voyage-4` for both index AND query — add metadata `embedding_model: "voyage-4"` and make `search()` check per-document model, not per-collection
2. **Option B**: For single-chunk CCE, still use `contextualized_embed()` with `inputs=[[chunk]]` — CCE may still produce valid embeddings for a single chunk (the API accepts it; the 2-chunk minimum was our assumption, not an API constraint)

Option B is simpler and more correct — it keeps the entire collection in one vector space. Validate during implementation that `contextualized_embed(inputs=[[single_chunk]])` actually works.

### C2: Paginate `_prune_deleted_files`

**Location:** `src/nexus/indexer.py:593`

**Bug:** `col.get()` without `limit=` silently truncates at 300 records (ChromaDB Cloud hard cap). Large repos never fully prune deleted files.

**Fix:** Replace with paginated loop matching the pattern in `expire()`:
```python
offset = 0
while True:
    batch = _chroma_with_retry(col.get, include=["metadatas"], limit=300, offset=offset)
    # ... process batch ...
    if len(batch["ids"]) < 300:
        break
    offset += 300
```

### C3: Same Pagination Fix for `_prune_misclassified` and `_run_index_frecency_only`

**Location:** `src/nexus/indexer.py:563,572` and `src/nexus/indexer.py:281-283`

Same pattern as C2. Apply paginated `col.get()` with `limit=300` to all unbounded get() calls.

### C4: Mixed-Model CCE Batch Consistency

**Location:** `src/nexus/doc_indexer.py:124-143`

**Bug:** When multi-batch CCE embedding partially fails (some batches succeed with voyage-context-3, others fall back to voyage-4), the `all_embeddings` list contains vectors from two incompatible spaces. Metadata records `voyage-4` for all chunks.

**Fix:** If any batch falls back, re-embed the entire document with `voyage-4` for consistency. Never store mixed-model vectors. Log a structured warning when this happens.

### C5: MCP Collection Cache Lock

**Location:** `src/nexus/mcp_server.py:65-72`

**Bug:** `_get_collection_names()` has no threading lock. Under concurrent MCP tool calls, a race between cache invalidation and read can return an empty list for up to 60 seconds.

**Fix:** Protect with a `threading.Lock`, or atomically update `_collections_cache` and `_collections_cache_ts` as a single tuple assignment.

### D1–D6: Documentation & Plugin Updates

After all code changes, update:

| ID | File | Change |
|----|------|--------|
| D1 | `nx/skills/nexus/reference.md` | Add collection_list, collection_info, collection_verify MCP tools; update search default |
| D2 | `docs/cli-reference.md` | Add `nx collection reindex`; update `verify --deep`; document per-chunk progress |
| D3 | `docs/architecture.md` | Update MCP tool surface (8 → 12 tools); note single-chunk CCE handling |
| D4 | `nx/agents/pdf-chromadb-processor.md` | Update for per-chunk progress expectations |
| D5 | `CLAUDE.md` | Add single-chunk CCE caveat to collection conventions |
| D6 | `nx/CHANGELOG.md` | Release notes for all changes |

## Research Findings

### R1: ef_override + EphemeralClient (A1 feasibility) — CONFIRMED

`T3Database.__init__` accepts `_ef_override` which bypasses VoyageAI EF entirely. The `local_t3` fixture in `conftest.py` already pairs `EphemeralClient()` with `DefaultEmbeddingFunction()` (ONNX MiniLM-L6-v2, 384-dim). Deterministic, semantically meaningful, zero API keys. 20+ tests use this pattern. For A1: use real MiniLM embeddings and assert rank ordering — no need for hand-crafted vectors.

### R2: ChromaDB peek() API (A2 feasibility) — CONFIRMED

`col.peek(limit=N)` exists, returns `{ids, documents, metadatas}`. Returns the first N items (not random), default limit=10. For verify-deep: `peek(limit=1)` retrieves a stored document for the known-document probe. Alternative: `col.get(offset=0, limit=1, include=["metadatas", "documents"])`.

### R3: Source Metadata for Reindex (A4 feasibility) — CONFIRMED with caveat

All collection types store `source_path` (absolute) in chunk metadata **except** `knowledge__` (manual entries via `nx store put`). `delete_by_source()` already exists in t3.py. **Caveat**: `knowledge__` entries cannot be auto-reindexed — they have no source path. Reindex command should walk the filesystem (like `nx index repo` does) rather than reconstruct from metadata — more robust, handles moved files.

### R4: Embed Callback Injection Point (A5 feasibility) — CONFIRMED

`_embed_with_fallback()` in `doc_indexer.py` has two batching loops:
- **CCE path**: `_batch_chunks_for_cce()` splits by token limits, iterates batches
- **Standard path**: fixed `_EMBED_BATCH_SIZE=128` per API call

Best injection: add `on_progress: Callable[[int, int], None] | None` parameter. Emit `on_progress(len(all_embeddings), len(chunks))` after each API call. Thread through: `index_pdf`/`index_markdown` → `_index_document` → `_embed_with_fallback`. Minimal overhead — only fires after network I/O.

### R5: MCP Server Conventions (B1–B4 feasibility) — CONFIRMED

8 tools currently registered. FastMCP framework (`mcp>=1.0`), no tool count limit. Pattern: `@mcp.tool()` decorator, snake_case names, all return `str`, never raise (return `"Error: {msg}"`). Lazy singletons with `_inject_t1`/`_inject_t3` for testing. Collection names cached 60s via `_get_collection_names()`. Adding 4 more tools is straightforward and follows established patterns.

### R6: Codebase Audit (bug discovery) — 2 HIGH, 5 IMPORTANT

Full audit of `src/nexus/` found no remaining CCE mismatches for the common case. Two high-severity bugs discovered:
- **H1**: Single-chunk CCE fallback to voyage-4 recreates original mismatch for short documents
- **H2**: Unpaginated `col.get()` in `_prune_deleted_files` truncates at 300 records

Plus: mixed-model batch consistency (I2), MCP cache race (I1), pagination gaps in frecency-only and misclassified paths (I3/I4). All embedded model paths verified end-to-end. TTL guards confirmed correct everywhere.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| A4 reindex can't find source files | Warn per missing source, report partial count |
| A2 verify probe false-positive on tiny collections | Skip probe if collection has < 2 documents |
| B1 multi-corpus default increases API cost | Same behavior as CLI — users already expect this |
| A5 callback adds overhead to embedding loop | Callback is `None` by default; no-op when unused |
| C1 single-chunk CCE may not work with contextualized_embed | Validate during impl; fall back to option A if API rejects |
| C4 full re-embed on partial failure increases latency | Only triggers on API errors; correctness > speed |
| D1–D6 doc updates lag code changes | Doc updates are a gated phase — code ships only after docs updated |

## Success Criteria

**Track A — Post-Mortem Gaps:**
1. Unit tests assert rank ordering with real MiniLM embeddings — not just `len(results) > 0`
2. `nx collection verify --deep <name>` reports raw distance + metric; catches broken collections where probe document is not in top-k
3. Cross-model invariant test (joint `index_model` + `embedding_model`) fails if someone re-routes CCE queries to voyage-4
4. `nx collection reindex <name>` works for code__, docs__, rdr__; aborts for knowledge__ with sourceless entries unless --force
5. `nx index pdf --monitor` shows per-chunk tqdm progress bar during embedding

**Track B — MCP Enhancement:**
6. MCP `search` defaults to `"knowledge,code,docs"`, matching CLI behavior; `"all"` alias works
7. `collection_list` returns names + counts; `collection_info` returns metadata; `collection_verify` returns health status — each with unit tests in `test_mcp_server.py`

**Track C — Bug Fixes:**
8. Single-chunk CCE documents are indexed in the same vector space as multi-chunk CCE documents (no voyage-4 fallback)
9. `_prune_deleted_files`, `_prune_misclassified`, `_run_index_frecency_only` all paginate `col.get()` at 300 records
10. Mixed-model CCE batches never stored — partial failure re-embeds entire document consistently
11. MCP collection cache is thread-safe

**Track D — Documentation:**
12. `nx/skills/nexus/reference.md` documents all new MCP tools
13. `docs/cli-reference.md` documents `reindex`, enhanced `verify --deep`, per-chunk progress
14. `CLAUDE.md` notes single-chunk CCE handling in collection conventions

## Out of Scope

- Automatic re-indexing of all existing CCE collections (operational, not code)
- Voyage AI model compatibility matrix (external dependency)
- Hybrid search for MCP (CLI-only for now, per RDR-026)
- Updating closed/historical RDRs — they are permanent record of decisions at the time
