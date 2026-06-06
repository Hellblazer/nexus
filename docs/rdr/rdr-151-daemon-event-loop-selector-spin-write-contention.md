---
title: "Daemon Event-Loop Selector Busy-Loop and Write-Lock Contention: Root-Cause Fix for the Recurring T2 Daemon 100% CPU Peg and Multi-Daemon database-is-locked Cascade"
id: RDR-151
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-05
accepted_date: 2026-06-05
related_issues: [nexus-xmohw, nexus-x47yx, nexus-00en9, nexus-hcw0g, nexus-whl8n]
related_rdrs: [RDR-128, RDR-129, RDR-140, RDR-141, RDR-146, RDR-149]
related_tests: []
implementation_notes: ""
---

# RDR-151: Daemon Event-Loop Selector Busy-Loop and Write-Lock Contention

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate the RDR.

> Grounded in a live forensic capture, not inference. Full evidence:
> `docs/postmortem/2026-06-05-daemon-concurrency-forensics.md`. Diagnostic
> workflow run `wf_b48bbf58-8b9`; T2 records
> `nexus/daemon-silent-peg-LIVE-CAPTURE-2026-06-05` and
> `nexus/daemon-concurrency-diagnosis-2026-06-05`.

## Problem Statement

The T2 daemon has pegged a CPU core at ~100% on a lone, singular daemon across
releases 5.10.1, 5.10.2, and 5.10.3. Three consecutive patches touched this
subsystem without resolving it, which is the repeated-patch trigger
(`feedback_root_cause_after_repeated_patches`): stop patching, root-cause.

On 2026-06-05 the peg was captured live, twice, with the macOS `sample`
profiler (py-spy requires root on macOS and was unavailable). The captures
settle the mechanism and refute the prior leading hypothesis (aggregate load
saturation from orphan MCP clients: there were zero production client orphans,
yet the daemon pegged with 5 to 7 open fds).

### Enumerated gaps to close

#### Gap 1 (RC-alpha, the peg): asyncio kqueue selector busy-loop from a leaked half-open accepted socket

Capture A (pid 82698): 3561 of 3565 samples (99.9%) in
`_PyEval_EvalFrameDefault -> select_kqueue_control_impl -> kevent`. Capture B
(pid 31605, the replacement daemon, re-pegged in ~4 minutes): the main
event-loop thread was 3079 of 3080 samples in the same `kevent` path.

Mechanism: the daemon accepts UNIX-domain connections whose client process then
vanishes (abrupt exit / RST, no clean FIN). The accepted socket reaches EOF but
its read fd is not removed from the kqueue, so the transport `_read_ready`
callback is rescheduled on every loop iteration. The event loop's ready queue
therefore never empties, so asyncio calls the selector with `timeout=0` every
iteration; `kevent(timeout=0)` returns instantly, producing a 100% CPU spin
with no useful work and no log output. `lsof` confirmed accepted UDS
connections accumulating on `t2.sock` (fds 31u, 33u, 35u, 37u) with no peer
process.

The application read loop is NOT the bug: `_handle_connection`
(`src/nexus/daemon/t2_daemon.py:1271-1304`) correctly breaks on
`asyncio.IncompleteReadError` and closes the writer in `finally`. The leak is
at the asyncio transport / fd-lifecycle layer, reached on the abrupt-death path
that the handler does not cover (see Gap 2).

#### Gap 2 (RC-epsilon, feeds Gap 1): `_handle_connection` does not catch `ConnectionResetError`

`t2_daemon.py:1274` catches `asyncio.IncompleteReadError` (graceful FIN) but not
`ConnectionResetError` / `OSError` (abrupt RST). An abrupt client death raises
out of the handler task rather than taking the orderly `break -> finally:
writer.close()` path, so the transport teardown is skipped and the half-open fd
is left registered. This is the most likely concrete entry into Gap 1.

#### Gap 3 (RC-2, the cascade trigger): `stop()` never marks the lease shutting-down

`T2Daemon.stop()` (`t2_daemon.py:1079`) closes the servers (`:1133-1136`) and
relinquishes the lease (`:1151`) but never calls
`ServiceRegistry.mark_shutting_down()`, which exists at
`service_registry.py:328` and is never invoked from the daemon. For the entire
drain the lease reads "live" (TTL 3.0s) while sockets are closing, so a
concurrent `ensure-running` resolves an apparently-live endpoint, fails to
connect, and counts it as a crash. This is the documented zero-daemon-window
and the nexus-x47yx cascade trigger.

#### Gap 4 (RC-3): `heartbeat_tick()` blocks the event-loop thread on a flock

