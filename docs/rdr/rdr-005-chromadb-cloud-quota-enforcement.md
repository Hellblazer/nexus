---
title: "ChromaDB Cloud Quota Enforcement"
type: architecture
status: closed
close_reason: implemented
priority: P1
author: Hal Hildebrand
date: 2026-02-28
accepted_date: 2026-02-28
closed_date: 2026-02-28
reviewed-by: self
related_issues: []
---

# RDR-005: ChromaDB Cloud Quota Enforcement

## Problem

Nexus uses ChromaDB Cloud for T3 storage across all four databases (code, docs, rdr, knowledge).
ChromaDB Cloud enforces hard quotas at the API level — when exceeded, operations fail with
HTTP 429 or structured error responses. Currently, Nexus has no client-side enforcement of
these limits, which means:

1. **Silent failures at boundaries**: A write of 301 records is rejected wholesale; the user
   gets an error with no indication that batching would fix it.
2. **Wasted round-trips**: Oversized payloads travel over the network before being rejected.
3. **Concurrency violations**: Nexus can spawn more than 10 concurrent reads or writes to a
   single collection, triggering rate-limiting errors the user sees as transient failures.
4. **Dimension/byte overruns**: Embeddings with >4,096 dimensions or documents >16,384 bytes
   are rejected at upsert time; early validation would surface these as clear config errors.
5. **Query clause explosion**: Queries built programmatically can silently exceed the 8
   `where` predicate limit, producing cryptic API errors instead of clear bounds violations.

The result is an unreliable UX: the user experiences intermittent, hard-to-diagnose errors
rather than clear, actionable feedback.

## Quotas Reference

Source: https://docs.trychroma.com/cloud/quotas-limits

### Data Size Limits

| Field | Limit |
|-------|-------|
| Embedding dimensions | 4,096 |
| Document size | 16,384 bytes |
| URI length | 256 bytes |
| ID length | 128 bytes |
| Database name length | 128 bytes |
| Collection name length | 128 bytes |
| Metadata key length | 36 bytes |
| Record metadata value size | 4,096 bytes |
| Collection metadata value size | 256 bytes |
| Record metadata keys per record | 32 |
| Collection metadata keys per collection | 32 |

### Query & Search Limits

| Field | Limit |
|-------|-------|
| Full-text / regex search input | 256 characters |
| Where predicates per query | 8 |
| Max results returned | 300 records |

### Concurrency Limits

| Field | Limit |
|-------|-------|
| Concurrent reads per collection | 10 |
| Concurrent writes per collection | 10 |

### Scale Limits

| Field | Limit |
|-------|-------|
| Collections per account | 1,000,000 |
| Records per collection | 5,000,000 |
| Records per write operation | 300 |
| Fork edges from root | 4,096 |

## Goals

1. Enforce all ChromaDB Cloud quotas **client-side** before any network call.
2. Automatically batch writes that exceed the 300-record-per-operation limit.
3. Cap concurrency at 10 reads and 10 writes per collection using semaphores.
4. Validate field sizes (embedding dimensions, document bytes, ID/URI/key lengths) at
   record construction time with clear, actionable error messages.
5. Validate query parameters (where clause count, result limit, query string length) before
   issuing queries.
6. Expose quota constants in a single canonical location so they can be updated when
   ChromaDB changes its limits without hunting through call sites.

## Non-Goals

- Dynamic quota discovery via the ChromaDB API (not currently supported).
- Quota increase requests (handled out-of-band with Chroma support).
- T1 (ephemeral) or T2 (SQLite) enforcement — these have no ChromaDB Cloud quotas.
- Retry logic or backoff (separate concern from enforcement).

## Design

### 1. Quota Constants Module

Create `src/nexus/db/chroma_quotas.py` containing a single frozen dataclass `ChromaQuotas`
with all limit values as class-level constants. This is the single source of truth —
no magic numbers scattered across call sites.

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import dataclass

