---
title: "Four-Store T3 Architecture"
type: architecture
status: closed
priority: P1
author: Hal Hildebrand
date: 2026-02-28
accepted_date: 2026-02-28
close_date: 2026-02-28
close_reason: implemented
reviewed-by: self
related_issues: []
---

# RDR-004: Four-Store T3 Architecture

## Problem

The original T3 implementation used a single `chromadb.CloudClient` pointed at one
ChromaDB Cloud database. All four collection types (`code__*`, `docs__*`, `rdr__*`,
`knowledge__*`) lived in the same database, with the collection name prefix being the
only logical separator.

This created two problems:

1. **Operational coupling**: A single database means all traffic (code search, knowledge
   retrieval, TTL expiry) shares one ChromaDB quota and can contend for rate limits.
2. **Scale ceiling**: ChromaDB Cloud databases have per-database resource limits. Splitting
   by content type allows each type to grow independently and hit different limits.

## Decision

Replace the single `CloudClient` in `T3Database` with a dict of four `CloudClient`
instances, each connected to a separate ChromaDB Cloud database named after the content
type:

| Store type | Database | Collections |
|-----------|----------|-------------|
| `code` | `{base}_code` | `code__*` |
| `docs` | `{base}_docs` | `docs__*` |
| `rdr` | `{base}_rdr` | `rdr__*` |
| `knowledge` | `{base}_knowledge` | `knowledge__*` |

Where `{base}` is the value of `CHROMA_DATABASE` / `chroma_database` credential.

**All routing is internal to `T3Database`.** No caller outside `t3.py` changes. The public
API of `T3Database` and `make_t3()` remains identical.

## Design

### Routing

The `_client_for(collection_name: str)` helper splits the collection name on `__` and
uses the prefix to look up the correct client. Unknown prefixes fall back to the
`knowledge` client with a warning log entry.

```python
def _client_for(self, collection_name: str) -> object:
    prefix = collection_name.split("__")[0] if "__" in collection_name else "knowledge"
    client = self._clients.get(prefix)
    if client is None:
        _log.warning("unknown_collection_prefix", prefix=prefix, collection=collection_name)
        client = self._clients["knowledge"]
    return client
```

### Construction

`T3Database.__init__` creates the four clients in a loop. Each failed connection raises
a `RuntimeError` with a clear message listing all four expected database names:

```python
for t in _STORE_TYPES:
    db_name = f"{database}_{t}"
    try:
        _clients[t] = chromadb.CloudClient(tenant=tenant, database=db_name, api_key=api_key)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to connect to ChromaDB Cloud database {db_name!r}: {exc}\n"
            f"Ensure these four databases exist in your ChromaDB Cloud dashboard:\n"
            + "\n".join(f"  - {database}_{t2}" for t2 in _STORE_TYPES)
        ) from exc
```

### Test injection

The existing `_client=mock` injection path continues to work: when `_client` is
provided, all four store types are mapped to the same client:

```python
if _client is not None:
    self._clients = {t: _client for t in _STORE_TYPES}
```

This preserves 100% backward compatibility with the existing test suite that uses
`conftest.py`'s `local_t3` fixture.

### `expire()` semantics

`expire()` only processes `knowledge__*` collections, so it only touches the knowledge
client (`self._clients["knowledge"]`). If the knowledge database does not yet exist,
it logs a warning and returns 0 rather than raising.

### `list_collections()` fan-out

`list_collections()` iterates all four clients and deduplicates using a `seen` set.
Deduplication is necessary because the single-mock injection path maps all four type
keys to the same client, which would otherwise return duplicates.

### Doctor check

`nx doctor` checks all four databases when credentials are present, reporting each as
reachable or not reachable. This surfaces misconfigured or missing databases before
the user runs `nx index`.

### Migration

`nx migrate t3` copies collections from the old single-database store to the new
four-store layout. The command:

1. Connects to the source via a raw `chromadb.CloudClient(database=chroma_database)`
   (the old unsuffixed name, e.g. `nexus`).
2. Connects to the destination via `make_t3()` (new four-store routing).
3. For each source collection: copies documents, metadata, and embeddings verbatim.
4. Idempotent: skips collections where destination count matches source count.

## Consequences

### Positive

- Each content type has dedicated throughput and quota headroom.
- Future shard-per-repo scaling becomes straightforward (create more `code__*` in
  `{base}_code`).
- `expire()` is cheaper — only queries the knowledge database, not all four.
- Clear operational separation for debugging and monitoring.

### Negative / Trade-offs

- Setup cost: users must create four databases in the ChromaDB Cloud dashboard instead
  of one.
- `T3Database.__init__` makes four HTTP connections (one per CloudClient) instead of
  one. Startup latency increases slightly for commands that create a `T3Database`.
- `nx doctor` makes four additional HTTP calls when credentials are present.

## Research Findings

- ChromaDB `CloudClient` is eager: it makes a network call at construction time. The
  old single-database tests that patched `CloudClient` return value needed updating to
  expect four calls.
- `list_collections()` in ChromaDB Cloud returns `Collection` objects (not just names)
  in some versions; the implementation handles both via `isinstance` check.
- Integration tests skip cleanly when the four databases don't exist, using a
  `_t3_reachable()` dynamic check cached as `_T3_AVAILABLE`.

## Implementation Plan

Implemented in two phases on branch `feature/rdr-003-collection-resolution`:

**Phase 1** (complete): `T3Database` routing refactor in `src/nexus/db/t3.py`.
Tests: P1–P8 in `tests/test_t3.py`.

**Phase 2** (complete): Migration command `nx migrate t3` in
`src/nexus/commands/migrate.py`. Tests: P9–P12 in `tests/test_migrate_cmd.py`.

See `docs/plans/2026-02-28-rdr-004-four-store-cloud-impl-plan.md` for the detailed
task breakdown.
