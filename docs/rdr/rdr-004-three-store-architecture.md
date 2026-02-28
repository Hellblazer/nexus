---
title: "Four-Store T3 Architecture"
id: RDR-004
type: architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-02-27
accepted_date:
close_date:
close_reason:
supersedes: RDR-003
related_issues: []
---

# RDR-004: Four-Store T3 Architecture

## Summary

Replace T3's single ChromaDB instance with four dedicated stores — one per content type: **code**, **docs**, **rdr**, **knowledge**. Three stores are repo-scoped (collection name = `_repo_identity()` output); one store (**knowledge**) is user-global with flat user-chosen collection names. Within a single-type store there is nothing to disambiguate, so the entire prefix-resolution problem class is eliminated. `resolve_corpus()` survives in simplified form for the code store only (where hash suffixes still need resolution). `t3_collection_name()` and `knowledge__` prefix injection are removed entirely.

This RDR supersedes RDR-003, which attempted prefix-based resolution inside a single mixed store and was BLOCKED after five gate rounds.

## Problem Statement

T3 uses one ChromaDB instance for all content types. Collection names encode type via prefix (`code__`, `knowledge__`). This creates three problems:

**P1 — Prefix disambiguation**: `code` must resolve to `code__nexus-8c2e74c0`. Multiple collections share the prefix; the correct one must be found at query time. Five gate rounds on RDR-003 could not produce a clean algorithm — exception-handling constraints and contract-preservation requirements interlock in ways that resist solution.

**P2 — Cross-type noise**: `list_collections()` returns all types. Every command that wants one type must filter by prefix to avoid acting on another.

**P3 — Implicit type model**: Type lives in the collection name, not in the store topology. There is no mechanism to ensure `knowledge__` collections are only queried by knowledge-type commands; a code query can accidentally hit them.

The root cause is not the resolution algorithm — it is the single-store design.

## Research Findings

T2 finding keys: `004-research-001` through `004-research-005` (project: `nexus_rdr`).

### RDR-003 Gate History (Verified — source-code confirmed by deep-analyst)

Five consecutive BLOCKED gate rounds. Each round fixed identified issues and revealed new interlocking ones:

| Round | Critical issues | Root conflict |
|-------|----------------|---------------|
| Gate 1 | 3 | Missing `resolve_collection_name()` function; Step 4 underspecified |
| Re-gate 2 | 2 | `doc_id` ordering constraint; audit table incomplete |
| Re-gate 3 | 3 | `list_store()` swallows `_ChromaNotFoundError` before resolution fires |
| Re-gate 4 | 2 | `resolve_corpus()` must return `[]` not raise; `info_cmd` line-31 guard bypasses fix |
| Re-gate 5 | 2 | `list_cmd` has no name arg; `promote_cmd` missing `t3_collection_name()` |

### Deep-Analyst Source Findings (Verified)

Key invariants confirmed from actual source code (`src/nexus/db/t3.py`, `src/nexus/corpus.py`, `src/nexus/commands/`):

- `list_store()` returns `[]` on miss (catches `_ChromaNotFoundError`) — existing except block must be replaced, not wrapped, for any resolution logic to fire
- `put()` computes `doc_id = sha256(f"{collection}:{title}")[:16]` at line 117, before `get_or_create_collection()` at line 142 — resolution must precede `put()` at the call site
- `resolve_corpus()` returns `[]` on zero matches (never raises) — `search_cmd.py` handles this gracefully and this contract must be preserved
- `info_cmd` in `collection.py` has an exact-match guard at line 31 that raises before any `T3Database`-layer fix can help — the fix must be at the command layer
- `list_cmd` in `collection.py` takes no name argument — it lists all collections, no resolution applicable
- `promote_cmd` in `memory.py` passes raw `--collection` arg to `t3.put()` with no `t3_collection_name()` wrapping — pre-existing inconsistency

### Current T3 Surface (Verified)

| Component | File | Role |
|-----------|------|------|
| `T3Database` | `src/nexus/db/t3.py` | Single-store client wrapper |
| `t3_collection_name()` | `src/nexus/corpus.py` | Injects `knowledge__` prefix for bare names |
| `resolve_corpus()` | `src/nexus/corpus.py` | Prefix-based collection resolution |
| `_t3()` | `commands/*.py` | Factory for the single store |
| `list_store()` | `src/nexus/db/t3.py` | Returns `[]` on `_ChromaNotFoundError` |
| `put()` | `src/nexus/db/t3.py` | Computes `doc_id` before `get_or_create_collection()` |

### YAGNI Validation (Verified)

Collection types actually in use:
- `code__*` — source indexing (`nx index`)
- `knowledge__*` — default store target (`nx store`, `nx memory promote`)

