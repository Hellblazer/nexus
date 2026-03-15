---
title: "Local T3 Backend"
id: RDR-038
type: architecture
status: closed
closed_date: 2026-03-15
close_reason: implemented
priority: P1
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-14
accepted_date: 2026-03-14
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

- **Detail**: Verified against chromadb 1.5.5 — all 18 T3Database public methods pass. The only behavioral difference is that PersistentClient has no per-operation quota limits (CloudClient caps at 300 records per write, 300 query results). Concurrent access is SQLite WAL mode — single writer, multiple readers. Concurrent writers serialize at the SQLite level (no data corruption), but `database is locked` errors may surface under contention.

### F-02: ChromaDB bundles an ONNX embedding model (zero new dependencies)

ChromaDB ships `ONNXMiniLM_L6_V2` (`all-MiniLM-L6-v2`, 384 dimensions) which downloads an 83 MB ONNX model to `~/.cache/chroma/onnx_models/` on first use. No additional pip install required. This is the true zero-config option.

- **Detail**: Measured at 117-123 chunks/sec on Apple Silicon CPU. MTEB average ~56. Weak on code retrieval but functional for prose/docs/knowledge. Already used by the `local_t3` test fixture. Max token length: 256 tokens.

### F-03: fastembed provides better local embeddings without PyTorch

`fastembed` (108 KB wheel) uses ONNX Runtime for inference — no PyTorch (~700 MB) required. Models download to `~/.cache/fastembed/` on first use.

- **Detail**: `BAAI/bge-base-en-v1.5` (210 MB, 768d, MTEB 63.55) is the best general-purpose option. `jinaai/jina-embeddings-v2-base-code` (640 MB, 768d, 30 programming languages) is best for code-heavy repos. Both are Apache 2.0 licensed. Not yet validated in situ (published MTEB benchmarks credible).

### F-04: Voyage CCE cannot be replicated locally

Voyage AI's Contextualized Chunk Embedding (CCE) propagates cross-chunk context during embedding — a proprietary API feature. Local models embed each chunk independently. Quality gap is real but acceptable for the local tier.

- **Detail**: CCE is used for `docs__*`, `rdr__*`, and `knowledge__*` collections. Local mode uses standard per-chunk embedding. This is the primary quality difference between local and cloud tiers.

### F-05: Embedding dimensions are incompatible between modes

Cloud (Voyage): 1024d. Local bge-base/jina: 768d. Local MiniLM: 384d. Collections are bound to their embedding dimension at creation — you cannot query a 1024d collection with a 768d vector.

- **Detail**: Switching between local and cloud modes requires re-indexing. Code/docs/rdr collections are derived from repo files and can be regenerated. Only `knowledge__*` entries (user-created via `nx store put`) are non-rederivable and need export/import.

### F-06: Auto-detection eliminates configuration for both modes

No API keys present → local mode. API keys present → cloud mode. Users who `pip install conexus` without configuring anything get local mode automatically. Users who run `nx config init` and provide keys get cloud mode. No explicit mode flag required.

### F-07: Credential checks gate the indexer pipeline

`check_credentials()` in `indexer_utils.py` raises `CredentialsMissingError` when `voyage_api_key` or `chroma_api_key` is absent. Called unconditionally by `_run_index()` and `_run_index_frecency_only()`. In local mode both keys are absent by design — this check must be mode-aware or `nx index repo .` dies on arrival.

### F-08: Collection staleness detection depends on embedding model name

`check_staleness()` in `indexer_utils.py` compares stored `embedding_model` metadata against `corpus.py:embedding_model_for_collection()`. If `corpus.py` returns a cloud model name (`voyage-code-3`) but the collection was indexed locally (`bge-base-en-v1.5`), every file appears stale and triggers a full re-index on every run. `corpus.py` must return the active local model name when in local mode.

## Proposed Design

### Three embedding tiers

