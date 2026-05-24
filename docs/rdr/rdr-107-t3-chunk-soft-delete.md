---
title: "T3 Chunk Soft-Delete via Tombstone Metadata"
id: RDR-107
type: Architecture
status: superseded
superseded-by: RDR-108
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-08
related_issues: [nexus-jc63, nexus-b5mh]
related_rdrs: [RDR-053, RDR-106, RDR-108]
---

# RDR-107: T3 Chunk Soft-Delete via Tombstone Metadata

> **SUPERSEDED 2026-05-08 by RDR-108** before merge. The
> soft-delete approach proposed here is a partial fix: it
> mitigates stale-chunk accumulation by tombstoning old chunks
> on re-index, but leaves the structural root cause unaddressed
> (chunk Chroma natural IDs are position-derived, so identity
> shifts whenever line numbers shift). RDR-108 adopts the
> normalized model RDR-053 already chose at design time:
> `chunk_text_hash` becomes the Chroma natural ID, making
> `upsert` truly idempotent and stale rows impossible by
> construction. The architectural review during the 2026-05-08
> prod-shakeout determined that the half-step in this RDR was
> the wrong layer to fix the bug.
>
> This document is retained as the design exploration that led
> to RDR-108. The "Alternatives Considered" section in RDR-108
> reuses this content directly.

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

T3 (the ChromaDB tier holding chunk embeddings) accumulates
stale chunks every time a source file is edited and re-indexed.
The 2026-05-08 prod-shakeout probe quantified one collection:

- `code__1-2188__voyage-code-3__v1` (Nexus repo, 28,512 chunks)
- 1,630 distinct `(source_path, chunk_index)` keys map to >1
  chunk ID (25% of locations have stale duplicates)
- 23,714 chunks (83%) are involved in some duplicate group
- 346 dupe-groups have *identical* `chunk_text_hash`
  (semantically equivalent stale leftovers)
- 1,284 dupe-groups have *different* `chunk_text_hash`
  (prior-version chunk content the indexer never cleaned up)

The root cause is asymmetric prune logic across the two
indexing pipelines:

- `src/nexus/code_indexer.py:298-496` (`index_code_file`):
  builds a stable, position-keyed `chunk_chroma_id =
  sha256(f"{corpus}:{title}:chunk{i}")[:32]` where `title =
  "{rel_path}:{line_start}-{line_end}"`. Whenever a file edit
  shifts line numbers, the title changes, the chunk ID
  changes, and the upsert writes new IDs without deleting the
  old ones. **No prune step exists in this code path.**
- `src/nexus/doc_indexer.py:697-717` (markdown / PDF path):
  after upsert, queries via `_identity_where(file_path,
  corpus)` (resolves to a `doc_id` filter when the catalog
  knows the file) and hard-deletes any chunks whose IDs are
  not in the new ID set.

The prune in the doc/PDF path mitigates storage drift but is
itself a Xanadu-fidelity hazard: any external link referring
to a soft-deleted chunk's natural ID 404s after the next
re-index of that file. RDR-053 (Xanadu Fidelity, draft)
establishes the immutable-link discipline that this hard-prune
silently violates.

The 4.29.1 release introduced a backup-before-delete machinery
for the catalog projection, and RDR-106 (Draft) drafts a
tombstone-column soft-delete pattern as the structural fix at
that layer. T3 chunks need a parallel — but mechanically
distinct — treatment.

### Enumerated gaps to close

#### Gap 1: Code-path leaks chunks unboundedly

`code_indexer.index_code_file` has no prune-after-upsert step.
Every code-file re-index after an edit leaks one stale chunk
set. Across 15 code collections (~165k chunks today),
projected leakage is ~25% per collection.

#### Gap 2: Doc/PDF prune breaks Xanadu links

`doc_indexer.py:714-717` does `col.delete(ids=stale_ids)`.
Anyone who pinned a `chash:<hex>` reference to a chunk's
natural ID before the re-index loses it. The catalog link
resolver (`src/nexus/catalog/catalog_links.py`) has no
fallback to a "deleted but recoverable" state.

#### Gap 3: Retrieval can return same-content twice

Even where stale chunks are leaked silently (Gap 1), they
remain present in the HNSW index and so participate in ANN
search. Top-K may legitimately contain v1 *and* v2 of the
same chunk content with slightly different scores.

#### Gap 4: Storage cost grows linearly with edit volume

Stale chunks pay storage cost (document text + metadata) and
embedding cost (1024-dim float32 = 4 KiB per chunk). At ~25%
leakage on 290k chunks, ~72k stale chunks ≈ **~290 MiB** of
embeddings alone, plus a comparable amount of metadata + text.

#### Gap 5: HNSW index space grows with stale chunks

