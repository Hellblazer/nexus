---
title: "Three-Store T3 Architecture"
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

# RDR-004: Three-Store T3 Architecture

## Summary

Replace T3's single ChromaDB instance with three dedicated stores — one per content type: **code**, **docs**, **rdr**. Within a single-type store there is nothing to disambiguate, so the entire prefix-resolution problem class is eliminated. `resolve_corpus()` survives in simplified form for the code store only (where hash suffixes still need resolution). `t3_collection_name()` and `knowledge__` prefix injection are removed entirely.

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

No `docs__*` or `rdr__*` collections exist in code today. Three stores covers present usage with one slot for growth. Additional stores can be added if a fourth type materialises.

## Design

### Three Stores

| Store | Config key | Default path | Collection naming | Primary commands |
|-------|-----------|--------------|-------------------|-----------------|
| **code** | `chroma_code_path` | `~/.config/nexus/chroma_code/` | `{lang}__{repo-hash8}` | `nx index`, `nx search --type code` |
| **docs** | `chroma_docs_path` | `~/.config/nexus/chroma_docs/` | `{repo-hash8}` / `null` | `nx store`, `nx search --type docs` |
| **rdr** | `chroma_rdr_path` | `~/.config/nexus/chroma_rdr/` | `{repo-hash8}` / `null` | `nx rdr store`, `nx search --type rdr` |

