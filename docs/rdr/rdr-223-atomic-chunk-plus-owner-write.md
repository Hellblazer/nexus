---
title: "Atomic Chunk-Plus-Owner Write on Every Client Path"
id: RDR-223
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-28
related_issues: [nexus-wbfpw.28, nexus-wbfpw.29, nexus-wbfpw.31, nexus-wbfpw.32, nexus-kl2z6, nexus-y9t08, nexus-mfw6c]
related_rdrs: [RDR-222, RDR-192, RDR-191, RDR-181, RDR-108]
---

# RDR-223: Atomic Chunk-Plus-Owner Write on Every Client Path

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Asked for by Sam on 2026-09-28 (relayed by conexus-2a, tracked there as
conexus-ekg5): "this is exactly what we need: atomic chunk-plus-owner write
on the engine." The question behind it was whether nexus will ever stop
having orphan chunks. An orphan chunk is a row in `nexus.chunks_<dim>` that no
live document's manifest (`catalog_document_chunks`) points at.

## Problem Statement

Today orphans are hidden and cleaned up, not prevented. `live(c)` hides them
from search from engine v0.1.136 on, and the reaper drains them. But most
client paths still write a chunk in one request and its owner row in a later
one. A client that dies between the two, or whose second request fails, leaves
chunks nobody owns. Hiding and reaping treat the symptom; the write protocol
keeps producing the cause.

The engine already has the atomic write for one case. `POST
/v1/catalog/manifest/write_many` with inline `chunks` (nexus-kl2z6,
nexus-y9t08; `CombinedWriteService` then `CatalogRepository.writeManifestMany`)
commits each document's chunk rows, its manifest rows, its `chunk_count` and
its completion stamp in one transaction. Only one client path uses it. The
gaps below are the ones it does not reach.

#### Gap 1: A document larger than one request cannot use the combined write

The combined write's manifest step is a whole-document REPLACE, so every
chunk of the document has to ride in that one request. Documents that exceed
one request fall back to split writes:

- the streaming PDF pipeline (every PDF; 128-chunk batches, batch 1 replaces
  and later batches append);
- `nx index repo`'s oversize fallbacks (code files over 300 chunks, prose and
  RDR files over the 64-chunk CCE cap, PDFs over one batch);
- `.nxexp` import, which uploads every page and writes one manifest per
  document at the end, because a document's rows can span many 300-chunk
  pages.

#### Gap 2: Small-document paths that never adopted the combined write

These documents would fit in one request but still go through
`upsert-chunks` or `store-put` and then a separate manifest write:

- MCP `store_put`, `nx store put`, `nx memory promote` and recovery-bundle
  import (`/store-put`, then `atomic_manifest_replace`);
- `doc_indexer._index_document` (`nx index md`, `nx index rdr`, DEVONthink
  markdown).

#### Gap 3: The engine still accepts ownerless chunk writes

`/v1/vectors/upsert-chunks` and `/v1/vectors/store-put` insert chunks with no
manifest write in the same transaction; `PgVectorRepository` never inserts
into `catalog_document_chunks`. While those routes accept ownerless writes,
any client path, including a future one, can reintroduce the window.

#### Gap 4: One path writes ownerless chunks on purpose

`indexer.py:5587` (`combined_write_orphan_chunks_routed_to_legacy_upsert`)
routes chunks from files with no catalog identity to the legacy upsert and
never manifests them. They are searchable today only because they predate
`live(c)`; from v0.1.136 they are orphans by construction.

## Research Findings

### F-1. Path inventory (verified, source, develop 711056985)

Full table in T2 `nexus/chunk-owner-write-path-inventory-2026-09-28`, with
nexus-76's store-path notes. Counts: one path uses the combined write (the
`nx index repo` ChunkBatcher flush, `indexer.py:5637`, when the file fits one
batch). Sixteen are split. Of those, thirteen can orphan on a client death,
one is ownerless by design (Gap 4), one cannot orphan (`collection re-embed`
rewrites already-owned ids), and two are dead code (`db/reconcile.verify_fill_*`,
`db/embed_migrate`).

Split paths by traffic: streaming PDF (highest), MCP `store_put`,
`_index_document`, the `nx index repo` oversize fallbacks, `nx store put`,
`nx store import` (rare but the widest window: every page commits before any
manifest).

### F-2. The combined write is atomic per document (verified, source)

`CatalogRepository.writeManifestMany` opens one `tenantScope.withTenant` per
document; `writeManifestRows` runs `upsertManifestChunkVectors` (the chunk
INSERT), the manifest DELETE and INSERT, the `chunk_count` update and
`stampCompleteIfVerified` inside it. Embedding runs before, outside any
transaction; the superseded-chunk sweep runs after, in its own. Neither can
leave an ownerless chunk: `upsertManifestChunkVectors` writes only chashes the
document's own rows reference, so a failed document writes none.

### F-3. Append is already one transaction (verified, source)

`CatalogRepository.appendManifestChunks` (`:6108`) runs in one
`withTenant`: document-exists check, sweep gate, index-run lock,
`stampIndexedAt`, per-row `UPSERT_APPEND` inserts, and the `chunk_count`
fold. It takes no chunks today. Adding the same `upsertManifestChunkVectors`
call before the row inserts gives an append that commits chunks and owner rows
together, with the same shape `writeManifestRows` already has.

