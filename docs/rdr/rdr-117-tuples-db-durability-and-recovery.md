---
title: "tuples.db durability and recovery: backup, restore, and rebuild story"
id: RDR-117
type: Architecture
status: draft
priority: medium
author: Hellblazer
reviewed-by: self
created: 2026-05-17
accepted_date:
related_issues: [nexus-6m9i]
---

# RDR-117: tuples.db durability and recovery: backup, restore, and rebuild story

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The third 360° review (umbrella `nexus-6m9i`, dimension-3 failure
recovery, scratch `1053fcd6`) escalated to CRITICAL the absence
of a backup / restore story for `tuples.db`. The sibling
`memory.db` ships with:

- pre-migration snapshot via `pre_migration_backup(...)`
- a documented rotation policy (`retention=3`)
- an operator-facing recovery doc (`docs/operations/migration-recovery.md`)
- `introspection.export(db="memory")` for ad-hoc snapshots

`tuples.db` has none of these. It holds:

- the events table (the append-only log that the binding watcher
  + EventStream subscribers depend on for cursor resumption)
- the claim history (`tuple_claim_log`)
- the watcher cursors (`watcher_state`)
- the canonical tuple rows (joined to Chroma vectors for semantic
  search)

If `tuples.db` corrupts (disk error, abrupt power loss against a
WAL mid-checkpoint, ENOSPC during a write, accidental `rm`), the
operator has no documented recovery procedure and no rebuild path.

### Enumerated gaps to close

#### Gap 1: No backup mechanism for tuples.db

`introspection._export_sqlite` exists but is hardcoded to
`memory.db`. There is no `nx daemon t2 export --db tuples` verb,
no `pre_migration_backup` equivalent on the daemon side for tuples,
and no scheduled snapshot policy.

#### Gap 2: No restore / rebuild documentation

`docs/operations/migration-recovery.md` walks the operator through
`memory.db` corruption. There is no equivalent for tuples.db.
The events table is conceptually replay-source-of-truth for the
binding watcher's effects, but no documented procedure exists to
rebuild claim state or watcher cursors from a partial snapshot
plus the events log.

#### Gap 3: No `nx daemon t2 stop --force` for wedged daemons

`nx daemon t2 stop` does a clean shutdown; if the daemon is wedged
(deadlock, hung on a slow chroma write), the operator must
`kill -9` the pid in the discovery file AND manually `rm` the UDS
socket file (the kill path does not unlink the socket — only
discovery.json gets the marker+unlink ladder). This compounds the
recovery surface: a wedged daemon leaves orphaned filesystem
state that future starts trip on.

This RDR includes the force-stop verb in scope because the recovery
narrative cannot succeed without it.

## Context

### Background

Discovered through the third 360° failure-recovery audit. The
substrate the daemon has accumulated to date (`tuples.db`,
`tuple_claim_log`, `watcher_state`, `events`, `action_idempotency`
which lives in memory.db) has shipped without ever asking "what
happens when this corrupts?". The second 360° dim-2 + dim-8 covered
in-process resource hygiene + atomic writes, but did not consider
the after-the-event recovery story.

### Technical Environment

- Python 3.12+. SQLite WAL mode on tuples.db. `journal_size_limit`
  + `wal_autocheckpoint` set by `open_tuples_db` (after third 360°
  RECOV S-3).
- Daemon owns the writer (RDR-112 §9). Clients route through the
  RPC surface; the introspection RPCs `exec_raw` / `export` exist
  but are memory.db-only.
- `events` table receives one row per `out` / `claim` / `ack` / `nack`
  via SQLite triggers (`trg_tuples_out`, `trg_claim_log_event`).
- `watcher_state` persists last-rowid per (subspace_glob, profile).
- The chroma collection sibling holds embeddings; chroma has its
  own restore semantics (out of scope for this RDR but referenced
  for the join story).

## Research Findings

### Investigation

To be expanded under `/nx:rdr-research`. Minimum reading:

- `src/nexus/daemon/introspection.py` — `_export_sqlite` /
  `_export_*` methods; understand the memory-only assumption.
