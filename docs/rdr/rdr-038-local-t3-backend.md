---
title: "Local T3 Backend"
id: RDR-038
type: architecture
status: draft
priority: P1
author: Hal Hildebrand
created: 2026-03-14
related_issues:
  - "RDR-004 - Four Store Architecture"
  - "RDR-005 - ChromaDB Cloud Quota Enforcement"
  - "RDR-037 - T3 Database Consolidation"
---

# RDR-038: Local T3 Backend

## Problem Statement

T3 currently requires ChromaDB Cloud + Voyage AI API keys — both cloud services with signup friction. Even free tiers create a barrier: users must create accounts, generate API keys, and configure credentials before `nx search` works. This makes nexus harder to adopt for developers who just want local semantic search over their repos.

The ideal first-run experience is `pip install conexus && nx index repo . && nx search "query"` — zero configuration, zero API keys, instant value. Cloud T3 should remain available for users who want higher-quality embeddings and shared knowledge, but a local backend should be the default for new installations.

## Findings

### F-01: ChromaDB PersistentClient is API-compatible with CloudClient

ChromaDB's `PersistentClient(path="...")` exposes the same `get_or_create_collection`, `list_collections`, `get_collection`, `delete_collection` methods as `CloudClient`. All T3Database operations (`upsert`, `query`, `get`, `delete`) work unchanged. Storage is SQLite + HNSW index files on local disk.

- **Detail**: Verified against chromadb 1.5.5 — `upsert`, `query`, `get` with `where` filters, `list_collections`, pagination all work identically. The only behavioral difference is that PersistentClient has no per-operation quota limits (CloudClient caps at 300 records per write, 300 query results). Concurrent access is SQLite WAL mode — single writer, multiple readers.

### F-02: ChromaDB bundles an ONNX embedding model (zero new dependencies)

ChromaDB ships `ONNXMiniLM_L6_V2` (`all-MiniLM-L6-v2`, 384 dimensions) which downloads an 83 MB ONNX model to `~/.cache/chroma/onnx_models/` on first use. No additional pip install required. This is the true zero-config option.

- **Detail**: Benchmarked at ~113 docs/sec on Apple Silicon CPU. MTEB average ~56. Weak on code retrieval but functional for prose/docs/knowledge. Already used by the `local_t3` test fixture.

### F-03: fastembed provides better local embeddings without PyTorch

`fastembed` (108 KB wheel) uses ONNX Runtime for inference — no PyTorch (~700 MB) required. Models download to `~/.cache/fastembed/` on first use.

- **Detail**: `BAAI/bge-base-en-v1.5` (210 MB, 768d, MTEB 63.55) is the best general-purpose option. `jinaai/jina-embeddings-v2-base-code` (640 MB, 768d, 30 programming languages) is best for code-heavy repos. Both are Apache 2.0 licensed.

### F-04: Voyage CCE cannot be replicated locally

Voyage AI's Contextualized Chunk Embedding (CCE) propagates cross-chunk context during embedding — a proprietary API feature. Local models embed each chunk independently. Quality gap is real but acceptable for the local tier.

- **Detail**: CCE is used for `docs__*`, `rdr__*`, and `knowledge__*` collections. Local mode uses standard per-chunk embedding. This is the primary quality difference between local and cloud tiers.

### F-05: Embedding dimensions are incompatible between modes

Cloud (Voyage): 1024d. Local bge-base/jina: 768d. Local MiniLM: 384d. Collections are bound to their embedding dimension at creation — you cannot query a 1024d collection with a 768d vector.

- **Detail**: Switching between local and cloud modes requires re-indexing. Code/docs/rdr collections are derived from repo files and can be regenerated. Only `knowledge__*` entries (user-created via `nx store put`) are non-rederivable and need export/import.

### F-06: Auto-detection eliminates configuration for both modes

No API keys present → local mode. API keys present → cloud mode. Users who `pip install conexus` without configuring anything get local mode automatically. Users who run `nx config init` and provide keys get cloud mode. No explicit mode flag required.

