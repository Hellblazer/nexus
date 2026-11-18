# RDR-235 post-mortem: the aspect extraction queue onto the tuple-space queue template

## What the RDR set out to do

Nexus ran hand-built work queues, each with its own claim, lease and retry rules.
The aspect extraction queue had no claim id, so a slow batch could be reclaimed
while its worker was still extracting and two workers would extract the same
document. A row whose worker died every time was reclaimed forever, idle workers
polled, and the tuple space lacked the pieces (delayed visibility, replace on a
deterministic id, immediate dead-lettering) the queue would need. RDR-235 moved
the aspect queue onto the RDR-211 `queue/` template, with the aspect write itself
checking the claim (E5), and recorded why the PDF pipeline row stays where it is.

## Implementation status

Implemented. Epic nexus-5r2sc is closed (4/4), with the Phase 2 cross-walk
(T2 `nexus/rdr235-phase2-review-gate-2026-10-31`) finding no gaps and its three
residuals (nexus-7rwaj, nexus-eyynd, nexus-l0ai3) closed. The tuple queue became
the default in conexus 7.90.0, the producer's table mode was retired in 7.95.0,
and the table, its 15 routes and the migration rung were removed in 7.97.0 with
engine-service-v0.1.181.

The close critique (T2 `nexus/critique-d76add41d-rdr-235-close-2026-11-16-r1`)
found the substance justified but the text out of date. It still described the
transitional design and missed rulings made after acceptance. The wording was
reconciled under standing grant 3, and the confirmation round
(`nexus/confirm-ae194b703-rdr-235-close-2026-11-16-r2`) judged the close
justified.

## What diverged

**The retirement went further and faster than the plan.** The RDR planned a
flag, a worker that drained both queues, an upgrade-ladder rung that moved rows,
and a later separate changeset to retire the table. In practice the flag was
flipped, then the table side was removed in two steps within a week. The rung
gave way to `AspectQueueDrain`, a one-shot move at engine start that also turns
failed rows into dead tasks, where the rung would have left them in the table.

*Drift: under-specified architecture.*

**The drain runs at engine boot.** It shipped on 2026-11-06, four days before
Sam's 2026-11-10 ruling that a release must not rewrite user data at engine
boot. It acts only on a non-empty table, and the five managed tenants measured
zero rows, so the exposure is a local install that still holds queue rows.
Whether it needs a change is Sam's call; the RDR records it without a verdict.

*Drift: missing cross-cutting concern.*

**Sam ruled on a case the design had not covered.** A dead decision on a claim
whose row had been replaced while claimed ("dirty") was, by the text,
dead-lettered at once. Sam ruled on 2026-10-31 (nexus-7rwaj) that it returns to
available with the new body and its attempts reset, because the replace carries
new work. Review also added a 403 `ClaimTaskMismatch` for a live claim used on
another document's task (nexus-eyynd), and a fix made a first-claim "document
gone" verdict a short retry instead of a dead letter (nexus-1o3ft).

*Drift: missing failure mode.*

**The text stopped tracking the code after the last revision.** At close, the
RDR still listed settled assumptions as open, showed a template file that no
longer matched the shipped one, and described doctor gaps that had been fixed.
Each change was recorded on its bead, not in the RDR.

*Drift: missing Day 2 operation* (no step owned bringing the RDR up to date as
the transition finished).

## What to carry forward

- A transition plan should say when its transitional parts are retired and who
  updates the RDR when they are.
- A data move that runs at engine start is a boot-time migration under Sam's
  2026-11-10 rule; new ones should be visible, resumable jobs.
- When review or Sam rules on a case the decision text does not cover, add the
  ruling to the RDR in the same change, not only to the bead.
