# RDR-250 post-mortem: multi-hop answers read past the six-chunk budget

## What the RDR set out to do

`nx_answer` resolves customer ids to companies only from the chunks its
generator is handed, and plan 10 handed it 6. On the HERB benchmark's five
issue questions (issue5) that capped the answer at F1 0.500, while the search
step's own 40 hits held ids worth far more. The RDR raised the budget to 20
(Step 1) and added a pool key scan: ids found in search hits 21 to 40 are
resolved too, but only when the coverage assessment cites their chunk as
support (Steps 2 and 3, behind `NX_ANSWER_POOL_KEY_SCAN`, off by default).

## Implementation status

Implemented. Step 1 shipped as 4a25a7c5a (plans 10 and 8 default to 20). Steps
2 and 3 are on develop at 7c44ceee8, off by default, CI green. The MVV (Step 4)
passed the bar Sam signed before the run: issue5 recall .827, precision .967
(pooled .950), F1 .878, join completeness 1.0, against a same-sandbox control
of .764 (T2 `nexus_rdr/250-research-8`). Turning the switch on is not done: Sam
adopted five criteria for it (`250-turn-on-criteria`) and bead nexus-w8d30
carries the measurement and the flip. Gap 2, hop-1 ranking, stays open on
nexus-io7tw.

## What diverged

**Built before the gate.** Sam chose to measure the filter before gating
rather than accept an unverified assumption, so the code was written from a
draft Technical Design, and the design was then rewritten to match the code.
The gate therefore checked prose against code it had been derived from. The
tests (`tests/test_rdr250_pool_key_scan.py`) were the stronger evidence.

*Drift: under-specified architecture.*

**The first design let the pool starve its own floor.** The draft had the
filter share the resolve step's time window. Review found that a slow fetch or
filter left `resolve` under its one-second minimum, so even the anchors' ids
stopped resolving: worse than the switch off. The fix ran in two phases:
resolve the anchors exactly as before, then do the pool work in its own budget,
skipped up front when one assessment call cannot fit.

*Drift: missing failure mode.*

**A covered facet's evidence ids include contradicting chunks.** The draft
filter kept a pool id when its chunk sat in a covered facet's `evidence_ids`.
That list holds contradicting chunks too, which would have kept exactly the
ids the filter exists to drop. The filter now reads `supports` records only.

*Drift: framework API detail.*

**The first paid run measured nothing.** The MVV reused an existing HERB
sandbox whose engine (0.1.180) predated the search exclusion the 8.3.0 client
requires (0.1.182 and later). All ten calls were refused before retrieval. A
fresh sandbox on the client's pinned engine fixed it. A free probe before the
run used a plain search and so missed the refusal.

*Drift: missing cross-cutting concern.*

**The cost comparison was confounded by run order.** The pool pass ran first
and the control second, so the control reused prompts the pool pass had just
sent. The doubled cost was mostly that; like for like, the pool added 8 to 15
percent where it kept ids. The adopted turn-on criteria require an interleaved
pass for this reason.

*Drift: unvalidated assumption.*

**Step 1 moved two plans, and the text named one.** 4a25a7c5a changed plan 8's
default with plan 10's; the RDR said plan 10 until the gate's fix check found
it.

*Drift: scope underestimation.*

**Raising the budget joined the two gaps.** At budget 6, Gap 2 (hop-1
ranking) limited only the hand-written script. At 20, `nx_answer`'s anchors are
the hop-1 top 20, so Gap 2 binds it too, and the RDR's "separate limits"
rationale stopped being true. The close critiques found it.

*Drift: internal contradiction.*

**The MVV's filter result did not replicate.** The turn-on measurement
(2026-11-17, T2 `nexus_rdr/250-turn-on-run-2026-11-17`, bead nexus-w8d30) ran
issue5 twice more with the switch on. The first pass matched the MVV exactly,
keeping the same four gold ids. The second kept none: the filter rejected the
same four ids on the same evidence, and the answer scored the control's .764.
In the run that alternated on and off per question, no pass kept an id either.
The passes that kept ids were each the first after a sandbox start, and their
calls cost two to four times more. The cause is not known. The MVV's one pass
was the lucky case, so the switch stays off.

*Drift: unvalidated assumption.*

## What to carry forward

- Before a paid run on a reused sandbox, check that its engine meets the
  client's pinned engine, using the same call the run will make, not a plain
  one.
- A cost comparison between two arms runs them interleaved, or both cold.
- When a design is built before its gate, say so in the RDR and treat the
  tests as the record; the prose will follow the code.
- A model-call filter is measured on at least two passes before its result
  is reported as a pass; one pass can be the lucky one.
