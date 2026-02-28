# RDR-004: Four-Store T3 Architecture — Implementation Plan

**Epic**: `nexus-pjsc`
**Design**: `docs/rdr/rdr-004-four-store-architecture.md` (accepted, 2026-02-27)
**Author**: strategic-planner
**Date**: 2026-02-27

## Executive Summary

Replace T3's single ChromaDB database (CloudClient) with four dedicated local
PersistentClient stores — one per content type: **code**, **docs**, **rdr**,
**knowledge**. This eliminates the prefix disambiguation complexity that blocked
RDR-003 through five gate rounds.

The implementation is decomposed into 4 phases (12 tasks) with explicit
dependency ordering, TDD discipline, and an atomic deployment gate for Phases
2+3.

## Phase Overview

| Phase | Tasks | Description | Deploy Notes |
|-------|-------|-------------|-------------|
| 1 | nexus-pjsc.1, nexus-pjsc.2 | Config defaults, store factories, credential guards | Independent — can deploy alone |
| 2 | nexus-pjsc.3 through nexus-pjsc.9 | Command routing to new stores | **ATOMIC with Phase 3** |
| 3 | nexus-pjsc.10 | Remove dead code (make_t3, _t3, _t3_for_search) | **ATOMIC with Phase 2** |
| 4 | nexus-pjsc.11, nexus-pjsc.12 | Migration command + documentation | Independent — migration must run before Phase 2+3 for existing users |

## Dependency Graph

```
nexus-pjsc.1 (Config + Factories)
  |
  +---> nexus-pjsc.2 (Credential Guards)
  |       |
  |       +---> nexus-pjsc.8 (Indexer Rewrite) ----+
  |                                                  |
  +---> nexus-pjsc.3 (Store Commands) ------+       |
  +---> nexus-pjsc.4 (Collection Cmds) -----+       |
  +---> nexus-pjsc.5 (Search Command) ------+       |
  +---> nexus-pjsc.6 (Memory Promote) ------+       |
  +---> nexus-pjsc.7 (PM Commands) ---------+       |
  |                                          |       |
  |                  nexus-pjsc.8 ---------> nexus-pjsc.9 (Index Cmds)
  |                                          |       |
  |                                          +-------+
  |                                          |
  |                                          v
  |                                   nexus-pjsc.10 (Cleanup)
  |                                          |
  +---> nexus-pjsc.11 (Migration) ----+
                                     |
                                     v
                              nexus-pjsc.12 (Documentation)
```

## Critical Path

```
nexus-pjsc.1 -> nexus-pjsc.2 -> nexus-pjsc.8 -> nexus-pjsc.9 -> nexus-pjsc.10 -> nexus-pjsc.12
```

The indexer rewrite (nexus-pjsc.8) is the longest task on the critical path
because it rewrites 5 functions in `indexer.py` with multi-store routing logic.

## Parallelization Opportunities

After `nexus-pjsc.1` (Config + Factories) completes, the following tracks can
run in parallel:

| Track | Tasks | Files Touched |
|-------|-------|---------------|
| A | nexus-pjsc.2 (credential guards) | doc_indexer.py, indexer.py, t3.py |
| B | nexus-pjsc.3 (store commands) | commands/store.py |
| C | nexus-pjsc.4 (collection commands) | commands/collection.py |
| D | nexus-pjsc.5 (search command) | commands/search_cmd.py |
| E | nexus-pjsc.6 (memory promote) | commands/memory.py |
| F | nexus-pjsc.7 (PM commands) | pm.py, commands/pm.py |
| G | nexus-pjsc.11 (migration) | commands/migrate.py (NEW) |

Tracks B-F have zero file overlap and can be assigned to separate agents.
Track A must complete before Track H (indexer rewrite) starts, because both
touch `indexer.py`.

## Phase 1 — Config, Factories, and Credential Guards

### nexus-pjsc.1: Add four-store config defaults and create t3_stores.py

**Files**:
- `src/nexus/config.py` — lines 51-54 (`_DEFAULTS["chromadb"]`)
- `src/nexus/db/t3_stores.py` — NEW
- `tests/test_t3_stores.py` — NEW

**Test cases**: P1 (factory returns PersistentClient at correct path)

**Config changes** (`_DEFAULTS["chromadb"]` after this task):
```python
"chromadb": {
    "tenant":         "",
    "database":       "",
    "code_path":      "~/.config/nexus/chroma_code",
    "docs_path":      "~/.config/nexus/chroma_docs",
    "rdr_path":       "~/.config/nexus/chroma_rdr",
    "knowledge_path": "~/.config/nexus/chroma_knowledge",
    "path":           "",   # legacy alias; empty = not set
},
```

