---
title: "Graph Identity Normalization: Catalog Holds the Tree, T3 is a Content-Addressed Blob Store"
id: RDR-108
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-08
revised: 2026-05-08
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

### RF-5: Chunk-order reconstruction post-migration = unaffected (code-trace 2026-05-08)

Concern: with content-derived natural IDs, does the order of
chunks within a document survive migration? Found via grep + read:

- Order reconstruction reads `chunk_index` from each chunk's
  metadata. Confirmed at `src/nexus/catalog/synthesizer.py:616-672`
  (`chunk_index = int(meta.get("chunk_index", 0) or 0)` →
  `position=chunk_index`).
- Migration touches only the Chroma natural ID, not metadata.
  `chunk_index` survives byte-for-byte.
- For multi-chunk doc reconstruction, the existing pattern is
  `col.get(where={"doc_id": tumbler})` then sort by
  `chunk_index` ascending. Post-migration this still works.

**Implication**: order reconstruction is unaffected by D1.
Pre-existing characteristic that `chunk_index` is unstable
across re-index runs (boundaries shift) is unchanged: RDR-108
neither fixes nor worsens it. A separate RDR could address
position-stable identity within a doc, but it is out of scope
here.

### RF-6: Migration idempotency / interruption recovery = achievable with filter-based loop (code-trace 2026-05-08)

Concern: if Phase 2 crashes mid-collection, can it be safely
resumed? Existing nexus migrations don't surface an explicit
checkpoint pattern (grep for `resume`/`checkpoint`/`partial`
in `t3.py` + `commands/t3.py` returned only generic comments).
The loop design needs to be inherently idempotent.

**Recommended loop structure for `nx t3 reidentify`**:

```python
# Per-collection, paginated, idempotent.
seen_old_ids: set[str] = set()
offset = 0
while True:
    page = col.get(
        limit=300, offset=offset,
        include=["documents", "embeddings", "metadatas"],
    )
    if not page["ids"]:
        break

    to_migrate = []
    for cid, doc, emb, meta in zip(page["ids"], page["documents"],
                                    page["embeddings"], page["metadatas"]):
        new_id = meta["chunk_text_hash"][:32]
        if cid == new_id:
            continue  # already migrated, skip silently
        to_migrate.append((cid, new_id, doc, emb, meta))
        seen_old_ids.add(cid)

    if to_migrate:
        col.upsert(
            ids=[t[1] for t in to_migrate],
            documents=[t[2] for t in to_migrate],
            embeddings=[t[3] for t in to_migrate],
            metadatas=[t[4] for t in to_migrate],
        )

    if len(page["ids"]) < 300:
        break
    offset += 300

# Phase 2b: delete old IDs in batches of 300.
for batch in batched(seen_old_ids, 300):
    col.delete(ids=list(batch))
```

Key properties:
- Re-running on a fully-migrated collection: every page has
  `cid == new_id`, skip-silent, zero writes. Naturally idempotent.
- Re-running after partial migration: pages mix
  already-migrated and not-yet-migrated chunks. The filter
  correctly skips the former, processes the latter.
- Pagination doesn't break: deletes happen in Phase 2b after
  the get-loop completes, so offsets stay valid during reads.
- If the process crashes mid-collection: Phase 2b never ran
  for the un-deleted old IDs. Resume re-runs the get-loop,
  finds the un-deleted old IDs again, re-upserts (idempotent
  overwrite of new ID), re-collects them in `seen_old_ids`,
  Phase 2b deletes them. No data loss; some redundant work.

**Implication**: Phase 2 can be safely resumed without state
file or checkpoint table. Document the contract in `nx t3
reidentify`'s help text.

### RF-7: Existing `chash:<hex>` link backward compatibility = unaffected by D1; one code change for D4 (code-trace 2026-05-08)

Concern: existing catalog links use `chash:<full_hex>` spans.
After D1 (chunk natural ID = `chunk_text_hash[:32]`) and D4
(chash_index drops `chunk_chroma_id` column), do existing
links still resolve?

- `src/nexus/catalog/catalog_spans.py:89-122` (`resolve_span_in_t3`)
  resolves a chash span via
  `col.get(where={"chunk_text_hash": hex_chash}, include=["documents", "metadatas"])`.
  This is **already content-keyed**, not natural-ID-keyed.
  Post-D1 migration, this path works unchanged.
- `src/nexus/catalog/catalog_spans.py:300-329` (`resolve_chash_globally`)
  reads `row["chunk_chroma_id"]` from `chash_index` and passes
  it as `doc_id=` to `_build_ref`. After D4 drops that column,
  this code must be updated. Replacement: `doc_id=hex_chash[:32]`
  (a pure function of the chash that the call already has).

**Implication for D4**: one explicit code change in
`catalog_spans.py:327`. Phase 3 (chash_index column drop)
must be paired with this code update in the same commit;
otherwise `resolve_chash_globally` KeyErrors on missing column.

Caveat: `chash_index.upsert` (`src/nexus/db/t2/chash_index.py:105`)
takes a `chunk_chroma_id` parameter and inserts into the
`chunk_chroma_id` column. Phase 3 removes both. The call sites
(post-store hooks for chunk-grain inserts) need their parameter
list updated. Trivial change: grep `chash_index.upsert` to
find them.

### RF-8: ChromaDB "documents" ≠ catalog Documents — naming clash was masking the right architectural split (gate-revision 2026-05-08)

The Layer 3 substantive critique (gate run 2026-05-08) uncovered
that the original D1 (raw `chunk_text_hash[:32]` as natural ID)
caused silent data loss for identical-text chunks within the
same collection — RDR-101 had explicitly rejected this. The fix
candidates proposed during gate review (`sha256(doc_id:chash)`,
`sha256(chash:chunk_index)`, etc.) all carried tradeoffs; none
were structurally clean.

User-Hal (2026-05-08, post-gate) reframed the problem:

> Docs are a graph concept and live in the catalog. T3 has
> collections, not documents. What ChromaDB means by "document"
> is our chunks. A doc collection is a collection of chunks in
> a DB.

This unmasked the actual architectural split that the original
RDR-108 draft was fighting:

| Layer | What "document" means there |
|---|---|
| **Catalog** (graph layer) | Nexus's domain document — a tumbler-keyed entity. Graph node. |
| **ChromaDB** (T3) | Per `col.add(documents=...)`: just the text of one chunk. ChromaDB has zero awareness of multi-chunk structure. A T3 collection is a flat bag of these. |

Once the responsibility split is clear:

- **T3 = content-addressed blob store**. Flat. Records keyed by
  `chunk_text_hash[:32]`. ChromaDB doesn't model relationships,
  ordering, or composition, and shouldn't.
- **Catalog = graph + tree layer**. Documents are graph nodes.
  Their structure (which chunks they contain, in what order)
  lives in an explicit manifest field on `Document`, not as
  per-chunk metadata in T3.

This matches the git/IPFS model directly:

| git/IPFS | Nexus under RDR-108 (this revision) |
|---|---|
| Blob (content-addressed object) | T3 chunk record, natural ID = `chunk_text_hash[:32]` |
| Tree (manifest of blobs) | `Document.chunks` = ordered list of chash references |
| Commit (history pointer) | `Document.head_hash` (already present) |

**Implication**: D1 stays as `chunk_text_hash[:32]` (Xanadu-pure),
because the within-doc collision concern from the original gate
review evaporates: identical chunks in the same doc share ONE
T3 record by design (content addressing), and the manifest
records each occurrence's position. The "second occurrence
silently lost" critique no longer applies — both occurrences
appear as separate manifest entries pointing at the same
content.

**Net effect on the RDR**:

- Original D1 (raw chash[:32]) is restored, justified by the
  manifest layer.
- New D2 added: catalog gains `Document.chunks` manifest.
- Original D2 (document_aspects PK migration) renumbers to D3,
  expanded to also cover `aspect_extraction_queue` per the
  Layer 3 critique's C3 finding.
- Original D4 (chash_index simplification) renumbers to D5 and
  becomes more thorough: with chash[:32] as the universal
  natural ID, the `chunk_chroma_id` column has no remaining
  readers (collection_audit, mcp/core, commands/store, prose_indexer
  all simplify or drop their references). The Layer 3 C1
  finding (27 chunk_chroma_id sites) now has dispositions:
  most disappear, the rest reduce to `hex_chash[:32]`.
- Chunk metadata schema simplifies: `doc_id`, `chunk_index`,
  `chunk_count` move OUT of T3 chunk metadata into the catalog
  manifest. T3 chunks carry only filter-relevant fields
  (content_type, section_type, embedding_model, etc.).

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
- **Migration bandwidth cost**: 290k chunks × ~4 KiB
  embedding + ~2 KiB doc + metadata ≈ 2-3 GiB total bandwidth
  from ChromaDB Cloud. ~5,000 paginated GETs across 150
  collections. Estimated runtime: 1-3 hours for the full
  corpus. Operator-driven phasing is appropriate.

## Proposed Solution

Five locked decisions per Hal direction (2026-05-08, post-gate
revision after RF-8 reframing):

### D1: T3 chunk Chroma natural ID = `chunk_text_hash[:32]` (pure content addressing)

`chunk_chroma_id` becomes a pure function of chunk content:

```python
# Current (code_indexer.py:380, prose_indexer.py:101, mcp/core.py:973, ...)
chunk_chroma_id = sha256(f"{corpus}:{title}:chunk{i}").hexdigest()[:32]

# Proposed
chunk_chroma_id = chunk_text_hash[:32]
```

RF-3 confirmed ChromaDB natural IDs are per-collection scoped,
so raw `chunk_text_hash[:32]` is safe across collections. The
within-doc collision concern that surfaced during the Layer 3
gate review is structurally resolved by D2 (manifest layer):
identical text in the same doc shares ONE T3 record by design;
the manifest records each occurrence's position.

Consequences:
- `upsert` is truly idempotent. Same chunk text → same Chroma
  ID → replaces in place.
- Stale-chunk accumulation impossible by construction. When a
  chunk's text changes (any byte), the old chunk becomes
  unreferenced and goes to GC via the standard `nx t3 gc`
  path.
- RDR-053 design intent is fully realized: span identity =
  routing identity.
- Identical chunks across the corpus are deduplicated at the
  storage layer. Two files containing the same boilerplate
  contribute ONE T3 record; the manifest layer (D2) records
  the references.

### D2: Catalog `Document.chunks` manifest (the tree layer) — NEW

The catalog gains an explicit per-document chunk manifest. T3
remains a flat content-addressed blob store; the catalog is
the authoritative source for "which chunks compose this doc and
in what order."

Schema addition (catalog SQLite, `documents` table):

```sql
-- Either as a column on documents:
ALTER TABLE documents ADD COLUMN chunks JSON NOT NULL DEFAULT '[]';

-- Or as a separate table for normalized many-to-one:
CREATE TABLE document_chunks (
    doc_id     TEXT NOT NULL REFERENCES documents(tumbler),
    position   INTEGER NOT NULL,
    chash      TEXT NOT NULL,
    chunk_index INTEGER,        -- chunker-assigned ordinal at index time (informational)
    line_start  INTEGER,        -- optional, for code chunks
    line_end    INTEGER,        -- optional, for code chunks
    char_start  INTEGER,        -- optional, byte offset
    char_end    INTEGER,        -- optional, byte offset
    PRIMARY KEY (doc_id, position)
);
CREATE INDEX idx_document_chunks_chash ON document_chunks(chash);
```

The separate-table form is preferred (better for queries,
avoids JSON parsing on every read, supports cross-doc chash
lookup via the index). Decision deferred to implementation.

