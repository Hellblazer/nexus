---
title: "OS-Init as the Single Process Watchdog: Retire the In-Process Storage-Supervisor Respawn Layer"
id: RDR-175
type: Technical Debt
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-28
accepted_date: 2026-06-28
related_issues: [nexus-1brzs]
related_rdrs: [RDR-149, RDR-152, RDR-161, RDR-174]
supersedes: []
related_tests: [tests/daemon/test_storage_service_daemon.py, tests/daemon/test_rdr149_lifecycle_conformance.py, tests/daemon/test_service_install.py, tests/test_init_cmd.py]
---

# RDR-175: OS-Init as the Single Process Watchdog

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The storage-service supervisor (`src/nexus/daemon/storage_service_daemon.py`,
1322 lines) carries an in-process restart/respawn layer — `_respawn`, a windowed
restart budget (`_MAX_RESTART_ATTEMPTS=3`, `_RESTART_WINDOW_HEARTBEATS=300`,
`_RESTART_BACKOFF`), and stuck-process detection (`_MAX_UNHEALTHY_HEARTBEATS`).
The restart *mechanism* in that layer (`_respawn` + the windowed budget) was
built in RDR-152 Phase 5 as a "v1 reliability requirement" when the only way to
run the service was a bare detached subprocess with **no OS supervisor above
it**. RDR-174 introduces launchd/systemd autostart units — an OS-level process
watchdog — making the in-process restart mechanism duplicated work and the root
cause of a verified double-spawn hazard. (The stuck-process *detection* is
retained — see §Approach — since the OS watchdog only sees process death.)

Now that the Java storage service is singular and local-install-only, the
right model is: **OS init is the single process watchdog**; the supervisor
becomes a thin start-publish-heartbeat-die process.

### Enumerated gaps to close

#### Gap 1: Two restart layers do the same job

The in-process restart mechanism (`_respawn` + budget) and the RDR-174 OS-init
units (launchd `KeepAlive`+`ThrottleInterval`, systemd `Restart=on-failure`) both
restart a dead service. The OS layer is strictly more capable (never-give-up vs
the in-process "3 attempts then permanently abandon"). The duplication is ~115
lines of production code plus ~185 lines of tests (the stuck-process *detection*
test class is rewritten, not deleted, since detection is retained), and it
diverges in semantics (the in-process budget can give up while the OS layer would
keep trying).

#### Gap 2: The in-process respawn is the root cause of the double-spawn hazard

`install_autostart` activates the unit immediately (`installer.py:157-158`,
`launchctl bootstrap` / `systemctl --user enable --now`). When a session
supervisor already holds the lease and the unit then starts, the unit's
`run_storage_supervisor` → `_start_locked:805` short-circuits on the live lease
leaving `self._proc = None`; the first `heartbeat_once:927` returns
`(False, False)`; the supervise loop calls `_respawn:1217`, which spawns a
**second** `nexus-service` process and bumps the lease generation. Two live
service processes. Deleting `_respawn` closes this at its root — no bespoke
arbiter needed.

#### Gap 3: The gate-locked P2.4 ordering creates the two-supervisor situation

RDR-174 §4 P2.4 (`init.py:523-530`, gate-locked) prompts for autostart *after*
a successful `provision_and_start_service` — i.e. a session supervisor is
already running and holding the lease before autostart is decided. That ordering
is the proximate cause of Gap 2. It must be reworked to decide autostart first,
so a session supervisor is never started underneath a unit.

#### Gap 4: systemd lacks never-give-up parity

The systemd unit (`conexus/daemon/nexus-service.service`) ships
`Restart=on-failure` but no `StartLimit*` override. systemd's defaults
(`StartLimitIntervalSec=10s`, `StartLimitBurst=5`) drive the unit into a
`failed` state after >5 restarts/10s — systemd gives up where launchd
(`ThrottleInterval=30` + `KeepAlive`) does not. For parity the unit needs
`StartLimitIntervalSec=0`.

## Context

### Background

Discovered during RDR-174 P2.3 (`nexus-1brzs`, "supervisor handoff"). Verifying
the gate-locked premise surfaced that the supervisor's reliability machinery
predates autostart units and now duplicates them. Two parallel analyses
(codebase-deep-analyzer cruft inventory + deep-analyst minimal-watchdog design,
2026-06-28) converged on the design below. Full record: T2
`nexus/rdr-174-p23-supervisor-minimization-analysis.md`.

