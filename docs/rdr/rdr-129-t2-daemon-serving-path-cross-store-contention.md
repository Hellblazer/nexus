---
title: "T2 Daemon Write-Path Hardening: Guaranteed-Single-Daemon Enforcement and Contention-Free Internal Serialization"
id: RDR-129
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-25
related_issues: [nexus-qi1zb, nexus-kwqhd, nexus-exa2p, nexus-izpcb, nexus-uq8a4, nexus-070e2]
related_rdrs: [RDR-128, RDR-120, RDR-063]
epic_bead: nexus-70qc9
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-129: T2 Daemon Write-Path Hardening: Guaranteed-Single-Daemon Enforcement and Contention-Free Internal Serialization

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The T2 daemon write path is supposed to be the calm centre of the storage
substrate: one process owns `memory.db`, every writer reaches it through the
`T2Client` RPC, and SQLite's single-writer WAL discipline is satisfied by
construction. Three releases of incident-patching (5.0.2 to 5.0.4), one
accepted RDR (RDR-128), and the 5.1.5 prod shakeout all say the same thing:
the write path is robust in pieces but not robust as a whole. We keep coming
back to it because each fix closed one layer and the next layer was never
scoped. This RDR scopes the whole write path so the closure is complete.

The write path has two failure layers, and a bulletproof daemon needs both
closed:

- **Layer A: enforcing that exactly one daemon exists.** RDR-128 made the
  daemon the single *process* writer by routing all consumers through it and
  flagging direct opens (the boundary lint). It assumed the spawn lock plus
  the addr file guarantee a single daemon. They do not, robustly: the
  5.1.5 upgrade left two daemons coexisting on one WAL.
- **Layer B: ensuring that one daemon never contends with itself.** RDR-063
  Phase 2 gave the daemon one SQLite connection per domain store. Those
  connections contend with each other on SQLite's one WAL writer lock, and
  under sustained multi-writer load the daemon's own best-effort writes are
  dropped (the original RDR-129 scope).

RDR-128's enforcement closed cross-*process* contention. This RDR closes the
two residual gaps RDR-128 did not scope: the single-daemon guarantee is
best-effort rather than hard (Layer A), and the one daemon is not internally
single-writer (Layer B).

### Layer A: single-daemon enforcement is layered-best-effort, not a hard invariant

#### Gap A1: the spawn lock can be bypassed across a version transition

`T2Daemon._acquire_spawn_lock` takes two `fcntl` `LOCK_EX | LOCK_NB` locks
(config-dir-scoped and db-path-scoped) and refuses to start a second
instance. That is the single-daemon guarantee. In the 5.1.5 prod shakeout
(2026-05-27) two `nx daemon t2 start` processes coexisted on the same
`memory.db` (canonical pid in the addr file plus a ~30-minute-old orphan),
producing transient `FTS5: database is locked` contention. The flock was
bypassed: a predecessor survived the 5.1.1 -> 5.1.4 transition without
holding the lock (released-but-alive, or an older lock-path scheme), so the
new daemon acquired the lock and ran alongside it. The flock is the right
mechanism for the steady state but does not survive version transitions or a
released-but-alive window. Bead: `nexus-kwqhd` (root).

#### Gap A2: predecessor reap only covers the addr-file pid, not side-orphans

The 5.1.5 mitigation (`nexus-070e2`) added `T2Daemon._reap_predecessor_daemon`:
on takeover, the new daemon reaps the live pid named in the addr file
(SIGTERM, escalating to SIGKILL, guarded by a `ps` cmdline check against PID
reuse). This closes the common takeover case but not the general one. A
side-orphan that started *after* the canonical daemon (so it was never the
addr-file pid) is not reaped. In the shakeout the orphan had to be killed by
hand. Bead: `nexus-exa2p`. The reap is a backstop; it is not the same as a
guaranteed "exactly one daemon for this db" invariant.

### Layer B: the one daemon is not internally single-writer