`_reassert_discovery_loop` (`t2_daemon.py:990`) calls `heartbeat_tick()`
directly in the coroutine (not via `asyncio.to_thread`). The chain reaches a
blocking `fcntl.flock(LOCK_EX)` (`service_registry.py:200`, under `_elect`
`:284`). With `DEFAULT_TTL = 3.0` (`:64`), any flock stall beyond 3s freezes the
loop long enough for the daemon's own lease to age out, making a running daemon
look absent and triggering a spawn. This is an independent path into the same
cascade as Gap 3.

#### Gap 5 (RC-beta, the database-is-locked burn): intra-daemon SQLite write-lock contention

Capture B showed a thread-pool thread parked in
`sqlite3BtreeBeginTrans -> btreeInvokeBusyHandler -> sqlite3OsSleep` (a write
that could not acquire the WAL writer lock) while a second thread queued on the
store's Python rlock. It cleared within 3s (transient, not a deadlock).
`reclaim_stale` bounds its own retry (3 attempts, sleeps `(0.1, 0.5)`,
`aspect_extraction_queue.py:489-491`), but serve-path writes fall back on
SQLite's default busy handler, which spins in `OsSleep` rather than yielding.
This is the intra-daemon source of `T2ClientError(database is locked)`
(nexus-00en9), amplified when Gap 3 / Gap 4 let a second daemon coexist.

Lineage: this is the RDR-129 P3 / B3 deferral (nexus-izpcb, the single internal
write lock that would make serving genuinely contention-free), which RDR-129
shipped without (it shipped B1 raise busy_timeout 5000->30000 and B2 bounded
dispatch retry, but deferred B3). Gap 5 is that deferred item now
observationally confirmed firing in production, not a contradiction of RDR-129
as closed.

#### Gap 6 (RC-4): crash-loop sentinel read-modify-write is non-atomic on election-lock timeout

When `_acquire_election_lock` times out and returns `None`, `ensure-running`
proceeds unguarded (`commands/daemon.py:1040-1046`) into the crash-loop sentinel
read-modify-write (`:1123-1143`). `os.replace` is atomic per write but does not
serialize concurrent writers, so K racers lost-update the count. This is the
already-filed nexus-whl8n / RDR-140 P4 deferral; it degrades guard accuracy
under exactly the contention that Gaps 3 and 4 create.

## Context

### Background

This RDR is the root-cause follow-on the nexus-xmohw bead explicitly calls for.
RDR-128 and RDR-129 enforced single-writer and hardened the T2 write path;
RDR-140 added the supervisor election and crash-loop guard; RDR-141 split the
version-skew double-writer; RDR-149 unified the T1/T2/T3 lifecycle onto one
leased `ServiceRegistry`. None of these caught the asyncio transport fd leak
(Gap 1), because it lives below the lifecycle layer in the per-connection
transport handling. The 5.10.1/2/3 patches mitigated the reclaim flood and
bounded socket teardown but never addressed the selector spin.

### Technical Environment

- `src/nexus/daemon/t2_daemon.py`: asyncio server, `_handle_connection`
  (`:1271-1304`), `_reassert_discovery_loop` (`:968-1003`),
  `_reclaim_stale_loop` (`:1005-1058`), `stop()` (`:1079`),
  `_invoke_with_lock_retry` (`:1343-1377`).
- `src/nexus/daemon/service_registry.py`: `_elect` flock (`:188-204`),
  `heartbeat` (`:275-309`), `mark_shutting_down` (`:328`), `DEFAULT_TTL` (`:64`).
- `src/nexus/commands/daemon.py`: `ensure-running`, `_acquire_election_lock`
  (`:1040`), crash-loop guard (`:1123-1143`).
- `src/nexus/db/t2/aspect_extraction_queue.py`: `reclaim_stale` (`:489-491`).
- `src/nexus/db/t2/__init__.py`: `stored_schema_version` per-call connect
  (`:477`), `hello` (`:495`).

## Decision

> **SUPERSEDED IN PART (2026-06-05): see the RF-1 REFUTED correction under
> Research Findings.** "Gap 1" (a leaked accepted read fd as the peg cause) was
> refuted by a live capture; the peg is contention-driven (a wedged
> ``catalog_taxonomy`` write + a ``select()`` spin on the self-pipe/listen fd),
> which is the Gap-5 / P2.1 surface, not Gap 1/2. Gaps 3 and 4 (mark-shutting-down
> ordering, threaded heartbeat) shipped and stand as correct lifecycle hardening.
> The Phase-1 connection-handler change (Gap 1/2) is retained only as teardown
> hygiene. The peg root cause and its fix move to the contention work.

The persistent 100% CPU peg (Gap 1, with Gap 2 as its likely entry path) and the
cascade enablers (Gap 3, Gap 4) ship in **one milestone**, not in sequence. RF-5
showed the cascade is self-sustaining and actively degrading (the daemon was
replaced three times in minutes, each re-pegging within seconds), so fixing only
the peg would still leave the box churning daemons, and fixing only the cascade
would leave the core burning. They are one outage with one fix surface. To be
precise about vocabulary: the milestone is Phase 1 (one delivery containing Gaps
1-4); Phase 0 is a prerequisite reproduce-and-pin deliverable, not a separate
milestone and not a shippable fix.

