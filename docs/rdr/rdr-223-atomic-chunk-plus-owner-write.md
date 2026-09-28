---
title: "Atomic Chunk-Plus-Owner Write on Every Client Path"
id: RDR-223
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-28
related_issues: [nexus-wbfpw.28, nexus-wbfpw.29, nexus-wbfpw.31, nexus-wbfpw.32, nexus-kl2z6, nexus-y9t08, nexus-mfw6c, nexus-b50zw]
related_rdrs: [RDR-222, RDR-192, RDR-191, RDR-181, RDR-108]
---

# RDR-223: Atomic Chunk-Plus-Owner Write on Every Client Path

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Asked for by Sam on 2026-09-28 (relayed by conexus-2a, tracked there as
conexus-ekg5): "this is exactly what we need: atomic chunk-plus-owner write
on the engine." The question behind it was whether nexus will ever stop
having orphan chunks.

Two terms used throughout. A **chunk** is one row of embedded text in
`nexus.chunks`, identified by the hash of its text (its **chash**). A
document's **manifest** is its list of owner rows in
`catalog_document_chunks`, one per chunk position; a chunk that no live
document's manifest points at is an **orphan**.

## Problem Statement

Today orphans are hidden and cleaned up, not prevented. From engine v0.1.136
on, search hides chunks that no live manifest owns (the `live(c)` predicate,
RDR-192), and a reaper deletes them. But most client paths still write a chunk
in one request and its owner rows in a later one. A client that dies between
the two, or whose second request fails, leaves chunks nobody owns. Hiding and
reaping treat the symptom; the write protocol keeps producing the cause.

The engine already has the atomic write for one case: `POST
/v1/catalog/manifest/write_many` with an inline `chunks` array (the
**combined write**) commits each document's chunk rows and manifest rows in
one transaction (F-2). Only one client path uses it (F-1). The gaps below are
the paths it does not reach.

#### Gap 1: A document larger than one request cannot use the combined write

The combined write replaces a document's whole manifest, so every chunk of
the document must ride in that one request. Documents larger than one request
fall back to split writes: the streaming PDF pipeline (every PDF), the
`nx index repo` fallbacks for files over one batch, and `.nxexp` import.

#### Gap 2: Small-document paths that never adopted the combined write

These documents fit in one request but still write chunks through
`/v1/vectors/upsert-chunks` or `/v1/vectors/store-put` and their manifest
afterwards: MCP `store_put`, `nx store put`, `nx memory promote`,
recovery-bundle import, and `doc_indexer._index_document` (`nx index md`,
`nx index rdr`, DEVONthink markdown).

#### Gap 3: The engine still accepts ownerless chunk writes

`/v1/vectors/upsert-chunks` and `/v1/vectors/store-put` insert chunks with no
owner row in the same transaction (F-4). While they accept that, any client
path, including a future one, can reopen the window.

#### Gap 4: One path writes ownerless chunks on purpose

In `nx index repo`, chunks from a file with no catalog document go to the
legacy upsert and are never given an owner (`src/nexus/indexer.py:5598`,
event `combined_write_orphan_chunks_routed_to_legacy_upsert`). Since
`live(c)`, search hides them, so they are written and never found.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-192 | Precedent | It made orphans invisible (`live(c)`) and reapable; this RDR stops producing them. Its reaper and census tests seed orphans on purpose, which Phase 3 must keep possible. |
| RDR-222 | Adjacent draft | Request identity (retries, stale runs) on the same write routes. Its run fence (Phase 3) addresses a stale owner row, not a missing one, and is closed for lack of evidence; its raced-embed counter must also count on the new append path. The two sequence independently. |
| RDR-181 | Precedent | The existence partition (known chashes are not re-embedded) that the combined write already applies; append-with-chunks reuses it. |
| RDR-191, RDR-108 | Origin | The manifest as the doc-to-chunk join and the collection-stamped manifest row; this RDR writes those rows in the chunk transaction. |

## Context

### Background

