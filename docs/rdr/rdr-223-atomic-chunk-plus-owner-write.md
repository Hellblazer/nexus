---
title: "Atomic Chunk-Plus-Owner Write on Every Client Path"
id: RDR-223
type: Architecture
status: accepted
accepted_date: 2026-09-28
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

`/v1/vectors/upsert-chunks`, `/v1/vectors/store-put` and
`/v1/vectors/upsert-reference-only` insert chunks with no owner row in the
same transaction (F-4, R-12). While they accept that, any client path,
including a future one, can reopen the window.

Amended 2026-10-01: `upsert-reference-only` is retired rather than refused
(Phase 3 Step 2), so this Gap closes by refusing two routes and deleting the
third.

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
  fit one batch. Sixteen paths are split: twelve can orphan on a client
  death (R-13), one is ownerless by design (Gap 4), one cannot orphan
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
  nexus-b50zw, on hold pending this RDR. Retired 2026-09-30 (Sam, measured):
  Phase 3 Step 2 deletes the staging routes and nexus-b50zw closes.
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
   write (replace) and each later batch with append plus chunks.
   The superseded-chunk sweep is deferred to the document's last batch. Today
   `write_many` with `sweep` on sweeps the chashes the write dropped from the
   document's previous manifest, in its own transaction right after the
   commit (R-10); on a first batch that would delete the previous run's
   chunks for the later batches before those batches land. So the first batch
   is written with `sweep` off. The list to sweep comes from the document's
   manifest as it stood BEFORE the run (the index-run fence's `begin` returns
   it, read with the stamp), not from the first batch's `dropped_chashes`,
   which is empty when a lost first-batch response is resent; the client passes
   that list, minus every chash the run wrote, on the last append as
   `sweep_chashes`, and the engine sweeps it after
   that commit under the existing NOT EXISTS guard, so a dropped chash a later
   batch re-added, or another document owns, survives. A crash mid-document
   leaves a document with some of its batches and no chunk this run wrote
   without an owner; the deferred sweep never runs, so the previous run's
   dropped chunks stay ownerless and hidden from search. The RDR-192 reaper
   (nexus-2x9xa, not yet built) removes them: it covers every prefix,
   `knowledge__`, `docs__`, `code__` and `rdr__`, behind a per-collection
   census gate (Sam, 2026-09-30, T2
   `nexus/rdr-223-192-sam-decisions-2026-09-30-reaper-staging`; this text
   said `knowledge__` only until the amendment of 2026-10-01). It must not
   delete a chunk a running multi-batch run is about to reference, so it takes
   the sweep gate exclusive per collection and skips documents in
   `index_state = 'indexing'` (with a TTL); RDR-192 Step 9 carries the
   requirements. A rerun sweeps what its own
   snapshot shows, which is the crashed run's chunks in the manifest, not the
   tail of the run before the crash (the crashed run's first batch already
   dropped that tail from the manifest). A multi-document form, `append_many`, carries several
   documents in one request, one transaction per document, as `write_many`
   does. The raced-embed counter (RDR-222, nexus-ulrjq) counts on this path
   too.
2. **Client-supplied embeddings (engine, needed by import).** Both combined
   routes accept an optional vector per chunk. The engine checks its dimension
   and the collection's embedding model (F-8) and refuses the request on a
   mismatch. For each chunk (R-14):
   - new chash, no vector: embedded, as today;
   - new chash, vector: the supplied vector is stored as-is;
   - existing chash, no vector: not re-embedded, metadata refreshed (RDR-181);
   - existing chash, vector: the stored text and vector are kept, whatever text
     the request carries, and a mismatch with the supplied vector is counted
     and logged, not written (inferred, not read: a design choice). The same
     holds if another writer commits the chash between the existence check and
     the insert (ON CONFLICT keeps the stored row). With `force_re_embed` the
     supplied vector is written instead, and a differing stored vector is
     counted.