| Tier | Model | Package | Download | Dims | Quality | When |
|------|-------|---------|----------|------|---------|------|
| 0 (zero-config) | `ONNXMiniLM_L6_V2` | bundled with chromadb | 83 MB | 384 | ~56 MTEB | Default — no extra deps |
| 1 (recommended) | `bge-base-en-v1.5` | `fastembed` | 210 MB | 768 | 63.55 MTEB | `pip install conexus[local]` |
| 2 (code-heavy) | `jina-embeddings-v2-base-code` | `fastembed` | 640 MB | 768 | Code-optimized | Opt-in via config |

All tiers use a single model for all collection types (code, docs, rdr, knowledge) — no per-prefix model splitting in local mode.

**First-run notice**: When tier 0 is active (no fastembed installed), `nx index repo .` emits a one-time notice: *"Using basic embeddings (tier 0). For better code search quality: pip install conexus[local]"*. This prevents users from judging nexus quality on the weakest model without knowing a better option exists.

### Mode auto-detection

```python
def is_local_mode() -> bool:
    if os.environ.get("NX_LOCAL", "").lower() in ("1", "true"):
        return True
    # No cloud credentials → local mode
    return not get_credential("voyage_api_key") and not get_credential("chroma_api_key")
```

### T3Database.__init__ branch ordering

`local_mode` is the **first branch** in the conditional tree, short-circuiting before any cloud logic:

```python
def __init__(self, ..., local_mode: bool = False, local_path: str = "", ...):
    # ... field init (semaphores, locks, quota validator) ...

    if _client is not None:
        # Test injection (existing path, unchanged)
        self._client = _client
    elif local_mode:
        # LOCAL MODE — no cloud probe, no Voyage AI, no quotas
        path = local_path or _default_local_path()
        self._client = chromadb.PersistentClient(path=path)
        self._local_mode = True
        # Skip: OldLayoutDetected probe, voyage_client init
    else:
        # CLOUD MODE — existing probe-first init from RDR-037
        self._local_mode = False
        migrated = get_credential("migrated")
        # ... existing probe logic ...
```

The `self._local_mode` flag gates runtime behavior:
- `_write_batch()`: skip `QUOTAS` validation when local
- `search()`: skip `MAX_QUERY_RESULTS` clamping when local
- `list_store()`: skip `MAX_QUERY_RESULTS` clamping when local
- `put()`: use local EF instead of `_cce_embed()` when local
- `search()`: use `query_texts` (EF-driven) instead of `query_embeddings` (CCE) when local

### Local storage path

Default: `~/.local/share/nexus/chroma`. Respects `XDG_DATA_HOME` when set: `${XDG_DATA_HOME}/nexus/chroma`. On macOS this intentionally uses the XDG path (not `~/Library/Application Support`) for consistency with the existing `~/.config/nexus/` convention. Keeps multi-GB index data separate from config.

### make_t3() factory changes

```python
def make_t3(*, _client=None, _ef_override=None) -> T3Database:
    if is_local_mode():
        local_ef = _ef_override or LocalEmbeddingFunction()
        return T3Database(local_mode=True, _ef_override=local_ef)
    else:
        # existing cloud path unchanged
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
local_embed_model: BAAI/bge-base-en-v1.5  # NX_LOCAL_EMBED_MODEL — takes precedence over tier
```

**Config precedence**: `local_embed_model` (explicit model name) takes precedence when set. When absent, the tier is auto-selected: tier 1 if fastembed is installed, tier 0 otherwise. The `local_embed_tier` config key is removed — it was redundant with `local_embed_model` and created ambiguity.

### Credential and staleness gating

**Credential checks**: `check_credentials()` in `indexer_utils.py` must be mode-aware. In local mode, skip the cloud key check; instead verify that the local ChromaDB path is writable and the embedding model is available (fastembed installed for tier 1, or bundled ONNX for tier 0).