**Factory module** (`t3_stores.py`):
- `_persistent_t3(path_key, legacy_key=None)` — load config, expand path, check
  `voyage_api_key`, return `T3Database(_client=PersistentClient(path), voyage_api_key=key)`
- `t3_code()`, `t3_docs()`, `t3_rdr()` — call `_persistent_t3` with respective key
- `t3_knowledge()` — calls `_persistent_t3("knowledge_path", legacy_key="path")`

**Dependencies**: None (first task)
**Blocks**: nexus-pjsc.2, .3, .4, .5, .6, .7, .8, .11

### nexus-pjsc.2: Update credential guards for local-store architecture

**Files**:
- `src/nexus/doc_indexer.py` — line 40 (`_has_credentials()`)
- `src/nexus/indexer.py` — lines 162-172, 751-761 (credential guards)
- `src/nexus/db/t3.py` — lines 61, 252-253 (docstrings)
- `tests/test_doc_indexer.py` — add P14 test

**Test cases**: P14 (`_has_credentials()` True with only `voyage_api_key`)

**Changes**:
- `_has_credentials()`: check only `voyage_api_key`; remove `chroma_api_key`
- Indexer guards: remove `chroma_api_key` check, keep `CredentialsMissingError`
- `T3Database.__exit__` comment: update for PersistentClient
- `T3Database.expire()` docstring: note called on knowledge store only

**Dependencies**: nexus-pjsc.1
**Blocks**: nexus-pjsc.8

## Phase 2 — Command Routing (Deploy Atomically with Phase 3)

### nexus-pjsc.3: Route store commands to knowledge store

**Files**: `src/nexus/commands/store.py`
**Test cases**: P2 (store put -> knowledge only), P13 (expire -> knowledge only)
**Dependencies**: nexus-pjsc.1
**Blocks**: nexus-pjsc.10

Replace `_t3()` calls in `put_cmd` (line 94), `list_cmd` (line 116),
`expire_cmd` (line 139) with `t3_knowledge()`. Keep `_t3()` function body
intact (removed in Phase 3).

### nexus-pjsc.4: Add --type flag to collection commands

**Files**: `src/nexus/commands/collection.py`
**Test cases**: P8, P9, P10, P11
**Dependencies**: nexus-pjsc.1
**Blocks**: nexus-pjsc.10

- `list_cmd` with no `--type`: enumerate all 4 stores, grouped output
  `[code] code__nexus-8c2e74c0`
- `info_cmd`, `delete_cmd`, `verify_cmd`: default to `t3_knowledge()`; `--type`
  routes to specified store
- On "collection not found", suggest `--type <code|docs|rdr>`

### nexus-pjsc.5: Add --type flag and fan-out to search command

**Files**: `src/nexus/commands/search_cmd.py`
**Test cases**: P4, P5, P6
**Dependencies**: nexus-pjsc.1
**Blocks**: nexus-pjsc.10

Fan-out algorithm (no `--type`):
```python
stores = [("code", t3_code()), ("docs", t3_docs()),
          ("rdr", t3_rdr()), ("knowledge", t3_knowledge())]
for store_type, db in stores:
    names = [c["name"] for c in db.list_collections()]
    targets = resolve_corpus(c, names) for each --corpus arg
    results = search_cross_corpus(query, targets, n, t3=db, where=filter)
    tag results with store_type
merge all results by distance, take top-n
```

### nexus-pjsc.6: Route memory promote to knowledge store

**Files**: `src/nexus/commands/memory.py` (lines 112-139)
**Test cases**: P12
**Dependencies**: nexus-pjsc.1
**Blocks**: nexus-pjsc.10

Replace lines 112-139 entirely:
- Remove credential guard (lines 112-120)
- Remove direct `T3Database(tenant=..., ...)` constructor (lines 134-139)
- Replace with `with t3_knowledge() as t3:`

### nexus-pjsc.7: Route PM commands to knowledge store

**Files**: `src/nexus/pm.py`, `src/nexus/commands/pm.py`
**Test cases**: P15
**Dependencies**: nexus-pjsc.1
**Blocks**: nexus-pjsc.10

- `pm.py`: replace `make_t3()` at lines 319, 442, 455 with `t3_knowledge()`
- `commands/pm.py`: replace `_t3()` at line 227 with `t3_knowledge()`

