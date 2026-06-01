---
title: "T2 Version-Skew Double-Writer: Split t2_index_write Exception Arms + Self-Healing Daemon Re-Assert"
id: RDR-141
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-01
accepted_date: 2026-06-01
related_issues: [nexus-fcfhz]
related_rdrs: [RDR-128, RDR-129, RDR-140]
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-141: T2 Version-Skew Double-Writer: Split t2_index_write Exception Arms + Self-Healing Daemon Re-Assert

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-140 closed the daemon-vs-daemon single-writer hole (spawn-lock loser-attach, single-flight election, ownership-aware reap, crash-loop guard). The deep-analyst end-to-end single-writer analysis (2026-05-31, T2 `nexus/rdr140-single-writer-e2e-analysis-2026-05-31`) confirmed the invariant HOLDS for everything those four mechanisms govern, and identified exactly one reachable residual that RDR-140 deliberately left out of P3 scope (gate memo `rdr140-p3-gate`): the **A1 boundary** — non-daemon direct writers that bypass the spawn lock.

The acute case is a **version-skew double-writer**:

#### Gap 1: `t2_index_write` conflates "daemon down" with "daemon alive but version-skewed", opening a second writer in the latter case

`t2_index_write` (`src/nexus/mcp_infra.py:194`) catches **two** exceptions in a single `except` clause and treats them identically — close the half-open client and degrade to a direct `T2Database` writer:

```python
except (T2DaemonNotReachableError, T2SchemaVersionMismatchError):
    ...
    client = None
```

These two conditions have **opposite** single-writer implications:

- `T2DaemonNotReachableError` — **no** daemon writer exists. Degrading to a direct writer is *safe*; this is the intended RDR-128 availability fallback (documented-irreducible, `epsilon-allow` at `mcp_infra.py:218`).
- `T2SchemaVersionMismatchError` — a **stale-version daemon is alive**, holds the `<db>.spawn_lock`, and is **actively serving writes**. Opening a direct `T2Database` writer B against the same `memory.db` produces **two live writers**. This is a single-writer-invariant violation.

### Reachability (without `nx upgrade`)

A long-lived stale-version daemon `D_old` (e.g. 5.4.4 — the "orphan / long-lived MCP" class) keeps serving. A current-code client (`nx index repo`, `aspect_worker`, an MCP hook, post-reinstall 5.4.5) calls `t2_index_write` → `client.database.hello()` → `T2SchemaVersionMismatchError` → opens a direct writer B. Two writers, same DB. This needs **no** coordinated upgrade path; `nx upgrade` itself is safe (it quiesces, migrates, then cycles — serializing). Pure version skew between a stale daemon and a current client is the trigger, and the post-upgrade convergence window is exactly the window with the most write churn.

`t2_index_write` is the chokepoint for ~20 write sites (aspect_worker, enrich, collection, scratch, doctor, hooks), so the fault is concentrated at one function but has wide reach.

### Severity

MEDIUM strict / LOW practical. WAL byte-level writer locking prevents *corruption* (a current-code direct write against an old-schema DB ERRORs loud rather than silently corrupting; the direct `T2Database` in this path defaults `run_migrations=False`, so it does not migrate underneath the stale daemon). But it is a strict single-writer-invariant violation during the convergence window, and "WAL saves us" is the same posture RDR-128 was created to retire.

## Context

- RDR-128 established the single-writer invariant for `memory.db` and the daemon write-routing discipline. It explicitly documented the daemon-unreachable direct fallback as an irreducible availability tradeoff.
- RDR-129 hardened the serving path (deferred lock release to exit, etc.).
- RDR-140 added the supervisor: P1 cold-start fast-path + loser quiet-attach, P2 single-flight election, P3 ownership-aware wait-then-force reap, P4 crash-loop guard + restart-count status. RDR-140 *narrows* this residual via the P2 version-cycle (the next `ensure-running` reaps the stale daemon and restores daemon routing) but does not close it.
- The RDR-140 gate memo specced a "self-healing re-assert" as the intended closure mechanism for this boundary; it was deferred to an RDR-128 P3 successor (this RDR).

