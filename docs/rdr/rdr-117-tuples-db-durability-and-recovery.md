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

Round 1 research complete (2026-05-17). Full evidence in T2:
`nexus_rdr/117-research-1` through `117-research-4`. Sources
consulted:

- `src/nexus/db/migrations.py:4024-4040` — `_backup_sqlite_db()`
  already uses `sqlite3.Connection.backup()` against memory.db
  with a docstring explicitly confirming WAL-safety. Proven
  pattern.
- `src/nexus/daemon/introspection.py:498-538` — `_export_sqlite()`
  uses the identical RO-URI + backup pattern. Reads from a
  *separate* read-only connection, not the daemon's writer. The
  `db` discriminator extension is a one-liner.
- `src/nexus/tuplespace/store.py:73-93` — `tuples` table
  persists `embed_text`, `dimensions_json`, `template_name`,
  `subspace`, `id`, `consumed_at` — every field needed to
  reconstruct a `TupleIndex.out()` call.
- `src/nexus/tuplespace/store.py:142-189` — `events` table
  schema: no `updated_at`, no status column, no soft-delete.
  Triggers `trg_tuples_out` and `trg_claim_log_event` are
  `INSERT`-only.
- `src/nexus/tuplespace/store.py:414-450` — `prune_old_events()`
  performs `DELETE FROM events WHERE ts < ?` on a 7-day default
  retention window. Discovered nuance for A2.
- `src/nexus/tuplespace/index.py` — `TupleIndex.from_registry()`
  is idempotent (`get_or_create_collection`); `TupleIndex.out()`
  uses upsert semantics so re-emission is a no-op for existing
  vectors.
- `src/nexus/commands/daemon.py:187-220` — `stop_cmd` sends
  SIGTERM and returns immediately; does NOT wait, does NOT
  unlink the UDS socket.
- `src/nexus/daemon/t2_daemon.py:1027-1029` — `_bind_uds()`
  unlinks the socket file at NEXT `start`, not at current
  daemon's stop. POSIX does not auto-unlink AF_UNIX socket
  files on process death — orphaned socket survives SIGKILL.
- `src/nexus/daemon/t2_daemon.py:1720` — `_unlink_discovery()`
  stamps `status: "shutting_down"` + unlinks discovery JSON.
  Does NOT touch the UDS socket.
- `src/nexus/commands/daemon.py:907` — `install_cmd` already
  has the `--force` flag pattern. No new verb needed for
  Gap 3.
- Python `sqlite3.Connection.backup(target, *, pages=N,
  progress=None, name="main", sleep=0.250)` — official docs
  via Context7. `pages=1024` means ~4 MB per step; on a 50 MB
  tuples.db that's ~13 steps with writer unblocked between
  steps. Mid-backup WAL checkpoints are safe; the API
  re-reads modified pages on the next step.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `sqlite3.Connection.backup` against WAL DB | Yes | Already shipped in `migrations.py` + `introspection.py` for memory.db. Acquires shared lock per page-copy step only; writer unblocked between steps. WAL checkpoint mid-backup is handled by the API re-reading modified pages. |
| events table mutability | Yes | Schema has no mutable columns; triggers are INSERT-only. BUT `prune_old_events` deletes rows older than 7 days (default retention). Append-only at the row level, bounded at the log level. |
| TupleIndex rebuild from tuples.db | Yes | Every field needed by `TupleIndex.out` is persisted on the `tuples` row. `from_registry` is idempotent. Upsert semantics make re-emission of existing vectors a no-op. **Filter `WHERE consumed_at IS NULL`** to exclude tombstoned tuples (else they become semantically queryable again). |
| force-stop scope (Gap 3) | Yes | `--force` flag on existing `stop` command (precedent: `install_cmd`). All work is CLI-side: SIGTERM → poll → SIGKILL → unlink UDS socket → unlink discovery. No daemon-side code changes. |

### Key Discoveries

- **Verified** (A1): WAL-mode Online Backup API is already in
  shipped production use for memory.db
  (`_backup_sqlite_db` in migrations.py and
  `_export_sqlite` in introspection.py). Backup connection is
  opened separately (RO-URI), not the daemon's writer. Per-step
  shared lock; writer unblocked between steps. `pages=1024`
  → ~13 steps on a 50 MB DB, microsecond-scale per-step
  windows. See `nexus_rdr/117-research-1`.
