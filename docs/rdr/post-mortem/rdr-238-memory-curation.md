# RDR-238 post-mortem: memory curation

## What the RDR set out to do

Nexus could write and expire memory but not curate it. Three gaps: a replaced
entry kept being served (no write could say "this replaces that"); the
always-loaded `MEMORY.md` index grew without a budget; and nothing ran the
curation habits on its own. The RDR added a `replaces` write on T2 rows and
`memory_put` (Phase 1), split `MEMORY.md` into a bounded always tier and an
on-demand index and re-measured rule compliance (Phase 2), and added a
SessionStart check, `nx memory curate` and a release step (Phase 3).

## Implementation status

Implemented, closed 2026-11-19 by Sam's ruling (T2
`nexus/sam-ruling-rdr-closeout-batch-2026-11-19`) before Phase 2's own end
(2026-12-09). All three phases are built: `replaces` quarantines the named T2
row (restorable, never reaped); the split shipped 2026-11-09; the SessionStart
check, `nx memory curate --files/--t2/--agents/--cold` and release step 12a are
live. A scan at close found 200 memory files, 0 unindexed, 0 dangling and 2
over the size limit. A2 is resolved by Sam's decision; A3 (moved rules keep
compliance) stays assumed until the follow-up reads below.

## What diverged

**Quarantine replaced deletion.** The design deleted a replaced T2 row. The A2
read tripped its stop rule: Sam could not judge the rows, so an agent read the
first 20 replacement-type rows and marked 6 yes, 11 maybe, 3 no (not a random
sample, not Sam's marks), and Sam chose quarantine on 2026-11-02, which was the first
draft's design that the Decision Rationale had argued against. The RDR body
kept describing deletion until the close edit amended it in place.

*Drift: unvalidated assumption.*

**The compliance test had less power than planned.** The em-dash baseline was
3 of 79 sessions (3.8 per 100), not the 11.2 the RDR assumed, and the em-dash
line never left the always tier, so its re-measure is only a control. The four
rare rules pooled to 9 events in 162 sessions, most of them from rules a hook
or the hot rules already enforce, so the pooled read cannot refute A3.

*Drift: unvalidated assumption.*

**The measurement window meets transcript retention.** The only source for the
re-measure is session transcripts, kept about 30 days, so the 30-day window
has to be read on its last day or lose its first days.

*Drift: missing Day 2 operation.*

**The close preceded the last measurement.** Sam closed the RDR with Phase 2
Step 3 still to run, carrying it as a dated follow-up (nexus-qo7ie.15) instead
of holding the RDR open.

*Drift: deferred critical constraint.*

## Follow-up reads (appended when they run)

- 2026-11-24, em-dash control read (14 days from 2026-11-10): pending.
- 2026-12-09 (12-10 at the latest), pooled rare-rule read: pending. Record the
  realised window start, any truncation, and word the outcome "no regression
  observed; A3 stays assumed" unless a rule exceeds its bound.