Gap 5 (write busy-handler) and Gap 6 (crash-loop race) are in-scope correctness
hardening that may land in a following phase: they degrade behaviour under
contention but neither is the steady-state core burn.

A hard prerequisite gates everything: **Gap 1's exact mechanism is not yet known**
(RF-1 could not be pinned externally; py-spy and lldb both need root on macOS).
So the first deliverable is an instrumented reproducer that PINS the leak path
and serves as the regression test. No transport-layer change is written before
that harness reproduces the spin and identifies the responsible fd, otherwise we
would be shipping a fourth blind patch.

Directions per gap. Gap 1's direction is a HYPOTHESIS that Phase 0 must confirm
before any code is written; Gaps 3 and 4 are code-confirmed and locked:

1. **Gap 1 / Gap 2 (hypothesis, NOT locked — Phase 0 decides):** the goal is to
   make abrupt-death accepted sockets tear down and unregister their read fd from
   the selector. The leading hypothesis is Gap 2 (uncaught `ConnectionResetError`
   skips transport teardown) or the `eof_received()`-returns-True half-close path
   (RF-1 candidate b). BUT RF-1 explicitly warns that normal asyncio already
   removes the reader on EOF and force-closes on RST, and that two of the three
   candidates (RF-1 (a) self-pipe wakeup, (c) `call_soon` self-reschedule) are
   event-loop-scheduling faults that a per-handler exception catch would NOT fix.
   Therefore Phase 0 names the responsible fd and code path FIRST; only then is
   the fix chosen. If Gap 2 / candidate (b) is confirmed: catch
   `(ConnectionResetError, OSError)` beside `IncompleteReadError` in
   `_handle_connection` (`t2_daemon.py:1274`) and confirm the `finally` close
   unregisters the read fd. If candidate (a) or (c) is confirmed, the fix is the
   Phase 0 output and lives at the event-loop layer, not the handler. A defensive
   idle/read deadline on accepted connections is a belt-and-braces addition
   regardless of which candidate confirms; its size is a Phase 0 output (see Open
   Questions), bounded above the longest legitimate RPC.
2. **Gap 3:** call `self._registry.mark_shutting_down(self._lease_record)` as the
   first statement of `stop()` (`t2_daemon.py:1079`), before cancelling the
   reassert task or closing servers.
3. **Gap 4:** dispatch `heartbeat_tick()` via `asyncio.to_thread`
   (`t2_daemon.py:990`) so the blocking `flock` never runs on the event-loop
   thread.
4. **Gap 5:** replace serve-path reliance on SQLite's spinning default busy
   handler with a cooperative bounded retry+backoff (mirroring `reclaim_stale`),
   or a single serialized in-daemon writer. Following phase.
5. **Gap 6:** a dedicated `flock` around the crash-loop sentinel
   read-modify-write (`commands/daemon.py:1123-1143`), independent of the
   election lock. Following phase.

Operational acceptance bar (from this session): a SIGTERM `nx daemon t2 stop`
**failed to stop the pegged daemon** (it stayed at ~99% after the bounded stop
returned; only SIGKILL cleared it). The fix must restore the invariant that a
graceful stop terminates the daemon even under peak load within
`_GRACEFUL_STOP_TIMEOUT`.

## Revised Plan (2026-06-06, post-live-capture)

This section supersedes the Decision and the original Approach below, which were
built on the refuted RF-1 (see the REFUTED correction under Research Findings).
The mechanism was captured live and cross-validated by an independent codebase
deep-analysis and a research synthesis (T2:
`nexus/rdr151-LIVE-MECHANISM-CAPTURED-2026-06-05`,
`nexus/rdr151-p2.1-fix-design-synthesis`).

### Revised thesis

The 100% CPU peg is **contention-driven write starvation**, not a leaked
accepted fd. Four compounding causes:

1. **`taxonomy.*` write ops bypass the daemon's `_catalog_write_lock`.** The
   daemon serialises only `catalog_write.*` (`t2_daemon.py` `_dispatch`); taxonomy
   writes dispatch as `taxonomy.*`, so N concurrent RPCs each launch a parallel
   `asyncio.to_thread` writer racing for the one SQLite write lock.
2. **`assign_topic` uses DEFERRED transactions** (Python `sqlite3` default
   `isolation_level=''`), so a read→write upgrade returns `SQLITE_BUSY`
   *immediately, ignoring `busy_timeout`* — the instant `database is locked`.
3. **`busy_timeout=30000`** (`_tuning.py:19` `SERVING_BUSY_TIMEOUT_MS`) parks a
   wedged write's executor thread in C for 30 s → thread-pool exhaustion →
   cascade.
4. **Incomplete daemon-fronting:** `nx index` (direct-write fallback) and
   workers open their own write connections to the same DB.