- **Verified — with refinement** (A2): events rows are
  structurally immutable post-insert (no mutable columns;
  INSERT-only triggers). **Refinement**: `prune_old_events`
  deletes rows older than the 7-day default retention. The
  RECOVERY DOC must distinguish "row-level append-only"
  (true; validates event-replay consistency within retention)
  from "log-level append-only" (false; bounded by retention).
  Operationally: max-safe-data-loss-window =
  min(snapshot_age, retention_window). Recommend snapshot
  frequency well under 7 days; the proposed
  `NX_TUPLES_BACKUP_INTERVAL_S` default should be ≤ 6h to
  give comfortable margin. See `nexus_rdr/117-research-2`.
- **Verified — with refinement** (A3): chroma rebuild from
  `tuples` rows is closed-form. Every field needed by
  `TupleIndex.out` is preserved (`embed_text`,
  `dimensions_json`, `template_name`, `subspace`, `id`).
  **Refinement**: rebuild loop MUST
  `WHERE consumed_at IS NULL` to exclude tombstoned tuples
  (else consumed tuples become semantically queryable
  again — wrong). Known limitation: if the Voyage model
  version changes between snapshot and rebuild, vectors
  will differ — full rebuild is still correct, but
  semantic rankings may shift until the model stabilises.
  Phase 5 (chroma-rebuild helper) confirmed IN SCOPE; the
  rebuild is ~20 lines, not a separate RDR. See
  `nexus_rdr/117-research-3`.
- **Resolved** (Gap 3 — force-stop scope): `--force` is a
  flag on existing `stop` command, not a new verb. Precedent:
  `install_cmd` already uses `--force`. All work is CLI-side
  (SIGTERM → poll PID → SIGKILL → read uds_path from
  discovery → `Path(uds_path).unlink(missing_ok=True)` →
  `disc.unlink(missing_ok=True)`). No daemon-side code
  changes. Next `start` succeeds cleanly because
  `_bind_uds()` re-unlinks residual socket and the spawn
  lock was released by OS on process death. WAL is
  auto-recovered by SQLite on next open. See
  `nexus_rdr/117-research-4`.

### Critical Assumptions

- [x] SQLite Online Backup API can snapshot a WAL-mode database
  with the daemon's writer continuing to run, without blocking
  the writer beyond brief page-copy windows — **Status**:
  Verified — **Method**: Source Search + Shipped Production
  Evidence (`_backup_sqlite_db` in migrations.py + `_export_sqlite`
  in introspection.py for memory.db).
- [x] events table is genuinely append-only since RDR-110
  shipped (no rows ever updated post-insert) — **Status**:
  Verified with refinement — **Method**: Source Search.
  Row-level true (no mutable columns; INSERT-only triggers).
  Log-level bounded by 7-day default retention via
  `prune_old_events`. Recovery doc must document the
  max-safe-data-loss window.
- [x] The chroma collection can be rebuilt from `tuples.db`
  rows via `TupleIndex.from_registry(...)` plus a replay-out
  loop, without losing any post-restore vectors — **Status**:
  Verified with refinement — **Method**: Source Search.
  All required fields persisted on the `tuples` row.
  Rebuild loop MUST filter `WHERE consumed_at IS NULL` to
  exclude tombstones. Known limitation: Voyage model
  version drift between snapshot and rebuild changes
  vectors (still correct, may shift rankings).

## Proposed Solution

### Approach

Five coordinated deliverables (refined after Round 1
research):

1. **Snapshot verb**: `nx daemon t2 export --db tuples
   --output <path>` issues a UDS-only admin RPC that runs
   `sqlite3.Connection.backup(...)` against a separate
   RO-URI connection (NOT the daemon's writer — this is
   the pattern shipped in `introspection.py` for memory.db
   and confirmed safe under WAL by Round 1 research). Output
   is a single .db file the operator copies off-host. The
   existing `--db memory` path is unchanged; the `--db tuples`
   extension fills the gap.
2. **Scheduled snapshot policy**: a new optional daemon
   config knob (env: `NX_TUPLES_BACKUP_INTERVAL_S`, default:
   unset = disabled) triggers periodic snapshots into a
   rotation directory (`<config_dir>/backups/tuples/`) with
   `retention=N` matching the existing memory.db pattern.
   **Recommended default when enabled**: 6h (≤ 7-day events
   retention window per A2 refinement, leaves comfortable
   margin for event-replay recovery).
3. **Chroma-rebuild helper** (Phase 5 of original — now
   confirmed in scope after A3 research): `nx daemon t2
   rebuild-index [--dry-run]` reads `tuples WHERE
   consumed_at IS NULL` and replays via `TupleIndex.out()`.
   `from_registry` is idempotent; `out` upserts; safe to
   re-run. Required because the chroma collection is NOT
   covered by the SQLite snapshot.
