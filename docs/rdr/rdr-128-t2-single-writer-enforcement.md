---
title: "T2 Single-Writer Enforcement: One Owner for memory.db, or an Enforced Lock Discipline"
id: RDR-128
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-25
accepted_date:
related_issues: [nexus-kg8sj, nexus-v4m7y, nexus-aigkb, nexus-n8sbw, nexus-5ldk1]
related_rdrs: [RDR-105, RDR-120]
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-128: T2 Single-Writer Enforcement: One Owner for memory.db, or an Enforced Lock Discipline

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-120 introduced the T2 daemon on the premise that it is the **single writer**
of `memory.db` — one long-lived process owns the SQLite handles, and all other
consumers reach T2 through it via RPC. That invariant is asserted but **not
enforced**. In practice `memory.db` is a plain SQLite file that at least five
independent code paths open directly with their own connections. SQLite in WAL
mode permits many concurrent readers but exactly **one** writer; with multiple
direct writers, lock contention is structural rather than incidental, and it has
surfaced as a string of daemon incidents patched one symptom at a time across
three consecutive patch releases.

The releases 5.0.2, 5.0.3, and 5.0.4 each shipped a daemon "fix." None addressed
the contention itself. Within minutes of shipping 5.0.4, the daemon entered a
crash-loop on `sqlite3.OperationalError: database is locked` during its startup
migration, because a post-commit-hook indexer held the WAL lock. This RDR exists
to stop the patch-per-incident cycle by fixing the root cause: enforce a single
writer, or replace the WAL free-for-all with a real cross-process lock discipline.

The problem decomposes into four gaps (each maps to a verified research finding):

#### Gap 1: The single-writer invariant is unenforced

RDR-120 declared the daemon the single writer, but `memory.db` has many direct
writers — verified 20 `epsilon-allow` `sqlite3.connect` sites and 53 direct
`T2Database(...)` constructions outside the daemon (RF-1). They contend on
SQLite's one WAL writer lock, so contention is structural. The keystone offender
is the indexer (`nx index repo`, nexus-kg8sj): frequent (every commit's
post-commit hook) and long-running.

#### Gap 2: Contention hardening is inconsistent across DB paths

v4m7y gave `reclaim_stale` a 30s `busy_timeout` + bounded retry, but the daemon's
startup migration (`bootstrap_schema` → `apply_pending`) still uses a 5s
`busy_timeout` and no lock-retry (RF-3). Under a sustained foreign lock the 5s
expires and the daemon crashes — the proximate cause of the 2026-05-25 crash-loop.

#### Gap 3: The lifecycle cycle has no DB-acquire interlock

The 5.0.4 version-aware `ensure-running` SIGTERMs a healthy daemon unconditionally
to cycle it (RF-4). Composed with Gap 2, it tears down a working daemon and the
respawn crashes on a held lock — converting "stale-but-running" into "no daemon."

#### Gap 4: The boundary lint is partial

`storage_boundary_lint` (already wired into `nx doctor --check-storage-boundary`)
flags raw `sqlite3.connect` but not direct `T2Database(...)` construction (RF-5).
Closing only the raw-connect surface pushes the bypass into the construction form.

## Context

The piecemeal history, all tracing to the same invariant:

- **5.0.2 / nexus-v4m7y**: `aspect_queue.reclaim_stale` raised "database is locked"
  under WAL contention. Fix: `busy_timeout` 5s→30s + 3-attempt retry on that one
  code path.
- **5.0.2 / nexus-aigkb**: orphan T1 chromadb servers leaked across sessions. Fix:
  sweep them at MCP startup. (The T1 sibling of the same disease — substrate
  process lifecycle is not owned by one authority.)
- **5.0.3 / nexus-n8sbw**: the daemon ran with stdout/stderr → DEVNULL and no log
  sink, so its deaths were undiagnosable. Fix: file logging + status pid-liveness.
  This was not the disease; it was that we were *blind* to the disease.
