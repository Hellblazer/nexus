---
title: "T2 Daemon Supervisor & Ownership Model: Single-Flight Election to End the Spawn-Race / SQLite-Lock Thrash"
id: RDR-140
type: Architecture
status: closed
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-31
accepted_date: 2026-05-31
closed_date: 2026-05-31
closed_reason: implemented
related_issues: [nexus-13bcq]
related_rdrs: [RDR-128, RDR-129, RDR-120]
---

# RDR-140: T2 Daemon Supervisor & Ownership Model — Single-Flight Election to End the Spawn-Race / SQLite-Lock Thrash

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

GitHub issue #1041 (bead `nexus-13bcq`): when more than one nexus client stack
is alive on a machine (two CLI sessions' MCP servers, a running `nx index repo`,
and the Claude Desktop conexus MCPB extension), the T2 daemon enters a
**crash/restart thrash loop** instead of converging on a single shared instance.
Each stack runs a daemon *supervisor* (`nx daemon t2 ensure-running`) that, on
finding no reachable matching-version daemon, cold-spawns one. With N stacks this
is a continuous election race rather than a one-time election. Observed on
conexus 5.5.1 (macOS, Python 3.12.11, `memory.db` ~237 MB): **535
`t2_daemon_crashed` vs 344 `t2_daemon_started`** in one day, plus
`database is locked` write contention and `t2_index_write_daemon_unreachable_fallback`
windows where no daemon was live at all.

The single-writer invariant from RDR-128/129 (exactly one daemon holds open
SQLite write handles to `memory.db`) is *correct and must be preserved*. The bug
is that the mechanisms enforcing it degrade pathologically under N concurrent
supervisors: benign race outcomes are logged as crashes, healthy peers are
killed by competing supervisors, and migration re-runs on every restart.

### Enumerated gaps to close

#### Gap 1: Spawn-lock loser crashes instead of attaching

`_acquire_spawn_lock` (`src/nexus/daemon/t2_daemon.py:1013`) takes two
`fcntl.LOCK_EX | LOCK_NB` flocks and, on `BlockingIOError`, raises
`T2DaemonError`, which propagates through `start` → `run_t2_daemon` →
`asyncio.run` and is logged as `t2_daemon_crashed` with a full ERROR-level
traceback, then `sys.exit(2)`. Losing the spawn race is the **expected steady
state** with N clients, not an error. The loser should attach to the winning
daemon (poll briefly for the discovery file / socket) and exit `0` quietly at
`debug`/`info` level. Closing this gap removes the crash-traceback noise (the
535/day) without changing the invariant — the loser never opens `T2Database`.

#### Gap 2: Supervisors SIGTERM healthy same-version peers

`T2Daemon.start` calls `_reap_predecessor_daemon`
(`src/nexus/daemon/t2_daemon.py:940`), which kills **every** non-self PID found
in the discovery file or holding `memory.db` open, with **no ownership or
version check** (`_reap_one_daemon`, L980, SIGTERM→SIGKILL). The
`t2_daemon_stop_requested signal_received=True` lines that fire 2–13s after a
healthy start are a competing supervisor's newly-starting daemon reaping a
running peer. This is the actual thrash driver. The reap must become
ownership-/version-aware: never SIGTERM a daemon that is healthy AND at the
current version; reap only stale-version daemons or genuinely orphaned writers.
This is the gap that touches the RDR-128 backstop and needs the most care.

#### Gap 3: No single-flight around the election decision

`t2_ensure_running_cmd` (`src/nexus/commands/daemon.py:760`) reads the discovery
file, and if no matching daemon is reachable, cold-spawns. N supervisors
executing this near-simultaneously all read "no daemon" and all spawn; the spawn
lock then makes N-1 of them crash (Gap 1) and the winner reaps any peer that
slipped through (Gap 2). The election decision (discover → spawn) must be
single-flighted across processes (a coordination lock) with a re-check after the
lock is taken, so exactly one stack spawns and the rest attach.

#### Gap 4: Migration re-runs on every cold start even at current_version

