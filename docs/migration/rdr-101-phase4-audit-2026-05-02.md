# RDR-101 Phase 4 Audit — 2026-05-02

User-driven audit prompted by "everything looks like an orphan" observation.
Goal: **ensure stuff that's wired actually works as designed; find what isn't wired**.

## Verified empirically

| Path | doc_id written? | source_path regressed? | Notes |
|---|---|---|---|
| `nx index repo` → code (via `code_indexer.py`) | ✅ Yes | ❌ Yes | _catalog_hook UPFRONT, doc_id_resolver injected; works as designed |
| `nx index repo` → prose (via `prose_indexer.py`) | ✅ Yes | ❌ Yes | Same upfront-registration model |
| `nx index repo` → PDF (via `indexer.py:_index_pdf_file`) | ✅ Yes | ❌ Yes | Same upfront-registration model (line 1473) |
| `nx index repo` → RDR (via `_discover_and_index_rdrs`→`batch_index_markdowns`) | ⚠️ Inherits prior | ❌ Yes | Goes through doc_indexer._index_document; survives only because ChromaDB upsert MERGES |
| `nx index pdf` standalone (via `doc_indexer.index_pdf`) | ❌ No | ❌ Yes | Confirmed empirically with permission-systems.pdf |
| `nx index md` standalone (via `doc_indexer.index_markdown`→`_index_document`) | ❌ No | ❌ Yes | Confirmed empirically with README.md |
| `nx index rdr` standalone (via `batch_index_markdowns`→`_index_document`) | ❌ No | ❌ Yes | Same as md (force-reindex confirmed; old doc_id preserved by upsert merge only) |
| MCP `store_put` | ⚠️ Caller-supplied | N/A | doc_id is passed through but the source-path-keyed metadata still flows |

## Root causes

### Bug 1: `doc_indexer._index_document` doesn't include doc_id at chunk-write time

`_pdf_chunks` and `_markdown_chunks` build chunk metadata via
`make_chunk_metadata(...)` *without* passing `doc_id`. The catalog
registration (`_catalog_pdf_hook` / `_catalog_markdown_hook`) fires
**after** the upsert. Chunks land in T3 with no doc_id metadata.

Existing chunks survive only because **ChromaDB upsert merges metadata**
(verified with EphemeralClient: `upsert(metadatas=[{"a": 99}])` on a
record `{"a": 1, "b": 2}` yields `{"a": 99, "b": 2}` — `b` preserved).
So a re-index keeps any pre-existing doc_id from synthesize-log or
prior backfill, but **never adds one**.

**Why `nx index repo` works but `nx index pdf` doesn't:** the repo
indexer (`indexer.py:run`) calls `_catalog_hook()` UPFRONT, builds a
`file_to_doc_id: dict[Path, str]`, and passes it as `doc_id_resolver`
to all per-format indexers. They include doc_id in
`make_chunk_metadata()` at line 422 (code) and line 121/203/267 (prose).
The doc_indexer paths have no equivalent pre-registration step.

### Bug 2: `make_chunk_metadata` still writes `source_path` post-prune

`ALLOWED_TOP_LEVEL` (metadata_schema.py:50–108) still includes
`source_path`. `_PRUNE_DEPRECATED_KEYS` (commands/catalog.py:5136)
strips it. The contradiction means **every reindex regresses
source_path on every chunk it touches**, requiring a re-prune.

The reader-audit doc (rdr-101-phase4-reader-audit.md:155) explicitly
flagged this:

> "After prune-deprecated-keys lands AND ALLOWED_TOP_LEVEL removes
> source_path, this write becomes a no-op via normalize(). Until then,
> dual-write is harmless."

ALLOWED_TOP_LEVEL was never updated. The dual-write is no longer
harmless because the prune verb has shipped and run.

### Bug 3: doctor `--t3-doc-id-coverage` PASS gate is tautological

`_run_t3_doc_id_coverage` (commands/catalog.py:4300+) builds an
"expected" set from non-orphan ChunkIndexed events only, then checks
that every chunk in `expected` has a matching doc_id. Orphan chunks
are not counted against coverage. With **309,681 of ~370K T3 chunks
(84%) classified as orphans by synthesize-log**, the gate passes
vacuously: "every non-orphan has doc_id" is trivially true when most
things are orphans.

