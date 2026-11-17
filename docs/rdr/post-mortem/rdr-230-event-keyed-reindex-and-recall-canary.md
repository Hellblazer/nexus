# RDR-230 post-mortem: event-keyed REINDEX and a recall canary for per-collection HNSW indexes

## What the RDR set out to do

RDR-227 gave each large collection its own HNSW graph (a `pci_` index) and
measured its recall only when freshly built. Nothing measured an aged one, and
the engine could rebuild one only by dropping it. RDR-230 added two things.
Phase 1: a recall canary that replays a persisted ring of each collection's own
recent queries against an exact scan and reports green, red or unmeasured.
Phase 2: an engine-run `REINDEX CONCURRENTLY` of a `pci_` or leaf index, fired
by events (deletes since the build, size growth, a red canary) rather than by a
clock.

## Implementation status

Implemented. Phase 1 shipped across engine 0.1.181 and 0.1.182 (beads
nexus-4lnn1.1.1 to .1.8). Phase 2 shipped as develop 7b727d07e (engine 0.1.192)
with follow-ons on develop through 798c4e723 (canary fixes af47a7334, anchor v2 359276955 and f59e6ff37, the red_unfixed hold 1c4905e19, c721af09c and fd6cc1d46), and REINDEX was switched on
in the cloud with engine-service-v0.1.194 on 2026-11-16 07:13Z (STEP-6 113/113).
The phase-review gate is T2 `nexus/phase-review-rdr-230-phases-1-2-2026-11-16`.
Assumptions A1, A3, A4 and A6 have readings; A2 is only partly verified (see the
RDR's Critical Assumptions).

## What diverged

**A red canary is not always something a rebuild can fix.** The design treated
a red verdict as a sign of graph decay and answered it with a REINDEX, retried
after a 24-hour back-off. On `code__1-87` one ring query was an outlier whose
true nearest row the HNSW search could not reach in any build, so every daily
REINDEX finished and the index was still red. Sam chose a hold: after a REINDEX
that leaves the index red, the engine records `red_unfixed` and stops
rebuilding until a green canary clears it (798c4e723). The hold was proved on
2026-11-17: no REINDEX through the back-off expiry, then a green run cleared it
(T2 `conexus/reads-2026-11-17-code187-quiet`).

*Drift: missing failure mode.*

**Small rings gave false reds.** The canary first gave a verdict at three ring
queries. In the seven-day reading, two indexes went red at 4 and 5 queries and
turned green once their rings reached 10, with no rebuild (T2
`conexus/rdr230-a6-seven-day-reading-2026-11-14`). At P2.1 Sam set the
red-canary REINDEX to need at least ten queries (T2
`nexus_rdr/230-thresholds-2026-11-14`).

*Drift: unvalidated assumption.*

**The first fork rehearsal went red for a reason outside the index.** Fork 1's
red traced to a cluster of probe queries, not to the REINDEX. Fork 2 was green
before and after the REINDEX and after a killed REINDEX and retry (T2
`nexus/rdr-230-p24-findings-2026-11-15`). A near-duplicate survey found no dense
cluster in any HNSW-served collection, and Sam closed that question (option D).

*Drift: unvalidated assumption.*

**The Phase 2 beads were folded into one.** P2.2 to P2.R began as beads
nexus-4lnn1.1.11 to .1.15 and were folded into nexus-4lnn1.1.10 as checklist
items on 2026-11-07. Nothing was dropped; every item has a pointer in the
phase-review record.

*Drift: none (bookkeeping).*

**The RDR text was brought to the code twice** (2026-11-06 for Phase 1,
2026-11-15 for Phase 2): the admitting routes, the per-index list moving to
`/v1/admin/chunks-leaves` (Sam, ask `pci-names-on-status`), the 6-hour REINDEX
statement timeout, and the status fields.

*Drift: under-specified architecture.*

## What the next person should take from it

A recall canary measures the query set as much as the index. Before a red
verdict triggers work, ask two questions. Is the ring big enough to mean
anything? Would a rebuild change the answer at all? Build the "rebuild did not
help" state in from the start, not after it loops.