The weekly `live(c)` census (`scripts/sql/livec_census.sql`) finds new
orphans each week, and conexus keeps a FINDING path open for them
(conexus-ekg5). nexus-wbfpw.28 and .29 mitigated the split write on the
`store_put` and `nx index` paths (rollback on a confirmed failure, read-back
verification), but a client killed between requests still leaves orphans.

### Technical Environment

- Engine: Java service over PG17 with pgvector (`service/`), Liquibase
  changesets, jOOQ. Every write runs in `tenantScope.withTenant`, one
  transaction.
- Client: Python (`src/nexus/`), HTTP clients `HttpVectorClient` and
  `HttpCatalogClient`.
- Wire changes are recorded in `docs/wire-contract-pending.md` and pair an
  engine tag with a client release.

## Research Findings

### Investigation

Source reading on develop, recorded in T2 as `nexus_rdr/223-research-1`
through `-9`, plus a read-only inventory of every client path that writes
chunks (T2 `nexus/chunk-owner-write-path-inventory-2026-09-28`) and
nexus-76's store-path and import review.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Engine `CatalogRepository` | Yes | `writeManifestRows` and `appendManifestChunks` take the same locks in the same order (F-2, F-3) |
| Engine `VectorHandler` / `PgVectorRepository` | Yes | `store-put` and `upsert-chunks` never write an owner row (F-4) |
| Engine `CatalogHandler` | Yes | the combined write already reports token usage (F-5) |

### Key Discoveries

- **F-1 (Verified, source).** One client path uses the combined write: the
  `nx index repo` batch flush (`src/nexus/indexer.py:5658`), for files that
  fit one batch. Sixteen paths are split: thirteen can orphan on a client
  death, one is ownerless by design (Gap 4), one cannot orphan
  (`collection re-embed` rewrites already-owned chunks), two are dead code
  (`db/reconcile.verify_fill_*`, `db/embed_migrate`). By traffic: streaming
  PDF first, then MCP `store_put`, `_index_document`, the `nx index repo`
  oversize fallbacks, `nx store put`, and `nx store import` (rare, but every
  page commits before any manifest).
- **F-2 (Verified, source).** The combined write is atomic per document.
  `CatalogRepository.writeManifestMany` opens one transaction per document;
  `writeManifestRows` (`CatalogRepository.java:5063`) checks the document
  exists, takes the sweep gate and the index-run lock, then inserts the chunks
  (`upsertManifestChunkVectors`, `:5096`) and replaces the manifest in that
  transaction. Embedding happens before, outside any transaction; the
  superseded-chunk sweep runs after, in its own. A document that fails writes
  no chunks, because only chashes its own rows reference are inserted.
- **F-3 (Verified, source).** Append is already one transaction with the same
  lock order. `CatalogRepository.appendManifestChunks` (`:6225`) checks the
  document, takes the sweep gate and the index-run lock, then upserts manifest
  rows by position. It takes no chunks today; inserting them after the
  index-run lock reproduces `writeManifestRows` exactly.
- **F-4 (Verified, source).** `store-put` writes no owner row.
  `VectorHandler.handleStorePut` (`VectorHandler.java:736`) calls
  `putWithTokens`, which only upserts chunks; the note's `catalog_doc_id`
  goes into chunk metadata. MCP `store_put` registers the catalog document
  before the chunk write, so the owner exists; its rows are just not written
  with the chunks.
- **F-5 (Verified, source).** The combined write sets the same
  `X-Nexus-Usage-Tokens` header `store-put` does (`CatalogHandler.java:1257`),
  and no client code reads it, so the note paths can move with no engine
  change.
- **F-6 (Documented, nexus-76 review).** Import differs from every other path.
  `.nxexp` carries each chunk's vector, and gate-xr789 needs those vectors
  byte-identical; the combined write always embeds new chunks server-side,
  while `upsert-chunks` has a client-vector passthrough and the combined routes
  do not. Export is in chash-page order, so one document spans many 300-record
  pages: import needs append to take explicit positions, a multi-document
  form, and a first-sighting replace per document.
