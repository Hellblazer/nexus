# Post-Mortem: RDR-200 nx_answer Continuation Mode and the Composed-Retrieval Bridge Route

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

RDR-200 proposed a continuation mode for `nx_answer` (the tool that answers a
question by matching a stored retrieval plan, running its steps, and reducing
the evidence with a model). The server would still match the plan and run the
retrieval, but instead of forking a headless `claude -p` process to do the
reduction, it would hand the hydrated evidence and the exact prompt back to
the calling session, which already has a strong model in context. The fork
was where nearly all of the tool's dollars and most of its 80-second p50 went,
so the expected win was strong-model synthesis at near-zero marginal cost.
Phase 3 would have served the same envelope from the engine for editor and
plugin consumers.

## Implementation Status

**Parked — Alternative 4 accepted (Sam, 2026-09-03).** Phase 0 (the
per-operator prompt builders, extracted so the headless and continuation paths
share one byte-identical prompt) and Phase 1 (the opt-in envelope, the size
cap with headless fallback, the handoff telemetry and `nx_answer_report`)
shipped and stay in the tree. Continuation remains opt-in and dormant; the
headless path remains the default. Phase 2 (the default flip) and Phase 3 (the
bridge route) do not proceed. Beads nexus-4e75w, nexus-4e75w.7, nexus-5mft0,
nexus-5mft0.3 and nexus-mt9p8 closed on that decision.

---

## What we expected

That the reduction step was the problem. The disuse analysis showed retrieval
steps cost nothing and took seconds, while every dollar and most of the wall
time sat in the reduction dispatch. Move the reduction into the caller's
context and the tool becomes cheap and fast without losing quality. The RDR
named its own sharpest objection, R6: on today's corpus, the caller can just
search and reason itself. The pre-registered ship gate was built to test
exactly that, with the entropy argument predicting continuation would win at
least in the crowded stratum, where flat search drowns in adjacent facts.

## What actually happened

The gate ran three times and continuation never beat the caller working alone.

| Run | Date | vs headless (win/tie/loss) | vs caller-only (win/tie/loss) |
|---|---|---|---|
| Phase 1 | 2026-09-01 | 15 / 6 / 3, passed | 0 / 24 / 0, failed |
| Phase 1b | 2026-09-01 | 11 / 9 / 4, failed | 0 / 24 / 0, failed |
| Phase 1c | 2026-09-02 | 12 / 10 / 0, passed | 2 / 22 / 0, failed |

The margin did not appear in the crowded stratum either, which is the condition
OQ-1 set for Alternative 4 to return.

The judges' reasons converged on a single cause, and it was not reduction
quality. It was corpus reach. The caller-only arm made between four and nine
retrieval calls per question, re-phrased on a miss, listed the collections,
and named a collection explicitly when the default fan-out failed. The
plan-based arms, continuation and headless alike, took the plan's retrieval as
given, and that retrieval carried defects the gate itself surfaced: a
`corpus="all"` query returning zero hits from the RDR collection against
thousands of documents, and implementation source unreachable behind test
files. Continuation produced faithful, well-disciplined reductions of evidence
that had already missed the primary sources. A careful answer to a proxy
question still loses to an answer to the question.

Phase 1c fixed the scoping defect and ran on a held-out set. Reach doubled, and
continuation then beat headless cleanly. It still lost to the caller, and the
losses moved to degeneracy: eleven of twenty-three runs produced degenerate
plan output, from operator subprocesses that streamed but never reached a
terminal result, plan shapes that discard all but the final branch, and a
zero-evidence fallback that records none of its parameters. Those are filed
as independent bugs (nexus-4h0oh, ivv4d, kim0o, wi5h5, x79ne, 5dszx, mm5tx,
tx5hd, q4o43) and are not RDR-200 deliverables.

## What to check first next time

**Test the alternative that costs nothing before building the one that costs
a phase.** The caller-only arm needed no code. It was the RDR's own R6, and it
won every run. A gate that includes the do-nothing arm from the start would
have returned this verdict before Phase 0 was written.

**When the reduction is where the money goes, the retrieval is still where
the answer comes from.** The cost analysis was right and led to the wrong
lever. Every dollar sat in the reduction, and the reduction was never the
reason answers were worse. Before optimising a stage, check that stage is the
one deciding the outcome.

**A fixed plan cannot adapt, and a session can.** The caller's advantage was
not a better model or a better prompt. It was the second and third search
after the first one missed. Any design that freezes retrieval before the model
sees the result is competing against that loop, and should be measured
against it explicitly.

**Three runs of the same shape is a measurement.** The temptation after 1b was
to fix one more defect and re-gate. 1c was worth running because it changed
the mechanism under test (scoping) and used a held-out set. A fourth run
without a fresh pre-registration and a new mechanism would have been
rationalisation, and the bead's own rule forbade it.