The `kevent(timeout=0)` selector spin is a *consequence* (self-pipe /
listen-accept storm while an executor thread is wedged), not the cause. Live
capture: main thread in `_run_once → selector.select() → kevent`, `loop._ready`
empty, only self-pipe + listen fds registered, executor wedged in
`catalog_taxonomy._resync_topic_links_for → conn.execute`.

### Revised phases

- **Phase 0 (`nexus-8voni`) — DONE.** Mechanism pinned by live capture (the
  Phase-0 harness "reproduction" was write throughput, not the leak).
- **Phase 1 (P1.1–P1.4) — shipped as lifecycle/teardown HARDENING, NOT the peg
  fix.** Retained and correct: P1.2 idle deadline, P1.3 `mark_shutting_down`-first
  (+ T3, `nexus-6o4uj`), P1.4 `to_thread(heartbeat_tick)`, plus the review-found
  `ServiceRegistry.heartbeat` non-live-status preservation. P1.1
  (pause-reading/abort) is kept as teardown hygiene only.
- **Phase 2 — THE peg fix (contention), primary milestone (`nexus-gcu07`).**
  Ranked, file:line-anchored:
  - **2.1a (MVP, ~3 lines):** serialise `taxonomy.*` writes through the existing
    `_catalog_write_lock` in `t2_daemon._dispatch`.
  - **2.1b:** `BEGIN IMMEDIATE` on `assign_topic` write txns (set
    `isolation_level=None` first); audit other multi-step write sites in
    `catalog_taxonomy.py`.
  - **2.1c (conditional on soak):** replace the long in-C `busy_timeout` with a
    fast-fail to the cooperative Python async retry (`_invoke_with_lock_retry`).
  - Optional backstops: batch `persist_assignments` into one txn; bound the
    dispatch `ThreadPoolExecutor`. `nexus-1wpa4` (dedicated crash-loop flock)
    de-prioritised as cascade-hardening, not the peg fix.
- **Phase 3 — close incomplete daemon-fronting (`nexus-uzay8`).** Route the
  persist half through daemon RPC; enforce via `storage_boundary_lint` that no
  non-daemon process opens a direct T2 write connection. This is what makes the
  single-writer model actually hold.

### Validation gate (replaces the old soak signal)

Aggregate `select/s` is throughput-polluted and must NOT be the oracle. Use
**idle-after-contention return-to-baseline:** start a contending writer
(`nx index` or an in-process taxonomy write loop), stop it, assert the daemon
returns to single-digit `select/s` within ~10 s. Plus a `sample`-based soak
asserting no executor thread is wedged in the SQLite busy handler.

### Phase 3 design (2026-06-06, nexus-uzay8)

Full design: T2 `nexus/rdr151-phase3-design-2026-06-06`.

`run_collection_postprocessing` (`index.py:730-803`) opens one direct
`T2Database` (`index.py:735`, the counted epsilon-allow) and runs both halves on
it:

- **Compute half (irreducibly direct, RDR-128 P3):** `discover_for_collection`
  (returns a count, persists topics+centroids internally, atomic with T2-id
  generation) and `project_against` (returns `chunk_assignments`).
- **Persist half (pure-T2, routable):** the cross-collection
  `_persist_assignments` -> `assign_topic` burst (the live wedge site),
  `generate_cooccurrence_links`, `compute_topic_links(persist=True)`, relabel
  writes.

**Option B (CHOSEN, 2026-06-06) — eliminate the direct writer entirely.** Split
`discover_topics` and `rebuild_taxonomy` into a local compute half and a
daemon-routed pure-T2 persist half, mirroring the `compute_assignments` /
`persist_assignments` / `assign_batch` split already shipped for the assignment
path (RDR-128 P1, `nexus-fkq5q`). The persist RPC INSERTs the topic rows + chunk
assignments and **returns the generated `topic_id`s**; the caller then writes the
chroma centroids (keyed on `{collection}:{topic_id}`) locally.

**Key finding refuting the RDR-128 P3 "irreducible" framing:** the chroma centroid
write is *already* decoupled from the topic INSERT — in both `discover_topics`
(`catalog_taxonomy.py:1842-1844`) and `rebuild_taxonomy` (`2902-2904`) the
`_batched_upsert` of centroids runs **outside the lock, after `commit()`**, keyed
on the `topic_id` the INSERT returned. RDR-128 P3 conflated "topic_id generated at
INSERT" with "must be one transaction"; the centroid write is a separate post-commit
step that only *needs* the returned id, which the daemon can return. So Option B is
an extension of the established compute/persist pattern, **not** RDR-063 read/write-split
territory. Eliminates the direct `T2Database` at `index.py:735` (lint count for that
site → 0; enforce-flip becomes possible). Full contract + risks: T2
`nexus/rdr151-phase3-design-2026-06-06`.

