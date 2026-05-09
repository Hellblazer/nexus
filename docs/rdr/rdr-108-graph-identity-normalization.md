---
title: "Graph Identity Normalization: Content-Hash Chunk IDs and Cascade-Free Schema"
id: RDR-108
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-08
related_issues: [nexus-jc63, nexus-b5mh, nexus-je0b, nexus-mmf5, nexus-17wf]
related_rdrs: [RDR-053, RDR-101, RDR-103, RDR-106, RDR-107]
supersedes: [RDR-107]
---

# RDR-108: Graph Identity Normalization

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The 2026-05-08 prod-shakeout surfaced three drift symptoms that
appear independent at first read:

- **`chash_index` namespace drift** (nexus-mmf5): T2 routing
  table references 1,697 distinct collection names but T3 has
  only 150 collections; ~1,547 stale routing rows accumulate
  without reconciliation.
- **`document_aspects` 76% orphan rate** (nexus-je0b): of 753
  RDR-089 aspect extractions, 572 reference catalog rows that
  no longer exist (318 legacy collection names, 118 test-fixture
  leakage, 136 other).
- **Code-indexer 25% stale-chunk leakage** (nexus-jc63): every
  re-index after a file edit creates new Chroma natural IDs
  alongside the old ones; 23,714 of 28,512 chunks (83%) in the
  Nexus code collection are involved in some duplicate group.

All three trace to **a single structural root cause**: collection
names and file paths are STRING PROPERTIES copied into every
dependent row, not FOREIGN KEYS pointing to a stable identity.
Every rename or delete therefore leaves orphan denorm copies
behind, with no cascade to reconcile them. Every content edit
that shifts chunk boundaries mints new Chroma natural IDs
because the IDs are derived from position, not from chunk
content.

The fix is normalization: separate identity from naming, the
same way a filesystem separates inodes from pathnames. Rename
becomes a one-row update; chunk content edits leave content-
addressed identities stable.

### What RDR-053 already decided (and what didn't land)

RDR-053 (Xanadu Fidelity, accepted/closed) chose
`chunk_text_hash` as the immutable span identity for catalog
links. RDR-053 RF-13 confirmed that the chunk text sent to
Voyage AI is byte-identical to the text stored in ChromaDB's
`documents` field, so SHA-256 of that text is a stable address
that survives the embedding pipeline.

That decision is half-implemented:

- `chunk_text_hash` is computed and stored on every T3 chunk's
  metadata (`src/nexus/code_indexer.py:408`,
  `src/nexus/doc_indexer.py:944`).
- `chash:<hex>` link spans in the catalog use it correctly.
- **But the Chroma natural ID is still position-derived**:
  `chunk_chroma_id = sha256(f"{corpus}:{title}:chunk{i}")[:32]`
  where `title = "{rel_path}:{line_start}-{line_end}"`
  (`src/nexus/code_indexer.py:380`). This is the routing
  identity ChromaDB uses for `upsert` and `delete`. Because it
  embeds line numbers, any edit that shifts line boundaries
  changes the ID and `upsert` adds rather than replaces.

The wires aren't connected. RDR-053's design intent never
reached the routing layer.

### Enumerated gaps to close

#### Gap 1: `chunk_chroma_id` is position-derived

Position-derived IDs make `upsert` non-idempotent under content
edits. Fix by switching to content-derived IDs.

#### Gap 2: `documents.physical_collection` is a TEXT column, not a FK

Schema at `src/nexus/catalog/catalog_db.py:77` declares
`physical_collection TEXT`. The `collections` table at
`catalog_db.py:173` already exists with `name PRIMARY KEY`,
`superseded_by`, `superseded_at`, `created_at` columns —
intended to be the authority. But no foreign key constraint or
join is wired. Documents carry the collection name as a string
copy.

#### Gap 3: `document_aspects` PK is `(collection, source_path)`

Schema at the `document_aspects` accessor declares the PK as
the (collection, source_path) tuple. When a collection is
renamed or deleted, no cascade updates this table. The 318
legacy-collection-name orphans are a direct consequence: aspect
rows from before RDR-103 still carry the legacy collection
strings.

