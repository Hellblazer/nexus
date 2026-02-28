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

Replace T3's single ChromaDB database with four dedicated local stores — one per content type: **code**, **docs**, **rdr**, **knowledge**. Each store is a `chromadb.PersistentClient` instance at a separate filesystem path, injected into `T3Database` via its existing `_client` parameter. `T3Database` itself is unchanged. Collection naming within each store is unchanged (`{type}__{basename}-{hash8}`), so `index_model_for_collection()`, `expire()`, `_docs_collection_name()`, and `_rdr_collection_name()` all remain correct without modification.

This RDR supersedes RDR-003, which attempted prefix-based resolution inside a single mixed store and was BLOCKED after five gate rounds.

## Problem Statement

T3 currently uses one ChromaDB instance for all content types. Collection names encode type via prefix (`code__`, `docs__`, `rdr__`, `knowledge__`). This creates two operational problems:

**P1 — Prefix disambiguation at query time**: A user who types `code` as a `--corpus` argument must have it resolved to `code__nexus-8c2e74c0`. When the single `list_collections()` call returns all types, the resolution algorithm must filter across all of them — this is what RDR-003 failed to clean up after five gate rounds.

**P2 — Cross-type noise**: Every `list_collections()` call returns all four types. Commands that want one type must filter the results; the filter logic (prefix matching against a mixed list) is the source of the complexity.

The root cause is sharing one ChromaDB instance across all types. Separate stores eliminate both problems at the source: `list_collections()` on any single store returns only collections of that type.

## Research Findings

T2 finding keys: `004-research-001` through `004-research-005` (project: `nexus_rdr`).

### T3Database Constructor and Injection Point (Verified)

`T3Database.__init__` signature (confirmed from `src/nexus/db/t3.py` lines 34–53):

```python
def __init__(
    self,
    tenant: str = "",
    database: str = "",
    api_key: str = "",
    voyage_api_key: str = "",
    *,
    _client=None,         # ← injection point for testing and alternative clients
    _ef_override=None,
) -> None:
    ...
    if _client is not None:
        self._client = _client
    else:
        self._client = chromadb.CloudClient(tenant=tenant, database=database, api_key=api_key)
```

The `_client` parameter accepts any ChromaDB client. Passing `chromadb.PersistentClient(path)` gives a fully functional local store — the same path already used in tests via `EphemeralClient`. No constructor changes are needed.

### Existing Collection Naming (Verified)

All four type prefixes already exist and are in active use:

| Prefix | Function | File |
|--------|----------|------|
| `code__` | `_collection_name(repo)` | `src/nexus/registry.py:64` |
| `docs__` | `_docs_collection_name(repo)` | `src/nexus/registry.py:76` |
| `rdr__` | `_rdr_collection_name(repo)` | `src/nexus/registry.py:85` |
| `knowledge__` | `t3_collection_name(user_arg)` | `src/nexus/corpus.py:57` |

`_repo_identity()` (registry.py:17) provides the `{basename}-{hash8}` suffix. Collection names are `{type}__{basename}-{hash8}` (e.g., `code__nexus-8c2e74c0`). These functions are unchanged by this design.

### index_model_for_collection() Already Handles All Four Types (Verified)

```python
def index_model_for_collection(collection_name: str) -> str:
    if collection_name.startswith("code__"):
        return "voyage-code-3"
    if collection_name.startswith(("docs__", "knowledge__", "rdr__")):
        return "voyage-context-3"
    return "voyage-4"
```

Since collection names retain their `{type}__` prefix in all four stores, this function requires no change.

### expire() Prefix Filter (Verified)

`T3Database.expire()` skips any collection that does not start with `knowledge__`:

```python
for col_or_name in self._client.list_collections():
    name = col_or_name if isinstance(col_or_name, str) else col_or_name.name
    if not name.startswith("knowledge__"):
        continue
```

When `expire()` is called on a `T3Database` instance backed by the knowledge store, every collection in that store starts with `knowledge__`, so the filter passes all of them — correct. The filter was written to protect code/docs/rdr collections from TTL expiry in the single-store design; in the four-store design the store boundary provides that protection instead. The filter is now redundant but not harmful. The docstring should be updated (see S2).

### RDR-003 Gate History (Verified)

Five consecutive BLOCKED gate rounds on a single-store prefix-resolution algorithm. The design's failure to converge is the primary motivator for the four-store approach. See RDR-003 `## Revision History` for full findings.

### YAGNI Validation (Corrected)

