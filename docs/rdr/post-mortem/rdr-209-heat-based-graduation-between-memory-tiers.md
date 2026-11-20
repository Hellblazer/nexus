# Post-Mortem: RDR-209 Heat-Based Graduation Between Memory Tiers

## RDR Summary

The RDR began as graduation on heat: move a frequently read T2 memory row to
permanent T3 knowledge and shorten its T2 life. Its Phase 0 census refuted the
signal that design depended on. `access_count` counted every row a search
matched, not every row a caller saw, so "hot" meant "matched often". Sam
narrowed it on 2026-11-16 to the two problems that need no threshold:

- Gap 5, a read counter that counts only the rows a caller received (Phase 0b).
- Gap 6, gate records (review and critique verdicts) that never expire
  (Phase 3 Steps 1 to 3: an archive to T3, a pointer record, a loss check,
  then a 30-day TTL).

Graduation itself (Phases 1 and 2) was deferred under nexus-1gdv0.4, to be
revived only if a re-census on the new counter shows a usable tail.

## Implementation Status

Partially Implemented, as narrowed. Close reason: partial (narrowed to Phase 0b
and Phase 3 Steps 1-3; Phases 1-2 deferred under nexus-1gdv0.4; re-census
nexus-1gdv0.3 on 2026-12-19). Closed 2026-11-20 under Sam's close-out ruling
(T2 `nexus/sam-ruling-rdr-closeout-batch-2026-11-19`); the close critique came
back partial (0 critical, 4 significant), and its findings S1 to S3 were fixed
in the RDR text at close.

Phase 0b shipped in engine-service-v0.1.196 and conexus 8.5.0, and Phase 3 in
the `nx memory gate-rollup` command. Both phases are closed (nexus-1gdv0.1,
nexus-1gdv0.2). One measurement remains: the re-census nexus-1gdv0.3, due
2026-12-19. Its result is appended below when it runs.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- The engine's memory search takes `limit` and `offset`, stamps only the rows
  on the page, and returns the matching total (a7adca21b).
- `search_page` on the client and the MCP `memory_search` tool send the page.
  An older engine's bare list is cut on the client. `search()` and
  `nx memory search` are unchanged (7105b368e, 9c20d5bfd, af89ee054).
- The wire-ledger entry and the `access_count` docs (af91703d2, c69be7afb).
- A baseline listing at a deploy, for the re-census to subtract (T2
  `nexus/rdr209-access-baseline-2026-11-19`, 14,117 rows).
- Gate-rollup's loss check, T3 archive with read-back, pointer record and TTL
  through the import route, behind a recorded dry-run count. The first real
  run on RDR-249 archived 12 records, set TTL 30 on them, and quarantined
  none (nexus-1gdv0.2.17).

### What Diverged from the Plan

- **The counter's new meaning depends on the client version.** The RDR
  defined "received" as an engine property. On a new engine, an older client
  still sends no page and still stamps every match, and the counter records
  no client version. So any count mixes two meanings, per caller. The docs
  review caught it (86d7e0235); the RDR text was corrected only at close.
- **The baseline was not "at the deploy".** The counter's engine shipped in
  0.1.196, but the read ran in the next deploy window, 0.1.197 at 14:33:14Z,
  about four hours after conexus 8.5.0 began counting under the new meaning.
  Those hours, and every older client's searches, sit inside the re-census
  deltas.
- **Ordering a day's gate records needed more than the title's tail.** The
  first live dry run read titles with the round in the middle, and a bead id
  whose suffix starts with `r` and a digit can look like a round. A write-time tiebreak was
  added, then removed in review: a re-put re-stamps a row, so a rewritten
  partial could pass a later justified verdict. The round is now read with
  the bead id removed, and a tie goes to the worse verdict.
- **A partial verdict rolls up only with Sam's per-bead stamp.** The design
  treated a partial as one more verdict to order. Sam ruled that a partial
  rolls up only with his `--accept-open` stamp for that bead.
- **Gap 6 reaches only gate records tagged with an RDR.** A run is scoped to
  one RDR. The `gate_record` procedure adds `rdr:<NNN>` only when the bead
  serves an RDR, so a record whose bead serves no RDR is outside every run
  and stays permanent. The RDR named only the record with no tags at all. The
  share is unmeasured; the re-census counts it.

### Existing Infrastructure Reused Instead of New Code

- The TTL write goes through the existing import route, and the quarantine,
  rollup and reap path is RDR-207's, unchanged.
- The archive is an ordinary T3 note plus a T2 pointer, not a new store.

### What Was Added Beyond the Plan

- The count-first guard: a real run refuses unless `--counted N` equals the
  dry run's recorded would-quarantine count, recomputed at run time.
- Read-back of the archive and the pointer, including the marks
  (`ttl_set`, `restored`, `state`) the next run decides from.
- Refusal of `--project` values that would let one repository's RDR phases
  gate another repository's records.

### What Was Planned but Not Implemented

- Phases 1 and 2 (graduation, the promotion mark on the T2 row, the
  post-graduation TTL rule): deferred by Sam's narrowing, under
  nexus-1gdv0.4.
- A census of gate records outside any RDR: never measured (see above).

---

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 1 | Heat as a graduation signal (A1, refuted by the Phase 0 census) | Yes, and it was: the census ran before any build |
| **Framework API detail** | 1 | Round parsing confused bead ids with rounds | Yes: source search of real titles |
| **Missing failure mode** | 2 | A re-put re-stamping a row passes an older partial; records with no `rdr:` tag fall outside every run | Yes: source search of `gate_record.lua` |
| **Missing Day 2 operation** | 1 | The baseline read waited for the next deploy window | Yes: name the deploy window and owner in Step 5 |
| **Deferred critical constraint** | 0 | | |
| **Over-specified code** | 0 | | |
| **Under-specified architecture** | 1 | How a partial verdict rolls up (ruled by Sam mid-build) | Partly: an ask at gate time |
| **Scope underestimation** | 0 | | |
| **Internal contradiction** | 0 | | |
| **Missing cross-cutting concern** | 1 | The counter's meaning depends on the client version | Yes: the direction-safety table already had the facts |

### Pattern References

Missing failure mode (2): both came from the data's real shape (titles, tags)
differing from the convention the design assumed. Next time, read a sample of
real records before writing the selection rule.

---

## Key Takeaways

- A counter's meaning is set by every caller, not by the server. When a wire
  change alters what a number means, write down which client versions produce
  which meaning, and whether the number can tell them apart.
- A measurement "at the deploy" needs a named window and owner, or it lands at
  whatever window comes next.
- Narrowing an RDR is fine when it is disclosed. This one was disclosed in the
  scope paragraph, each gap's status line and the Revision History. The close
  still has to say that it is narrowed.

## Follow-up: the re-census (due 2026-12-19, nexus-1gdv0.3)

Pending. Appended here when it runs: the bucket table against the baseline,
the mixed-client caveat, and the count of gate records with no `rdr:` tag.