#### Gap 4: cascade-on-rename works for `chash_index` only

`ChashIndex.rename_collection` (`src/nexus/db/t2/chash_index.py:178`)
exists and is wired. The same cascade for `document_aspects`
does not. Asymmetry IS the bug.

#### Gap 5: `chunk_index` is not a stable identity

`chunk_index` is assigned by position in the chunker output
list (`src/nexus/chunker.py:209`). The `_enforce_byte_cap`
post-processor renumbers all indices when oversized chunks
split (`chunker.py:206-209`). It is a display ordinal, not an
identity primitive that survives rechunking. Anyone treating
it as identity (including the position-derived
`chunk_chroma_id`) is structurally exposed.

## Context

### Background

The 2026-05-08 prod-shakeout (full umbrella in
`~/.claude/projects/-Users-hal-hildebrand-git-nexus/memory/project_2026_05_08_prod_shakeout_umbrella.md`)
ran a comprehensive read-only audit across catalog, T2, and T3
surfaces. The three findings above were filed as separate beads
on the assumption they were independent. The codebase deep-
analyzer subagent (analysis stored at T3
`analysis-normalization-nexus-inode-identity-2026-05-08`)
demonstrated they share a root cause and that RDR-053 had
already chosen the right primitive — it just hadn't propagated
through the routing layer.

User direction (Hal, 2026-05-08): adopt the full normalization
(D1=A, D2=A, D3=A, D4=A — see "Proposed Solution"). RDR-107
(soft-delete tombstones) is superseded before merge.

### Constraints

- **ChromaDB Cloud quotas** (`src/nexus/db/chroma_quotas.py`):
  ≤300 records per `upsert`/`delete`/`get`/`update`. Migration
  must batch.
- **HNSW index immutability**: switching natural IDs requires
  re-upsert (delete + insert), not in-place update. Re-embed
  cost must be considered for any chunk whose original
  embedding cannot be reused; embeddings can be reused if the
  text is identical.
- **Catalog SQLite size**: 64 MB (23,305 documents); migrations
  are cheap — well under a minute per phase.
- **Backward compatibility**: chunks indexed before this RDR
  ships have position-derived IDs and stale rows alongside
  current rows. Migration must reconcile all of them in a
  single sweep, not lazily.
- **RDR-053 already shipped `chunk_text_hash`**: every chunk
  written since RDR-053 carries the field. Pre-RDR-053 chunks
  may not. Verify coverage before relying on it as universal
  identity.

## Research Findings

The codebase deep-analyzer subagent produced a comprehensive
denormalization map and decision matrix on 2026-05-08. Stored
at:

- T3 knowledge store: `analysis-normalization-nexus-inode-identity-2026-05-08`
- T1 scratch: `0823e897-3308-4aa7-b85b-8f0cecb6f10f`

A second pass (T1 scratch
`209bfc7f-9ca3-4c7c-8a89-e8038950ab05`, tagged
`rdr108-q2q3-research-2026-05-08`) and a direct prod probe
(2026-05-08) answered the four questions raised at draft time.
Findings are recorded in T2 as `nexus_rdr/108-research-1`
through `108-research-4`.

### RF-1: `chunk_text_hash` coverage = 99.02% (probe 2026-05-08)

Across all 290,663 T3 chunks:

- **287,803 (99.02%)** have `chunk_text_hash` populated.
- **2,860 (0.98%)** missing the field, concentrated in two
  collections:
  - `taxonomy__centroids`: 2,170 chunks (100% missing). These
    are HDBSCAN cluster centroids written by the topic-mining
    pipeline, not source-content chunks. They have no "chunk
    text" in the conventional sense and cannot participate in
    the new natural-ID scheme as drafted.
  - `docs__scheme-evolution-research-b7de0b63`: 690 chunks
    (10.8% of 6,392 chunks in that collection). Pre-RDR-053
    indexing artifact; the other 89% have it. Re-index of the
    underlying source materials would backfill them.

**Implication for D1**: the chunk-content corpus is effectively
ready for the migration. Two carve-outs needed:

