# Post-Mortem: RDR-151 Daemon Event-Loop Selector Busy-Loop and Write-Lock Contention

## RDR Summary

The recurring T2 daemon 100% CPU peg and multi-daemon `database is locked` cascade
were traced to event-loop selector busy-looping on a half-closed accepted socket plus
SQLite write-lock contention. RDR-151 proposed a two-phase root-cause fix: Phase 1 the
event-loop/contention fixes (Gaps 1-4 + the peg), Phase 2 a set of refinements (Gap 5
cooperative write retry, Gap 6 a dedicated flock for the crash-loop-sentinel
read-modify-write).

## Implementation Status

**Partially Implemented (close_reason: partial).** Phase 1 — the actual peg + write
contention — shipped and was accepted (epic `nexus-5kcsq`, 11/16 children closed). Phase 2
Gap 6 plus its review/critique/test/phase-gate wrappers were **deliberately dropped**
2026-06-26 during a bead-graph audit, because the daemon they harden is being deleted by
RDR-152.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- **Gap 1 + Gap 2 (the peg)**: deregister/close the accepted socket read-fd on
  peer-death-during-dispatch (`nexus-th4dh`).
- **Gap 3**: `mark_shutting_down()` first in `stop()` to stop the cascade trigger (`nexus-yd6fy`).
- **Gap 4**: `to_thread(heartbeat_tick)` so the flock never blocks the event loop (`nexus-tjgl2`).
- **Phase 1 backstop**: idle/read deadline on accepted connections (`nexus-5haam`).
- **Gap 5**: cooperative bounded write retry/backoff replacing the SQLite default
  busy-handler spin (`nexus-gcu07`).
- Phase 1 review / critique / test-validation / phase-review-gate all passed.

### What Was Planned but Not Implemented (deliberately dropped)

- **Gap 6** (`nexus-1wpa4`): a dedicated flock for the crash-loop-sentinel
  read-modify-write, to stop K concurrent racers lost-updating the sentinel count.
- Its Phase-2 process wrappers: substantive critique (`nexus-seuwj`), code review
  (`nexus-336ig`), test-validation (`nexus-n9vxu`), phase-review-gate (`nexus-eceik`).

### Why It Was Dropped

A deliberate keep/drop call (recorded in the epic and RDR `implementation_notes`):

1. **The contention Gap 6 guards against was already eliminated by Phase 1.** Gap 6's own
   description scopes it to "exactly the contention Gaps 3/4 create" — and Gaps 3/4 shipped
   in Phase 1. The residual risk it addresses is largely gone.
2. **The daemon is being deleted.** RDR-152 P4.1 (`nexus-gmiaf.24`) deletes
   `src/nexus/daemon/` in full, and RDR-158 P3 (`nexus-7bomn`) removes the `=sqlite` opt-out
   that is the only path still reaching the daemon. Hardening soon-deleted code is
   negative-value.
3. **The wrappers cost more than the fix.** Four review/critique/test/gate beads to land a
   single low-severity guard-accuracy refinement is disproportionate.

Reversible: if the RDR-152 daemon-deletion timeline slips materially AND a real
guard-accuracy / crash-loop incident occurs, Gap 6 can be re-filed.

---

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Deferred critical constraint** | 1 | Phase 2's value was contingent on the daemon surviving; the parallel RDR-152 decision to delete it made Phase 2 moot | No — the daemon-retirement decision (RDR-152) postdated RDR-151's authoring |

The single "drift" is not an implementation error but a cross-RDR obsolescence: RDR-152
(authored later) retired the substrate RDR-151 Phase 2 was hardening. This is expected when
a root-cause fix (Phase 1, valuable now) and a refinement (Phase 2) straddle a substrate
replacement decision.

---

## RDR Quality Assessment

### What the RDR Got Right

- The **root-cause diagnosis** (selector busy-loop on a half-closed fd + write-lock
  contention) was correct and Phase 1 durably fixed the production peg.
- The **phase split** was prescient: putting the actual peg fix in Phase 1 and refinements
  in Phase 2 meant the high-value work shipped independently and the low-value remainder was
  cleanly droppable when the substrate decision changed.

### What the RDR Missed

- Nothing it could have known: RDR-152's daemon-deletion decision came later. The phase
  boundary happened to make the obsolescence cheap to absorb.

---

## Key Takeaways for RDR Process Improvement

1. **Split "fixes the pain now" from "hardens for later" at the phase boundary.** RDR-151's
   Phase 1 / Phase 2 split is the reason a mid-flight substrate-retirement decision cost a
   clean drop instead of stranded or wasted work. Refinements whose value is contingent on a
   substrate surviving belong in their own phase.
2. **Re-examine accepted-but-unfinished RDRs against later architecture decisions.** RDR-151
   sat 68% done while RDR-152 decided to delete its target. A periodic audit (this one) is
   what surfaced that Phase 2 was now negative-value; without it the wrappers would have been
   ground through as rote backlog.
3. **A deliberate scope-drop is a first-class close outcome.** `close_reason: partial` with a
   recorded rationale is honest and reversible; it is not the same as abandoning an RDR, and
   it should carry a post-mortem like any other close.