@dataclass(frozen=True)
class ChromaQuotas:
    # Data size
    MAX_EMBEDDING_DIMENSIONS: int = 4_096
    MAX_DOCUMENT_BYTES: int = 16_384
    MAX_URI_BYTES: int = 256
    MAX_ID_BYTES: int = 128
    MAX_DB_NAME_BYTES: int = 128
    MAX_COLLECTION_NAME_BYTES: int = 128
    MAX_METADATA_KEY_BYTES: int = 36
    MAX_RECORD_METADATA_VALUE_BYTES: int = 4_096
    MAX_COLLECTION_METADATA_VALUE_BYTES: int = 256
    MAX_RECORD_METADATA_KEYS: int = 32
    MAX_COLLECTION_METADATA_KEYS: int = 32

    # Query
    MAX_QUERY_STRING_CHARS: int = 256
    MAX_WHERE_PREDICATES: int = 8
    MAX_QUERY_RESULTS: int = 300

    # Concurrency
    MAX_CONCURRENT_READS: int = 10
    MAX_CONCURRENT_WRITES: int = 10

    # Scale
    MAX_RECORDS_PER_WRITE: int = 300
    MAX_RECORDS_PER_COLLECTION: int = 5_000_000
    MAX_COLLECTIONS_PER_ACCOUNT: int = 1_000_000

QUOTAS = ChromaQuotas()
```

### 2. Quota Validator

`src/nexus/db/chroma_quotas.py` contains both `ChromaQuotas`/`QUOTAS` and the full
`QuotaValidator` class plus the `QuotaViolation` error hierarchy. Keeping them in one file
avoids a circular import and keeps the single source of truth self-contained.

`QuotaValidator` checks individual records and query parameters against `QUOTAS`. Methods
raise the appropriate `QuotaViolation` subclass with a human-readable message identifying
the field, the value's actual size, and the limit.

Key validation methods:
- `validate_record(id, document, embedding, metadata, uri=None)` — checks all per-record limits
- `validate_collection_metadata(metadata)` — checks collection-level metadata limits
- `validate_query(query_text, where, n_results)` — checks query parameter limits
- `validate_collection_name(name)` and `validate_db_name(name)` — name length checks (both
  the 3–63 character structural rule and the 128-byte Cloud byte limit via `len(name.encode())`)

The validator is **pure** (no I/O, no ChromaDB imports) so it can be tested without a
live client.

### 3. Auto-Batching Write Helpers

*Open Question 1 resolved by Research Finding 8*: no new wrapper class is needed. Quota
enforcement is added as private helper methods directly inside `T3Database` in
`src/nexus/db/t3.py`, keeping all T3 logic in one file.

Three private helpers cover all write and delete paths:

- **`_write_batch(col, ids, documents, metadatas, embeddings=None)`** — validates each
  record via `_validate_record()`, then splits the payload into chunks of at most
  `QUOTAS.MAX_RECORDS_PER_WRITE` (300), dispatching each chunk to `col.upsert()` sequentially.
  Raises immediately on any chunk failure, including a `records_written` count in the exception.
  Upsert is idempotent — callers may safely retry the full call on failure.

- **`_delete_batch(col, ids)`** — splits `ids` into chunks of at most
  `QUOTAS.MAX_RECORDS_PER_WRITE` (300) and calls `col.delete()` sequentially per chunk.
  Delete is **not** idempotent; partial-delete failures raise immediately with the count of
  IDs successfully deleted.

- **`_validate_record(id, document, embedding, metadata, uri=None)`** — checks all
  per-record field limits before any network call. Raises `QuotaViolation` subclass with
  `field`, `actual`, `limit`, and `hint` attributes.

All existing call sites (`upsert_chunks()`, `upsert_chunks_with_embeddings()`,
`update_chunks()`, `expire()`, `delete_by_source()`) are updated to route through these
helpers.

**Migration path**: `src/nexus/commands/migrate.py` calls `dest_col.upsert()` directly on
a raw ChromaDB collection object with `_PAGE_SIZE = 5_000`, bypassing `T3Database`. This
path must be updated in Phase 2 to call `T3Database.upsert_chunks_with_embeddings()` (which
will then route through `_write_batch()`) rather than the raw collection API.

### 4. Concurrency Semaphores

*Open Question 2 resolved by Research Finding 7*: T3 is entirely synchronous (no asyncio).
`asyncio.Semaphore` is not used here.

`T3Database` holds two dicts of `threading.BoundedSemaphore`, lazily initialized on first
collection access and keyed by collection name:

```python
self._read_sems: dict[str, threading.BoundedSemaphore] = {}
self._write_sems: dict[str, threading.BoundedSemaphore] = {}

def _read_sem(self, name: str) -> threading.BoundedSemaphore:
    return self._read_sems.setdefault(name, threading.BoundedSemaphore(QUOTAS.MAX_CONCURRENT_READS))

def _write_sem(self, name: str) -> threading.BoundedSemaphore:
    return self._write_sems.setdefault(name, threading.BoundedSemaphore(QUOTAS.MAX_CONCURRENT_WRITES))