- `src/nexus/db/migrations.py` — `pre_migration_backup(...)`
  pattern (retention=3, snapshot directory layout).
- `src/nexus/tuplespace/store.py` — schema, triggers, indexes,
  the contracts a restored copy must preserve.
- `docs/operations/migration-recovery.md` — the existing memory.db
  recovery doc as a template.
- SQLite `BEGIN IMMEDIATE` + Online Backup API
  (sqlite3.Connection.backup) for the live-daemon snapshot path.
- `nx daemon t2 stop` flow in `src/nexus/commands/daemon.py` —
  understand the current clean-shutdown path before adding `--force`.

### Critical Assumptions

- [ ] SQLite Online Backup API can snapshot a WAL-mode database
  with the daemon's writer continuing to run, without blocking the
  writer beyond the page-copy windows
  — **Status**: Documented — **Method**: Source Search
- [ ] events table is genuinely append-only since RDR-110 shipped
  (no rows ever updated post-insert) — **Status**: Unverified
  — **Method**: Source Search
- [ ] The chroma collection can be rebuilt from `tuples.db.content`
  + `dimensions` via `TupleIndex.from_registry(...)` plus a
  replay-out loop, without losing any post-restore vectors
  — **Status**: Unverified — **Method**: Spike

## Proposed Solution

### Approach

Four coordinated deliverables:

1. **Snapshot verb**: `nx daemon t2 export --db tuples
   --output <path>` issues a UDS-only admin RPC that runs
   `sqlite3.Connection.backup(...)` against the daemon's writer
   connection. Output is a single .db file the operator copies
   off-host. Companion `--db memory` is the existing path; the
   `--db tuples` extension fills the gap.
2. **Scheduled snapshot policy**: a new optional daemon config
   knob (env: `NX_TUPLES_BACKUP_INTERVAL_S`, default: unset =
   disabled) triggers periodic snapshots into a rotation directory
   (`<config_dir>/backups/tuples/`) with `retention=N` matching
   the existing memory.db pattern.
3. **Recovery doc + rebuild procedure**:
   - `docs/operations/tuplespace-recovery.md` walks the operator
     through tuples.db corruption: snapshot restore, chroma rebuild
     from rows, watcher cursor reset.
   - Define what is recoverable: tuple content + dimensions
     (recoverable from snapshot OR event replay if newer); claim
     history (recoverable from snapshot only); active claims
     (recoverable from the latest snapshot OR rebuildable as fresh
     by waiting out lease TTLs).
4. **Force-stop verb**: `nx daemon t2 stop --force` (Gap 3) sends
   SIGTERM as normal; on a `--force-timeout` (default 10s) elapsed,
   sends SIGKILL, unlinks the UDS socket, and stamps the discovery
   marker. Operator's wedge-recovery procedure becomes one command.

### Technical Design

To be expanded. Initial sketch:

- Extend `introspection.export` (UDS-only) with a `db` argument
  matching the existing memory/tuples discriminator.
- Snapshot uses `sqlite3.Connection.backup(...)` with `pages=1024
  per step` so writer interleaving is preserved (page-copy windows
  < 50ms).
- Scheduled snapshot loop reuses the `_retention_task` pattern
  (`asyncio.create_task` + cancel-on-stop).
- Recovery doc structure mirrors `docs/operations/migration-recovery.md`.
- Force-stop is a thin wrapper around the existing `stop_cmd`
  that adds `--force` + `--force-timeout` click options. SIGKILL
  + unlink belong on the CLI side (the daemon cannot kill itself).

### Decision Rationale

Substantive design — the recovery procedure has multiple defensible
shapes (snapshot vs. event-replay vs. hybrid) and the operator
surface (CLI verbs vs. file-drop-and-restart) needs a single
opinion. Deferred from third 360° remediation precisely because
inline shipping of incomplete options would lock in the wrong
operator workflow.

## Alternatives Considered

### Alternative 1: Event-replay-from-snapshot only (no live backup)