Manifest semantics:
- `position` = 0-indexed ordinal within the doc (replaces
  `chunk_index` on T3 chunk metadata).
- `chash` = full 64-char SHA-256 of chunk text (the chunk's
  identity in T3 is `chash[:32]`).
- Same `chash` may appear at multiple `(doc_id, position)`
  rows when a chunk's text recurs in the doc — manifest
  preserves position; T3 stores content once.
- Same `chash` may appear in multiple docs — same chash,
  different doc_id rows. Cross-doc dedup at the storage layer.

Doc reconstruction (replaces `where={"doc_id": tumbler}` +
metadata sort):

```python
manifest = catalog.get_chunks(doc_id)  # ordered list[(position, chash)]
chashes = [m.chash[:32] for m in manifest]
chunks = col.get(ids=chashes, include=["documents", "metadatas"])
# preserve manifest order (col.get may reorder by ID)
ordered = {c.id: c for c in chunks}
return [ordered[chash[:32]] for chash in chashes]
```

T3 chunk metadata simplifies (removes per-chunk doc identity):

| Field | Pre-RDR-108 | Post-RDR-108 |
|---|---|---|
| `chunk_text_hash` | present | present (authoritative; the natural ID is `[:32]` of this) |
| `content_hash` | present | present (file-level, useful for stale-source detection) |
| `content_type` | present | present (filter-relevant) |
| `section_type` | present | present (filter-relevant) |
| `programming_language` | present | present (filter-relevant) |
| `embedding_model` | present | present (filter-relevant) |
| `indexed_at` | present | present (operational) |
| `doc_id` | present | **REMOVED** (now in catalog manifest) |
| `chunk_index` | present | **REMOVED** (now `position` in manifest) |
| `chunk_count` | present | **REMOVED** (catalog manifest length) |
| `source_path` | sometimes | already removed in RDR-101 Phase 5c |

### D3: `document_aspects` + `aspect_extraction_queue` PK migration to `(doc_id)`

