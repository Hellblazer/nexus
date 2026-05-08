---
title: "Soft-Delete via Tombstone Columns on Catalog Projection"
id: RDR-106
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-08
related_issues: [nexus-6ims, nexus-tnz3, nexus-9nim]
---

# RDR-106: Soft-Delete via Tombstone Columns on Catalog Projection

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The catalog's destructive verbs (`nx catalog delete`, `gc`,
`prune-stale`, `link-bulk-delete`; `nx t3 prune-stale`) physically
drop rows from the projection (and chunks from T3) in a single
transaction. Recovery requires a full rebuild from `events.jsonl`
(catalog) or a re-index from source (T3, with re-embedding cost).
The 2026-05-08 prod shakeout caught `nx catalog prune-stale`
about to delete 11,766 valid catalog entries because of a
cwd-dependent `Path(fp).exists()` classification bug
(nexus-6ims). Only the user's intuition ("have we checked these
aren't misclassified?") averted a hard-to-recover-from incident.

The 4.29.1 release bundled a backup-before-delete safety net
(JSONL snapshot to `.deleted-backups/`, `nx catalog undelete`
verb) plus the cwd fix. That covers the immediate hazard but
leaves the structural gap: physical delete is the only delete.
There is no in-projection "trashed" state to filter or undo
without an explicit recovery action. Reads of a deleted tumbler
return `None`; the link graph silently loses the edges; UI
surfaces show empty results. A second mistake on top of a backup
(or after the backup TTL elapses) loses the data.

### Enumerated gaps to close

#### Gap 1: Reads can't distinguish "never existed" from "deleted"

`Catalog.resolve(tumbler)` returns `None` for both
"tumbler-never-registered" and "tumbler-deleted-via-prune-stale".
Callers that need to know which case applies (audit tools, UI
banners, link-graph repair) have no signal. A tombstone-aware
read could surface the deleted state.

#### Gap 2: Recovery requires explicit verb invocation

Backup-before-delete (4.29.1) preserves the audit trail but
recovery is operator-driven (`nx catalog undelete <backup>`).
There is no automatic grace period; a deletion is functionally
permanent at the moment the verb runs. A tombstone column makes
"undelete within N days" a flag flip rather than a side-channel
restore.

#### Gap 3: Backup files are out-of-tree state

`.deleted-backups/<verb>-<ts>.jsonl` files live alongside the
catalog dir but are NOT tracked in git, NOT replicated by
`nx catalog sync`, and NOT visible to `nx catalog stats` or
`doctor`. A second machine pulling the catalog has no view of
recent deletions. A tombstone column is in the canonical
projection — visible everywhere the projection is.

#### Gap 4: Link graph integrity during the grace window

When a document is hard-deleted, the link graph loses BOTH the
inbound and outbound edges. If the deletion was a mistake, the
edges are lost too — the catalog can't rebuild them without the
backup. With tombstone columns, edges remain in the graph but
filter out by default; the relationship is recoverable atomic
with the document.

## Context

### Background

RDR-101 (event-sourced catalog) established events.jsonl as
canonical truth; the SQLite projection is regenerated on demand.
That model already provides "soft" history at the event-log
level — a deleted document is still in the log. But the
projection drops the row, and every read goes through the
projection. The events.jsonl preserves the audit trail; it does
not provide read-time filtering, undo, or grace-period semantics.

The 4.29.1 backup-before-delete pattern (Option A in the
2026-05-08 verb-safety review) is a recovery affordance bolted
onto physical deletes. It works for "I deleted the wrong thing
yesterday," but doesn't change the read semantics or provide a
grace window. Tombstone columns are the proper architectural
fix.

### Technical Environment

- `documents` SQLite table — currently no `deleted_at` column
- `links` SQLite table — same
- `collections` SQLite table — already has `superseded_by` /
  `superseded_at` (a partial form of soft-delete via
  supersession; the new tombstone columns generalize this)
- `Projector.apply` verbs — currently `DocumentDeleted` does
  `DELETE FROM documents WHERE tumbler = ?`; the new verb
  `DocumentSoftDeleted` would `UPDATE documents SET
  deleted_at = ?, deleted_reason = ?`
- All read paths in `_DocumentOps` and `_LinkOps` need to filter
  on `deleted_at IS NULL OR deleted_at = ''` by default

## Research Findings

### Investigation

(to be filled during /nx:rdr-research)

### Critical Assumptions

- [ ] **Existing events.jsonl deletions can be coalesced into the
  new schema without losing history** — Status: unverified.
  Method: write a migration that walks `DocumentDeleted` events
  and re-emits them as `DocumentSoftDeleted` with a synthetic
  `deleted_at` from the original event timestamp, then a
  follow-up `DocumentPurged` for the actual SQL DELETE. Replay
  equality should hold.
