# RDR-147 post-mortem: multi-hop entity-resolution retrieval

## What the RDR set out to do

`nx_answer` declined questions like "find the names of companies affected by
issues in X". The evidence it retrieved held customer ids (`CUST-0042`) but
never the company names, and nothing turned an id into the record that names
the company, so the coverage check found nothing that answered the question.
The RDR added a `resolve` plan step that looks keys up by exact catalog title,
per-collection entity-key patterns in config, and an exact-key branch inside
RDR-232's coverage repair. The branch resolves the keys the coverage anchors
name and joins each record to the evidence item that named it, before the one
re-check. An ingest-time entity linker was planned as conditional and was not
needed on the benchmark corpus (HERB).

## Implementation status

Implemented. Steps 0, 1, 3, 4 and 5 are closed (nexus-3lu23, nexus-fai28,
nexus-v9v5r, nexus-qpimy, nexus-gie46); the MVV run (nexus-irndi) and the final
phase-review gate (nexus-6z0ph) are closed. Phase 2 turned the repair on by
default (nexus-bwct4.2). Deferred with beads: the linker (nexus-81183,
nexus-9xvqr), md/pdf key extraction (nexus-bwct4.3) and hop-1 retrieval recall
(nexus-io7tw).

The final MVV (2026-11-06, develop 1ecf3ca6d, 147-research-17) passed: in both
runs every company the anchors' keys resolve to was named, 4 of the 5 issue
questions in that domain, precision 1.00. issue5 F1 was 0.500 against the fixed
script's 0.764.

## What diverged

**The success bar was revised four times, each after a failing run.** The June
bar was F1 against a fixed script. It became join completeness plus precision
(2026-11-03), then one in-domain miss allowed and precision over answers that
name a company (2026-11-05), then a domain of questions whose coverage anchors
hold a key (2026-11-06). Each change was Sam's and is recorded, but the final
pass sits exactly at its floor and says nothing about retrieval quality.

*Drift: unvalidated assumption* (the RDR assumed the join, not retrieval and
routing, was the only thing between the evidence and a correct answer).

**The re-check rejected joined items the RDR had measured as supported.** The
design rested on the coverage re-check accepting a joined item (10 of 10 in
147-research-9). Across the 2026-11-05 runs it accepted 11 of 15, so correct
answers were withheld. Option 1 (generate after a merged join even when the
re-check says uncovered, with a notice) was added to the design after the fact.

*Drift: unvalidated assumption.*

**Routing decided the outcome before the repair could run.** A model call reads
each request's kind. It read the HERB questions as a lookup 13 times in 14 and
as a listing once. The listing reading sent the question to a single-query plan
with no coverage boundary, and that run lost a question. Branch (c), a curated
listing template carrying the coverage-checked steps, was rewritten from a
contingency into the fix. Its first version stole other requests below the
cosine floor; review caught it and a `cosine-only` tag fixed it.

*Drift: missing cross-cutting concern* (the repair lives inside one plan
family; nothing guaranteed every reading of the question reached it).

**The scan reads coverage anchors, not all retrieved evidence.** One of the
five issue questions holds 8 ids in its retrieved chunks and none in its
anchors in every run, so it is still declined. Widening the scan was left out
(Sam, nexus-z938f).

*Drift: scope underestimation.*

**The harness had to be fixed before its numbers meant anything.** The first
live MVV attempt ran with no `claude` on the sandbox PATH and measured nothing;
the spend cap had to be read from the ledger, not the row file; plan validity
had to use creation time. Each was a separate harness PR.

*Drift: missing failure mode.*

**The Phase 2 default flip changed the wire for every install.** Turning the
switch on made every repaired run report the branch's reason and scan counts,
even with no entity pattern configured, which broke a recorded envelope fixture
and the "no change without a pattern" claim. Both reviewers caught it; the fix
skips the branch entirely when nothing is configured.

*Drift: missing cross-cutting concern.*

## What to carry forward

- Measure the whole path a question takes, routing included, before trusting a
  component measurement. The join worked from the first spike; the run failed
  on the request-kind call and the re-check.
- A default flip needs a test that a not-configured install's wire is byte-for-
  byte unchanged; the recorded envelope fixture was the only thing that caught
  it.
- State a pass at its floor as a pass at its floor, with the ungated numbers
  beside it.