4. **Recovery doc + procedure**:
   - `docs/operations/tuplespace-recovery.md` walks the
     operator through tuples.db corruption: stop daemon →
     restore snapshot → start daemon → `nx daemon t2
     rebuild-index` → verify.
   - Define what is recoverable: tuple content +
     dimensions (recoverable from snapshot; chroma vectors
     recoverable from `tuples` rows via rebuild-index);
     claim history (recoverable from snapshot only);
     active claims (recoverable from latest snapshot OR
     rebuildable as fresh by waiting out lease TTLs).
   - Document the **max-safe-data-loss window** =
     `min(snapshot_age, 7-day events retention)`. If the
     most recent snapshot is older than 7 days AND
     intervening events were pruned, replay cannot bridge
     the gap.
5. **Force-stop flag** (Gap 3 — RESOLVED by research as a
   flag on existing `stop`, not a new verb): `nx daemon t2
   stop --force [--force-timeout 10]` sends SIGTERM, polls
   `os.kill(pid, 0)` up to the timeout, then SIGKILL,
   then unlinks the UDS socket (read from discovery JSON
   BEFORE unlinking discovery), then unlinks discovery.
   All CLI-side. No daemon code changes. Operator's
   wedge-recovery procedure becomes one command.

### Technical Design

Locked-in design after Round 1 research.

#### Snapshot RPC + verb

- Extend `introspection._export_sqlite` (already exists at
  `introspection.py:498-538`) with a `db: Literal["memory",
  "tuples"]` argument. Path lookup branches between
  `self._memory_db_path` and `self._tuples_db_path`.
- The RO-URI open + `sqlite3.Connection.backup(...)` body
  is unchanged — same pattern, different source path.
- `pages=1024` per step (~4 MB/step). On a 50 MB tuples.db
  this is ~13 steps. Writer is shared-locked only during
  each step, unblocked between steps.
- CLI: extend `nx daemon t2 export` to accept `--db
  [memory|tuples]` with `memory` as default for
  back-compat.

#### Scheduled snapshot loop

- New optional `_tuples_snapshot_task: asyncio.Task | None`
  on `T2Daemon`, started in `start_async()` only if
  `NX_TUPLES_BACKUP_INTERVAL_S` is set and positive.
- Loop pattern mirrors existing retention sweep
  (`asyncio.sleep(interval)` → snapshot → rotation →
  loop). Cancelled in `stop_async()`.
- Rotation directory layout:
  `<config_dir>/backups/tuples/tuples-YYYYMMDD-HHMMSS.db`.
  Keep newest `NX_TUPLES_BACKUP_RETENTION` files (default
  3, matching memory.db convention).

#### Chroma-rebuild helper

- New `rebuild_chroma_index(conn, index)` in
  `tuplespace/store.py` or a new
  `tuplespace/recovery.py`:
  ```python
  def rebuild_chroma_index(
      conn: sqlite3.Connection,
      index: TupleIndex,
  ) -> int:
      rows = conn.execute(
          "SELECT id, template_name, subspace, embed_text, "
          "dimensions_json FROM tuples "
          "WHERE consumed_at IS NULL"
      ).fetchall()
      for r in rows:
          dims = json.loads(r["dimensions_json"])
          # Mirror api.py:437-440 production out() call site:
          # metadata MUST include the "subspace" key
          # (TupleIndex.out docstring at index.py:143;
          # _merge_where at index.py:364 unconditionally
          # injects {"subspace": {"$eq": ...}} into every
          # in_() query — rebuilt tuples missing the subspace
          # key are silently invisible to all semantic queries).
          # Dimensions are coerced to Chroma-acceptable
          # primitives (str/int/float/bool) the same way the
          # production call site does.
          meta: dict[str, Any] = {"subspace": r["subspace"]}
          for k, v in dims.items():
              meta[k] = (
                  v if isinstance(v, (str, int, float, bool))
                  else str(v)
              )
          index.out(
              template_name=r["template_name"],
              subspace=r["subspace"],
              tuple_id=r["id"],
              payload=r["embed_text"],
              metadata=meta,
          )
      return len(rows)
  ```
- Admin RPC: `t2.rebuild_chroma_index(dry_run: bool) -> {"rebuilt": int}`.
  In dry-run mode, returns the count of rows that WOULD
  be rebuilt without actually calling `index.out`.
- CLI: `nx daemon t2 rebuild-index [--dry-run]`.
- Safe to re-run (`get_or_create_collection` is
  idempotent; `out` upserts; rebuild reads
  `consumed_at IS NULL` so re-running after a partial
  rebuild only redoes the unconsumed set).
- Documented caveat: if the Voyage model version changed
  between snapshot and rebuild, vectors will differ —
  full rebuild is still correct, semantic queries may
  return different rankings until model stabilises.

