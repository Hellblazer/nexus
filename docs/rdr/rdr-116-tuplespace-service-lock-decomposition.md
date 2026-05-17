---
title: "TuplespaceService lock decomposition: reader-conn pool + writer lock"
id: RDR-116
type: Architecture
status: draft
priority: medium
author: Hellblazer
reviewed-by: self
created: 2026-05-17
accepted_date:
related_issues: [nexus-6m9i]
---

# RDR-116: TuplespaceService lock decomposition: reader-conn pool + writer lock

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The third 360° review (umbrella `nexus-6m9i`, dimension-1
performance, scratch `4f981397`) escalated to CRITICAL the
single-`threading.Lock` design in
`src/nexus/daemon/tuplespace_service.py`. The lock serialises every
service method — `out`, `read`, `take`, `ack`, `nack`,
`list_active_claims`, `recent_events`, `subspace_stats`,
`blocking_take` — through a single connection. Under mixed
read/write load every reader queues behind any in-flight writer,
and every blocking_take's per-iteration `ts_api.take` re-acquires
the lock. Throughput cap was modelled at 70–300 ops/s regardless
of executor / CPU headroom.

The second 360° (dim-1 N2) flagged this as a NOTABLE performance
ceiling and deferred. The third 360° re-audit, with a perf-first
lens, escalated it to CRITICAL given the projected agent fan-out
load (multiple Claude subprocesses + cockpit panel queries + hook
bridge writes converging on a single daemon).

### Enumerated gaps to close

#### Gap 1: Reads serialise behind writes through a single shared connection

`TuplespaceService.read`, `.list_active_claims`, `.recent_events`,
`.subspace_stats` are read-only. They take `self._lock` only because
they share `self._conn` with the writer path. SQLite's WAL mode
supports unlimited concurrent readers; the project's
``chroma_quotas.py`` already documents
`MAX_CONCURRENT_READS = 10` per collection. The current lock
prevents readers from exploiting either.

#### Gap 2: blocking_take's per-iteration take() acquires the writer lock

`blocking_take` enters its poll loop and on each iteration calls
`ts_api.take(conn=self._conn, ...)` under `with self._lock:`. With
the post-CR-3 wake mechanism, this is up to 16 concurrent callers
each grabbing the lock for ~10 ms per iteration (chroma query +
SQL CAS). Aggregate writer-lock contention scales O(N callers ×
wake rate); legitimate writers (`out`) and reads queue behind.

#### Gap 3: blocking_take wake-event amplification across subspaces

The wake_event fires on EVERY commit (cross-subspace) so N
in-flight blocking_take callers in unrelated subspaces all wake
on each commit and all re-poll, multiplying contention. Already
acknowledged in `tuplespace_service.py:447-456` as a v1 trade-off
(`nexus-ku5k.1` follow-up); this RDR is the right home for the
per-subspace wake channel design.

## Context

### Background

Discovered through the third 360° perf-lens audit. Coupled with
INTEG C-3 (3 in-process writers on the same `tuples.db`: service,
binding-watcher derived-out, retention sweep) which shipped its
fix (PRAGMA busy_timeout=5000) under nexus-6m9i. The busy_timeout
masks the symptom under brief contention; this RDR addresses the
underlying lock granularity so legitimate workloads don't depend
on sleep-on-busy.

The substrate that makes a reader-pool feasible was put in place by:
- WAL mode on tuples.db (`open_tuples_db`)
- busy_timeout=5000 on every writer
- Chroma's documented per-collection read concurrency budget

### Technical Environment

- Python 3.12+. `threading.Lock` (non-reentrant); single
  `sqlite3.Connection` with `check_same_thread=False`.
- Daemon dispatches via `asyncio.run_in_executor(None, fn)`;
  default executor (post-third-360°) sized to
  `_BLOCKING_TAKE_MAX_CONCURRENT + 32 = 48` workers.
- HR-1 caps in-flight blocking_take callers at 16; per-claimant
  cap (SEC-4) caps at 4 per claimant.

## Research Findings

### Investigation

Round 1 research complete (2026-05-17). Full evidence in T2:
`nexus_rdr/116-research-1` through `116-research-6`. Sources
consulted:

- `src/nexus/daemon/tuplespace_service.py` — every method
  classified as reader-pool / writer-lock / both (full table in
  `nexus_rdr/116-research-5`).