### Technical Environment

- `src/nexus/daemon/storage_service_daemon.py` — the supervisor (target of the cut).
- `src/nexus/daemon/service_registry.py` (775 lines) — **shared substrate** across
  t1/t2/t3/storage_service/plan tiers; the RDR-149 leased/fenced/atomic registry.
  Its lease/generation/election machinery is load-bearing for every daemon and
  protected by the RDR-149 lifecycle gate (`tests/daemon/test_lifecycle_gate.py`).
  **Out of scope to change.**
- `conexus/daemon/{com.nexus.service.plist,nexus-service.service}` — the RDR-174
  autostart units (the OS watchdog).
- `src/nexus/commands/daemon.py` (`ensure_storage_supervisor:1592`,
  `service_start_cmd:1687`), `src/nexus/commands/init.py`
  (`provision_and_start_service:321`, the P2.4 dispatch).

## Research Findings

### Investigation

Two-agent analysis over the supervisor, the registry consumption, the autostart
units, the init dispatch, and the RDR-149 conformance battery. Every claim below
is file:line-cited in the T2 analysis note.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| systemd unit semantics | Yes (docs) | `Restart=on-failure`+`RestartSec` restart on non-zero exit; default `StartLimitBurst=5/10s` enters `failed` — needs `StartLimitIntervalSec=0` for never-give-up. |
| launchd plist semantics | Yes (existing unit) | `KeepAlive=true`+`ThrottleInterval=30`+`ExitTimeOut=20` = never-give-up, ≥30s apart. Strict superset of the in-process budget. |
| `service_registry` (RDR-149) | Yes (source) | `discover`/`publish`/`heartbeat`/lease-generation are shared substrate; `self_heal[storage_service]=pass` is satisfied by the lease re-stamp, not by `_respawn`. |

### Key Discoveries

- **Verified** — In-process respawn is reachable ONLY from
  `_supervise_until_stopped` ← `run_storage_supervisor` ← `service start
  --foreground` (the path the OS unit runs). No other caller needs it
  (`start_storage_service` is one-shot; `ensure_storage_supervisor` detaches and
  polls, then returns).
- **Verified** — The double-spawn hazard (Gap 2) reproduces through the
  short-circuit→`_proc=None`→`heartbeat_once (False,False)`→`_respawn` chain.
- **Verified** — `stop()` intentionally does NOT stop PG (`storage_service_daemon.py:997`),
  so PG survives a supervisor bounce; a restarted supervisor's `_ensure_pg_running`
  no-ops on the already-running cluster.
- **Documented** — launchd is a strict superset of the in-process budget;
  systemd needs one directive for parity (Gap 4).
- **Verified** — The RDR-149 conformance battery's storage_service rows are
  satisfied by the shared registry primitive, not by `_respawn` — so the cut is
  RDR-149-orthogonal.
- **Verified (research pass 1, 2026-06-28)** — The dead-lease liveness primitive
  for heal-on-next-use ALREADY EXISTS. `_publish:684` stamps
  `payload={"supervisor_pid": os.getpid()}` on every supervised lease, and
  `stop_storage_service:1288-1309` already reads `record.payload["supervisor_pid"]`
  and gates on `_pid_is_alive`. The heal-on-next-use hardening is therefore ~5
  lines in `ensure_storage_supervisor` reusing that exact pattern (after
  `discover()` returns a fresh lease, if `supervisor_pid` is dead →
  `registry.relinquish` + fall through to spawn). It lives in the storage-specific
  caller, NOT in the shared `service_registry.discover`, so it is RDR-149-gate-safe.
  storage_service TTL is 15s (`service_registry.TIER_TTLS`), so the un-hardened
  gap is already bounded at ≤15s; the check makes heal instant. Residual:
  pid-reuse could read a recycled pid as "alive," but pid is only a liveness HINT
  layered on TTL-freshness + the uuid `owner_token` identity (RDR-149 chose
  owner_token over pid precisely for reuse immunity), so the worst case is a
  ≤15s heal delay (fall back to TTL), never a correctness error. A non-supervised
  lease (no `supervisor_pid`) falls back to current TTL-freshness behavior.