**Option A (not taken):** route only the post-compute persists; keep the direct
handle for topic/centroid creation, narrowed epsilon-allow. Shrinks rather than
eliminates the last direct writer (lint stays non-zero). Rejected in favour of B
once the post-commit centroid decoupling showed full elimination was tractable.

**Lint:** keep one narrowed epsilon-allow at `index.py:735`; add a focused
AST/grep test (mirroring `tests/test_no_direct_catalog_writes_outside_projector.py`)
banning the taxonomy WRITE method names (`assign_topic`, `persist_assignments`,
`generate_cooccurrence_links`, `refresh_projection_links`) on a directly-constructed
handle outside `db/`+`daemon/`, baselined at the post-split count.

## Approach

**Phase 0 — Reproduce and pin (gates everything; resolves RF-1/RF-3).**
Build a standalone harness that stands up an asyncio UNIX-domain server with the
daemon's exact read-loop shape (`readexactly`-framed, `IncompleteReadError`
break, `finally: writer.close()`), then drives clients through four exit modes
(abrupt RST via SO_LINGER 0, half-close via `shutdown(SHUT_WR)`, mid-frame
disconnect, idle-after-frame). Run the server loop under `loop.set_debug(True)`
with a logging wrapper around the selector's `select()` that records the ready
fd set each iteration. Acceptance: identify which exit mode pins the loop, name
the fd it reports ready, and reproduce a sustained ~100% CPU spin. This harness
is the Gap 1 regression test. If no mode reproduces it against the simplified
server, escalate to wiring the wrapper into a controlled local daemon restart
(the only way to introspect without root). Exit gate: RF-1 answered with a
named fd and code path.

**Phase 1 — Stop the burn (Gap 1 + Gap 2 + Gap 3 + Gap 4, one milestone).**
Apply the Phase-0-identified transport teardown/unregister fix (the specific fix
depends on which RF-1 candidate Phase 0 confirms: the `ConnectionResetError` /
`OSError` catch in `_handle_connection` only if candidate (b)/Gap 2 is the
confirmed path, otherwise the event-loop-layer fix Phase 0 names); add the
accepted-connection idle deadline as the candidate-independent backstop. In the
same change, `mark_shutting_down()` first in `stop()` and
`to_thread` the heartbeat. Verify against: the Phase 0 harness (no spin, fd
returns to baseline); a `stop()` concurrent with `ensure-running` spawning no
replacement (closes RF-2 path); and a graceful-stop-under-load test asserting
SIGTERM terminates within `_GRACEFUL_STOP_TIMEOUT`. Live soak: run a daemon
under client churn beyond the historical re-peg interval (the live re-peg was
under 1 minute, so a multi-minute flat-CPU soak is decisive).

**Phase 2 — Contention hardening (Gap 5 + Gap 6; resolves RF-4).**
Cooperative bounded write retry/backoff (or a single serialized in-daemon
writer); dedicated crash-loop `flock`. Add the transient op-label dispatch log
to answer RF-4 (which write stalls), then remove it. Exact-count regression
tests for each.

Out of this RDR's scope, tracked as beads:
- **nexus-hcw0g** — test reaper format-blindness; a one-line `endpoint.pid`
  fallback in `tests/_daemon_leak_guard.py:39`. Leaked 66 daemons + 252 `/tmp`
  dirs live. Test-infra, not architecture, but should land soon to stop the
  test-side accumulation.
- **`stored_schema_version` caching** — per-`hello` fresh `sqlite3.connect`
  (`db/t2/__init__.py:477`); cache at `T2Database.__init__`. Efficiency only.

## Research Findings

Recorded 2026-06-05 from the live forensic session (3 daemons captured).

> **CORRECTION (2026-06-05, post-implementation live capture): RF-1 is REFUTED.**
> The mechanism this RDR was built on — "a leaked accepted-socket read fd left
> registered while a handler is parked in ``to_thread`` spins the kqueue
> selector" — is **wrong**. It was reproduced only by a measurement artifact
> (the Phase-0 heavy harness's high select/s under load is write *throughput*,
> identical with and without the fix, and returns to idle when churn stops).
> Three independent synthetic falsifications (RST-during-park, full exit-mode
> sweep, throughput isolation) showed no single-connection trigger spins the
> loop. A definitive **live capture of a pegged production daemon** (pid 65224 at
> 95.8% CPU, 3 consistent ``sys._current_frames()`` snapshots via a temporary
> ``SIGUSR2`` handler) shows:
> - the main thread is spinning in ``_run_once → selector.select() → kevent``;
> - ``loop._ready`` is **empty** (not a self-rescheduling-callback spin);
> - the selector holds **only** the self-pipe (fd 5) and the two listen sockets
>   (fd 31/32) — **zero accepted connection fds** during the peg;
> - an executor thread is **wedged** in
>   ``catalog_taxonomy.persist_assignments → assign_topic →
>   _resync_topic_links_for → conn.execute`` — blocked on the contended T2 write
>   lock (the ``database is locked`` the reclaim loop logs every 30s).
>
> **Real mechanism: the peg is contention-driven.** A ``catalog_taxonomy`` write
> wedged on the multi-writer-contended T2 lock, while the loop spins in
> ``select()`` on a perpetually-ready self-pipe/listen fd. The orphaned
> accepted fds seen via ``lsof`` are a *symptom* (clients abandoning a pegged
> daemon), not the cause. This converges with the Gap-5 / P2.1 contention work
> (catalog-write serialisation, ``BEGIN IMMEDIATE``), **not** the Phase-1
> connection-handler framing. Evidence: T2 memory
> ``nexus/rdr151-LIVE-MECHANISM-CAPTURED-2026-06-05`` and
> ``nexus/rdr151-mechanism-REFUTED-2026-06-05``. The Decision and Approach below
> predate this capture and must be revisited; the Phase-1 P1.1 change
> (pause-reading/abort) is retained only as connection-teardown hygiene, not as
> the peg fix.