- `src/nexus/tuplespace/api.py` — confirmed read-only surface
  (`read`, `subspace_stats`) vs write surface (`out`, `take`
  CAS, `ack`, `nack`).
- `src/nexus/daemon/event_stream.py` — TWO already-shipped
  production precedents for concurrent read connections against
  the daemon's writer (the wake watcher uses `mode=ro&uri=true`;
  the EventStream subscribe handler uses `PRAGMA query_only=ON`).
- `src/nexus/daemon/t2_client.py` — `_ConnectionPool` pattern
  (pop-on-checkout, return-on-success, close-on-exception)
  transfers directly to sqlite3 reader connections.
- SQLite WAL mode documentation — concurrent-reader semantics,
  `query_only` interaction, `PRAGMA busy_timeout` already
  applied (third 360° INTEG C-3).
- `src/nexus/db/chroma_quotas.py` — `MAX_CONCURRENT_READS = 10`
  baseline for pool sizing.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| SQLite WAL concurrent readers | Yes | Unbounded readers + single writer is the WAL guarantee; verified in production via the wake watcher (1ms cadence against the writer conn since nexus-z4m7) and EventStream subscribe path. No busy errors observed. |
| `sqlite3.connect("...?mode=ro&uri=true")` | Yes | The wake watcher uses this exact form. The reader pool should standardise on it (stronger than `PRAGMA query_only=ON` which can be flipped at runtime). |
| `_ConnectionPool` lease pattern (t2_client.py) | Yes | Pop / yield / checkin-on-success / close-on-exception. Idiomatic; transfers cleanly. Nuance: sqlite3 connections carry per-connection state (row_factory, transactions) — pool must initialise `row_factory=sqlite3.Row` at create time and discard on exception. |
| Per-commit subspace identification | Yes | `SELECT DISTINCT subspace FROM events WHERE rowid > ?` is a rowid-PK scan, no join, cheap. Watcher tracks `last_events_rowid` alongside `last_version`. Resolves Gap 3's open question. |
| `threading.Event` vs `threading.Condition` for fan-out | Yes | Bare Event has a race when N concurrent `blocking_take` callers each clear-then-wait. Condition (or Event + generation counter) is required so all waiters wake correctly on each commit. |

### Key Discoveries

- **Verified** (A1): WAL concurrent readers proven safe by two
  shipped production paths. The wake watcher opens
  `mode=ro&uri=true` and polls at 1ms cadence against the
  writer connection without any busy errors observed since
  nexus-z4m7. The EventStream subscribe handler uses
  `PRAGMA query_only=ON` (weaker form). Pool should
  standardise on `mode=ro&uri=true`. See
  `nexus_rdr/116-research-1`.
- **Verified** (A2): `_ConnectionPool` in `t2_client.py`
  provides the exact lease pattern. Transfers cleanly to
  sqlite3 with one nuance: pool connections must be
  initialised with `row_factory=sqlite3.Row` at create time
  and dropped on exception (not returned to the pool). See
  `nexus_rdr/116-research-2`.
- **Verified** (A3): Current daemon uses ~36-52 file
  descriptors (SQLite + ChromaDB + UDS/TCP sockets). Adding
  10 reader connections brings total to ~46-62 — well within
  the default 256 soft limit. Real constraint is executor
  threads (capped at 48), not fds. Pool exhaustion would
  surface as reader queueing, not fd starvation. See
  `nexus_rdr/116-research-3`.
- **Verified — with refinement** (A4): Lazy per-subspace
  `threading.Condition` allocation is safe (small objects,
  ref-count-bounded). The "can we identify subspace without
  a join?" open question is RESOLVED — the watcher executes
  `SELECT DISTINCT subspace FROM events WHERE rowid > ?` on
  each data_version bump. The events table already exists
  for EventStream; the watcher state gains a
  `last_events_rowid` cursor alongside `last_version`. See
  `nexus_rdr/116-research-4`.
- **Verified** (method classification): every TuplespaceService
  method classified by lock requirement in
  `nexus_rdr/116-research-5`. `blocking_take`'s inner CAS
  stays on the writer lock; only the OUTER framing (per-iter
  wait) benefits from the per-subspace channel. `take`'s CAS
  is writer-only. Phase 1 must NOT route `blocking_take`'s
  inner loop through the reader pool.

### Critical Assumptions