`start` opens `T2Database(run_migrations=True)`
(`src/nexus/daemon/t2_daemon.py:658`). `bootstrap_schema` has only an
*in-process* `_upgrade_done` fast-path; a fresh cold-spawn process always opens a
connection, enables WAL (a `BEGIN IMMEDIATE` write that takes the writer lock),
and reads `_nexus_version` even when `db_version == current_version`. Under
contention each spawner blocks up to `busy_timeout=30000` on the writer lock,
which is the 8–16s figure and the `database is locked` amplifier. A
`db_version == current_version` fast-path before any write closes this gap.

#### Gap 5: No crash-loop observability or backoff

There is no restart loop in the daemon (single `start → run → stop`), so "535
crashes" is N independent supervisor invocations each crashing once, not one
process looping. There is no surface to diagnose this: no owner-PID / socket-
liveness / restart-count report. Add `nx daemon t2 status` (owner PID, socket
liveness, version, restart count in the last interval) and a bounded
log-once-then-stop guard so a pathological config can't emit hundreds of error
tracebacks.

## Context

### Background

The T2 daemon serves `memory.db` (the cross-process shared SQLite tier) to every
nexus client stack. RDR-128 established the single-writer invariant (one owner
for `memory.db`) and RDR-129 hardened the write path (guaranteed-single-daemon
enforcement, deferred spawn-lock release to process exit, PID-liveness interlock,
busy_timeout 30000, bounded dispatch retry). Those RDRs solved correctness under
*restart and version-cycle*; they did not address the *steady-state election
race* when many independent supervisors are alive at once, which is the regime a
real user hits (two terminals + Claude Desktop + an index run). #1041 is that
regime.

### Technical Environment

- conexus 5.5.1, macOS (Darwin 25.4.0), Python 3.12.11 (uv-managed).
- `memory.db` ~237 MB, WAL mode, `busy_timeout=30000`.
- Supervisor entry: each MCP server boot shells `nx daemon t2 ensure-running`
  via `ensure_installed_and_running` (`src/nexus/mcp/_first_run.py:87`), result
  discarded.
- Discovery: `~/.config/nexus/t2_addr.<uid>` (JSON: pid, socket, tcp_port,
  daemon_version, status) + `t2.sock`; `discovery.py:165` `find_t2_daemon`.
- Single-writer enforcement: `t2_spawn.lock` + `<db>.spawn_lock` flocks held for
  the daemon's lifetime (`stop` deliberately does NOT release — RDR-129 A2).

## Research Findings

### Investigation

Traced the spawn/ownership/supervisor/kill flow across the daemon module (code
map, 2026-05-31). Citations below are the load-bearing call sites.

| Concern | Location | Current behavior |
| --- | --- | --- |
| Supervisor / election | `commands/daemon.py:760` `t2_ensure_running_cmd` | discover → if matching-version daemon live, return; if version-skew, SIGTERM stale + cold-spawn; else cold-spawn `nx daemon t2 start` |
| Predecessor reap | `t2_daemon.py:940` `_reap_predecessor_daemon` / `:980` `_reap_one_daemon` | kills any non-self PID in discovery file OR holding the db open; **no ownership/version check** |
| Spawn lock | `t2_daemon.py:1013` `_acquire_spawn_lock` | two `LOCK_EX|LOCK_NB` flocks; `BlockingIOError` → `T2DaemonError`; **no retry/attach** |
| Crash path | `t2_daemon.py:1121` `run_t2_daemon` → `:1129` `except Exception` | `_log.exception("t2_daemon_crashed")` (full traceback) → re-raise → `sys.exit(2)` |
| Migration on start | `t2_daemon.py:658` `T2Database(run_migrations=True)` → `db/t2/__init__.py:437` `bootstrap_schema` → `migrations.py:2396` `apply_pending` | in-process fast-path only; cross-process always opens conn + WAL-enable write; **no `db_version == current` short-circuit** |
| Backoff / restart | `t2_daemon.py` (none) / `commands/daemon.py:909` ensure-running poll | no restart loop; ensure-running polls liveness, no retry-spawn |

### Key Discoveries

- **Verified (code reading):** `_reap_predecessor_daemon` has no ownership or
  version guard — it reaps healthy same-version peers. This is the thrash driver
  (Gap 2). [`t2_daemon.py:940–1010`]
