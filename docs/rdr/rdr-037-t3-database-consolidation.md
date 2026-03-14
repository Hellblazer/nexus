---
title: "T3 Database Consolidation"
id: RDR-037
type: enhancement
status: accepted
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-14
accepted_date: 2026-03-14
related_issues:
  - "RDR-004 - Four Store Architecture"
  - "RDR-005 - ChromaDB Cloud Quota Enforcement"
---

# RDR-037: T3 Database Consolidation

## Problem Statement

T3 storage currently uses four separate ChromaDB Cloud databases — `{base}_code`, `{base}_docs`, `{base}_rdr`, `{base}_knowledge` — one per content type. This was the original design (RDR-004) but the separation provides no technical benefit: collection names already carry type prefixes (`code__`, `docs__`, `rdr__`, `knowledge__`) that prevent collisions, and embedding functions are configured per-collection, not per-database.

The four-database design creates unnecessary friction:

- **Setup**: users must create 4 databases in the ChromaDB Cloud dashboard
- **Auto-provisioning**: `nx` must provision and verify 4 databases
- **Connection overhead**: `T3Database.__init__` creates 4 `CloudClient` instances
- **Routing complexity**: `_client_for()` dispatches by prefix to the correct client
- **Enumeration**: `list_collections()` fans out across 4 clients and deduplicates
- **Diagnostics**: `nx doctor` checks 4 databases

A single database with the same prefixed collections would be simpler to set up, simpler to code, and no less capable.

## Context

The four-database split was introduced in RDR-004 to separate content types with different embedding models. At the time, the concern was that mixing embedding spaces within a single database could cause confusion or cross-contamination. In practice, ChromaDB isolates collections completely — different collections in the same database share nothing. The embedding function is attached at the collection level, and queries only hit the specified collection.

### Current model assignments

| Collection prefix | Index model | Query model |
|---|---|---|
| `code__` | `voyage-code-3` | `voyage-4` |
| `docs__` | `voyage-context-3` (CCE) | `voyage-context-3` (CCE) |
| `rdr__` | `voyage-context-3` (CCE) | `voyage-context-3` (CCE) |
| `knowledge__` | `voyage-context-3` (CCE) | `voyage-context-3` (CCE) |

These assignments are determined by `corpus.py` functions (`index_model_for_collection`, `embedding_model_for_collection`) based on the collection name prefix — not the database. Consolidation changes nothing about how embeddings work.

## Research Findings

### F-01: ChromaDB collections are fully isolated within a database
- **Classification**: Verified — Documentation + Testing
- **Detail**: ChromaDB collections within a single database share no state. Each collection has its own embedding function, its own document store, and its own vector index. Querying collection A never touches collection B. The database is purely an organizational namespace.

### F-02: Collection name prefixes already prevent collisions
- **Classification**: Verified — Source Code
- **Detail**: All collection names follow `{type}__{repo}-{hash8}` format (e.g. `code__nexus-a1b2c3d4`). The `__` separator and type prefix guarantee uniqueness across content types within a single database. See `registry.py:_safe_collection()`.

### F-03: Routing logic exists only because of the multi-database design
- **Classification**: Verified — Source Code
- **Detail**: `T3Database._client_for()` (t3.py:110-133) parses the collection prefix to select one of four `CloudClient` instances. With a single client, this method becomes a no-op. The `_STORE_TYPES` tuple and the init loop that creates 4 clients also become unnecessary.

### F-04: ChromaDB Cloud limits are per-collection and per-tenant, not per-database
- **Classification**: Verified — ChromaDB Cloud Documentation (docs.trychroma.com/cloud/quotas-limits)
- **Detail**: The relevant limits are: max 1,000,000 collections (tenant-wide), max 5,000,000 records per collection, max 10 concurrent reads and 10 concurrent writes per collection, max 300 records per write. None of these are per-database. Consolidating into one database changes no limits.