#### Force-stop flag (CLI-side only)

- Add `--force` (bool) and `--force-timeout` (int,
  default 10) Click options to existing
  `commands/daemon.py:stop_cmd`. No daemon-side code
  changes.
- Force-stop sequence (CLI-side, reuses the same
  `os.kill(pid, 0)` PID-liveness probe from
  `discovery.py:50-61`; note the `PermissionError`
  disposition is INVERTED here — discovery.py is a
  reader trying to decide whether to attempt a connect,
  so it treats `PermissionError` as "live, try anyway";
  force-stop is an operator command trying to decide
  whether the original daemon is still worth killing,
  so it treats the same exception as "recycled, do NOT
  SIGKILL the wrong process"):
  ```python
  # Read uds_path FROM discovery BEFORE unlinking it
  disc_payload = json.loads(disc.read_text())
  uds_path = Path(disc_payload["uds_path"])

  os.kill(pid, signal.SIGTERM)
  if force:
      deadline = time.monotonic() + force_timeout
      recycled = False
      while time.monotonic() < deadline:
          try:
              os.kill(pid, 0)  # liveness probe
              time.sleep(0.2)
          except ProcessLookupError:
              break  # daemon dead, proceed to cleanup
          except PermissionError:
              # PID exists but under a different UID —
              # the original daemon process exited and
              # the kernel reused this PID for another
              # process. Do NOT SIGKILL it; that would
              # signal the wrong process. Proceed to
              # cleanup. discovery.py:58-61 documents
              # the same rationale.
              recycled = True
              break
      else:
          # Loop completed without break → daemon is
          # still our daemon and is wedged. SIGKILL.
          os.kill(pid, signal.SIGKILL)
          time.sleep(0.5)  # let OS reap
      # Cleanup orphaned filesystem state regardless
      # of whether we SIGKILLed, the daemon exited
      # cleanly, or the PID was recycled.
      uds_path.unlink(missing_ok=True)
      disc.unlink(missing_ok=True)
      if recycled:
          click.echo(
              f"warning: pid {pid} was recycled by the "
              "kernel under a different UID. Discovery "
              "and UDS socket cleaned up; daemon was "
              "already dead. No SIGKILL sent.",
              err=True,
          )
  ```
- Next `nx daemon t2 start` succeeds cleanly because
  `_bind_uds()` re-unlinks residual socket and the
  spawn lock was released by OS on process death.
  WAL is auto-recovered by SQLite on next open.

#### Recovery decision matrix

Cross-product of snapshot freshness × events-table
retention. At the default `NX_TUPLES_BACKUP_INTERVAL_S=6h`
+ `NX_TUPLES_BACKUP_RETENTION=3` (18h of snapshot
coverage) and the 7-day events retention:

| Scenario | Snapshot age | Events age | Recoverable? |
|---|---|---|---|
| Steady state | < 6h | < 7d | **Yes** — restore snapshot + `rebuild-index`; events bridge cursor position from snapshot → corruption time |
| Recent rotation | 6h-18h | < 7d | **Yes** with a gap — restore oldest retained snapshot; events bridge cursor; tuples created in the rotation gap are lost from the snapshot but recoverable from the events log up to retention |
| Snapshot expired | > 18h (rotation rolled off) | < 7d | **Partial** — events bridge cursor position only (no tuple content reconstruction from events; events are claim-log entries, not full tuple replay). Tuples created after the most recent snapshot but before corruption are LOST. |
| Events expired | any | > 7d in gap | **Partial** — same as "Snapshot expired" but with no cursor recovery for the > 7d window |
| Never enabled backups | none | < 7d | **Partial** — `rebuild-index` reconstructs chroma vectors from whatever tuples.db rows survive (if the corruption is chroma-only); events bridge cursor position only |
| Never enabled backups | none | > 7d in gap | **No recovery** — operator should declare data loss and restart from empty |

**Practical rule for operators**: keep
`NX_TUPLES_BACKUP_INTERVAL_S` ≤ 6h. Max-safe-data-loss
window = `min(snapshot_age, 7-day events retention)`.

#### Recovery doc structure

`docs/operations/tuplespace-recovery.md` mirrors
`docs/operations/migration-recovery.md` with these
sections:
1. Symptoms (how to recognise tuples.db corruption)
2. Force-stop the daemon (`nx daemon t2 stop --force`)
3. Restore from snapshot (`cp` over the corrupt file)
4. Restart the daemon (`nx daemon t2 start`)
5. Rebuild the chroma index (`nx daemon t2 rebuild-index`)
6. Verify (smoke queries against restored state)
7. What you lost — reproduce the recovery decision
   matrix verbatim. Walk the operator through finding
   their row by inspecting `<config_dir>/backups/tuples/`
   directory listing and the `events` table's oldest
   `ts` value.
8. Prevention (enable `NX_TUPLES_BACKUP_INTERVAL_S`;
   recommend 6h or less)

### Decision Rationale

**Online Backup API over stop-and-cp**: the simplest
recovery story is "stop the daemon, copy the .db file,
restart" — `cp` is the API. That works for memory.db
today via the introspection.export path BUT also for an
operator who shells in and copies the file while the
daemon is stopped. The reason this RDR mandates the
Online Backup API path for tuples.db is operational
asymmetry: memory.db already has BOTH paths
(`introspection.export` for live snapshots,
`pre_migration_backup` for migration-window snapshots);
giving tuples.db only the downtime path would force
operators to schedule a daemon outage for every backup.
For agent-fanout workloads where the daemon is the
critical path for every Claude subprocess, scheduled
downtime defeats the daemon-as-service model RDR-112
committed to. The Online Backup API is shipped
production code (`_backup_sqlite_db` in migrations.py
+ `_export_sqlite` in introspection.py); reusing it is
strictly less risky than inventing a new pattern.

**Chroma-rebuild from rows over chroma-snapshot**: the
alternative is to snapshot the chroma collection AND
tuples.db in lockstep — give every backup TWO files
that must be restored together. Three problems with
that: (1) chroma's local persistence is its own format
with its own restore semantics, requiring an additional
documented procedure operators must learn; (2)
maintaining consistency between two snapshots requires
quiescing both at the same instant — the Online Backup
API gives no such cross-database synchronisation
primitive, so an operator restoring a snapshot pair
taken even seconds apart could see a row in tuples.db
with no matching chroma vector or vice versa; (3)
chroma vectors are deterministic functions of
`embed_text` + the Voyage model version — they are
strictly reconstructible from tuples.db rows. Rebuild
adds one CLI verb and ~20 lines; snapshot-pairing
adds an entire second restore surface and a
correctness invariant operators must verify by hand.
Rebuild is the strictly simpler shape.

**`--force` flag on existing `stop` over separate
`stop-force` verb**: precedent — `nx daemon t2
install --force` already exists in the same command
group with similar "skip a safety check" semantics
(install: skip the existing-install check; stop:
skip the graceful-wait). Adding `--force` to `stop`
preserves the operator's mental model: there is one
verb for "shut the daemon down", with optional escape
hatches. A separate `stop-force` verb fragments the
namespace and makes operators choose between two
similar-looking commands. The implementation is
CLI-side only — no daemon code changes — which means
both code paths share `stop_cmd`'s discovery
resolution and config-dir handling without
duplication. The split-verb shape would either
duplicate that code or factor a third helper, both of
which add maintenance surface for zero operator
benefit.

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

Sequenced after Round 1 research.

- **Phase 1: snapshot RPC + CLI verb**
  - Extend `introspection._export_sqlite` with `db`
    discriminator (memory|tuples).
  - Extend CLI `nx daemon t2 export` with `--db
    [memory|tuples]` (default memory for back-compat).
  - Test: snapshot tuples.db under concurrent writer
    load; verify post-restore schema, claim coherence,
    no orphan rows; verify writer latency within noise
    floor.
- **Phase 2: chroma-rebuild helper + CLI verb**
  - Implement `rebuild_chroma_index(conn, index)` with
    `WHERE consumed_at IS NULL` filter.
  - Admin RPC `t2.rebuild_chroma_index(dry_run)`.
  - CLI `nx daemon t2 rebuild-index [--dry-run]`.
  - Test: corrupt chroma collection; rebuild from
    tuples.db; smoke-query restored vectors.
- **Phase 3: force-stop flag**
  - Add `--force` / `--force-timeout` Click options to
    `commands/daemon.py:stop_cmd`. CLI-side only.
  - Test: wedge a daemon (block on a slow chroma write);
    run `stop --force`; verify process gone, UDS socket
    unlinked, discovery unlinked; restart succeeds.
- **Phase 4: scheduled snapshot loop + rotation**
  - `_tuples_snapshot_task` on T2Daemon, gated on
    `NX_TUPLES_BACKUP_INTERVAL_S`.
  - Rotation directory `<config_dir>/backups/tuples/`
    with `NX_TUPLES_BACKUP_RETENTION` (default 3).
  - Test: enable with 5s interval (test override); run
    for 30s; verify 3 snapshots present, oldest rotated
    out.
- **Phase 5: recovery doc**
  - Write `docs/operations/tuplespace-recovery.md`
    following the structure in §Technical Design.
  - Cross-link from `docs/operations/migration-recovery.md`
    (the memory.db precedent) and from the doctor output
    when corruption is detected.
  - Reviewer test: a fresh operator can follow the doc
    end-to-end without asking questions.

- **Scenario**: live snapshot under concurrent writer
  load — **Verify**: snapshot consistent (post-restore:
  schema correct, no orphan rows, claim state coherent);
  writer latency unchanged within noise floor.
- **Scenario**: scheduled snapshot rotation —
  **Verify**: retention=3 keeps the latest 3, rotates oldest.
- **Scenario**: corrupt tuples.db → restore from
  snapshot → daemon starts cleanly → binding watcher
  resumes cursors — **Verify**: no event replay storm;
  cursors point at the snapshotted last-rowid.
- **Scenario**: wedged daemon → `nx daemon t2 stop
  --force` → daemon process terminated, UDS socket gone,
  discovery file stamped + unlinked, restart succeeds —
  **Verify**: no leftover state from the wedged process.
- **Scenario (rebuild metadata correctness — C1
  regression)**: snapshot tuples.db with tuples in N
  distinct subspaces → corrupt chroma collection →
  `rebuild-index` → call `in_(subspace="...")` on every
  subspace — **Verify**: each `in_` call returns the
  exact tuples seeded for that subspace (count + ids
  match). This test formally locks the gate-C1 fix:
  rebuilt metadata MUST include the `subspace` key or
  `_merge_where` returns empty results.
- **Scenario (rebuild race — new tuple during rebuild)**:
  start `rebuild-index` on a snapshot with 1000 tuples;
  while the rebuild loop is running, issue an `out()`
  for a NEW tuple from the live daemon → **Verify**:
  the new tuple appears in chroma exactly once (the live
  `out()` writes it; the rebuild loop's iteration over
  the snapshot does NOT see it since it was inserted
  post-snapshot). No double-write, no missing tuple.
  Documents the snapshot-isolation semantics of the
  rebuild path.
- **Scenario (force-stop with PID reuse — S4
  regression)**: simulate PID-reuse by mocking
  `os.kill(pid, 0)` to raise `PermissionError` after the
  first call → **Verify**: force-stop loop breaks
  without sending SIGKILL; warning printed; UDS socket
  + discovery unlinked anyway; restart succeeds.
- **Scenario (force-stop during mid-snapshot)**: start
  `nx daemon t2 stop --force` while a scheduled snapshot
  loop iteration is mid-flight (e.g. step 7 of 13) →
  **Verify**: partial .db file is NOT left as a valid
  snapshot file in the rotation directory (the
  in-progress snapshot must be either complete OR
  removed; never a partial blob that the operator could
  mistake for a usable backup).
- **Scenario (snapshot under concurrent memory.db
  migration)**: trigger a memory.db migration via
  `pre_migration_backup` path while a tuples.db
  scheduled snapshot is mid-loop — **Verify**: both
  complete without error; neither blocks the other; both
  produce consistent files (memory.db migration
  snapshot, tuples.db scheduled snapshot, both valid).
- **Scenario (operator end-to-end recovery —
  scripted)**: CI integration test runs the full
  recovery sequence: (a) seed daemon with known tuples;
  (b) trigger a scheduled snapshot; (c) corrupt
  tuples.db (truncate, or inject random bytes); (d)
  `nx daemon t2 stop --force`; (e) restore snapshot
  via `cp`; (f) `nx daemon t2 start`; (g) `nx daemon
  t2 rebuild-index`; (h) smoke-query the restored
  state — **Verify**: every step succeeds, query
  results match pre-corruption state for tuples
  captured by the snapshot.

## Validation

### Performance Expectations

Snapshot latency: comparable to memory.db's `export` (~seconds
per 100k rows). Writer-impact during page-copy windows: <50ms
per step at default pages-per-step. Quantify before locking.