**Description**: Skip the live backup API entirely. Document
that the operator stops the daemon, copies the .db file, restarts.
Recovery is "stop, restore, restart"; no Online Backup API.

**Pros**:
- Simpler — `cp` is the API.
- No daemon-side code change beyond the recovery doc + force-stop.

**Cons**:
- Operators must accept downtime for every snapshot.
- Memory.db has a no-downtime snapshot path already (`introspection.export`);
  asymmetry is hostile.

**Reason for rejection (provisional)**: memory.db precedent +
operator ergonomics rule this out.

### Alternative 2: Drop tuples.db; persist nothing

**Description**: Treat the tuplespace as an in-process queue with
no persistence. On restart, everything is empty.

**Pros**:
- No recovery problem.

**Cons**:
- Breaks RDR-110 (durability is a CA), RDR-111 (binding cursors
  must survive restarts), RDR-112 (single-writer-of-tuples.db
  is a load-bearing invariant).

**Reason for rejection (provisional)**: contradicts every shipped
RDR in the substrate.

### Briefly Rejected

- **Daemon-side automatic restore from latest snapshot on corrupt
  detection**: rejected as too aggressive — operator should
  authorise destructive recovery rather than have the daemon
  decide silently.

## Trade-offs

### Risks and Mitigations

- **Risk**: Online Backup API page-copy interleaving stalls the
  writer in pathological cases.
  **Mitigation**: spike measurement under the existing CA-3
  read-latency harness; expose `--pages-per-step` if needed.
- **Risk**: Scheduled snapshot accumulation eats disk.
  **Mitigation**: retention rotation policy + a `nx daemon t2
  doctor` line surfacing snapshot size.
- **Risk**: SIGKILL during a write window leaves a partial WAL
  that next-start must recover (sqlite handles this, but bears
  testing).
  **Mitigation**: include a recovery-path integration test.
- **Risk**: chroma rebuild from rows is lossy if the rows were
  ever updated post-out (vector embedding generated at out time
  cannot be re-derived if the match_text was edited).
  **Mitigation**: declare events as append-only in the doc;
  document the lossy-edit edge case if it arises.

## Implementation Plan

To be expanded. High-level phases:

- Phase 1: extend `introspection.export` for tuples; ship
  `nx daemon t2 export --db tuples`.
- Phase 2: scheduled snapshot loop + rotation.
- Phase 3: `nx daemon t2 stop --force` verb.
- Phase 4: `docs/operations/tuplespace-recovery.md`.
- Phase 5: chroma-rebuild helper (if scope warrants — could be
  a Phase 5 follow-up RDR).

## Test Plan

- **Scenario**: live snapshot under concurrent writer load —
  **Verify**: snapshot consistent (post-restore: schema correct,
  no orphan rows, claim state coherent); writer latency
  unchanged within noise floor.
- **Scenario**: scheduled snapshot rotation —
  **Verify**: retention=3 keeps the latest 3, rotates oldest.
- **Scenario**: corrupt tuples.db → restore from snapshot →
  daemon starts cleanly → binding watcher resumes cursors —
  **Verify**: no event replay storm; cursors point at the
  snapshotted last-rowid.
- **Scenario**: wedged daemon → `nx daemon t2 stop --force` →
  daemon process terminated, UDS socket gone, discovery file
  stamped + unlinked, restart succeeds —
  **Verify**: no leftover state from the wedged process.

## Validation

### Performance Expectations

Snapshot latency: comparable to memory.db's `export` (~seconds
per 100k rows). Writer-impact during page-copy windows: <50ms
per step at default pages-per-step. Quantify before locking.

## Finalization Gate

To be completed before `/nx:rdr-accept`.

## References

- nexus-6m9i umbrella (third 360° remediation)
- Third 360° agent scratch entry `1053fcd6` (failure recovery)
- `docs/operations/migration-recovery.md` (memory.db precedent)
- `src/nexus/db/migrations.py` (`pre_migration_backup` pattern)
- SQLite Online Backup API documentation
- RDR-110 (tuple-space durability contracts)
- RDR-112 (daemon as single writer)

## Revision History

(Gate rounds will be appended here.)