All four collection types are already in use today:
- `code__*` — `nx index` via `_collection_name()`
- `docs__*` — `nx index` via `_docs_collection_name()`
- `rdr__*` — `nx index` via `_rdr_collection_name()`
- `knowledge__*` — `nx store`, `nx memory promote` via `t3_collection_name()`

The previous draft's claim that "no `docs__*` or `rdr__*` collections exist in code today" was incorrect.

### ChromaDB PersistentClient Compatibility (Verified)

`chromadb.PersistentClient(path)` implements the same collection API as `CloudClient`. Injection via `_client=` is already the test path (tests use `EphemeralClient`). `PersistentClient` is the direct local equivalent.

## Design

### Four Stores

| Store | Config key | Default path | Collection naming | Primary commands |
|-------|-----------|--------------|-------------------|-----------------|
| **code** | `chroma_code_path` | `~/.config/nexus/chroma_code/` | `code__{basename}-{hash8}` | `nx index`, `nx search --type code` |
| **docs** | `chroma_docs_path` | `~/.config/nexus/chroma_docs/` | `docs__{basename}-{hash8}` | `nx index`, `nx search --type docs` |
| **rdr** | `chroma_rdr_path` | `~/.config/nexus/chroma_rdr/` | `rdr__{basename}-{hash8}` | `nx index --type rdr`, `nx search --type rdr` |
| **knowledge** | `chroma_knowledge_path` | `~/.config/nexus/chroma_knowledge/` | `knowledge__{user_name}` | `nx store`, `nx search --type knowledge` |

Each store is a `chromadb.PersistentClient` instance at its configured path. `T3Database` is unchanged; stores are constructed via `_client` injection.

Collection naming within each store is unchanged from the current single-store naming — the `{type}__` prefix is retained because it carries functional meaning (`index_model_for_collection()` dispatches on it). The store boundary provides type isolation; the prefix provides model-selection metadata.

### Store Factories

```python
# src/nexus/db/t3_stores.py
import chromadb
from nexus.db.t3 import T3Database
from nexus.config import load_config, get_credential


def _persistent_t3(path_key: str, legacy_key: str | None = None) -> T3Database:
    cfg = load_config()
    chromadb_cfg = cfg.get("chromadb", {})
    path = chromadb_cfg.get(path_key) or (
        chromadb_cfg.get(legacy_key) if legacy_key else None
    )
    if not path:
        raise RuntimeError(f"T3 store not configured: set chromadb.{path_key} in config")
    voyage_api_key = get_credential(cfg, "voyage_api_key")
    return T3Database(
        voyage_api_key=voyage_api_key,
        _client=chromadb.PersistentClient(path=path),
    )


def t3_code() -> T3Database:
    return _persistent_t3("code_path")


def t3_docs() -> T3Database:
    return _persistent_t3("docs_path")


def t3_rdr() -> T3Database:
    return _persistent_t3("rdr_path")


def t3_knowledge() -> T3Database:
    return _persistent_t3("knowledge_path", legacy_key="path")  # legacy chroma_path alias
```

No dynamic dispatch, no prefix parsing. The store is selected at the call site based on command context.

### Config Schema Changes

New keys under `[chromadb]` in `~/.config/nexus/config.toml`:

```toml
[chromadb]
# Four-store layout (RDR-004)
code_path     = "~/.config/nexus/chroma_code"
docs_path     = "~/.config/nexus/chroma_docs"
rdr_path      = "~/.config/nexus/chroma_rdr"
knowledge_path = "~/.config/nexus/chroma_knowledge"

# Legacy single-store (deprecated — alias for knowledge_path)
path = ""  # if set, used as fallback for knowledge_path
```

Existing `tenant`, `database`, `api_key` keys remain for CloudClient fallback (if users want to keep reading from the old cloud store during transition).

### Command Routing

All commands that currently call `_t3()` or `make_t3()` are updated to call the appropriate store factory:

| Command | Current call | Updated call |
|---------|-------------|-------------|
| `nx index` (code) | `make_t3()` | `t3_code()` |
| `nx index` (docs) | `make_t3()` | `t3_docs()` |
| `nx index` (rdr) | `make_t3()` | `t3_rdr()` |
| `nx store put` | `_t3()` | `t3_knowledge()` |
| `nx store list` | `_t3()` | `t3_knowledge()` |
| `nx store expire` | `_t3()` | `t3_knowledge()` |
| `nx collection *` (default) | `_t3()` | `t3_knowledge()` (with `--type` override) |
| `nx search` (default) | `make_t3()` | all four stores, merged |
| `nx memory promote` | direct `T3Database(...)` | `t3_knowledge()` |
| `_prune_deleted_files()` | `make_t3()` | `t3_code()` + `t3_docs()` |
| `_prune_misclassified()` | `make_t3()` | `t3_code()` + `t3_docs()` |