- **5.0.4 / nexus-5ldk1**: the daemon froze its code version at start, so an
  upgrade left a stale daemon. Fix: version-aware `ensure-running` that cycles a
  stale daemon.
- **OPEN / nexus-kg8sj** (deferred to 5.1.x): `nx index repo` bypasses the daemon
  and opens `memory.db` directly, holding the WAL lock. This is the keystone
  offender: frequent (fires from the post-commit hook on every commit) and
  long-running.

**The live incident that motivated this RDR (2026-05-25):** immediately after
the 5.0.4 publish, `nx daemon t2 ensure-running` (the new 5.0.4 primitive)
SIGTERM'd a healthy daemon to cycle it onto the new version; the respawn's startup
migration hit the indexer's WAL lock and crashed; ensure-running is one-shot, so
the daemon was left **down**. The 5.0.4 lifecycle fix, composed with kg8sj and an
un-retried startup-migration path, converted "stale-but-running daemon" into "no
daemon at all." We amplified the failure with the patch meant to fix lifecycle.

## Research Findings

**RF-1 — `memory.db` has 5+ direct accessors, not one.** Confirmed direct openers:
(a) the T2 daemon (serving + startup migration); (b) `nx index repo` (kg8sj); (c)
`nx upgrade` (explicit `epsilon-allow: nx upgrade chicken-and-egg substrate
bootstrap (cannot route through daemon)`); (d) the `aspect_worker` thread inside
every `nx-mcp` process (per the `daemon-restart-not-worker-fix` finding, the
worker opens its own short-lived SQLite connection); (e) `nx doctor` (multiple
`epsilon-allow` read-only diagnostic connections). The `epsilon-allow` comments
are themselves an audit trail of where the single-writer invariant was knowingly
broken. **Verified 2026-05-25 (codebase grep, develop @ 5.0.4): 20 `epsilon-allow`
sites and 53 direct `T2Database(...)` constructions outside the daemon and the
store implementations** — far more than five. The worst writers beyond the
indexer: `nx upgrade`, `nx repair plans`, `nx aspects` repair verbs, and ~10
operator/CLI paths in `enrich.py`, `operators/aspect_sql.py`, `mcp_infra.py`,
`merge_candidates.py`, and the `collection_*` modules.

**RF-2 — every daemon incident is a contention symptom.** v4m7y = writer-vs-writer
collision; kg8sj = the indexer-writer starves others; the 2026-05-25 crash-loop =
the indexer-writer starves the daemon's *startup-migration* writer; aigkb = the
unowned-lifecycle sibling. The pattern is not coincidence; it is the predictable
consequence of N writers on one WAL lock.

**RF-3 — contention hardening is inconsistent across paths. VERIFIED 2026-05-25.**
v4m7y added `busy_timeout`+retry to `reclaim_stale`, but the daemon's **startup
migration** (`apply_pending` via `T2Database.bootstrap_schema`) has no such
tolerance. Confirmed by reading the source: `bootstrap_schema`
(`db/t2/__init__.py`) opens its connection with `PRAGMA busy_timeout=5000` (5s)
and no lock-retry (`apply_pending`'s `MigrationRetry` handles migration-internal
signals, not SQLite `database is locked`), whereas `reclaim_stale`
(`aspect_extraction_queue.py:162`) uses `busy_timeout=30000` (30s) + bounded
retry. Under the observed 10+ minute indexer lock the 5s timeout expires and the
daemon crashes — matching the live traceback. **P0 spec:** give `bootstrap_schema`
`busy_timeout>=30000` and wrap `apply_pending` in a lock-retry, mirroring
`reclaim_stale`.

