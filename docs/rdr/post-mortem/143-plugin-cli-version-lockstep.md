# Post-Mortem: RDR-143 Plugin↔CLI Version Lockstep

**Closed:** 2026-06-02 (implemented)
**Shipped:** PR #1077, squash `9abd6e7e` on `develop`
**Epic:** `nexus-cuocb` (P1.1-P1.9, all closed)

## What shipped

A `SessionStart` (matcher `startup`) hook that keeps the installed `nx` CLI in lockstep with the conexus plugin version. On detected skew the hook emits an `additionalContext` nudge and dispatches a detached action that performs the extras-preserving two-command upgrade (`uv tool upgrade conexus`, then `nx upgrade`) and records a per-user marker (`~/.config/nexus/cli_lockstep_marker`) only on confirmed success. Single phase, Shape B, as locked.

- `conexus/hooks/scripts/version_lockstep_hook.py` (stdlib-only, fail-safe, never blocks startup, never writes the marker).
- `conexus/hooks/scripts/version_lockstep_action.py` (detached; uv-receipt editable gate, ordered upgrade, marker-on-confirmed-lockstep-only).
- Dedicated `SessionStart`/`startup` block in `conexus/hooks/hooks.json`.
- 42 tests across hook + action.

## What went right

- **TDD held end-to-end.** Tests for both scripts were written and run red before either script existed, then driven to green. No retrofitted tests.
- **The continuation handoff was load-bearing.** Locked contracts (marker-write-on-confirmed-upgrade, inline editable gate, two-command order, CA-4 detach) were carried verbatim into the enriched beads, so implementation was straight execution with no design churn.
- **Stacked review caught two real defects that line-level review and green tests both missed** (see below).

## What the gate caught (and a clean session would have shipped without)

Both came from the substantive-critic pass, after code-review-expert had already APPROVED and all tests were green:

1. **Matcher deviation (Significant).** The hook was first wired into the shared `startup|resume|clear|compact` SessionStart block. The RDR locks matcher `startup`. In the shared block the nudge re-fired on every `/clear` and `/compact` within a session, contradicting the "next session" message. Fix: a dedicated `startup`-only block.
2. **Downgrade nudge-loop (Significant).** The post-upgrade confirmation used strict `==`. If the plugin ref is pinned back below the installed CLI, `uv tool upgrade` can never reach the older target, so the marker would never be written and the nudge would fire forever. Fix: `>=` (`satisfies`) semantics in both the fast path and the confirm, so a CLI at or above the target records lockstep and goes quiet.

code-review-expert independently caught two Important items: a module-level `int()` on a timeout env var that could raise before `main()`'s fail-safe guard (fixed with `_env_int`), and a dispatch test that asserted only the absence of `wait()` without pinning the detach contract (now asserts `start_new_session` + all three stdio handles `DEVNULL`).

## Lesson reinforced

The two Significant catches are the recurring pattern: **green tests + an approving line-level review are not sufficient before a phase close.** Both defects were spec-alignment / edge-behavior issues invisible to the passing suite. The stacked-reviewer discipline (code-review-expert AND substantive-critic, the critic briefed with the locked spec) earned its keep again, consistent with the RDR-004 / RDR-010 precedents.

## Process notes worth keeping

- **The phase-review-gate tool keys on `### Approach`; this RDR uses `## Implementation Plan`.** The automated enumerate/validate could not parse the heading, so the cross-walk was done manually (recorded in T2 `nexus_rdr/143-phase1-review-gate`). Candidate follow-up: teach the gate to also recognize `## Implementation Plan`, or standardize the heading.
- **The close gate's "Active Beads" advisory lists all open beads project-wide, not RDR-scoped.** At close time 20 unrelated beads (RDR-142/140/misc) tripped the warning even though every RDR-143 bead was closed. The gate is doing its job (forcing a human ack) but the list is not RDR-filtered, so the operator must cross-check ownership.
- **Docs were not audited in the first pass.** The hook + marker file were initially shipped without updating the user-facing docs that enumerate them. A follow-up sweep found two stale surfaces (conexus/README.md hooks table, configuration.md file-locations) and corrected them. Reminder: a new hook script or per-user state file means auditing the exhaustive-enumeration docs, not just the code.

## Accepted limitation (carried from CA-4)

Next-session lockstep, not within-session: the detached action completes after the current session already started against the old binary. A foreground action that would achieve within-session lockstep is rejected because it wedges synchronous `SessionStart`.