### search Routing

`nx search` gains a `--type` flag; `--corpus` continues to work within the selected store(s):

```
nx search "query"                          # all four stores, fan-out, merged by score
nx search "query" --type code              # code store only
nx search "query" --type code --corpus nexus  # code store, corpus filter applied within it
nx search "query" --type knowledge         # knowledge store only
```

When `--type` is absent, results from all four stores are collected and merged by relevance score. When `--type` is given, only that store is queried; `--corpus` continues to filter within the store via `resolve_corpus()` as today.

### Collection Naming — Unchanged

`t3_collection_name()`, `_collection_name()`, `_docs_collection_name()`, `_rdr_collection_name()`, `_repo_identity()` are all unchanged. Collection names within each store retain their `{type}__` prefix.

### What Is Unchanged

- `T3Database` class: no changes
- `index_model_for_collection()`: no changes (prefix dispatch still correct)
- `expire()`: no code changes; docstring updated to reflect that it is called on the knowledge store only
- Collection naming functions in `registry.py` and `corpus.py`: no changes
- `t3_collection_name()`: no changes
- `resolve_corpus()`: no changes; continues to work within a store's `list_collections()`

### Migration of Existing Data

`nx migrate t3` — non-destructive one-time migration:

1. Open the existing single store (via `chromadb.CloudClient` using current `[chromadb]` credentials, or `PersistentClient` at legacy `path` if already local)
2. For each collection in the old store:
   - `code__*` → copy to code store
   - `docs__*` → copy to docs store
   - `rdr__*` → copy to rdr store
   - `knowledge__*` → copy to knowledge store
   - Unrecognised prefix → warn, copy to knowledge store with a migration warning tag
3. Count invariant: `sum(counts across 4 new stores) == sum(counts across old store)`
4. Print per-type migration report; do not delete old store (user removes manually after verifying)

## Trade-offs

| Dimension | Single store | Four stores |
|-----------|-------------|------------|
| Prefix resolution complexity | Fatal for RDR-003 | Eliminated — `list_collections()` returns only the relevant type |
| Cross-type search default | One query | Fan-out to 4 stores; negligible latency (embedding dominates) |
| Config surface | One path or (tenant, db, key) | Four paths |
| Resource usage | One ChromaDB instance | Four PersistentClient instances (lightweight) |
| Type isolation | Convention only | Enforced by store boundary |
| `index_model_for_collection()` | Works on prefix | Works on prefix — unchanged |
| `expire()` | Filters on `knowledge__` prefix | Called on knowledge store only; filter now redundant but harmless |
| Migration | N/A | One-time; non-destructive; count-verified |
| Cloud dependency | Required (CloudClient) | None (PersistentClient); VoyageAI key still required for embeddings |

## Implementation Plan

### Phase 1 — Config and Store Factories
1. Add `code_path`, `docs_path`, `rdr_path`, `knowledge_path` under `[chromadb]` in config schema (`src/nexus/config.py`). Default each to `~/.config/nexus/chroma_{type}/`. Add `path` as deprecated legacy alias for `knowledge_path`.
2. Create `src/nexus/db/t3_stores.py` with `_persistent_t3()`, `t3_code()`, `t3_docs()`, `t3_rdr()`, `t3_knowledge()`.

### Phase 2 — Command Routing (deploy atomically with Phase 3)
3. `commands/store.py`: `put_cmd`, `list_cmd`, `expire_cmd` → `t3_knowledge()`.
4. `commands/collection.py`: all four subcommands (`list_cmd`, `info_cmd`, `delete_cmd`, `verify_cmd`) → `t3_knowledge()` by default; add `--type` flag routing to all four stores.
5. `commands/search_cmd.py`: add `--type` flag; default queries all 4 stores; `--corpus` continues to work within selected store(s).
6. `commands/memory.py` (`promote_cmd`): replace direct `T3Database(...)` construction with `t3_knowledge()`.
7. `indexer.py`: `_prune_deleted_files()` and `_prune_misclassified()` replace `make_t3()` with `t3_code()` + `t3_docs()` as appropriate.
8. `index.py` entry point: route `nx index` to `t3_code()` (code collections) and `t3_docs()` (docs collections).

### Phase 3 — Cleanup (deploy atomically with Phase 2)
9. Update `T3Database.expire()` docstring: remove `knowledge__` prefix filter explanation; state it is called on the knowledge store only.
10. Remove or deprecate `make_t3()` / `_t3()` single-store factory; replace all remaining callers.