### F-05: Four databases consume 40% of free-tier database slots
- **Classification**: Verified — ChromaDB Cloud Pricing (trychroma.com/pricing)
- **Detail**: The Starter (free) tier allows 10 databases. Nexus currently uses 4, leaving only 6 for other projects or tools. Consolidating to 1 frees 3 slots — a 30% increase in available capacity. Team tier allows 100, Enterprise unlimited. No per-database cost on any tier.

### F-06: RDR-004's original rationale does not hold
- **Classification**: Verified — Documentation cross-reference
- **Detail**: RDR-004 justified four databases on two grounds: (1) "operational coupling" — shared quota and rate limit contention, and (2) "scale ceiling" — per-database resource limits. F-04 shows both are incorrect: rate limits are per-collection (not per-database), and collection caps are tenant-wide (not per-database). The split provides no isolation benefit.

### F-07: `nx migrate t3` was already removed
- **Classification**: Verified — Source Code
- **Detail**: The original migration command from single→four databases was removed in a prior release. Only a stale comment in `t3.py:500` references it. A new consolidation migration would need to be purpose-built or use the existing `nx store export/import` path.

### F-08: Test injection already maps all four clients to one mock
- **Classification**: Verified — Source Code
- **Detail**: `T3Database.__init__` with `_client=mock` already sets `self._clients = {t: _client for t in _STORE_TYPES}` — i.e., all four store types share one client. The entire test suite already exercises single-client behavior. Consolidation aligns production with what tests already do.

### F-09: Change scope is well-bounded — 6 source files, 5 test files, 6 doc files
- **Classification**: Verified — Source Code analysis
- **Detail**: Core changes: `t3.py` (5 locations: init, `_client_for` retained as shim, expire docstring, list_collections, class docstring), `_provision.py` (rewrite ensure_databases), `doctor.py` (simplify 3 check loops), `config_cmd.py` (update help text). Minor: `store.py` (error messages), `exporter.py` (uses `_client_for` shim — no change needed). Tests: `test_provision.py` (6 tests), `test_t3.py` (12 routing tests to remove/simplify), `test_config_cmd.py` (2 tests), `test_integration.py` (1 test), `test_exporter.py` (7 calls use `_client_for` shim — no change needed). Unchanged: `corpus.py`, `registry.py`, `indexer.py`, `code_indexer.py`, all search/memory/scratch code. Docs: 6 files (storage-tiers, configuration, getting-started, cli-reference, architecture, CLAUDE.md).

## Proposed Approach

### Single database, same collections

