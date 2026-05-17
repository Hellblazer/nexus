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

To be expanded under `/nx:rdr-research`. Minimum reading:

- `src/nexus/daemon/tuplespace_service.py` — every method that
  touches `self._conn` under `self._lock`.
- `src/nexus/tuplespace/api.py` — distinguish which functions are
  read-only vs. write (`out`, `take`, `ack`, `nack`, `_consume_claim`
  etc.).
- `src/nexus/tuplespace/store.py` — schema, indexes, the contracts
  reads rely on.
- SQLite WAL mode docs — concurrent-reader semantics, write
  serialisation, checkpoint behaviour.
- `src/nexus/db/chroma_quotas.py` — concurrent-reads quota +
  validator (informs the reader-pool sizing).
- `src/nexus/daemon/event_stream.py` — its own read-only connection
  pattern (a precedent for separating concerns).

### Critical Assumptions

- [ ] SQLite read-only connections opened with `mode=ro` against
  the same WAL-mode database can run concurrently without
  triggering busy errors against the daemon's writer
  — **Status**: Documented — **Method**: Source Search
- [ ] A reader-conn pool of size N can be safely shared across
  the daemon's executor threads (`check_same_thread=False`
  semantics + connection-per-execution pattern) — **Status**:
  Unverified — **Method**: Spike
- [ ] The reader-conn pool can be sized at 10 (matching
  `MAX_CONCURRENT_READS` from `chroma_quotas.py`) without
  exhausting the daemon's fd budget — **Status**: Documented
  — **Method**: Docs Only
- [ ] Per-subspace `threading.Event` channels can be allocated
  lazily without bloating the steady-state memory footprint
  — **Status**: Unverified — **Method**: Spike

## Proposed Solution

### Approach

Three coordinated changes:

1. **Reader-conn pool**: `TuplespaceService` owns a fixed-size
   pool of read-only `sqlite3.Connection`s (opened with
   `mode=ro&uri=true`). `.read`, `.list_active_claims`,
   `.recent_events`, `.subspace_stats` check out a reader,
   run, check back in. No lock needed — WAL allows concurrent
   readers.
2. **Writer lock isolated**: `self._lock` continues to protect
   `self._conn` (the writer). Only `out`, `take`, `ack`, `nack`,
   and the per-iteration take in `blocking_take` acquire it.
3. **Per-subspace wake channel** (addresses Gap 3): replace the
   single shared `_wake_event` with a `dict[str, threading.Event]`
   keyed on subspace. The data_version watcher reads a
   subspace-tagged delta (or maintains its own subspace-aware
   poll). blocking_take callers wait on their subspace's event
   only.

### Technical Design

To be expanded. Initial sketch:

- New `_reader_pool: _ReaderConnPool` analogous to
  `nexus.daemon.t2_client._ConnectionPool` but holding
  read-only sqlite3 handles.
- `acquire_reader()` context manager mirroring the t2_client
  pool's lease semantics.
- Writer path unchanged except blocking_take loop now waits on
  `self._wake_event_for(subspace)` instead of the shared event.
- The data_version watcher gains a per-commit subspace tag (read
  from the last-committed event's row?) so it can fire only the
  relevant subspace's event. Open question: can we read the
  subspace efficiently without a join against `events`?

Performance target: 5–10x improvement on mixed read-heavy workloads
where reads dominate writes; modest improvement on write-heavy
workloads (the writer lock is unchanged).

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

To be expanded. High-level phases:

- Phase 1: reader-conn pool + read-only method classification
  (Gap 1).
- Phase 2: per-subspace wake channel (Gap 3); requires the watcher
  changes.
- Phase 3: blocking_take poll loop benefits from Phase 1+2 without
  itself changing.

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
- RDR-110 (semantic tuple space — defines the API surface this
  refactor preserves)
- RDR-112 (daemon — single-writer invariant this refactor must
  preserve)
- SQLite WAL mode documentation
- `src/nexus/db/chroma_quotas.py` (concurrent-reads sizing reference)

## Revision History

(Gate rounds will be appended here.)