RDR-128 verified at 5.1.0 (via `lsof`) that the daemon is the sole *process*
opener of `memory.db`. But the daemon runs one SQLite connection per domain
store (memory, plans, chash_index, taxonomy, telemetry, document_aspects,
aspect_queue, catalog) per RDR-063 Phase 2, and those connections contend
with each other on SQLite's single WAL writer lock. The RDR-128 5.1.0 live
shakeout (2026-05-25) drove two simultaneous full-repo indexes (nexus plus a
launchd Luciferase job), all routing their chash / aspect / taxonomy writes
through the one daemon. The daemon's own `chash_index.upsert_many` dispatch
hit `database is locked` ~11 times in ~10 minutes, exceeding the per-store
`busy_timeout=5000`. The daemon did not crash (this is not the RDR-128
crash-loop class); the failing writes are the best-effort chash dual-writes
(RDR-086), swallowed at debug and dropped. The same `FTS5: database is
locked` flicker reappeared in the 5.1.5 shakeout under three concurrent
Claude sessions. `chash_index` is a rebuildable lookup cache, so the data is
recoverable via backfill, but catalog chash resolution is incomplete until
then.

`docs/architecture.md` claimed `busy_timeout=5000` "absorbs the brief queue
so callers do not see `OperationalError: database is locked`." Two shakeouts
falsify that under sustained concurrency.

#### Gap B1: the serving dispatch has no busy_timeout/retry tolerance

The per-store connections use `busy_timeout=5000` and the daemon's
`_dispatch` path does not retry on `database is locked`. RDR-128 RF-3 added
`busy_timeout>=30000` plus bounded retry to `bootstrap_schema` (the startup
migration) only, not to the regular serving dispatch. A cross-store
contention window over 5s makes the serving op fail rather than wait.

#### Gap B2: best-effort writes drop silently on contention

`chash_dual_write_batch_hook` swallows the daemon's
`T2ClientError('database is locked')` at debug level, so dropped chash rows
are invisible without log inspection. There is no metric or counter for
dropped best-effort writes, so the completeness gap is unobservable in normal
operation, and `nx doctor` reports the transient lock as a hard failure
rather than a soft warning (bead `nexus-uq8a4`).

#### Gap B3: the daemon's per-store connections contend unmanaged

RDR-063 gave each store its own connection plus its own `threading.Lock`, but
cross-store writes coordinate only via SQLite's file lock plus `busy_timeout`.
There is no daemon-internal write serialization, so a long write on store A
(for example a taxonomy batch) can starve a write on store B (chash) past the
timeout. A related consolidation gap lives in the upgrade path: `upgrade.py`
opens a separate `apply_pending` migration connection and a T3-steps
`T2Database`, two in-process writers that should be one (bead `nexus-izpcb`).

## Context

This is the third return to the daemon write path:

1. **5.0.2 to 5.0.4** patched daemon incidents one symptom at a time; 5.0.4
   crash-looped on `database is locked` during startup migration.
2. **RDR-128** (accepted + closed 2026-05-25) fixed the root of *that* class:
   route all writers through the daemon, harden the bootstrap migration
   (30s busy_timeout + retry), extend the boundary lint, add the RF-4
   ensure-running interlock caution. It eliminated cross-process contention.
3. **The 5.1.5 prod shakeout** (2026-05-27) surfaced the two residual layers:
   Layer A (two daemons coexisted; flock bypassed across the version
   transition; reap only caught the addr-file pid) and Layer B (the same
   internal `FTS5: database is locked` contention RDR-129's draft already
   described, now reproduced under three concurrent sessions).

The original RDR-129 (draft 2026-05-25) scoped only Layer B and was filed as
low-urgency "do not lose the option." The 5.1.5 shakeout showed Layer A is
the same problem one level up, and that patching it incident-by-incident
(the reap was one such patch) repeats the 5.0.2-5.0.4 pattern. This RDR is
widened to the comprehensive write-path treatment so the daemon's write path
is closed as a whole, not patched a layer at a time. Implementation is
tracked by epic `nexus-70qc9`.