## Proposed Design

### Three embedding tiers

| Tier | Model | Package | Download | Dims | Quality | When |
|------|-------|---------|----------|------|---------|------|
| 0 (zero-config) | `ONNXMiniLM_L6_V2` | bundled with chromadb | 83 MB | 384 | ~56 MTEB | Default — no extra deps |
| 1 (recommended) | `bge-base-en-v1.5` | `fastembed` | 210 MB | 768 | 63.55 MTEB | `pip install conexus[local]` |
| 2 (code-heavy) | `jina-embeddings-v2-base-code` | `fastembed` | 640 MB | 768 | Code-optimized | Opt-in via config |

All tiers use a single model for all collection types (code, docs, rdr, knowledge) — no per-prefix model splitting in local mode.

### Mode auto-detection

```python
def is_local_mode() -> bool:
    if os.environ.get("NX_LOCAL", "").lower() in ("1", "true"):
        return True
    # No cloud credentials → local mode
    return not get_credential("voyage_api_key") and not get_credential("chroma_api_key")
```

### T3Database changes

Add `local_mode` and `local_path` parameters to `__init__`. When `local_mode=True`:
- Use `chromadb.PersistentClient(path=local_path)` instead of `CloudClient`
- Skip the old-layout probe (`OldLayoutDetected`)
- Skip Voyage AI client initialization
- Skip CloudClient quota enforcement (`QUOTAS` validation, `MAX_QUERY_RESULTS` clamping)
- Use `LocalEmbeddingFunction` instead of `VoyageAIEmbeddingFunction`
- Skip CCE path in `put()` and `search()` — use the local EF directly

### Local storage path

`~/.local/share/nexus/chroma` (XDG data home). Keeps multi-GB index data separate from config in `~/.config/nexus/`.

### make_t3() factory changes

```python
def make_t3(*, _client=None, _ef_override=None) -> T3Database:
    if is_local_mode():
        return T3Database(local_mode=True, _ef_override=_ef_override)
    else:
        # existing cloud path
        return T3Database(tenant=..., database=..., ...)
```

### New optional dependency

```toml
[project.optional-dependencies]
local = ["fastembed>=0.7.0"]
```

`pip install conexus` → tier 0 (bundled MiniLM).
`pip install conexus[local]` → tier 1 (fastembed + bge-base).

### Config additions

```yaml
# ~/.config/nexus/config.yml  (or env vars)
local_mode: true               # NX_LOCAL — explicit override
local_chroma_path: /path/to   # NX_LOCAL_CHROMA_PATH — default: ~/.local/share/nexus/chroma
local_embed_model: BAAI/bge-base-en-v1.5  # NX_LOCAL_EMBED_MODEL
local_embed_tier: 1            # NX_LOCAL_EMBED_TIER — 0, 1, or 2
```

### Mode switching and migration

Switching modes requires re-indexing because embedding dimensions differ. Provide:
- `nx migrate to-local` — re-index all tracked sources with local embeddings
- `nx migrate to-cloud` — re-index all tracked sources with Voyage AI
- Knowledge entries: `nx store export --all` → switch mode → `nx store import`

### nx doctor changes

Add local mode health checks:
- Local ChromaDB path exists and is writable
- Embedding model downloaded
- Collection count and disk usage
- No cloud credential checks when in local mode

## Changes Required

*Updated after live validation (2026-03-14). Original estimates corrected per code impact analysis.*

**Source — core (4 files):**
1. `db/t3.py` — add `local_mode`/`local_path` to `__init__`, gate quota clamping (`MAX_QUERY_RESULTS`) on mode
2. `db/__init__.py` — update `make_t3()` with `is_local_mode()` auto-detection
3. `db/local_ef.py` — new: `LocalEmbeddingFunction` wrapper (tier 0/1/2)
4. `config.py` — add local mode credentials/settings