- [x] SQLite read-only connections opened with
  `mode=ro&uri=true` against the same WAL-mode database can
  run concurrently without triggering busy errors against
  the daemon's writer — **Status**: Verified — **Method**:
  Source Search + Production Evidence (two shipped
  precedents: wake watcher + EventStream subscribe).
- [x] A reader-conn pool can be safely shared across the
  daemon's executor threads (`check_same_thread=False`
  semantics + connection-per-execution pattern) —
  **Status**: Verified — **Method**: Source Search. Reuses
  `_ConnectionPool` lease pattern; nuance documented
  (row_factory init + discard-on-exception).
- [x] The reader-conn pool can be sized at 10 (matching
  `MAX_CONCURRENT_READS` from `chroma_quotas.py`) without
  exhausting the daemon's fd budget — **Status**: Verified —
  **Method**: Source Search. Current ~36-52 fds + 10 pool =
  ~46-62; well below 256 soft limit. Pool exhaustion surfaces
  as reader queueing, not fd starvation.
- [x] Per-subspace wake channels can be allocated lazily —
  **Status**: Verified (with design refinement) — **Method**:
  Source Search + Spike. **CORRECTION**: use
  `threading.Condition` (not `threading.Event` as the draft
  proposed) — bare Event has a multi-waiter clear-race that
  Condition handles correctly. Watcher gains a
  `last_events_rowid` cursor + `SELECT DISTINCT subspace
  FROM events WHERE rowid > ?` query per data_version bump
  (rowid-PK scan, no join, cheap).

## Proposed Solution

### Approach

Three coordinated changes (refined after Round 1 research):

1. **Reader-conn pool**: `TuplespaceService` owns a fixed-size
   pool of read-only `sqlite3.Connection`s opened with
   `?mode=ro&uri=true`. `.read`, `.list_active_claims`,
   `.recent_events`, `.subspace_stats` check out a reader,
   run, check back in. No lock needed — WAL allows concurrent
   readers. Two shipped production precedents already use
   this pattern (wake watcher + EventStream subscribe; see
   `nexus_rdr/116-research-1`).
2. **Writer lock isolated**: `self._lock` continues to protect
   `self._conn` (the writer). Only `out`, `take`, `ack`,
   `nack`, AND `blocking_take`'s per-iteration `ts_api.take`
   CAS acquire it. **Important** (Round 1 method classification
   in `nexus_rdr/116-research-5`): `blocking_take`'s inner loop
   does NOT move to the reader pool — its CAS is a writer
   operation. The Phase 1 win is that the four read-only
   methods stop queueing behind `blocking_take` iterations.
3. **Per-subspace wake channel** (addresses Gap 3): replace the
   single shared `_wake_event` with a
   `dict[str, threading.Condition]` keyed on subspace.
   **CORRECTION** from initial sketch: use
   `threading.Condition`, not `threading.Event`. Bare Event
   has a multi-waiter clear-race (each `blocking_take` caller
   clears the event before its CAS; concurrent waiters can
   miss a signal fired between clears). Condition's
   `notify_all` + `wait` pattern handles fan-out cleanly.
   The data_version watcher resolves the per-commit subspace
   via `SELECT DISTINCT subspace FROM events WHERE rowid > ?`
   per bump (rowid-PK scan, no join, cheap; see
   `nexus_rdr/116-research-4`).

### Technical Design

Locked-in design after Round 1 research:

- New `_ReaderConnPool` class in `tuplespace_service.py`
  analogous to `nexus.daemon.t2_client._ConnectionPool`.
  Lease semantics: `pop` on checkout, `append` on success,
  `close` on exception. Pool connections initialised with
  `row_factory = sqlite3.Row` at create time; never returned
  to the pool from an exception path (sqlite3 conn state
  carries open-transaction risk).
- `acquire_reader()` context manager on `TuplespaceService`
  mirroring `_ConnectionPool.acquire`.
- Writer path unchanged except `blocking_take`'s outer
  framing waits on `self._cond_for(subspace).wait(timeout=...)`
  instead of the shared `_wake_event.wait(...)`. The inner
  CAS still runs under `self._lock` (writer); only the
  WAIT is per-subspace.