(Combines original D2 with the Layer 3 critique's C3 finding.)

Both tables migrate from `(collection, source_path)` PK to
`(doc_id)` PK. The two are coupled: `aspect_extraction_queue`'s
`mark_done(collection, source_path)` signals completion to the
extractor; if `document_aspects` migrates without the queue,
in-flight entries lose their completion signal.

Schema change (both tables):

```sql
-- aspect_extraction_queue
ALTER TABLE aspect_extraction_queue ADD COLUMN doc_id TEXT NOT NULL DEFAULT '';
-- backfill via JOIN, then:
ALTER TABLE aspect_extraction_queue DROP CONSTRAINT pk_aspect_extraction_queue;
ALTER TABLE aspect_extraction_queue ADD PRIMARY KEY (doc_id);
-- collection, source_path retained as denorm cache columns

-- document_aspects (same pattern)
ALTER TABLE document_aspects ADD COLUMN doc_id TEXT NOT NULL DEFAULT '';
ALTER TABLE document_aspects DROP CONSTRAINT pk_document_aspects;
ALTER TABLE document_aspects ADD PRIMARY KEY (doc_id);
```

Pre-migration precondition: drain queue to zero pending
**and zero in-progress** entries (per re-gate S1). The queue
is a three-state machine (`pending` / `in_progress` / `failed`);
workers transition rows to `in_progress` via `claim_next`
(`aspect_extraction_queue.py:317`) and complete with
`mark_done`. Checking only `status = 'pending'` misses
in-flight rows, which can be lost during the
CREATE-TABLE-new + INSERT + DROP-old + RENAME PK swap.

```sql
SELECT count(*) FROM aspect_extraction_queue WHERE status != 'failed';
-- Must return 0 before the PK swap runs.
-- (status='failed' rows are inert and can survive the swap.)
```

Operational sequence: stop the AspectWorker thread (or signal
it to stop claiming), wait for in-progress rows to drain,
verify the precondition, run the PK swap, restart the worker.

`mark_done` updates: `mark_done(doc_id)` instead of
`mark_done(collection, source_path)`.

Backfill: for every existing row, look up the matching catalog
document via `(collection, file_path)`. Per RF-4:

- 11 distinct legacy-collection orphan collections (~453 rows).
- 1 has automated `collections.superseded_by` mapping.
- Top-4 high-row-count orphans (445 rows / 98%) need
  hand-curated supersede mapping pre-step (~30 minutes
  operator time).
- 7 collections with 1-2 rows each: hard-delete (orphan beyond
  reasonable mapping).
- 118 test-fixture orphans (`knowledge__cli-*`,
  `nexus-integration-test`, `reproducer`, `pagtest`, `pagend`):
  hard-delete (came from CLI tests that should never have
  persisted).

### D4: RDR-107 superseded

This RDR fully replaces RDR-107. The soft-delete approach in
RDR-107 was a half-step that mitigated symptoms (stale-chunk
accumulation) without addressing the structural root cause
(position-derived natural IDs). Status flip to `superseded`
landed in the same PR as this RDR.

The soft-delete pattern remains valid for catalog tombstones
(RDR-106) where the use case is operator-driven undelete, not
content-edit handling. RDR-106 stays unchanged.

### D5: `chash_index` simplified to membership table

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

`chunk_chroma_id` column dropped. With D1 universally applied,
`chunk_chroma_id == chash[:32]` for every row — pure function
of `chash`, no need to store separately. The compound PK
`(chash, physical_collection)` retains its existing routing
role per RDR-101 nexus-tcwm.

Layer 3 critique C1 dispositions for the 27 `chunk_chroma_id`
sites enumerated by grep:

| Site | Disposition under D1 + D5 |
|---|---|
| `code_indexer.py:380, 424` | replace formula → `chunk_text_hash[:32]` |
| `prose_indexer.py:101, 121, 167, 200` | same |
| `mcp/core.py:973, 978, 984` | replace `sha256(...).hexdigest()[:16]` with `chunk_text_hash[:32]`. **Note (re-gate O1)**: current code uses 16-char ID for store_put; D1 standardizes on 32-char. Phase 2's idempotent loop handles the mixed-length case via `cid == new_id` skip, but old 16-char IDs accumulate until GC. Phase 4 GC rewrite catches them via the manifest cross-check. |
| `commands/store.py:113, 115` | same — replace 16-char or position-derived ID with `chunk_text_hash[:32]` |
| `catalog/catalog_spans.py:327` | replace `row["chunk_chroma_id"]` → `hex_chash[:32]` |
| `collection_audit.py:425-431` | replace the `chunk_chroma_ids_present_in_collection(col, ids)` call with a direct chromadb cross-check: `live_ids = set(col.get(ids=...))`, then `intersect = chashes_from_index & live_ids`. The chash_index returns chash values via `lookup`; T3 natural IDs equal chash[:32] post-D1, so the comparison is direct |
| `db/t2/chash_index.py:62, 64, 105-135, 145-158, 304-338` | drop the column from schema + `upsert` signature + `lookup` SELECT + `record_chunks` helper |
| `db/t2/chash_index.py:261-283` (`chunk_chroma_ids_present_in_collection` method, per re-gate S3) | **remove the method entirely** along with its sole caller at `collection_audit.py:426`. The method's purpose (cross-check chash_index entries against live T3 IDs) was specifically tied to the old chunk_chroma_id column; under D1 the audit equivalent is `set(chash_index.lookup(chash) for chash in expected) ∩ set(col.get(ids=...))`, a one-liner that doesn't need a dedicated accessor |
| `db/migrations.py:912-961, 1939` | historical migrations — leave untouched (they describe the column's lifecycle); add a new migration that drops the column after Phase 4 verification |

The `chunk_chroma_id` column AND the `chunk_index` /
`chunk_count` / `doc_id` chunk metadata fields all disappear
together, dropping ~270 lines of supporting code by rough
count.

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
Content-derived IDs (D1) avoid this by anchoring on text, not
position.

### Alternative B': `sha256(doc_id:chash:occurrence_within_doc)[:32]` (gate-revision draft)

A post-gate proposal during the C2 critique discussion. Adds an
occurrence ordinal to disambiguate identical chunks within a
doc: same text in same doc gets distinct IDs by 0-based
occurrence count. Position-precise reconstruction works via
chunk metadata; current chunk_count + chunk_index semantics
intact; small delta from raw `chunk_text_hash[:32]`.

**Why rejected**: this is the half-step before the manifest
model. It carries the same per-chunk doc-identity metadata
denorm that D1+D2 eliminate. Cross-doc identical chunks remain
duplicated (different doc_ids → different IDs) instead of
being deduplicated at the storage layer. The naming-clash
problem (ChromaDB doc vs catalog Document) stays unresolved.
Choosing this would require migrating again later when the
manifest model is adopted. Skip it; go directly to the
manifest model.

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

| Dimension | RDR-108 (D1-D5, manifest model) | RDR-108 (B variant: chash + occurrence ordinal) | Soft-delete (RDR-107) | Do nothing |
|---|---|---|---|---|
| Chunk drift | structurally impossible | structurally impossible | mitigated, retention-window-bounded | persists |
| Aspect drift (existing) | fixed | fixed | persists | persists |
| Aspect drift (future) | fixed | fixed | persists | persists |
| chash_index drift | reduced (FK-enforced) | reduced | persists | persists |
| Within-doc identical chunks | content-addressed (manifest preserves position) | disambiguated by occurrence ordinal; one record per chunk regardless | n/a | persists |
| Cross-doc identical chunks | deduplicated at storage | duplicated (per-doc IDs differ) | duplicated | duplicated |
| RDR-053 design intent | fully realized + extended | fully realized | partial | unaddressed |
| Naming clash with ChromaDB | resolved (catalog Document, T3 chunk; clean split) | unresolved (chunks still carry doc_id metadata) | unresolved | unresolved |
| T3 migration cost | re-upsert 290k chunks | re-upsert 290k chunks | none (incremental) | none |
| Catalog migration cost | docs.chunks manifest + aspects PK swap + queue PK swap | aspects PK swap + queue PK swap | none | none |
| Retrieval rewrites | doc-grouping reads catalog manifest first (~5-10 call sites) | unchanged (chunk metadata still has doc_id) | unchanged | unchanged |
| Reversibility | reversible (re-upsert under old IDs from a backup snapshot) | reversible | trivially reversible | N/A |
| Doctor-check noise | minimal | minimal | persists at low rate | grows |
| Architectural alignment with git/IPFS | full | partial | none | none |

The manifest model trades a one-time migration cost (~1.3× the
catalog work of the B variant; similar T3 work; one new
retrieval pattern) for permanent elimination of four bug
classes (the three from B + the within-doc collision RDR-101
warned about) and a clean responsibility split between the
graph layer (catalog) and the blob store (T3).

## Implementation Plan

### Phase 0: Approve decisions D1-D5 (gate)

This RDR's finalization gate. D1-D5 are locked per Hal direction
(2026-05-08, post-gate revision); the gate verifies (a) the
research questions in §"Research Findings" are answered and
(b) the migration cost estimate is plausible.

### Phase 1: Catalog schema migrations (cheap, no T3 touch)

**Step 1a: Manifest table (D2)**

- Create `document_chunks` table per the D2 schema (PK
  `(doc_id, position)`, idx on `chash`).
- Add FK from `documents.physical_collection` to
  `collections(name)`. Backfill `collections` rows for any
  physical_collection value that doesn't have one yet.

**Step 1b: Manifest backfill (D2)**

For each catalog Document, paginate T3 chunks matching its
current `doc_id` metadata, sort by `chunk_index`, write
`document_chunks` rows preserving order and capturing
positional metadata (line_start, line_end, char_start, char_end).

```python
# Pseudocode
for doc in catalog.iter_documents():
    chunks = []
    offset = 0
    while True:
        page = col.get(
            where={"doc_id": doc.tumbler}, limit=300, offset=offset,
            include=["metadatas"],
        )
        if not page["ids"]: break
        for cid, meta in zip(page["ids"], page["metadatas"]):
            chunks.append({
                "chash": meta["chunk_text_hash"],
                "position": meta["chunk_index"],
                "line_start": meta.get("line_start"),
                "line_end": meta.get("line_end"),
                "char_start": meta.get("chunk_start_char"),
                "char_end": meta.get("chunk_end_char"),
            })
        if len(page["ids"]) < 300: break
        offset += 300
    chunks.sort(key=lambda c: c["position"])
    catalog.write_manifest(doc.tumbler, chunks)
```

Idempotent — re-running overwrites the manifest with the same
content. Per re-gate O2: a catalog Document with zero matching
T3 chunks (catalog knows the doc, T3 has no chunks) produces
an empty manifest row-set — valid, not an error. The reverse
direction (T3 chunks with no matching catalog doc) is the
existing orphan case handled by the GC path described in
Phase 4.

**Step 1c: Aspect tables PK migration (D3)**

- Pre-step (per RF-4): hand-curate `collections.superseded_by`
  mappings for the 4 high-row-count legacy orphan collections
  (`rdr__nexus-571b8edd` 224, `rdr__ART-8c2e74c0` 94,
  `knowledge__art-papers` 78, `rdr__1-1__voyage-context-3__v1`
  49 = 445 rows / 98% of legacy-orphan corpus). Operator
  inspects each and decides the current target.
- **Pre-step (per Layer 3 critique C3 + re-gate S1)**: drain
  `aspect_extraction_queue` to zero pending AND zero
  in-progress entries (full spec in §D3 above). Stop the
  AspectWorker, wait for in-flight rows, verify SQL
  `SELECT count(*) FROM aspect_extraction_queue WHERE status
  != 'failed'` returns 0, then run the PK swap, restart the
  worker.
- For each of `aspect_extraction_queue` and `document_aspects`:
  1. Add `doc_id TEXT NOT NULL DEFAULT ''` column.
  2. Backfill via `(collection, file_path)` JOIN to
     `documents`. Follow `collections.superseded_by` chain for
     legacy collection names. Hard-delete the 118 test-fixture
     orphans + the 7 low-row-count unmapped legacy collections.
  3. Swap PK from `(collection, source_path)` to `(doc_id)`.
  4. Keep `collection` and `source_path` as denorm cache
     columns.
- Update `mark_done(collection, source_path)` →
  `mark_done(doc_id)`. Cascade callers.

**Step 1d: Cascade wiring**

- `Catalog.rename_collection`: UPDATE the denorm `collection`
  cache column on `aspect_extraction_queue` and
  `document_aspects` (PK is `doc_id`, unaffected; cache stays
  in sync).
- Note: with PKs no longer keyed on collection name, a rename
  is now safe — no row identity changes.

### Phase 2: T3 chunk re-upsert with content-derived natural IDs (D1)

**Per RF-2: migration is cheap, no Voyage re-embed.**

Per collection, paginated, idempotent (per RF-6):

```python
seen_old_ids = set()
offset = 0
while True:
    page = col.get(
        limit=300, offset=offset,
        include=["documents", "embeddings", "metadatas"],
    )
    if not page["ids"]: break

    to_migrate = []
    for cid, doc, emb, meta in zip(page["ids"], page["documents"],
                                    page["embeddings"], page["metadatas"]):
        new_id = meta["chunk_text_hash"][:32]
        if cid == new_id:
            continue  # already migrated, skip silently
        # Strip doc-level identity fields from metadata (D2 manifest is authoritative now)
        new_meta = {k: v for k, v in meta.items()
                    if k not in {"doc_id", "chunk_index", "chunk_count"}}
        to_migrate.append((cid, new_id, doc, emb, new_meta))
        seen_old_ids.add(cid)

    if to_migrate:
        col.upsert(
            ids=[t[1] for t in to_migrate],
            documents=[t[2] for t in to_migrate],
            embeddings=[t[3] for t in to_migrate],
            metadatas=[t[4] for t in to_migrate],
        )
    if len(page["ids"]) < 300: break
    offset += 300

# Delete old IDs in batches of 300
for batch in batched(seen_old_ids, 300):
    col.delete(ids=list(batch))
```

Critical detail: the `to_migrate` step ALSO strips `doc_id`,
`chunk_index`, `chunk_count` from chunk metadata (those fields
now live in the catalog manifest). Re-upserting under the new
content-derived ID effectively replaces the chunk's identity
AND its metadata in one op.

**Carve-outs per RF-1**:
- `taxonomy__centroids`: skip; uses centroid-hash identity
  from the `topics` table, not chunk_text_hash. The migration
  command MUST guard for missing `chunk_text_hash` per the
  Layer 3 critique S3 finding (raise a structured error if
  missing, do not KeyError).
- `docs__scheme-evolution-research-b7de0b63`: re-index 690
  pre-RDR-053 chunks from source as a pre-step (only ~few
  minutes of Voyage embedding for that subset; the rest of
  the collection migrates normally).

**Identical-text collapse**: when two chunks in the same
collection have the same `chunk_text_hash`, the second
`upsert` on the new ID is a no-op (chromadb upsert is
idempotent). The catalog manifest from Phase 1 records both
positions; both refer to the same T3 record.

### Phase 3: T3 chunk metadata schema cleanup (D2)

After Phase 2 completes for a collection, its chunks no longer
carry `doc_id`, `chunk_index`, `chunk_count` in metadata
(stripped at re-upsert). The system-wide cleanup:

- Update `metadata_schema.ALLOWED_TOP_LEVEL` to drop these
  three fields. New chunk writes via `make_chunk_metadata`
  stop including them.
- Update chunk-write paths: `code_indexer.py`, `prose_indexer.py`,
  `pipeline_stages.py`, `mcp/core.py:store_put`,
  `commands/store.py`, `db/t3.py:upsert_chunks_with_embeddings`
  no longer pass these fields.
- Catalog post-store hooks (`fire_post_store_hooks`,
  `fire_post_document_hooks`) write the manifest entry instead
  of relying on chunk metadata.

### Phase 4: chash_index simplification (D5) + retrieval call site rewrites

**chash_index column drop** (per RF-7 + Layer 3 C1):

- Drop `chunk_chroma_id` column from `chash_index` schema.
- Update accessor methods enumerated in D5's site-disposition
  table.
- Drop the migration that created the column (`db/migrations.py:912-961`)
  — historical, leave; add a new migration that drops the
  column.

**Retrieval call site rewrites** (the new pattern):

| Site | Pre | Post |
|---|---|---|
| `db/t3.py:1228` | `where={"doc_id": doc_id}` | `chashes = catalog.get_chunk_chashes(doc_id); col.get(ids=[c[:32] for c in chashes])` |
| `mcp/core.py:847-855` | group results by `chunk.metadata["doc_id"]` | gather `chunk_text_hash` values from results; query manifest by chash → group by doc |
| `catalog/synthesizer.py:616-672` | sort by `chunk_index` from metadata | walk manifest in order |
| `catalog/catalog_spans.py:327` | `doc_id=row["chunk_chroma_id"]` | `doc_id=hex_chash[:32]` |

For the doc-grouping case (`mcp/core.py:847`), the catalog
gains a helper:

```python
catalog.docs_for_chashes(chashes: list[str]) -> dict[str, list[doc_id]]
# SELECT doc_id FROM document_chunks WHERE chash IN (?, ?, ...)
```

This is a fast index lookup (idx_document_chunks_chash). Adds
one catalog query per retrieval batch, far smaller than the
embedding similarity computation cost.

**GC rewrite** (per re-gate S2 — critical implementation
gap surfaced at re-gate):

`indexer.py:1606-1620` (`_prune_deleted_files`) currently
identifies orphan chunks via `meta.get("doc_id", "")` on each
T3 chunk's metadata. After Phase 2 strips that field, the
existing pruner becomes a no-op for migrated collections —
silently re-creating the stale-chunk root bug RDR-108 set out
to fix. RDR-101 RF-3 stated "GC keys on `chunk_id`, not
`chash`, because chash is non-unique across documents"; under
the manifest model, the answer is "GC keys on absence-from-
manifest in the catalog," resolving RDR-101's concern at the
graph layer rather than the chunk-metadata layer.

New GC contract:

```python
# nx t3 gc, per collection
def prune_orphan_chunks(col, catalog):
    """Delete T3 chunks whose chash[:32] does not appear in any
    document_chunks manifest entry for this collection's docs.
    """
    # 1. Gather all chash[:32] values referenced by manifest for
    #    docs whose physical_collection == col.name.
    referenced_chashes = catalog.chashes_for_collection(col.name)
    # 2. Paginate T3 chunks; identify orphans (chunk natural ID
    #    not in referenced_chashes after Phase 2's content-derived
    #    ID change). Note chunk natural ID == chunk_text_hash[:32]
    #    so identity comparison is direct.
    orphan_ids = []
    offset = 0
    while True:
        page = col.get(limit=300, offset=offset, include=[])
        if not page["ids"]: break
        for cid in page["ids"]:
            if cid not in referenced_chashes:
                orphan_ids.append(cid)
        if len(page["ids"]) < 300: break
        offset += 300
    # 3. Batch-delete orphans (300 per delete, per quota).
    for batch in batched(orphan_ids, 300):
        col.delete(ids=list(batch))
```

Catalog gains a helper:

```python
catalog.chashes_for_collection(physical_collection: str) -> set[str]
# SELECT DISTINCT substr(chash, 1, 32) FROM document_chunks dc
#   JOIN documents d ON d.tumbler = dc.doc_id
#   WHERE d.physical_collection = ?
```

This replaces (does not augment) the metadata-based path in
`_prune_deleted_files`. Cost: one catalog query per
collection at GC time, plus the existing T3 pagination. Net
~equivalent to the pre-migration cost; the work moves from
T3 metadata-filtering to catalog index-lookup, which is
faster.

The rewrite is required as part of Phase 4. `_prune_deleted_files`
loses the `meta.get("doc_id", ...)` branch entirely; the
fallback `source_path` branch was already deprecated by
RDR-101 Phase 5b.

### Phase 5: Documentation + verification

- Update `CLAUDE.md`: §"Critical conventions" gains an entry on
  the catalog/T3 split: "Catalog Documents are graph nodes;
  T3 chunks are content-addressed blobs. Doc structure lives
  in catalog manifest, not chunk metadata."
- Update `docs/architecture.md` with the manifest model.
- Update `docs/rdr/rdr-053-xanadu-fidelity.md` Deviations
  Register: D5 "Position-Based Chunk Spans" → mark resolved
  by RDR-108.
- Re-run the 2026-05-08 prod-shakeout probes:
  - chash_index distinct collections == T3 collection count
  - document_aspects orphan rate == 0%
  - code__1-2188 `(source_path, chunk_index)` dupe-key count == 0
  - Per-collection check: any chunks with `doc_id` /
    `chunk_index` / `chunk_count` in metadata = 0 (cleanup
    successful)
- Run a code-repo re-index and verify:
  - Zero stale chunks accumulate
  - Manifest correctly captures all chunks in order
  - Identical chunks (e.g. test fixture re-import) collapse to
    one T3 record

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
- **Carve-out — `taxonomy__centroids`**: assert
  `nx t3 reidentify` skips this collection by default and
  emits a structured log entry naming it as exempt. Per Layer 3
  S3: also assert that if a chunk in any collection lacks
  `chunk_text_hash` metadata, the migration raises a
  structured error rather than KeyError.
- **Manifest preserves order under content addressing**
  (D2): index a doc with chunks `[A, B, A, C]` (chunk B is
  unique; chunk A and C are unique; A appears at positions 0
  and 2). Assert (a) T3 has exactly 3 records (A, B, C) keyed
  by their content hashes, (b) `document_chunks` manifest has
  4 rows in `(doc_id, position)` order: `(d, 0, A), (d, 1, B),
  (d, 2, A), (d, 3, C)`, (c) doc reconstruction returns the
  4-element ordered chunk list with A's content appearing at
  positions 0 and 2.
- **Cross-doc chash dedup** (D1 + D2): index the same chunk
  text in two distinct docs. Assert (a) T3 has ONE record
  (the chash[:32] is shared), (b) `document_chunks` manifest
  has TWO rows (one per doc), (c) `catalog.docs_for_chashes`
  returns both doc_ids for the chash.
- **Manifest backfill from existing chunk metadata** (Phase 1
  Step 1b): fixture T3 with chunks carrying current
  `doc_id`/`chunk_index` metadata; run Phase 1 Step 1b; assert
  the resulting `document_chunks` rows preserve order and
  match the original metadata.
- **Aspect queue drain precondition** (Phase 1 Step 1c, per
  re-gate S1): with `aspect_extraction_queue` rows in EACH of
  pending and in_progress state (separate fixtures), run the
  PK migration; assert it BLOCKS for both with a clear error
  naming the offending state, and does not modify schema. Then
  drain to zero pending+in_progress; assert migration runs.
- **Aspect PK migration end-to-end** (D3): fixture with five
  rows: (a) matching catalog, (b) legacy-collection-name with
  hand-curated supersede mapping, (c) legacy-collection-name
  with NO supersede (drop class), (d) test-fixture collection
  (`knowledge__cli-...`), (e) collection unknown to catalog.
  Assert (a) and (b) migrate to the correct doc_id, (c) and
  (d) are hard-deleted, (e) is surfaced for manual review.
- **Catalog rename cascades to denorm caches**: rename a
  collection via `Catalog.rename_collection`; assert the
  denorm `collection` column on `document_aspects` and
  `aspect_extraction_queue` updates atomically. PKs (`doc_id`)
  unaffected.
- **chash_index FK enforcement** (D5): attempt INSERT into
  chash_index pointing at a non-existent
  `physical_collection`; assert FK violation. Verify cascade
  behavior on collection deletion.
- **chunk_chroma_id column drop is safe** (D5 + Layer 3 C1):
  before dropping the column, run a structured migration that
  asserts every row has `chunk_chroma_id == chash[:32]`. If
  any row violates, log + fail loud (allows operator to
  diagnose stale rows).
- **Retrieval rewrite — doc-grouping by manifest** (Phase 4):
  search returns chunks with no `doc_id` metadata. Assert
  `mcp/core.py:847` doc-grouping pulls from
  `catalog.docs_for_chashes(chashes)` and produces the same
  grouping as the pre-migration metadata path.
- **Cross-collection chash routing** (existing semantics):
  insert the same `chunk_text_hash` into two collections;
  assert `chash_index.lookup(chash)` returns both rows (the
  per-collection scope from RF-3 is preserved).
- **Quota compliance**: migration batches respect
  `chroma_quotas.MAX_RECORDS_PER_WRITE = 300` for both
  `col.get` (page size) and `col.upsert` / `col.delete`
  (batch size).
- **Carve-out — pre-RDR-053 partial-coverage collection**:
  assert that running Phase 2 against
  `docs__scheme-evolution-research-b7de0b63` requires the
  690-chunk re-index pre-step or fails loud.
- **GC rewrite — orphan detection by manifest absence** (per
  re-gate S2): fixture with N migrated chunks where some have
  manifest entries and some don't (simulating a deleted-file
  scenario). Run `nx t3 gc`. Assert (a) chunks without
  manifest entries are deleted, (b) chunks with manifest
  entries survive, (c) the new code path does NOT consult
  chunk metadata for `doc_id`. Includes the negative case:
  metadata still has `doc_id` (mid-migration mixed state) AND
  manifest is absent → still treated as orphan (manifest is
  authoritative).

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

- **Versioning**: catalog SQLite schema additions (new
  `document_chunks` manifest table, `doc_id` columns +
  reshuffled PKs on `document_aspects` and
  `aspect_extraction_queue`, FK on
  `documents.physical_collection`, `chash_index` drops
  `chunk_chroma_id` column). T3 chunk metadata schema
  simplifies (drops `doc_id`, `chunk_index`, `chunk_count`).
  T3 natural-ID migration is data-level, not schema-level
  (ChromaDB doesn't enforce a metadata schema).
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: ships in conexus wheel. Phase 1
  catalog migrations run automatically at first DB open after
  upgrade (current nexus pattern: `db/migrations.py`
  numbered migrations). Phase 2 (T3 re-upsert) is operator-
  driven via `nx t3 reidentify --collection ...` to bound the
  time and cost.
- **Incremental adoption**: Phase 1 is mandatory at upgrade
  (catalog migrations land first; manifest backfill for
  existing chunks runs as part of it; aspect tables PK migration
  blocks until queue is drained). Phase 2 is per-collection
  on operator demand; until run, that specific collection
  retains old position-derived IDs and per-chunk doc_id
  metadata (system continues to function in mixed-state).
  Phase 3 (chunk metadata cleanup) and Phase 4 (chash_index
  column drop) run after Phase 2 completes globally — both
  require a corpus-wide assertion before the cleanup migrations.
- **Mixed-state retrieval (per re-gate O3)**: between Phase 1
  manifest backfill and Phase 2 completion for a given
  collection, retrieval may return chunks that DO have
  `doc_id`/`chunk_index` in metadata (Phase 2 hasn't stripped
  them) but ALSO have manifest entries (Phase 1 wrote them).
  Doc-grouping reads via `catalog.docs_for_chashes` work
  uniformly. The transitional gap is for chunks indexed AFTER
  Phase 1 manifest backfill but BEFORE Phase 3's write-path
  changes — those chunks have OLD natural IDs and no manifest
  entry. The retrieval-path fix: when grouping chunks by doc,
  fall back to `chunk.metadata["doc_id"]` if the manifest
  lookup misses. Document this fallback for the migration
  window and remove it after Phase 3 + a corpus-wide
  reconciliation pass.
- **Memory management**: re-upserting 290k chunks fits within
  ChromaDB Cloud quotas at 300/op batches. Catalog manifest
  table adds ~290k rows × ~80 bytes = ~23 MiB to the
  catalog SQLite — negligible relative to the existing 64 MiB.
- **Secret/credential lifecycle**: N/A.

### Proportionality

Right-sized given the architectural payoff. The migration is a
one-time cost (catalog: 1-2 minutes for schema + manifest
backfill; T3: hours per large collection, parallelizable;
chash_index + retrieval rewrites: a focused PR each) that
structurally eliminates four bug classes (the three from the
prod-shakeout + the within-doc collision RDR-101 warned
about) AND establishes a clean responsibility split between
the catalog (graph + tree layer) and T3 (content-addressed
blob store). The git/IPFS architectural alignment is a
substantive long-term win: future work (e.g., distributing
the corpus across nodes, adding a delta-encoding layer for
similar chunks, exposing chunks as linkable resources to
external systems) becomes naturally tractable under this
model.

Half-fixes (RDR-107 soft-delete, B' occurrence-ordinal,
cascade-only) trade migration cost for permanent operator
burden AND lock in the naming-clash architecture — the wrong
direction for nexus's robot-mode disposition.

## References

### Beads addressed

- nexus-jc63 (P0): chunk soft-delete steady-state — superseded
  by D1 + D2 in this RDR (content-addressed natural ID +
  manifest layer eliminate the stale-chunk problem
  structurally).
- nexus-b5mh (P1): one-shot stale-chunk reconciliation —
  superseded by Phase 2 in this RDR (re-upsert under
  content-derived IDs is the migration AND the cleanup).
- nexus-je0b (P1): document_aspects 76% orphan rate — fixed by
  D3 PK migration to (doc_id) in this RDR.
- nexus-mmf5 (P1): chash_index namespace drift — fixed by D5 +
  FK enforcement on documents.physical_collection in this RDR.
- nexus-17wf (P2): low-confidence aspect rows — orthogonal,
  not addressed here.

### Related RDRs

- RDR-053 (Xanadu Fidelity, accepted/closed): chose
  `chunk_text_hash` as immutable span identity. RDR-108
  completes the design by making it the routing identity AND
  introducing the manifest layer (catalog Document.chunks)
  that resolves the within-doc collision concern RDR-053
  itself anticipated.
- RDR-101 (Catalog T3 Metadata Design, closed): established
  UUID7 doc_id and the event-sourced catalog. RDR-108 imports
  these and resolves RDR-101's stated rationale for NOT using
  chash as natural ID — the rationale held under per-chunk
  metadata; under the manifest model, identical content is
  one record by design and the manifest preserves position.
- RDR-103 (Catalog as Collection-Name Authority, closed):
  established the `collections` table. RDR-108 makes
  `documents.physical_collection` a foreign key to it and
  uses RDR-103's supersede chain for the Phase 1 pre-step.
- RDR-106 (Soft-Delete via Tombstone Columns on Catalog
  Projection, draft): catalog-projection-layer soft-delete.
  Independent of RDR-108; both can ship.
- RDR-107 (T3 Chunk Soft-Delete via Tombstone Metadata,
  superseded by this RDR): the partial-fix predecessor.

### Architectural references

- Git's object model (blobs + trees): same content-addressed
  blob store + tree-of-references pattern that RDR-108 adopts.
  T3 chunks are the blobs; catalog Document.chunks is the
  tree.
- IPFS DAG: same content-addressed model with a distributed
  twist. RDR-108 uses the conceptual pattern (blob + tree)
  but stays on local SQLite + ChromaDB Cloud rather than
  introducing distributed addressing.

### Architectural-revision provenance

The original D1 (raw `chunk_text_hash[:32]` as natural ID,
no manifest layer) was BLOCKED at the Layer 3 finalization
gate (substantive-critic, 2026-05-08) on three issues:

- **C1**: D4 blast radius understated (27 `chunk_chroma_id`
  sites, not 1; collection_audit.py read site has no defined
  replacement)
- **C2**: silent data loss for identical-text chunks within
  the same collection (RDR-101 explicitly rejected raw chash
  as natural ID for this reason)
- **C3**: D2 ignored the coupled `aspect_extraction_queue`
  table

User-Hal reframed the problem post-gate (the ChromaDB-doc vs
catalog-Document naming clash had been masking the right
architectural split). The current RDR-108 (manifest model)
resolves all three:

- C1 dispositions enumerated in D5.
- C2 structurally resolved by D2 (manifest layer; identical
  content collapses to one T3 record).
- C3 folded into D3.

The pre-revision draft (B variant: `sha256(doc_id:chash:occurrence)[:32]`)
is documented in §"Alternatives Considered". RDR-107 (the
soft-delete predecessor) is also retained as a closed
exploration that led here.

### Probes / analysis

- 2026-05-08 prod-shakeout umbrella memory.
- Subagent denormalization analysis: T3
  `analysis-normalization-nexus-inode-identity-2026-05-08`,
  T1 scratch `0823e897-3308-4aa7-b85b-8f0cecb6f10f`.
