---
title: "TuplespaceService lock decomposition: reader-conn pool + writer lock"
id: RDR-116
type: Architecture
status: accepted
priority: medium
author: Hellblazer
reviewed-by: self
created: 2026-05-17
accepted_date: 2026-05-17
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

Locked-in design after Round 1 research.

#### Reader-conn pool

New `_ReaderConnPool` class in `tuplespace_service.py`
analogous to `nexus.daemon.t2_client._ConnectionPool`. Lease
semantics: `pop` on checkout, `append` on success, `close` on
exception. Pool connections initialised with
`row_factory = sqlite3.Row` at create time; never returned to
the pool from an exception path (sqlite3 conn state carries
open-transaction risk).

`acquire_reader()` is a context manager on `TuplespaceService`
mirroring `_ConnectionPool.acquire`. The four read-only
methods replace `with self._lock:` framing with a pool
acquisition AND replace `conn=self._conn` with the
pool-acquired conn — `ts_api.read(conn=self._conn)` without
the writer lock is a data race (the writer conn is not safe
for concurrent access).

**Pool exhaustion contract** (resolves Gap 1 failure mode):
- Pool is fixed-size 10 (matches
  `chroma_quotas.MAX_CONCURRENT_READS`). The fixed bound is
  intentional — growing the pool unbounded would defeat the
  whole point of bounding daemon resource use.
- `acquire_reader()` waits on an internal
  `threading.Semaphore(10)` with a 5-second timeout. On
  timeout, raises a typed `ReaderPoolExhausted` exception
  (in `nexus.daemon.errors`). The dispatcher maps this to an
  RPC `ServiceBusy` reply so callers can retry; the daemon
  itself does not block indefinitely.
- The exhaustion path is observable: `ping` returns the
  pool's `available` count alongside `active_handlers`
  (extending the third 360° OBS C-1 ping surface).
- Pool teardown: `TuplespaceService.close()` calls
  `self._reader_pool.close_all()` which drains the deque
  closing each connection. The writer `self._conn.close()`
  call is unchanged.

#### Writer lock + per-subspace wake channel

The writer path is unchanged except for `blocking_take`'s
outer-loop wake source. The inner CAS still acquires
`self._lock` and runs against `self._conn` — only the WAIT
between iterations becomes per-subspace.

Wake-channel state:
- `self._cond_for_subspace: dict[str, threading.Condition]`
- `self._cond_lookup_lock: threading.Lock` — guards dict
  lookup/insert ONLY. Never held while calling `notify_all`
  or `wait` on a Condition; `_cond_for(subspace)` releases
  this lock before returning.

`_cond_for(subspace)` pattern:

```python
def _cond_for(self, subspace: str) -> threading.Condition:
    with self._cond_lookup_lock:
        cond = self._cond_for_subspace.get(subspace)
        if cond is None:
            cond = threading.Condition()
            self._cond_for_subspace[subspace] = cond
    return cond
```

Notifier (inside `_wake_watcher_loop`, on each detected
data_version bump — reuses the watcher's existing
`mode=ro&uri=true` connection; does NOT open a new one):

```python
# self._ro_conn is the existing watcher conn opened in
# _wake_watcher_loop init at current line 251. Do NOT
# open a new connection per bump — that leaks fds at
# ~1ms cadence.
cur = self._ro_conn.execute(
    "SELECT DISTINCT subspace, MAX(rowid) "
    "FROM events WHERE rowid > ? GROUP BY subspace",
    (self._last_events_rowid,),
)
rows = cur.fetchall()
if not rows:
    continue
changed = [row[0] for row in rows]
# advance cursor to the new max rowid across all
# changed subspaces in this bump
self._last_events_rowid = max(row[1] for row in rows)
for subspace in changed:
    cond = self._cond_for(subspace)
    with cond:           # acquire Condition's internal lock
        cond.notify_all()  # safe under acquired lock
```

Waiter (`blocking_take` outer loop):

```python
# inside blocking_take's poll loop
cond = self._cond_for(subspace)
with cond:               # acquire Condition's internal lock
    cond.wait(timeout=remaining)
```

**Why threading.Condition not threading.Event**: a bare
`Event` race exists when N callers each clear-then-wait. If
a commit fires between two callers' clears, the second
caller misses the signal. Condition + `notify_all` makes
fan-out atomic: the Condition's internal lock serialises
acquire/notify/wait without TOCTOU.

**Lock-order discipline**: `_cond_lookup_lock` is held ONLY
inside `_cond_for`. It is never held across a `with cond:`
block. This is the invariant that prevents deadlock — a
nested acquisition of `_cond_lookup_lock` inside a
`Condition` block would create an inversion against a
notifier holding the Condition trying to take
`_cond_lookup_lock`.