- `taxonomy__centroids` is excluded from RDR-108 Phase 2 (it
  uses a different identity primitive: the centroid hash
  computed at clustering time, stored in the `topics` table).
  Document this exception in Phase 2's command surface
  (`nx t3 reidentify` skips taxonomy collections by default).
- The 690 missing chunks in `docs__scheme-evolution-research-b7de0b63`
  re-index from source as a Phase-2 prerequisite for that
  specific collection (cost: a few minutes of Voyage embedding
  for the 690 affected chunks).

### RF-2: ChromaDB embedding reuse on re-upsert = YES (subagent verified)

`col.add(ids=, documents=, embeddings=, metadatas=)` and
`col.upsert(...)` accept externally-supplied embeddings and
**bypass the collection's embedding function entirely** when
`embeddings=` is non-None. This holds in Cloud mode:
`CloudClient` is a thin REST wrapper, the EF / supplied
decision happens client-side before the HTTP POST.

**Authoritative evidence:**
- ChromaDB SDK source
  `chromadb/api/models/CollectionCommon.py:239-243` (add) and
  `:444-450` (upsert): explicit `if embeddings is None: ...
  else: use as-is` branch. The EF (`_embed_record_set`) is
  only consulted in the `is None` branch.
- Nexus already exercises this in production:
  `src/nexus/db/t3.py:upsert_chunks_with_embeddings` docstring
  states verbatim: "ChromaDB accepts pre-computed embeddings
  when `embeddings=` is supplied to `col.upsert()`, even when
  the collection was created with an EF attached."
- Existing migration pattern at
  `src/nexus/db/t2/catalog_taxonomy.py:2183` already paginates
  `col.get(include=["embeddings"], limit=300, offset=...)` and
  reuses vectors against real Cloud (production-tested).
