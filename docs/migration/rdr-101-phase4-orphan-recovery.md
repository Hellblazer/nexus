# RDR-101 Phase 4 — Orphan Recovery Procedure

> Companion operator doc to [RDR-102](../rdr/rdr-102-phase4-completion.md) §D5. Use when `nx catalog doctor --t3-doc-id-coverage` reports a high orphan ratio on a collection that should be tracked, and the `WARN` is operationally hostile because the chunks predate the Phase 4 catalog wiring.

## When to run this

Run when **all three** are true:

- A collection's orphan ratio is above ~50% per `nx catalog doctor --t3-doc-id-coverage --json | jq '.t3_doc_id_coverage.per_coll'`.
- The collection is one you expect to be tracked (knowledge papers, mirrored repos, project docs) — not the deliberately-orphan classes (`docs__stale`, `docs__shakedown_*`, etc.).
- The chunks in the collection were indexed **before** the Phase A doc_indexer fix landed (commit `c4b523d0`); fresh writes after that commit already populate `doc_id` at chunk-write time.

The recovery loop converts the operational `WARN` into an actionable signal. Skip it for collections you do not care to recover.

## What the recovery does

T3 chunks indexed pre-Phase-A carry `source_path` + `content_hash` but no `doc_id`. The doctor's coverage check classifies them as orphans because the synthesized `events.jsonl` entries cannot map them back to a catalog Document. Re-indexing the source files fixes this for the chunks that get re-written, but a naive re-index is expensive (millions of embeddings on a large collection) and produces a NEW catalog tumbler instead of consolidating with whatever Document the chunks already belong to.

The procedure below threads the existing chunks back to their canonical Document via `content_hash`, then writes the resolved `doc_id` to T3 chunk metadata. No re-embedding. No new tumbler.

## Procedure

### Step 1 — Synthesize the event log against the live catalog

```bash
nx catalog synthesize-log \
  --force \
  --chunks \
  --prefer-live-catalog \
  --json
```

What this does:

- `--force` truncates `events.jsonl` so the synthesis starts from the current state of the catalog and T3, not a possibly-stale prior log. Doc_id mappings from any prior `synthesize-log` run are preserved per-tumbler so a previous `t3-backfill-doc-id` run does not silently invalidate.
- `--chunks` walks every T3 collection and emits one `ChunkIndexed` event per chunk.
- `--prefer-live-catalog` is the recovery toggle. For each chunk that would otherwise orphan (source_path / title both miss the synthesized `DocumentRegistered` events), it consults a `content_hash → tumbler` map built from the live catalog SQLite and uses the matching Document's tumbler as the chunk's `doc_id`. Chunks with no live-catalog content_hash match remain orphan — `--prefer-live-catalog` is a recovery FALLBACK, not an override.
- `--json` returns the report as JSON for scripting. Inspect `orphan_chunks` before vs after to confirm the recovery worked.

The verb is read-only against T3 and the live catalog SQLite — only `events.jsonl` is rewritten.

### Step 2 — Backfill the resolved doc_ids to T3

```bash
nx catalog t3-backfill-doc-id
```

Reads `events.jsonl`'s `ChunkIndexed` entries and writes each `doc_id` to the corresponding T3 chunk's `doc_id` metadata field. Uses ChromaDB `col.update()` — no embeddings touched, no new chunks written. Pre-existing chunks gain the `doc_id` from the synthesized events.

The verb supports `--collection <name>` to scope the backfill to one collection at a time when staging the recovery; omit for an all-collections sweep.

### Step 3 — Re-run the doctor coverage check

```bash
nx catalog doctor --t3-doc-id-coverage --json | jq '.t3_doc_id_coverage'
```

Confirm the orphan ratio dropped on the recovered collection. Residual orphans are chunks whose `content_hash` does not match any live catalog Document — those represent genuine "we cannot determine the canonical Document" cases (file deleted from the source corpus, content drift since first index, etc.) and the operator decides per-collection whether to delete them or accept them as long-tail orphans.

## Limitations of content_hash recovery

The live-catalog lookup map is built from `Document.metadata["content_hash"]`. Documents that do **not** carry `content_hash` in their meta cannot be recovered via this path:

- **Repo-indexed code / prose** (`nx index repo`): YES — `indexer.py:_catalog_hook` stamps `meta={"content_hash": file_hash}` per file. Recovery works.
- **Standalone PDFs** (`nx index pdf`): NOT YET — `_catalog_pdf_hook` does not write `content_hash` to meta. Use the alternate recovery path below.
- **Standalone markdown / RDR** (`nx index md` / `nx index rdr`): NOT YET — `_catalog_markdown_hook` does not write `content_hash` to meta. Use the alternate recovery path below.

For PDFs / markdown collections, the recovery is to **re-index the source files** through the Phase A entry points. After commit `c4b523d0`, every `nx index pdf` / `nx index md` / `nx index rdr` call:

1. Pre-flights catalog registration (resolves an existing tumbler or registers a fresh one — idempotent via `Catalog.register`'s `by_file_path` early-return at `catalog.py:1218-1234`).
2. Threads the resolved tumbler through `make_chunk_metadata(..., doc_id=tumbler)` so chunks land in T3 with `doc_id` populated at write time.

Existing chunks in T3 (whose content_hash hasn't changed) survive the re-index via ChromaDB's metadata-merge upsert — they retain their original chunk text + embedding and gain the `doc_id` field via the new write. The re-index path costs the embedding API calls only for files whose content actually changed.

## No-catalog mode caveat (RDR-102 Phase B side effect)

`nx index pdf` / `nx index md` / `nx index rdr` and `nx index repo` all support running without an initialized catalog (`NEXUS_CATALOG_PATH` points at a directory that has no `.git` / `documents.jsonl`). Pre-RDR-102 the staleness check used `source_path` as the chunk-identity key, so re-indexing an unchanged file in no-catalog mode was a no-op even without `doc_id` wiring.

Phase B removes `source_path` from the chunk schema. With no catalog initialized, `_register_or_lookup_doc_id` returns `""` and the chunks land in T3 with neither `source_path` nor `doc_id`. The staleness check's `_identity_where(file_path, corpus)` falls back to `{"source_path": file_path}`, queries T3, finds zero chunks (nothing carries source_path), and reports "no existing chunks" — so the indexer re-embeds the file every run.

Implications:

- Operators running in no-catalog mode will see every `nx index ...` call re-embed already-indexed files. This wastes Voyage API quota and clock time but does not corrupt T3 — the upsert overwrites with identical content.
- The fix is to initialize a catalog: `nx catalog setup` (or any other path that calls `Catalog.init` at the configured `NEXUS_CATALOG_PATH`). Once initialized, the next index pre-flight registers each file and chunks gain `doc_id` at write time; the staleness check then keys on `doc_id` and re-index becomes a no-op as before.
- This regression is intentional per RDR-102 D2 ("Hard-remove the source_path parameter from make_chunk_metadata") and the substantive-critic gate that rejected the deprecated-noop alternative. The honest-signal approach trades the no-catalog re-index ergonomics for the elimination of the prune-vs-write regression cycle.

If you depend on no-catalog re-index being a no-op (e.g., scripted test fixtures), initialize the catalog as part of the test harness setup or accept the per-run re-embed cost.

## What you should NOT do

- Do **not** run `synthesize-log --prefer-live-catalog` without `--force` against a non-empty `events.jsonl`. The verb refuses to overwrite a non-empty log without `--force`; that guard exists precisely so an accidental re-synthesis does not replace a log carrying production state.
- Do **not** run the recovery on a collection that is intentionally orphan-by-design (e.g., `docs__stale`, `docs__shakedown_*`). The `WARN` on those is the right signal — they are not meant to map back to live Documents.
- Do **not** skip Step 2. Step 1 only updates `events.jsonl`; T3 chunk metadata still carries no `doc_id` until `t3-backfill-doc-id` writes it. Skipping makes the doctor's next run report the same orphan ratio.
- Do **not** treat the residual orphans (those `content_hash` does not match any live Document) as failures. They represent genuine "no canonical Document" state — the collection has chunks for content that is no longer cataloged. Decide per-collection: delete the chunks, accept them as long-tail orphans, or investigate why the content fell out of the catalog.

## Smoke test (operator-runnable, NOT in CI)

Before running on production, validate against one collection. The Delos knowledge collection is a known orphan-heavy dataset and is the canonical smoke target:

```bash
# Backup is cheap — take one before any catalog mutation
cp -a "$(nx config get catalog_path)" "$(nx config get catalog_path).bak.$(date +%Y%m%d-%H%M%S)"

# Baseline before recovery
nx catalog doctor --t3-doc-id-coverage --json \
  | jq '.t3_doc_id_coverage.per_coll["knowledge__delos"] // {}'

# Run the three-step recovery
nx catalog synthesize-log --force --chunks --prefer-live-catalog --json
nx catalog t3-backfill-doc-id --collection knowledge__delos
nx catalog doctor --t3-doc-id-coverage --json \
  | jq '.t3_doc_id_coverage.per_coll["knowledge__delos"] // {}'
```

The orphan ratio should drop measurably between the baseline and the post-recovery doctor run for any collection whose Documents carry `content_hash` in their meta. For PDF-heavy collections like `knowledge__delos` the drop will be partial — see "Limitations" above for the alternate re-index path.

## References

- [RDR-102 §D5](../rdr/rdr-102-phase4-completion.md#d5--orphan-recovery-documented-operator-runnable-path) — design rationale
- [RDR-101 Phase 4 audit (2026-05-02)](rdr-101-phase4-audit-2026-05-02.md) — orphan-ratio baseline
- `src/nexus/catalog/synthesizer.py:build_live_catalog_content_hash_map` — lookup map builder
- `src/nexus/catalog/synthesizer.py:synthesize_t3_chunks` — chunk-resolution priority chain