#### Watcher cursor state

The data_version watcher's `_wake_watcher_loop` gains a
`last_events_rowid: int = 0` state field initialised on
loop start (`SELECT MAX(rowid) FROM events` against the
watcher's existing read-only connection). The SELECT
DISTINCT-with-MAX(rowid) query then runs against deltas
only — each poll touches O(rows-since-last-bump) rows of
the `events` rowid PK, not the whole table. The watcher's
existing `mode=ro&uri=true` connection (line 251 of
current `tuplespace_service.py`) is reused for the new
query; the notifier pseudocode above is illustrative of
the query shape, not the connection lifecycle. No
per-bump `sqlite3.connect` calls are added.

#### No backward-compat fallback (S3)

The initial sketch retained `_wake_event` "for one release
as a fan-out fallback for callers that don't know their
target subspace yet." Round-2 grep of the codebase confirms
every `blocking_take` call-site passes a concrete subspace
string; there are no cross-subspace `blocking_take` callers
in-tree. The fallback would be dead code on arrival. Phase 2
deletes `_wake_event` outright; the watcher's `notify_all`
on per-subspace channels replaces all wake fan-out.

**Performance target**: 5–10x improvement on mixed read-heavy
workloads where reads dominate writes; modest improvement on
write-heavy workloads (the writer lock is unchanged). The
baseline harness and metric definitions are specified in
§Validation.

### Decision Rationale

**Reader-conn pool over `PRAGMA query_only`-only**: Alternative
2 (flip `query_only=ON` per read) keeps the single-lock design
intact and only hardens against accidental writes from
read-classified methods. That is a safety belt, not a
throughput mechanism — reads still serialise on `self._lock`,
the 70–300 ops/s ceiling stays put, and the dim-1 N2 escalation
is unaddressed. The pool unlocks true read concurrency on WAL
mode (already proven safe by two shipped production precedents:
the wake watcher's 1ms-cadence `mode=ro&uri=true` polls and the
EventStream subscribe handler's `query_only` reads). The pool
and `query_only` are complementary — Phase 4 keeps the
`query_only` PRAGMA on pool conns as defense-in-depth.

**threading.Condition over Event + generation counter**: the
multi-waiter signal-loss race in bare `threading.Event` is
real (N callers each clear-then-wait; a `set` fired between
two callers' clears is lost to the second). The natural fix
is either Condition (semantically the right primitive — its
internal lock serialises notify and wait) or Event +
monotonic generation counter (waiter remembers gen, retries
if `event.is_set() and gen unchanged`). Generation counters
require manual TOCTOU management on every wait/notify pair;
Condition handles it for free. Condition is also the Python
documented idiom for fan-out wake — sticking with the
documented primitive reduces the surface for the next
maintainer to misread.

**Events-table cursor over a separate subspace-tag column**:
the watcher needs per-bump "which subspaces changed?". Two
shapes: (a) add a `subspace_tag` column to `tuples` or a
separate index table, denormalised at INSERT time; (b)
`SELECT DISTINCT subspace FROM events WHERE rowid > ?` against
the existing events table. Option (a) couples schema to wake
routing, requires a migration, and adds writer-side cost for
every `out`. Option (b) reuses the `events` table (already
maintained for EventStream subscribers since RDR-110), runs
as a rowid-PK delta scan (cheap), and adds nothing to the
write path. Watcher state grows by one int
(`last_events_rowid`). The events table is the right
source-of-truth because it is the only place that already
records every commit with subspace attribution.

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
- **Risk**: reader-conn pool exhaustion under burst panel queries
  triggering an executor deadlock (48 executor threads contending
  for 10 pool conns).
  **Mitigation**: bounded wait — `acquire_reader()` blocks on
  the pool semaphore for 5 seconds then raises
  `ReaderPoolExhausted`; the RPC dispatcher maps to a
  `ServiceBusy` reply so callers can retry. The daemon never
  blocks indefinitely on pool acquisition. Observability: the
  pool's `available` count is exposed on the `ping` admin RPC.
- **Risk**: regression in correctness — splitting reads from
  writes leaves one path that touches both (the `take` CAS reads
  candidates then writes the claim) requiring careful classification.
  **Mitigation**: any method that writes stays under `_lock` even
  if it also reads.

## Implementation Plan

Sequenced after Round 1 research and Round 2 critique.

- **Phase 0: baseline capture**.
  - Implement the mixed-load arm in
    `tests/tuplespace/spikes/test_ca_3_read_latency.py`
    BEFORE any pool work. Configuration: N=32 reader threads
    (each issuing `read(subspace=...)` in a tight loop),
    M=4 writer threads (each issuing `out(...)`), run for
    60 seconds against a freshly-initialised
    `TuplespaceService` with the CURRENT single-lock
    implementation.
  - Capture: read p50, read p99, write p50, write p99,
    aggregate ops/s. Record hardware (cpu model, core
    count, OS) in the spike output.
  - Persist baseline numbers to `nexus_rdr/116-baseline`
    in T2. Phase 1 gate checks against these numbers
    directly — no later phase may re-define "baseline".
- **Phase 1: reader-conn pool + read-only method classification
  (Gap 1)**.
  - Add `_ReaderConnPool` class mirroring `_ConnectionPool` in
    `t2_client.py`. Connections opened
    `sqlite3.connect(f"file:{path}?mode=ro&uri=true",
    uri=True, check_same_thread=False)` with
    `row_factory = sqlite3.Row` set at create time.
  - Internal `threading.Semaphore(10)` for the 5-second
    exhaustion wait; `ReaderPoolExhausted` exception in
    `nexus.daemon.errors`.
  - `acquire_reader()` context manager on
    `TuplespaceService`. Pool teardown in
    `TuplespaceService.close()`.
  - Migrate `.read`, `.list_active_claims`, `.recent_events`,
    `.subspace_stats` to acquire from the pool. Each call
    site must replace BOTH the `self._lock` framing AND
    the `conn=self._conn` argument with the pool conn.
  - Watcher loop's existing `mode=ro` connection upgraded to
    `mode=ro&uri=true` for parity with the pool string
    (current line 251 of `tuplespace_service.py`).
  - **Do NOT touch** `blocking_take`'s inner CAS — stays on
    writer lock.
  - **Gate**: rerun the Phase 0 mixed-load spike. Acceptance
    criterion: read throughput (ops/s) ≥ 4x baseline at
    N=32 readers + M=4 writers. Read p99 latency ≤ baseline.
    Write p50/p99 ≤ baseline + 10% noise. If the gate fails,
    diagnose before Phase 2.
- **Phase 2: per-subspace wake channel + drop `_wake_event`
  (Gap 3)**.
  - Add `self._cond_for_subspace: dict[str, threading.Condition]`
    + `self._cond_lookup_lock: threading.Lock`. Lazy creation
    keyed on subspace string. `_cond_for(subspace)` releases
    `_cond_lookup_lock` BEFORE returning (lock-order
    discipline per §Technical Design).
  - Watcher loop gains `last_events_rowid` cursor + per-bump
    `SELECT DISTINCT subspace FROM events WHERE rowid > ?`
    query; `notify_all` on each changed subspace's Condition
    (acquired via `with cond:`).
  - `blocking_take` poll loop's outer wait switches from
    `self._wake_event.wait(...)` to (matching the
    §Technical Design waiter pseudocode):
    ```python
    cond = self._cond_for(subspace)
    with cond:
        cond.wait(timeout=remaining)
    ```
  - **Delete `_wake_event`** in the same change — Round 2
    grep confirmed no in-tree cross-subspace callers; no
    backward-compat surface to preserve.
  - **Gate**: cross-subspace wake-amplification spike. 8
    `blocking_take` callers in subspace A; 8 in subspace B;
    commits land only in A for 30 seconds. Acceptance
    criterion: discriminate via `Condition.wait(timeout)`
    return value (True = notified, False = timed out).
    B's callers' notified-wake count == 0; A's callers'
    notified-wake count == commit count.
- **Phase 3 (deferred to follow-up)**: optional `PRAGMA
  query_only=ON` on the reader-pool conns as defense-in-depth
  (Alternative 2 in this RDR — safety belt, not perf).

- **Scenario**: 32 concurrent reads + 4 concurrent writes
  (Phase 0 baseline + Phase 1 gate) — **Verify**: reads do
  not block on writes; aggregate read throughput ≥ 4x
  baseline; read p99 ≤ baseline p99; write p50/p99 ≤
  baseline + 10%.
- **Scenario**: reader-pool exhaustion under burst —
  spawn 20 simultaneous reader tasks against pool size
  10 — **Verify**: first 10 acquire immediately; remaining
  10 either acquire within the 5s window OR raise
  `ReaderPoolExhausted` (no thread hangs indefinitely;
  no executor deadlock).
- **Scenario**: blocking_take wake amplification (Phase 2
  gate) — 8 callers in subspace A, 8 in subspace B;
  commits land in A only for 30 seconds —
  **Verify**: discriminate notify-wakes from
  timeout-wakes via `Condition.wait(timeout)`'s return
  value (`True` = notified, `False` = timed out). B's
  callers' notified-wake count == 0; A's callers'
  notified-wake count == commit count (per caller, since
  `notify_all` wakes all waiters).
- **Scenario**: Condition lost-signal regression — 1
  notifier thread issues N `notify_all` calls against a
  single subspace's Condition, with a 5ms sleep between
  notifies; 4 waiter threads each call `wait(timeout=1.0)`
  in a loop and append to per-thread deques on each
  notified-return. **Verify**: each waiter's deque length
  ≥ ceil(N × 0.9) (allows 10% coalescing tolerance for
  scheduling overlap). The deliberate inter-notify sleep
  prevents the level-triggered coalescing that
  `Condition.wait` is allowed to perform, so the
  expected per-waiter wake count matches notify count
  within tolerance. The test rules out the bare-Event
  race that motivated the Condition switch.
- **Scenario**: writer lock invariant — concurrent `out`
  + `take` + `ack` — **Verify**: no claim-state
  corruption; existing CA-1 atomicity test passes
  unchanged.
- **Scenario**: pool teardown — start a service, acquire
  3 reader conns, return them, call `close()` —
  **Verify**: all pool conns closed (no
  `ResourceWarning`); writer conn closed; no fd leak.

## Validation

### Performance Expectations

5–10x improvement on read-heavy mixed workloads (the dominant
agent-fanout case); modest improvement on write-heavy workloads
because the writer lock is unchanged.

### Baseline Definition (resolves S4)

The "baseline" referenced throughout this RDR is captured by
Phase 0 BEFORE Phase 1 changes ship. The baseline spike lives
in `tests/tuplespace/spikes/test_ca_3_read_latency.py`
(extended with a mixed-load arm) and runs the CURRENT
single-lock `TuplespaceService` against the following
configuration:

- **Workload**: 32 reader threads issuing
  `service.read(subspace=...)` in a tight loop; 4 writer
  threads issuing `service.out(...)` with distinct
  subspaces; 60-second run window.
- **Metrics**: read p50, read p99, write p50, write p99,
  aggregate ops/s. Captured per-thread then aggregated.
- **Hardware**: recorded in the spike's JSON output
  (`platform.processor()`, `os.cpu_count()`,
  `platform.system()`, `platform.release()`).
- **Persistence**: numbers stored to
  `nexus_rdr/116-baseline` in T2. Phase 1 acceptance
  compares directly against the persisted numbers — no
  later phase may re-define baseline.

**Acceptance thresholds** (also re-stated in Implementation
Plan Phase 1 Gate):
- Read throughput (ops/s): ≥ 4x baseline.
- Read p99 latency: ≤ baseline p99.
- Write p50/p99: ≤ baseline + 10% noise floor.

The 5–10x claim is a stretch target; the gate threshold is
the more conservative 4x. If Phase 1 hits the 4x floor but
not the 5–10x ceiling, that is a PASS — the optimisation
shipped per spec.

## Finalization Gate

- [x] **Memory management**: reader-pool connections are
  owned by `TuplespaceService` and torn down in
  `TuplespaceService.close()` via
  `self._reader_pool.close_all()` (drains the deque,
  closing each connection). Pool size is fixed at 10 so
  the daemon's resource footprint grows by a bounded
  amount on startup, never beyond that.
- [x] **Incremental adoption**: `NX_STORAGE_MODE=direct-file`
  callers bypass `TuplespaceService` entirely (they own
  their own SQLite connection per `TupleStore` instance)
  so this change is invisible to non-daemon users.
  Backward-compatible on the wire — RPC surface, request
  shapes, and reply shapes are unchanged. Only the
  in-daemon execution path changes.
- [x] **No schema migration required**: events table and
  watcher_state table are unchanged. `last_events_rowid`
  is in-memory watcher state initialised from
  `SELECT MAX(rowid) FROM events` on each daemon start.
- [x] **Versioning**: no public API change, no minor-bump
  required. Lands as a patch.
- [x] **Build / dependency**: no new dependencies. Uses
  stdlib `threading.Condition` and `threading.Semaphore`.
- [x] **Observability**: `ping` admin RPC extended with
  reader-pool `available` count (matches the third 360°
  OBS C-1 surface for `active_handlers`,
  `blocking_take_in_flight`, `wake_thread_alive`).
- [x] **Secret/credential lifecycle**: N/A — this RDR
  touches no credential material.
- [x] **Deployment model**: single-host daemon mode only;
  this RDR makes no claims about multi-daemon or
  distributed deployments.

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

### 2026-05-17 — Round 1 gate (BLOCKED) + single-pass remediation

Gate result `nexus_rdr/116-gate-latest` (initial): BLOCKED,
3 critical, 4 significant, 4 observations. Findings
remediated in-place; ready for Round 2 gate.

- **C1 (Decision Rationale was a one-sentence placeholder)**:
  §Decision Rationale expanded to three paragraphs
  justifying: (a) reader-pool over `PRAGMA query_only`-only,
  (b) `threading.Condition` over `Event + generation
  counter`, (c) events-table cursor over a separate
  subspace-tag column.
- **C2 (Condition locking protocol underspecified — lost-
  signal risk)**: §Technical Design gained explicit
  pseudocode blocks for `_cond_for`, the notifier (watcher
  loop), and the waiter (`blocking_take` outer loop). The
  lock-order discipline is stated outright: 
  `_cond_lookup_lock` is held ONLY inside `_cond_for`,
  never across a `with cond:` block. `notify_all` is
  called only under the Condition's own internal lock
  (`with cond: cond.notify_all()`).
- **C3 (Phase 3 conflated spike measurement with
  destructive deletion)**: Implementation Plan rewritten.
  Phase 0 captures baseline BEFORE any pool work. Phase 1
  gate compares against the persisted baseline. Phase 2
  combines wake-channel switch + `_wake_event` deletion
  (justified by S3). Old "Phase 4 deferred" PRAGMA work
  renumbered to Phase 3 (still deferred).
- **S1 (Finalization Gate was empty)**: §Finalization Gate
  populated with explicit memory mgmt, incremental
  adoption, no-schema-migration, versioning, build/dep,
  observability, secret lifecycle, deployment-model
  sign-offs.
- **S2 (pool exhaustion contract not specified)**:
  §Technical Design pool block now specifies bounded
  semaphore wait (5s timeout → `ReaderPoolExhausted` →
  RPC `ServiceBusy`). §Risks/Mitigations rewritten.
  `ping` admin RPC extended with pool `available` count.
- **S3 (_wake_event fallback was dead code on arrival)**:
  §Technical Design "No backward-compat fallback" block
  added. §Implementation Plan Phase 2 deletes
  `_wake_event` in the same change as the Condition
  switch.
- **S4 (spike baseline undefined)**: §Validation gained
  explicit Baseline Definition section — workload,
  metrics, hardware capture, T2 persistence
  (`nexus_rdr/116-baseline`). Acceptance thresholds
  cross-referenced from Implementation Plan Phase 1
  Gate. Phase 0 captures baseline BEFORE Phase 1 changes
  ship.
- Test Plan extended with: reader-pool exhaustion under
  burst, Condition lost-signal regression, pool teardown
  scenarios.

### 2026-05-17 — Round 2 gate (PASSED after NS-1 fix)

Round 2 confirmed all 7 Round 1 findings CLOSED. One new
significant issue surfaced by the remediation; fixed in
the same pass:

- **NS-1 (notifier pseudocode contradicted prose)**: the
  notifier pseudocode opened a throwaway
  `sqlite3.connect(...)` per data_version bump and never
  closed it, while the Watcher cursor state prose
  correctly said "watcher's existing connection is
  reused." At ~1ms cadence this would have leaked a
  file descriptor per bump if the pseudocode were
  copy-pasted. Fixed: pseudocode rewritten to use
  `self._ro_conn` directly. Watcher cursor state prose
  updated to explicitly call out that the pseudocode
  illustrates query shape, not connection lifecycle.
  Query also collapsed to a single
  `SELECT DISTINCT subspace, MAX(rowid) FROM events
  WHERE rowid > ? GROUP BY subspace` to fetch the changed
  set and the new cursor in one round-trip.
- **NO1 (cosmetic — double `_cond_for` call)**:
  Implementation Plan Phase 2 waiter snippet now matches
  the §Technical Design waiter pseudocode
  (assign Condition to local, then `with cond: cond.wait`).
- **NO2 (Condition lost-signal test underspecified)**:
  test now specifies per-thread deques, a 5ms inter-notify
  sleep to prevent level-triggered coalescing, and a
  `≥ ceil(N × 0.9)` per-waiter wake-count assertion with
  documented coalescing tolerance.
- **NO3 (wake-amplification gate needed timeout
  discrimination)**: both the Test Plan scenario and
  Implementation Plan Phase 2 Gate now explicitly call out
  `Condition.wait(timeout)`'s return value (`True` =
  notified, `False` = timed out) as the discriminator.