```

The `_read_sem` is acquired (as a context manager) before any `col.query()` or `col.get()`
call. The `_write_sem` is acquired before any `col.upsert()`, `col.update()`, or `col.delete()`
call (including each chunk in `_write_batch()` and `_delete_batch()`).

**Semaphores are never held simultaneously.** Read and write operations on a collection are
mutually exclusive at the application level — acquiring both would deadlock.

**Thread safety of `setdefault()`**: In CPython 3.12, the GIL makes `dict.setdefault()` effectively
atomic — two racing callers will get the same `BoundedSemaphore` (the loser's is GC'd). This is
correct for the target runtime. If Nexus is ever run under CPython 3.13+ free-threaded mode (PEP 703),
this becomes a data race and a `threading.Lock` on the dict will be needed.

**`list_collections()` exemption**: The `ThreadPoolExecutor(max_workers=8)` in
`list_collections()` issues `col.count()` reads directly on raw collection objects, bypassing
the semaphore. This is intentional: it is a non-hot-path administrative operation where each
collection receives at most one `count()` call per invocation (8 < 10 per-collection limit).
However, two concurrent invocations of `list_collections()` would issue up to 16 concurrent
reads on the same collection, violating the limit. Therefore: `list_collections()` must not
be called concurrently with itself or with hot-path read operations on the same collections.
If stronger enforcement is required in future, wrap the `ThreadPoolExecutor` block with a
module-level lock.

### 5. Query and Get Parameter Validation

**`query()` calls** — before dispatching `col.query()`:
- Count top-level keys in the `where` dict as an interim approach (see Open Question 3).
  This is **potentially permissive for compound `$and`/`$or` filters**: a single `{"$and": [...9 preds...]}`
  has only 1 top-level key, so our validator passes it while ChromaDB (if it counts leaf
  predicates) may reject it at the API. Empirical verification in Phase 3 will determine
  the correct counting method. Until then, top-level counting prevents the most common case
  (many flat predicates) while acknowledging it may under-reject deeply nested filters.
  Raise `TooManyPredicates` if top-level count exceeds `QUOTAS.MAX_WHERE_PREDICATES` (8).
- Clamp `n_results` to `min(requested, QUOTAS.MAX_QUERY_RESULTS)` (300) and emit a
  `structlog` warning if clamped; do not silently truncate.
- Validate query text length (≤ `QUOTAS.MAX_QUERY_STRING_CHARS`, 256 chars) for full-text
  search calls.

**`get()` calls** — `col.get()` is also subject to the 300-result cap and must not be called
without an explicit `limit`. Three call sites require remediation:

- **`expire()`** (line 404): replace the single `col.get(where=ttl_where)` with a paginated
  loop that fetches pages of at most `QUOTAS.MAX_QUERY_RESULTS` IDs, accumulating them before
  the delete step (or deleting each page via `_delete_batch()` immediately).
- **`delete_by_source()`** (line 496): same paginated-loop pattern; without pagination,
  files with >300 chunks leave orphaned records that survive re-indexing.
- **`list_store()`** (line 428): clamp the caller-supplied `limit` parameter to
  `min(limit, QUOTAS.MAX_QUERY_RESULTS)` at the call site.

### 6. Error Type Hierarchy

```
NexusError (base)
└── QuotaViolation
    ├── RecordTooLarge       (document, embedding, metadata oversize)
    ├── NameTooLong          (id, uri, collection name, db name, key)
    ├── TooManyPredicates    (where clause count exceeded)
    ├── ResultsExceedLimit   (n_results > MAX_QUERY_RESULTS)
    └── ConcurrencyLimitExceeded  (semaphore timeout, future: if we ever add timeouts)