### RF-1: exact asyncio internal that leaves the fd registered — PARTIAL (class confirmed; exact path deferred to Phase 0)

The mechanism CLASS is confirmed. macOS `sample` on three successive daemons
(82698, 31605, 74112) shows the main event-loop thread at ~99.9% in
`select_kqueue_control_impl -> kevent`, i.e. a `kevent(timeout=0)` spin driven
by a perpetually-ready selector fd, with accepted UDS sockets accumulating on
`t2.sock` with no peer process.

The exact fd and CPython transport path are NOT pinnable from outside the
process on this host: **both py-spy and lldb require root on macOS** (py-spy
refused; lldb attach hung, then detached) and were unavailable; 30s at 1ms
sampling cannot catch the sub-millisecond `_read_ready` callback (the profile is
~100% kevent). This is itself a finding: external introspection is blocked.

Non-obviousness flag: normal asyncio removes the reader from the selector on EOF
and force-closes on RST, so the leak is NOT the naive "EOF socket stays
registered" path. Leading candidates to discriminate in Phase 0: (a) the
self-pipe / `call_soon_threadsafe` wakeup driven by the daemon's heavy
`asyncio.to_thread` dispatch (every RPC, plus the reclaim and reassert loops);
(b) `eof_received()` returning True for half-close support, keeping the read fd
registered; (c) a `call_soon` self-reschedule keeping `loop._ready` non-empty so
the selector is always polled with `timeout=0`.

RESOLUTION (re-scopes the plan): RF-1 is promoted to the **first Phase 0
deliverable** rather than a pre-implementation research item. Build a reproducer
harness with an instrumented selector (`select()` recording the ready fd set and
the `timeout<=0` spin signature) that records which fd is reported ready each
iteration. That harness pins the exact path AND is the regression test for Gap 1.

**Phase 0 progress (2026-06-05), harnesses `tests/daemon/rdr151_phase0_repro.py`
and `tests/daemon/rdr151_phase0_signalfd.py`.** A faithful-but-simplified
asyncio UDS server (daemon's read-loop shape, `to_thread` dispatch, background
`to_thread` traffic, main-thread signal handlers) was driven through all four
client-exit modes AND 600-800 connect/RPC/disconnect churn cycles. **No mode
reproduced the spin** (post-churn idle select/s stayed 1-340; the bug threshold
is >5000). REFUTED as sole causes, with harness evidence:
- the bare read-loop + any client-exit mode (RST / half-close / mid-frame / idle);
- `to_thread` dispatch churn (RF-1 candidate a, self-pipe) at the simplified level;
- the main-thread signal-wakeup fd (`add_signal_handler` / `set_wakeup_fd`);
- **uncaught `ConnectionResetError` (Gap 2): asyncio tears the transport down
  when the handler task raises, so the fd does NOT leak.** This is load-bearing
  for Phase 1: **the Gap-2 `ConnectionResetError` catch is necessary hygiene but
  proven INSUFFICIENT on its own to stop the spin.** Do not ship it as "the fix."

The spin therefore requires real-daemon machinery the simplified harness omits
(real `T2Database`/SQLite + WAL, the on-loop `flock` heartbeat, the lease/
discovery file I/O, `_invoke_with_lock_retry`, the real framing protocol, or
their interaction).

**Escalation A — real daemon, in-process, `hello`-churn
(`tests/daemon/rdr151_phase0_realdaemon.py`).** Ran the actual `T2Daemon` (real
SQLite+WAL, heartbeat, lease I/O, dispatch) on the SpySelector loop with 3
threads of real `T2Client` connect/`hello`/disconnect churn for a full 13-minute
soak. **No spin.** Steady ~2150-2240 select/s, zero-ready/s flat at ~275, fd 4
(self-pipe) and fd 30 accumulating linearly (constant rate, not runaway). So the
peg is NOT triggered by connection churn or `hello`-RPC volume against the real
daemon either. Added to the refuted set.