Relevant code:
- `src/nexus/mcp_infra.py:185-220` — `t2_index_write` and its fallback.
- `src/nexus/daemon/t2_client.py:57,62` — `T2DaemonNotReachableError`, `T2SchemaVersionMismatchError`.
- `src/nexus/commands/daemon.py:948` — `t2_ensure_running_cmd` (P2 election + P3 reap + P4 crash-loop guard).

## Research Findings

Carried from the RDR-140 deep-analyst e2e analysis (no new spikes required to frame the decision; Critical Assumptions below are the verification targets):

1. The spawn lock (`t2_daemon._acquire_spawn_lock`, `LOCK_EX|LOCK_NB`, db_path-scoped) is the daemon-vs-daemon backstop and is NOT in question here — the hole is strictly the non-daemon direct writer that never touches the spawn lock.
2. The version-mismatch arm is the *only* arm where a live competing writer is guaranteed to exist; the unreachable arm is the only arm where one is guaranteed absent. The current code conflates them.
3. `t2_ensure_running_cmd` already encapsulates reap-stale + spawn-current + election + crash-loop suppression. Re-asserting the supervisor from the client path is reuse of an existing, tested mechanism, not a new lifecycle.

## Proposed Solution

**Split the two exception arms in `t2_index_write` and make the version-mismatch arm self-healing.**

1. **Unreachable arm (`T2DaemonNotReachableError`)** — unchanged. Degrade to the direct `T2Database` writer. No daemon writer exists; this is the documented availability fallback.

2. **Version-mismatch arm (`T2SchemaVersionMismatchError`)** — do NOT open a second writer. Instead **re-assert the supervisor**:
   - Invoke `t2_ensure_running_cmd` (P2/P3: reap the stale daemon, spawn a current one). The stale daemon is serving stale-schema writes — reaping it is correct independent of this write.
   - Re-create the client and retry `hello()` against the freshly-spawned current daemon.
   - Route `write_fn` through the daemon.
   - **Bounded fallback**: if the re-assert cannot produce a reachable current daemon, fall back to the direct writer, logging at WARNING. Critically, the three abort triggers leave D_old in **different liveness states**, so they are NOT uniformly "safe" and must emit **distinct events**:
     1. **Crash-loop-tripped** (`daemon.py:1126`, `sys.exit(1)`): D_old was successfully reaped before D_new entered the crash-loop, so D_old is **dead** → genuine down-arm semantics, direct write is safe. Catch the `SystemExit` (P0) and degrade.
     2. **Write-lock-held abort** (`daemon.py:1065`, after the ~30s `_t2_db_write_lock_acquirable` budget): re-assert returns without reaping → D_old is **alive and writing**. Direct fallback = temporary double-writer (the RDR-128 documented-availability residual, WAL-non-corrupting, errors loud). Not down-arm.
     3. **SIGTERM-not-exited abort** (`daemon.py:1098`, after `_T2_CYCLE_EXIT_TIMEOUT=10s`): D_old SIGTERMed but PID not dead → **alive**. Same residual as (2).

This makes the dangerous arm converge to single-writer by construction, while preserving availability via the same bounded-fallback posture RDR-128 already accepts for the genuinely-down case.

### Why the in-flight-before-reap interleaving is NOT a second writer

A natural objection: client C2 calls `t2_index_write`, passes `hello()` against the stale D_old, and is *mid-write through D_old* exactly when our re-assert reaps D_old. Does that race produce a direct-writer-plus-D_new coexistence? **No.** C2 holds a `T2Client` (a daemon RPC client), not a direct `T2Database` writer. D_old's `stop()` awaits `wait_closed` (RDR-129 A2 deferred-release-to-exit), draining all in-flight RPCs before the process exits and before the spawn lock releases. So C2's write completes through D_old → D_old exits → spawn lock frees → D_new spawns and opens its `T2Database`. There is no instant where a writer connection outlives its process or coexists with D_new from this interleaving. The only direct writers this RDR is concerned with are the ones `t2_index_write` itself opens on the fallback path — which is precisely what the arm-split governs.

### Boundary conditions to nail in the plan