3. **Client migration (closes Gaps 1 and 2).** Each split path moves onto the
   combined write, or append plus chunks for later batches. The note paths
   move with no engine change (F-5); a note is one document of a few pieces,
   so it is one `write_many` request with `sweep` on and the first-batch
   problem of step 1 cannot arise. `store_put` carries machinery built for
   the split write (R-11), reconciled piece by piece:
   - replaced by the one `write_many` request: `put_note_pieces`
     (`store_hook.py:303`, one `/store-put` per piece with a compensating
     delete of the pieces it wrote when a later piece fails, R-15), whose
     pieces become that request's `chunks` array, and
     `store_put_manifest_direct_with_recovery` (`store_hook.py:1469`, a
     single retry that re-puts pieces a concurrent rollback deleted between
     the chunk write and the manifest write, R-15), which has no gap left to
     recover;
   - retired: `rollback_uncataloged_chunk_write` (a chunk can no longer land
     without its manifest), and nexus-bb6n2's client reap of chashes a
     supersede dropped (`store_hook.py:1449-1460`), replaced by `write_many`'s
     own sweep;
   - kept: the index fence (`_fence_begin`, `_fence_fail`), the registration
     rollback `rollback_minted_catalog_entry` (Decision 5; a failed first
     put's empty document is still removed), and the
     `ManifestVerifyUncertainError` outcome check, because an atomic request
     can still time out with an unknown result.
   Each piece is retired only after a test shows its replacement covers the
   same failure. A failed re-put leaves the old manifest intact.
4. **Stop the ownerless route (closes Gap 4).** The route at
   `src/nexus/indexer.py:5598` becomes a counted event that writes no chunks,
   and the registration gap behind it is fixed. No ghost documents.
5. **Refuse ownerless writes, last (closes Gap 3).** *Amended 2026-10-01
   (Sam, 2026-09-30 and 2026-10-01): this ships in the single final cut, not
   after a separate Phase 2 release; it ships log-only first and then
   enforces; and `upsert-reference-only` is retired, not refused. See Phase 3
   Step 2.* Once the client release carrying step 3 is the paired release, `upsert-chunks` and
   `store-put` accept a write only when every chash in it already
   has a live manifest row in that collection, checked inside the write's own
   transaction; that keeps `collection re-embed` and metadata refreshes of
   owned chunks working. Every other write is refused (422) with an error
   naming the combined routes. A request field naming an owner is not enough,
   because these routes never write manifest rows. `upsert-reference-only`
   has no client caller (R-12); if RDR-169's G4 is built, it writes through
   the combined routes.
   As built (nexus-z0o2p.24):
   - *Where.* The handlers pass an ownership guard to the repository's upsert
     methods; the repository methods called without one (the contract and
     fixture tests, the migration ingest) are unchanged, and
     `OwnershipGuardCoverageScan` reads the main sources so a handler cannot
     call a guarded method without building a guard, nor can a new route write
     chunks without one. The check runs twice. The first runs after the
     collection resolves and BEFORE the `force_re_embed`, supplied-vector and
     existence-partition branches and before embedding, so no branch skips it,
     a refused write never pays the embedder, and the existence partition's
     committed metadata-only refresh never touches an ownerless chunk of a
     refused request. It is a short read in its own transaction, because the
     embedder call must stay outside any transaction (RDR-181). That leaves a
     window: a chash can lose its last owner while the embed runs, and the
     post-commit sweep (`runSweepTransaction`, immediate, no grace) can then
     delete its chunk row, after which the write's `INSERT ... ON CONFLICT`
     would create a NEW chunk with no owner. So the second check runs inside the
     write transaction, after the embed, under `CatalogRepository.acquireSweepGateShared`
     (the sweep takes the same gate exclusive, so it cannot interleave), on the
     rows the insert will write. The accepted wording of this step, "checked
     inside the write's own transaction", is therefore what is built, with the
     read ahead of the embedder added so a refusal costs no embed.
     `OwnerlessWriteRefusalTest` pins the in-transaction check with the
     `afterNeedEmbedResolvedHookForTests` seam (it deletes the manifest row and the chunk
     during the embed and asserts 422 and no chunk row), pins the shared gate (a
     hook holds the gate exclusive for 1.5 s and the write must wait for it),
     and pins both scoping axes (a chash owned in another collection, or by
     another tenant, authorises nothing; the tenant predicate has its own test
     on a connection that bypasses row-level security, since RLS would hide it
     on the service's connections).
   - *Order of the 4xx answers.* A wrong-width id is 400 (`Chash.requireCanonical`
     in the handler, before the repository is reached); an unregistered
     collection is the "register it first" 422 (`dimForCollection`, the first
     statement of the repository write); the ownership refusal comes after both.
     `OwnerlessWriteRefusalTest` pins the order, including that an ownerless
     write with a throwing embedder is 422, not 503.
   - *Mode.* `NX_OWNERLESS_WRITE_MODE` is `enforce` or `log-only`, and an UNSET
     value means `log-only`: only an explicit `enforce` enforces (conexus's
     condition for the first production deploy, so the engine's first run
     against real traffic refuses nothing until its would-refuse log has been
     read; the flip to enforce is then an environment change, not a tag). A
     local install enforces regardless: the local engine launch
     (`storage_service_daemon._spawn_service`) sets `enforce` explicitly unless
     the variable is already set. Any other value fails the engine boot.
     Log-only writes as before and logs one line per request, counted in
     `ownerless_writes_would_refuse_total` on `GET /v1/status` (enforce counts
     `ownerless_writes_refused_total`, and the status carries
     `ownerless_write_mode`). The engine, not a client-side probe, is the oracle
     for which writers remain: a probe cannot see a subprocess, a shell script
     or a Java HTTP writer.
   - *The log line.* `ownerless_chunk_write_refused` (enforce) and
     `ownerless_chunk_write_would_refuse` (log-only) carry `route`, `tenant`,
     `collection`, `phase` (`pre_embed` or `in_tx`), `unowned`, `requested`,
     `sample` (up to eight chashes), `user_agent`, `client_version`,
     `suppressed_since_last` and `first_chunk_meta` (the first unowned chunk's
     `source_path`, `title` and `source_agent`, 120 characters each). The line is
     rate limited to one per route, tenant and collection per minute, with the number
     suppressed since the last; the counters are not limited. The client names
     itself in `X-Nexus-Client-Version` on every engine request; the log records
     `absent` when it is missing, which marks a client older than the release that
     sends it. The `User-Agent` cannot do that job (`Python-urllib/3.12` or
     `python-httpx/0.28`: the transport, not the product).
   - *Wire.* The 422 body carries `reason: "ownerless_chunk_write"` plus
     `unowned_count`, `requested_count` and `unowned_chashes` (a sample of at
     most eight).
   - *Re-embed.* `nx collection re-embed` reads a live page and writes it back;
     a chunk that lost its owner in between would 422 the whole request. On
     that refusal the client re-reads the batch through the live-filtered get
     and resends only the chashes still owned (`_upsert_reembed_batch`).
   - *`store-put`.* The route stays, guarded by the same check. No client in
     this repository calls it (the note writers moved to `write_many` in
     Phase 2) but nothing here shows it has no production caller, and a route
     is not retired without that evidence; with the guard it can only rewrite
     an owned chunk.
   - *Cutover.* The ledger entry is `[not-additive]`: an installed pre-RDR-223
     client gets 422 on its ownerless writes. Every long-lived `nx-mcp` server
     and every hook-spawned `nx` on a machine runs the code it started with, so
     after the upgrade each must be RESTARTED, not merely upgraded; until it
     is, its writes are refused. The 422 text tells a human what to do
     ("upgrade conexus and restart nx-mcp / Claude Code sessions"). The deploy is
     armed with conexus before the paired client tag is pushed (AGENTS.md,
     nexus-1emxn rule (b)).
6. **The completion stamp goes last, on every writer path.** *Decided by Sam,
   2026-09-30 (T2 `nexus/rdr-223-stamp-last-every-path-decision-2026-09-30`,
   bead nexus-z0o2p.34), extending the PDF and markdown decision of the same
   day.* A document is stamped `index_state='complete'` only AFTER the
   post-store hooks of its write have RUN: chash, taxonomy assignment, aspect
   enqueue and catalog enrichment (title, author, year, chunk count). The
   staleness check skips a complete document whose hash matches, so a stamp
   that rode the write left a document that read complete after a process
   killed in a hook, and nothing would fire the hooks for it again. With the
   stamp last, a kill in a hook leaves the fence `indexing` and the next run
   redoes the write and fires the hooks again (at-least-once).

   The property is "the hooks ran, or the process was killed before the
   stamp", not "the hooks succeeded". The hook registry isolates each hook's
   failure (a taxonomy service that is down, an aspect enqueue that errors):
   such a failure is recorded in T2 `hook_failures`, the document is still
   stamped, and a later assign or enqueue run heals it, as before. A hook
   chain that raises out of the registry (the ChunkBatcher's flush-grain
   chain) skips the stamp for that flush. A stamp that itself fails (a
   transport error, a lost acknowledgement) leaves the fence `indexing` on
   every path, never `failed`: it may have committed, and `failed` would flip
   a `complete` document. A document the engine failed in place on a flush
   write (`failed_doc_ids`) is never stamped: the stamp-only request verifies
   only the row count and that no chunk is missing, so an edit that keeps the
   chunk count would read `complete` with the new hash over the old content.

   The chunks and owner rows still land in ONE request; only the stamp moves.
   Cost, measured by request count and not yet by throughput (Phase 2
   measurement, nexus-z0o2p.26): one stamp request per ChunkBatcher flush
   (at most `MANIFEST_APPEND_MANY_MAX_DOCS`, 1000, documents), one per import
   page that finishes a document, and one `complete_index_run` per oversize
   file, PDF or note. Recovery differs by path: a repo, PDF, markdown or
   import run reruns with its command and finds the document `indexing`. A
   NOTE has no rerun: MCP `store_put`, `nx store put`, `nx memory promote` and
   the recovery import are single calls, so a note killed in a chain, or whose
   stamp failed, stays `indexing` until it is put again.
   `nx catalog reconcile-fences` is deliberately not the remedy (it would
   stamp the note complete without firing the chains that never ran); the
   `nx doctor` row "stale index-run fences" names such notes after six hours
   and says to re-put them (an idempotent re-write that fires the chains and
   stamps the note). Every writer path:
   - `nx index md`/`rdr` (`_index_document`), the small and incremental PDF
     paths and the streaming PDF pipeline: the writer's `defer_completion`,
     with the stamp after the hooks and after the catalog enrichment
     (`_register_in_catalog`, `_catalog_markdown_hook`, which moved ahead of
     the stamp);
   - the three `nx index repo` oversize fallbacks (code, prose, repo PDF):
     `write_oversize_file(defer_completion=True)`, hooks, then
     `complete_oversize_write`;
   - the ChunkBatcher flush: `_batch_flush` sends `write_manifest_many` with no
     `complete`, `ChunkBatcher` calls `on_batch_stamp` after the flush-grain and
     per-file hooks, and the indexer sends ONE stamp-only `append_many` for the
     flush's documents (an empty row list and a `complete` per document, a
     shape `append_many` already accepts, so no engine change);
   - the four note writers (MCP `store_put`, `nx store put`, `nx memory promote`,
     the recovery import): `put_note` writes with no stamp, the producer calls
     `fire_note_chains`, and `stamp_note` sends the stamp. A refused or failed
     stamp leaves the fence `indexing` and the note is reported as uncertain
     (the existing stamp-refused outcome; a failed stamp adds "stays
     'indexing'" to the unstamped wording);
   - the `.nxexp` import: `MultiDocumentImportWriter(defer_completion=True)`
     lands each document's last page with its sweep and no stamp, the importer
     fires that page's chains, and `complete_documents` stamps the documents
     the page finished.
   The `tests/test_vw594_fence_coverage_gate.py` stamp-last leg lists every
   path with its per-function stamp count and the deferral keyword at each
   write call site, and each path has a kill-in-a-hook journey against the
   real engine.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Append with chunks | `CatalogRepository.appendManifestChunks`, `CombinedWriteService` | Extend: add the chunk insert and the existence partition |
| Client-supplied vectors | `PgVectorRepository.upsertChunksWithVectors` | Reuse its dimension check pattern on the combined routes |
| Bulk staging | `StagingHandler` (F-7) | Not used. Retired in Phase 3 (Sam, 2026-09-30); nexus-b50zw closes |

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

Measured 2026-09-30 (T2 `nexus/staging-decision-evidence-2026-09-30`), on a
local bge engine over `nx index repo` of a 3,613-file tree: 13.7 chunks/s,
about 90% of each request spent embedding and about 9% writing, Postgres near
idle, no 429. Staging's `embed_fill` runs the same embedder serially, so it
cannot raise throughput, and `promote` writes chunks with no owner until
`finalize`, which is the ownerless window this RDR closes. Sam retired
staging on that evidence (2026-09-30): Phase 3 Step 2 deletes the routes and
nexus-b50zw closes.

### Briefly Rejected

- **Ownership in chunk metadata** (`store-put`'s `catalog_doc_id`): search and
  the reaper read the manifest, not metadata.
- **Registering note documents in the chunk transaction**: Sam kept the
  existing rollback instead (Decision 5).

## Trade-offs

### Consequences

- Positive: once Phase 3 lands and the engine runs in `enforce` mode,
  `upsert-chunks` and `store-put` insert no chunk that lacks a live manifest
  row in its collection. That is narrower than "no write inserts a chunk
  without an owner". Not covered: (a) `log-only` mode, the default when
  `NX_OWNERLESS_WRITE_MODE` is unset, which writes as before and only logs and
  counts; (b) repository methods called without a guard (the contract and
  fixture tests, the migration ingest, and `upsertReferenceOnlyChunk`, which no
  handler calls since the route was retired); (c) the time after the write:
  the check says the chash is owned when the write commits, not that it stays
  owned (the next two sentences). Chunks a supersede drops still lose their owner; they are swept at the
  document's last batch. After a crash they stay ownerless until the RDR-192
  reaper removes them (nexus-2x9xa, all prefixes since Sam's 2026-09-30
  decision), or `nx t3 gc` does.
- Positive: the note paths get atomic chunk-plus-owner writes with no engine
  change.
- Negative: a crash mid-document still leaves a partial document (some
  batches). It is owned and visible, and the next index run replaces it.
- Negative: old clients that still call `upsert-chunks` or `store-put`
  without an owner break when the refusal enforces. The paired-release floor
  bounds that window, and the single final cut means the engine and its paired
  client release go out together. Sam's condition (2026-09-30): every cloud
  machine upgrades to the new client when the engine deploys (Sam is the only
  cloud tenant; local installs are always paired). A long-lived `nx-mcp`
  server or hook-spawned `nx` keeps the old code until restarted and gets the
  422, so the cutover runbook says restart, not only upgrade (T2
  `nexus/rdr-223-phase2-gate-critique` S4(f)).

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
chunks, with the client killed between two appends, leaves no chunk this
run wrote without an owner; run to completion, its final manifest and chunks
equal a single combined write of the whole document.

### Phase 1: Engine

#### Step 1: Append with chunks

Add the optional `chunks` array to `/v1/catalog/manifest/append` and the
`append_many` form, the `dropped_chashes` response field on `write_many`
with `sweep` off, and the `sweep_chashes` request field on append, as in
Technical Design 1. Record the wire change as an
`[additive]` entry in `docs/wire-contract-pending.md`.

#### Step 2: Client-supplied vectors on the combined routes

Add the optional per-chunk vector with the model and dimension check, as in
Technical Design 2. Rides the single final engine tag (Sam, 2026-09-30, T2
`nexus/rdr-223-192-single-cut-decision-2026-09-30`), not a Phase 1 tag of its
own. The same holds for every Phase 1 and Phase 2 engine change: there is no
intermediate engine tag and no intermediate client release.

### Phase 2: Client migration

#### Step 1: Migrate paths in traffic order

Streaming PDF, MCP `store_put`, `_index_document`, the `nx index repo`
oversize fallbacks, then `nx store put`, `nx memory promote` and
recovery-bundle import. One bead per path. Multi-batch paths write the first
batch with `sweep` off and carry the dropped list to the last append
(Technical Design 1). The `store_put` bead retires and keeps the machinery
listed in Technical Design 3.

#### Step 2: Import

Move `.nxexp` import onto the combined routes with chunks and client-supplied
vectors, keeping the nexus-wbfpw.31 legacy `doc_id` handling. Owner: nexus-76.

Corrected 2026-09-30 (Sam; rulings in T2
`nexus/rdr-223-nxexp-import-rulings-2026-09-30`). This step first said "onto
append". The keep-existing rule of nexus-wbfpw.40 (Sam, 2026-09-29) governs:
an import never replaces or extends the manifest of a document that already
owns chunks, and that document's chunks in the file are skipped and counted.
A first-seen document gets `write_many` and its later pages `append_many`. A
document left `indexing` or `failed` by the same file's earlier dead run is
finished with append. Each document is swept at its own last page and
stamped right after that page's post-store chains (Technical Design 6). The
import sends the exported vectors with `force_re_embed`, so they replace
stored ones.

#### Step 3: Stop the ownerless route

Count identity-less files (A-1), stop writing their chunks, and fix the
registration gap the count exposes.

#### Engine surface Phase 2 carried

Added 2026-10-01 (nexus-wbfpw.39; the Phase 2 gate critique, T2
`nexus/rdr-223-phase2-gate-critique` O1, found none of this in the text).
Phase 2 grew the engine beyond Phase 1's append-with-chunks and client
vectors. Each item has an `[additive]` entry in
`docs/wire-contract-pending.md` and rides the single final engine tag; the
paired client release pins it.

- **Begin snapshot** (nexus-z0o2p.10). `POST /v1/catalog/index-run/begin`
  takes an optional `snapshot_manifest`; the response then carries
  `prior_chashes` (the document's distinct chashes in position order) and
  `prior_count`, read in the same transaction as the `indexing` stamp and
  before any write of the run. This is the pre-run manifest Technical Design
  1 sweeps from, so a resent first batch cannot hide the previous run's tail.
- **`append_many` `complete`** (nexus-z0o2p.19). Each document in an
  `append_many` request may carry `complete: {content_hash, chunk_count}`.
  After that document's rows and chunks land, the engine runs the same
  fail-closed verify as `write_many`'s `complete` (no manifest row names a
  missing chunk and the manifest has exactly `chunk_count` rows) and stamps
  `index_state='complete'` in the same transaction. A refused verify does not
  fail the append; the response lists it as `complete_refused`.
- **`index-run/begin-many` `snapshot_manifest`** (nexus-z0o2p.19). The
  per-document form of the begin snapshot, for the `.nxexp` import.
- **`metadata_merge` and `metadata_delete_keys`** (nexus-z0o2p.13). On
  `write_many`, `append` and `append_many`, a request carrying `chunks` may
  ask that the metadata of a chash the engine already holds be merged
  (`(stored - metadata_delete_keys) || incoming`) instead of replaced. The
  combined routes replaced metadata and the old upsert merged, so without
  this an `_index_document` re-index would have wiped `bib_*` enrichment.
- **Pipeline `reset_uploaded`** (nexus-z0o2p.11; Sam, 2026-09-30, T2
  `nexus/rdr-223-pdf-kill-and-stamp-decisions-2026-09-30`). `POST
  /v1/pipeline/reset_uploaded` puts every chunk of one streaming-PDF run back
  to "not yet uploaded", keeping the extracted pages, the chunks and their
  embeddings, under the same `run_epoch` check as every other pipeline write
  (a stale run gets 409); `GET /v1/pipeline/counts` gains `uploaded_chunks`.
  After a hard kill the rerun re-sends the document from position 0 with a
  fresh writer instead of re-extracting. The engine keeps stored vectors, so
  nothing is re-embedded.
- **`put` lock order** (nexus-z0o2p.12). `write_many` takes each document's
  sweep gate (shared) and index-run lock before it reads the document's
  previous manifest, in `writeManifestRows`' own order, so two concurrent
  replacers of one document cannot both read the same previous manifest and
  strand the loser's chunks. No wire change.

### Phase 3: Refuse ownerless writes

#### Step 1: Move orphan seeding to direct SQL

The `livec_census` tests, the nexus-wbfpw.31 import tests and the RDR-192
reaper and census tests build orphan states through substrate SQL instead of
`upsert-chunks`.

#### Step 2: Refuse

In the single final cut, the engine applies Technical Design 5's rule to
`upsert-chunks` and `store-put`, and deletes `upsert-reference-only`. Delete
the dead paths (`db/reconcile.verify_fill_*`, `db/embed_migrate`) and retire
the staging routes.

Amended 2026-10-01 (nexus-wbfpw.39). This step first read: once the client
release carrying Phase 2 is the paired release, the engine applies Technical
Design 5's rule to `upsert-chunks`, `store-put` and `upsert-reference-only`;
if nexus-b50zw closes, retire the staging routes. Sam's decisions of
2026-09-30 and 2026-10-01 changed it in five ways:

- **One final cut.** There is no Phase 1 engine tag and no Phase 2 client
  release that becomes the Phase 3 floor. The refusal ships in the same final
  engine tag as the Phase 2 engine surface and the RDR-192 engine work, and
  one client release pins that tag (Sam, 2026-09-30, T2
  `nexus/rdr-223-192-single-cut-decision-2026-09-30`). The cut waits for the
  RDR-223 Phase 3 gate and the RDR-192 Phase 4 gate. The intermediate beads
  nexus-z0o2p.9 (engine tag prep) and nexus-z0o2p.22 (paired client release)
  fold into it. The condition is Sam's: every cloud machine upgrades to the
  new client when the engine deploys.
- **Log-only first, then enforce.** The handler ownership check ships first
  in a log-only mode: the engine counts and logs each write it would refuse
  and refuses nothing. The full suite, the local-service gate, native-smoke
  and the battery legs run against a dev jar, every writer the log names is
  listed, and the check flips to enforce only when that list is empty or each
  entry has a disposition. A client-side probe cannot see subprocess, shell or
  Java HTTP writers, so the engine is the only real oracle (T2
  `nexus/review-z0o2p23-25-critique`; orchestrator decisions on nexus-z0o2p.24,
  2026-10-01). Enforcement checks ownership before embedding, so a refused
  write never pays the embedder, and before the `force_re_embed` and
  client-vector branches that skip the embed.
- **The ledger entry is `[not-additive]`.** Old clients get 422 on an
  ownerless write once it deploys, so the deploy is armed with conexus and the
  arming confirmed before the client tag pushes (AGENTS.md, rule nexus-1emxn
  (b)). It must never be written `[additive]` by habit (T2
  `nexus/review-z0o2p34-35-critique` Issue 3).
- **`upsert-reference-only` is retired, not refused.** Sam's condition,
  2026-10-01: retire the route in the final cut if a production log read shows
  no traffic. It showed none: no request to `/v1/vectors/upsert-reference-only`
  in the WAF log for 2026-07-03 to 2026-10-01 (5,567,106 records, 90-day
  retention, with a positive control on `upsert-chunks` and `write_many`),
  and the one apparent hit was a T2 record title in a query string.
  conexus has no caller either. The retirement is a `[not-additive]` ledger
  entry, and `VectorHandlerUpsertReferenceOnlyTest` flips to assert the route
  is gone. The refusal therefore covers `upsert-chunks` and `store-put`.
- **Staging is retired.** Measured first, then decided by Sam on 2026-09-30
  (T2 `nexus/staging-decision-evidence-2026-09-30`, summarised under
  Alternative 2): the staging routes (`/v1/staging`: load, embed_fill,
  promote, finalize, clear, counts) are deleted with their schema and the
  sweep-guard branch that reads them (nexus-z0o2p.27), and nexus-b50zw closes.
  A follow-up bead: `ChunkBatcher` must not bisect a failed flush on a 429 or
  a deadline error.
Built and gated as nexus-z0o2p.24 (Technical Design 5, "As built").

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
  no chunk this run wrote is without an owner.
- **Scenario**: append with chunks carrying known and content-changed chashes
  — **Verify**: known ones are not re-embedded; changed ones are.
- **Scenario**: combined write plus N appends — **Verify**: manifest and
  chunks equal one combined write of the whole document.
- **Scenario**: multi-batch re-index of an unchanged document, first batch
  with `sweep` off — **Verify**: no chunk is swept before its batch lands,
  zero re-embeds, and nothing is swept at the end, because an unchanged
  document drops nothing.
- **Scenario**: multi-batch re-index of a partially changed document, first
  batch with `sweep` off — **Verify**: no chunk is swept before its batch
  lands, and after the last append the chashes the new version dropped are
  swept while re-added and shared ones survive. (Split from the scenario above
  on 2026-10-01: the gate round 2 critique found the single scenario asserted
  a sweep for a document that drops nothing; the journeys under
  `tests/integration/test_rdr223_*_journey.py` already test the two cases
  apart.)
- **Scenario**: client killed before the last append — **Verify**: the
  deferred sweep does not run and no chunk this run wrote is ownerless.
- **Scenario**: supplied vector for an existing chash — **Verify**: the stored
  vector is kept and the mismatch is counted.
- **Scenario**: append with chunks concurrent with a superseded-chunk sweep —
  **Verify**: no deadlock; the sweep does not remove the new chunks.
- **Scenario**: each migrated client path, killed after its first request —
  **Verify**: no chunk that request wrote is without an owner.
- **Scenario**: a gate-xr789-shaped fixture imported through append with
  chunks and supplied vectors — **Verify**: vectors byte-identical, zero
  embedder calls, scattered positions in manifest order, a re-import writes
  nothing. (Corrected 2026-09-30 from "a re-import replaces": under the
  nexus-wbfpw.40 keep-existing rule a second import keeps every document that
  already owns chunks. See Phase 2 Step 2.)
- **Scenario**: supplied vector with the wrong dimension or model —
  **Verify**: refused, nothing stored.
- **Scenario**: Phase 3 refusal — **Verify**: an `upsert-chunks` or
  `store-put` write of a chash with no live manifest row is refused naming
  the combined routes, even when the request names a document, and also with
  `force_re_embed` or client-supplied vectors; ownership is checked before
  embedding, so a refused write never pays the embedder; a re-embed of owned
  chashes is accepted; the seeding helpers still build orphan states. In
  log-only mode the same writes are counted and logged and still land.
  `upsert-reference-only` is not refused: a test asserts the route is gone.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution. The one prior tension (engine staging, F-7, against
append-with-chunks) is resolved in Alternatives; staging is retired (Phase 3
Step 2).

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

- **Versioning**: new wire fields are `[additive]`. The Phase 3 refusal and
  the `upsert-reference-only` retirement are `[not-additive]`, so the deploy
  is armed with conexus before the client tag pushes (AGENTS.md, rule
  nexus-1emxn (b)). All of it ships in one engine tag and one paired client
  release (Sam, 2026-09-30).
- **Deployment model**: engine changes ride an engine tag; conexus deploys.
- **Incremental adoption**: each client path migrates in its own bead;
  unmigrated paths keep working until Phase 3.
- **Build tool compatibility**, **Licensing**, **IDE compatibility**,
  **Secret/credential lifecycle**: N/A.
- **Memory management**: the engine caps `append` and `append_many` at 300
  chunks per request (400 before any transaction or embed) and `sweep_chashes`
  at 300 per append (P1.0). It does not cap `write_many`: released clients send
  it with up to `NX_ONNX_LOCAL_UPSERT_CHUNK_CAP` chunks, a setting with no upper
  bound, so a server cap there could refuse a request an existing client
  legitimately sends. The new client clamps every combined-write request to 300
  (`QUOTAS.MAX_RECORDS_PER_WRITE`) itself. The asymmetry is recorded in the wire
  ledger.

### Proportionality

Right-sized: three phases, one of which (Phase 2) is a set of small
per-path beads.

## Decisions (Sam, 2026-09-28)

1. **Refuse ownerless writes at the end** (Phase 3).
2. **Note paths need no engine change** (settled by checking, F-5). The
   client-side `store_put` machinery is reconciled in Technical Design 3.
3. **Gap 4: stop writing identity-less chunks and find why they lack
   identity.** No ghost documents.
4. **Own RDR**, separate from RDR-222.
5. **Keep nexus-wbfpw.28's registration rollback.** No register-if-absent on
   `write_many`.

## Decisions (Sam, 2026-09-30 and 2026-10-01)

Recorded 2026-10-01 (nexus-wbfpw.39). The records are T2 `nexus` entries
unless stated.

1. **One final engine tag and one paired client release**, shared with
   RDR-192, with the refusal in it. `rdr-223-192-single-cut-decision-2026-09-30`.
2. **The RDR-192 reaper covers every prefix** (`knowledge__`, `docs__`,
   `code__`, `rdr__`), behind a per-collection census gate. Technical Design
   1 and Consequences were amended to match.
   `rdr-223-192-sam-decisions-2026-09-30-reaper-staging`.
3. **Staging is retired**, on measurement.
   `rdr-223-192-sam-decisions-2026-09-30-reaper-staging`,
   `staging-decision-evidence-2026-09-30`.
4. **`upsert-reference-only` is retired**, condition met 2026-10-01 (zero calls
   in 90 days of WAF logs). Bead nexus-z0o2p.24 comments of 2026-10-01.
5. **The refusal ships log-only first, then enforces; its ledger entry is
   `[not-additive]`.** Bead nexus-z0o2p.24; `review-z0o2p23-25-critique`,
   `review-z0o2p34-35-critique`.
6. **The `.nxexp` import and PDF kill rulings**: keep-existing import,
   append-form resume, per-document stamp, `force_re_embed` vector overwrite,
   `taxonomy__*` rejected up front, and the pipeline `reset_uploaded` route.
   `rdr-223-nxexp-import-rulings-2026-09-30`,
   `rdr-223-pdf-kill-and-stamp-decisions-2026-09-30`.
7. **The completion stamp goes last on every writer path.**
   `rdr-223-stamp-last-every-path-decision-2026-09-30`. Technical Design 6
   states the rule.

## References

- T2 `nexus/chunk-owner-write-path-inventory-2026-09-28`
- T2 `nexus_rdr/223-research-1` through `-9`
- `service/src/main/java/dev/nexus/service/db/CatalogRepository.java`
- `service/src/main/java/dev/nexus/service/http/VectorHandler.java`,
  `CatalogHandler.java`, `StagingHandler.java`
- `scripts/sql/livec_census.sql`

## Revision History

- 2026-10-01: Phase 3 Step 2 built (nexus-z0o2p.24): the ownership guard
  (a read before the embed and a recheck inside the write transaction under the
  sweep gate), log-only mode (unset means log-only, local launch enforces) and
  counters, the client-version header and the rate-limited log line, the 410 on
  `upsert-reference-only`, the re-embed answer, and the cutover note (restart
  long-lived `nx-mcp` servers). Technical Design 5 gained an "As built" block.
  Review round 3 narrowed the Consequences line "no write inserts a chunk
  without an owner" to what is enforced (two routes, enforce mode, and the
  residual cases listed there), added the tenant to the log limiter key, and
  pinned the sweep gate and the tenant predicate.

- 2026-09-30: Technical Design 6 added: the completion stamp goes after the
  post-store hooks on every writer path, not only the PDF and markdown paths
  (Sam, 2026-09-30, T2 `nexus/rdr-223-stamp-last-every-path-decision-2026-09-30`;
  found by the Phase 2 gate, T2 `nexus/rdr-223-phase2-gate-code-review` I1 and
  `nexus/rdr-223-phase2-gate-critique` Issue 5; bead nexus-z0o2p.34).

- 2026-09-28: Gate round 2 — PASSED (0 Critical, 2 Significant, 0 ship-blocker(s)); commit `83f6b9197`; critique `nexus_rdr/223-gate-critique-2026-09-28-r2`.

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

- 2026-10-01: Amended for Sam's decisions of 2026-09-30 and 2026-10-01 and
  the Phase 2 gate's text findings (nexus-wbfpw.39; T2
  `nexus/rdr-223-phase2-gate-critique` S1, S3 and O1). The reaper wording in
  Technical Design 1 and Consequences now says all prefixes. Phase 2 records
  the engine surface it carried (begin snapshot, `append_many` `complete`,
  `begin-many` snapshots, `metadata_merge` and `metadata_delete_keys`,
  pipeline `reset_uploaded`, the `put` lock order). Phase 3 Step 2 records the
  single final cut, log-only-then-enforce, the `[not-additive]` ledger entry,
  the `upsert-reference-only` retirement and the staging retirement, with
  Alternative 2, F-7, the infrastructure audit and the Test Plan updated to
  match. The Test Plan's "unchanged document" scenario is split in two. A new
  Decisions section lists the 2026-09-30 and 2026-10-01 decisions.