- [ ] **All read paths can be uniformly filtered** — Status:
  needs audit. Some read paths (audit tooling, doctor checks)
  legitimately want to see soft-deleted rows. The right
  abstraction is a `include_deleted=False` parameter that
  defaults to False but threads through.
- [ ] **Performance: adding `WHERE deleted_at IS NULL` to every
  query is acceptable** — Status: assumed safe based on the
  existing `superseded_by` filter pattern, but should benchmark
  against a 23K-document catalog.

## Proposed Solution

### Approach

Add `deleted_at` (TEXT, ISO 8601) and `deleted_reason` (TEXT)
columns to `documents` and `links`. Keep the existing
`superseded_by` / `superseded_at` on `collections` (semantic
overlap is acceptable; collections supersede, documents/links
soft-delete). New projector verbs:

- `DocumentSoftDeleted` — `UPDATE documents SET deleted_at = ?,
  deleted_reason = ? WHERE tumbler = ?`
- `LinkSoftDeleted` — same shape on links
- `DocumentPurged` — `DELETE FROM documents WHERE tumbler = ?`
  (the actual destructive op; only emitted by
  `nx catalog purge` after the grace window)
- `LinkPurged` — same shape on links

The existing `DocumentDeleted` and `LinkDeleted` events stay in
events.jsonl for back-compat; they project to soft-delete
semantics post-migration (UPDATE rather than DELETE) so old
events replay into the new schema correctly.

New `nx catalog purge --older-than 30d` verb walks the
soft-deleted rows and emits `*Purged` events for those past the
grace window. Default grace period is 30 days.

`Catalog.resolve(tumbler)` filters `deleted_at IS NULL` by
default; `Catalog.resolve(tumbler, include_deleted=True)`
surfaces it. Same parameter on `find`, `by_*`, link query
methods.

`nx catalog undelete <tumbler>` emits `DocumentUndeleted` event
that clears the columns. Reversible while soft-deleted; not
reversible after purge.

### Technical Design

(to be filled during /nx:rdr-research and /nx:create-plan)

#### Phase 1: Schema migration

- Add `deleted_at TEXT DEFAULT ''` and `deleted_reason TEXT
  DEFAULT ''` to `documents` and `links` via T2 migration step
  (idempotent; checks for column existence before adding).
- Migration also walks events.jsonl: for every
  `DocumentDeleted` / `LinkDeleted` event, emits a
  corresponding `*Purged` event so the projection stays
  consistent. Old events are NOT rewritten in place; the
  projector handles both shapes.

#### Phase 2: Projector verbs

- `_v0_document_soft_deleted`, `_v0_document_purged`,
  `_v0_document_undeleted` — all idempotent.
- `_v0_link_soft_deleted`, `_v0_link_purged`,
  `_v0_link_undeleted` — same.
- Existing `_v0_document_deleted` and `_v0_link_deleted` rewire
  to call the soft-delete projector internally (back-compat).

#### Phase 3: Read-path filter

- `_DocumentOps.resolve` / `find` / `by_*` /
  `list_by_collection` / `all_documents` — add
  `include_deleted=False` parameter, default False.
- `_LinkOps.links_from` / `links_to` / `link_query` /
  `bulk_unlink` — same.
- SQL filter: `WHERE (deleted_at IS NULL OR deleted_at = '')`
  appended to existing WHERE clauses.

#### Phase 4: New verbs

- `nx catalog undelete <tumbler>` — reversal within grace.
- `nx catalog purge --older-than 30d` — physical delete after
  grace.
- `nx catalog list-deleted` — show soft-deleted entries with
  age + reason (operator visibility).
- Existing destructive verbs (`delete`, `gc`, `prune-stale`,
  `link-bulk-delete`) rewire to emit `*SoftDeleted` events
  instead of `*Deleted`. The 4.29.1 backup pattern stays as
  belt-and-suspenders.

#### Phase 5: Doctor + tests

- `nx catalog doctor --soft-delete-health` — count
  soft-deleted, age distribution, oldest entry.
- Replay-equality must handle the new verbs.
- Test coverage: per-verb soft-delete + undelete + purge cycle,
  read-path filter (default + override), backwards-compat with
  old `DocumentDeleted` events.

### Decision Rationale

(to be filled during /nx:rdr-research)

## Alternatives Considered

### Alternative A: Backup-before-delete only (4.29.1 Option A)

Pure recovery layer; no schema change; no read-path changes.
Pros: trivial, immediate. Cons: doesn't solve gap 1 (reads
can't distinguish), gap 2 (recovery is operator-driven), gap 3
(backup files are out-of-tree), gap 4 (link graph still loses
edges atomically with the document). Adopted as the 4.29.1
safety net but insufficient as the long-term answer.

