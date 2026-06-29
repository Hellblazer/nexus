# RDR-175 Post-Mortem: OS-Init as the Single Process Watchdog

**Closed:** 2026-06-28 · **Reason:** implemented · **Type:** Technical Debt
**Epic:** nexus-m3mle (closed) · **Author:** Hal Hildebrand

## Outcome

Phase 1 shipped. OS init (launchd/systemd autostart units from RDR-174) is now
the single process watchdog for the storage-service supervisor. The in-process
restart mechanism is retired; on failure the supervisor exits non-zero and the
OS restarts the whole process.

## What shipped (vs the accepted plan)

| Item | Bead | Outcome |
| --- | --- | --- |
| Prereq: RDR-174 §4 supersession breadcrumb | nexus-xsqb1 | Shipped |
| Step 1: reduce supervise loop + delete restart mechanism | nexus-9ff8l | Shipped — deleted `_respawn`, `_maybe_reset_restart_budget`, `_MAX_RESTART_ATTEMPTS`/`_RESTART_BACKOFF`/`_RESTART_WINDOW_HEARTBEATS`, `_restart_count`/`_clean_heartbeats_since_restart`. Stuck-process DETECTION retained (action → exit non-zero). `(True,False)` PG-only arm preserved. |
| Step 2: systemd `StartLimitIntervalSec=0` | nexus-ckwhd | Shipped (kept `SuccessExitStatus=143`) |
| Step 3B: heal-on-next-use dead-lease guard | nexus-1f3lv | Shipped in `ensure_storage_supervisor` |
| MVV: single-supervisor / no-double-spawn | nexus-56o1u | Shipped (subsumes nexus-1brzs) |
| Reviews: code-review-expert + substantive-critic | nexus-5pdef, nexus-9hoyj | Both ran; findings addressed |
| Phase-review-gate cross-walk | nexus-8spnp | PASS (manual; parser can't read bullet-contract §Approach) |
| Test-validator: RDR-149 orthogonality | nexus-bz744 | PASS — conformance + lifecycle gate UNCHANGED, 636 affected-suite passed |

## Divergence: Gap 3 dropped as void (premise error)

The single material divergence. RDR-175 §Approach Step 3 originally had two parts:
3A (decide-autostart-first init ordering, addressing Gap 3) and 3B
(heal-on-next-use). Only 3B shipped.

Gap 3 asserted: "the gate-locked P2.4 ordering creates the two-supervisor
situation," citing `init.py:523-530` as a prompt-then-start path needing
reordering. **Implementation-time verification found this was bad reasoning:**
those lines are a placeholder comment, not code. RDR-174 P2.4 (an autostart
prompt in `nx init`) was never implemented; `nx init` has no autostart prompt
and never installs a unit (it unconditionally session-detaches via
`ensure_storage_supervisor`). The standalone `nx daemon service install
--autostart` command exists but `init` does not call it.

Consequence: there is no extant ordering to rework and no two-supervisor
situation in current code. "Decide-autostart-first" would fix a non-problem.
The actual double-spawn root cause (Gap 2) was the `--foreground` → `_respawn`
chain, removed in Step 1. Gap 3 was therefore dropped as **void** (a premise
error), not deferred — the RDR §Approach/§Step 3/§Risks/§Consequences were
re-marked VOID, and a transient follow-up bead (nexus-shkww) plus a
"coexistence crash-loop" consequence that had been written up around the false
premise were deleted as artifacts of the same bad reasoning.

## Minor divergence: exit-code narrowing (substantive-critic SIG-3)

Exit 4 (PG-unrecoverable) is now emitted only from the `(True,False)` PG-only
arm. A simultaneous service+PG death exits 3, and a permanently-broken PG on the
OS-restart's `start()` surfaces as exit 1. Under `StartLimitIntervalSec=0` /
launchd `KeepAlive` all of 1/3/4 restart, so this affects log-based triage only,
not recovery. Documented in the `run_storage_supervisor` docstring and RDR
§Consequences.

## Lessons

- **Verify architectural premises against the actual code before implementing —
  including premises locked into an accepted RDR.** Gap 3 cited a line range as
  if it were behavior; it was a comment. The finalization gate did not catch
  this because the cited code was not re-read at gate time. A premise that names
  a `file:line` is worth opening that file before building on it.
- **A premise error is "void," not "deferred."** When the problem an item
  addresses does not exist, drop the item and delete the tracking artifacts —
  do not file a follow-up bead that institutionalizes a phantom.
- **The stacked reviewers caught different real things.** code-review-expert
  surfaced a silent-suppress loudness gap (relinquish failure). substantive-critic
  surfaced the exit-4 narrowing and pressure-tested the Gap 3 deferral hard
  enough to expose it as a premise error rather than legitimate deferral.
