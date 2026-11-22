# RDR-252 post-mortem: groups, relevance and one responder

## What the RDR set out to do

Sessions already coordinated in named crews (the push train led by halcyon,
push windows, review pairs), but only in prose: no tool could ask who is in a
crew, who leads it, or who owns a task. Every session with an aura on evaluated
every board event, relevant to it or not, and nothing said which session should
answer a red develop run, so several did or none did. The board also did not
record which session pushed a commit, and the displayed red or green followed
the order posts arrived in, not commit order.

The RDR added groups (membership and a leader, kept in the engine), loading an
aura only in member sessions, and one responder for a develop red: the engine
stamps an owner post naming the pusher of the earliest suspect commit, wakes
that session and the next one up, and tells every other member to hold their
pushes. It also added a push record naming the pushing session, and commit
order for red and green.

## Implementation status

Implemented. Phase 1 (code) and Phase 2 (activation) are done:
- `push-train` is formed (leader halcyon, `serves = ["nexus-develop"]`,
  `ack_deadline` 20), and `ci_develop_red` loads by membership.
- The first real red, on 2026-11-20, named its owner, woke it, and was acked in
  85 s.
- The engine half is first in engine-service-v0.1.200 (live 2026-11-21 15:29Z).
- The CI board adapter's `FOLD_BRANCH_OUTCOME` switch went on at 16:23Z.
- The client half is first in conexus v8.5.4.

The close critique ran five rounds. Round 5 (T2
`nexus/critique-rdr252-close-2026-11-21-r5`) found it justified, with no
Critical and no Significant.

## What diverged

**Typed assignments were cut.** The title still says "Act by Assignment", but
gate round 1 cut typed assignments. What shipped is membership, the leader and,
for a develop red, the owner post. Gap 1 and the Finalization Gate disclose the
cut, and Sam kept the title.

*Drift: scope underestimation.*

**The engine ack half missed the release planned for it.** The activation bead
said the close waited on the fix-code-first engine half in 0.1.199. Two parts
were not in 0.1.199: the ack `from` stamp (b308e6dc9) and the 35-day `--owner`
retention (b545859c0). Both rode 0.1.200. The RDR text kept describing them in
the present tense while they were only on develop. Close critiques r3 and r4
both flagged that, and the fix was a release-carrier line for each behaviour.

*Drift: deferred critical constraint.*

**The earliest suspect is not always the cause.** On 2026-11-20 a red at
9f14aa59a named 11da32740 as the earliest suspect, and that commit went green on
rerun. The cause was the later commit, so the owner row named the wrong pusher.
Known limits records the case. Whether the stamper should drop a suspect once
its own run is green is one of the questions left for the calibration read.

*Drift: missing failure mode.*

**A carried skipped or neutral run needed its own data and a second switch.**
Carry-forward assumed a run with no result on a sha could take the last real
result. Making that correct needed a `branch_outcome` dim, an adapter change in
the conexus repo, and an adapter switch turned on only after the engine was
live. The RDR says nothing in this repository watches the assumption. The
decision on it is carried by a dated thread.

*Drift: unvalidated assumption.*

**The fail-safe trusted any ack.** When the liveness read fails, the fail-safe
holds escalation off for an ack. As first written, any ack held it off. It was
narrowed to the owner's own stamped ack, on the row's tip, at or after the row.
The known side effect: ack rows from before 0.1.200 with a typed `from`
escalate with `owner` null under that compound fault.

*Drift: missing failure mode.*

**JDR-002 fell behind the engine.** The engine now refuses a client post that
claims `from=engine` on `board/ci` (403 `reserved_dim`), and stamps an ack's
`from` from the sender session. JDR-002, the joint record on sender trust, still
says ack replies are unguarded and `from` is a label. A joint record is Sam's
edit, so the revision is an open ask (`jdr002-revision`) and not part of this
close.

*Drift: missing cross-cutting concern.*

**Most MVV legs were verified locally only.** The local sandbox passed 7 of 7.
The live repeat covered the group verbs, and the first real red evidences the
live wake. The legs for a late red or late green, a replaced run, a lapsed
owner and `/clear` were verified locally only.

*Drift: none.* This was a narrower verification, recorded so nobody reads it as
live.

## What trails the close

The calibration and attribution read is due 2026-12-20. It is carried on the
threads board (thread "RDR-252 calibration and attribution read") under Sam's
2026-11-19 ruling that a measurement may trail a close. It covers:
- ack time, now that acks are stamped;
- whether the first owner row named the real cause;
- how often a non-owner fixer acked through the owner;
- the carried skipped or neutral decision;
- whether path-filtered workflows should hold pushes;
- whether the stamper drops a suspect whose own run is green.