**Staleness detection**: `corpus.py:embedding_model_for_collection()` and `index_model_for_collection()` must return the active local model name when `is_local_mode()` is true. This ensures `check_staleness()` correctly skips unchanged files after a local index.

**Mode-switch re-index**: When a collection's stored `embedding_model` metadata doesn't match the current active model (e.g., `voyage-code-3` stored but `bge-base-en-v1.5` active), `check_staleness()` returns stale for all files — triggering a full re-index. This is the **correct** behavior (dimensions differ), but `nx index repo .` should emit a diagnostic: *"Collection was indexed with {stored_model}; re-indexing with {active_model}."*

### Pipeline version stamp

Local-mode collections use `PIPELINE_VERSION = "4"` (unchanged) but store an additional `embed_mode` metadata field on each collection: `"local"` or `"cloud"`. Combined with `embedding_model`, this unambiguously identifies how a collection was indexed. The `embed_mode` field is checked by `check_staleness()` alongside the model name.

### Scoring and reranking

`scoring.py:rerank_results()` calls Voyage AI's rerank API. In local mode, the reranker is unavailable. The guard lives in the **caller** (`search_cmd.py`), not in `rerank_results()` itself — this avoids adding a `config.py` dependency to `scoring.py`:

```python
# In search_cmd.py:
if not is_local_mode():
    results = rerank_results(query, results, ...)
# In local mode: skip reranking, return distance-sorted results
```

### Concurrent write handling

PersistentClient uses SQLite WAL mode. Concurrent writers serialize at the SQLite level — no data corruption, but `database is locked` errors may surface. The existing `_chroma_with_retry()` must be verified to handle SQLite lock errors as retryable (same pattern as cloud 503/504 errors). If not, add `sqlite3.OperationalError` to the retryable set.

### Mode switching and migration

Switching modes requires re-indexing because embedding dimensions differ. **No automatic migration command** — this follows the precedent set by RDR-037 (manual export/import). The documented procedure:

**Cloud → Local:**
1. `nx store export --all` (while still in cloud mode)
2. `unset CHROMA_API_KEY VOYAGE_API_KEY` (or `nx config set local_mode 1`)
3. `nx index repo .` (re-indexes all tracked repos with local embeddings)
4. `nx store import <file>` (restores knowledge entries)

**Local → Cloud:**
1. `nx store export --all` (while still in local mode)
2. `nx config init` (set cloud credentials)
3. `nx index repo .` (re-indexes with Voyage AI)
4. `nx store import <file>`

### nx doctor changes

Local mode health checks:
- Local ChromaDB path exists and is writable
- Embedding model available (tier 0 always, tier 1 if fastembed installed)
- Collection count and disk usage
- Skip cloud credential checks entirely
- Skip Voyage AI key check

Cloud credential checks only shown when cloud mode is active.

## Changes Required

*Updated after live validation + gate critique (2026-03-14).*

**Source — core (4 files):**
1. `db/t3.py` — add `local_mode`/`local_path` to `__init__` (first branch before cloud probe), add `self._local_mode` flag, gate `MAX_QUERY_RESULTS` clamping and CCE path on mode
2. `db/__init__.py` — update `make_t3()` with `is_local_mode()` auto-detection, create `LocalEmbeddingFunction` in local path
3. `db/local_ef.py` — new: `LocalEmbeddingFunction` wrapper (auto-select tier 0 or 1 based on fastembed availability)
4. `config.py` — add local mode credentials (`NX_LOCAL`, `NX_LOCAL_CHROMA_PATH`, `NX_LOCAL_EMBED_MODEL`), add `is_local_mode()` function

**Source — indexer pipeline (5 files):**
5. `indexer_utils.py` — gate `check_credentials()`: in local mode verify local path writable + embedding model available instead of cloud keys; emit mode-switch re-index diagnostic in `check_staleness()`
6. `index_context.py` — add optional `embed_fn: Callable` field for local mode injection
7. `code_indexer.py` — replace `ctx.voyage_client.embed()` with `ctx.embed_fn()` (injected)
8. `doc_indexer.py` — extend `embed_fn` injection to all callers of `_embed_with_fallback()`
9. `indexer.py` — create local `embed_fn` from `LocalEmbeddingFunction` when local mode, pass through `IndexContext`; emit first-run tier notice

