---
title: "T2 Daemon Write-Path Hardening: Guaranteed-Single-Daemon Enforcement and Contention-Free Internal Serialization"
id: RDR-129
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-25
accepted_date: 2026-05-27
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

#### Gap 1: (Layer A) the spawn lock can be bypassed across a version transition

`T2Daemon._acquire_spawn_lock` takes two `fcntl` `LOCK_EX | LOCK_NB` locks
(config-dir-scoped and db-path-scoped) and refuses to start a second
instance. That is the single-daemon guarantee. In the 5.1.5 prod shakeout
(2026-05-27) two `nx daemon t2 start` processes coexisted on the same
`memory.db` (canonical pid in the addr file plus a ~30-minute-old orphan),
producing transient `FTS5: database is locked` contention. The flock was
bypassed by a **release-before-exit** window (verified, RF-A1): the db-path
lock is version-stable (`<db_path>.spawn_lock`), so this is not a lock-path
mismatch; rather, `T2Daemon.stop()` calls `_release_spawn_lock()` while the
process is still draining, freeing the lock before the process exits. A
respawn during that window (notably `ensure-running` on version skew, which
SIGTERMs the old daemon then spawns a new one) acquires the freed lock and
runs alongside the still-draining predecessor. The flock is the right
mechanism for the steady state but the early release punches a hole in it.
Bead: `nexus-kwqhd` (root).

#### Gap 2: (Layer A) predecessor reap only covers the addr-file pid, not side-orphans

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

#### Gap 3: (Layer B) the serving dispatch has no busy_timeout/retry tolerance

The per-store connections use `busy_timeout=5000` and the daemon's
`_dispatch` path does not retry on `database is locked`. RDR-128 RF-3 added
`busy_timeout>=30000` plus bounded retry to `bootstrap_schema` (the startup
migration) only, not to the regular serving dispatch. A cross-store
contention window over 5s makes the serving op fail rather than wait.

#### Gap 4: (Layer B) best-effort writes drop silently on contention

`chash_dual_write_batch_hook` swallows the daemon's
`T2ClientError('database is locked')` at debug level, so dropped chash rows
are invisible without log inspection. There is no metric or counter for
dropped best-effort writes, so the completeness gap is unobservable in normal
operation, and `nx doctor` reports the transient lock as a hard failure
rather than a soft warning (bead `nexus-uq8a4`).

#### Gap 5: (Layer B) the daemon's per-store connections contend unmanaged

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

- **RF-A1: VERIFIED, with a correction.** The flock bypass is NOT a lock-path
  mismatch across versions. `_spawn_lock_path_for_db` (`t2_daemon.py:139`)
  anchors the db-path lock at `<db_path>.spawn_lock`, a sibling of the data
  file, version-stable and config-dir-independent. So a 5.1.1 and a 5.1.5
  daemon against the same `memory.db` contend on the *same* lock file. The
  actual bypass is **release-before-exit**: `T2Daemon.stop()` calls
  `_release_spawn_lock()` at `t2_daemon.py:611`, which unlocks the flock while
  the process is still shutting down (draining RPCs, closing connections).
  During that window the lock is free but the process is alive. A respawn, notably `ensure-running` on version skew, which SIGTERMs the old daemon then
  spawns a new one (the RDR-128 RF-4 path), acquires the freed lock and runs
  alongside the still-draining predecessor. The OS would otherwise hold a
  flock until process exit; the *early* release in `stop()` is the precise
  enabler. Correction to the draft: drop "make the lock version-stable" (it
  already is); the fix is purely defer-release-to-exit + an ensure-running
  interlock that waits for the predecessor's full exit before spawning.
- **RF-A2: VERIFIED.** `_reap_predecessor_daemon` reads `t2_discovery_path`
  (the addr file) and signals only that single pid; a side-orphan never named
  in the addr file is invisible to it. A full same-db enumeration is feasible
  but the cmdline `nx daemon t2 start` does not name the db, so scoping needs
  the db-path spawn-lock identity, an open-fd probe (`lsof` on `memory.db`),
  or an advertised `db_path`, RF for A1's sweep mechanism resolves to the
  open-fd probe or db-path lock identity.

Layer B:

- **RF-B1: VERIFIED.** Every serving store sets `PRAGMA busy_timeout=5000`
  (`chash_index.py:84`, `document_aspects.py:202`, `plan_library.py:224`,
  `catalog_taxonomy.py:242`, `telemetry.py:129`, `memory_store.py`,
  `db/t2/__init__.py:552`). Only the bootstrap migration got the 30s timeout +
  bounded retry (`db/t2/__init__.py:454-459`, RDR-128 RF-3). The serving
  `_dispatch` (`t2_daemon.py:708`) runs the store call via
  `asyncio.to_thread` under a generic `except Exception` with no
  `database is locked` retry, so a >5s contention window fails the op (logged
  `t2_daemon_dispatch_failed`).