- ChromaDB cookbook FAQ ("Measure Embedding and Addition
  Performance"): "this will add your documents and the
  generated embeddings without Chroma doing the embedding for
  you internally."

Caveats: (a) supplied embedding dim must match the collection's
locked dim (non-issue: voyage-code-3 and voyage-context-3 are
both 1024-dim); (b) doc-size ≤16384 bytes still enforced;
(c) batch ≤300 records per `upsert` (already chunked).

**Implication for Phase 2**: migration is **CHEAP**. Loop is
`col.get(include=["documents","embeddings","metadatas"])` →
relabel IDs → `col.upsert(..., embeddings=page["embeddings"])`
→ `col.delete(old_ids)`. Zero Voyage API calls, zero $$$.
Dominant cost is paginated GETs (~967 per typical collection).

### RF-3: ChromaDB natural-ID scope = PER-COLLECTION (subagent verified)

Natural IDs are scoped to the collection's segment, not the
database. Two collections can carry the same `id` for
different content with no conflict.

**Authoritative evidence:**
- ChromaDB local schema
  `chromadb/migrations/metadb/00001-embedding-metadata.sqlite.sql`:
  `UNIQUE (segment_id, embedding_id)` — compound, scoped by
  segment (≈ collection). No database-level uniqueness.
- ChromaDB cookbook "Tenancy and DB Hierarchies"
  (https://cookbook.chromadb.dev/core/concepts): records live
  inside collections.
- Nexus already assumes this in
  `src/nexus/db/t2/chash_index.py:60-67` with
  `PRIMARY KEY (chash, physical_collection)`. Module docstring
  (lines 24-29) explicitly cites the same-paper-in-two-
  collections case. Test
  `tests/test_chash_index_store.py:76-92`
  (`test_upsert_allows_same_chash_in_different_collections`)
  locks the contract.

**Implication for D1 + D4**:

- **D1 valid as drafted**: use raw `chunk_text_hash[:32]` as
  the Chroma natural ID. The 4,504 cross-collection
  `chunk_text_hash` dupes observed in the 2026-05-08 probe
  (umbrella row 21) are independent records under the
  per-collection scope, not conflicts.
- **D4 simplification valid**: the `chunk_chroma_id` column in
  `chash_index` becomes `chash[:32]` after migration (a pure
  function of `chash`). The compound PK
  `(chash, physical_collection)` already encodes the
  per-collection scope. Drop the redundant column without
  losing addressability.

### RF-4: Aspect orphan mappability = poor (probe 2026-05-08)

Of the legacy-collection-name aspects flagged in nexus-je0b,
the actual breakdown by mapping path:

- **11 distinct orphan collections** (excluding test fixtures),
  ~453 aspect rows total. (The "318 legacy" figure in the
  earlier probe was approximate; precise counts:
  `rdr__nexus-571b8edd` 224, `rdr__ART-8c2e74c0` 94,
  `knowledge__art-papers` 78, `rdr__1-1__voyage-context-3__v1`
  49, plus 7 collections with 1-2 rows each.)
- **1 collection** has an automated `collections.superseded_by`
  mapping in the catalog
  (`docs__art-architecture → docs__1-2153__voyage-context-3__v1`).
- **0 of 11** mapped via the owner-prefix heuristic
  (`<type>__<owner_token>__<embedding>__v<n>`). The legacy
  hashes don't decode to current owner tumblers.
- **Manual disposition needed for 10 of 11.**

**Implication for D2**: the original Phase 1 plan
("backfill via RDR-103 collection-name authority where
possible") cannot run automated. Three sub-strategies, each a
real choice:

1. **Backfill the supersede chain first** — before Phase 1's
   PK migration runs, populate `collections.superseded_by`
   for the 10 unmapped legacy names by hand-curated review.
   Then Phase 1's automated mapping covers all of them. Cost:
   ~30 minutes of operator time to inspect each legacy-orphan
   collection and decide its successor.
2. **Hard-delete unmapped orphans** — if a legacy collection
   has no clear successor, treat its aspect rows as stale and
   delete. Cost: lose ≤453 rows of LLM-extracted aspect data
   permanently. Lowest operator effort; clean schema; some
   data loss.
3. **Migrate aspects with `collection=NULL`** — preserve the
   data with `collection` set to NULL or the legacy string;
   the new PK is `(doc_id)` so collection becomes a denorm
   cache only. The legacy-string rows still resolve via
   `doc_id` if the underlying document is in the catalog;
   otherwise they are true orphans.

**Recommendation**: option 1 (backfill supersede chain first)
for the 4 high-row-count orphans (`rdr__nexus-571b8edd` 224,
`rdr__ART-8c2e74c0` 94, `knowledge__art-papers` 78,
`rdr__1-1__voyage-context-3__v1` 49 = 445 rows / 98% of the
orphan corpus). Option 2 (hard-delete) for the remaining 7
collections with 1-2 rows each. Decision deferred to gate
time; document operator workflow.

### Open follow-ups (not blocking)

- **`taxonomy__centroids` natural-ID strategy**: separate RDR
  or note in RDR-108 Phase 4. Centroids are computed
  artifacts, not source chunks, and may use the existing
  `centroid_hash` from the `topics` table as their identity.
  Worth tracking as a follow-up bead post-acceptance, not
  blocking RDR-108.
- **`docs__scheme-evolution-research-b7de0b63` partial
  coverage**: Phase-2 prerequisite for that specific
  collection; either re-index the source materials (preferred)
  or hard-delete the 690 pre-RDR-053 chunks (cheaper).
  Operator choice.

## Proposed Solution

Four locked decisions per Hal direction (2026-05-08):

### D1: Chunk Chroma natural ID = `chunk_text_hash`-derived

`chunk_chroma_id` becomes content-derived, not position-derived.

```python
# Current (code_indexer.py:380)
chunk_chroma_id = sha256(f"{corpus}:{title}:chunk{i}").hexdigest()[:32]

# Proposed (per RF-3: per-collection scope eliminates collision risk)
chunk_chroma_id = chunk_text_hash[:32]
```

RF-3 confirmed ChromaDB natural IDs are per-collection
scoped, so raw `chunk_text_hash[:32]` is safe; the same hash
in two different collections is two independent records, not a
conflict.

Consequences:
- `upsert` is truly idempotent. Same chunk text → same Chroma
  ID → replaces in place.
- Stale-chunk accumulation impossible by construction. When a
  chunk's text changes (any byte), the old chunk becomes
  unreferenced and goes to GC via the standard `nx t3 gc`
  path.
- RDR-053 design intent is fully realized: span identity =
  routing identity.

### D2: `document_aspects` PK migration to `(doc_id)`

Schema change:

```sql
-- Current
PRIMARY KEY (collection, source_path)

-- Proposed
PRIMARY KEY (doc_id)
collection TEXT REFERENCES collections(name)  -- denorm cache for read filters
source_path TEXT  -- denorm cache for display
```

`doc_id` is the catalog tumbler (UUID7 per RDR-101). Cascade on
collection rename / document delete becomes a one-row update,
not a JOIN-and-update.

Backfill: for every existing row, look up the matching catalog
document via `(collection, file_path)`. The 318
legacy-collection orphans are mapped via RDR-103 collection-
name authority where possible; the 118 test-fixture orphans
are hard-deleted (they came from CLI tests that should never
have persisted); the 136 other orphans are surfaced for manual
review.

### D3: RDR-107 superseded

This RDR fully replaces RDR-107. The soft-delete approach in
RDR-107 was a half-step that mitigated symptoms without
addressing the structural cause. Status flip to `superseded`
landed in the same PR as this RDR.

The soft-delete pattern remains valid for catalog tombstones
(RDR-106) where the use case is operator-driven undelete, not
content-edit handling. RDR-106 stays unchanged.

### D4: `chash_index` simplified to membership table

Current schema:

```sql
CREATE TABLE chash_index (
    chash                TEXT NOT NULL,
    physical_collection  TEXT NOT NULL,
    chunk_chroma_id      TEXT NOT NULL,  -- redundant when chunk_chroma_id == chash[:32]
    created_at           TEXT NOT NULL,
    PRIMARY KEY (chash, physical_collection)
)
```

Proposed schema:

```sql
CREATE TABLE chash_index (
    chash                TEXT NOT NULL,
    physical_collection  TEXT NOT NULL REFERENCES collections(name),
    created_at           TEXT NOT NULL,
    PRIMARY KEY (chash, physical_collection)
)
```

`chunk_chroma_id` column dropped. The compound PK `(chash,
physical_collection)` retains its existing routing role per
RDR-101 nexus-tcwm. Lookup answer "which collection holds this
chash?" is now a single column read; the row's existence IS the
answer to "is this chash in this collection?".

The cascade-on-rename via `ChashIndex.rename_collection`
(`chash_index.py:178`) survives unchanged — it's still a
`UPDATE physical_collection FROM <old> TO <new>` on this table.

## Alternatives Considered

### Alternative A: Soft-delete tombstones (RDR-107's path)

Position-derived IDs stay; old chunks marked `is_current=False`
on re-index; default-filter retrieval; vacuum verb compacts
after retention window.

**Why rejected**: partial fix. Solves the storage-bloat half of
the problem but leaves the structural root cause. New chunks
still mint new IDs on every line shift; the soft-delete is
forever chasing the position-shift bug. RDR-053 already chose
`chunk_text_hash` as the right identity primitive — adopting
content-derived IDs eliminates the entire class of bugs by
construction. RDR-107 retained as the design exploration that
led here.

### Alternative B: `(doc_id, chunk_index)` as natural ID (position-stable variant)

Use the doc_id tumbler plus chunk_index as the routing
identity: `f"{doc_id}:{chunk_index}"`. When a file is edited
and the chunker re-runs, chunk N is overwritten by the new
chunk N — `upsert` becomes idempotent within a file's chunk
sequence.

**Why rejected**: chunk_index is unstable. The `_enforce_byte_cap`
renumbers all indices when oversized chunks split. So
"chunk 5" before the edit may not be "chunk 5" after — same
identity, different content semantically. Routing breaks.
Content-derived IDs (Alternative D1=A) avoid this by anchoring
on text, not position.

### Alternative C: D2 only (cascade-on-rename for `document_aspects`)

Wire `Catalog.rename_collection` to also UPDATE
`document_aspects` SET collection = new WHERE collection = old.
Cheaper than PK migration, but leaves the existing 318
legacy-collection orphans unfixed (their old collection names
are deleted, not renamed, so the cascade doesn't fire).

**Why rejected**: half-fixes drift but doesn't reconcile the
existing rot. PK migration is a one-time cost that resolves
both future drift AND existing orphans.

### Alternative D: Do nothing (let drift compound)

Accept the existing drift, surface it via doctor checks, and
let operators clean up on demand.

**Why rejected**: drift compounds. Every renamed collection
adds new orphans. Every code-file edit adds new stale chunks.
Doctor checks become noise. The cleanup cost only grows.

### Alternative E: Extend RDR-101 in-place

Roll D1-D4 into RDR-101 as a Phase 6 follow-up.

**Why rejected**: RDR-101 is closed. Reopening to add a phase
breaks the gate-then-implement discipline. RDR-108 cleanly
imports RDR-101's primitives (UUID7 doc_id, collections table)
without amending the closed document.

## Trade-offs

| Dimension | RDR-108 (D1=A, D2=A, D4=A) | Soft-delete (RDR-107) | Cascade-only (D2 alone) | Do nothing |
|---|---|---|---|---|
| Chunk drift | structurally impossible | mitigated, retention-window-bounded | persists | persists |
| Aspect drift (existing 318) | fixed | persists | persists | persists |
| Aspect drift (future) | fixed | persists | fixed | persists |
| chash_index drift | reduced (FK-enforced) | persists | persists | persists |
| RDR-053 design intent | fully realized | partial | unaffected | unaddressed |
| T3 migration cost | re-upsert 290k chunks | none (incremental) | none | none |
| Catalog migration cost | document_aspects PK swap | none | document_aspects cascade only | none |
| Doctor-check noise | minimal | persists at low rate | aspect-drift only | grows |
| Reversibility | reversible (re-upsert old IDs from backup) | trivially reversible (flag flip) | trivially reversible | N/A |

The RDR-108 path trades a one-time migration cost for permanent
elimination of three bug classes. The soft-delete path trades
zero migration for ongoing operator burden (vacuum schedule,
retention tuning, drift monitoring).

## Implementation Plan

### Phase 0: Approve decisions D1-D4 (gate)

This RDR's finalization gate. D1, D2, D3, D4 are locked per
user direction (2026-05-08); the gate verifies (a) the
research questions in §"Research Findings" are answered and
(b) the migration cost estimate is plausible.

### Phase 1: Catalog schema migrations (cheap, no T3 touch)

- `documents.physical_collection`: declare a foreign key
  reference to `collections(name)`. Backfill `collections`
  rows for any physical_collection value that doesn't have
  one yet.
- **Pre-step (per RF-4)**: hand-curate
  `collections.superseded_by` mappings for the 4 high-row-count
  legacy orphan collections (`rdr__nexus-571b8edd` 224,
  `rdr__ART-8c2e74c0` 94, `knowledge__art-papers` 78,
  `rdr__1-1__voyage-context-3__v1` 49 = 445 rows / 98% of
  legacy-orphan corpus). Operator inspects each and decides
  the current target. The remaining 7 collections (1-2 rows
  each) are hard-deleted at the end of Phase 1.
- `document_aspects` PK migration:
  1. Add `doc_id TEXT NOT NULL DEFAULT ''` column.
  2. Backfill: for each row, look up the catalog doc_id via
     `(collection, file_path)` JOIN. For rows whose collection
     was just supersede-mapped, follow the supersede chain
     before the JOIN. Hard-delete the 118 test-fixture
     orphans. Surface any remaining unmapped rows for manual
     review.
  3. Swap PK from `(collection, source_path)` to `(doc_id)`.
  4. Keep `collection` and `source_path` as denorm cache
     columns for read filters and display.
- Wire `Catalog.rename_collection` to UPDATE
  `document_aspects.collection` (and any other dependent
  table discovered during research).

### Phase 2: Chunk Chroma natural ID switch (T3 re-upsert)

**Per RF-2: migration is cheap, no Voyage re-embed.**

Per collection, paginated:

1. Fetch all chunks via
   `col.get(limit=300, offset=..., include=["documents", "embeddings", "metadatas"])`.
2. For each chunk, compute new natural ID = `chunk_text_hash[:32]`
   (per RF-3, raw form, no collection scoping needed).
3. If the new ID differs from the old, re-upsert under the new
   ID with `embeddings=page["embeddings"]` (existing vector,
   no Voyage call per RF-2).
4. After all chunks for a collection are re-upserted, delete
   the old IDs in batches of 300.
5. Update `chash_index.chunk_chroma_id` to match (or drop the
   column per Phase 3).

This phase is collection-by-collection to bound risk. A
failure mid-collection is recoverable by replaying the same
phase against that collection.

**Carve-outs per RF-1**:
- `taxonomy__centroids`: skip; uses centroid-hash identity
  from the `topics` table, not chunk_text_hash.
- `docs__scheme-evolution-research-b7de0b63`: re-index 690
  pre-RDR-053 chunks from source as a pre-step (only ~few
  minutes of Voyage embedding for that subset; the rest of
  the collection migrates normally).

### Phase 3: chash_index simplification

- Drop `chunk_chroma_id` column from `chash_index`.
- The (chash, physical_collection) compound PK and routing
  semantics survive unchanged.
- Update `ChashIndex` accessor methods to match the new
  schema; ensure `delete_stale` and `rename_collection` keep
  working.
- Reconcile (Phase 1's migration may already have done this):
  drop chash_index rows whose physical_collection has no
  matching `collections` row.

### Phase 4: Documentation + cleanup

- Update `CLAUDE.md`: the "Critical conventions" §"Hot rules"
  needs a new entry on chunk identity = `chunk_text_hash`.
- Update `docs/architecture.md` with the normalized schema.
- Remove RDR-107's soft-delete plumbing if any landed
  (verify nothing did before this RDR's gate).
- Update `nx doctor` to surface drift between
  `documents.physical_collection` and `collections.name`
  (FK violations).

### Phase 5: Verification

- Re-run the 2026-05-08 prod-shakeout probes:
  - chash_index distinct collections should == T3 collection
    count
  - document_aspects orphan rate should be 0%
  - code__1-2188 dupe rate should be 0%
- Run a re-index of any code repo and verify zero stale chunks
  accumulate.

## Test Plan

- **Regression — content-derived ID stability**: re-index a
  code file twice with a line-shift edit (insert a function at
  the top so all subsequent chunks shift line numbers). Assert
  (a) `chunk_chroma_id` is unchanged for chunks whose text
  didn't shift (only their position did), (b) chunks whose
  text changed get new IDs, (c) old IDs for replaced chunks
  are physically removed by the standard `nx t3 gc` path.
- **Embedding reuse on re-upsert** (per RF-2): build a
  fixture with N chunks whose embeddings are known. Run
  `col.get(include=["embeddings"])`, re-upsert under new IDs
  with `embeddings=` populated, fetch back, assert the
  embeddings are byte-identical (no re-embedding occurred).
  Catches any Cloud-mode regression that bypasses the
  externally-supplied path.
- **Per-collection ID scope** (per RF-3): insert the same
  `chunk_text_hash[:32]` as a natural ID into two different
  collections with distinct content. Assert (a) both `add`
  calls succeed, (b) `col_A.get(ids=[chash])` and
  `col_B.get(ids=[chash])` return the distinct content.
- **Aspect PK migration backfill**: fixture with five rows:
  (a) matching catalog, (b) legacy-collection-name with
  hand-curated supersede mapping, (c) legacy-collection-name
  with NO supersede (test-fixture pattern), (d) test-fixture
  collection (`knowledge__cli-...`), (e) collection unknown
  to catalog. Assert (a) and (b) migrate to the correct
  doc_id, (c) and (d) are hard-deleted, (e) is surfaced for
  manual review (no silent loss).
- **Catalog rename cascades to document_aspects**: rename a
  collection via `Catalog.rename_collection`; assert the
  denorm `collection` column on `document_aspects` rows
  updates atomically with the FK target.
- **chash_index FK enforcement**: attempt to INSERT a
  chash_index row pointing at a non-existent
  `physical_collection`; assert FK violation. Then attempt
  delete-cascade behavior: delete a collection row; assert
  chash_index rows referencing it are removed (or rejected if
  ON DELETE RESTRICT).
- **Cross-collection chash routing**: insert the same
  `chunk_text_hash` into two collections; assert
  `chash_index.lookup(chash)` returns both rows.
- **Quota compliance**: migration batches respect
  `chroma_quotas.MAX_RECORDS_PER_WRITE = 300` for both
  `col.get` (page size) and `col.upsert` / `col.delete`
  (batch size).
- **Carve-out — `taxonomy__centroids`**: assert
  `nx t3 reidentify` skips this collection by default and
  emits a structured log entry naming it as exempt.
- **Carve-out — pre-RDR-053 partial-coverage collection**:
  assert that running Phase 2 against
  `docs__scheme-evolution-research-b7de0b63` requires the
  690-chunk re-index pre-step or fails loud.

## Validation

### Testing Strategy

(to be filled during /nx:rdr-research)

## Finalization Gate

> Complete each item with a written response before
> marking this RDR as **Accepted**. Written responses
> prevent rubber-stamping and produce a review record.

### Contradiction Check

(to be filled at gate time)

### Assumption Verification

(to be filled at gate time)

### Scope Verification

(to be filled at gate time)

### Cross-Cutting Concerns

- **Versioning**: catalog SQLite schema bump (document_aspects
  PK swap, chash_index column drop, FK declaration on
  documents.physical_collection). T3 chunk metadata schema
  unchanged (chunk_text_hash already present per RDR-053). T3
  natural-ID migration is data-level, not schema-level.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: ships in conexus wheel; first run
  after upgrade triggers Phase 1 catalog migrations
  automatically. Phase 2 (T3 re-upsert) is operator-driven via
  `nx t3 reidentify --collection ...` to bound the time and
  cost.
- **Incremental adoption**: Phase 1 is mandatory at upgrade
  (catalog migrations). Phase 2 is per-collection on operator
  demand; until run, the collection retains old position-
  derived IDs and continues to leak stale chunks. Phase 3
  follows Phase 2 globally.
- **Memory management**: re-upserting 290k chunks fits within
  ChromaDB Cloud quotas if batched at 300/op. No new
  memory pressure.
- **Secret/credential lifecycle**: N/A.

### Proportionality

Right-sized. The migration is a one-time cost (catalog: under
a minute; T3: hours per large collection, parallelizable
across collections) that structurally eliminates three bug
classes documented in five filed beads. Multi-week
implementation matches the value: the alternative is operator
drift-monitoring forever. Half-fixes (RDR-107 soft-delete,
D2-only cascade) trade migration cost for permanent operator
burden — the wrong direction for nexus's robot-mode
disposition.

## References

### Beads addressed

- nexus-jc63 (P0): chunk soft-delete steady-state — superseded
  by D1=A in this RDR.
- nexus-b5mh (P1): one-shot stale-chunk reconciliation —
  superseded by Phase 2 in this RDR.
- nexus-je0b (P1): document_aspects 76% orphan rate — fixed by
  D2=A in this RDR.
- nexus-mmf5 (P1): chash_index namespace drift — fixed by D4=A
  + FK enforcement on documents.physical_collection in this
  RDR.
- nexus-17wf (P2): low-confidence aspect rows — orthogonal,
  not addressed here.

### Related RDRs

- RDR-053 (Xanadu Fidelity, accepted/closed): chose
  `chunk_text_hash` as immutable span identity. RDR-108
  completes the design by making it the routing identity too.
- RDR-101 (Catalog T3 Metadata Design, closed): established
  UUID7 doc_id and the event-sourced catalog. RDR-108 builds
  on these.
- RDR-103 (Catalog as Collection-Name Authority, closed):
  established the `collections` table. RDR-108 makes
  `documents.physical_collection` a foreign key to it.
- RDR-106 (Soft-Delete via Tombstone Columns on Catalog
  Projection, draft): catalog-projection-layer soft-delete.
  Independent of RDR-108; both can ship.
- RDR-107 (T3 Chunk Soft-Delete via Tombstone Metadata,
  superseded by this RDR): the partial-fix predecessor.

### Probes / analysis

- 2026-05-08 prod-shakeout umbrella memory.
- Subagent denormalization analysis: T3
  `analysis-normalization-nexus-inode-identity-2026-05-08`,
  T1 scratch `0823e897-3308-4aa7-b85b-8f0cecb6f10f`.