### Critical Assumptions

- [ ] **The OS watchdog restart of the whole supervisor process is an acceptable
  substitute for in-process respawn** — **Status**: Verified — **Method**: Source
  Search (unit directives + `start()` re-runs `_ensure_pg_running`; PG survives
  the bounce).
- [ ] **No production caller other than the `--foreground` path needs in-process
  respawn** — **Status**: Verified — **Method**: Source Search (caller graph).
- [ ] **Deleting `_respawn` does not regress the RDR-149 conformance battery** —
  **Status**: Verified — **Method**: Source Search (conformance rows map to the
  shared registry primitive).
- [x] **The heal-on-next-use no-autostart contract needs a dead-lease liveness
  check** — **Status**: Verified (research pass 1) — **Method**: Source Search.
  The primitive exists (`_publish:684` stamps `supervisor_pid`;
  `stop_storage_service:1288-1309` already gates on `_pid_is_alive`). Hardening is
  ~5 lines in `ensure_storage_supervisor`, RDR-149-gate-safe; the un-hardened gap
  is bounded at the 15s storage_service TTL. No spike required.

## Proposed Solution

### Approach

OS init is the single process watchdog. The supervisor collapses to:

```
start():  _ensure_pg_running → _spawn_service → _wait_for_service_ready → _publish   (unchanged)
loop:     running, pg_ok = heartbeat_once()
          if not (running and pg_ok):  log → return non-zero   # EXIT; OS restarts the whole process
          else:                        sleep(DEFAULT_HEARTBEAT_INTERVAL)
```

Delete the in-process restart **mechanism**: `_respawn`,
`_maybe_reset_restart_budget`, constants `_MAX_RESTART_ATTEMPTS` /
`_RESTART_BACKOFF` / `_RESTART_WINDOW_HEARTBEATS`, the restart-budget state
(`_restart_count`, `_clean_heartbeats_since_restart`), and the respawn branch of
`_supervise_until_stopped`.

**Retain the stuck-process DETECTION** (`_MAX_UNHEALTHY_HEARTBEATS` +
`_consecutive_unhealthy_heartbeats`, `heartbeat_once:949-970`). This is a health
*signal*, not a restart *action*: a wedged-but-alive Java process (HTTP 503,
connection-pool exhaustion, deadlock) stays process-alive, so the OS watchdog —
which only sees process death — would never catch it without this signal. The
change is in the *action*: on threshold breach `heartbeat_once` returns a falsey
`running` signal and the minimal loop **exits non-zero** so the OS restarts the
whole supervisor (vs. the current in-process Java-only respawn). Transient
non-200s below the threshold are still tolerated (lease not re-stamped; no exit).

Keep `_ensure_pg_running`, lease publish/heartbeat, `_wait_for_service_ready`,
loud-fail-on-no-binary, the `stop()` triad, and the `(True, False)` PG-restart
arm (valid under an OS watchdog — the OS supervises the supervisor process, not
PG; the Java service stays alive through a PG-only restart, unchanged from today).

**Contracts:**

- **Autostart installed (the default)** → OS init restarts the supervisor on any
  non-zero exit. Add `StartLimitIntervalSec=0` to the systemd unit for
  never-give-up parity with launchd (Gap 4).
- **Autostart declined** → heal-on-next-use. A crashed supervisor leaves no fresh
  lease; the next `nx` command re-spawns it via `ensure_storage_supervisor`'s
  existing discover-then-spawn idempotency + lease TTL. The deliberate
  non-persistence choice gets best-effort. **Required hardening:** the discover
  path must detect a stale-but-fresh lease left by a hard crash (OOM-kill, no
  relinquish) and re-spawn, rather than returning a dead endpoint for up to the
  TTL window (`ensure_storage_supervisor:1601` currently trusts TTL-freshness,
  not process-liveness). Reuse the exact guard from `stop_storage_service:1293` —
  `isinstance(supervisor_pid, int) and supervisor_pid > 0 and _pid_is_alive(...)`
  — so an absent `supervisor_pid` (a non-supervised/legacy lease) falls through to
  current TTL-freshness behavior rather than re-spawning spuriously.