- The data_version watcher's `_wake_watcher_loop` gains:
  - `last_events_rowid: int = 0` state field initialised
    on the first poll.
  - On each detected data_version bump: execute
    `SELECT DISTINCT subspace FROM events WHERE rowid > ?`
    with `last_events_rowid`; collect the changed-subspace
    set; advance `last_events_rowid` to the new max rowid;
    `notify_all` on the Condition for each changed subspace
    (lazily create on first reference).
  - Backward-compat: the shared `_wake_event` is retained
    for one release as a fan-out fallback for callers that
    don't know their target subspace yet (e.g. cross-subspace
    administrative tasks). Removed in the release AFTER all
    in-tree callers migrate to per-subspace wait.

**Performance target**: 5-10x improvement on mixed read-heavy
workloads where reads dominate writes; modest improvement on
write-heavy workloads (the writer lock is unchanged).
Quantify under a derived spike harness based on
`tests/tuplespace/spikes/test_ca_3_read_latency.py`.

### Decision Rationale

Substantive design — needs careful migration to preserve the
SQLite single-writer invariant and the per-claimant blocking_take
cap, while broadening read parallelism. Deferred from the third
360° remediation precisely because an inline fix risked breaking
the contracts shipped under RDR-110 / RDR-112.

## Alternatives Considered

### Alternative 1: Status quo + horizontal scaling

**Description**: Accept the 70–300 ops/s ceiling, document it,
recommend operators run multiple daemons (sharded by tuples.db
file or subspace prefix) when scale demands.

**Pros**:
- Zero code change.
- Matches what RDR-110 / RDR-112 baselines committed to.

**Cons**:
- Daemon-mode was sold as the single-writer convenience layer;
  asking operators to shard back into multiple instances negates
  the simplification.
- Multi-daemon coordination introduces new bugs (discovery,
  request routing, cross-shard reads).
- Doesn't help the v1 single-host agent-fanout case.

**Reason for rejection (provisional)**: contradicts the daemon-mode
v1 promise.

### Alternative 2: PRAGMA query_only on the read paths

**Description**: Keep one connection; flip `PRAGMA query_only=ON`
before reads, `OFF` before writes. Still under a single lock but
the boundary is now compiler-enforced.

**Pros**:
- No new pool/connection plumbing.
- Hardens against accidental writes from a method that thinks it's
  read-only.

**Cons**:
- Doesn't unlock read concurrency — still single-lock.
- Adds two extra PRAGMA round-trips per read.

**Reason for rejection (provisional)**: doesn't address Gap 1
(the perf concern); useful in addition to the pool design as a
read-path safety belt.

### Briefly Rejected

- **Async sqlite (aiosqlite)**: rejected — current synchronous
  design is well-understood; adding async sqlite breaks the
  service's existing call-site contracts.

## Trade-offs

### Risks and Mitigations

- **Risk**: per-subspace wake channels add dict-lookup overhead
  on every commit. Marginal but quantify under the existing
  CA-6 1ms-data_version-cost spike.
  **Mitigation**: benchmark; if regression, fall back to shared
  event for Phase 1 and ship the subspace channels as a Phase 2.
- **Risk**: reader-conn pool exhaustion under burst panel queries.
  **Mitigation**: cap at 10 (matching chroma quota); document the
  ceiling.
- **Risk**: regression in correctness — splitting reads from
  writes leaves one path that touches both (the `take` CAS reads
  candidates then writes the claim) requiring careful classification.
  **Mitigation**: any method that writes stays under `_lock` even
  if it also reads.

## Implementation Plan

Sequenced after Round 1 research:

- **Phase 1: reader-conn pool + read-only method classification
  (Gap 1)**.
  - Add `_ReaderConnPool` class mirroring `_ConnectionPool` in
    `t2_client.py`. Connections opened
    `sqlite3.connect(f"file:{path}?mode=ro&uri=true",
    uri=True, check_same_thread=False)` with
    `row_factory = sqlite3.Row` set at create time.
  - `acquire_reader()` context manager on
    `TuplespaceService`.
  - Migrate `.read`, `.list_active_claims`, `.recent_events`,
    `.subspace_stats` to acquire from the pool; remove their
    `self._lock` acquire.
  - **Do NOT touch** `blocking_take`'s inner CAS — stays on
    writer lock. The Phase 1 win is read methods no longer
    queue behind in-flight `blocking_take` iterations.
  - Spike: extend `tests/tuplespace/spikes/test_ca_3_read_latency.py`
    with a mixed-load arm (N readers + M writers).