- **Verified (code reading):** the spawn-lock loser has no attach path; it
  crashes with a logged traceback. (Gap 1) [`t2_daemon.py:1013–1064`, `:1121–1130`]
- **Verified (code reading):** migration's only cross-process fast-path is the
  flock + `_nexus_version` read; the WAL-enable write runs unconditionally on a
  fresh process. (Gap 4) [`db/t2/__init__.py:437–490`]
- **Documented (RDR-129):** spawn-lock release is deliberately deferred to
  process exit; `ensure-running` polls PID liveness, not the discovery file,
  precisely because `stop` unlinks the file early. Any new attach/quiet-exit
  logic must respect this lifetime model.
- **Verified (A1 source search, 2026-05-31) — Two invariants.** Daemon-spawn
  serialization gives the **daemon-process single-writer** invariant only. The
  **full `memory.db` single-writer** property is NOT achieved by it, because
  non-daemon paths open `T2Database` directly with no flock (WAL `busy_timeout`
  only). **Correction (gate, 2026-05-31):** an earlier draft listed
  `aspect_worker` persist as a live direct-writer — that path was already routed
  through the daemon by `nexus-zir76` (shipped 5.3.0): every `aspect_worker`
  persist now goes via `t2_index_write` → the daemon's `complete_aspect` RPC
  (`aspect_worker.py:277,447`; `complete_aspect` is NOT on `_RPC_DENY_OPS`,
  `t2_daemon.py:458`). The actual remaining live direct-write paths are: (1) the
  `t2_index_write` daemon-unreachable fallback (`mcp_infra.py:218`) — and its
  `T2SchemaVersionMismatchError` arm opens a second writer *even while a daemon
  is running*, the post-upgrade window; (2) `t2_ctx()` write-path calls in MCP
  command implementations (`mcp/core.py`, `hook_registry.py` — a larger surface
  than first stated; read-dominated but includes writes); (3) `epsilon-allow`
  irreducible writers (taxonomy rebuild, aspects gc, index auto-discover, upgrade
  bootstrap) classified as documented-irreducible by RDR-128 P3. **This RDR
  scopes to invariant (a)**; routing (1)–(3) through the daemon is RDR-128 P3
  deferred work, out of scope here. The one path most relevant to the thrash is
  the **schema-mismatch fallback arm of (1)**: post-upgrade, clients open a
  direct writer AND supervisors SIGTERM the stale daemon, compounding both the
  lock contention and the kill churn — the migration fast-path + faster daemon
  version-convergence shrink that window (a candidate for in-scope mitigation, see
  Trade-offs).

### Critical Assumptions

- [x] **A1 — Attaching the spawn-lock loser as a client never opens a second
  writer.** — **Status**: VERIFIED (daemon-process scope) — **Method**: Source
  Search (codebase-deep-analyzer, 2026-05-31). `_acquire_spawn_lock`
  (`t2_daemon.py:1013`) is strictly before `T2Database(...)` (`:658`); a
  spawn-loser raises `T2DaemonError` at `:1034` and exits WITHOUT constructing
  `T2Database`. **Scope boundary** (see Key Discoveries → "Two invariants",
  corrected at gate): daemon-spawn serialization does NOT by itself give full
  `memory.db` single-writer — the `t2_index_write` fallback (incl. its
  schema-mismatch arm), MCP `t2_ctx()` writes, and epsilon-allow writers remain;
  the `aspect_worker` path was already closed by `nexus-zir76`. A1 is confirmed
  for the daemon-process race only; the full-db property is scoped to RDR-128 P3.
- [x] **A2 — A version/identity token in the discovery file is sufficient to
  make reaping ownership-aware without reintroducing orphan-writer risk.** —
  **Status**: VERIFIED-FEASIBLE — **Method**: Multi-stack race harness +
  discovery-file inspection (2026-05-31). The `t2_addr.<uid>` token ALREADY
  carries `pid`, `daemon_version`, `uds_path`, `tcp_port`, `start_time`. Sufficient
  to reap only when `(pid dead) OR (daemon_version != current) OR (health ping
  fails)`; a healthy same-version peer is never reaped → attach. The orphan case
  (flock held by a dead PID) is the pid-liveness check. No new persisted state
  required (an explicit owner-uuid would only harden against pid reuse).