- **F-7 (Verified, source).** Engine staging exists
  (`StagingHandler.java`: load, embed_fill, promote); its client half is
  nexus-b50zw, on hold pending this RDR.
- **F-8 (Verified, source).** Each collection's embedding model is recorded
  and foreign-keyed (`catalog_collections.embedding_model`,
  `hygiene-002-collection-attributes-walk.xml:287-288`), so a client-supplied
  vector can be checked server-side.

### Critical Assumptions

- [x] Chunks can be inserted inside append's transaction without a new lock
  order — **Status**: Verified — **Method**: Source Search (F-2, F-3)
- [x] The note paths need no engine change to use the combined write —
  **Status**: Verified — **Method**: Source Search (F-5)
- [ ] Identity-less files in `nx index repo` are rare and point at an upstream
  registration gap (A-1, `nexus_rdr/223-research-9`) — **Status**:
  Unverified — **Method**: Docs Only. Phase 2 counts them before the route is
  removed; the count, not this assumption, decides the registration fix.

## Proposed Solution

### Approach

One rule: every chunk the engine inserts is written in the same transaction
as at least one owner row, so a client never needs two requests to make a
chunk owned.

### Technical Design

1. **Append with chunks (engine, closes Gap 1).**
   `POST /v1/catalog/manifest/append` gains an optional inline `chunks` array.
   The chunks are deduplicated, checked against what already exists, and
   embedded exactly as the combined write does (RDR-181), then inserted after
   the index-run lock inside `appendManifestChunks`'s transaction (F-3).
   Manifest rows keep their explicit positions (append already upserts by
   position). A multi-batch document writes its first batch with the combined
   write (replace) and each later batch with append plus chunks; a crash
   mid-document leaves a document with some of its batches and never an
   ownerless chunk. A multi-document form, `append_many`, carries several
   documents in one request, one transaction per document, as `write_many`
   does. The raced-embed counter (RDR-222, nexus-ulrjq) counts on this path
   too.
2. **Client-supplied embeddings (engine, needed by import).** Both combined
   routes accept an optional vector per chunk. The engine checks its dimension
   and the collection's embedding model (F-8) and refuses the request on a
   mismatch; a supplied vector is stored as-is and never re-embedded.
3. **Client migration (closes Gaps 1 and 2).** Each split path moves onto the
   combined write, or append plus chunks for later batches. The note paths
   move with no engine change (F-5). Note registration stays a separate
   request before the chunk write, and nexus-wbfpw.28's rollback of a failed
   first put's empty document stays; a failed re-put leaves the old manifest
   intact.
4. **Stop the ownerless route (closes Gap 4).** The route at
   `src/nexus/indexer.py:5598` becomes a counted event that writes no chunks,
   and the registration gap behind it is fixed. No ghost documents.
5. **Refuse ownerless writes, last (closes Gap 3).** Once the client release
   carrying step 3 is the paired release, `upsert-chunks` and `store-put`
   refuse a write that names no owner, with an error naming the replacement
   route.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Append with chunks | `CatalogRepository.appendManifestChunks`, `CombinedWriteService` | Extend: add the chunk insert and the existence partition |
| Client-supplied vectors | `PgVectorRepository.upsertChunksWithVectors` | Reuse its dimension check pattern on the combined routes |
| Bulk staging | `StagingHandler` (F-7) | Not used; retire in Phase 3 if nexus-b50zw closes |

### Decision Rationale

Append-with-chunks reuses the combined write's code and locks, so the new
transaction shape already exists in production. The alternatives either keep
producing orphans or add a table, a lease and a promotion sweep.

## Alternatives Considered

### Alternative 1: Keep split writes; rely on `live(c)` and the reaper

**Description**: the status quo.

**Pros**: no work; search is already correct.

**Cons**: orphans are produced forever; the census FINDING path never
retires; every new client path must be mitigated by hand.

**Reason for rejection**: treats the symptom permanently.