## Finalization Gate

- [x] **Memory / fd hygiene on snapshot loop**: each
  scheduled-snapshot iteration opens a fresh RO-URI
  connection to tuples.db (mirrors `_export_sqlite`),
  runs `sqlite3.Connection.backup(target, pages=1024)`,
  closes both source and target connections in a
  `try/finally` block. No long-lived conn beyond the
  per-iteration window. Loop teardown in
  `T2Daemon.stop_async` cancels the task and awaits
  cancellation cleanly.
- [x] **No new runtime deps**: uses stdlib (`sqlite3`,
  `asyncio`, `signal`, `os`, `pathlib`, `time`) +
  existing `click`. No new requirements in
  `pyproject.toml`.
- [x] **Deployment model**: single-host daemon mode only.
  Chroma is local. Backups land on the same filesystem
  as the source DB; operators are responsible for
  off-host copies (CLI returns a path the operator can
  `scp`).
- [x] **CI coverage**: Phase 1 test in
  `tests/daemon/test_introspection.py` (extend the
  existing `_export_sqlite` test suite). Phase 2 in
  new `tests/tuplespace/test_rebuild_chroma_index.py`
  (must include the C1 regression test from §Test
  Plan). Phase 3 in `tests/commands/test_daemon.py`
  (extend `stop_cmd` suite with `--force` cases).
  Phase 4 in `tests/daemon/test_t2_daemon.py` (scheduled
  loop). Phase 5 is doc-only.