- [x] **A3 — The `current_version` migration fast-path can elide the per-start
  migration work safely.** — **Status**: VERIFIED (re-weighted) — **Method**:
  SQLite lock spike + source reading. Reading `journal_mode` and `_nexus_version`
  is lock-free (works under a held writer lock); `PRAGMA journal_mode=WAL` when
  already WAL is a no-op (no writer lock). Migration is ALREADY flock-serialized
  cross-process (`t2_migration_flock`, `db/t2/__init__.py:474`), so the original
  "N concurrent BEGIN IMMEDIATE" framing was wrong. A `version==current && WAL`
  fast-path before the flock elides the flock-wait + no-op `apply_pending` on
  every cold start — real but MODEST. **Re-weight**: the dominant lock contention
  is the non-daemon second-writer fallbacks (A1 boundary), not migration.
- [x] **A4 — Single-flighting the election converges N stacks to one daemon
  without deadlock when a holder dies mid-election.** — **Status**: VERIFIED
  (mechanism) + DESIGNED (outcome, pending the P2 harness) — **Method**:
  Multi-stack race harness against the CURRENT code (2026-05-31, single 5-way
  run). VERIFIED: the current spawn lock converges to exactly 1 daemon
  (single-writer holds) but via crash + reap churn (started=1, crashed=9,
  stop_requested=1), and an `fcntl` flock auto-releases on holder death (the
  no-deadlock property). DESIGNED, not yet code: the proposed
  election-flock + re-check + loser-attach yielding "1 start / K-1 quiet
  attaches / 0 crashes / 0 reaps" — that path does not exist yet; the Test Plan's
  multi-stack race harness is the GATE for P2, run repeatedly (race outcomes are
  stochastic — a single run is not sufficient at implementation time).

### New finding — self-healing discovery file (from the A2/A4 harness)

The discovery-gap case (a healthy daemon alive but its `t2_addr` removed) made
**all 5 racers crash** on the spawn lock they couldn't trace back to the live
daemon. So Gap 1 (loser-attach) needs the loser to FIND the live daemon even
when `t2_addr` is transiently absent: on spawn-lock `BlockingIOError`, poll for
`t2_addr` to (re)appear AND have the spawn-lock holder periodically re-assert /
validate its own discovery file so a transient gap self-heals. Add this to the
Proposed Solution (item 2 + a holder-side discovery re-assert).

## Proposed Solution

A **single-flight election** model layered on the existing RDR-128/129 flock
invariant — change *who decides to spawn and what losers/peers do*, not the
single-writer guarantee itself.

1. **Election coordination lock (Gap 3).** `ensure-running` acquires a blocking
   (with timeout) coordination flock around the discover→spawn decision, then
   **re-discovers** after acquiring it. If a daemon appeared while waiting,
   attach and return. Only the lock holder may cold-spawn. N stacks serialize;
   exactly one spawns; the rest attach.

2. **Loser attaches, exits 0, quiet + self-healing discovery (Gap 1).**
   `_acquire_spawn_lock` `BlockingIOError` becomes a typed, non-error outcome:
   poll briefly for the winner's discovery file/socket, log
   `t2_daemon_spawn_lost` at `info`, exit 0. No traceback. Because a transient
   `t2_addr` gap makes losers unable to trace the live daemon (verified: all 5
   racers crashed in the discovery-gap case), the spawn-lock **holder periodically
   re-asserts/validates its own discovery file** so a gap self-heals and losers
   can attach. (Belt-and-suspenders behind the election lock.) Two constraints,
   both load-bearing against RDR-129's deliberate early-unlink-on-stop (`stop()`
   unlinks `t2_addr` at `t2_daemon.py:734` BEFORE the spawn-lock releases, to
   signal departure): **(i)** the re-assert task MUST be cancelled at the START of
   `stop()`, before the unlink, so it can never re-write a discovery file for a
   daemon that is mid-shutdown (which would make a loser attach to a dying
   socket); **(ii)** the re-assert interval and the loser's attach-poll timeout
   must be provably consistent — `loser_poll_timeout >= reassert_interval +
   worst_case_write_latency` — with documented defaults (proposed:
   `reassert_interval = 1s`, `loser_poll_timeout = 3s`). The loser, on a
   discovered-but-unreachable socket, falls back to polling (it must not treat a
   stale `t2_addr` as a live attach).