- **Autostart ordering rework (Gap 3)** → decide autostart first; never start a
  session supervisor under a unit. If autostart=yes → install the unit (sole
  starter) → poll the lease for readiness. If autostart=no →
  `ensure_storage_supervisor` session detach. Exactly one supervisor in either
  branch.

  > **DEFERRED 2026-06-28 (implementation-time premise check).** Gap 3 assumed
  > `nx init` already prompts for autostart and installs a unit (RDR-174 P2.4)
  > whose ordering needs reworking. Implementation-time verification found P2.4
  > was **never landed**: `init.py:523-530` is a placeholder comment only; `nx
  > init` has no autostart prompt and never installs a unit (it unconditionally
  > session-detaches via `ensure_storage_supervisor`). The standalone `nx daemon
  > service install --autostart` command exists but `init` does not call it.
  > There is therefore no extant ordering to rework, and the "two-supervisor
  > situation" cannot occur in current code (init never starts a unit). The
  > double-spawn root cause (Gap 2) was the `--foreground` → `_respawn` chain,
  > already removed in Step 1. Building a decide-first autostart prompt in `init`
  > from scratch is net-new feature scope (effectively implementing RDR-174 P2.4)
  > and is preventive hardening for a path that does not yet exist. Deferred to a
  > follow-up bead, to be implemented together with the autostart-in-init prompt
  > if/when that lands. The heal-on-next-use hardening (the bullet above) and
  > Steps 1 + 2 are unaffected and shipped.

### Technical Design

Interfaces / contracts (verify signatures at implementation):

- `run_storage_supervisor(config_dir) -> int` — unchanged signature; loop body
  reduced to start + heartbeat + die-on-failure. Non-zero exit codes retained as
  the OS-restart signal (3 = service-unrecoverable, 4 = PG-unrecoverable).
- `heartbeat_once() -> tuple[bool, bool]` — keeps `(service_running, pg_ok)`;
  drops the stuck-process counter (a non-200 health beat now contributes to
  `service_running=False` directly per the chosen threshold, or is folded into
  the existing `_service_healthy()` semantics — settle at implementation).
- Init dispatch — split `provision_and_start_service` so the autostart decision
  precedes the supervisor start (decide-first ordering).