- **Phase 2: per-subspace wake channel (Gap 3)**.
  - Add `self._cond_for_subspace: dict[str, threading.Condition]`
    + `self._cond_lookup_lock: threading.Lock`. Lazy creation
    keyed on subspace string.
  - Watcher loop gains `last_events_rowid` cursor + per-bump
    `SELECT DISTINCT subspace FROM events WHERE rowid > ?`
    query; `notify_all` on each changed subspace's Condition.
  - `blocking_take` poll loop's outer wait switches from
    `self._wake_event.wait(...)` to
    `with self._cond_for(subspace): self._cond_for(subspace).wait(timeout=...)`.
  - Existing `_wake_event` retained as fallback for one
    release (backward-compat for callers not yet migrated).
- **Phase 3: spike + remove the fallback**.
  - Mixed-load + cross-subspace wake-amplification spike
    measures: (a) read p50/p99 under 32 reader / 4 writer
    load; (b) blocking_take wake spurious-wake count with N
    callers across 2 subspaces and commits in only one.
  - Delete `_wake_event` after all in-tree callers migrate.
- **Phase 4 (deferred to follow-up)**: optional `PRAGMA
  query_only=ON` on the reader-pool conns as defense-in-depth
  (Alternative 2 in this RDR — safety belt, not perf).

## Test Plan

- **Scenario**: 32 concurrent reads + 4 concurrent writes —
  **Verify**: reads do not block on writes; aggregate throughput
  ≥ 4x current single-lock baseline.
- **Scenario**: blocking_take wake amplification — 8 callers in
  subspace A, 8 in subspace B; commits land in A only —
  **Verify**: B's callers do not see spurious wake re-polls.
- **Scenario**: writer lock invariant — concurrent `out` + `take`
  + `ack` — **Verify**: no claim-state corruption; existing
  CA-1 atomicity test passes unchanged.

## Validation

### Performance Expectations

5–10x improvement on read-heavy mixed workloads; modest improvement
on write-heavy. Measure under a new spike harness deriving from
existing `tests/tuplespace/spikes/test_ca_3_read_latency.py`.

## Finalization Gate

To be completed before `/nx:rdr-accept`.

## References

- nexus-6m9i umbrella (third 360° remediation)
- Third 360° agent scratch entry `4f981397` (performance)
- T2 research entries: `nexus_rdr/116-research-1` (A1
  verified — WAL concurrent readers + 2 production
  precedents), `116-research-2` (A2 verified —
  `_ConnectionPool` lease pattern transfer), `116-research-3`
  (A3 verified — fd budget), `116-research-4` (A4 verified
  with Condition correction + watcher subspace-tag query),
  `116-research-5` (method classification), `116-research-6`
  (design surprises + refinements)
- Existing precedents: `_wake_watcher_loop` in
  `tuplespace_service.py` (mode=ro&uri=true reader against
  the writer);
  `event_stream.subscribe` server-side handler
  (PRAGMA query_only=ON pattern)
- RDR-110 (semantic tuple space — defines the API surface
  this refactor preserves)
- RDR-112 (daemon — single-writer invariant this refactor
  must preserve)
- SQLite WAL mode documentation
- `src/nexus/db/chroma_quotas.py` (concurrent-reads sizing
  reference)

## Revision History

### 2026-05-17 — Round 1 research

- All four Critical Assumptions resolved:
  - A1 (WAL concurrent readers): Documented → **Verified**
    via two shipped production precedents.
  - A2 (reader-pool lease pattern): Unverified → **Verified**
    via `_ConnectionPool` source search; nuance noted on
    `row_factory` init.
  - A3 (fd budget): Documented → **Verified**; real
    constraint is executor threads, not fds.
  - A4 (per-subspace wake): Unverified → **Verified with
    correction**. Bare `threading.Event` had a multi-waiter
    clear-race; switched to `threading.Condition`. Watcher's
    subspace-tag derivation resolved via
    `SELECT DISTINCT subspace FROM events WHERE rowid > ?`
    (rowid-PK scan, cheap, no join).
- §Approach + §Technical Design rewritten to incorporate the
  Condition correction, the watcher's `last_events_rowid`
  cursor, and the explicit clarification that
  `blocking_take`'s inner CAS stays on the writer lock.
- §Implementation Plan re-sequenced: Phase 1 reader-pool +
  method migration; Phase 2 per-subspace Condition + watcher
  upgrade; Phase 3 spike + remove `_wake_event` fallback;
  Phase 4 optional `PRAGMA query_only` defense-in-depth.
- §References gained citation block for the 6 research
  entries + 2 production precedents.
