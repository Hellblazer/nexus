# RDR-179 Post-Mortem: Self-Correction Machinery Remediation

**Closed** 2026-09-16 (from draft, Sam's override) · **Created** 2026-07-04 · **Beads** nexus-o02xe, nexus-9kq3h, nexus-vtp8h (all closed)

## What the RDR set out to do

Four feedback loops that were supposed to make retrieval improve itself had
gone dark: plan reuse returned nothing in service mode because the CLI and
the MCP server read different plan stores, the RDR-090 retrieval benchmark
was built but never run as a gate, the plan library accepted plans it could
not execute and never retired plans that always failed, and taxonomy-aware
recall (RDR-134) had no sequencing. The RDR ordered the repairs into five
phases and named a bead for each of the first three.

## Implementation status

Partially implemented, then closed. Phases 1 to 3 shipped under their beads.
Phase 4 is RDR-134, still a draft. Phase 5 was a disposition sweep and was
done at this close rather than as code.

## Implementation vs plan

### As planned

- Phase 1: the plan-surface split-brain is gone. `_open_plan_library()` in
  `src/nexus/commands/plan.py` returns the HTTP plan library only; the
  SQLite seam died with the store (nexus-o02xe).
- Phase 3: `plan_save` validates an executable DAG at save time,
  always-failing plans decay out of matching, and `nx plan hygiene` prunes
  the null-verb bead dumps (nexus-vtp8h).

### Diverged

- **Benchmark as a gate**: the RDR planned a weekly GitHub workflow with
  `check_regression.py` and a `floors.yaml`. The implementation landed
  `tests/benchmarks/test_retrieval_drift_gate.py` on the nightly
  local-service gate instead (nexus-9kq3h). Same guard, cheaper shape, no
  separate workflow to keep alive.
- **`use_count` reconciliation** (Phase 3.4) was scoped out of nexus-vtp8h
  and taken by RDR-203 Gap 3, which collapsed the three telemetry writes it
  depended on into one run record.

### Not implemented

- Phase 2.1's 10/10/10 query set. `bench/queries/` still holds only the
  5-query spike and the abstract-themes set.
- Phase 5.1, the provenance corpus for the AgenticScholar fixture.
- Phase 5.3, the cfc72 throughput measurement; nexus-r300v closed on its
  own terms.

## What was wrong in the document by the time it closed

The Problem Statement still said the twelve CLI sites hardcode SQLite and
that `plan_save` accepts non-executable JSON. Both had been false for
weeks. A reader taking the draft at face value would have reopened closed
work. This is the cost of a remediation RDR that delivers through beads:
the beads close, the document does not, and nothing flips it.

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| Over-specified code | 1 | weekly workflow + floors.yaml vs nightly pytest gate | Yes, source search |
| Scope underestimation | 1 | Phase 5 sweep and Phase 4 left to a later reader | No |

## What to check first next time

A remediation RDR whose phases are beads needs a close bead of its own, or
the document outlives its work. When its last named bead closes, read the
Problem Statement against the code and flip the status the same day.