The "Collections in log: 23" line further hides the gap — 23 is the
count of collections with non-orphan ChunkIndexed events. Events.jsonl
covers 783 distinct coll_ids; the 760 missing from the report contain
exclusively orphan markers and never appear.

### Bug 4: synthesize-log marks every unmatched chunk as orphan

`synthesized_orphan=true` was used as a "we don't know what Document
this chunk belongs to, so skip it for any doc_id-aware operation"
fallback. Result: collections indexed pre-RDR-101 (knowledge papers,
mirrored code repos, old docs) became uniformly orphan. They cannot be
fixed by reindex alone because:

1. `nx index pdf` doesn't write doc_id (Bug 1)
2. Even if it did, the new doc_id wouldn't match the synthesized UUID7
   already in events.jsonl, so the chunk would still appear orphan from
   the doctor's perspective

A proper recovery requires either (a) reindexing through a fixed PDF
path AND clearing the synthesized-orphan markers, or (b) running a
new synthesize-log that *prefers* the live catalog Documents over
the pre-existing orphan markers when a content_hash match is found.

## Coverage picture

From doctor (post-prune, post-backfill, 2026-05-02 16:30):

```
Total T3 chunks (estimated)    ~370,000
  with doc_id (non-orphan)     ~ 60,000  (16%)
  orphans (synthesize-log)      309,681  (84%)
```

Per-collection coverage (subset; full report in catalog doctor):

| Collection | Total | with_doc_id | Coverage | Notes |
|---|---|---|---|---|
| code__ART-8c2e74c0 | 63,077 | 63,077 | 100% | Live-indexed via nx index repo |
| docs__nexus-571b8edd | 3,382 | 3,334 | 98.58% | Live-indexed; 48 stragglers |
| rdr__nexus-571b8edd | 4,235 | 4,235 | 100% | Backfilled from synthesize-log UUIDs |
| knowledge__knowledge | 12,474 | 567 | 4.55% | PDF imports; never properly registered |
| knowledge__art | 5,725 | 1 | 0.02% | Same |
| docs__Luciferase-f2d57dbc | 4,197 | 1 | 0.02% | Mirrored repo |
| docs__claude-code-7dbc4de3 | 405 | 1 | 0.25% | Mirrored repo |

Conclusion: **only the host repo's own collections + ART code are
fully wired**. Everything imported via `nx index pdf` or as a mirrored
external repo is orphan.

## Required fixes (in dependency order)

### Phase 4 finisher (must ship before Phase 5)

1. **`doc_indexer._index_document` writes doc_id** — pre-flight catalog
   registration (or lookup) so doc_id is known at chunk-prep time.
   Add `doc_id` parameter to `_pdf_chunks` and `_markdown_chunks`.
   Affects: nx index pdf, nx index md, nx index rdr standalone.

2. **Drop `source_path` from `ALLOWED_TOP_LEVEL`** — completes the
   "dual-write is harmless" bargain referenced in the reader-audit doc.
   Reduces metadata key budget back below the Chroma 32-key cap.

3. **Doctor surfaces orphan ratio** — add a section to
   `--t3-doc-id-coverage` reporting `total_chunks_t3` vs
   `total_non_orphan_in_log` vs `coverage_of_non_orphans`. Warn when
   orphan ratio > 50% on a collection. PASS gate stays as-is for now
   (changing it would invalidate the Phase 4 close criterion).

### Phase 4.5 (orphan recovery — separate effort)

4. **Re-synthesize for known-PDF imports** — re-run `synthesize-log`
   in a mode that walks T3 chunks and matches against `Document`
   entries via content_hash, preferring live Documents over existing
   synthesized-orphan markers. Rewrites events.jsonl in place.

5. **Provide a "reindex collection through fixed pipeline" verb** —
   one-shot CLI that pulls each chunk's source_uri from T3, runs it
   through the (fixed) doc_indexer pipeline, and re-upserts. Cheaper
   than full reindex when source files exist.

## Open questions

- Should `_PRUNE_DEPRECATED_KEYS` and `ALLOWED_TOP_LEVEL` be
  cross-validated by a unit test? CI didn't catch the divergence.
- Is the upsert-merges-metadata behavior documented anywhere in
  ChromaDB's contract? If it changes, every reindex would silently
  start clearing doc_id.
