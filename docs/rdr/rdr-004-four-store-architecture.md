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

| Store | Config key (`[chromadb]`) | Default path | Collection naming | Primary commands |
|-------|--------------------------|--------------|-------------------|-----------------|
| **code** | `code_path` | `~/.config/nexus/chroma_code/` | `code__{basename}-{hash8}` | `nx index`, `nx search --type code` |
| **docs** | `docs_path` | `~/.config/nexus/chroma_docs/` | `docs__{basename}-{hash8}` | `nx index`, `nx search --type docs` |
| **rdr** | `rdr_path` | `~/.config/nexus/chroma_rdr/` | `rdr__{basename}-{hash8}` | `nx index --type rdr`, `nx search --type rdr` |
| **knowledge** | `knowledge_path` | `~/.config/nexus/chroma_knowledge/` | `knowledge__{user_name}` | `nx store`, `nx search --type knowledge` |

Each store is a `chromadb.PersistentClient` instance at its configured path. `T3Database` is unchanged; stores are constructed via `_client` injection.

Collection naming within each store is unchanged from the current single-store naming — the `{type}__` prefix is retained because it carries functional meaning (`index_model_for_collection()` dispatches on it). The store boundary provides type isolation; the prefix provides model-selection metadata.

### Store Factories

```python
# src/nexus/db/t3_stores.py
import chromadb
from pathlib import Path
from nexus.db.t3 import T3Database
from nexus.config import load_config, get_credential


def _persistent_t3(path_key: str, legacy_key: str | None = None) -> T3Database:
    cfg = load_config()
    chromadb_cfg = cfg.get("chromadb", {})
    raw_path = chromadb_cfg.get(path_key) or (
        chromadb_cfg.get(legacy_key) if legacy_key else None
    )
    if not raw_path:
        raise RuntimeError(f"T3 store not configured: set chromadb.{path_key} in config")
    path = str(Path(raw_path).expanduser())
    voyage_api_key = get_credential(cfg, "voyage_api_key")
    if not voyage_api_key:
        raise RuntimeError("voyage_api_key not configured — required for embeddings")
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

New keys under `chromadb:` in `~/.config/nexus/config.yml` (the codebase uses YAML via `yaml.safe_load()`, not TOML):

```yaml
chromadb:
  # Four-store layout (RDR-004)
  code_path: ~/.config/nexus/chroma_code
  docs_path: ~/.config/nexus/chroma_docs
  rdr_path: ~/.config/nexus/chroma_rdr
  knowledge_path: ~/.config/nexus/chroma_knowledge

  # Legacy single-store (deprecated — alias for knowledge_path)
  path: ""  # if set, used as fallback for knowledge_path
```

All four `*_path` keys are added to `_DEFAULTS["chromadb"]` in `config.py` with empty-string defaults (no default paths — an absent value raises a clear `RuntimeError` rather than silently using a path that has never been initialised). The `~` in path values is expanded by `_persistent_t3()` via `Path.expanduser()` before passing to `PersistentClient`.

Existing `tenant`, `database`, `api_key` keys remain for CloudClient access during migration.

### Credential Guard Updates

The following guards must be updated in Phase 1 alongside the factory functions:

| Location | Current guard | Updated guard |
|----------|--------------|--------------|
| `doc_indexer._has_credentials()` (line 40) | `voyage_api_key and chroma_api_key` | `voyage_api_key` only |
| `indexer.py:163` (`_run_index_frecency_only`) | checks `chroma_api_key`, raises `CredentialsMissingError` | check `voyage_api_key` only |
| `indexer.py:752` (`_run_index`) | checks `chroma_api_key`, raises `CredentialsMissingError` | check `voyage_api_key` only |
| `store._t3()` (lines 13–38) | validates `chroma_api_key`, `chroma_tenant`, `chroma_database` | removed — replaced by factory `voyage_api_key` guard |

`polling.py`'s retry logic catches `CredentialsMissingError` to avoid recording `head_hash` on credential failures. This behaviour is preserved: the updated guards still raise `CredentialsMissingError` (same exception type, new message), so polling skips `head_hash` writes correctly.

### Command Routing

All commands that currently call `_t3()` or `make_t3()` are updated to call the appropriate store factory:

| Command | Current call | Updated call |
|---------|-------------|-------------|
| `nx index` (code) | `make_t3()` | `t3_code()` |
| `nx index` (docs) | `make_t3()` | `t3_docs()` |
| `nx index` (rdr) | `make_t3()` | `t3_rdr()` |
| `_discover_and_index_rdrs()` (inside `_run_index()`) | receives `db` from caller | call `t3_rdr()` directly — do not pass `db` from code/docs path |
| `nx store put` | `_t3()` | `t3_knowledge()` |
| `nx store list` | `_t3()` | `t3_knowledge()` |
| `nx store expire` | `_t3()` | `t3_knowledge()` |
| `nx collection list` (no `--type`) | `_t3()` | enumerate all 4 stores (see below) |
| `nx collection info/delete/verify` (no `--type`) | `_t3()` | `t3_knowledge()` default; `--type` routes to specified store |
| `nx search` (default) | `make_t3()` | fan-out to all 4 stores (see below) |
| `nx memory promote` | direct `T3Database(...)` | `t3_knowledge()` |
| `pm.py:archive()` | `make_t3()` | `t3_knowledge()` |
| `pm.py:reference()` (semantic path) | `make_t3()` | `t3_knowledge()` |
| `pm.py:reference()` (project-name path) | `make_t3()` | `t3_knowledge()` |
| `commands/pm.py` | `_t3()` | `t3_knowledge()` |
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

#### Fan-out algorithm (no `--type`)

When `--type` is absent, `search_cmd.py` implements the following:

```python
stores = [("code", t3_code()), ("docs", t3_docs()),
          ("rdr", t3_rdr()), ("knowledge", t3_knowledge())]