Even after a (hypothetical) metadata-flag soft-delete, the
embedding row still occupies HNSW slots and gets compared
during ANN search. A vacuum/compaction policy is needed
beyond the soft-delete flag.

## Context

### Background

RDR-053 (Xanadu Fidelity) frames the Nexus-wide commitment to
immutable, addressable references. RDR-101 (Catalog T3
Metadata Design) defined the `doc_id` tumbler keying that
makes per-document identity work across re-index cycles.
RDR-106 drafts the soft-delete pattern at the catalog
projection layer.

The 2026-05-08 prod-shakeout traced an indexing-cycle
duplication mystery to the asymmetric prune logic above. User
direction (Hal, 2026-05-08): "if we 'reindex' the content
which has changed we have to clean up, right? this is where
Xanadu immutable links start to bite. :(" The architectural
choice — soft-delete over hard-prune — flowed from that read.

### Constraints

- **ChromaDB Cloud quotas** (`src/nexus/db/chroma_quotas.py`):
  reads/writes capped at 300 records per call; metadata-only
  updates via `col.update(ids=..., metadatas=[...])` count
  against `MAX_RECORDS_PER_WRITE`.
- **HNSW index immutability**: ChromaDB does not expose an
  in-place "tombstone" on the index itself. Marking a chunk
  via metadata leaves the embedding live in HNSW; physical
  deletion via `col.delete(ids=...)` is the only way to
  reclaim ANN slots.
- **Backward compatibility**: pre-fix chunks have no
  `is_current` field. Retrieval default-filter must treat
  field-absent as `is_current=True` to avoid silently dropping
  the entire pre-fix corpus from results.
- **Catalog link resolution** (`catalog_links.py`): chunk-ID
  spans (`chash:<hex>`) must continue to resolve to the
  superseded chunk's content; "is_current=False" is a *display
  filter*, not a *content delete*.

## Research Findings

(to be filled during /conexus:rdr-research)

Open questions to research:

1. **ChromaDB metadata-only update cost.** Does
   `col.update(ids, metadatas)` re-embed? What's the rate
   limit and elapsed cost for a 41k-chunk backfill?
2. **HNSW filter pre/post**. ChromaDB applies `where`
   pre-filter at query time. Confirm the embedding rows for
   `is_current=False` are still ANN-searched (worst-case) or
   skipped at the index layer (best-case).
3. **Retention compaction policy.** What's the right TTL for
   superseded chunks? RDR-106 chose 90 days for catalog
   tombstones; same number for T3 or different?
4. **Cross-collection link coherence.** When a chunk in code
   collection A is linked from a chunk in docs collection B,
   and A's chunk gets superseded, does the link's
   `chash:<hex>` resolution path still work? Trace via
   `Catalog.resolve_chash`.

## Proposed Solution

### Schema additions to T3 chunk metadata

Three new metadata fields on every chunk:

| Field | Type | Default | Semantics |
|-------|------|---------|-----------|
| `is_current` | bool | `True` | False ⇒ chunk has been replaced by a newer version |
| `superseded_at` | ISO8601 string | `""` | When the supersede flag flipped; `""` while current |
| `superseded_by` | str | `""` | Optional pointer at the new chunk ID for redirect resolution |

Backward compatibility: pre-fix chunks have none of these.
Retrieval defaults to `where={"is_current": {"$ne": False}}`
which matches both *missing* and *True* values.

### Re-index hot path (steady-state)

After `ctx.db.upsert_chunks_with_embeddings(...)` in
`code_indexer.py:467` and the equivalent insert site in
`doc_indexer.py:665` (and PDF post-pass at
`pipeline_stages.py:737`):

1. Compute `prune_where = _identity_where(file_path, corpus)`
   (already present; resolves to `{"doc_id": tumbler}` when
   the catalog has the file).
2. Paginate `col.get(where=prune_where, include=["metadatas"],
   limit=300, offset=...)`.
3. For each returned chunk, if `id not in current_ids_set` AND
   `metadata.get("is_current", True)`, add to
   `to_supersede`.
4. Batch `col.update(ids=batch, metadatas=[{"is_current":
   False, "superseded_at": now_iso, "superseded_by": ""} ...])`
   in chunks of 300.

The `superseded_by` pointer is intentionally empty in the
steady-state path — populating it correctly requires a
position-stable mapping from old chunk IDs to new chunk IDs,
which only exists when `chunk_index` is preserved across the
edit. A follow-up RDR can refine this once the basic mechanism
ships.

### Default retrieval filter

All retrieval call sites add `where={"is_current": {"$ne":
False}}` by default:

- `src/nexus/db/t3.py` (search)
- `src/nexus/db/t3_query.py` (query)
- `src/nexus/mcp/core.py:nx_answer`
- `src/nexus/mcp/core.py:store_get`, `store_get_many`
- `src/nexus/catalog/synthesizer.py` (chunk synthesis)

Add an explicit `include_superseded: bool = False` parameter
on the public APIs for link-resolution paths.

### Link resolution path

`Catalog.resolve_chash` (in `catalog/catalog.py`) and the
`chash:<hex>` span resolver (in `catalog_links.py`) MUST fetch
chunks regardless of `is_current`. The
`include_superseded=True` flag exists for this case.

### Compaction (vacuum-superseded)

After a configurable retention window (default: 90 days; same
as RDR-106), a separate verb hard-deletes superseded chunks:

```
nx t3 vacuum-superseded [--collection NAME] [--older-than DAYS]
                        [--dry-run]
```

This is the only path that calls `col.delete(ids=...)` for
soft-deleted chunks. Mirrors RDR-106's `vacuum-backups`. The
retention window is the user-facing knob trading storage cost
against link-resolution availability.

### One-shot reconciliation (separate concern)

Pre-existing stale chunks have no `is_current` field. The
backfill bead `nexus-b5mh` covers a one-time sweep that
classifies every `(source_path, chunk_index)` group and marks
all-but-the-youngest as `is_current=False` with
`superseded_at=2026-05-08T00:00:00+00:00` and
`superseded_reason="backfill-2026-05-08"`. This RDR's scope
covers the design of that backfill but the implementation is
tracked under nexus-b5mh.

## Alternatives Considered

### Alternative A: Hard-prune (status quo for doc/PDF; nexus-j1ro proposed extending to code)

Mirror the doc-indexer prune pass into `code_indexer`. After
upsert, identify stale chunk IDs and `col.delete(ids=...)`.

**Why rejected**: solves Gap 1 (code leakage) and Gap 4
(storage growth) but breaks Gap 2 (Xanadu links 404 across
re-index). User direction (2026-05-08) explicitly cited the
Xanadu tension as the reason to abandon hard-prune. Bead
nexus-j1ro closed superseded.

### Alternative B: Stable, position-keyed chunk IDs (no supersede needed)

Change `chunk_chroma_id` from
`sha256("{corpus}:{title}:chunk{i}")[:32]` (where title
includes line numbers) to
`sha256("{doc_id}:{chunk_index}")[:32]`. Then `upsert` becomes
a true upsert: same (doc_id, chunk_index) maps to the same ID,
new content overwrites old.

**Why rejected**: solves Gap 1 elegantly but loses *all*
version history at the chunk layer. Xanadu links pinning a
specific chunk version (e.g., "the chunk as it was when this
RDR was written") become impossible — there is no second
version to link to, the new one overwrites the old. Soft-delete
preserves the version chain.

A weaker variant — stable IDs *plus* a versioning suffix
(`{doc_id}:{chunk_index}:v{version}`) — devolves to the
soft-delete proposal but with worse ergonomics: every re-index
needs a "next version number" lookup, racier than a simple
`is_current` flip.

### Alternative C: Append-only with no cleanup at all (Xanadu-pure)

Never mark chunks superseded; never delete. Retrieval filters
by `indexed_at` MAX per `(source_path, chunk_index)` group at
query time.

**Why rejected**: too expensive at retrieval time (group-by +
sort across hundreds of thousands of chunks per query).
Soft-delete pre-computes the `is_current` flag at write time,
so retrieval is a single O(1) WHERE clause.

### Alternative D: Extend RDR-106 to cover T3

Add a §"T3 chunk soft-delete" extension to RDR-106 instead of
filing this as RDR-107.

**Why rejected**: RDR-106 is close to its finalization gate;
loading T3 mechanics onto it now is scope creep. T3 chunks
have distinct concerns (HNSW index space, embedding cost,
vacuum policy) that warrant their own RDR. Cross-link via
`related_rdrs` instead.

## Trade-offs

| Dimension | Soft-delete (proposed) | Hard-prune (rejected A) | Stable IDs (rejected B) |
|-----------|------------------------|-------------------------|-------------------------|
| Storage cost | grows until vacuum | bounded | bounded |
| Embedding cost | grows until vacuum | bounded | bounded |
| HNSW index slots | grows until vacuum | bounded | bounded |
| Xanadu link integrity | retained until vacuum | broken at re-index | lost forever |
| Retrieval filter cost | one WHERE clause | none | none |
| Version history | retained until vacuum | none | none |
| Implementation complexity | medium (schema + filter + verb) | low | low (but loses links) |
| Reconciliation needed | yes (one-shot backfill) | yes (delete dupes) | no (idempotent rewrite) |

The soft-delete approach trades bounded storage for time-bounded
link integrity. The vacuum window is the user-facing knob.

## Implementation Plan

### Phase 1: Schema field + steady-state supersede (nexus-jc63)

- Add `is_current`, `superseded_at`, `superseded_by` to
  `metadata_schema.make_chunk_metadata`.
- Add supersede pass to `code_indexer.index_code_file` after
  the upsert at `code_indexer.py:467`.
- Replace hard-delete in `doc_indexer.py:714-717` and
  `pipeline_stages.py:_prune_stale_chunks` with the
  metadata-update soft-delete.
- Add default `where={"is_current": {"$ne": False}}` filter to
  retrieval call sites enumerated above.
- Add `include_superseded: bool = False` parameter to public
  retrieval APIs.
- Update `Catalog.resolve_chash` to set `include_superseded=True`
  on its T3 lookup.

### Phase 2: One-shot backfill (nexus-b5mh; blocks-on Phase 1)

- New verb: `nx t3 dedup-backfill [--collection NAME]
  [--dry-run] [--apply] [--report-path PATH]`.
- Default mode: dry-run with per-collection report (chunks
  total, dupe groups, chunks to supersede, estimated reclaim).
- `--apply` flips `is_current=False` on all-but-youngest
  per `(source_path, chunk_index)` group.
- `--undo --from-report PATH` reverses a misclassification.

### Phase 3: Vacuum-superseded verb

- New verb: `nx t3 vacuum-superseded [--collection NAME]
  [--older-than DAYS] [--dry-run]`.
- Hard-deletes chunks where `is_current=False AND superseded_at
  < now - DAYS`.
- Default `--older-than=90`.

### Phase 4: Documentation + telemetry

- Update `docs/architecture.md` with the soft-delete contract.
- Add the soft-delete fields to the Critical Conventions table
  in `CLAUDE.md` / `AGENTS.md`.
- Surface the dupe ratio in `nx doctor` (extends the
  `--chunk-size-distribution` family from nexus-6dan).

## Test Plan

(to be filled during /conexus:rdr-research)

Sketch:

- **Regression**: re-index a code file twice with a line-shift
  edit; assert pre-edit chunk's ID is still fetchable but
  `is_current=False`, post-edit chunk's ID is `is_current=True`,
  retrieval default-filters to the post-edit only.
- **Backward compat**: pre-fix chunks (no `is_current` key)
  remain visible at retrieval time.
- **Link resolution**: `chash:<hex>` resolver returns
  superseded chunk content (catalog link integrity preserved).
- **Backfill**: fixture with three indexed-at versions of the
  same `(source_path, chunk_index)`; assert backfill marks two
  as superseded with the correct timestamps.
- **Vacuum**: superseded chunks past the retention window are
  physically removed; superseded chunks within the window are
  retained.
- **Quota compliance**: backfill batches respect the
  `MAX_RECORDS_PER_WRITE = 300` cap from
  `chroma_quotas.QUOTAS`.

## Validation

### Testing Strategy

(to be filled during /conexus:rdr-research)

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

- **Versioning**: chunk metadata schema bump (T3 only — no T2
  migration). Retrieval default-filter is backward-compatible
  with field-absent chunks.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: ships in conexus wheel; no operator
  action required at upgrade (retrieval filter is opt-out via
  `include_superseded=True`).
- **Incremental adoption**: existing collections keep working;
  the one-shot backfill (Phase 2) is operator-driven and
  idempotent.
- **Memory management**: vacuum verb (Phase 3) is the
  operator-controlled knob for HNSW index size.
- **Secret/credential lifecycle**: N/A.

### Proportionality

Right-sized. Schema additions are minimal (3 metadata fields,
defaults preserve back-compat). Steady-state supersede is one
paginated query + one batched update per re-indexed file.
Retrieval filter is a single WHERE clause. Multi-week
implementation matches the value: structurally closes a
Xanadu-fidelity gap that has been silently leaking ~25% of
code-collection storage.

## References

- nexus-jc63 (P0): T3 chunk soft-delete steady-state — drives
  Phase 1.
- nexus-b5mh (P1, blocks-on jc63): one-shot reconciliation of
  existing stale T3 chunks — drives Phase 2.
- nexus-j1ro (CLOSED, superseded by this RDR): hard-prune
  proposal abandoned in favor of soft-delete.
- RDR-053 (Xanadu Fidelity, draft): the immutable-link
  discipline this RDR preserves.
- RDR-101 (Catalog T3 Metadata Design): the `doc_id` tumbler
  keying the supersede query depends on.
- RDR-106 (Soft-Delete via Tombstone Columns on Catalog
  Projection, draft): sibling RDR for the catalog projection
  layer; same pattern, different storage tier.
- 2026-05-08 prod-shakeout umbrella: the probe that surfaced
  the leakage at scale.
