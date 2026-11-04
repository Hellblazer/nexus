# RDR-239 post-mortem: the tuple-space dictionary

## What the RDR set out to do

Every tuple template described its shape but not its meaning, so an agent or
a person reading the space could not tell what a subspace was for, who wrote
it, who read it, or what a valid post looked like. The RDR gave each of the
seven shipped templates an optional, closed `doc` block holding a summary,
the writers and readers, the meaning of each field's values, and one example
post that the engine validates at boot with the same validator `out()` uses.
The engine serves the block on `?doc=1`, `nx tuple dictionary` joins it to
the live census, and a lint keeps the cited files and RDRs from going stale.
It is stage 1 of the scriptable tuple space (nexus-mdbds).

## Implementation status

Implemented. Epic nexus-mdbds.2 is closed with all 24 children closed. The
engine half shipped in engine-service-v0.1.175 and the client half in
v7.94.0. At close the critic's verdict was `justified`, 0 Critical and 0
Significant (T2 `nexus_rdr/239-scope-critique-2026-11-03-r2`), after a first
close critique found two record gaps that were then fixed in the RDR text.

## What diverged

**A2's measurement lived only in bead comments.** The RDR stated A2 (serving
the dictionary is cheap) as unmeasured, and Phase 2 measured it: the default
registry response is 2,807 bytes before and after, `?doc=1` is 11,865 bytes,
and boot time moved from 0.425 ms to 0.430 ms. Nobody wrote the figures back
into the RDR, so a reader of the design of record could not see that a
`doc=True` call costs about 12 KB of model context. Recorded at close.

*Drift: missing cross-cutting concern* (closing the loop on a stated
assumption is part of the work, not an afterthought).

**Design was added after the gate without a revision row.** Four small
additions landed in beads after round 1: the Python mirror lint, the
NAME-resolution rules, the old-engine messages and the lint's exemption and
non-vacuity rules. Each was reviewed in its bead, but the RDR's history did
not say who accepted them. Recorded at close as one revision row naming the
beads.

*Drift: under-specified architecture* (the gate read a design that still had
open edges the implementation had to settle).

## What to carry forward

- When a phase measures a Critical Assumption, the same change writes the
  figure into the RDR's assumption and verification sections.
- A design decision made in a bead after the gate gets a Revision History row
  in the same push, naming the bead.
- Open at close, recorded in the critique and not blocking: the served
  `aspects` example posts to the live queue with no in-band caveat, and the
  `writers`/`readers` symbol names are checked by nothing (the RDR's Drift
  section says so).
