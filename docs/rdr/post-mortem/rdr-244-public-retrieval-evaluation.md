# RDR-244 post-mortem: a public retrieval evaluation others can run

## What the RDR set out to do

Nexus had no retrieval number anyone outside the project could check. The
internal bench (RDR-090) used hand-labelled queries over this repository's own
RDRs, its one pre-registered run had failed, and that failure was never
published. RDR-244 set out to close four gaps: a retrieval number on a public
benchmark (Gap 1), a paper-path answer-quality number (Gap 2, deferred to
Track B by the owner on 2026-11-03), a harness a third party can run with pinned
data (Gap 3), and publication of the failed internal result (Gap 4). Track A
picked a five-dataset subset of CoIR (code retrieval, Apache-2.0), loaded it on
a local engine embedding with bge-base-en-v1.5 at no cost, and scored `nx search`
with NDCG@10, recall@10 and MRR under a pre-registration committed before any run.

## Implementation status

Implemented. The harness (`scripts/bench/coir_run.py`, `coir_sample.py`,
`coir_score.py`) shipped with its tests. The pre-registration was committed at
cc378a4a6, and the run took place on 2026-11-07 on chas. Results and the
RDR-090 figures were published in `docs/rdr/evidence/rdr-244/results.md`
(587af4687, 3c946044a). Sam's decision from the result is "baseline only": the
numbers are a reproducible baseline with no product decision and no comparison
claim against CoIR. Epic nexus-2k66t is closed (7/7). The memory-capture child
nexus-2k66t.1 is deferred on RDR-209 and does not block.

The close critique (T2 `nexus/critique-d76add41d-nexus-2k66t.2-2026-11-16-r1`)
came back partial, with two documentation findings. Both were fixed before close
(47030d594, 1fe11b5ad), and the confirmation round
(`nexus/confirm-47030d594-nexus-2k66t.2-2026-11-16-r2`) found one more bug in
the new run recipe, also fixed.

## What diverged

**The run-time assumption held for three datasets and failed for two.** Step 0
measured 0.03 to 0.18 s per document and projected about 15 minutes for a
5,000-document sample. In the run, codetrans-contest took 0.58 s per document,
and neither CodeFeedback dataset finished its sample inside the 30-minute cap.
Both were dropped by the cap, as the pre-registration allowed. Those two were the
subset's only plain-language-to-general-code datasets. The published number
therefore covers code translation and text-to-SQL, not searching general code
from a request in words. Step 0 timed a few documents. The cost that counts is
the whole sample, on the run's host.

*Drift: unvalidated assumption.*

**The first attempt died with its client session.** Attempt 1 ran under a client
session that dropped after about 60 minutes and took the run with it. Sam had it
rerun unchanged, and attempt 2 is the published run. The two codetrans datasets
reproduced exactly across attempts, a check nobody planned. synthetic-text2sql
has one run only.

*Drift: missing failure mode* (a long run tied to an interactive session).

**Gap 3 was built but not written down.** The harness, the pins and the results
format existed, but until the close nothing outside the RDR said how to fetch
the data, that the run needs a source checkout with the gate jar built, or that
the production-write guard needs a scoped opt-in. The first recipe written at
close put the data inside the checkout, where the harness's clean-tree check
refuses it. The confirmation critique caught that before close.

*Drift: missing Day 2 operation.*

**The RDR text was not updated as the run happened.** At close, Assumption 4
still read as verified, and two finished prerequisites and Step 2 were still
unticked. They were reconciled under standing grant 3.

*Drift: missing cross-cutting concern* (no step owned keeping the RDR in step
with the run).

## What to carry forward

- Time a pre-registered cap on the full sample on the host that will run it, not
  on a spike's few documents.
- Run anything longer than a few minutes detached (nohup inside `lab-run`) from
  the start, not only after the first loss.
- "A third party can run it" is only true once someone has followed the written
  steps from a clean checkout. Write the recipe with the harness and check it
  against the harness's own refusals.
- Update the RDR's step records in the same commit that records a run.
