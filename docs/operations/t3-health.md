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

**Cause**: a T3 collection was deleted (manual `nx collection delete`, or chromadb-side housekeeping) without going through `Catalog.supersede_collection`.

**Action**: operator decision. If the documents that referenced this collection are still real, re-create the T3 collection by re-indexing the source files. If they're stale, either delete the catalog rows (`nx catalog delete <tumbler>`) or supersede the collection projection row to a known target via the manual Python snippet the doctor prints. There is no automated fix for this class because the right answer depends on whether the source data still exists.

## `--t3-doc-id-coverage`

### Symptom: `WARN: <collection> orphan_ratio=100.00%` for every collection

Most collections show 100% orphan ratio.

**Cause**: post-RDR-108 architecture removed `doc_id` from chunk metadata; the catalog `document_chunks` manifest is the authoritative position record. Collections where chunks lack `doc_id` AND lack a manifest entry are the genuine orphan class. The 100% ratios you see are usually historical synthesized-orphan events from the retired Phase 5b verb, not live drift.

**Action**: ignore unless `--strict-not-in-t3` flips the check to FAIL. The doctor's manifest-fallback path (`nexus-esrl`) resolves chunks via `chash -> doc_id` from the catalog manifest, so a chunk that has `chunk_text_hash` AND a manifest row counts as covered.

### Symptom: `taxonomy__centroids` reports a high orphan count

**Cause**: `taxonomy__*` collections are bypass-schema. Centroids carry `{topic_id, label, doc_count, collection}` metadata; there is no `doc_id` by design (centroids are not documents; they're embedding-space anchors per RDR-070).

**Action**: ignore. As of `nexus-wszt` (PR #653) the doctor skips bypass-schema collections from this audit. If you see them in current output, your `nx` is older than 4.30; upgrade.

## `--replay-equality`

### Symptom: `collections live=N projected=M` with timestamp diffs in the diff section

`created_at` differs between live SQLite and the projected replay.

**Cause**: pre-`nexus-33xm` (PR #654) the auto-bootstrap stamped `created_at = NOW()` while the synthetic `CollectionCreated` event carried `created_at = ""`. The projector's COALESCE preserved the NOW stamp on live but the replay started empty and took the synthetic empty string. Persistent drift on every run.

**Action**: upgrade to 4.30+. The fix lands `created_at = ""` on both sides; `--replay-equality` then PASSes for auto-bootstrapped collections.

### Symptom: `events_applied: <huge>` followed by tabletype-specific FAIL

A specific table type (owners / documents / links / collections) shows a count or row mismatch.

**Cause**: real data drift. Either a direct SQLite write bypassed the projector chain (filed under epsilon-allow review), or a projector handler version is stale.

**Action**: `--json` for the structured payload, share with maintainers. Do NOT run `--rebuild` until the source of the divergence is identified; rebuilding from the event log silently overwrites whichever side was wrong.

## False-positive classes the doctor knows about

The doctor pre-skips these:

- **`taxonomy__*` collections**: bypass-schema (RDR-070 centroids). Excluded from `--t3-doc-id-coverage`, `--collections-drift` projection scan, and `nx catalog backfill-collections`.
- **`x-devonthink-item://` source URIs**: collapsed to a single home bucket (`nexus-n3md` PR #662). Pre-fix every UUID-netlocked DEVONthink import looked like its own home; post-fix the audit treats them as one logical curator.
- **Empty `source_uri` rows**: knowledge notes (MCP-stored, no source file) bucket separately so they can't flip a small clean collection to "contaminated" via a single self-marker row.

## Useful follow-ups

- For the audit-membership three-axis interpretation: [`audit-membership-interpretation.md`](audit-membership-interpretation.md).
- For the post-Phase-3 metadata field semantics (chunk vs document level, `content_hash` vs `chunk_text_hash`): [`../architecture.md`](../architecture.md) § Metadata field semantics.
- Beads referenced above: `nexus-wszt`, `nexus-33xm`, `nexus-esrl`, `nexus-n3md`. Each carries the full prod-probe context from the 2026-05-08 shakeout.