Evidence: `~/.config/nexus/logs/t2_daemon.log` (both shakeouts);
`t2_daemon_dispatch_failed op='chash_index.upsert_many' error='database is
locked'` entries; the 5.1.5 two-daemon census (canonical pid + orphan, both
`nx daemon t2 start`, both parented by launchd). T2:
`nexus/rdr128-5.1.0-live-shakeout`. Beads: `nexus-kwqhd`, `nexus-exa2p`,
`nexus-qi1zb`, `nexus-izpcb`, `nexus-uq8a4`, `nexus-070e2` (reap, shipped
5.1.5).

## Research Findings (verified 2026-05-27 against the daemon code)

Layer A:

- **RF-A1 — VERIFIED, with a correction.** The flock bypass is NOT a lock-path
  mismatch across versions. `_spawn_lock_path_for_db` (`t2_daemon.py:139`)
  anchors the db-path lock at `<db_path>.spawn_lock`, a sibling of the data
  file — version-stable and config-dir-independent. So a 5.1.1 and a 5.1.5
  daemon against the same `memory.db` contend on the *same* lock file. The
  actual bypass is **release-before-exit**: `T2Daemon.stop()` calls
  `_release_spawn_lock()` at `t2_daemon.py:611`, which unlocks the flock while
  the process is still shutting down (draining RPCs, closing connections).
  During that window the lock is free but the process is alive. A respawn —
  notably `ensure-running` on version skew, which SIGTERMs the old daemon then
  spawns a new one (the RDR-128 RF-4 path) — acquires the freed lock and runs
  alongside the still-draining predecessor. The OS would otherwise hold a
  flock until process exit; the *early* release in `stop()` is the precise
  enabler. Correction to the draft: drop "make the lock version-stable" (it
  already is); the fix is purely defer-release-to-exit + an ensure-running
  interlock that waits for the predecessor's full exit before spawning.
- **RF-A2 — VERIFIED.** `_reap_predecessor_daemon` reads `t2_discovery_path`
  (the addr file) and signals only that single pid; a side-orphan never named
  in the addr file is invisible to it. A full same-db enumeration is feasible
  but the cmdline `nx daemon t2 start` does not name the db, so scoping needs
  the db-path spawn-lock identity, an open-fd probe (`lsof` on `memory.db`),
  or an advertised `db_path` — RF for A1's sweep mechanism resolves to the
  open-fd probe or db-path lock identity.

Layer B:

- **RF-B1 — VERIFIED.** Every serving store sets `PRAGMA busy_timeout=5000`
  (`chash_index.py:84`, `document_aspects.py:202`, `plan_library.py:224`,
  `catalog_taxonomy.py:242`, `telemetry.py:129`, `memory_store.py`,
  `db/t2/__init__.py:552`). Only the bootstrap migration got the 30s timeout +
  bounded retry (`db/t2/__init__.py:454-459`, RDR-128 RF-3). The serving
  `_dispatch` (`t2_daemon.py:708`) runs the store call via
  `asyncio.to_thread` under a generic `except Exception` with no
  `database is locked` retry, so a >5s contention window fails the op (logged
  `t2_daemon_dispatch_failed`).
- **RF-B2 — VERIFIED.** `chash_dual_write_batch_hook` (`mcp_infra.py:506`)
  swallows the failure at `debug` level (`mcp_infra.py:544`,
  `chash_dual_write_batch_failed`) with no metric; the inline comment
  (`mcp_infra.py:558`) confirms the row is "current without manual backfill",
  i.e. recoverable via a chash backfill/reconcile path (`ln-reconcile` /
  `chash_index` rebuild).
- **RF-B3 — VERIFIED.** Per RDR-063 each store has its own connection + its
  own `threading.Lock`; cross-store writes coordinate only via SQLite's file
  lock + `busy_timeout` (`db/t2/__init__.py:42-44` documents exactly this).
  There is no daemon-internal write serialization. Deterministic reproduction
  (N concurrent routed writers across >=2 stores vs one daemon) is a test-plan
  item; the shakeout drop evidence (~11 in ~10 min under two indexers)
  establishes it fires under sustained multi-writer load.

## Proposed Solution

The two layers are solved together so the write path is closed end to end.