- **RF-B2: VERIFIED.** `chash_dual_write_batch_hook` (`mcp_infra.py:506`)
  swallows the failure at `debug` level (`mcp_infra.py:544`,
  `chash_dual_write_batch_failed`) with no metric; the dropped row is a
  rebuildable `chash_index` cache entry, recoverable via a chash
  backfill/reconcile path (`ln-reconcile` / `chash_index` rebuild). (The
  "current without manual backfill" phrasing in the draft was attributed to
  the wrong line; the load-bearing fact is the silent debug-level swallow at
  `mcp_infra.py:544`, confirmed.)
- **RF-B3: VERIFIED.** Per RDR-063 each store has its own connection + its
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

A2. **Hold the spawn lock until the predecessor has fully exited, and make the
respawn wait on PID liveness, not the discovery file.** The db-path spawn lock
is already version-stable (RF-A1: `<db_path>.spawn_lock`), so the fix is not
"make it stable" but "stop releasing it early." Defer the release to process
exit: remove the early `_release_spawn_lock()` call in `stop()` (let the OS
drop the flock on exit) or hold an exit-scoped lock, so a respawn cannot
acquire the lock while the predecessor is alive.

This fix is **co-dependent with the `ensure-running` wait** and must not ship
without it. `stop()` unlinks the discovery file (`t2_daemon.py:601`) *before*
releasing the lock (`:611`), and `ensure-running`'s wait polls
`_daemon_is_alive()` (a discovery-file probe). So with defer-release alone:
the old daemon's discovery file disappears, `ensure-running` sees "no daemon"
and cold-spawns a new one, the new daemon's `_acquire_spawn_lock` hits EAGAIN
because the still-draining predecessor holds the flock, and the new process
exits, leaving **zero** daemons, the exact failure RDR-128 prevents. The
interlock therefore replaces the discovery-file poll with a **bounded PID-
liveness poll** (`os.kill(pid, 0)`, reusing the `_is_t2_daemon_process`
cmdline guard) on the prior pid: wait up to a bounded timeout for the
predecessor to fully exit before cold-spawning; if it is still alive at
timeout, abort the spawn and leave the stale-but-working daemon up (the
RDR-128 RF-4 "never trade a working daemon for none" principle). The Test Plan
covers the version-cycle-leaves-exactly-one case explicitly.

A3. **Fail loud on multiplicity.** If, despite A1/A2, more than one daemon is
ever observed for a db, `nx doctor` reports it as a hard error with the
offending pids, and the daemon logs it. The invariant becomes observable, not
silent.

### Layer B: the one daemon is internally single-writer

B1. **Raise the serving connections' `busy_timeout`** (5000 -> 30000),
matching the bootstrap path. Cheapest; absorbs longer contention windows. Two
prerequisites: (a) audit the `T2Client` per-call RPC timeout and raise it
commensurately (or confirm it already exceeds 30s) so a daemon-side 30s wait
does not surface to callers as an RPC-timeout error of a different type than
`database is locked`; (b) `_dispatch` runs each store call on
`asyncio.to_thread`, so a 30s blocking wait holds a pool thread for 30s, acceptable for one daemon, but under multi-daemon coexistence (Layer A still
unfixed pre-P2) N daemons each saturating their thread pool can stall RPC
dispatch. B1 therefore ships **paired with P2**, not as a standalone pre-P2
change (see Implementation Plan), so the longer wait never runs while two
daemons can coexist.

B2. **Add bounded lock-retry to the serving dispatch** on `database is
locked`, mirroring `reclaim_stale` / RDR-128 RF-3. Transient contention
becomes a wait, not a drop.

B3. **Serialize the daemon's own cross-store writes** behind a single
internal write lock (one writer at a time within the daemon), and consolidate
the upgrade-path writers (`nexus-izpcb`) to one in-process writer. Strongest;
eliminates internal contention at the cost of cross-store write parallelism.

B4. **Meter and surface dropped best-effort writes.** Count them; `nx doctor`
treats a transient FTS5 lock during an active write as a soft WARN (not a hard
fail) and reports the drop counter (`nexus-uq8a4`). The soft classification is
correct pre-P2 (the lock is expected under heavy concurrent indexing). It is
kept soft post-P2 as **intentional defense-in-depth**: after single-daemon
enforcement ships, an FTS5 lock on the serving path should be impossible in
steady state, so a B4 fire then *indicates a single-daemon invariant
violation* (two daemons, or a direct writer bypassing the daemon) and should
be investigated. A3 detects the violation on the daemon census (hard error);
B4 detects it on live-write lock contention (soft signal + counter), the two
are complementary, and B4 stays soft so the drop metric is never lost to a
hard fail. A future maintainer must not flip B4 to hard without understanding
this relationship.

Likely end state: A1 + A2 + A3 (hard enforcement) + B1 + B2 + B4 (tolerance +
observability), with B3 adopted only if B1+B2 prove insufficient under the
deterministic load test, since B3 trades away the cross-store parallelism
RDR-063 Phase 2 introduced.

## Implementation Plan

Phased under epic `nexus-70qc9`; ordered cheap-and-safe first, structural
last:

- **P1: observability + tolerance (low risk, immediate relief).** B2
  (serving-dispatch bounded retry) and B4 (meter drops + doctor soft-WARN,
  `nexus-uq8a4`). Makes transient contention a retried wait and a metric, not
  a silent drop. B1 (the 30s busy_timeout raise) is deliberately NOT here: a
  30s blocking wait under multi-daemon coexistence (Layer A still unfixed)
  risks thread-pool starvation, so B1 ships with P2 once exactly-one-daemon is
  guaranteed.
- **P2: hard single-daemon enforcement (+ B1).** A1 (full same-db sweep,
  generalising `nexus-exa2p`), A2 (defer lock release to exit + bounded
  PID-liveness ensure-running interlock; the lock is already version-stable,
  `nexus-kwqhd`), A3 (doctor multiplicity check), and B1 (raise serving
  busy_timeout, now safe because only one daemon can hold the WAL). Audit the
  `T2Client` RPC timeout as a B1 prerequisite.
- **P3: internal serialization (conditional).** B3 (internal write lock +
  upgrade-path writer consolidation, `nexus-izpcb`) only if the P2 load test
  still shows drops after B1+B2.
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
- Deferring lock release to process exit (A2) means the spawn lock is held for
  the whole of `stop()`. If `T2Database.close()` stalls (for example on a
  pending WAL checkpoint), the predecessor holds the lock open-ended and the
  PID-liveness interlock waits out its bounded timeout, then aborts the spawn
  (leaving the stale daemon up). The existing `_GRACEFUL_STOP_TIMEOUT` guards
  socket teardown, not DB close; A2 should add a bounded guard around the
  shutdown so a hung close cannot wedge restarts.
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

- **Layer A enforcement:** (1) the side-orphan sweep: manufacture a real
  lockless live "predecessor" plus a side daemon (both same-db) and assert a
  starting daemon reduces the system to exactly one. (2) The version-cycle
  no-daemon regression: drive the `stop()` (defer-release) -> `ensure-running`
  respawn sequence and assert it converges to exactly *one* daemon, never
  *zero* (the A2 PID-liveness-interlock case the critic flagged) and never
  two. (3) `nx doctor` multiplicity check fires when two daemons are forced.
  The sweep's enumeration mechanism (open-fd probe / db-path lock) is
  platform-sensitive: if `lsof`-based, cover the macOS case explicitly (its
  output format is not POSIX-standardized); on Linux `/proc/<pid>/fd` is the
  cheaper path.
- **Layer B contention:** reproduce the cross-store contention
  deterministically (N concurrent routed writers across >=2 stores vs one
  daemon); confirm `database is locked` on the serving dispatch pre-fix; after
  the fix, the same load produces zero *unrecovered* drops (writes that B2
  retries and succeeds are not drops; only un-retried, unmetered failures
  count). Audit that the `T2Client` RPC timeout exceeds the 30s serving
  busy_timeout so B1 does not convert a wait into an RPC-timeout error.
- Existing single-writer invariant tests
  (`tests/daemon/test_t2_daemon_startup_invariant.py`) extended for the sweep
  and the version-cycle case.

## Validation

Comprehensive closure: (1) under a version transition or a forced
double-spawn, the system always converges to exactly one daemon for the db
(never zero, never two; no manual cleanup needed); (2) `nx doctor` reports
daemon multiplicity as a hard error and the best-effort-drop counter; (3) the
deterministic multi-writer load test produces zero *unrecovered* drops
(B2-retried-and-succeeded writes do not count); (4) full unit suite green.

## Finalization Gate

**PASSED, 2026-05-27.** Layer 1 (structural): 5 digit-numbered gaps, all
sections present and non-empty. Layer 2 (assumption audit): all five RFs
VERIFIED against the daemon code with file:line evidence, no unevidenced
"Assumed" findings. Layer 3 (substantive-critic): 0 Critical, 3 Significant, 4
Observations, all resolved in-place before accept:

1. A2 ensure-running interlock under-specified (could leave *zero* daemons
   after a version cycle, since `stop()` unlinks the discovery file at
   `:601` before releasing the lock at `:611`). Resolved: A2 now specifies a
   bounded PID-liveness poll (not the discovery-file probe) and abort-if-alive
   per RDR-128 RF-4.
2. B1's 30s busy_timeout not audited vs `T2Client` timeout + pre-P2
   thread-pool starvation. Resolved: B1 audits the RPC timeout and ships
   paired with P2 (not standalone pre-P2); moved in the Implementation Plan.
3. B4 soft-WARN would mask single-daemon invariant violations post-P2.
   Resolved: B4 documents the intentional post-P2 defense-in-depth meaning and
   its complementarity with A3.

Observations resolved: RF-B2 line-citation corrected; Validation/Test-Plan
"silent drops" -> "unrecovered drops"; macOS `lsof` sweep-portability noted;
stop()-close-timeout trade-off added. Gate result in T2:
`nexus_rdr/129-gate-latest`.

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
- 2026-05-27: Gate PASSED (0 Critical, 3 Significant + 4 Observations resolved
  in-place). Significant: A2 PID-liveness interlock (avoid zero-daemon after a
  version cycle), B1 paired with P2 + T2Client-timeout audit (avoid pre-P2
  thread-pool starvation), B4 post-P2 invariant-violation semantics. See
  Finalization Gate. Awaiting accept.
