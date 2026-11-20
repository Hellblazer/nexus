# RDR-245 post-mortem: structured asks on the threads board

## What the RDR set out to do

Sam's decisions reached him through a page linda rebuilt by hand each day
from free-text `needs_sam:` lines, and his answers went back through linda as
peer mail, which the receiving session is told not to treat as his approval.
The RDR made an ask a typed block on the session's threads entry (question,
context, options, `reversible:`, `blocking:`, optional `after:`), answered
through a mediator session running the conexus `asks` skill or through the
`nx ask` CLI. The answer is a T2 record holding Sam's own words, and the
asker's drain hook frames the mail as an unverified claim that authorizes at
most the one option it names.

## Implementation status

Implemented. Phase 1 closed on 2026-11-09 (epic nexus-wc4jj, all 16 children
closed). The Minimum Viable Validation ran on a real engine plus a manual pass
on 2026-11-09. By 2026-11-19 the T2 `asks` project held 105 answer records,
nearly all through the mediator path.

The RDR said it closes after a hand query of its Success Metrics around
2026-11-23. Sam closed it before that, on 2026-11-19: "go with all of it"
(T2 `nexus/sam-ruling-rdr-closeout-batch-2026-11-19`), which closes RDRs
whose build is done and only a measurement remains, and for RDR-245
supersedes his U3 hold of 2026-11-11. The query still runs on 2026-11-23, and
its numbers are appended below.

## What diverged

**Routing moved from the session id to the identity, and the RDR did not
follow.** RDR-253 Step 5 (2026-11-18) made an entry's `identity:` line route
answers and key their records (`<identity>/<ask>/<version>`), because an
identity outlives `/clear` and a session id does not. The RDR's 2026-11-11
wording pass predated it. The close critique found the text describing
session-only routing, and found the digest still matching session UUIDs
only, so `nx ask digest` skipped the four newest answers. Both were fixed at
close (close critique S1 and S2). One mixed-fleet gap is left: for an entry with
an identity line, `nx ask list` reads the answer status from the identity-keyed
title only, so a record a pre-Step-5 client wrote under the session title shows
no status there (`show`, `answer` and the console read both titles). It
narrows as old clients retire.

*Drift: missing cross-cutting concern* (a later RDR changed this one's key,
and no test tied the digest to the record writer).

**Several mechanisms were cut or reshaped during Phase 1, each by Sam's
decision.** The console ask cards and the tmux popup surfaces went
(Alternatives 3 and 6); `default:` went to lazy consensus; `nx ask lint` was
folded into `nx ask list`; `after:` became conditional only on a real value;
the log-mined latency baseline was not taken (Sam, 2026-11-11); and Step 0
measured the cloud engine only. Each is recorded in the Revision History as
a decision or a wording amendment to reviewed code.

*Drift: none* (decided scope changes, recorded as such).

**Two success metrics cannot be computed as written.** "Zero disputed
answers, counted from mailbox replies" has no source: there is no reply-to,
and the record carries no replace marker. "At least 80% of open asks
structured" has a near-zero numerator by design, because a structured ask is
deleted once answered, while the legacy list counts plain `needs_sam:` and
state lines that are not asks. The metric section was written before the
record shape settled.

*Drift: unvalidated assumption.*

**Two assumptions were never verified.** That Sam can use AskUserQuestion from
his phone over mosh (Decision 2, Gap 5): the answer records show the mediator
path works, but none records which device or client Sam answered from. That
sessions write a `context:` line good enough to answer from (Gap 2): no
sample was read. And `isatty` on a Windows console is inferred, not read.

*Drift: unvalidated assumption.*

## What held

- The trust model: the drain hook's frame and its pinned sentence, the
  one-option authorization, and the re-read of the asker's own entry before
  acting. Sessions confirmed with Sam on `reversible: no` asks as designed.
- Gap 3 is closed only in part, as Decision 1 says: the relay hop is gone,
  but the asker still confirms with Sam for an irreversible or unclear answer.
- The portability claim is enforced by an AST walk of the ask branch
  (`tests/hooks/test_mailbox_drain_ask_grammar.py`).

## The 2026-11-23 query

Run on 2026-11-23 over T2 `asks` (both title forms, now that the digest reads
them) and the board. The forms below are the computable versions the close
critique asked for (S3):

1. Share of answers by `via`: count of records per `via` value.
2. Disputed answers: answer records rewritten for the same ask and version,
   plus any ask Sam re-answered differently in a later version. A dispute not
   rewritten is not countable and is reported as such.
3. Structured share: asks ever written as structured blocks (answer records
   plus open structured asks) against legacy `needs_sam:` lines that are real
   asks, with `none` and state lines excluded by hand.
4. Median time from `asked:` to record `time`, with no baseline.
5. Interrupt-class asks per day: records with `blocking: yes` plus open
   blocking asks.
6. Gap 3 regression: an ask id re-asked after a `sent` record.
7. Gap 2: a sample of ten `context:` lines, each marked answerable from the
   line alone or not.

Results: (to be appended 2026-11-23)