Collection identity uses the existing `_repo_identity()` function throughout (SHA-256 of the repo filesystem path, first 8 hex digits, prefixed with the repo name — e.g., `nexus-8c2e74c0`). The store path encodes the type; the collection name needs only to encode the repo. The `{lang}__` prefix is retained for the code store because it is meaningful (distinguishes `python__nexus-8c2e74c0` from `javascript__nexus-8c2e74c0` for polyglot repos). For docs and rdr stores there is no lang-equivalent, so no prefix is needed. When no git repo is present, the sentinel `null` is used as the collection name. One collection per repo per store; all RDRs for a repo are documents within that collection, distinguished by `rdr_id` in metadata.

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
```

No dynamic dispatch, no prefix parsing. The store is selected at the call site based on command context.

### Collection Naming Per Store

**Code store**: Naming unchanged — `{lang}__{repo-hash8}`. The lang prefix distinguishes `python__nexus-8c2e74c0` from `javascript__nexus-8c2e74c0` for polyglot repos. `resolve_corpus()` still applies here; `list_collections()` on the code store returns only code collections.

**Docs store**: `{repo-identity}` (e.g., `nexus-8c2e74c0`), computed via `_repo_identity()`. One collection per repo containing all documentation for that repo. The store path (`chroma_docs/`) provides the type context. No `knowledge__` prefix; `t3_collection_name()` is removed. For standalone docs not in a repo: collection name `null`.

**RDR store**: `{repo-identity}` (e.g., `nexus-8c2e74c0`), computed via `_repo_identity()`. One collection per repo containing all RDRs for that repo as documents, with `rdr_id` and `status` in metadata. The store path (`chroma_rdr/`) provides the type context. For RDRs without a repo: collection name `null`. `_repo_identity()` is reused without modification.

### search Routing

`nx search` gains a `--type` flag:

```
nx search "query" --type code         # code store only
nx search "query" --type docs         # docs store only
nx search "query" --type rdr          # rdr store only
nx search "query"                     # all three stores, results merged by score
```

Multi-store fan-out queries each store and merges results. Latency cost is negligible — ChromaDB query latency is dominated by embedding generation, not store count.

### Removal of t3_collection_name() and knowledge__ Prefix

`t3_collection_name()` existed to prevent bare names from colliding with `code__` collections in the single store. With separate stores, this is unnecessary: the docs store contains no code collections. The function is deleted; all callers pass bare names to `t3_docs()` directly.

`resolve_corpus()` is retained for the code store. For docs and rdr stores, exact match suffices — collections have stable, user-chosen names without hash suffixes.

### Migration of Existing Data

One-time migration via `nx migrate t3`:
1. Read existing single store at `chroma_path` (preserved as deprecated config alias for `chroma_docs_path`)
2. Copy `code__*` collections → code store, names unchanged
3. Copy `knowledge__*` collections → docs store, renamed to `{repo-identity}` for the current repo or `null` for unscoped collections; strip `knowledge__` prefix
4. No existing `rdr__*` collections exist — rdr store starts empty; RDR tooling populates it going forward
5. Verify document counts match before and after
6. Print migration report; do not delete source store (user destroys manually after verification)

## Trade-offs

| Dimension | Single store | Three stores |
|-----------|-------------|--------------|
| Prefix resolution complexity | High (fatal for RDR-003) | Eliminated for docs/rdr; simplified for code |
| Cross-type search default | Yes (one query) | Fan-out to 3 stores; negligible latency cost |
| Config surface | One path | Three paths (migration adds deprecated alias) |
| Resource usage | One ChromaDB instance | Three instances (lightweight PersistentClient) |
| Type isolation | None (enforced by naming convention) | Complete (enforced by store boundary) |
| Migration effort | N/A | One-time; non-destructive |
| `resolve_corpus()` scope | All collection types | Code store only |

## Implementation Plan

### Phase 1 — Config and Store Factories
1. Add `chroma_code_path`, `chroma_docs_path`, `chroma_rdr_path` to config schema in `src/nexus/config.py`. Default: `~/.config/nexus/chroma_{type}/`. Add `chroma_path` as deprecated alias for `chroma_docs_path` with deprecation warning on read.
2. Create `src/nexus/db/t3_stores.py` with `t3_code()`, `t3_docs()`, `t3_rdr()` factories.

### Phase 2 — Command Routing
3. `commands/store.py`: `put_cmd` and `list_cmd` call `t3_docs()`.
4. `commands/collection.py`: `_t3()` → `t3_docs()` for default; add `--type` flag routing to all three.
5. `commands/search_cmd.py`: add `--type` flag; default queries all 3 and merges results.
6. `commands/memory.py` (`promote_cmd`): call `t3_docs()`, pass bare collection name (no `t3_collection_name()` wrapping).
7. Code indexing entry point: use `t3_code()`.

### Phase 3 — Cleanup
8. Delete `t3_collection_name()` from `src/nexus/corpus.py`. Update all callers.
9. Remove `knowledge__` prefix injection everywhere.
10. Restrict `resolve_corpus()` callers to code-store paths only. Simplify docs/rdr call sites to direct `get_collection()`.

### Phase 4 — Migration
11. Implement `nx migrate t3` subcommand: non-destructive copy with count verification and report.
12. Update documentation: `docs/architecture.md` T3 section, `docs/cli-reference.md` for new `--type` flag and `nx migrate t3`.

## Test Plan

- P1: `t3_code()`, `t3_docs()`, `t3_rdr()` each connect to the correct configured path.
- P2: `nx store put "doc"` → item appears in docs store; absent from code and rdr stores.
- P3: `nx index` → collection created in code store; absent from docs and rdr stores.
- P4: `nx search "query"` (no `--type`) → results from all 3 stores, merged.
- P5: `nx search "query" --type code` → only code store queried (verify via store access log or mock).
- P6: `nx migrate t3` → `knowledge__topic` in old store becomes `topic` in docs store; `code__nexus-abc12345` appears unchanged in code store; counts match.
- P7: `info_cmd` on docs store — `get_collection(name)` directly, no `list_collections()` + guard needed; raises `ClickException` on `_ChromaNotFoundError` cleanly.
- P8: `promote_cmd` with bare `--collection notes` → stored in docs store as `notes`, no `knowledge__` prefix.

## Open Questions

- CloudClient mode: does ChromaDB Cloud support multiple databases per account, or do we use separate projects? Verify before Phase 1 config design.
- Should `--type` on `nx search` accept a comma-separated list (`--type code,docs`)? Default is all-three fan-out; defer until a use case materialises.

## Revision History

_Gate reviews will be appended here._