### nexus-pjsc.8: Rewrite indexer for multi-store routing (LARGEST)

**Files**: `src/nexus/indexer.py` (5 functions rewritten)
**Test cases**: P3, P16
**Dependencies**: nexus-pjsc.1, nexus-pjsc.2
**Blocks**: nexus-pjsc.9, nexus-pjsc.10

This is the largest and most complex task. Rewrites:

1. **`_run_index_frecency_only()`** (line 144): Replace single `make_t3()` with
   split-loop: `for (col_name, db) in [(code_col, t3_code()), (docs_col, t3_docs())]:`

2. **`_run_index()`** (line 647): Replace single `make_t3()` (line 768) with
   `db_code=t3_code()`, `db_docs=t3_docs()`, `db_rdr=t3_rdr()`. Route each
   helper to its store.

3. **`_discover_and_index_rdrs()`** (line 532): Remove `db` AND `voyage_key`
   parameters (`voyage_key` is present in the signature but unused inside the
   function body); call `t3_rdr()` internally. Update call site at line 809 to
   drop both arguments.

4. **`_prune_deleted_files()`** (line 613): Remove `db` parameter; call
   `t3_code()` and `t3_docs()` internally.

5. **`_prune_misclassified()`** (line 577): Remove `db` parameter; call
   `t3_code()` and `t3_docs()` internally.

### nexus-pjsc.9: Route index commands and update doc_indexer

**Files**: `src/nexus/commands/index.py`, `src/nexus/doc_indexer.py`
**Test cases**: P3 (partial)
**Dependencies**: nexus-pjsc.8
**Blocks**: nexus-pjsc.10

- `index_pdf_cmd`: pass `t3=t3_docs()`
- `index_md_cmd`: pass `t3=t3_docs()`
- `index_rdr_cmd`: pass `t3=t3_rdr()`
- `_index_document()` line 170: replace `make_t3()` fallback with
  `RuntimeError("t3 store must be provided")`
- Remove `from nexus.db import make_t3` import from `doc_indexer.py` (line 19)
  — becomes dead code once the `make_t3()` fallback at line 170 is replaced

## Phase 3 — Cleanup (Deploy Atomically with Phase 2)

### nexus-pjsc.10: Remove make_t3(), _t3(), and dead code

**Files**:
- `src/nexus/db/__init__.py` — deprecate `make_t3()`
- `src/nexus/commands/store.py` — remove `_t3()`
- `src/nexus/search_engine.py` — remove `_t3_for_search()` (lines 25-28)
- `src/nexus/commands/pm.py` — remove dead imports
- `src/nexus/commands/search_cmd.py` — remove dead import (line 8)

**Dependencies**: nexus-pjsc.3 through nexus-pjsc.9 (all routing complete)
**Blocks**: nexus-pjsc.12

Remove:
1. `_t3()` from `commands/store.py`
2. `_t3_for_search()` from `search_engine.py` (dead code — unused)
3. `from nexus.db import make_t3` from `commands/pm.py` line 10
4. `from nexus.commands.store import _t3` from `commands/pm.py` promote_cmd (inline import)
5. `from nexus.commands.store import _t3` from `commands/search_cmd.py` line 8
   — becomes dead code after nexus-pjsc.5 rewrites `search_cmd.py` to use factory functions
6. Mark `make_t3()` as deprecated in `db/__init__.py` (retain for migrate only)

**Test**: Verify `from nexus.commands.store import _t3` raises `ImportError` after
removal. Verify full test suite passes with no `make_t3()` or `_t3()` calls
remaining in non-migrate modules.

## Phase 4 — Migration

### nexus-pjsc.11: Implement nx migrate t3 subcommand

**Files**:
- `src/nexus/commands/migrate.py` — NEW
- `tests/test_migrate.py` — NEW
- `src/nexus/cli.py` — register migrate group

**Test cases**: P7
**Dependencies**: nexus-pjsc.1
**Blocks**: nexus-pjsc.12

Migration algorithm:
1. Source: `PersistentClient(legacy_path)` if `chromadb.path` set; else
   `make_t3()` (CloudClient)
2. Destination: `T3Database(_client=PersistentClient(path), _ef_override=DefaultEmbeddingFunction())`
   -- no `voyage_api_key` required for migration; embeddings copied verbatim