- [x] **Operator ergonomics**: the recovery doc is
  written for a `nx daemon t2 doctor`-aware operator;
  the doctor output should surface backup-state info
  (last snapshot age, count of snapshots in rotation,
  estimated max-safe-data-loss window). Cross-link
  doctor → recovery doc when corruption is detected.
- [x] **Cross-RDR conformance**:
  - RDR-110 §CA-1 (atomicity): preserved — the snapshot
    is read-only via RO-URI; the rebuild path goes
    through `TupleIndex.out` which uses Chroma upsert
    (atomic per-tuple).
  - RDR-110 §CA-2 (watcher resumability): preserved —
    `watcher_state` is in the snapshot; restored cursors
    pick up at the snapshotted rowid.
  - RDR-110 §CA-5 (cross-process visibility): preserved
    — restore happens with the daemon STOPPED; on next
    start every client re-discovers via the discovery
    file (rebuilt at start).
  - RDR-111 (binding watcher cursors): cursors are
    persisted in `watcher_state`; restore + start
    resumes cleanly. Event-replay storm prevented by
    `last_events_rowid` cursor.
  - RDR-112 §9 (single-writer): preserved — backup
    opens a separate RO conn; rebuild runs against the
    daemon's writer through normal `TupleIndex.out`;
    no parallel writers introduced.