- **Re-entrancy / recursion**: the re-assert must not loop (re-assert → still mismatched → re-assert …). Cap to a single re-assert attempt per `t2_index_write` call; on a second mismatch, fall to the bounded-direct path.
- **Concurrency**: N clients hitting the mismatch arm simultaneously must not each spawn a reap. `t2_ensure_running_cmd`'s P2 election lock already collapses the thundering herd — confirm it covers the client-initiated re-assert path (CA-2).
- **Crash-loop interaction**: re-assert must respect `_crashloop_tripped`; a daemon that cannot stay up must not be respawned on every write (CA-3).
- **Latency**: a version-skewed write now blocks on reap+respawn. Worst case is bounded at ~55s (30s write-lock probe `_T2_CYCLE_DB_PROBE_TIMEOUT_MS` + 10s SIGTERM exit poll `_T2_CYCLE_EXIT_TIMEOUT` + 15s D_new reachability `timeout`), only if all three abort paths trigger sequentially. Acceptable because the skew window is transient and post-convergence every write is fast again. The plan confirms these constants against the code at implementation time.

## Implementation Plan

- **P0 (structural prerequisite, from CA-4)**: extract a non-Click inner function from `t2_ensure_running_cmd` — e.g. `_t2_ensure_running_inner(config_dir: Path, timeout: float) -> bool` (returns daemon-now-reachable) — and have the Click command delegate to it. The re-assert path calls the inner function, NOT the Click command (which `sys.exit`s in standalone mode). The re-assert must also be wrapped in `try/except SystemExit` defensively (the crash-loop guard path `sys.exit(1)`s — see CA-3) so guard activation degrades to direct rather than killing the calling MCP process.
- **P1 (RED)**: tests reproducing the version-skew double-writer — a stale-version daemon holding the spawn lock + a current client calling `t2_index_write`; assert exactly one live writer after the call (split-arm behavior), and assert the unreachable arm still degrades to direct.
- **P2**: split the `except` in `t2_index_write`; implement the version-mismatch self-healing re-assert with single-attempt cap + bounded fallback + distinct WARNING events.
- **P3**: confirm election-lock coverage of the client-initiated re-assert and crash-loop-guard interaction; add the concurrency + crash-loop regression tests.
- **Phase-review gate** cross-walk against this §Proposed Solution before close.

## Trade-offs