Replace the four `CloudClient` instances with one. The `chroma_database` config value becomes the actual database name (currently it's a base that gets `_code`, `_docs`, `_rdr`, `_knowledge` suffixed). The default database name for new installs is `nexus`.

### `_client_for()` retained as compatibility shim

`_client_for(collection_name)` is kept as a method that returns `self._client` regardless of input. This preserves the call signature used by `exporter.py` (line 128) and `test_exporter.py` (7 call sites) without requiring changes to those files. All prefix-parsing logic and log warnings are removed — the method body is simply `return self._client`. The shim can be removed in a future release once all callers migrate to `self._client` directly.

### Auto-detection of old four-database layout

`CloudClient` is eager — it connects at construction time and raises `RuntimeError` on failure (documented in RDR-004). For an upgrading user whose `chroma_database = nexus`, the new single-database init would try to connect to `nexus` (which doesn't exist), fail before any migration probe could run, and show a cryptic connection error instead of migration guidance.

To prevent this, the probe runs **before** the primary client connection. The init sequence is:

1. **Probe first**: attempt `CloudClient(database="{base}_code")`. If this succeeds, the old four-database layout is detected.
2. **Old layout detected**: emit a structured warning with migration steps, then raise `OldLayoutDetected` (a custom error subclass) to prevent silent operation against stale data. No client is assigned — all CLI commands exit with the migration message.
3. **Probe fails (404/NotFound)**: old layout does not exist. Proceed to connect to `{base}` as the single database.
4. **Probe fails (other error)**: auth/network errors are wrapped in `RuntimeError` with a diagnostic message so CLI callers surface clean output.

The migration is **non-destructive** — old databases are never modified or deleted. They remain in the ChromaDB Cloud dashboard until the user chooses to remove them.

To migrate (export must happen **before** upgrading):

```
  1. nx store export --all           # back up knowledge entries (pre-upgrade version)
  2. Upgrade nexus
  3. nx config init                  # provisions single '{base}' database
  4. nx index repo .                 # re-index code, docs, and RDRs
  5. nx store import <exported-file> # restore knowledge entries
  6. export NX_MIGRATED=1            # or: nx config set migrated 1
  7. nx doctor                       # verify everything works
  8. (Optional) delete old {base}_code, {base}_docs, {base}_rdr, {base}_knowledge
```

Once the user sets the migration flag (step 6), subsequent runs skip the probe and connect directly to the single database.

`nx doctor` also surfaces this warning when the old layout is detected.

### RDR-005 compatibility

RDR-005's `_write_sems` and `_read_sems` are keyed by collection name, not by database. ChromaDB Cloud's concurrency limits (10 reads, 10 writes) are per-collection (F-04). Database consolidation does not change the semaphore key space or the limit enforcement. No changes to RDR-005's implementation are required.

### Changes required

**Source (4 files, core logic):**
1. **`db/t3.py`** — remove `_STORE_TYPES`, replace `self._clients` dict with `self._client`, retain `_client_for()` as shim returning `self._client`, update `expire()` docstring (remove stale `nx migrate t3` reference), update `list_collections()` to single client call, add old-layout auto-detection probe, update class docstring
2. **`commands/_provision.py`** — `ensure_databases()` creates 1 database instead of 4, remove `_STORE_TYPES` import
3. **`commands/doctor.py`** — single database reachability check, simplify pipeline version and pagination audit loops, add old-layout detection warning
4. **`commands/config_cmd.py`** — update help text from "four databases" to "single database"

**Source (2 files, minor):**
5. **`exporter.py`** — no change needed (`_client_for()` shim preserves call signature)
6. **`commands/store.py`** — update error message wording

**Unchanged:** `corpus.py`, `registry.py`, `indexer.py`, `code_indexer.py`, `search_engine.py`, `db/__init__.py`

**Tests (5 files):**
7. **`test_t3.py`** — remove 12 routing tests, simplify `four_clients` fixture, add old-layout detection test
8. **`test_provision.py`** — rewrite 6 tests for single database
9. **`test_config_cmd.py`** — update 2 provisioning tests
10. **`test_integration.py`** — update 1 migration test
11. **`test_exporter.py`** — no change needed (`_client_for()` shim preserves call signature)

**Docs (6 files):** `storage-tiers.md`, `configuration.md`, `getting-started.md`, `cli-reference.md`, `architecture.md`, `CLAUDE.md`

### Migration strategy

Auto-detection at startup ensures no user is caught unaware. The probe-first design raises `OldLayoutDetected` with migration guidance before any operation proceeds.

1. **`knowledge__` entries** — `nx store export --all` must be run with the **pre-upgrade** version (before `OldLayoutDetected` exists). This is the only non-rederivable data.
2. **Code/docs/rdr collections** are derived from repo files — `nx index repo .` recreates them in the new single database.
3. **Migration flag** — setting `NX_MIGRATED=1` or `nx config set migrated 1` tells the init to skip the old-layout probe and connect directly to the single database.
4. **Non-destructive** — the old four databases are never modified or deleted. They remain in the ChromaDB Cloud dashboard until the user manually removes them. They cost no compute, only storage slots.

There is no automatic migration command. The export/import path already exists and is well-tested (RDR-031). The auto-detection warning guides users through the steps.

### Rejected alternatives

- **Keep four databases**: status quo. Works but adds unnecessary setup friction and code complexity with no benefit. Wastes 3 of the free tier's 10 database slots.
- **Merge only CCE databases (docs + rdr + knowledge), keep code separate**: marginally simpler than status quo but still requires multi-database routing for one special case.
- **Automatic migration command**: higher implementation cost and risk (copying embeddings across databases) for a one-time operation that export/import already handles. Not worth the complexity.
- **Support both layouts indefinitely**: dual-path code is the opposite of simplification. Auto-detection with a clear migration warning is sufficient.

## Consequences

### Positive

- Setup cost drops from 4 databases to 1 — especially significant on free tier (40% → 10% of database slots)
- `T3Database` code simplifies substantially (remove routing, dedup, multi-client init)
- `nx doctor` and auto-provisioning become simpler and faster
- Production behavior aligns with what the test suite already exercises (F-08)

### Negative / Trade-offs

- **Single blast radius**: a database-level failure or accidental deletion now affects all T3 content simultaneously, whereas the four-database design provided per-content-type isolation. This is acceptable because: CloudClient is stateless REST (no persistent connection to lose), there are no per-database rate limits (F-04), and `nx store export --all` provides a backup path. The isolation benefit of four databases was theoretical — rate limits and quotas are per-collection regardless.
- **Breaking config change**: `chroma_database` semantics change from "base prefix" to "actual database name." The probe-first auto-detection ensures upgrading users see a clear migration warning (with a working export path) rather than a cryptic connection error.

## Implementation Plan

Steps 1–2 must land atomically (same commit) since the code is non-functional between removing the four-client init and adding the probe-first single-client init.

1. **`T3Database.__init__` rewrite (probe-first)** — the init sequence becomes: (a) check migration flag → if set, connect to `{base}` directly; (b) probe for `{base}_code` → if found, temporarily connect all four old databases, emit warning, raise `OldLayoutDetected`; (c) probe fails → connect to `{base}` as single database. Remove `_STORE_TYPES`. Retain `_client_for()` as shim (`return self._client`, all prefix-parsing and warning log lines removed). Update class docstring.
2. **`expire()` docstring** — remove stale `nx migrate t3` reference (currently at t3.py:500), update to reflect single-database layout. Also update `self._clients["knowledge"]` → `self._client`.
3. **Auto-provisioning** — `ensure_databases()` creates 1 database; database name is `chroma_database` value directly (default: `nexus`). Remove `_STORE_TYPES` import.
4. **`nx doctor`** — check 1 database; add old-layout detection warning; simplify pipeline version and pagination audit loops.
5. **Config docs + help text** — update `chroma_database` semantics in `config_cmd.py`, `configuration.md`, `getting-started.md`
6. **Other docs** — update `storage-tiers.md`, `cli-reference.md`, `architecture.md`, `CLAUDE.md`
7. **Tests** — remove 12 routing tests from `test_t3.py`, rename `four_clients` fixture, rewrite 6 provisioning tests, add old-layout detection + migration-flag tests; `test_exporter.py` unchanged (shim preserves call signature)
8. **CHANGELOG + release notes** — document breaking change with migration instructions

## Resolved Questions

1. **Backward compatibility**: probe-first auto-detection. `T3Database.__init__` probes for `{base}_code` *before* attempting the single-database connection. If old layout detected, temporarily connects all four databases (so `nx store export --all` works), emits migration warning, and raises `OldLayoutDetected`. A migration flag (`NX_MIGRATED=1` or `nx config set migrated true`) skips the probe on subsequent runs.
2. **Default database name**: `nexus` — matches what users already have as their base prefix. New installs get `nexus` as the single database name. Existing users create a new database named `nexus` (the unsuffixed name) and migrate.
3. **`_client_for()` fate**: retained as a compatibility shim with body `return self._client`. All prefix-parsing logic and log warnings removed. Preserves `exporter.py` and `test_exporter.py` call signatures without changes.