- [x] **Test execution evidence**: tests added in the
  files listed above will be run under the standard
  `uv run pytest` gate; the operator end-to-end
  scenario goes under `tests/e2e/` (integration suite,
  excluded from default CI per project convention but
  runnable manually).
- [x] **Secret / credential lifecycle**: N/A — backup
  files are SQLite databases on the local filesystem.
  No credentials material touched.

## References

- nexus-6m9i umbrella (third 360° remediation)
- Third 360° agent scratch entry `1053fcd6` (failure
  recovery)
- T2 research entries: `nexus_rdr/117-research-1` (A1
  verified — WAL backup API + shipped memory.db
  precedent), `117-research-2` (A2 verified with
  retention refinement), `117-research-3` (A3 verified
  with consumed_at filter + Voyage model caveat),
  `117-research-4` (Gap 3 resolved as --force flag on
  existing stop)
- Existing precedents: `_backup_sqlite_db` in
  `src/nexus/db/migrations.py:4024`; `_export_sqlite` in
  `src/nexus/daemon/introspection.py:498`; `install_cmd`
  `--force` flag in `src/nexus/commands/daemon.py:907`;
  `prune_old_events` in
  `src/nexus/tuplespace/store.py:414`
- `docs/operations/migration-recovery.md` (memory.db
  precedent for the new tuplespace-recovery.md)
- SQLite Online Backup API documentation
- RDR-110 (tuple-space durability contracts)
- RDR-112 (daemon as single writer)

## Revision History

### 2026-05-17 — Round 1 research

- All three Critical Assumptions resolved:
  - A1 (WAL Online Backup API): Documented → **Verified**
    via two shipped memory.db production paths
    (`_backup_sqlite_db` + `_export_sqlite`).
  - A2 (events append-only): Unverified → **Verified
    with refinement**. Rows immutable post-insert; log
    is bounded by 7-day `prune_old_events` retention.
    Recovery doc must document max-safe-data-loss
    window = min(snapshot_age, retention_window).
  - A3 (chroma rebuild from tuples): Unverified →
    **Verified with refinement**. All required fields
    persisted on the `tuples` row. Rebuild loop MUST
    filter `WHERE consumed_at IS NULL` to exclude
    tombstones. Voyage model version drift caveat
    documented.
- Open design question (force-stop scope): **Resolved**
  as `--force` flag on existing `stop` command, CLI-side
  only. No daemon code changes. Precedent: `install_cmd`.
- §Approach expanded to 5 deliverables (added explicit
  chroma-rebuild helper as Phase 5 of original promoted
  to its own deliverable; force-stop reframed as flag
  not verb).
- §Technical Design rewritten with concrete signatures
  for snapshot RPC `db` discriminator, rebuild helper,
  force-stop CLI sequence, and scheduled snapshot loop.