### Alternative 2: Engine-side staging

**Description**: upload a document's chunks into staging under a lease, then
promote them with the manifest in one step (F-7).

**Pros**: whole-document atomicity, including batch counts.

**Cons**: a staging table, a lease, a promotion sweep, and a client half not
yet built (nexus-b50zw).

**Reason for rejection**: per-batch ownership is enough to stop orphans, and
append reuses code that exists.

### Briefly Rejected

- **Ownership in chunk metadata** (`store-put`'s `catalog_doc_id`): search and
  the reaper read the manifest, not metadata.
- **Registering note documents in the chunk transaction**: Sam kept the
  existing rollback instead (Decision 5).

## Trade-offs

### Consequences

- Positive: no client path can leave an ownerless chunk once Phase 3 lands.
- Positive: the note paths get atomic chunk-plus-owner writes with no engine
  change.
- Negative: a crash mid-document still leaves a partial document (some
  batches). It is owned and visible, and the next index run replaces it.
- Negative: old clients that still call `upsert-chunks` without an owner
  break at Phase 3; the paired-release floor bounds that window.

### Risks and Mitigations

- **Risk**: append-with-chunks deadlocks against the sweep.
  **Mitigation**: it takes the combined write's lock order (F-3); a
  concurrent-sweep test pins it.
- **Risk**: Phase 3 removes the only way tests build orphan states.
  **Mitigation**: Phase 3 first moves those seeds to direct SQL in the
  substrate.

### Failure Modes

- A client-supplied vector with the wrong model or dimension is refused
  loudly (a 4xx naming both), never stored.
- An append for a document that does not exist fails as append does today,
  before any chunk is inserted.

## Implementation Plan

### Prerequisites

- [x] Critical Assumptions 1 and 2 verified (F-2, F-3, F-5).

### Minimum Viable Validation

A multi-batch document written as the combined write plus N appends with
chunks, with the client killed between two appends, leaves zero ownerless
chunks, and its final manifest equals a single combined write of the whole
document.

### Phase 1: Engine

#### Step 1: Append with chunks

Add the optional `chunks` array to `/v1/catalog/manifest/append` and the
`append_many` form, as in Technical Design 1. Record the wire change as an
`[additive]` entry in `docs/wire-contract-pending.md`.

#### Step 2: Client-supplied vectors on the combined routes

Add the optional per-chunk vector with the model and dimension check, as in
Technical Design 2. Rides the next engine tag after it lands.

### Phase 2: Client migration

#### Step 1: Migrate paths in traffic order

Streaming PDF, MCP `store_put`, `_index_document`, the `nx index repo`
oversize fallbacks, then `nx store put`, `nx memory promote` and
recovery-bundle import. One bead per path.

#### Step 2: Import

Move `.nxexp` import onto append with chunks and client-supplied vectors,
keeping the nexus-wbfpw.31 legacy `doc_id` handling. Owner: nexus-76.

#### Step 3: Stop the ownerless route

Count identity-less files (A-1), stop writing their chunks, and fix the
registration gap the count exposes.

### Phase 3: Refuse ownerless writes

#### Step 1: Move orphan seeding to direct SQL

The `livec_census` tests, the nexus-wbfpw.31 import tests and the RDR-192
reaper and census tests build orphan states through substrate SQL instead of
`upsert-chunks`.

#### Step 2: Refuse

Once the client release carrying Phase 2 is the paired release, the engine
refuses ownerless `upsert-chunks` and `store-put` writes. Delete the dead
paths (`db/reconcile.verify_fill_*`, `db/embed_migrate`). If nexus-b50zw
closes, retire the staging routes.

### Day 2 Operations

No new persistent resource. The exit is operational: the weekly `live(c)`
census shows no growth, week over week, in its no-manifest and
other-collection-only buckets, net of known dispositions; conexus then retires
its FINDING path (conexus-ekg5). Quarantine and tombstoned-owner rows are
permanent, correct residents of the census and never read zero.

### New Dependencies

None.

## Test Plan

- **Scenario**: client killed between two appends with chunks — **Verify**:
  zero ownerless chunks.
- **Scenario**: append with chunks carrying known and content-changed chashes
  — **Verify**: known ones are not re-embedded; changed ones are.
- **Scenario**: combined write plus N appends — **Verify**: manifest and
  chunks equal one combined write of the whole document.
- **Scenario**: append with chunks concurrent with a superseded-chunk sweep —
  **Verify**: no deadlock; the sweep does not remove the new chunks.
- **Scenario**: each migrated client path, killed after its first request —
  **Verify**: no ownerless chunk.
- **Scenario**: a gate-xr789-shaped fixture imported through append with
  chunks and supplied vectors — **Verify**: vectors byte-identical, zero
  embedder calls, scattered positions in manifest order, a re-import replaces.
- **Scenario**: supplied vector with the wrong dimension or model —
  **Verify**: refused, nothing stored.
- **Scenario**: Phase 3 refusal — **Verify**: an ownerless `upsert-chunks` is
  refused naming the replacement route; the seeding helpers still build
  orphan states.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution. The one prior tension (engine staging, F-7, against
append-with-chunks) is resolved in Alternatives and parked on nexus-b50zw.

### Assumption Verification

Assumptions 1 and 2 are verified by source reading. Assumption 3 (A-1) is
unverified by design: Phase 2 Step 3 measures it before acting on it.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `appendManifestChunks` lock order | nexus engine | Source Search |
| `X-Nexus-Usage-Tokens` on `write_many` | nexus engine | Source Search |
| `catalog_collections.embedding_model` FK | nexus schema | Source Search |

### Scope Verification

The Minimum Viable Validation is the first Test Plan scenario and runs in
Phase 1, against the real engine substrate.

### Cross-Cutting Concerns

- **Versioning**: new wire fields are `[additive]`; the Phase 3 refusal waits
  for the paired client release.
- **Deployment model**: engine changes ride an engine tag; conexus deploys.
- **Incremental adoption**: each client path migrates in its own bead;
  unmigrated paths keep working until Phase 3.
- **Build tool compatibility**, **Licensing**, **IDE compatibility**,
  **Secret/credential lifecycle**: N/A.
- **Memory management**: an append request is bounded by the existing
  per-request chunk caps.

### Proportionality

Right-sized: three phases, one of which (Phase 2) is a set of small
per-path beads.

## Decisions (Sam, 2026-09-28)

1. **Refuse ownerless writes at the end** (Phase 3).
2. **Note paths need no engine change** (settled by checking, F-5).
3. **Gap 4: stop writing identity-less chunks and find why they lack
   identity.** No ghost documents.
4. **Own RDR**, separate from RDR-222.
5. **Keep nexus-wbfpw.28's registration rollback.** No register-if-absent on
   `write_many`.

## References

- T2 `nexus/chunk-owner-write-path-inventory-2026-09-28`
- T2 `nexus_rdr/223-research-1` through `-9`
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java`
- `service/src/main/java/dev/nexus/service/http/VectorHandler.java`,
  `CatalogHandler.java`, `StagingHandler.java`
- `scripts/sql/livec_census.sql`

## Revision History

- 2026-09-28: restructured to the RDR template for the gate: Context,
  Relationship to Prior RDRs, Critical Assumptions, Trade-offs, Finalization
  Gate and a `### Phase` / `#### Step` Implementation Plan; findings recorded
  as `nexus_rdr/223-research-1..9`; citations re-read from develop.

- 2026-09-28: Sam's answers to the five open questions recorded under
  Decisions.

- 2026-09-28: nexus-76's store-path and import review folded in: F-5,
  client-supplied embeddings, explicit positions and `append_many`, the
  note-path registration orphan, the census-based exit criterion, and Phase 3
  test seeding.

- 2026-09-28: created from the path inventory (T2
  `nexus/chunk-owner-write-path-inventory-2026-09-28`) and nexus-76's
  store-path notes.