### Alternative B: Supersession-based soft-delete (Option C)

Reuse the existing `supersede_collection` / `set_alias`
mechanism. Set `superseded_by='__deleted__'` on documents /
links. Pros: zero schema change. Cons: semantic conflation
(supersession means "replaced by Y"; using it for "deleted, no
replacement" muddies a clean abstraction). Read paths that
already filter superseded would extend; many don't and need
surgery anyway. Net surgery comparable to schema-add.

### Alternative C: Trash table

Move deleted rows to a parallel `trashed_documents` table.
Pros: keeps the live table clean. Cons: every read that wants
"include deleted" needs a UNION; the trash table needs its own
indexes; schema duplication; doesn't generalize cleanly to
links and collections.

### Briefly Rejected

- Per-row `is_deleted BOOL` instead of `deleted_at` — loses
  age info needed for grace-window purge. Tombstone column with
  timestamp is the canonical pattern.
- Hard-delete with a `deleted_log` audit table — equivalent to
  the 4.29.1 backup pattern at higher complexity. Adopted at
  the file level (`.deleted-backups/`) instead.

## Trade-offs

### Consequences

- **Read-path performance**: every catalog read filters on the
  new column. SQLite handles this efficiently with an index on
  `deleted_at`, but every WHERE clause grows by one predicate.
  Benchmarks against the 23K-document prod catalog will measure
  the actual cost.
- **Replay-equality complexity**: the projector now has 6
  document/link verbs (Registered, SoftDeleted, Undeleted,
  Purged, Updated, plus the legacy Deleted that maps to
  SoftDeleted). Replay-equality testing surface widens.
- **Grace window operational discipline**: operators must run
  `nx catalog purge` (or wait for an automatic schedule) to
  reclaim space. Without it, soft-deleted rows accumulate.

### Risks and Mitigations

- **Risk**: Migration leaves projection inconsistent with
  events.jsonl.
  **Mitigation**: Migration is replay-equivalent — running
  `nx catalog doctor --replay-equality` after migration must
  pass (modulo the new `deleted_at` columns which are part of
  both live and projected state).
- **Risk**: `include_deleted=False` parameter forgotten on a
  read path → soft-deleted rows leak.
  **Mitigation**: All read methods on `_DocumentOps` /
  `_LinkOps` get the parameter at extraction time; lint-style
  test scans for any new read method that doesn't accept it.

### Failure Modes

- Soft-delete row count growing unboundedly without `purge`
  cron → projection grows but reads stay correct (filtered).
- Migration dry-run shows replay-equality divergence → block
  release.

## Implementation Plan

### Prerequisites

- 4.29.1 backup-before-delete shipped (Option A done).
- Schema-migration framework supports column additions
  (already exists; T2 migrations).

### Phases

P1: Schema migration + projector verbs (no read-path changes
yet; events.jsonl still uses old verbs; projector translates).
P2: Read-path filter on `_DocumentOps` and `_LinkOps`.
P3: New verbs (undelete, purge, list-deleted).
P4: Existing destructive verbs rewired to emit `*SoftDeleted`.
P5: Doctor checks + backwards-compat smoke tests.

### Minimum Viable Validation

- Schema migration runs cleanly on the 23K-document prod
  catalog (`nx catalog doctor --replay-equality` passes
  post-migration).
- `nx catalog delete <tumbler>` followed by
  `nx catalog undelete <tumbler>` round-trips cleanly.
- `nx catalog purge --older-than 30d` only deletes rows with
  `deleted_at` older than 30 days.
- Read paths return None for soft-deleted by default; surface
  with `include_deleted=True`.

## Test Plan

(to be filled during /nx:rdr-research)

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

- **Versioning**: schema bump (T2 migration); event-log
  back-compat preserved.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: ships in conexus wheel.
- **Incremental adoption**: existing catalogs migrate on first
  Catalog construction post-upgrade.
- **Memory management**: N/A
- **Secret/credential lifecycle**: N/A

### Proportionality

Right-sized: schema additions are minimal (2 columns × 2
tables); the projector verb count grows from 2 to 6 per kind,
but each verb is a one-line UPDATE/DELETE; read-path filter is
a one-clause WHERE addition. Multi-week implementation
matches the value: structural answer to the data-loss verb
class identified in the 2026-05-08 review.

## References

- nexus-6ims (P0): catalog prune-stale + t3 prune-stale cwd bug
- nexus-tnz3 (P1): catalog gc default-flip
- nexus-9nim (P2): link-bulk-delete --confirm
- 4.29.1 release: backup-before-delete (Option A)
- RDR-101: event-sourced catalog (the substrate this builds on)
- RDR-104: incremental projection rebuild (the substrate the
  migration uses)