- §Implementation Plan re-sequenced: snapshot RPC →
  rebuild helper → force-stop → scheduled loop →
  recovery doc.
- §References gained citation block for the 4 research
  entries + 4 production precedents.

### 2026-05-17 — Round 1 gate (BLOCKED) + single-pass remediation

Gate result `nexus_rdr/117-gate-latest` (initial): BLOCKED,
1 critical, 5 significant, 0 observations. Findings
remediated in-place; ready for Round 2 gate.

- **C1 (rebuild helper silently invisible to all
  semantic queries)**: rebuild snippet passed
  `metadata=dims` where `dims` came from
  `dimensions_json` column which does NOT include the
  `subspace` key. `_merge_where` in
  `tuplespace/index.py:364-367` unconditionally
  injects `{"subspace": {"$eq": ...}}` into every
  `in_()` query — rebuilt tuples missing the subspace
  key are silently invisible. Verified against the
  production `out()` call site at
  `tuplespace/api.py:437-440` which builds
  `meta = {"subspace": subspace}` then merges
  dimensions with primitive coercion. Fix: rebuild
  loop now constructs `meta` the same way + applies
  the same primitive coercion + includes inline
  comment cross-referencing the docstring and
  `_merge_where` so the next maintainer cannot miss
  it. Added §Test Plan scenario "rebuild metadata
  correctness — C1 regression" that locks the fix.
- **S1 (Finalization Gate empty)**: populated with 7
  explicit sign-offs (memory/fd hygiene, no new deps,
  deployment model, CI coverage with specific test
  file targets, operator ergonomics, cross-RDR
  conformance against RDR-110/111/112, test execution
  evidence, secret lifecycle).
- **S2 (Decision Rationale was placeholder)**:
  expanded to three substantive paragraphs justifying:
  (a) Online Backup API over stop-and-cp (operational
  asymmetry; daemon-as-service model requires no
  scheduled downtime), (b) chroma rebuild from rows
  over chroma snapshot (avoids two-snapshot
  consistency invariant; vectors are deterministic
  functions of embed_text + model version), (c)
  `--force` flag on existing `stop` over separate
  `stop-force` verb (precedent: `install --force`;
  unified mental model; CLI-side only avoids code
  duplication).
- **S3 (max-safe-data-loss matrix missing)**: added a
  cross-product table to §Technical Design before
  §Recovery doc structure. Six rows covering steady
  state, recent rotation, snapshot expired, events
  expired, never-enabled-backups (events intact), and
  never-enabled-backups (events gone). Recovery doc
  §7 will reproduce this matrix verbatim and walk the
  operator through identifying their row.
- **S4 (force-stop PID-reuse failure mode)**: liveness
  probe sequence now mirrors `discovery.py:50-61`
  handling. `PermissionError` from `os.kill(pid, 0)`
  treated as "PID recycled under another UID" — break
  loop, skip SIGKILL, proceed to cleanup with a
  warning. `ProcessLookupError` continues to be
  "daemon dead, proceed to cleanup". Only the
  timeout-elapsed path (neither error raised) sends
  SIGKILL.
- **S5 (Test Plan missing 4 scenarios)**: added 5
  scenarios (the critic flagged 4; the C1 regression
  is a 5th derived from the critical fix). New
  scenarios: C1 metadata regression, rebuild race
  during concurrent `out()`, force-stop with PID
  reuse, force-stop during mid-snapshot, snapshot
  under concurrent memory.db migration, operator
  end-to-end recovery scripted CI test.

### 2026-05-17 — Round 2 gate (PASSED)

Round 2 confirmed all 6 Round 1 findings CLOSED. Critic
verified the C1 rebuild snippet matches the production
`api.py:437-440` call site exactly (seed
`{"subspace": subspace}`, merge dimensions with primitive
coercion), the Finalization Gate is concrete + testable,
the Decision Rationale runs three substantive paragraphs,
the max-safe-data-loss matrix arithmetic is internally
consistent (6h × 3 = 18h matches §Proposed Solution
defaults), and the force-stop sequence correctly mirrors
`discovery.py` PID-liveness handling.

One critic observation addressed in this pass: the
inline comment for the force-stop sequence now
explicitly notes that `PermissionError` disposition is
INVERTED relative to discovery.py (reader treats as
"live, try anyway"; force-stop treats as "recycled, do
NOT SIGKILL"). Removes the trap where a future
maintainer reads the "mirrors discovery.py:50-61"
note, looks up the reference, sees "treat as live",
and concludes the force-stop handling is wrong.

No new defects introduced by remediation. Outcome:
PASSED. Ready for `/nx:rdr-accept RDR-117`.