3. For each collection: route by prefix (`code__*` -> code store, etc.)
4. Copy via `get(include=["documents","embeddings","metadatas","ids"])` +
   `upsert(...)` -- no `limit` arg
5. Idempotency: skip if same doc count; upsert if different
6. Per-type count verification post-migration
7. Print report; do not delete source

### nexus-pjsc.12: Update documentation

**Files**: `docs/architecture.md`, `docs/cli-reference.md`
**Dependencies**: nexus-pjsc.10, nexus-pjsc.11
**Blocks**: None (terminal task)

Update:
- Architecture diagram for four-store layout
- CLI reference for `--type` flag on search and collection commands
- CLI reference for `nx migrate t3`
- Release notes: migration ordering, config changes, legacy path alias

## Test Plan Mapping

| Test ID | Description | Task |
|---------|------------|------|
| P1 | Factory returns PersistentClient at correct path | nexus-pjsc.1 |
| P2 | `nx store put` -> knowledge store only | nexus-pjsc.3 |
| P3 | `nx index repo` -> code/docs/rdr in correct stores | nexus-pjsc.8 |
| P4 | `nx search` (no --type) -> all 4 stores, merged | nexus-pjsc.5 |
| P5 | `nx search --type code` -> code store only | nexus-pjsc.5 |
| P6 | `nx search --type code --corpus nexus` -> code + filter | nexus-pjsc.5 |
| P7 | `nx migrate t3` -> per-type counts match | nexus-pjsc.11 |
| P8 | `nx collection list` -> all 4 stores grouped | nexus-pjsc.4 |
| P9 | `nx collection info` default/--type routing | nexus-pjsc.4 |
| P10 | `nx collection delete --type code` | nexus-pjsc.4 |
| P11 | `nx collection verify --type docs` | nexus-pjsc.4 |
| P12 | `promote_cmd` -> knowledge store | nexus-pjsc.6 |
| P13 | `expire_cmd` -> knowledge store only | nexus-pjsc.3 |
| P14 | `_has_credentials()` with voyage_api_key only | nexus-pjsc.2 |
| P15 | `pm_archive()` -> knowledge store | nexus-pjsc.7 |
| P16 | `_discover_and_index_rdrs()` -> rdr store | nexus-pjsc.8 |

## Risk Factors and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Phase 2+3 partial deploy | Data split: some commands use old store, others new | All Phase 2+3 tasks on single feature branch; single PR |
| Indexer rewrite complexity | Subtle routing bugs; wrong store for content type | TDD with P3, P16 tests. Mock each factory individually |
| Fan-out search performance | 4 store opens per query | PersistentClient is lightweight SQLite; monitor in integration tests |
| Migration ordering | Empty new stores if Phase 2+3 deploys before migration | Document in release notes; migration is idempotent |
| Breaking existing tests | Test mocks assume single make_t3() | Update mocks to patch factory functions; existing EphemeralClient pattern works with PersistentClient injection |

## Branch Strategy

```
feature/nexus-pjsc-rdr-004-four-store
  |
  +-- Phase 1 commits (config + factories + credential guards)
  +-- Phase 2 commits (command routing, one per task)
  +-- Phase 3 commit (cleanup)
  +-- Phase 4 commits (migration + docs)
```

All phases on one feature branch. Single PR to main. Phase 2+3 commits must be
consecutive (no interleaving with other work).

## Bead Summary

| Bead ID | Type | Title | Phase |
|---------|------|-------|-------|
| nexus-pjsc | epic | RDR-004: Four-Store T3 Architecture | -- |
| nexus-pjsc.1 | task | P1: Config defaults + factory module | 1 |
| nexus-pjsc.2 | task | P1: Credential guards | 1 |
| nexus-pjsc.3 | task | P2: Store commands -> knowledge | 2 |
| nexus-pjsc.4 | task | P2: Collection commands + --type flag | 2 |
| nexus-pjsc.5 | task | P2: Search command + --type + fan-out | 2 |
| nexus-pjsc.6 | task | P2: Memory promote -> knowledge | 2 |
| nexus-pjsc.7 | task | P2: PM commands -> knowledge | 2 |
| nexus-pjsc.8 | task | P2: Indexer multi-store routing | 2 |
| nexus-pjsc.9 | task | P2: Index commands + doc_indexer | 2 |
| nexus-pjsc.10 | task | P3: Remove dead code | 3 |
| nexus-pjsc.11 | task | P4: Migration command | 4 |
| nexus-pjsc.12 | task | P4: Documentation updates | 4 |
