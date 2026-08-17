# T3 health runbook

When `nx catalog doctor` reports something concerning, this page tells you whether the report is a real problem or a known false-positive class. Each section starts with the symptom (what the doctor says) and ends with the action (do this, or ignore and add `# noise` to the noise list).

The doctor is intentionally conservative: it favors WARN over silent pass. Several reported classes are operationally fine, OR are surfacing pre-existing data drift that the architecture has since obviated. Knowing which is which saves 30 minutes of forensics per investigation.

## `--collections-drift`

### Symptom: `T3 collections without projection rows (N)`

T3 has a collection that the catalog `collections` projection table doesn't know about.

**Cause**: indexer or MCP write created the T3 collection, but the catalog hook either failed silently or hasn't run yet.

**Action**: `nx catalog backfill-collections --no-dry-run`. Idempotent; safe to run any time. After the verb the projection has one row per T3 collection, drift count drops to 0.

### Symptom: `Projection rows whose T3 collection is gone and not superseded (N)`

The catalog has a collection row, but T3 doesn't have a matching collection.

**Cause**: a T3 collection was deleted (manual `nx collection delete`, or service-side / migration housekeeping) without going through `Catalog.supersede_collection`.

**Action**: operator decision. If the documents that referenced this collection are still real, re-create the T3 collection by re-indexing the source files. If they're stale, either delete the catalog rows (`nx catalog delete <tumbler>`) or supersede the collection projection row to a known target via the manual Python snippet the doctor prints. There is no automated fix for this class because the right answer depends on whether the source data still exists.

### Symptom: `Projection rows whose T3 collection is TRASHED but RESTORABLE (N)`

The catalog has a collection row, and T3 has NO live chunks under that name — but the chunks are not gone. Every one belongs to a soft-deleted (trashed) document (`catalog_documents.deleted_at` set); trashing never touches the physical `chunks_<dim>` rows (that is `purge_trash`'s job, a separate later step). `list_collections()` reads the tombstone-filtered `collection_vector_stats` view, so a fully-trashed collection reads exactly like one that never existed unless the doctor specifically probes for it (`probe_collection_state`, nexus-9n485).

**Cause**: every document that ever wrote into this collection has since been trashed, but nothing purged the underlying chunk rows.

**Action**: do **NOT** supersede this name — superseding sets `superseded_by`, which permanently excludes the name from resolution, and the revive path is identity-gated (only the collection a row was superseded FROM may revive it), so superseding a tombstoned name turns a reversible trash into an unreachable orphan (nexus-e1k14, the bug this symptom class exists to prevent). There is currently no CLI/MCP/REST verb that restores a trashed document — `document_restore` is a PG function (`catalog-003-soft-delete.xml`) with zero operator-facing callers anywhere in the codebase (nexus-xavu7, open). Until that verb exists, recovery means a direct SQL `UPDATE` against `catalog_documents.deleted_at`, scoped with an explicit `WHERE tenant_id = <this tenant> AND collection = <this name>` clause, run only by someone with production DB access. Once restored, the chunks reappear in the live T3 view and the collection drops out of this report on its own.

### Symptom: `Projection rows whose T3 state could not be probed (N) -- NEEDS RERUN`

The per-name tombstoned-vs-absent probe (`probe_collection_state`) raised for this name — a transient network/service failure, not a classification. The check reports it separately rather than either guessing a bucket or aborting the whole run for every other candidate: the established contract here is best-effort-with-report, not all-or-nothing.

**Cause**: the vector service was briefly unreachable during the per-name probe round trip (a second network call per candidate, made after the bulk T3 listing already succeeded).

**Action**: re-run `nx catalog doctor --collections-drift` once the vector service is reachable. A name in this bucket has NOT been classified as tombstoned or gone — neither of the two remedies above applies until it re-probes cleanly.

## Removed check: manifest damage (RUNFENCE) corpus-wide sweep

`nx doctor`'s corpus-wide "dangling manifest chashes" sweep
(`manifest_verify_all()`) and the per-document `nx catalog manifest-verify
TUMBLER_OR_TITLE` diagnostic it pointed at were REMOVED in RDR-191 Phase 6
(bead nexus-o8dil.33, 2026-08-15). The manifest-chunk FK
(`catalog_document_chunks -> nexus.chunks`, RDR-191 Phase 5, VALIDATEd)
now REJECTS a manifest row referencing a nonexistent chunk at write time —
the class this section used to triage (chunks removed or superseded
without the manifest being updated to match) is unreachable by
construction, so there is no longer a corpus sweep or a per-document check
to run for it.

If a document's chunks look wrong today, use `nx catalog show
TUMBLER_OR_TITLE` (`index_state`) instead: the write-path fail-closed
verify-then-stamp (`CatalogRepository.completeIndexRun`) already refuses
to mark a document `complete` unless its manifest was verified whole at
write time, so `index_state != 'complete'` is the current signal that
`nx index <path> --force` is worth running. `nx catalog verify
--collection NAME` still detects per-document manifest damage
client-side, independent of the retired engine functions, if you need a
`referenced`/`present`/`missing` breakdown for a specific collection.

If you are troubleshooting a pre-Phase-6 install and hit this symptom, the
guidance (including the pre-fence-engine 404 false-positive note) is in
this file's git history.

## Removed checks: `--t3-doc-id-coverage` and `--replay-equality`

Both were deleted in 7.0.0 (nexus-i711w) along with the local catalog, and
their troubleshooting sections went with them. Each read its expectations out
of the local `events.jsonl`, and replay-equality additionally diffed a
projection rebuilt from that log against the local `.catalog.db`. Neither
artifact exists in service mode, where the nexus service owns the catalog, so
both checks already refused there before they were removed.

If you are on a pre-7.0.0 install and hit one of their symptoms, the guidance
is in this file's git history.

## False-positive classes the doctor knows about

The doctor pre-skips these:

- **`taxonomy__*` collections**: bypass-schema (RDR-070 centroids). Excluded from the `--collections-drift` projection scan and `nx catalog backfill-collections`.
- **`x-devonthink-item://` source URIs**: collapsed to a single home bucket (`nexus-n3md` PR #662). Pre-fix every UUID-netlocked DEVONthink import looked like its own home; post-fix the audit treats them as one logical curator.
- **Empty `source_uri` rows**: knowledge notes (MCP-stored, no source file) bucket separately so they can't flip a small clean collection to "contaminated" via a single self-marker row.

## Useful follow-ups

- For the audit-membership three-axis interpretation: [`audit-membership-interpretation.md`](audit-membership-interpretation.md).
- For the post-Phase-3 metadata field semantics (chunk vs document level, `content_hash` vs `chunk_text_hash`): [`../architecture.md`](../architecture.md) § Metadata field semantics.
- Beads referenced above: `nexus-wszt`, `nexus-33xm`, `nexus-esrl`, `nexus-n3md`. Each carries the full prod-probe context from the 2026-05-08 shakeout.