### F-4. store-put carries the owner id but does not write it

`/store-put` (`VectorHandler.handleStorePut`, `:736`) calls
`putWithTokens`, which only upserts chunks; the note's `catalog_doc_id` is
stamped into chunk metadata and nowhere else. The MCP path registers the
catalog document BEFORE the chunk write (`mcp/core.py:4906`), so the owner
exists when the chunks land. The owner row just is not written with them.

## Proposed Solution

One rule: every chunk the engine inserts is written in the same transaction
as at least one owner row. The client never needs two requests to make a
chunk owned.

1. **Append with chunks (engine, closes Gap 1).** `POST
   /v1/catalog/manifest/append` accepts an optional inline `chunks` array,
   deduped, existence-partitioned and embedded through `CombinedWriteService`
   exactly as `write_many` does, then written by `upsertManifestChunkVectors`
   inside `appendManifestChunks`'s transaction. A multi-batch document writes
   batch 1 with `write_many` plus chunks (replace) and every later batch with
   append plus chunks. Each request commits its own chunks and their owner
   rows together. A crash mid-document leaves a document with some of its
   batches (on the indexing paths the index fence reports it incomplete) and
   never an ownerless chunk.
2. **Client migration (closes Gaps 1 and 2).** Move each split path onto
   `write_many` plus chunks, or append plus chunks for later batches: streaming
   PDF, the three oversize fallbacks, `_index_document`, `store_put`, `nx store
   put`, `nx memory promote`, recovery-bundle import and `.nxexp` import. For
   the note paths, a note is one document of a few pieces, so `write_many` plus
   chunks may cover it with no engine change; whether it returns the token
   usage `store-put` reports is Open Question 2.
3. **Retire ownerless writes (closes Gap 3), last.** Once no client path
   uses them, `/v1/vectors/upsert-chunks` and `/v1/vectors/store-put` refuse
   writes that do not name an owner. A version window keeps old clients
   working until the refusal lands.
4. **Decide Gap 4** (Open Question 3): register identity-less files as ghost
   documents so their chunks have an owner, or stop writing them at all.

## Alternatives Considered

- **Keep split writes; rely on `live(c)` plus the reaper** (the status quo).
  Correct but permanent churn: orphans are produced forever, and the weekly
  `live(c)` census keeps a FINDING path open to catch them.
- **Engine-side staging**: chunks upload into a staging area under a document
  lease; the final manifest commit promotes them. It solves Gap 1 without
  per-batch owner rows, but adds a new table, a lease and a promotion sweep,
  where append-with-chunks reuses code that exists.
- **Stamp ownership in chunk metadata** (store-put's `catalog_doc_id`). The
  metadata is not the manifest; `live(c)` and the reaper read the manifest.

## Relationship to RDR-222

RDR-222 Phase 3 (the run fence, nexus-mfw6c) is about a late write from an
abandoned run reverting a newer run's metadata. That is a different class:
the chunk has an owner; the owner row is the stale one. This RDR does not
close it. It does close the orphan class RDR-222's M-c query can also
surface. Both paths use the same existence partition, so RDR-222's raced-embed
counter (nexus-ulrjq) keeps working on the new routes; the append path must
feed it too.

## Implementation Plan

- Phase 1 (engine): append with chunks, wire-ledger `[additive]` entry,
  tests. Rides the next engine tag after it lands.
- Phase 2 (client): migrate the paths in traffic order: streaming PDF, MCP
  `store_put`, `_index_document`, the oversize fallbacks, then the rest. Each
  migration is its own bead. `.nxexp` import moves last; nexus-76 owns it.
- Phase 3 (engine): refuse ownerless writes once the client floor carrying
  Phase 2 is `REQUIRED_ENGINE_VERSION`'s pair; delete the dead paths.
- Exit: the weekly `live(c)` census (`scripts/sql/livec_census.sql`) reads
  zero new orphans over a week, and conexus retires its FINDING path
  (conexus-ekg5).

## Test Plan

- Append with chunks: a crash between two appends leaves every inserted chunk
  owned (count ownerless chashes after killing between requests: zero).
- Append with chunks: known chashes are not re-embedded (RDR-181
  existence partition), and a content-divergent chash is.
- A document written as write_many plus chunks then N appends plus chunks
  has the same manifest and chunks as one write_many of the whole document.
- Each migrated client path: an end-to-end test that kills the client after
  the first request and asserts no ownerless chunk.
- Phase 3: `upsert-chunks` without an owner is refused with a message naming
  the replacement route.

## Open Questions (for Sam)

1. Is the Phase 3 refusal wanted, or is migrating the clients enough?
2. Can the note paths use `write_many` plus chunks as it is, or do they need
   `store-put`'s token-usage reporting carried over first?
3. Gap 4: ghost-document ownership for identity-less files, or stop writing
   them?
4. Own RDR, as drafted, or fold into RDR-222?

## Revision History

- 2026-09-28: created from the path inventory (T2
  `nexus/chunk-owner-write-path-inventory-2026-09-28`) and nexus-76's
  store-path notes.