**Source — indexer pipeline (4 files, more complex than initially estimated):**
5. `index_context.py` — add optional `embed_fn` field for local mode injection
6. `code_indexer.py` — replace `ctx.voyage_client.embed()` with injected `embed_fn`
7. `doc_indexer.py` — extend `embed_fn` injection to all callers of `_embed_with_fallback()`
8. `indexer.py` — create local `embed_fn` when local mode, pass through `IndexContext`

**Source — commands (3 files):**
9. `commands/store.py` — skip cloud credential checks in local mode
10. `commands/memory.py` — refactor `promote` to use `make_t3()` instead of direct `T3Database` construction
11. `commands/doctor.py` — local mode health checks, skip cloud credential checks

**Source — scoring + corpus (2 files):**
12. `scoring.py` — gracefully skip Voyage AI reranking in local mode (distance-sorted fallback)
13. `corpus.py` — `embedding_model_for_collection()` returns local model name in local mode

**Tests (~4 files):**
14. `test_t3.py` — add local mode tests (PersistentClient path, quota bypass)
15. `test_local_ef.py` — new: embedding function tier tests
16. `test_e2e.py` — verify local mode e2e with PersistentClient
17. `test_indexer_e2e.py` — verify local indexing pipeline end-to-end

**Docs (~4 files):**
18. `docs/getting-started.md` — local-first setup flow
19. `docs/configuration.md` — local mode config reference
20. `docs/storage-tiers.md` — local vs cloud T3 comparison
21. `CLAUDE.md` — local mode overview

### Validation notes (2026-03-14)

All findings validated by live experiments:
- **F-01 CONFIRMED**: PersistentClient passed all 18 T3Database method tests, including batch >300 and `get` with embeddings
- **F-02 CONFIRMED**: ONNX MiniLM measured at 117-123 chunks/sec (Apple Silicon), 384d
- **F-05 CONFIRMED**: 384d/768d/1024d dimension incompatibility measured
- **Quota note**: `MAX_QUERY_RESULTS=300` clamping is actively harmful locally — must be gated on `local_mode`
- **Scoring gap**: `scoring.py` uses Voyage AI rerank API (omitted from initial design, corrected above)

## Performance Comparison

| Metric | Cloud (Voyage) | Local Tier 0 | Local Tier 1 | Local Tier 2 |
|--------|---------------|-------------|-------------|-------------|
| Text quality (MTEB) | ~68+ | ~56 | 63.55 | N/A |
| Code retrieval | excellent | weak | passable | good |
| CCE context | yes | no | no | no |
| Max tokens/chunk | 16K | 256 | 512 | 8192 |
| Embedding dims | 1024d | 384d | 768d | 768d |
| Index throughput | ~1-2/sec (API) | ~120/sec (measured) | ~80/sec (est.) | ~50/sec (est.) |
| Query latency | 100-500ms | 5-20ms | 5-20ms | 10-30ms |
| First-use download | 0 MB | 83 MB | 210 MB | 640 MB |
| Ongoing cost | API pricing | $0 | $0 | $0 |

Local mode is 50-100x faster to index (no network) and 10-50x faster to query. Quality is lower but sufficient for personal dev tooling.

## Rejected Alternatives

### Alternative vector databases (LanceDB, Qdrant, sqlite-vec, FAISS)

All require complete T3Database API rewrites. ChromaDB PersistentClient is API-identical to CloudClient — zero new code paths for CRUD operations. The marginal performance or feature benefits don't justify the rewrite cost.

### sentence-transformers (PyTorch-based)

Requires PyTorch (~700 MB download). Disqualified for a zero-config CLI tool. fastembed with ONNX Runtime achieves similar quality without the dependency weight.

### Different local models per collection type

Using separate code and text models locally would add 500+ MB of total download for marginal benefit. A single good general-purpose model (bge-base or jina-code) handles both adequately for the local tier.

### GPU-accelerated inference

Requiring CUDA/Metal adds setup complexity that contradicts the zero-config goal. CPU inference at 50-113 chunks/sec is fast enough — a 10,000-chunk repo indexes in ~2 minutes on CPU.