### Layer A: a hard "exactly one daemon for this db" invariant

A1. **Sweep, do not just reap.** On startup, after acquiring the spawn lock,
enumerate *all* live processes that are t2 daemons for *this db path* (not
just the addr-file pid) and reap every one that is not self, before opening
`T2Database`. Scope to the db via the db-path spawn-lock identity / an
open-fd probe on `memory.db` / an advertised `db_path` in the process
metadata (RF-A2 decides the mechanism). This generalises `nexus-070e2` from
"reap the addr-file predecessor" to "guarantee single occupancy."

A2. **Hold the spawn lock until the predecessor has fully exited.** The
db-path spawn lock is already version-stable (RF-A1: `<db_path>.spawn_lock`),
so the fix is not "make it stable" but "stop releasing it early." Defer the
release to process exit: remove the early `_release_spawn_lock()` call in
`stop()` (let the OS drop the flock on exit) or hold an exit-scoped lock, so a
respawn cannot acquire the lock while the predecessor is alive. Close the
`ensure-running` RF-4 window in tandem: when it spawns a new daemon on version
skew, it must wait for the old one's full exit (not just its SIGTERM) before
spawning.

A3. **Fail loud on multiplicity.** If, despite A1/A2, more than one daemon is
ever observed for a db, `nx doctor` reports it as a hard error with the
offending pids, and the daemon logs it. The invariant becomes observable, not
silent.

### Layer B: the one daemon is internally single-writer

B1. **Raise the serving connections' `busy_timeout`** (5000 -> 30000),
matching the bootstrap path. Cheapest; absorbs longer contention windows.

B2. **Add bounded lock-retry to the serving dispatch** on `database is
locked`, mirroring `reclaim_stale` / RDR-128 RF-3. Transient contention
becomes a wait, not a drop.

B3. **Serialize the daemon's own cross-store writes** behind a single
internal write lock (one writer at a time within the daemon), and consolidate
the upgrade-path writers (`nexus-izpcb`) to one in-process writer. Strongest;
eliminates internal contention at the cost of cross-store write parallelism.

B4. **Meter and surface dropped best-effort writes.** Count them; `nx doctor`
treats a transient FTS5 lock during an active write as a soft WARN (not a hard
fail) and reports the drop counter (`nexus-uq8a4`).

Likely end state: A1 + A2 + A3 (hard enforcement) + B1 + B2 + B4 (tolerance +
observability), with B3 adopted only if B1+B2 prove insufficient under the
deterministic load test, since B3 trades away the cross-store parallelism
RDR-063 Phase 2 introduced.

## Implementation Plan

Phased under epic `nexus-70qc9`; ordered cheap-and-safe first, structural
last:

- **P1: observability + tolerance (low risk, immediate relief).** B1 (raise
  serving busy_timeout), B2 (serving-dispatch retry), B4 (meter drops + doctor
  soft-WARN, `nexus-uq8a4`). Makes contention a wait and a metric, not a
  silent drop.
- **P2: hard single-daemon enforcement.** A1 (full same-db sweep, generalising
  `nexus-exa2p`), A2 (version-stable lock + release-on-exit +
  ensure-running interlock, `nexus-kwqhd`), A3 (doctor multiplicity check).
- **P3: internal serialization (conditional).** B3 (internal write lock +
  upgrade-path writer consolidation, `nexus-izpcb`) only if the P1 load test
  still shows drops.
- **Phase gate** at each boundary: cross-walk §Validation against the closing
  beads.

## Trade-offs

- Higher `busy_timeout` / retry adds latency under contention (callers wait
  rather than fail fast).
- Internal serialization (B3) reduces cross-store write parallelism, the very
  thing RDR-063 Phase 2 introduced; hence it is conditional on B1+B2 being
  insufficient.
- A full same-db daemon sweep (A1) must be conservatively scoped (PID-reuse
  guard, correct db-path scoping) or it could kill an unrelated process or a
  legitimate daemon for a different db; the design care is in RF-A2.
