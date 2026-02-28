# RDR-004 Four-Store T3 Architecture — Design

**Date:** 2026-02-28
**Status:** Approved for implementation

## Context

The previous RDR-004 implementation was reverted because it mistakenly used
`chromadb.PersistentClient` (local stores) when the intent was always four
separate **ChromaDB Cloud** (CloudClient) databases — one per content type.

## Core Mechanic

`chroma_database` is the **base name**. Nexus derives four database names by
appending `_code`, `_docs`, `_rdr`, `_knowledge`:

```
chroma_database = "nexus"
  → nexus_code      (code__* collections)
  → nexus_docs      (docs__* collections)
  → nexus_rdr       (rdr__* collections)
  → nexus_knowledge (knowledge__* collections)
```

**No new credentials.** The same `chroma_api_key`, `chroma_tenant`, and
`chroma_database` are shared by all four stores.

Users create four databases in their ChromaDB Cloud dashboard with these
derived names before first use.

## Design Principle: Abstraction in the Right Place

The routing belongs **inside `T3Database`**. The public API of `T3Database`,
`make_t3()`, and every caller (`search_cmd.py`, `indexer.py`, `pm.py`,
`commands/store.py`, etc.) remain **completely unchanged**.

This is the minimal change that achieves four-store routing with no ripple
across the codebase.

## Implementation

### Phase 1 — T3Database routing refactor (`src/nexus/db/t3.py`)

Replace `self._client: chromadb.CloudClient` with
`self._clients: dict[str, chromadb.CloudClient]`:

```python
if _client is not None:
    # Test injection: single mock serves all types
    self._clients = {t: _client for t in ("code", "docs", "rdr", "knowledge")}
else:
    self._clients = {
        t: chromadb.CloudClient(
            tenant=tenant, database=f"{database}_{t}", api_key=api_key
        )
        for t in ("code", "docs", "rdr", "knowledge")
    }
```

Add routing helper:

```python
def _client_for(self, collection_name: str):
    prefix = collection_name.split("__")[0] if "__" in collection_name else "knowledge"
    return self._clients.get(prefix, self._clients["knowledge"])
```

Update every `self._client.xxx(name, ...)` callsite to
`self._client_for(name).xxx(name, ...)`.

Special cases:
- `list_collections()`: fan out across all four clients (existing thread pool)
- `expire()`: only needs `self._clients["knowledge"]` (already filters for
  `knowledge__` prefix)

### Phase 2 — Migration command (`src/nexus/commands/migrate.py` + `cli.py`)

`nx migrate t3` copies collections from the old single-database store to the
four new typed stores.

- **Source**: `T3Database` pointed at `chroma_database` with NO suffix (old
  single store). Uses the existing `make_t3()` but with an explicit
  `database=get_credential("chroma_database")` override via a direct
  `T3Database(...)` call.
- **Destination**: `make_t3()` (new four-store routing)
- **Routing**: collection prefix determines destination store
  (`code__*` → code store, etc.)
- **Copy method**: `col.get(include=["documents","metadatas","embeddings"])` +
  `dest_col.upsert(ids=..., documents=..., embeddings=..., metadatas=...)`
  — embeddings copied verbatim, no re-embedding
- **Idempotent**: skip a collection when destination count already equals
  source count
- **Non-destructive**: source store is never deleted
- Print per-collection progress and a final summary

### Phase 3 — Tests

For Phase 1:
- `tests/test_t3.py`: verify routing — a `T3Database` with 4 mock clients
  routes `code__x` to the code client, `docs__x` to the docs client, etc.
- `tests/test_t3.py`: verify `list_collections()` combines results from all
  four clients
- `tests/test_t3.py`: verify `expire()` only touches the knowledge client

For Phase 2:
- `tests/test_migrate_cmd.py`: migrate routes each collection prefix to the
  correct destination store
- `tests/test_migrate_cmd.py`: migrate is idempotent (same count → skip)
- `tests/test_migrate_cmd.py`: migrate copies embeddings verbatim

### Phase 4 — Documentation

- `docs/storage-tiers.md`: update T3 table to show four separate cloud databases
- `docs/getting-started.md`: mention four databases to create in ChromaDB Cloud
- `docs/configuration.md`: explain base-name derivation
- `docs/rdr/`: restore `rdr-004-four-store-architecture.md` with correct design
- `CHANGELOG.md`: add rc5 entry for four-store architecture

## What Does NOT Change

- `src/nexus/db/__init__.py` (`make_t3()`) — unchanged
- `src/nexus/commands/store.py` (`_t3()`) — unchanged
- `src/nexus/commands/search_cmd.py` — unchanged
- `src/nexus/commands/collection.py` — unchanged
- `src/nexus/commands/index.py` — unchanged
- `src/nexus/commands/memory.py` — unchanged
- `src/nexus/commands/pm.py` — unchanged
- `src/nexus/pm.py` — unchanged
- `src/nexus/indexer.py` — unchanged
- `src/nexus/doc_indexer.py` — unchanged
- `src/nexus/search_engine.py` — unchanged

## Test Plan (TDD)

Each phase follows RED → GREEN → REFACTOR.

**P1** `test_t3_routes_code_collection_to_code_client`
**P2** `test_t3_routes_docs_collection_to_docs_client`
**P3** `test_t3_routes_rdr_collection_to_rdr_client`
**P4** `test_t3_routes_knowledge_collection_to_knowledge_client`
**P5** `test_t3_unknown_prefix_falls_back_to_knowledge_client`
**P6** `test_t3_list_collections_fans_out_across_all_four_clients`
**P7** `test_t3_expire_only_touches_knowledge_client`
**P8** `test_t3_single_mock_injection_still_works` (backward compat)
**P9** `test_migrate_routes_code_collection_to_code_store`
**P10** `test_migrate_routes_docs_collection_to_docs_store`
**P11** `test_migrate_is_idempotent_when_counts_match`
**P12** `test_migrate_copies_embeddings_verbatim`

## Risks

- **ChromaDB Cloud database creation**: CloudClient will error if the database
  doesn't exist (ChromaDB Cloud does not auto-create databases). The error
  message should guide users to create the four databases. Consider surfacing
  a clear `nx doctor` check.
- **Backward compat for existing single-store users**: `make_t3()` now
  connects to `{database}_code` etc. instead of `{database}`. Users with
  existing data must run `nx migrate t3` before upgrading. Document prominently.
- **Test injection**: the `_client=mock` path maps all four types to the same
  mock — this is correct for tests that don't care about routing, but routing
  tests need four distinct mocks.
