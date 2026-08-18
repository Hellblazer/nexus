# Post-Mortem: RDR-156 — Vector-Store Capability Leverage

**Closed**: 2026-08-18 (implemented) · **Epic**: nexus-70r3c (25/25 children closed) · **Accepted**: 2026-06-10 (gate PASSED 0C/0S)

## Outcome vs design

The premise held: co-residence of vectors and governing metadata in one
Postgres makes integrity schema-enforceable and cross-store retrieval a
single statement. All six phases shipped, though two decisions were
absorbed by faster-moving arcs before this RDR could finish them itself:

- **P0 — FK spine + empty-table hygiene** (fk-002): shipped, then the
  per-dim FKs died with RDR-191's `DROP CASCADE` and were succeeded by
  fk-004 on the unified table. The FK idea won; the specific DDL was
  twice replaced.
- **P1 — soft delete** (catalog-003): `deleted_at` tombstones, partial
  indexes, trash/restore/purge ceremony. The "single enforcement point"
  claim was FALSIFIED in production (nexus-3ck2g: five unfiltered read
  sites), repaired by catalog-019/022/026. Recorded as
  aspirational-then-corrected, not as-built.
- **P2 — manifest functions**: shipped, then RETIRED by catalog-030 once
  RDR-191's manifest FK made the orphan check structural. Correct
  outcome: the capability graduated from callable to constraint.
- **P3 — collection_vector_stats** (catalog-005): live; doctor and
  collection_state consume it.
- **P4 — combined-query shapes** (catalog-006/007/008/012): metadata-
  scoped, topic-scoped, and graph-hop shipped and repointed; the
  app-side stitching was deleted at P4.2c (55e3dd8b3) after an explicit
  loud-reject disposition for the two surviving dance consumers.
  Aspect-filtered and frecency-boosted shapes were decided but never
  beaded — caught only by the 2026-08-18 true-state cross-walk
  (nexus-ubnwk now tracks them).
- **P5 — hybrid_search** (vectors-007, engine v0.1.80, conexus 7.10.0):
  server-side RRF fusion, exact-over-gate, tombstone-filtered. Gate
  verdict GO on measurement: lcogi regression case 6/6 in a 100k corpus
  at ~640ms both paths; fixture-scale oracle recall 0.9841 vs the
  shipped Java path's 0.9735, never worse per-query.

## What the record should remember

1. **A cleared world-block sat unnoticed for two months.** The xr7.8.9
   go-live gate PASSED 2026-06-12; the bead's own notes recorded it on
   06-19; nothing surfaced it again until Sam went looking on 08-18.
   Groomings, shakedowns, and a full RDR audit all ran in between — none
   owned the question "is this deferral still true." The fix outlived the
   incident: `scripts/check_deferral_staleness.py` + shakedown row S17
   (nexus-6uhw9 carries the residual mechanization).
2. **Bead instructions rot faster than beads.** P5.1's spec said "extend
   the P3.E DualRunHarness" — deleted at RDR-155 P4b, months before the
   bead unblocked. The premise was only caught by a pre-implementation
   explore pass. Verify a bead's named artifacts exist before dispatching
   on them.
3. **Exact-set parity is the wrong bar for a deliberately different
   algorithm.** The inherited recall==1.0 assertion was unachievable by
   construction against RRF reordering (and divided by a fixed K).
   Redefined evidence-first: gate containment + selective-subcase
   equality + oracle quality-non-regression, thresholds pinned from
   measured data with margin. The replacement caught nothing less and
   asserts something true.
4. **Value re-justification moved from entry to exit and was worth it.**
   nexus-lcogi shipped a standalone fix for the original incident while
   P5 was blocked, narrowing the function's value. P5.G measured instead
   of assuming: the function wins or ties everywhere except dense gates
   >5k rows (6.5x at 25k, ratio grows), where the Java path's dispatch
   is faster. GO, with the tradeoff tracked (nexus-76352) and dense
   callers steered to the shipped path meanwhile.
5. **Long-running RDRs need true-state cross-walks.** Over ten weeks,
   RDR-191 superseded D2, catalog-030 retired P2's functions, RDR-180
   mooted D7's checks, and production falsified a P1 claim. The
   2026-08-18 cross-walk (bfc021311) that annotated every decision with
   its as-built truth is what made an honest close possible — and found
   the never-beaded D5 shapes.

## Residue (tracked, deliberate)

nexus-zekpl (tumbler-aware hydration) · nexus-0zcn9 (narrow-collection
max_scan_tuples defense, Finding 5b — still undefended on the shipped
shapes) · nexus-ubnwk (the two never-beaded D5 shapes) · nexus-xlnfk
(real-catalog Python coverage for service-mode catalog params) ·
nexus-2rtge (stale tombstone-predicate sweep across vectors-005
siblings) · nexus-76352 (dense-gate escape valve). All P2, all named in
the gate close records.