No `docs__*` or `rdr__*` collections exist in code today; `knowledge__*` are actively used via `nx store`. Four stores covers all present usage. Additional stores can be added if a fifth type materialises (YAGNI applies from here).

## Design

### Four Stores

| Store | Config key | Default path | Collection naming | Scope | Primary commands |
|-------|-----------|--------------|-------------------|-------|-----------------|
| **code** | `chroma_code_path` | `~/.config/nexus/chroma_code/` | `{lang}__{repo-identity}` | repo | `nx index`, `nx search --type code` |
| **docs** | `chroma_docs_path` | `~/.config/nexus/chroma_docs/` | `{repo-identity}` / `null` | repo | `nx store --type docs`, `nx search --type docs` |
| **rdr** | `chroma_rdr_path` | `~/.config/nexus/chroma_rdr/` | `{repo-identity}` / `null` | repo | `nx rdr store`, `nx search --type rdr` |
| **knowledge** | `chroma_knowledge_path` | `~/.config/nexus/chroma_knowledge/` | flat user name | global | `nx store`, `nx search --type knowledge` |

The three repo-scoped stores use the existing `_repo_identity()` function (SHA-256 of the repo filesystem path, first 8 hex digits, prefixed with the repo name — e.g., `nexus-8c2e74c0`). The store path encodes the type; the collection name encodes only the repo. The `{lang}__` prefix is retained for code because it is meaningful (distinguishes `python__nexus-8c2e74c0` from `javascript__nexus-8c2e74c0` for polyglot repos). Docs and rdr have no lang-equivalent. When no git repo is present, the sentinel `null` is used. One collection per repo per store; all RDRs (or docs) for a repo are documents within that collection, distinguished by metadata.

The knowledge store is user-global: collections have flat user-chosen names (e.g., `llm-papers`, `meeting-notes`). No repo scoping — this is the successor to `knowledge__*` in the single store. `nx store` without `--type` defaults to knowledge (preserving current behaviour).

`T3Database` is unchanged — three instances are constructed from three paths. No class hierarchy needed; the store is selected at the call site.

### Store Factories

```python
# src/nexus/db/t3_stores.py
def t3_code() -> T3Database:
    return T3Database(config().chroma_code_path)

def t3_docs() -> T3Database:
    return T3Database(config().chroma_docs_path)

def t3_rdr() -> T3Database:
    return T3Database(config().chroma_rdr_path)

def t3_knowledge() -> T3Database:
    return T3Database(config().chroma_knowledge_path)
```

No dynamic dispatch, no prefix parsing. The store is selected at the call site based on command context.

### Collection Naming Per Store

**Code store**: Naming unchanged — `{lang}__{repo-hash8}`. The lang prefix distinguishes `python__nexus-8c2e74c0` from `javascript__nexus-8c2e74c0` for polyglot repos. `resolve_corpus()` still applies here; `list_collections()` on the code store returns only code collections.

**Docs store**: `{repo-identity}` (e.g., `nexus-8c2e74c0`), computed via `_repo_identity()`. One collection per repo containing all documentation for that repo. For docs not in a repo: collection name `null`. `nx store --type docs` targets this store.

**RDR store**: `{repo-identity}` (e.g., `nexus-8c2e74c0`), computed via `_repo_identity()`. One collection per repo containing all RDRs for that repo as documents, with `rdr_id` and `status` in metadata. For RDRs without a repo: collection name `null`. `_repo_identity()` is reused without modification.

**Knowledge store**: flat user-chosen collection name (e.g., `llm-papers`, `meeting-notes`). No repo scoping — knowledge is user-global. `nx store` without `--type` defaults here, preserving current behaviour. `t3_collection_name()` is removed; callers pass the bare name directly to `t3_knowledge()`.

### search Routing

`nx search` gains a `--type` flag:

```
nx search "query" --type code         # code store only
nx search "query" --type docs         # docs store only
nx search "query" --type rdr          # rdr store only
nx search "query" --type knowledge    # knowledge store only
nx search "query"                     # all four stores, results merged by score
```

Multi-store fan-out queries each store and merges results. Latency cost is negligible — ChromaDB query latency is dominated by embedding generation, not store count.

### Removal of t3_collection_name() and knowledge__ Prefix

`t3_collection_name()` existed to prevent bare names from colliding with `code__` collections in the single store. With separate stores, this is unnecessary: the docs store contains no code collections. The function is deleted; all callers pass bare names to `t3_docs()` directly.

`resolve_corpus()` is retained for the code store. For docs and rdr stores, exact match suffices — collections have stable, user-chosen names without hash suffixes.

### Migration of Existing Data

One-time migration via `nx migrate t3`:
1. Read existing single store at `chroma_path` (preserved as deprecated config alias for `chroma_knowledge_path`)
2. Copy `code__*` collections → code store, names unchanged
3. Copy `knowledge__*` collections → knowledge store, strip `knowledge__` prefix; collection name becomes the bare user name (e.g., `knowledge__llm-papers` → `llm-papers`)
4. Docs store and rdr store start empty — no legacy collections exist for either type; tooling populates them going forward
5. Verify document counts match before and after
6. Print migration report; do not delete source store (user destroys manually after verification)