**Escalation B (queued) — heavy real-op churn
(`tests/daemon/rdr151_phase0_heavy.py`).** Broadens the churn to real
`memory.put` writes (the real SQLite write path / WAL writer-lock contention)
plus a high-rate abrupt-RST-mid-write pattern (raw socket sends a valid
`memory.put` frame, then RSTs without reading the response, so the daemon is
inside `to_thread` mid-write when the peer vanishes — matching the live
"accepted sockets, no peer process" evidence). This targets the
client-dies-mid-write path against the real write machinery.

**Escalation B REPRODUCED THE SPIN (RF-1 / RF-3 ANSWERED).** With heavy churn
(real `memory.put` writes + abrupt-RST-mid-write), the SpySelector recorded a
sustained ~6500-7000 select/s spin (`SPIN!`, 3x the ~2150 baseline) for ~4
minutes, with the ready-fd set dominated by a CLUSTER of accepted connection
sockets (fds 33/34/35/32/36), each accumulating thousands of `timeout<=0` ready
hits per 30s window, plus 404,794 uncaught `ConnectionResetError` tracebacks
(RC-6 spam, live).

**Mechanism (named):** the trigger is NOT RST-during-`readexactly` (the minimal
harness proved that path cleans up via `IncompleteReadError` and transport
teardown). It is **RST while the handler is parked in
`await asyncio.to_thread(...)` mid-write** — the handler is not in a read at that
instant, so the peer's RST/abrupt-close leaves the accepted socket's **read fd
registered and perpetually reported ready** by the kqueue, spinning the loop.
When the `to_thread` write completes, the handler's post-dispatch
`writer.write()` / `drain()` hits the dead socket and raises the (uncaught)
`ConnectionResetError`. The fd in the live spin is a **connection socket, not the
self-pipe** — matching the live `lsof` evidence (accepted `t2.sock` fds with no
peer process).

This refines the Gap-1/Gap-2 fix: catching `ConnectionResetError` on the read
loop is insufficient (proven earlier); the fix must ensure the accepted socket's
read fd is **deregistered/closed when the peer dies during dispatch**, e.g. by
detecting `connection_lost` at the transport level and closing the writer, and/or
guarding the dispatch await so a mid-dispatch peer death tears the transport down
rather than leaving its fd in the selector. The Phase-0 harness
(`rdr151_phase0_heavy.py`) is the Gap-1 regression test: post-fix, the same churn
must keep select/s at baseline (no `SPIN!` window).

Open refinement (RF-1b): harness B reproduced a 3x spin that DECAYS after ~4
min, whereas production sustained a full 100% peg. The decay suggests per-fd
self-limiting; the sustained production peg likely needs continuous
mid-dispatch deaths (always-fresh leaked fds). Confirm the amplification path
during Phase-1 fix validation (the fix should zero the spin regardless).

### RF-2: does Gap 4's flock stall exceed the 3.0s TTL in production? — OPEN

Code-confirmed plausible (`heartbeat_tick()` runs a blocking `fcntl.flock` on the
event-loop thread, `t2_daemon.py:990`; TTL is `DEFAULT_TTL = 3.0`,
`service_registry.py:64`). Not quantified live. Resolve with the same
`loop.set_debug(True)` slow-callback log used for RF-1.

### RF-3: which client-exit pattern produces the leaked fd? — OPEN

To be resolved by the Phase 0 reproducer: test abrupt RST (SO_LINGER 0),
half-close (shutdown SHUT_WR), mid-frame timeout, and idle-after-frame, and
observe which leaves the accepted fd registered and the loop spinning.

### RF-4: which write stalls in Gap 5 (RC-beta)? — OPEN

Capture B caught a write-transaction begin parked in
`sqlite3BtreeBeginTrans -> btreeInvokeBusyHandler -> sqlite3OsSleep`, but
`sample` cannot yield the SQL text. Discriminate serve-path write vs. the 30s
reclaim vs. a `stored_schema_version` connection by adding a transient
op-label log at the `_invoke_with_lock_retry` dispatch site under a controlled
restart.

### RF-5 (new, from this session): the cascade is actively churning — RC-2/RC-3 priority elevated

The T2 daemon was observed replaced three times within minutes (82698 ->
31605 -> 74112), each replacement re-pegging within seconds (74112 reached
99.9% CPU at 53s uptime). The peg makes `hello()` slow, the daemon is declared
stale, a replacement spawns, and the new one re-pegs almost immediately. This is
a self-sustaining loop that burns a core continuously, not an occasional event.
It elevates Gap 3 (RC-2) and Gap 4 (RC-3) from "cascade enablers" to
"actively-degrading"; the phase plan should land them alongside, not after, the
Gap 1 peg fix.