all_results = []
for store_type, db in stores:
    collections = db.list_collections()
    targets = resolve_corpus(corpus, collections) if corpus else collections
    if targets:
        results = db.search(query, target_collections=targets, n_results=n)
        for r in results:
            r["store"] = store_type  # tag for display
        all_results.extend(results)
# merge by distance (ascending), take top-n overall
all_results.sort(key=lambda r: r["distance"])
return all_results[:n]
```

#### `--corpus` without `--type`

When `--corpus` is given without `--type`, `resolve_corpus()` is called against each store's `list_collections()` independently:

- A bare corpus name (`nexus`) is prefix-matched against all collections in each store. The code store may match `code__nexus-8c2e74c0`; the docs store may match `docs__nexus-8c2e74c0`; other stores may return no match. All matching stores contribute results.
- A fully-qualified name containing `__` (e.g. `code__nexus-8c2e74c0`) is exact-matched against each store independently. It will match in exactly one store and return no results in the others — correct behaviour.

When `--type` is given, only the specified store is queried; `resolve_corpus()` is called once against that store's collection list, as today.

### collection list Default Behaviour

`nx collection list` without `--type` enumerates all four stores and displays collections from all of them, labelled by store type. This preserves current behaviour where all collections are visible. The output groups by store:

```
[code]      code__nexus-8c2e74c0
[docs]      docs__nexus-8c2e74c0
[knowledge] knowledge__my-notes
[rdr]       rdr__nexus-8c2e74c0
```

The `info_cmd`, `delete_cmd`, and `verify_cmd` subcommands default to the knowledge store when no `--type` is given (since bare names like `my-notes` are most often knowledge collections). `--type` routes any of the four commands to the specified store.

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

`nx migrate t3` — non-destructive, idempotent one-time migration:

1. **Open source store**: If `chromadb.path` is set in config, open via `PersistentClient(path)`. Otherwise, open via `CloudClient(tenant, database, api_key)` using the existing `[chromadb]` credentials (call `make_t3()` — the old factory). This is the only place `make_t3()` is called post-Phase 3; migration must run before `make_t3()` is removed.
2. **Open destination stores** via `t3_code()`, `t3_docs()`, `t3_rdr()`, `t3_knowledge()`.
3. **For each collection in the source store**:
   - `code__*` → copy to code store
   - `docs__*` → copy to docs store
   - `rdr__*` → copy to rdr store
   - `knowledge__*` → copy to knowledge store
   - Unrecognised prefix → warn, copy to knowledge store with metadata tag `migrated_unknown_prefix: true`
4. **Idempotency**: If a collection already exists in the destination, compare document count. If equal, skip. If different, warn and overwrite (do not silently double-insert).
5. **Per-type count verification**: After migration, assert that the count of `code__*` docs in the code store equals the count of `code__*` docs in the source, and similarly for each other type. Total count invariant is insufficient — per-type verification catches cross-type routing errors.
6. Print per-type migration report; do not delete source store (user removes manually after verifying).

**Deployment ordering**: Phase 4 migration must be run by existing users before Phases 2+3 are deployed. Deploying Phases 2+3 first on a machine with existing cloud data leaves the new PersistentClient stores empty. Document this in the release notes.

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

### Phase 1 — Config, Factories, and Credential Guards
1. Add `code_path`, `docs_path`, `rdr_path`, `knowledge_path` to `_DEFAULTS["chromadb"]` in `src/nexus/config.py` with empty-string defaults. Add `path` as deprecated legacy alias for `knowledge_path`.
2. Create `src/nexus/db/t3_stores.py` with `_persistent_t3()` (including `Path.expanduser()` and `voyage_api_key` guard), `t3_code()`, `t3_docs()`, `t3_rdr()`, `t3_knowledge()`.
3. Update credential gates:
   - `doc_indexer._has_credentials()`: check only `voyage_api_key`; remove `chroma_api_key` requirement.
   - `indexer.py:163` and `indexer.py:752`: replace `chroma_api_key` guard with `voyage_api_key` guard; keep `CredentialsMissingError` as the exception type.
   - `store._t3()`: credential validation will be replaced when `_t3()` is removed in Phase 3.

### Phase 2 — Command Routing (deploy atomically with Phase 3)
4. `commands/store.py`: `put_cmd`, `list_cmd`, `expire_cmd` → `t3_knowledge()`.
5. `commands/collection.py`: `list_cmd` enumerates all 4 stores by default (grouped output); `info_cmd`, `delete_cmd`, `verify_cmd` → `t3_knowledge()` default; add `--type` flag routing to all four stores for all subcommands.
6. `commands/search_cmd.py`: add `--type` flag; implement fan-out algorithm (four `T3Database` instances, `resolve_corpus()` per store, merge by distance); `--corpus` applies per-store as specified.
7. `commands/memory.py` (`promote_cmd`): replace direct `T3Database(...)` construction with `t3_knowledge()`.
8. `pm.py` and `commands/pm.py`: replace all `make_t3()` / `_t3()` calls with `t3_knowledge()`.
9. `indexer.py`:
   - `_prune_deleted_files()` and `_prune_misclassified()` → `t3_code()` + `t3_docs()`.
   - `_discover_and_index_rdrs()`: call `t3_rdr()` directly instead of receiving `db` from caller.
10. `index.py` entry point: route `nx index` to `t3_code()` (code collections), `t3_docs()` (docs collections), `t3_rdr()` (rdr collections).

### Phase 3 — Cleanup (deploy atomically with Phase 2)
11. Update `T3Database.expire()` docstring and `__exit__` comment to reflect that the client may be `PersistentClient` or `CloudClient`.
12. Remove or deprecate `make_t3()` / `_t3()` single-store factory; replace all remaining callers (except `nx migrate t3` which retains `make_t3()` for CloudClient source access).

### Phase 4 — Migration
13. Implement `nx migrate t3` subcommand: CloudClient or legacy-path source; four destination stores; per-type count verification; idempotent re-run; migration report.
14. Update `docs/architecture.md` T3 section; update `docs/cli-reference.md` for new `--type` flag and `nx migrate t3`; document migration deployment ordering in release notes.

## Test Plan

- P1: `t3_code()`, `t3_docs()`, `t3_rdr()`, `t3_knowledge()` each return a `T3Database` whose `_client` is a `chromadb.PersistentClient` at the configured path (verify via `type(db._client).__name__ == "PersistentClient"` and `db._client._settings.persist_directory == expected_path`).
- P2: `nx store put "doc"` (no `--type`) → item appears in knowledge store; absent from code, docs, and rdr stores.
- P3: `nx index repo` → code collection created in code store; docs collection in docs store; absent from knowledge and rdr stores.
- P4: `nx search "query"` (no `--type`) → results from all 4 stores, merged and sorted by distance; result metadata includes `store` label.
- P5: `nx search "query" --type code` → only code store queried; docs/rdr/knowledge stores not touched.
- P6: `nx search "query" --type code --corpus nexus` → code store only; `resolve_corpus("nexus", code_collections)` filters to `code__nexus-*` collections.
- P7: `nx migrate t3` → per-type count verification: `code__*` count in code store matches source; same for docs, rdr, knowledge. Re-running migration on an already-migrated store skips equal-count collections without double-inserting.
- P8: `nx collection list` (no `--type`) → collections from all 4 stores displayed, grouped by store type.
- P9: `nx collection info myname` → queries knowledge store; `nx collection info myname --type code` → queries code store.
- P10: `nx collection delete myname --type code` → deletes from code store only.
- P11: `nx collection verify myname --type docs` → verifies docs store only.
- P12: `promote_cmd` with bare `--collection notes` → stored in knowledge store as `knowledge__notes`.
- P13: `expire_cmd` → only knowledge store entries with expired TTL are deleted; code/docs/rdr stores untouched.
- P14: `_has_credentials()` returns True when only `voyage_api_key` is set and `chroma_api_key` is absent.
- P15: `pm.py:archive()` writes `knowledge__pm__*` collection to knowledge store; subsequent `nx search "query" --type knowledge` returns the archived content.
- P16: `_discover_and_index_rdrs()` writes `rdr__*` collections to the rdr store, not the code store.

## Open Questions

- Should `--type` on `nx search` accept a comma-separated list (`--type code,docs`)? Default is all-four fan-out; defer until a use case materialises.

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

### Gate 2 (2026-02-27) — BLOCKED → fixed

**3 critical, 5 significant, 5 observations.**

Critic read source files: `t3.py`, `corpus.py`, `config.py`, `indexer.py`, `doc_indexer.py`, `commands/store.py`, `commands/search_cmd.py`, `commands/collection.py`, `commands/memory.py`, `commands/pm.py`, `pm.py`, `db/__init__.py`.

#### Critical

**C1. `_has_credentials()` in `doc_indexer.py` checks `chroma_api_key` — silently blocks all document indexing for local-only users.** The guard at `doc_indexer.py:40` returns False if `chroma_api_key` is absent; `_index_document()`, `index_pdf()`, `index_markdown()`, and `batch_index_markdowns()` all return 0 silently. Additionally `indexer.py` lines 163 and 752 check `chroma_api_key` and raise `CredentialsMissingError` before calling `make_t3()`. Phase 1 must add a new credential gate that checks only `voyage_api_key` for local-store paths; `_has_credentials()` and both indexer guards must be updated.

**C2. `pm.py` uses `make_t3()` for PM archive and reference — not in routing table.** Three call sites confirmed: `pm.py:319` (PM archive writes `knowledge__pm__*`), `pm.py:442` and `pm.py:455` (PM reference reads from `knowledge__pm__*`). `commands/pm.py:227` uses `_t3()`. Post-migration, PM archive and reference silently operate against the old cloud store — data is split; `nx search` cannot find PM-archived content. Add `pm.py` and `commands/pm.py` to Phase 2 routing table.

**C3. Cross-store `nx search` fan-out is architecturally unspecified and unimplementable as written.** A single `T3Database` backed by one `PersistentClient` can only see one store's collections. Querying all four stores requires four `T3Database` instances, four `list_collections()` calls, four `resolve_corpus()` invocations, and a merge step — none of which are specified. The design does not state: whether a facade function is needed; how `resolve_corpus()` is called per-store; how `--corpus` without `--type` applies across stores. Developer will make these decisions ad hoc in code.

#### Significant

**S1. `store._t3()` credential validation (`chroma_api_key`, `chroma_tenant`, `chroma_database`) remains active until Phase 3.** Commands that still call `_t3()` after Phase 2 will demand now-irrelevant cloud credentials. Removing `_t3()` also removes the only `voyage_api_key` guard — new factories need explicit validation that `voyage_api_key` is set.

**S2. Config format documented as TOML; codebase uses YAML.** `config.py` uses `yaml.safe_load()` on `~/.config/nexus/config.yml`; there is no TOML parser. New keys must be added to `_DEFAULTS["chromadb"]` in YAML format. Also: Design table uses `chroma_code_path` but factory code uses `code_path` — inconsistent within the RDR itself. Reconcile key names.

**S3. Migration has no code path to open the old CloudClient source store.** `_persistent_t3()` only handles local paths; it cannot open a CloudClient. `nx migrate t3` must separately call `make_t3()` (the old factory) to open the cloud source, then call the four new factories for destinations. Also: total count invariant does not catch partial failure + re-run double-counting; add per-type count verification and document idempotency.

**S4. `nx collection list` without `--type` silently hides code/docs/rdr collections.** Default routing to `t3_knowledge()` means `nx collection list` shows only knowledge collections after migration — a behavioral regression from today's all-types view. Either enumerate all four stores by default, or explicitly document the change and provide `--all`. No test case covers this.

**S5. `resolve_corpus()` breaks silently for fully-qualified corpus names across stores.** Exact-match path (`if "__" in corpus`) returns `[]` if the named collection does not exist in the store being searched. For fan-out search (C3 above), `resolve_corpus()` must be called against each store's collection list independently. Until C3 is resolved, this is a latent correctness failure.

#### Observations

- O1: `T3Database.__exit__` docstring says "CloudClient is HTTP-based; no persistent connection to close" — incorrect for `PersistentClient`. Update to cover both client types.
- O2: Test P1 cites `_client.get_settings().chroma_server_http_port` — this attribute does not exist on `PersistentClient`. Use `type(db._client).__name__` or `_client._system.settings.persist_directory` instead.
- O3: `~` expansion (`pathlib.Path.expanduser()`) is in Open Questions but not in any Phase 1 implementation step. `PersistentClient` does not expand `~`. Add to Phase 1 step 2.
- O4: `_discover_and_index_rdrs()` receives `db` as a parameter from `_run_index()`. If `_run_index()` is updated to use `t3_code()` as primary `db`, RDR chunks will be written to the code store, not the rdr store. Add `_discover_and_index_rdrs()` to the routing table (Phase 2 step 8).
- O5: Fan-out search over 4 stores may open up to 32 `ThreadPoolExecutor` threads simultaneously (8 per `T3Database.list_collections()` call). Unlikely to be a practical problem with local SQLite WAL, but note for large deployments.

#### Critical — Resolved

**C1 — RESOLVED.** Added "Credential Guard Updates" section to Design. Phase 1 step 3 explicitly updates `doc_indexer._has_credentials()` and both `indexer.py` guards to check only `voyage_api_key`. `CredentialsMissingError` exception type preserved for `polling.py` retry logic.

**C2 — RESOLVED.** Added `pm.py` and `commands/pm.py` rows to Command Routing table. Phase 2 step 8 explicitly routes all three `pm.py` `make_t3()` call sites and `commands/pm.py:227` to `t3_knowledge()`. Test P15 verifies PM archive is retrievable from knowledge store.

**C3 — RESOLVED.** Added "Fan-out algorithm (no --type)" subsection to search Routing with explicit pseudocode: four `T3Database` instances, `resolve_corpus()` per store, merge by distance. Added "`--corpus` without `--type`" subsection specifying per-store resolution behaviour for both bare names and fully-qualified `__`-containing names.

#### Significant — Resolved

**S1 — RESOLVED.** Factory `_persistent_t3()` now explicitly validates `voyage_api_key` and raises `RuntimeError` if absent. Phase 1 step 3 addresses `store._t3()` credential validation. "Credential Guard Updates" section specifies the full set of changes.

**S2 — RESOLVED.** Config Schema section corrected from TOML to YAML format (`config.yml`). Design table config key column renamed from `chroma_*_path` to `*_path` (within `[chromadb]`). New keys added to `_DEFAULTS["chromadb"]` specified in Phase 1 step 1.

**S3 — RESOLVED.** Migration section rewritten: step 1 now explicitly handles both CloudClient source (via `make_t3()`) and PersistentClient source (via legacy `path`). Idempotency documented. Per-type count verification replaces total-only invariant. Deployment ordering documented.

**S4 — RESOLVED.** Added "collection list Default Behaviour" section: `nx collection list` without `--type` enumerates all four stores, grouped by type. `info_cmd`/`delete_cmd`/`verify_cmd` default to knowledge store. Test P8 updated.

**S5 — RESOLVED.** Fan-out algorithm in "search Routing" explicitly calls `resolve_corpus()` per store independently. "`--corpus` without `--type`" subsection specifies that fully-qualified names are exact-matched against each store's collection list — correct result is returned from the matching store, `[]` from the others.

#### Observations — Applied

- O1: Phase 3 step 11 adds `T3Database.__exit__` docstring update covering both client types
- O2: Test P1 updated to use `type(db._client).__name__` and `persist_directory`
- O3: `Path.expanduser()` added to `_persistent_t3()` factory code; removed from Open Questions (resolved)
- O4: `_discover_and_index_rdrs()` added to Command Routing table and Phase 2 step 9
- O5: Threading note acknowledged; no action required