```

All quota errors carry: `field`, `actual`, `limit`, `hint` (human-readable suggestion).

## Alternatives Considered

### A: Catch-and-Retry on ChromaDB Error Codes

Intercept HTTP 400/429 from ChromaDB and parse error messages to infer the violated quota,
then retry with corrected parameters.

**Rejected**: ChromaDB error messages are not stable across versions; parsing them is
fragile. Retry on 429 (rate-limit) belongs in a retry layer, not quota enforcement. Client-
side validation is cheaper (no network round-trip on failure) and produces clearer messages.

### B: Configurable Quotas via `~/.config/nexus/config.toml`

Allow users to override quota values in config.

**Deferred**: Nothing in this RDR implements config-overridable quotas. The `ChromaQuotas`
frozen dataclass is intentionally not configurable in this version — hardcoded free-tier
limits are the safe default. If a user upgrades their Chroma plan and needs higher limits,
this is a follow-on feature: instantiate `ChromaQuotas` with values read from
`[chroma.quotas]` in `~/.config/nexus/config.toml`.

### C: Per-Database Quota Tracking (Record Counts)

Track how many records exist per collection and refuse writes that would exceed
`MAX_RECORDS_PER_COLLECTION`.

**Deferred**: Requires a metadata query before every write. Expensive for the common case
where collections are nowhere near 5M records. Better implemented as a periodic health check
(`nx doctor`) than a hot-path guard. Not part of this RDR.

## Implementation Plan

### Phase 1 — Constants and Validator (no behavior change)

1. Create `src/nexus/db/chroma_quotas.py` with `ChromaQuotas`, `QUOTAS`, `QuotaViolation`
   error hierarchy, and `QuotaValidator`.
2. Augment `validate_collection_name()` in `src/nexus/corpus.py` to also check the
   128-byte Cloud limit (byte length via `len(name.encode())`) alongside the existing
   3–63 character structural rule.
3. Write unit tests for all validator methods covering: at-limit (pass), one-over (fail),
   empty/None handling, and multi-byte name encoding.

### Phase 2 — Integrate into T3 Write Path

4. Add `_validate_record()`, `_write_batch()`, and `_delete_batch()` private helpers to
   `T3Database` in `src/nexus/db/t3.py`. Update all write/delete call sites in `t3.py`
   to route through these helpers.
5. Update `src/nexus/commands/migrate.py` to call `T3Database.upsert_chunks_with_embeddings()`
   instead of `dest_col.upsert()` directly, so the 5,000-record pages are auto-batched.
6. Write integration tests using a mock collection confirming:
   - Single write of ≤300 records → one `upsert()` call.
   - Write of 301 records → two `upsert()` calls (300 + 1).
   - Write of 5,000 records (migration scenario) → 17 `upsert()` calls.
   - Oversized document raises `RecordTooLarge` before any network call.
   - `delete()` of 500 IDs → two `delete()` calls (300 + 200).

### Phase 3 — Integrate into T3 Read/Query/Get Path

7. Add `_read_sems` and `_write_sems` dicts to `T3Database`; add `_read_sem()` and
   `_write_sem()` accessors. Acquire semaphores in `_write_batch()`, `_delete_batch()`,
   and at each `col.query()` / `col.get()` call site.
8. Validate query parameters in `db.search()` before dispatch; clamp and warn on `n_results`.
9. Replace the single `col.get()` in `expire()` with a paginated loop that **accumulates
   all matching IDs first, then calls `_delete_batch()` once**. Do not interleave get pages
   with deletes: mid-pagination deletes cause ChromaDB to recompute the filtered result set,
   producing page-offset drift that silently skips records. Apply the same accumulate-then-
   delete pattern to `delete_by_source()`. Clamp `limit` in `list_store()` to
   `min(limit, QUOTAS.MAX_QUERY_RESULTS)`. Also update the `_PAGE_SIZE = 5_000` comment in
   `migrate.py` to note that the effective per-call read limit from ChromaDB is 300; the
   while-loop pagination handles this correctly, but the constant name is now misleading.
10. Write tests confirming: semaphore bounds under simulated concurrency; `expire()` with
    >300 expired records processes all of them; `delete_by_source()` with >300 chunks
    deletes all chunks.

### Phase 4 — Surface in `nx doctor`

11. Add a `chroma_quotas` check to `nx doctor` that reports current quota constants and
    flags any T3 collection where `count() / MAX_RECORDS_PER_COLLECTION ≥ 0.80` (≥80% full).

## Open Questions

1. **Where does quota enforcement live?** ~~Unresolved.~~ **Resolved by Research Finding 8**:
   private helper methods `_write_batch()`, `_delete_batch()`, and `_validate_record()` inside
   `T3Database` in `src/nexus/db/t3.py`. No new wrapper class or file needed beyond
   `src/nexus/db/chroma_quotas.py`.

2. **Async vs sync concurrency**: ~~Unresolved.~~ **Resolved by Research Finding 7**: T3 is
   entirely synchronous. Use `threading.BoundedSemaphore`. No asyncio.

3. **Where clause predicate counting**: ChromaDB's `where` filter is a nested dict. Does
   ChromaDB count top-level keys or leaf predicates?
   — *Interim decision*: count top-level keys. This is **potentially permissive for compound
   filters**: `{"$and": [p1, ..., p9]}` has 1 top-level key, so our validator allows it, but
   ChromaDB (if it counts leaf nodes) sees 9 and rejects it at the API. The interim approach
   prevents the most common flat-predicate case but may under-reject deeply nested filters.
   Empirical verification against the live API is a Phase 3 prerequisite before finalizing
   the predicate validator.

4. **Warning vs error on n_results clamp**: Should requesting `n_results > 300` be a
   warning+clamp or an error?
   — *Decision*: warn and clamp for this release. Existing callers that passed large values
   were getting API errors before; clamping to 300 with a warning is strictly better than
   the current failure. Promote to error in a future breaking release.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| ChromaDB changes quota values without notice | Medium | Medium | Constants in one file; update is a one-liner |
| Batching changes write semantics (partial failure) | Low | High | Upsert is idempotent — callers may retry the full call safely. Raise immediately on chunk failure, including `records_written` count in exception. Delete is not idempotent; raise on partial delete with count of IDs deleted. Document in method docstrings. |
| Semaphore deadlock if code holds read sem and tries to acquire write sem | Low | High | Never acquire both semaphores; read and write are mutually exclusive operations |
| Predicate counting wrong (ChromaDB counts differently than we do) | Medium | Low | Integration tests against live API in Phase 3; validator is conservative (errs toward stricter) |

## Research Findings

*Codebase research conducted 2026-02-28 against commit on `main`.*

### Finding 1 — Primary T3 implementation is `src/nexus/db/t3.py` (531 lines)

`T3Database` is the single class managing all ChromaDB Cloud interactions. It holds four separate
`CloudClient` instances keyed by collection prefix (`code`, `docs`, `rdr`, `knowledge`).
All T3 write and read operations funnel through this class.

**Key methods and current batch behavior**:

| Method | Line | Current batch behavior |
|--------|------|----------------------|
| `put()` | ~241 | Passes single record — no batching needed |
| `upsert_chunks()` | ~261 | Passes full list to `.upsert()` with no size cap |
| `upsert_chunks_with_embeddings()` | ~282 | Same — full list, no cap |
| `update_chunks()` | ~302 | Passes full list to `.update()` — no size cap |
| `expire()` | ~411 | `.delete(ids=expired_ids)` — no cap on deletion batch |
| `delete_by_source()` | ~499 | `.delete(ids=ids)` — no cap |

**Verdict**: `upsert_chunks()` and `upsert_chunks_with_embeddings()` are the two critical call sites
that need auto-batching. Both accept arbitrary-length `ids`/`documents`/`metadatas` lists with no
300-record enforcement. Migration (`migrate.py`) uses a configurable `page_limit` for reads but
passes the whole page to `dest_col.upsert()` unchecked.

### Finding 2 — No existing concurrency semaphores on ChromaDB operations

Existing concurrency handling:
- `threading.Lock()` on the EF (embedding function) cache (non-hot-path, not a ChromaDB API call)
- `ThreadPoolExecutor(max_workers=8)` in `list_collections()` (non-hot-path, listing only)

There are **no semaphores** protecting `.upsert()`, `.query()`, `.get()`, `.delete()`, or `.update()`
calls against the CloudDB's 10-concurrent-reads / 10-concurrent-writes per collection limit.

The indexer (`indexer.py`) processes files and can fan out multiple `upsert_chunks_with_embeddings()`
calls. Under heavy indexing, concurrent writes to the same collection are plausible.

### Finding 3 — No field size validation before writes

Validation currently in place:
- `validate_collection_name()` in `corpus.py` — checks ChromaDB's 3–63-character alphanumeric rule
  but does NOT check the Cloud quota's 128-byte database/collection name limit (different constraint)
- TTL metadata struct is built from typed fields (strings, ints) — no byte-length checks
- `upsert_chunks_with_embeddings()` accepts embeddings as a raw list — no dimension count check
- Documents are passed through from caller — no byte-length check

**Specific gaps**:
- No check that documents are ≤ 16,384 bytes
- No check that embeddings have ≤ 4,096 dimensions
- No check that IDs are ≤ 128 bytes
- No check that metadata values are ≤ 4,096 bytes or that metadata keys are ≤ 36 bytes
- No check that individual metadata key strings are ≤ 36 bytes

### Finding 4 — Query path: single `.query()` call, no n_results clamping

`db.search()` (~line 358) calls `col.query(**query_kwargs)` where `n_results` comes from the
caller. No clamping to ≤ 300 is applied. A caller passing `n_results=1000` would get a ChromaDB
Cloud error (limit is 300).

`where` predicates in the existing codebase are always small (TTL filter is 1–2 predicates;
source-path filter is 1 predicate). The 8-predicate limit is not currently at risk, but there
is no enforcement preventing a future caller from hitting it.

### Finding 5 — `corpus.py` collection naming validates the wrong constraint

`validate_collection_name()` enforces ChromaDB's structural name rule (3–63 chars, regex
`^[a-zA-Z0-9][a-zA-Z0-9_-]{1,61}[a-zA-Z0-9]$`). This is the open-source ChromaDB constraint.

ChromaDB Cloud adds a separate **128-byte** limit for collection names and database names (byte
length, not character length — relevant if names ever contain non-ASCII). The existing validator
does not check this. Since current collection names are ASCII and well within 63 chars, there is
no current bug, but the validator should be augmented to reject names exceeding the Cloud limit.

### Finding 6 — Migration path (`migrate.py`) passes uncapped batches via raw collection object

`nx migrate t3` uses `_PAGE_SIZE = 5_000` and calls `dest_col.upsert()` directly on a raw
ChromaDB collection object (not through `T3Database`). This means:

1. Every migration of a collection with >300 records will fail on the very first page with a
   quota error — the current migration command is broken for any non-trivial collection.
2. Because `migrate.py` bypasses `T3Database`, private helpers added to `T3Database` will
   **not** automatically fix this path. `migrate.py` must be updated explicitly to call
   `T3Database.upsert_chunks_with_embeddings()` instead of the raw collection API.

### Finding 7 — All T3 callers are synchronous; no asyncio in T3 layer

T3 uses synchronous threading throughout. `asyncio.Semaphore` would not be appropriate here.
The concurrency layer should use `threading.BoundedSemaphore`. The RDR's Open Question 2
(async vs sync) is resolved: **use `threading.BoundedSemaphore`**.

### Finding 8 — Best insertion point for quota enforcement

The cleanest wrapping point is inside `T3Database` itself — specifically, three private helpers:
- `_write_batch(col, ids, documents, metadatas, embeddings)` — called by all upsert/update paths
- `_delete_batch(col, ids)` — called by expire() and delete_by_source()
- `_validate_record(id, document, embedding, metadata)` — called per record before batching

This avoids touching every call site and keeps quota logic centralized in `t3.py` with the
constants imported from the new `chroma_quotas.py` module.

## Research Conclusions

1. **Auto-batching is the highest-priority fix** — `upsert_chunks()` and `upsert_chunks_with_embeddings()`
   are the critical paths; migration is also affected.
2. **Semaphores belong on `T3Database`** using `threading.BoundedSemaphore(10)`, one per
   collection (lazily initialized, stored in a dict keyed by collection name).
3. **Field validation is pre-network** — add to `T3Database._validate_record()`, called before
   any write attempt.
4. **Open Question 2 is resolved**: use `threading.BoundedSemaphore` (synchronous), not asyncio.
5. **`corpus.py` validator gap**: should add 128-byte check alongside the existing regex check.
6. **`n_results` clamp**: add to `db.search()` with a `structlog` warning when clamped. This
   is a backward-compatible change (callers that passed >300 were getting errors before).

## Success Criteria

- `nx store add` with 500 documents executes successfully via transparent auto-batching (no user action required).
- `nx store add` with a 17,000-byte document raises `RecordTooLarge` with a clear message before any network call.
- `nx migrate t3` with a collection of 5,000 records completes without quota errors (auto-batched into 17 chunks of ≤300).
- `nx store expire` with >300 expired TTL records deletes all of them (paginated get + batched delete).
- `nx index` on a file producing >300 chunks re-indexes cleanly; `delete_by_source()` removes all prior chunks, not just the first 300.
- `nx search` with `n_results=500` logs a warning, clamps to 300, and returns results.
- `nx doctor` reports current quota constants and flags collections at ≥80% of `MAX_RECORDS_PER_COLLECTION`.
- Concurrent calls to `upsert_chunks()` from 15 threads on the same collection do not exceed 10 simultaneous `upsert()` calls at the ChromaDB layer (verified by mock).
- `nx collection list` (which uses `list_collections()`) executes correctly without acquiring or deadlocking on collection semaphores.
- All existing T3 tests pass without modification (enforcement is transparent for in-spec inputs).
- Zero magic numbers related to ChromaDB limits anywhere outside `src/nexus/db/chroma_quotas.py`.