- **Latency on the skew window**: version-skewed writes block on a bounded reap+respawn instead of returning immediately via a (dangerous) second writer. Accepted.
- **Coupling**: the write path (`mcp_infra`) now invokes the supervisor (`commands/daemon`). This is a new dependency direction; the plan must confirm it introduces no import cycle and keep the coupling thin (call the existing command entry, do not re-implement reap).
- **Residual unreachable-arm coexistence**: the down-arm still admits N direct writers serialized only by WAL `busy_timeout` (RDR-128's documented availability tradeoff). This RDR does NOT change that — it is out of scope and remains documented-irreducible.
- **Version-mismatch arm cycle-deferred residual**: the write-lock-held abort and SIGTERM-not-exited abort (triggers 2 and 3 above) fire with D_old **alive**, so the direct-writer fallback opens a temporary second writer. This is NOT a new hole — it collapses to the RDR-128 documented-availability residual (WAL non-corrupting, old-schema direct writes error loud). The mechanism narrows the window from "immediate on any mismatch" to "only after ~55s of re-assert attempts," but does not fully close it for these two paths. Each must emit a **distinct** WARNING event so it is operator-visible and so the §Validation acceptance signal does not silently count a cycle-deferred fallback as a non-event.

## Alternatives Considered

- **Accept-and-document** — leave the conflated `except`, rely on WAL non-corruption + P2 version-cycle narrowing. Rejected: it is the "WAL saves us" posture RDR-128 exists to retire, and the violation sits in the highest-churn window.
- **Fail-loud on mismatch** — raise instead of degrading. Rejected: turns a transient skew into hard write failures across all ~20 sites; unacceptable availability hit for a window that self-heals in seconds.

## Test Plan

- Version-skew double-writer regression (RED first): stale daemon + current client → exactly one live writer.
- Unreachable arm unchanged: daemon down → single direct writer, WARNING emitted.
- Concurrency: N concurrent mismatch-arm calls → one reap (election lock holds), no writer pileup.
- Crash-loop interaction: tripped guard → no respawn storm; down-arm semantics.
- Re-assert single-attempt cap: persistent mismatch → bounded fallback, no recursion.

## Validation

Field signal: after release, the version-skew window self-heals to daemon routing (D_old reaped, D_new serving) with zero *immediate* second-writer fallbacks. The cycle-deferred fallbacks (triggers 2/3) are expected-but-rare and must be counted via their distinct WARNING events — the acceptance criterion is "zero immediate-mismatch direct writes," NOT "zero direct writes," since the ~55s-deferred residual is the accepted RDR-128 availability tradeoff. Distinguishing the events in telemetry is a release requirement, not optional.

## Critical Assumptions

_Verified 2026-06-01 (codebase-deep-analyzer; T2 `nexus_rdr/141-research-CA1-CA4`)._

- **CA-1 — VERIFIED**: The version-mismatch arm is reachable in production without `nx upgrade` (stale daemon holding the spawn lock + serving, current client → `hello()` raises mismatch → direct writer B). From the RDR-140 deep-analyst e2e analysis (`nexus/rdr140-single-writer-e2e-analysis-2026-05-31`).
- **CA-2 — VERIFIED**: `_acquire_election_lock(db_path, …)` is acquired unconditionally in `t2_ensure_running_cmd` (`commands/daemon.py:1024`), no caller-identity gate; `fcntl.LOCK_EX` (blocking) queues N concurrent client re-asserts, the holder re-discovers at `:1033` and attaches if a winner already spawned (`finally` release at `:1184`). Keyed on `db_path`, not caller. Nuance: on lock timeout it returns `None` and proceeds unguarded with a warning — the daemon's own `_acquire_spawn_lock` (`LOCK_NB`) is the hard backstop; the election lock is a perf guard, not the safety gate.
- **CA-3 — VERIFIED (with refinement)**: `_crashloop_tripped` is checked inside the election-lock `try` on every spawn-decision path (`commands/daemon.py:1108-1126`); suppressed calls do not increment the restart counter (`_record_restart` at `:1127` runs only on the actual spawn), so no death spiral. **Refinement**: when tripped it `sys.exit(1)`s → the re-assert call must catch `SystemExit` and degrade to direct, else guard activation kills the calling MCP process. Folded into P0.
- **CA-4 — VERIFIED (with refinement)**: No module-load cycle — `commands/daemon.py` imports only `nexus.config` at top level (everything else function-local); `mcp_infra.py` has no module-level `nexus.commands.*` import. A function-local import in `t2_index_write` is safe. **Refinement**: `t2_ensure_running_cmd` is a Click `Command` (`:921`), not directly callable (invoking it `sys.exit`s in standalone mode). Requires the P0 non-Click inner-function extraction. Current signature: `t2_ensure_running_cmd(config_dir_str: str|None, timeout: float=15.0, quiet: bool=False) -> None`.

## Finalization Gate

_Pending. Run `/conexus:rdr-gate` after Critical Assumptions are verified._

## References

- RDR-128 (single-writer invariant), RDR-129 (serving-path hardening), RDR-140 (supervisor & ownership model).
- T2: `nexus/rdr140-single-writer-e2e-analysis-2026-05-31`, `nexus/rdr140-deep-review-spirit-2026-05-31`, `nexus/5.6.0 field shakeout 2026-06-01`.
- Code: `src/nexus/mcp_infra.py:185-220`, `src/nexus/daemon/t2_client.py:57-62`, `src/nexus/commands/daemon.py:948`.
- Bead: nexus-fcfhz.

## Revision History

- 2026-06-01: Draft. Decision pre-settled with user as split-arms + self-healing re-assert (option 2 of 3); created as RDR-128 P3 successor for nexus-fcfhz.
- 2026-06-01: Research. CA-1..CA-4 verified (codebase-deep-analyzer); CA-3/CA-4 refinements added a P0 prerequisite (extract non-Click `_t2_ensure_running_inner` + catch `SystemExit`). Ready for gate.
- 2026-06-01: Gate PASSED (0 critical, 2 significant, 3 observations). Both significant findings absorbed: the bounded fallback has three triggers with different D_old liveness (only crash-loop-tripped is true down-arm; write-lock-held and SIGTERM-abort leave D_old alive → cycle-deferred residual), now enumerated in §Proposed Solution / §Trade-offs / §Validation with distinct-event requirement; worst-case latency quantified at ~55s. Critic confirmed the C2 in-flight-before-reap interleaving is safe (RPC client drained by `stop()→wait_closed`, not a direct writer) and the mixed-version ping-pong concern is moot (`_installed_version` resolves the same package version for all local processes).
