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

(to be filled during /nx:rdr-research)

The codebase deep-analyzer subagent produced a comprehensive
denormalization map and decision matrix on 2026-05-08. Stored
at:

- T3 knowledge store: `analysis-normalization-nexus-inode-identity-2026-05-08`
- T1 scratch: `0823e897-3308-4aa7-b85b-8f0cecb6f10f`

Open questions to research:

1. **Coverage of `chunk_text_hash`**: every chunk written since
   when? Pre-coverage chunks need a backfill before they can
   participate in the new identity scheme. Estimate the
   pre-coverage chunk count and design the backfill or accept
   re-embed cost.
2. **Embedding reuse during migration**: can ChromaDB reuse a
   stored embedding when re-upserting under a new natural ID?
   If yes, the migration is metadata-only at the embedding
   layer (cheap). If no, every chunk re-embeds (expensive
   Voyage call).
3. **Cross-collection `chunk_text_hash` collision**: same
   chunk text legitimately lives in multiple collections (4,504
   cross-collection dupes observed in the 2026-05-08 probe,
   row 21 of the umbrella). The natural ID must include the
   collection scope: `(collection, chunk_text_hash)` becomes
   the compound identity. Verify ChromaDB's natural-ID model
   supports this — the natural ID is a string, so it must
   encode both: e.g. `f"{collection_id}:{chunk_text_hash[:32]}"`
   or rely on per-collection scoping that ChromaDB already
   provides. Confirm via ChromaDB docs / test.
4. **Aspect orphan backfill correctness**: the 318
   legacy-collection-name orphans need to be mapped to current
   collection names via RDR-103 collection-name authority. Is
   that mapping deterministic for every legacy name, or are
   some unrecoverable?

## Proposed Solution

Four locked decisions per Hal direction (2026-05-08):

### D1: Chunk Chroma natural ID = `chunk_text_hash`-derived

`chunk_chroma_id` becomes content-derived, not position-derived.

```python
# Current (code_indexer.py:380)
chunk_chroma_id = sha256(f"{corpus}:{title}:chunk{i}").hexdigest()[:32]

# Proposed
chunk_chroma_id = chunk_text_hash[:32]  # already a sha256 hex
# OR if cross-collection scoping isn't free:
chunk_chroma_id = sha256(f"{collection}:{chunk_text_hash}").hexdigest()[:32]
```

Decided during /nx:rdr-research after question 3 above.

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
- `document_aspects` PK migration:
  1. Add `doc_id TEXT NOT NULL DEFAULT ''` column.
  2. Backfill: for each row, look up the catalog doc_id via
     `(collection, file_path)` JOIN. Hard-delete the 118
     test-fixture orphans. Surface the 136 other orphans for
     manual review.
  3. Swap PK from `(collection, source_path)` to `(doc_id)`.
  4. Keep `collection` and `source_path` as denorm cache
     columns for read filters and display.
- Wire `Catalog.rename_collection` to UPDATE
  `document_aspects.collection` (and any other dependent
  table discovered during research).

### Phase 2: Chunk Chroma natural ID switch (T3 re-upsert)

Per collection, paginated:

1. Fetch all chunks via `col.get(limit=300, offset=...)` with
   metadatas.
2. For each chunk, compute new natural ID from
   `chunk_text_hash` (per question-3 resolution).
3. If the new ID differs from the old, re-upsert under the new
   ID. Reuse the existing embedding if ChromaDB allows
   (per question-2 resolution); otherwise re-embed.
4. After all chunks for a collection are re-upserted, delete
   the old IDs in batches of 300.
5. Update `chash_index.chunk_chroma_id` to match (or drop the
   column per Phase 4).

This phase is collection-by-collection to bound risk. A
failure mid-collection is recoverable by replaying the same
phase against that collection.

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

(to be filled during /nx:rdr-research)

Sketch:

- **Regression**: re-index a code file twice with a line-shift
  edit; assert (a) chunk_chroma_id is unchanged for chunks
  whose text didn't shift, (b) chunks whose text changed
  result in new IDs, (c) old IDs are physically replaced (not
  duplicated).
- **Aspect PK migration**: backfill fixture with three rows
  (matching catalog, legacy-collection-name with mappable
  RDR-103 successor, legacy-collection-name with no successor);
  assert all three resolve to the right state.
- **Cascade test**: rename a collection via
  `Catalog.rename_collection`; assert document_aspects rows
  pointing at the old name are updated to the new name (or
  unchanged if PK is doc_id-keyed and collection is denorm).
- **chash_index FK enforcement**: insert a chash_index row
  pointing at a non-existent collection; assert it raises (or
  is silently dropped if the FK is `ON DELETE CASCADE`).
- **Cross-collection chash collision**: insert the same
  `chunk_text_hash` into two collections; assert both rows
  exist (compound PK satisfied) and routing returns both.
- **Quota compliance**: migration batches respect
  MAX_RECORDS_PER_WRITE = 300.

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
