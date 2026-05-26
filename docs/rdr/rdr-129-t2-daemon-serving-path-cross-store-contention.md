---
title: "T2 Daemon Serving-Path Internal Cross-Store WAL Contention"
id: RDR-129
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-25
related_issues: [nexus-qi1zb]
related_rdrs: [RDR-128, RDR-120, RDR-063]
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-129: T2 Daemon Serving-Path Internal Cross-Store WAL Contention

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-128 made the T2 daemon the single **process** writer of `memory.db`
(verified at 5.1.0: `lsof` shows only the daemon opens the file; all other
writers route through it via `T2Client`). That closed the cross-process
contention that crash-looped the daemon across 5.0.2 to 5.0.4.

But the daemon is not internally single-writer. Per RDR-063 Phase 2 it runs
**one SQLite connection per domain store** (memory, plans, chash_index,
taxonomy, telemetry, document_aspects, aspect_queue, catalog). Those
connections contend with **each other** on SQLite's single WAL writer lock.
The RDR-128 5.1.0 live shakeout (2026-05-25) drove two simultaneous
full-repo indexes (nexus + a launchd Luciferase job), all routing their
chash / aspect / taxonomy writes through the one daemon. The daemon's own
`chash_index.upsert_many` dispatch hit `database is locked` ~11 times in
~10 minutes, exceeding the per-store `busy_timeout=5000`. The daemon did
NOT crash (this is not the RDR-128 crash-loop class); the failing writes are
the best-effort chash dual-writes (RDR-086), swallowed at debug and dropped.
`chash_index` is a rebuildable lookup cache, so the data is recoverable via
backfill, but catalog chash→(collection, doc_id) resolution is incomplete
until then.

`docs/architecture.md` claims `busy_timeout=5000` "absorbs the brief queue
so callers do not see `OperationalError: database is locked`." The shakeout
falsifies that under sustained multi-indexer load.

#### Gap 1: The serving dispatch has no busy_timeout/retry tolerance

The per-store connections use `busy_timeout=5000` and the daemon's
`_dispatch` path does not retry on `database is locked`. RDR-128 RF-3 added
`busy_timeout>=30000` + bounded retry to `bootstrap_schema` (the startup
migration) only, NOT to the regular serving dispatch. A cross-store
contention window >5s makes the serving op fail rather than wait.

#### Gap 2: Best-effort writes drop silently on contention

`chash_dual_write_batch_hook` swallows the daemon's
`T2ClientError('database is locked')` at debug level, so dropped chash rows
are invisible without log inspection. There is no metric/counter for
dropped best-effort writes, so the completeness gap is unobservable in
normal operation.

#### Gap 3: The daemon's per-store connections contend unmanaged

RDR-063 gave each store its own connection + its own `threading.Lock`, but
cross-store writes coordinate only via SQLite's file lock + `busy_timeout`.
There is no daemon-internal write serialization, so a long write on store A
(e.g. a taxonomy batch) can starve a write on store B (chash) past the
timeout.

## Context

Surfaced by the RDR-128 5.1.0 live shakeout (2026-05-25). Evidence:
`~/.config/nexus/logs/t2_daemon.log`, ~11 `t2_daemon_dispatch_failed
op='chash_index.upsert_many' error='database is locked'` entries between
`2026-05-26T02:55Z` and `03:05Z`, under two concurrent `nx index repo`
runs. `lsof` confirmed the daemon (pid 46412) was the sole `memory.db`
opener and no writer fell back to direct (`t2_index_write_daemon_unreachable_fallback`
= 0), so this is the daemon's internal cross-store contention, not a
cross-process regression. Bead: `nexus-qi1zb`. T2:
`nexus/rdr128-5.1.0-live-shakeout`.

This is the "next layer" past RDR-128: RDR-128 eliminated cross-process
contention; this is the residual within-daemon contention RDR-128 did not
scope.

## Research Findings (to verify before gate)

- **RF-1 (to verify):** the daemon serving connections use `busy_timeout=5000`
  and `_dispatch` has no `database is locked` retry. Confirm from
  `src/nexus/daemon/t2_daemon.py` + `src/nexus/db/t2/__init__.py` store
  connection setup.
- **RF-2 (to verify):** the failing op is the RDR-086 best-effort chash
  dual-write, swallowed by `chash_dual_write_batch_hook` (debug log, no
  metric). Confirm the drop is silent and recoverable via an existing chash
  backfill/repair path.
- **RF-3 (to verify):** the contention is daemon-internal (per-store
  connections), reproducible deterministically with N concurrent routed
  writers spanning >=2 stores against one daemon. Measure drop rate under
  single-indexer vs multi-indexer load to confirm it only fires under
  sustained multi-writer concurrency.

## Proposed Solution (draft — candidates, not yet chosen)

1. **Raise the serving connections' `busy_timeout`** (5000 -> 30000),
   matching the bootstrap path. Cheapest; absorbs longer contention windows.
2. **Add bounded lock-retry to the serving dispatch** on `database is
   locked`, mirroring `reclaim_stale` / RDR-128 RF-3. Makes transient
   contention a wait, not a drop.
3. **Serialize the daemon's own cross-store writes** behind a single
   internal write lock (one writer at a time within the daemon). Strongest;
   eliminates internal contention but reduces cross-store write parallelism.
4. **Meter dropped best-effort writes** so the gap is observable
   (complements any of the above).

Likely end state: (1) + (2) + (4). (3) is heavier and may be unnecessary if
(1) + (2) suffice.

## Trade-offs

Higher `busy_timeout` / retry adds latency under contention (callers wait
rather than fail fast). Internal serialization (3) reduces cross-store write
parallelism — the very thing RDR-063 Phase 2 introduced. Doing nothing:
best-effort chash writes drop under heavy concurrent indexing, leaving
catalog chash resolution incomplete until a backfill.

## Alternatives Considered

- **Accept as-is (do nothing).** Defensible short-term: only fires under
  sustained MULTI-indexer concurrency; normal single-indexer use likely
  never triggers it, and the dropped writes are recoverable. Tracked as P2
  `nexus-qi1zb`. This RDR exists so the architectural option is not lost.

## Validation (draft)

- Reproduce the contention deterministically (N concurrent routed writers
  across >=2 stores vs one daemon); confirm `database is locked` on the
  serving dispatch.
- After the fix: the same load produces zero dropped best-effort writes (or
  a bounded, metered, retried count), and `nx doctor` surfaces the drop
  metric.

## References

- RDR-128 (T2 Single-Writer Enforcement — closed the cross-process gap; this
  is the within-daemon residual it did not scope)
- RDR-120 (Storage Substrate Split — introduced the T2 daemon)
- RDR-063 (per-store connections — the source of the internal cross-store
  contention)
- Bead: `nexus-qi1zb`. T2: `nexus/rdr128-5.1.0-live-shakeout`.

## Revision History

- 2026-05-25: Created (draft). Surfaced by the RDR-128 5.1.0 live shakeout;
  captured so the within-daemon contention option is not lost. Low urgency
  (multi-indexer-only); needs RF verification + gate before accept.
