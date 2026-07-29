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