**RF-4 — the 5.0.4 lifecycle cycle has no safety interlock. VERIFIED 2026-05-25.**
Confirmed in `commands/daemon.py`: on version skew `ensure-running` calls
`os.kill(running_pid, SIGTERM)` **unconditionally**, waits ≤10s for the old daemon
to die, then falls through to cold spawn — with no precondition that a replacement
can acquire the DB lock. **RF-3 × RF-4 is the exact amplification mechanism of the
live incident:** the cycle tears down the healthy daemon (RF-4); the respawn's
5s-no-retry startup migration crashes on the indexer's lock (RF-3); `ensure-running`
is one-shot, so the daemon is left down. **P0 spec:** do not SIGTERM a healthy
daemon for a version-cycle unless the DB is confirmed acquirable (or make the cycle
atomic — only tear down once the replacement is confirmed startable).

**RF-5 — the boundary is already recognized and linted; this is a tightening
exercise.** `src/nexus/storage_boundary_lint.py` already flags direct
`sqlite3.connect` / T2 opens and requires a per-line `# epsilon-allow: <reason>`
(reason >= 8 chars); `nx doctor` surfaces violations. So RDR-128 introduces no new
concept — the single-writer boundary exists and is enforced-by-lint, but the
exemption list has proliferated (RF-1: 20 sites). This gives the cure a built-in
**metric** (the `epsilon-allow` count) and a built-in **gate** (the lint): route
the writers so their exemptions can be deleted, and reserve exemptions for the
genuinely-irreducible bootstrap cases (the `nx upgrade` chicken-and-egg) under an
explicit lock discipline. Acceptance = the exemption count reduced to that
documented-irreducible set.

**RF-1 and RF-5 VERIFIED 2026-05-25** (independent recount + reading the lint
source): counts reproduce exactly (20 / 53); `storage_boundary_lint.py` is wired
into `nx doctor --check-storage-boundary` (RDR-120 P0.A / nexus-7xxxg), which
emits a `storage_boundary_lint` structlog metric — so the acceptance gate and
metric already exist and can be baselined. **Refinement:** the lint keys on raw
`sqlite3.connect` only; it does NOT flag direct `T2Database(...)` construction
(the 53 sites). The implementation must therefore extend the banned-call set to
construction sites, or routing the raw connects merely pushes the bypass into the
`T2Database()` form. Second live contention incident recorded the same day: an
`nx memory put` (recording this very finding) failed with `database is locked`
because a post-commit `nx index repo` held the WAL lock, succeeding only on
retry — RDR-128's thesis demonstrated twice in one session.

## Proposed Solution

Enforce the single-writer invariant. Direction (to be refined in research/gate):

1. **Route the worst offenders' writes through the daemon.** Keystone: the
   indexer (kg8sj) writes T2 (catalog manifest, chash index, telemetry) via the
   daemon RPC instead of a direct connection. Then audit `nx upgrade` and the
   `aspect_worker` similarly.
2. **For paths that genuinely cannot route** (the `nx upgrade` chicken-and-egg:
   it migrates the schema the daemon needs before the daemon can own it), define
   and enforce a **cross-process lock discipline** — e.g. the daemon must be
   stopped for the migration window, or a documented advisory file-lock both the
   daemon and `nx upgrade` honor — replacing the implicit WAL free-for-all.
3. **Make every remaining direct DB path lock-tolerant**, starting with the
   daemon startup migration (the same `busy_timeout`+retry v4m7y gave
   `reclaim_stale`), so a transient lock causes a wait, not a crash.
4. **Add the lifecycle interlock**: `ensure-running` must not SIGTERM a healthy
   daemon to cycle it unless the replacement can acquire the DB lock first.

The first principle is "one writer." Where that is impossible, the second
principle is "explicit, enforced, documented lock discipline" — never the
current implicit contention.

## Alternatives Considered

- **A. Keep patching per-incident.** Rejected — this is the status quo that
  produced three releases of band-aids and a crash-loop. Each new code path that
  touches `memory.db` is a new contention source and a new patch.
- **B. Cross-process advisory lock only, no routing.** A shared advisory lock all
  writers honor, without funnelling writes through the daemon. Lighter than full
  routing; preserves the "many writers" topology but serializes them explicitly.
  Viable for paths that can't route (upgrade), insufficient alone for the hot path
  (indexer) where routing also buys batching + back-pressure.