## Trade-offs

| Dimension | Single store | Four stores |
|-----------|-------------|------------|
| Prefix resolution complexity | High (fatal for RDR-003) | Eliminated for docs/rdr/knowledge; simplified for code |
| Cross-type search default | Yes (one query) | Fan-out to 4 stores; negligible latency cost |
| Config surface | One path | Four paths (migration adds deprecated alias) |
| Resource usage | One ChromaDB instance | Four instances (lightweight PersistentClient) |
| Type isolation | None (enforced by naming convention) | Complete (enforced by store boundary) |
| Migration effort | N/A | One-time; non-destructive |
| `resolve_corpus()` scope | All collection types | Code store only |
| `nx store` default target | `knowledge__*` in single store | knowledge store (behaviour preserved) |

## Implementation Plan

### Phase 1 — Config and Store Factories
1. Add `chroma_code_path`, `chroma_docs_path`, `chroma_rdr_path`, `chroma_knowledge_path` to config schema in `src/nexus/config.py`. Default: `~/.config/nexus/chroma_{type}/`. Add `chroma_path` as deprecated alias for `chroma_knowledge_path` with deprecation warning on read (knowledge is the direct successor to the old single store).
2. Create `src/nexus/db/t3_stores.py` with `t3_code()`, `t3_docs()`, `t3_rdr()`, `t3_knowledge()` factories.

### Phase 2 — Command Routing
3. `commands/store.py`: `put_cmd` and `list_cmd` default to `t3_knowledge()`; add `--type` flag to route to docs, rdr, or knowledge.
4. `commands/collection.py`: `_t3()` → `t3_knowledge()` for default; add `--type` flag routing to all four.
5. `commands/search_cmd.py`: add `--type` flag; default queries all 4 stores and merges results.
6. `commands/memory.py` (`promote_cmd`): call `t3_knowledge()`, pass bare collection name (no `t3_collection_name()` wrapping).
7. Code indexing entry point: use `t3_code()`.

### Phase 3 — Cleanup
8. Delete `t3_collection_name()` from `src/nexus/corpus.py`. Update all callers.
9. Remove `knowledge__` prefix injection everywhere.
10. Restrict `resolve_corpus()` callers to code-store paths only. Simplify docs/rdr call sites to direct `get_collection()`.

### Phase 4 — Migration
11. Implement `nx migrate t3` subcommand: non-destructive copy with count verification and report.
12. Update documentation: `docs/architecture.md` T3 section, `docs/cli-reference.md` for new `--type` flag and `nx migrate t3`.

## Test Plan

- P1: `t3_code()`, `t3_docs()`, `t3_rdr()`, `t3_knowledge()` each connect to the correct configured path.
- P2: `nx store put "doc"` (no `--type`) → item appears in knowledge store; absent from code, docs, and rdr stores.
- P3: `nx store put "doc" --type docs` → item appears in docs store; absent from knowledge store.
- P4: `nx index` → collection created in code store; absent from all other stores.
- P5: `nx search "query"` (no `--type`) → results from all 4 stores, merged.
- P6: `nx search "query" --type code` → only code store queried (verify via store access log or mock).
- P7: `nx migrate t3` → `knowledge__llm-papers` in old store becomes `llm-papers` in knowledge store; `code__nexus-abc12345` appears unchanged in code store; counts match.
- P8: `info_cmd` on knowledge store collection — `get_collection(name)` directly, no `list_collections()` + guard needed; raises `ClickException` on `_ChromaNotFoundError` cleanly.
- P9: `promote_cmd` with bare `--collection notes` → stored in knowledge store as `notes`, no `knowledge__` prefix.

## Open Questions

- CloudClient mode: does ChromaDB Cloud support multiple databases per account, or do we use separate projects? Verify before Phase 1 config design.
- Should `--type` on `nx search` accept a comma-separated list (`--type code,docs`)? Default is all-three fan-out; defer until a use case materialises.

## Revision History

### Gate 1 (2026-02-27) — BLOCKED

**3 critical, 7 significant, 6 observations.**

#### Critical — Must Fix

**C1. Store factory signature wrong — T3Database uses CloudClient, not path-based constructor.**
The proposed factories call `T3Database(config().chroma_code_path)`. The actual `T3Database.__init__` signature is `__init__(self, tenant, database, api_key, voyage_api_key, ...)` — it wraps `chromadb.CloudClient`, not `PersistentClient`. The entire "four paths" premise depends on path-based construction that does not exist. Resolution requires answering: do the four new stores use local PersistentClient (new capability) or separate CloudClient databases? Either answer has large implementation consequences that must be specified in the design before the factory code is written.

