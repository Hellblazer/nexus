# Post-Mortem: RDR-193 Server-Side Catalog Reconciliation and Taxonomy Compute

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

The client registered catalog documents through a reconcile loop over a downloaded copy of the
catalog (Gap 1). Its index-time linking listed whole content types across the tenant to link
one repository's files, which made wrong links in the order of whatever the list returned
(Gap 4). Taxonomy discover downloaded every embedding to cluster it on the client (Gap 2). RDR-193
moved catalog reconcile onto the engine through landing tables, a job registry and one
all-or-nothing commit (Part A). It moved linking onto scoped engine reads and a bulk link write
with paths resolved by file identity (Part A4). It also designed taxonomy compute on the engine
(Part B).

## Implementation Status

Implemented for Parts A and A4. Engine cut 1 (engine-service-v0.1.185) went live on 2026-11-09.
The client half shipped across conexus 8.1.0 to 8.2.0. Part B was held at accept and moved to
RDR-251 at close. Gap 3 was out of scope and stays open, now with a count of 0 on this
repository (below).

---

## Implementation vs. Plan

### What Was Implemented as Planned

- Landing tables, a run header and one reconcile job per tenant and owner on a shared
  `EngineJobs` registry, with an all-or-nothing plpgsql commit (Steps 1 and 2). Its parity
  fixture, 31 cases, showed 0 differences. A 20,000-row commit took 8.5 s on insert and 9.2 s
  on update, against a 30 s bar.
- `links_upsert_many`, `same_content` and the scoped list filter (Step 3), with paths resolved
  by file identity first.
- The client switch (Step 6): one commit, `ceil(N/1000)` stage pages and no single writes on a
  warm run. The old client housekeeping and path deletes are gone.
- The linking client (Step 7). On the delta shape the probe read 27 rows where the old code
  read 23,337.

### What Diverged from the Plan

- **Cross-repository links dropped**: Step 0 found 103 live cross-repository `implements` links
  and 0 same-repository ones. That fired open question 2's trigger. Sam chose to keep the drop,
  so the 103 links are not re-created on re-index.
- **The stale-owner population was not there**: the design sized the second RDR read arm for
  "about 150 RDRs under a stale owner". Step 0 count 4 found 0 under its definition.
- **Step 9 raised no pin**: Step 6 shipped in 8.1.0 and 8.1.1, which already required an engine
  past cut 1. Raising the pin "to cut 1" as written would have lowered it.
- **Gate 5b.1 deferred, then covered**: the package-upgrade rehearsal for `catalog-048` was
  deferred at the cut to a pin bump that never came in that form. Sam recorded it as covered by
  conexus 8.3.0's battery: the package-upgrade leg, and the 6e walk from 7.76.0, 7.99.0 and
  8.2.3 to engine 0.1.194.
- **The MVV ran on a reduced tree**: text files were cut to 1 KB and the 7 PDFs dropped
  (N=5010), because a full cold run takes 5+ hours of embedding. Sam accepted the run as it
  was. No real PDF went through a delta.
- **Step 7's timing compare became a row count**: the probe's loopback seconds are not
  comparable to the managed-tenant baseline. It is still unknown whether list time or probe and
  frontmatter time dominated.
- **"No row rewritten" was not literal**: the reconcile commit rewrites nothing on a warm run,
  but the frecency stamp still writes every row through `update_many`.
- **The Gap 3 count was dropped, then taken at close**: Step 6 was to count the documents a full
  run leaves with no manifest. It was carried to the Phase 2 gate and not taken there. At close
  it was measured on a local engine: 0 of 5328 documents (develop 9b4618611, the same reduced
  tree as the MVV, PDFs not covered).
- **Part B moved out**: held at accept on Sam's answer to open question 3, and moved to RDR-251
  at close.

### What Was Added Beyond the Plan

- Nothing material. Review rounds added gates and tests within each step.

### What Was Planned but Not Implemented

- Part B (taxonomy SQL, labeler routes and the Java discover job: Steps 4, 8 and 10 to 12).
  It moved to RDR-251 with the carry-list from the epic's 2026-11-11 comment.

---

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 2 | "about 150 stale-owner RDRs" (measured 0); Step 7's time split assumed measurable against a managed baseline | Yes, by running the Step 0 count before sizing the read arm |
| **Framework API detail** | 0 | | |
| **Missing failure mode** | 0 | | |
| **Missing Day 2 operation** | 1 | gate 5b.1 deferred to a release event that the plan's own Step 9 then removed | Yes, by naming a fallback owner for a deferred gate |
| **Deferred critical constraint** | 1 | Gap 3's count, the input to the reorder decision, slipped from Step 6 to close | Yes, by giving the measurement its own bead or checklist line |
| **Over-specified code** | 0 | | |
| **Under-specified architecture** | 0 | | |
| **Scope underestimation** | 0 | | |
| **Internal contradiction** | 2 | Step 9 "raise to cut 1" against a pin already past it; Test Plan "no row rewritten" against the frecency stamp | Yes, by reading the current pin and the warm-run writes at plan time |
| **Missing cross-cutting concern** | 0 | | |

---

## RDR Quality Assessment

### What the RDR Got Right

- It put the correctness defect (Gap 4) first and held the cost-only work (Part B). The
  accepted scope shipped in about a week.
- Its contract tests were stated as request shapes (one commit, `ceil(N/1000)` stage pages,
  zero single writes). Those still held when the MVV tree had to be cut down, because the
  counts do not depend on file size.
- It measured before deciding: Step 0 ran before the identity tests were written, so the
  open-question-2 trigger fired on a number, not a guess.

### What the RDR Missed

- A release step worded against a pin value that no longer held by the time it ran.
- A plan-time check of what a warm run writes outside the reconcile commit.

### What the RDR Over-specified

- **Performance targets unvalidated**: the Step 7 time split, which the probe environment
  could not measure.

---

## Key Takeaways for RDR Process Improvement

1. **Write release steps against the pin they find, not a fixed value**: say "the pin is at
   least engine cut N", so a later release that already passed it does not read as a
   lowering.
2. **Give a deferred gate a named fallback**: when a gate moves to "the release that does X",
   say who runs it if X never happens.
3. **Track a measurement the next decision depends on as its own item**: Gap 3's count rode
   inside a step's prose and slipped through two gates.
4. **Measure a population before sizing a mechanism for it**: the stale-owner read arm was
   sized at about 150 RDRs, and the count was 0.