- **C. Full single-writer routing (all writes via daemon RPC).** The cleanest
  realization of RDR-120's premise, highest implementation cost (every direct
  writer must gain an RPC path, including the bootstrap chicken-and-egg). Likely
  the end state; phase toward it.

## Trade-offs

Routing through the daemon adds RPC latency and a hard dependency on the daemon
being up for writes that today degrade to direct access. The mitigation is that
the daemon is *supposed* to be the authority anyway, and the 5.0.4 work already
makes it self-heal on install. The cost of NOT doing this is the demonstrated
patch-per-incident cycle plus user-facing daemon outages.

## Implementation Plan

Phased; P0 stops the bleeding, P1 removes the keystone, P2 closes the rest.

- **P0 (crash-loop stop, patch-shippable):** (a) startup migration gets
  `busy_timeout`+retry (RF-3); (b) `ensure-running` lifecycle interlock (RF-4).
  These are small, self-contained, and end the self-inflicted amplification. They
  remain mitigations, not the cure.
- **P1 (keystone, nexus-kg8sj):** route `nx index repo` T2 writes through the
  daemon. The single highest-value change; removes the most frequent, longest-held
  lock.
- **P2 (close the invariant):** audit `nx upgrade`, `aspect_worker`, `nx doctor`;
  route or place each under the enforced lock discipline; remove the
  `epsilon-allow` exemptions or convert them to documented, locked exceptions.

## Test Plan

- Reproduce the contention deterministically: hold a write lock on `memory.db`
  (simulated indexer) and assert the daemon startup *waits and succeeds* rather
  than crashing.
- Assert `ensure-running` does not tear down a healthy daemon when the DB lock
  is unavailable.
- Concurrency test: N simulated writers (indexer + upgrade + worker) against one
  daemon, assert no `database is locked` surfaces to a caller.
- Regression guard that no new code path opens `memory.db` directly without going
  through the routing/lock helper (lint or test over `epsilon-allow` sites).

## Validation

The disease is cured when a full release cycle (the kind that produced this RDR)
can run — many commits firing the post-commit indexer, an upgrade, a daemon
restart — without a single `database is locked` daemon incident, and with no new
daemon band-aid bead filed.

## Finalization Gate

Pending — to be run via `/conexus:rdr-gate` after research findings are verified.

## References

- RDR-120 (Storage Substrate Split — asserted single-writer; this RDR enforces it)
- RDR-105 (T1 sub-agent contract — sibling substrate-lifecycle discipline)
- Beads: nexus-kg8sj (keystone), nexus-v4m7y, nexus-aigkb, nexus-n8sbw, nexus-5ldk1
- Memory: `daemon-restart-not-worker-fix` (aspect_worker opens its own SQLite conn)

## Revision History

- 2026-05-25: Created (draft). Root-cause RDR motivated by the 5.0.4 post-publish
  daemon crash-loop and the three-patch band-aid pattern Hal flagged.
- 2026-05-25: RF-1 and RF-5 verified (20 epsilon-allow + 53 direct T2Database;
  storage_boundary_lint wired into `nx doctor --check-storage-boundary`). Added
  the construction-site lint-coverage gap and a second live contention incident.
  T2 findings: nexus_rdr/128-research-1, /128-research-1-verified.
- 2026-05-25: RF-3 and RF-4 verified from source (bootstrap_schema 5s/no-retry vs
  reclaim_stale 30s+retry; ensure-running unconditional SIGTERM). RF-3 × RF-4
  pinned as the exact amplification mechanism; P0 specs recorded on each. A third
  live `database is locked` contention hit while recording these findings. T2:
  nexus_rdr/128-research-2, /128-research-3. All outstanding RFs (1,3,4,5) now
  verified; RF-2 corroborated by the three live incidents + the RF-3 traceback.