### Phase 4 — Migration
11. Implement `nx migrate t3` subcommand with count-invariant verification and per-type report.
12. Update `docs/architecture.md` T3 section; update `docs/cli-reference.md` for new `--type` flag and `nx migrate t3`.

## Test Plan

- P1: `t3_code()`, `t3_docs()`, `t3_rdr()`, `t3_knowledge()` each return a `T3Database` connected to the correct configured path (verify via `T3Database._client.get_settings().chroma_server_http_port` or by querying a sentinel collection).
- P2: `nx store put "doc"` (no `--type`) → item appears in knowledge store; absent from code, docs, and rdr stores.
- P3: `nx index repo` → code collection created in code store; docs collection in docs store; absent from knowledge and rdr stores.
- P4: `nx search "query"` (no `--type`) → results from all 4 stores, merged; verify store labels in result metadata.
- P5: `nx search "query" --type code` → only code store queried.
- P6: `nx search "query" --type code --corpus nexus` → code store, filtered by `nexus__` prefix.
- P7: `nx migrate t3` → sum of document counts across 4 new stores equals sum from old store; each type lands in correct store.
- P8: `nx collection info myname` → queries knowledge store; `nx collection info myname --type code` → queries code store.
- P9: `nx collection delete myname --type code` → deletes from code store only.
- P10: `nx collection verify myname --type docs` → verifies docs store only.
- P11: `promote_cmd` with bare `--collection notes` → stored in knowledge store as `knowledge__notes`.
- P12: `expire_cmd` → only knowledge store entries with expired TTL are deleted; code/docs/rdr stores untouched.

## Open Questions

- Should `--type` on `nx search` accept a comma-separated list (`--type code,docs`)? Default is all-four fan-out; defer until a use case materialises.
- Should `path` config keys accept `~` expansion? Use `pathlib.Path.expanduser()` — yes, document this.

## Revision History

### Gate 1 (2026-02-27) — BLOCKED

**3 critical, 7 significant, 6 observations.**

#### Critical — Resolved

**C1. Store factory signature wrong — T3Database uses CloudClient constructor, not path.** Fixed: factories use `_client=chromadb.PersistentClient(path)` injection. T3Database constructor unchanged. Config uses `code_path`, `docs_path`, etc. under `[chromadb]`.

**C2. `expire()` hardcodes `knowledge__` prefix — silently broken post-migration.** Fixed: `expire()` is called only via `t3_knowledge()` — that store contains only `knowledge__*` collections, so the filter is harmless. Docstring updated. No code change to `expire()` required.

**C3. `index_model_for_collection()` dispatches on prefix — would be wrong if prefix removed.** Fixed: collection names retain their `{type}__` prefix in all four stores. `index_model_for_collection()` is unchanged and remains correct.

#### Significant — Resolved

**S1. File named `rdr-004-three-store-architecture.md`; Open Questions said "all-three fan-out".** Fixed: file renamed to `rdr-004-four-store-architecture.md`; open questions corrected to "all-four fan-out".

**S2. `T3Database` claimed "unchanged" but multiple methods implied to require changes.** Fixed: design now explicitly states what is unchanged and why (expire docstring only; no code changes).

**S3. `delete_cmd` and `verify_cmd` not addressed in plan.** Fixed: Phase 2 step 4 explicitly covers all four `collection` subcommands with `--type` routing. Test cases P9 and P10 added.

**S4. YAGNI claim wrong — `docs__` and `rdr__` already exist.** Fixed: YAGNI section corrected; migration handles all four prefix types; naming functions acknowledged as pre-existing.

**S5. `--corpus` vs `--type` flag conflict not specified.** Fixed: Design section "search Routing" specifies that `--type` routes the store and `--corpus` filters within it. Both can be composed.

**S6. `promote_cmd` direct `T3Database` construction not addressed for testability.** Fixed: Phase 2 step 6 explicitly replaces direct construction with `t3_knowledge()`; `_persistent_t3()` accepts injected `_client` for testing via the same mechanism.

**S7. `null` sentinel for no-git-repo case was unresolved.** Resolved: not needed. `_repo_identity()` already has a fallback when git is unavailable; collection naming follows existing conventions.

#### Observations — Applied

- O1: `--type` routes store; `--corpus` filters within store — both specified, compose naturally
- O2: `_prune_deleted_files()` and `_prune_misclassified()` added to Phase 2 step 7
- O3: Phases 2 and 3 noted as requiring atomic deployment
- O4: P1 verifies store identity by path (PersistentClient, not CloudClient)
- O5: Migration count invariant explicitly stated in migration step 3
- O6: `nx rdr store` reference removed; `nx index --type rdr` / `nx index rdr` used instead