3. **Ownership-/version-aware reaping (Gap 2).** Persist an owner token +
   `daemon_version` in the discovery file. `_reap_predecessor_daemon` reaps a PID
   only if it is NOT (healthy AND current-version): i.e. reap stale-version
   daemons and orphaned writers (flock held but PID dead / health ping fails),
   never a healthy current-version peer. The flock remains the hard backstop; the
   reap stops being the thrash source.

4. **Migration current-version fast-path (Gap 4).** Before the WAL-enable write,
   if `bootstrap_version(conn) == current_version` and WAL is already on, skip
   the migration transaction entirely. Eliminates the per-start writer-lock
   contention.

5. **Observability + bounded crash (Gap 5).** `nx daemon t2 status` (owner PID,
   socket liveness, version, restart count in interval). A sentinel-file-based
   crash-loop guard logs once at error and stops respawning after N failures in
   a window.

## Implementation Plan

Phased; each phase independently shippable and testable. Earlier phases are
contained (no invariant change); the invariant-touching phase (P3) is gated.

- **P1 — Stop the noise (contained).** Gap 1 (loser attach + quiet exit) + Gap 4
  (migration fast-path). Removes the crash tracebacks and the lock-contention
  amplifier. No ownership change.
- **P2 — Single-flight election (contained).** Gap 3 coordination lock +
  re-check. Converges N stacks without touching the reap. **Caveat:** P1+P2 break
  the *cascading* restart loop (only one supervisor spawns), but the new daemon's
  startup `_reap_predecessor_daemon` (RDR-129's full same-db fd-sweep) is still
  unconditional until P3 — so a same-version healthy peer found by the sweep (a
  version-transition edge or a second config-dir) is still killed. P1+P2 bound
  the healthy-peer-kill risk to the single-spawn case; P3 eliminates it. The
  "earlier phases contained / no invariant change" claim is about not *weakening*
  the single-writer flock, not about fully ending healthy-peer kills.
- **P3 — Ownership-aware reaping (invariant-touching, gated).** Gap 2 owner/
  version token + guarded reap. Requires the A2 spike + a multi-stack race
  harness + `code-review-expert` + `substantive-critic` before merge.
- **P4 — Observability (contained).** Gap 5 `nx daemon t2 status` + bounded
  crash guard.

## Trade-offs

- **Election lock adds boot latency** (serialized spawn) — bounded by the lock
  timeout; the common case (daemon already up) skips the lock via fast
  re-discovery. Net win vs the current thrash.
- **Owner token in the discovery file** is more state to keep correct across the
  `stop`-unlinks-early lifetime (RDR-129); mitigated by version+health being the
  primary signal and the flock remaining the hard backstop.
- **Migration fast-path** trades a tiny "what if WAL got disabled" risk for
  modest contention relief; guarded by the explicit WAL-pragma check.
- **Scope honesty (gate):** this RDR fixes the *crash/kill thrash*; it does NOT
  by itself fix `database is locked`, whose dominant source is the non-daemon
  second-writer paths (A1 boundary), explicitly RDR-128-P3-deferred. The one
  exception worth pulling IN-scope is the `t2_index_write`
  `T2SchemaVersionMismatchError` fallback arm (`mcp_infra.py`): it is both a
  thrash compounder (post-upgrade, clients open a direct writer while supervisors
  reap the stale daemon) AND a genuine second-writer-while-daemon-alive. Faster
  version-convergence (the election + ownership-aware reap reaching a single
  current-version daemon quickly) shrinks that window; whether to additionally
  make the fallback *wait briefly for daemon version-convergence* instead of
  opening a direct writer is a P3 decision flagged here, not silently dropped.

## Alternatives Considered

- **systemd/launchd-managed single daemon (infra-owned election).** Cleanest
  single-owner model, but nexus runs cross-platform and installs per-user
  without root; can't assume an init supervisor. Rejected as the primary
  mechanism; the in-process election must work standalone.
- **Block-and-wait spawn lock (make losers wait on the flock).** Simpler than an
  election lock, but a holder that dies mid-migration would stall all waiters for
  the lock timeout, and it doesn't give the loser an attach path. The election
  lock + re-check is strictly more responsive.
- **Reference-counted shared daemon (clients register/deregister).** More moving
  parts and a new failure mode (leaked refcounts pin a dead daemon). The
  version+health reap guard achieves convergence without a refcount ledger.

## Test Plan

- **Multi-stack race harness (new):** spawn K parallel `ensure-running`
  invocations against one `memory.db`; assert exactly one `t2_daemon_started`, K-1
  quiet attaches, zero `t2_daemon_crashed`, zero healthy-peer SIGTERMs.
- **Version-skew convergence:** start an old-version daemon, launch K
  current-version supervisors; assert exactly one reap of the stale daemon, one
  new daemon, the rest attach.
- **Orphan-writer reap still works:** simulate a flock held by a daemon whose
  health ping fails; assert it is reaped (invariant preserved).
- **Migration fast-path:** at `current_version`, assert no `BEGIN IMMEDIATE` /
  writer-lock acquisition on cold start; assert a real pending migration still
  runs.
- **Election-lock holder dies mid-spawn:** assert waiters recover (no deadlock).
- Regression: the RDR-129 suite (`tests/daemon/test_t2_daemon_lifecycle.py`,
  `test_t2_ensure_running.py`, `test_t2_daemon_startup_invariant.py`,
  `test_t2_concurrency.py`) must stay green.

## Validation

Reproduce the #1041 loop (two CLI stacks + Desktop MCPB + `nx index repo
--force`), `tail -f logs/t2_daemon.log`, and assert: a single stable daemon
(PPID 1, no further `t2_daemon_started`/`stop_requested` churn), crashes == 0
over a 10-minute window, and no `database is locked` during a concurrent index.

## Finalization Gate

To be run via `/conexus:rdr-gate` once Critical Assumptions A1–A4 are verified
(P3's A2 spike is the gating one). Structural + assumption-audit + AI critique
(`substantive-critic`, briefed with the RDR-128/129 invariant).

## References

- GitHub issue #1041 (bead `nexus-13bcq`).
- RDR-128 (T2 Single-Writer Enforcement), RDR-129 (T2 Daemon Write-Path
  Hardening) — the invariant this RDR must preserve.
- Code map (2026-05-31): `t2_daemon.py` (`_acquire_spawn_lock` L1013,
  `_reap_predecessor_daemon` L940, `start` L642), `commands/daemon.py`
  (`t2_ensure_running_cmd` L760), `db/t2/__init__.py` (`bootstrap_schema` L437),
  `mcp/_first_run.py` (`ensure_installed_and_running` L87).

## Revision History

- 2026-05-31: Draft created from GH #1041 root-cause analysis + daemon code map.
- 2026-05-31: Research complete — all 4 Critical Assumptions verified.
  A1 VERIFIED (daemon-process scope) + two-invariant scope boundary discovered.
  A2 VERIFIED-FEASIBLE (discovery token already carries pid+version+conn; reap
  guard needs no new state). A3 VERIFIED + re-weighted (migration is already
  flock-serialized; fast-path is a modest win; second-writer fallbacks are the
  dominant lock contention). A4 VERIFIED (spawn lock converges via crash/reap
  churn; an fcntl election flock + re-check makes it clean, no deadlock).
  New design finding: self-healing discovery file (holder re-asserts t2_addr).
  Ready for /conexus:rdr-gate.
- 2026-05-31: Gate (substantive-critic) — BLOCKED on 2 criticals, fixed in place:
  (1) A1 boundary cited the `aspect_worker` direct-write path, already CLOSED by
  `nexus-zir76` (5.3.0) — corrected to the actual live paths; (2) self-healing
  re-assert lacked a `stop()`-cancel spec + interval constraint — added
  (cancel-before-unlink; `loser_poll_timeout >= reassert_interval + write
  latency`). Also clarified A4 (VERIFIED mechanism / DESIGNED outcome), qualified
  P1+P2 (startup reap unconditional until P3), and added the schema-mismatch
  fallback scope note. Re-gate to confirm PASS.
