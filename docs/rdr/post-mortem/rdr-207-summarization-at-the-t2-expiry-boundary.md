# Post-Mortem: RDR-207 Summarization at the T2 Expiry Boundary

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

T2 is the project-scoped note store in the engine (the Java service over
Postgres); its rows live in `nexus.memory`. A row written with a TTL (a time to
live in days) was deleted outright when it expired, before anything had
summarized or reviewed it, and nothing recorded the loss. RDR-207 split that
one destructive step into two. Expiry now quarantines a row: every read hides
it, but the row stays in the table. A separate step, reap, deletes only a
quarantined row that carries a rollup mark. The mark is written in the same
engine transaction as a summary that covers the row, and the summary comes
from an attended command, `nx memory rollup`, which runs only when someone
types it.

## Implementation Status

**Implemented, all three phases.** The engine half (the memory-004 schema
change, the quarantine rules in `MemoryRepository`, six routes) is in
engine-service-v0.1.120, tagged on a9551ee21 and live in the cloud since
2026-09-15 11:42Z. The client half (both id lists from expire, the verbs
`nx memory reap`, `restore`, `list --quarantined`, `summaries` and `rollup`,
the delete fallback, the `memory.quarantine` doctor row, and the MVV module,
the RDR's minimum viable validation) is on develop at ba928f72f. It ships in
the next client release, which also pins the engine to v0.1.120. Epic
nexus-l3yuc: every child bead closed. Closed 2026-09-15.

## What Differed From the Plan

- **Quarantine broke the explicit delete the design promised.** Day 2
  Operations names `nx memory delete <id>` as the way to remove one
  quarantined row. But delete looked the row up with `get` first, and `get`
  hides a quarantined row by design, so delete answered "entry not found".
  The second planning audit found it (plan ambiguity A7). Both
  `nx memory delete` and `T2Database.delete` now fall back to the quarantined
  listing, and the taxonomy cleanup still runs for the deleted row.
- **The client's expiry message was wrong against the engine it pins.**
  Develop's client said "Quarantined N memory entries" at session end, and
  `nx memory expire` said the same, while `REQUIRED_ENGINE_VERSION` still named
  v0.1.119, an engine that deletes on expiry. For anyone on a develop build
  that told them deleted rows were recoverable. The Phase 2 critic caught it;
  the message is now worded from which id list is non-empty
  (`MemoryExpireResult.describe`), and says "Deleted" when the engine deleted.
- **Reap's taxonomy cleanup was left to the implementer.** The engine's reap
  deletes rows with no cleanup of their topic assignments (plan residual 2).
  The verb now lists the marked rows first, reaps, and removes the topic
  assignments of each reaped row from that listing, as delete does.
- **The rollup made a known engine gap easy to hit.** The engine will mark
  any existing row, live or quarantined (plan residual 1; Sam left it as is
  for this engine release). The rollup reads its source rows, waits minutes on
  a summarizer call, then marks them. A row restored during that wait would
  be marked with no summary of its current content. The Phase 3 critic found
  it; the rollup now re-reads the quarantined listing right before each mark
  and leaves a group unmarked if any of its rows left quarantine.
- **Groups fall by last-write month, not creation month.** The RDR says
  "creation month". The schema keeps no creation date, and every write
  refreshes the timestamp. The command's help and the CLI reference say so.
- **Two defects reached a gate because a push followed targeted tests only.**
  An integration test still expected the old "Expired" wording (the
  local-service gate caught it), and a session-end test mocked the old expire
  call after the hook moved to `expire_detail` (develop CI caught it). Both
  sat outside the files the change touched.

## What To Check First Next Time

1. **When a design changes what a read returns, list every caller that reads
   before it writes.** Delete read the row before deleting it; quarantine made
   that read return nothing. The plan audit found it in round 2. A search for
   callers of `get` at design time finds it before the plan.
2. **Word user-facing messages from what the pinned dependency does.** Between
   an engine change and the client release that pins it, develop runs against
   the old engine. A message that assumes the new behavior is wrong for that
   whole window, and the window can be days.
3. **Re-read state right before a write that depends on it, when a slow call
   sits in between.** A check made before a multi-minute summarizer call is a
   check of the past.
4. **Treat an admitted gap as a question about every new caller.** Residual 1
   was harmless while no caller paused between reading and marking. The rollup
   was that caller, and nothing flagged it until a critic looked.
5. **Run the full suite before pushing a change to a call signature.** Both
   escaped defects were in tests the targeted set did not include.

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 2 | delete's lookup survives quarantine (A7); rows carry a creation month | Yes, source search |
| **Framework API detail** | 0 | | |
| **Missing failure mode** | 1 | rollup marks a row restored during the summarizer call | Yes, reasoning about the new caller |
| **Missing Day 2 operation** | 0 | | |
| **Deferred critical constraint** | 0 | | |
| **Over-specified code** | 0 | | |
| **Under-specified architecture** | 1 | reap's taxonomy cleanup left open (residual 2) | Yes, source search |
| **Scope underestimation** | 0 | | |
| **Internal contradiction** | 0 | | |
| **Missing cross-cutting concern** | 1 | client message assumed the unpinned engine (versioning) | Yes, checking the pinned version |

## Left Open at Close

- Residual 1 stays in the engine: `insertSummary` can mark a live row. Sam
  decided on 2026-09-15 to ship engine v0.1.120 without the fix. The rollup no
  longer triggers it through its own wait; a direct `POST /v1/memory/summaries`
  still can.
- Out of scope by the RDR: promoting heavily used rows to T3 (RDR-209), and
  using summaries in reads.
- The signed mac-arm64 engine binary is still not exercised by the
  post-publish acquire gate (nexus-2oh5q, older than this RDR).