```text
// Illustrative — verify during implementation
loop: running, pg_ok = sup.heartbeat_once()
      if not (running and pg_ok): return EXIT_SERVICE_DOWN  // OS restarts
      sleep(interval)
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Minimal supervise loop | `storage_service_daemon._supervise_until_stopped` | Replace (reduce to start+heartbeat+die) |
| OS restart | `conexus/daemon/*` units (RDR-174) | Reuse; add systemd `StartLimitIntervalSec=0` |
| Single-writer / lease | `service_registry` (RDR-149) | Reuse unchanged (out of scope) |
| Session heal | `ensure_storage_supervisor` discover-then-spawn | Extend (add dead-lease liveness check) |
| Autostart ordering | `init.py` P2.4 dispatch | Replace (decide-autostart-first) |

### Decision Rationale

The in-process budget can permanently abandon the service (3 failures/window);
the OS watchdog never does. One watchdog is simpler, more resilient, and removes
the double-spawn class entirely. The registry lease already provides
single-writer arbitration, so no bespoke handoff machinery is needed.

## Alternatives Considered

### Alternative 1: Keep a thin session-only respawn

**Description**: Retain in-process respawn but only on the bare-detach path; add
a mode flag so the OS-hosted path does not respawn.

**Pros**:
- No behavior regression for users who run without autostart.

**Cons**:
- Deletes almost nothing; adds a mode flag and two argv variants to keep in sync
  against the "single persistent-start path" rule (`nexus-qke1e`).
- Does not close the double-spawn at its root.

**Reason for rejection**: It is the opposite of the goal (cut the cruft) and
leaves the hazard. Hal chose heal-on-next-use.

### Briefly Rejected

- **Autostart mandatory**: `systemctl --user enable --now` has no session bus in
  headless/CI/SSH-no-linger; breaks `guided_upgrade` and migration-rehearsal
  (which get a service today without a unit). `install_autostart` already carries
  `ActivationError`+`force` for exactly this reason.

## Trade-offs

### Consequences

- (+) ~115 prod + ~185 test lines removed; one restart layer; double-spawn class gone.
- (+) Never-give-up restart (better than 3-attempts-then-abandon).
- (−) A stuck-but-alive Java process (HTTP 503 past the unhealthy threshold:
  connection-pool exhaustion, deadlock, sustained GC) that previously triggered a
  Java-only in-process respawn (sub-5s) now triggers a full supervisor exit →
  OS-restart cycle (PG `_ensure_pg_running` no-op + Java spawn + `_wait_for_service_ready`
  up to 60s). Accepted: stuck-process events are rare and the full cycle is still
  bounded; transient non-200s below the threshold are still tolerated (no exit).
- (−) No-autostart mode loses continuous self-heal; gains heal-on-next-use.
- (=) PG-only failure is UNCHANGED — the `(True, False)` arm restarts PG directly
  without bouncing the alive Java service.

### Risks and Mitigations

- **Risk**: Heal-on-next-use returns a dead endpoint for up to the lease TTL after
  a hard crash. **Mitigation**: add a dead-lease liveness check to the discover
  path (Critical Assumption 4); under autostart this is moot (instant OS restart).
- **Risk**: systemd enters `failed` after a restart burst. **Mitigation**:
  `StartLimitIntervalSec=0`.
- **Risk**: Reworking the gate-locked P2.4 ordering regresses the autostart prompt
  invariant. **Mitigation**: the decide-first flow preserves "prompt for autostart"
  semantics; only the start ordering moves. Stacked review at the phase boundary.

### Failure Modes

- Service dies → supervisor exits non-zero → OS restarts (visible in unit logs).
  No autostart → service down until next `nx` command (heal-on-next-use).
- PG unrecoverable → exit 4 → OS restart retries PG bring-up.
- Diagnose via `nx service probe`, unit status (`systemctl --user status` /
  `launchctl print`), and `<config_dir>/logs/storage_service.log`.

## Implementation Plan

### Prerequisites

- [x] Critical Assumption 4 (dead-lease liveness) settled by source search
  (research pass 1) — primitive exists, hardening is ~5 lines, no spike needed.
- [ ] RDR gated + accepted (this RDR) — supersedes the RDR-174 §4 P2.3/P2.4
  gate-locked text for the supervisor handoff.
- [ ] On acceptance, add a supersession breadcrumb to RDR-174 §Approach §4 (the
  accepted RDR keeps its status; the note records that the P2.3/P2.4
  supervisor-handoff/ordering text is superseded by RDR-175 Phase 1 Step 3) so
  RDR-174's gate-locked text is not left silently stale.

### Minimum Viable Validation

A single-supervisor integration test: provision + start, then install autostart
(activates the unit); assert **exactly one** lease and **one** `nexus-service`
process afterward (no double-spawn). This is the regression proof of the minimal
design and subsumes the original `nexus-1brzs` single-lease test.

### Phase 1: Code Implementation

#### Step 1: Reduce the supervise loop + delete the restart mechanism (TDD)
Rewrite `_supervise_until_stopped` to start+heartbeat+die-non-zero; delete
`_respawn`, `_maybe_reset_restart_budget`, the restart-budget constants/state.
RETAIN the stuck-process detection in `heartbeat_once` but wire its threshold
breach to the falsey-`running` return (→ loop exit), not to `_respawn`. Delete the
restart-budget/auto-restart test classes; REWRITE the stuck-process test class to
assert exit-non-zero (not in-process respawn); add the minimal-loop tests.

#### Step 2: systemd never-give-up parity
Add `StartLimitIntervalSec=0` to `conexus/daemon/nexus-service.service`; assert
via the rendered-unit test that BOTH `StartLimitIntervalSec=0` and the existing
`SuccessExitStatus=143` are present (the edit must not drop the graceful-stop
directive).

#### Step 3: Decide-autostart-first ordering (Gap 3) + heal-on-next-use hardening
Add the dead-lease liveness check to `ensure_storage_supervisor`'s discover path
(SHIPPED). The decide-autostart-first init ordering rework is **DEFERRED** to a
follow-up bead — see the DEFERRED note in §Approach: RDR-174 P2.4 (the
autostart-in-init prompt this would reorder) was never implemented, so there is
no extant ordering to rework and no two-supervisor path in current code.

### Phase 2: Operational Activation

N/A — no new shared infra; the units already ship (RDR-174).

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| autostart unit | `nx daemon service status` (RDR-174) | same | `nx daemon service uninstall` | unit-render test | N/A (templated) |

No new persistent resource is created by this RDR.

### New Dependencies

None.

## Test Plan

- **Scenario**: Service process killed under autostart — **Verify**: supervisor
  exits non-zero; OS restarts it; lease re-published; exactly one process.
- **Scenario**: Install autostart while a session supervisor runs — **Verify**:
  exactly one lease, one `nexus-service` (the MVV; no double-spawn).
- **Scenario**: No-autostart, supervisor hard-crashes (no relinquish) — **Verify**:
  next `nx` command re-spawns (dead-lease detected, not a dead endpoint returned).
- **Scenario**: PG-only death under the minimal loop — **Verify**: the
  `(True,False)` arm calls `_ensure_pg_running()` directly WITHOUT supervisor exit;
  the Java service stays alive through the PG restart; exactly one `nexus-service`
  process before and after (semantics locked in §Approach, not re-decided here).
- **Scenario**: Stuck-but-alive Java (HTTP 503 ≥ `_MAX_UNHEALTHY_HEARTBEATS`) —
  **Verify**: supervisor exits non-zero (no in-process respawn); below-threshold
  503s do NOT exit.
- **Scenario**: Rendered systemd unit — **Verify**: BOTH `StartLimitIntervalSec=0`
  AND the existing `SuccessExitStatus=143` present (the edit must not drop the
  graceful-SIGTERM-stop directive).

## Validation

### Testing Strategy

1. **Scenario**: MVV single-supervisor integration test. **Expected**: one lease,
   one process after install-with-autostart.
2. **Scenario**: RDR-149 conformance battery for `storage_service`. **Expected**:
   unchanged (pass) — proves the cut is registry-orthogonal.

### Performance Expectations

N/A — net code reduction; no throughput target.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution. The cut is consistent with RDR-149's "lean on the lease"
principle and RDR-174's OS-init watchdog direction.

### Assumption Verification

All four Critical Assumptions are Verified by source search (research pass 1
settled the dead-lease-liveness assumption: the `supervisor_pid` + `_pid_is_alive`
primitive already exists in `_publish` and `stop_storage_service`; the hardening
is ~5 lines in `ensure_storage_supervisor`, RDR-149-gate-safe, no spike needed).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `KeepAlive`/`ThrottleInterval` | launchd | Docs Only (existing unit in production) |
| `Restart`/`StartLimitIntervalSec` | systemd | Docs Only |
| `discover`/`publish`/`heartbeat` | service_registry (RDR-149) | Source Search |

### Scope Verification

The MVV (single-supervisor integration test) is in scope and is Phase 1's
deliverable, not deferred.

### Cross-Cutting Concerns

- **Versioning**: N/A (no schema/version change).
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: central — this RDR is about the deployment/watchdog model;
  the units already ship via RDR-174.
- **IDE compatibility**: N/A.
- **Incremental adoption**: autostart remains opt-in; no-autostart degrades to
  heal-on-next-use.
- **Secret/credential lifecycle**: N/A (no new secrets).
- **Memory management**: N/A (removes code; PG/heap bounds unchanged).

### Proportionality

Right-sized: the document is scoped to the supervisor cut + the three dependent
deltas (systemd parity, ordering rework, heal hardening). The JAR/JVM dev-test
escape hatch removal (~103 lines) is deliberately excluded to keep focus.

## References

- T2 `nexus/rdr-174-p23-supervisor-minimization-analysis.md` (the two-agent analysis).
- RDR-152 §Approach pt5 (supervisor origin), RDR-149 (registry substrate),
  RDR-161 (native-only), RDR-174 (autostart units).
- `src/nexus/daemon/storage_service_daemon.py`, `src/nexus/commands/daemon.py`,
  `src/nexus/commands/init.py`, `conexus/daemon/{com.nexus.service.plist,nexus-service.service}`.

## Revision History

- 2026-06-28 — draft created from the RDR-174 P2.3 supervisor-minimization
  analysis (brainstorming-gate approved; heal-on-next-use contract chosen by Hal).