**C2. `expire()` hardcodes `knowledge__` prefix — silently broken post-migration.**
`T3Database.expire()` skips any collection whose name does not start with `"knowledge__"`. Under the new design, knowledge store collections have bare user-chosen names (`llm-papers`, not `knowledge__llm-papers`). Post-migration, `expire()` matches zero collections and silently expires nothing. TTL enforcement breaks completely with no error. Must be addressed in Phase 3 cleanup — the updated `expire()` for the knowledge store should operate on all collections in that store instance without prefix filtering.

**C3. `index_model_for_collection()` dispatches on collection name prefix — wrong embedding model for docs/rdr stores.**
`index_model_for_collection()` in `corpus.py` selects the embedding model (`voyage-code-3`, `voyage-context-3`, `voyage-4`) by checking if the collection name starts with `code__`, `docs__`, `knowledge__`, or `rdr__`. Under the new naming, docs and rdr store collections are bare `{repo-identity}` strings (e.g., `nexus-8c2e74c0`) — no prefix. They fall through to the `voyage-4` default instead of `voyage-context-3`. All docs and rdr content is indexed with the wrong model; embedding spaces are corrupted silently. This function must be refactored: since store type is now encoded in which factory is used rather than in the collection name, it needs to accept a store-type parameter or be replaced with per-store constants.

#### Significant — Must Fix

**S1. File named `rdr-004-three-store-architecture.md`; Open Questions says "all-three fan-out".** Rename the file and correct the Open Questions section to say "all-four fan-out."

**S2. `T3Database` claimed "unchanged" — false.** Beyond `expire()` (C2), `collection_metadata()` calls `index_model_for_collection()` using the collection name (C3 applies here too). The docstring at line 21 documents the `code__/docs__/knowledge__/rdr__` namespace conventions — all become inaccurate for the new naming. Replace "unchanged" with a precise list of what stays the same and what must be updated.

**S3. `delete_cmd` and `verify_cmd` in `collection.py` not addressed in the plan.** The plan routes `_t3()` → `t3_knowledge()` for `collection.py` but is silent on `delete_cmd` and `verify_cmd`. Without `--type` routing, `nx collection delete` and `nx collection verify` would always target the knowledge store and fail for code/docs/rdr collections. Add explicit routing for all four `collection` subcommands.

**S4. YAGNI claim wrong — `docs__*` and `rdr__*` collections already exist.** `registry.py` already has `_docs_collection_name()` and `_rdr_collection_name()`, used by `indexer.py`. Any user who has run `nx index repo` has `docs__` and `rdr__` collections in their current single store. Migration steps 2-4 only migrate `code__*` and `knowledge__*`; `docs__*` and `rdr__*` are silently dropped. Correct the YAGNI claim and add migration steps for all four prefix types.

**S5. `--corpus` vs `--type` flag conflict on `search_cmd`.** The existing `search_cmd` uses `--corpus` for collection selection via `resolve_corpus()`. RDR-004 adds `--type` for store routing. These two mechanisms overlap — the RDR does not specify what happens when both are provided, whether `--corpus` is deprecated, or how the all-stores default fan-out interacts with `resolve_corpus()`. Specify the interaction explicitly.

**S6. `promote_cmd` directly constructs `T3Database` — factory refactor must be stated explicitly for testability.** The current `promote_cmd` bypasses `make_t3()` entirely. The plan says "call `t3_knowledge()`" but does not acknowledge the direct construction or specify that `t3_knowledge()` must support `_client`/`_ef_override` injection for unit testing. P9 would otherwise require real CloudClient credentials to run.

**S7. `null` sentinel for no-git-repo case is an unresolved design gap for CloudClient mode.** If CloudClient databases are shared, two users without git repos both write to a collection named `null` in the same database. CloudClient Open Question (already noted) must be answered first; the `null` sentinel design depends on the answer.

#### Observations

- O1: `--type` and `--corpus` are two overlapping control surfaces; consider retiring `--corpus` explicitly
- O2: `_prune_deleted_files()` and `_prune_misclassified()` in `indexer.py` take `code_collection` and `docs_collection` names via `make_t3()` — must use `t3_code()` / `t3_docs()` after refactor; not addressed
- O3: Phases 2 and 3 must be deployed atomically — deploying Phase 2 without Phase 3 leaves `t3_collection_name()` injecting `knowledge__` prefixes into a store without prefixed collections
- O4: P1 test says "connect to the correct configured path" — if CloudClient, this must verify `(tenant, database)` pair, not path
- O5: Migration verification step 5 should explicitly state the invariant: sum of counts across all four new stores = sum across all old store collections
- O6: `nx rdr store` referenced in the Four Stores table does not exist; `nx index rdr` exists but is different