## Open Questions

- Should accepted connections carry an idle/read timeout as a belt-and-braces
  guard, or is the teardown fix sufficient on its own?
- Is a single serialized in-daemon writer (one connection, one queue) the right
  long-term answer to Gap 5, superseding per-call busy-handler reliance?

## Out of Scope

- The test-daemon reaper fix (nexus-hcw0g) and the `stored_schema_version`
  caching: filed as beads, not part of this architecture decision.
- Cross-tier (T1/T3) lifecycle changes: RDR-149 owns the shared primitive; this
  RDR fixes T2-daemon-specific connection handling and may surface a shared-
  primitive change only if RF-1 implicates `service_registry`.
- The T3 analog of Gap 3: `T3Supervisor.stop()` (`t3_daemon.py:445`) also never
  calls `mark_shutting_down` (only `t1_lease.py:277` does). Filed as
  **nexus-6o4uj** so the gap is tracked, not silently dropped. T3's supervisor
  is synchronous (a `time.sleep` loop, not asyncio), so its false-crash window
  is likely narrower than T2's, but the structural gap is identical. Per the
  RDR-149 invariant ("a lifecycle fix that should apply cross-tier lands in the
  shared primitive, never one tier's copy"), the preferred fix for both is to
  fold the `mark_shutting_down` call into a shared supervisor-stop path; if Gap 3
  here lands T2-only first, nexus-6o4uj carries the T3 follow-on.

## Alternatives Considered

- Ship a fourth point patch (raise busy_timeout, tweak reclaim cadence):
  rejected: it does not touch Gap 1, which is the actual peg, and would be the
  fourth blind patch on this subsystem.
- Periodic forced restart of the daemon as a mitigation: rejected: masks the
  leak, perpetuates the cascade, and burns the reproducer.

## Trade-offs

- Adding an idle/read deadline on accepted connections risks closing a slow but
  legitimate client mid-request; the deadline must exceed the longest legitimate
  RPC. Mitigated by sizing against the existing `_GRACEFUL_STOP_TIMEOUT` and RPC
  timeouts.

## Critical Assumptions

- CA-1: the leaked fd is an accepted UDS socket left registered after abrupt
  client death (supported by `lsof` accumulation + the `kevent(0)` spin; pending
  RF-1 for the exact internal).
- CA-2: closing/unregistering the fd on the abrupt-death path stops the spin
  (high confidence; the spin is a `timeout=0` selector loop driven by the
  perpetually-ready fd).

## Test Plan

- A red-first reproducer (Phase 0) that leaks a half-open accepted socket and
  asserts no fd accumulation and no busy-loop.
- Exact-count regression tests for the crash-loop sentinel under simulated
  election-lock-timeout contention (Gap 6).
- A stop()-during-ensure-running test asserting no replacement spawn (Gap 3).

## Validation

Re-peg interval, reconciled (two distinct scenarios, do not conflate):

- **Lone daemon, from zero:** a freshly spawned daemon with no prior cascade
  pegs in roughly 4 to 12 minutes of ordinary session `nx` traffic (observed:
  pid 31605 ~4 min from spawn; pid 97219 ~12 min from a clean spawn). This is the
  steady-state leak rate and the relevant figure for the soak gate.
- **Mid-cascade:** a replacement daemon spawned while the cascade is already
  churning re-pegs within seconds (pid 74112 reached 99.9% at 53s). This is NOT
  the leak rate; it reflects a replacement inheriting an already-degraded
  environment, and is the figure Gaps 3/4 (cascade enablers) must eliminate.

Soak-test acceptance gate (single, unambiguous): after the Phase 1 fix, run a
lone daemon under representative client churn for **at least 30 minutes** (well
above the 12-minute worst-case lone-from-zero peg) and require CPU to stay flat
(no sustained core burn) and the accepted-fd count to return to baseline between
connections. Separately, a cascade-suppression check: drive `stop()` concurrent
with `ensure-running` and confirm no replacement is spawned and no mid-cascade
fast re-peg occurs.

Instrumentation: the macOS `sample` profiler is the accepted live instrument
(py-spy and lldb both need root on macOS); registering a SIGUSR1 `faulthandler`
handler in the daemon would make future all-thread captures one-step.

## Finalization Gate

Pending. Run `/conexus:rdr-gate` after Research Findings are populated.

## References

- `docs/postmortem/2026-06-05-daemon-concurrency-forensics.md` (live captures,
  per-claim authentication log).
- Beads: nexus-xmohw (primary), nexus-x47yx, nexus-00en9, nexus-hcw0g,
  nexus-whl8n.
- Prior art: RDR-128, RDR-129, RDR-140, RDR-141, RDR-146, RDR-149.
- Live sample artifacts: `/tmp/daemon-peg-sample-82698.txt`,
  `/tmp/daemon-peg-CAPTURE-B-31605.txt`.