**Source — commands (3 files):**
10. `commands/store.py` — skip cloud credential checks in local mode (`_t3()` guard)
11. `commands/memory.py` — refactor `promote_cmd`: replace direct `T3Database(...)` construction AND credential check block with `make_t3()`
12. `commands/doctor.py` — local mode health checks (path, model, disk), skip cloud checks

**Source — scoring + corpus (2 files):**
13. `scoring.py` — no changes needed (guard lives in caller)
14. `commands/search_cmd.py` — skip `rerank_results()` call when `is_local_mode()`
15. `corpus.py` — `embedding_model_for_collection()` and `index_model_for_collection()` return local model name when `is_local_mode()`; add `embed_mode` metadata field

**Tests (7 files):**
16. `test_t3.py` — local mode PersistentClient path, quota bypass, no probe
17. `test_local_ef.py` — new: embedding function tier auto-select, fastembed fallback to tier 0
18. `test_e2e.py` — local mode e2e (put, search, expire, list)
19. `test_indexer_e2e.py` — local indexing pipeline end-to-end
20. `test_local_credentials.py` — new: `is_local_mode()` detection, `check_credentials()` gating, mode-switch staleness diagnostic
21. `test_memory_cmd.py` — `promote` in local mode (no cloud credentials)
22. `test_staleness.py` — new: staleness round-trip (index locally → re-run → verify skip; cloud metadata → switch to local → verify re-index + diagnostic)

**Docs (4 files):**
23. `docs/getting-started.md` — local-first setup flow (pip install → index → search, no keys)
24. `docs/configuration.md` — local mode config reference, mode switching procedure
25. `docs/storage-tiers.md` — local vs cloud T3 comparison table
26. `CLAUDE.md` — local mode overview

### Validation notes (2026-03-14)

All findings validated by live experiments:
- **F-01 CONFIRMED**: PersistentClient passed all 18 T3Database method tests, including batch >300 and `get` with embeddings
- **F-02 CONFIRMED**: ONNX MiniLM measured at 117-123 chunks/sec (Apple Silicon), 384d
- **F-05 CONFIRMED**: 384d/768d/1024d dimension incompatibility measured
- **F-07 CONFIRMED**: `check_credentials()` raises `CredentialsMissingError` unconditionally — must be gated
- **F-08 CONFIRMED**: `corpus.py` returns cloud model names regardless of mode — staleness check fails

## Performance Comparison

| Metric | Cloud (Voyage) | Local Tier 0 | Local Tier 1 | Local Tier 2 |
|--------|---------------|-------------|-------------|-------------|
| Text quality (MTEB) | ~68+ | ~56 | 63.55 | N/A |
| Code retrieval | excellent | weak | passable | good |
| CCE context | yes | no | no | no |
| Reranking | Voyage AI rerank | distance only | distance only | distance only |
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

Requiring CUDA/Metal adds setup complexity that contradicts the zero-config goal. CPU inference at 50-120 chunks/sec is fast enough — a 10,000-chunk repo indexes in ~2 minutes on CPU.

### Automatic migration command (`nx migrate to-local/to-cloud`)

Rejected in favor of documented manual procedure (export → switch → re-index → import), following the precedent set by RDR-037. The export/import path already exists and is well-tested. An automatic command adds implementation complexity with minimal UX benefit — mode switching is a one-time operation.

### Separate `local_embed_tier` config key

Removed. Redundant with `local_embed_model` and created ambiguity about precedence. Tier is now auto-selected: tier 1 if fastembed is installed, tier 0 otherwise. Users who want a specific model set `local_embed_model` explicitly.