- Doing nothing: best-effort chash writes drop under heavy concurrent
  indexing (recoverable by backfill), and a future version transition can
  again leave two daemons contending until manual cleanup.

## Alternatives Considered

- **Accept Layer B as-is (do nothing).** Defensible short-term: only fires
  under sustained multi-writer concurrency; dropped writes are recoverable.
  Rejected as the whole answer because the 5.1.5 shakeout shows it recurs and
  because Layer A (two daemons) is not recoverable without manual
  intervention.
- **Keep Layer A as incident-patches (reap-only).** This is what
  `nexus-070e2` did; the 5.1.5 side-orphan (`nexus-exa2p`) shows the patch is
  incomplete. Rejected: it repeats the 5.0.2-5.0.4 patch-per-incident cycle
  the comprehensive treatment exists to end.
- **One connection for the whole daemon (collapse RDR-063 Phase 2).**
  Eliminates internal contention by construction but discards per-store
  parallelism wholesale; B3's single internal write lock is the lighter
  version that keeps reads parallel.

## Test Plan

- **Layer A enforcement:** a test that manufactures the takeover and
  side-orphan conditions (a real lockless live "predecessor" plus a side
  daemon, both same-db) and asserts a starting daemon reduces the system to
  exactly one; a version-transition simulation (old lock scheme + new daemon)
  asserting no coexistence; `nx doctor` multiplicity check fires when two are
  forced.
- **Layer B contention:** reproduce the cross-store contention
  deterministically (N concurrent routed writers across >=2 stores vs one
  daemon); confirm `database is locked` on the serving dispatch pre-fix; after
  the fix, the same load produces zero dropped best-effort writes (or a
  bounded, metered, retried count).
- Existing single-writer invariant tests
  (`tests/daemon/test_t2_daemon_startup_invariant.py`) extended for the sweep.

## Validation

Comprehensive closure: (1) under a version transition or a forced
double-spawn, the system always converges to exactly one daemon for the db
(no manual cleanup needed); (2) `nx doctor` reports daemon multiplicity as a
hard error and the best-effort-drop counter; (3) the deterministic
multi-writer load test produces zero silent drops; (4) full unit suite green.

## References

- RDR-128 (T2 Single-Writer Enforcement; closed the cross-process gap; this
  RDR closes the single-daemon-enforcement residual it assumed and the
  within-daemon contention it did not scope)
- RDR-120 (Storage Substrate Split; introduced the T2 daemon)
- RDR-063 (per-store connections; the source of the internal cross-store
  contention)
- Epic `nexus-70qc9`. Beads: `nexus-kwqhd` (root enforcement), `nexus-exa2p`
  (side-orphan sweep), `nexus-qi1zb` (serving-path contention), `nexus-izpcb`
  (writer consolidation), `nexus-uq8a4` (doctor soft-WARN), `nexus-070e2`
  (reap, shipped 5.1.5). T2: `nexus/rdr128-5.1.0-live-shakeout`.

## Revision History

- 2026-05-25: Created (draft). Scoped Layer B only (within-daemon serving-path
  cross-store WAL contention, `nexus-qi1zb`). Filed low-urgency so the option
  was not lost.
- 2026-05-27: Widened to the comprehensive write-path treatment after the
  5.1.5 prod shakeout surfaced Layer A (two coexisting daemons: flock bypassed
  across the version transition, reap only caught the addr-file pid). Now
  covers both single-daemon enforcement (A1-A3, beads kwqhd/exa2p) and
  internal serialization (B1-B4, beads qi1zb/izpcb/uq8a4). Priority raised
  medium -> high. Implementation tracked by epic nexus-70qc9. Next: verify
  RFs against the daemon code, then gate.
- 2026-05-27: Research pass complete. All five RFs verified against the daemon
  code with file:line evidence. RF-A1 corrected: the db-path spawn lock is
  already version-stable (`t2_daemon.py:139`), so the bypass is not a
  path-mismatch but release-before-exit (`stop()` unlocks at
  `t2_daemon.py:611` while the process is still draining); A2 sharpened to
  defer-release-to-exit + ensure-running full-exit interlock. Gate-ready.
