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

## Divergence: Step 3A (Gap 3) handed to RDR-174 P2.4 / nexus-3pfj0

The single material divergence. RDR-175 §Approach Step 3 had two parts: 3A
(decide-autostart-first init ordering, addressing Gap 3) and 3B
(heal-on-next-use). Only 3B shipped inside RDR-175. 3A is a real requirement
that cannot be built here because the `nx init` autostart prompt it orders does
not yet exist — it is RDR-174 P2.4, the open planned bead `nexus-3pfj0`. The
decide-first requirement is therefore recorded ON `nexus-3pfj0` (install
decide-first; never start a session supervisor under a unit), to land with the
code that needs it.

### Close-time error (caught by Hal, corrected same day)

This divergence was, for several hours during close, mis-recorded as a **void
premise error** — the claim being that Gap 3 was "bad reasoning" because P2.4
"was never implemented." That conclusion was wrong, and the disconfirming
evidence (`nexus-3pfj0`, the open planned P2.4 bead) was visible in the
rdr-close preamble's Active Beads list the whole time. "Not yet implemented" was
conflated with "abandoned." On that bad basis a tracking bead (`nexus-shkww`)
was deleted, the RDR §Approach/§Step 3/§Risks/§Consequences were re-marked VOID,
and the real coexistence-crash-loop consequence was struck. Hal caught it by
asking what `nexus-3pfj0` was and why it had not been surfaced during the
determination. All of it was reverted: Gap 3 restored as a real forward
requirement, the requirement re-homed onto `nexus-3pfj0`, the coexistence
consequence restored and kept pinned by a regression test.

The root failure: an irreversible-ish determination (delete a bead, rewrite +
close an RDR) was made on an incomplete premise without the one `bd` lookup
(the RDR-174 epic's open beads) that would have flipped it — the exact
silent-premise class the RDR discipline exists to prevent.

## Minor divergence: exit-code narrowing (substantive-critic SIG-3)

Exit 4 (PG-unrecoverable) is now emitted only from the `(True,False)` PG-only
arm. A simultaneous service+PG death exits 3, and a permanently-broken PG on the
OS-restart's `start()` surfaces as exit 1. Under `StartLimitIntervalSec=0` /
launchd `KeepAlive` all of 1/3/4 restart, so this affects log-based triage only,
not recovery. Documented in the `run_storage_supervisor` docstring and RDR
§Consequences.

## Lessons

- **"Not yet implemented" is not "abandoned" — check the plan, not just the code.**
  The close-time error came from verifying that the `nx init` autostart prompt
  was absent from the *code* (true) and leaping to "the requirement is void"
  (false). One `bd` lookup of the RDR-174 epic's open beads would have surfaced
  `nexus-3pfj0` (the planned P2.4 prompt) and shown Gap 3 was a real
  forward-coordination requirement, not bad reasoning. When an item references
  another RDR's phase, read that RDR's bead graph before declaring it void.
- **An irreversible determination needs a complete premise first.** Deleting a
  bead and rewriting + closing an RDR are hard to walk back. Those actions were
  taken on an incomplete premise with the disconfirming evidence in plain view
  (the rdr-close preamble listed `nexus-3pfj0`). Surface the contradicting
  artifact *during* the determination, not after the close.
- **A user agreeing ("moot, bad reasoning") is not premise verification.** The
  void framing was amplified by a quick agreement; the agreement was based on the
  same incomplete premise. Agreement does not substitute for the `bd`/code check.
- **The stacked reviewers caught real things.** code-review-expert surfaced a
  silent-suppress loudness gap (relinquish failure). substantive-critic surfaced
  the exit-4 narrowing AND the coexistence crash-loop (SIG-1) — which was correct
  and was wrongly struck during the void error, then restored.
