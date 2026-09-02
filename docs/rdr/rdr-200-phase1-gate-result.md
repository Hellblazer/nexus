---
title: "RDR-200 Phase 1 Ship-Gate Result (nexus-4e75w.7)"
parent_rdr: RDR-200
parent_prereg: docs/rdr/rdr-200-phase1-prereg.md
question_set: docs/rdr/rdr-200-phase1-questions.md
created: 2026-09-01
kind: companion
verdict: FAIL — Alternative 4
---

# RDR-200 Phase 1 Ship-Gate Result

**The gate FAILED its pre-registered rule. Alternative 4 (accept the
disposition; do not proceed to Phases 2-3) is the answer the protocol
returns.** Per the pre-registration's own terms this decision now goes
back to Sam with the data, exactly as the no-signal and refutation
paths both specify.

## The rule, and the measurement against it

Frozen rule (prereg §5): continuation must be **>= headless AND
strictly > caller-only**, ties awarded against continuation, with the
prediction that the margin over caller-only is **largest in the
crowded stratum**.

| Comparison | continuation | opponent | tie | Rule |
|---|---|---|---|---|
| vs **headless** | **15** | 6 | 3 | >= headless — **PASSED** |
| vs **caller-only** | **0** | 24 | 0 | > caller-only — **FAILED** |

Stratified (the entropy prediction):

| Stratum | vs headless | vs caller-only |
|---|---|---|
| crowded (12) | 8 / 2 / 2 | **0 / 12 / 0** |
| clean (12) | 7 / 4 / 1 | **0 / 12 / 0** |

The entropy prediction is **refuted in its own terms**: the margin was
not larger in the crowded stratum. It was a uniform loss in both.

## Method integrity

96 blinded comparisons (24 questions x 2 opponents x both position
orders), judged by `claude-opus-5` against the frozen rubric; arm
answers produced by `claude-fable-5` for both in-context arms
(same-model constraint held). Blinding was constructed by the
orchestrator; no judge read an arm directory, the question file, or the
unblind key. **Position consistency was perfect: all 48 pairs resolved
identically in both orderings, zero order flips** — the judging
measured content, not position.

## Why continuation lost — the mechanism, not an excuse

The judges' reasons converge on one cause, and it is **not** reduction
quality: **corpus reach**. In 10 of 12 pairs in the first batch the
deciding difference was that one side had the primary source (the
Knapp, p4est, Burstedde+Holke papers) and quoted equation, algorithm
and definition numbers from it, while the other had only implementation
code and honestly said so.

Both arms search the same corpus. The asymmetry is in **how**:

- The caller-only arm ran 3.6-8.8 retrieval calls per question,
  re-phrased on failure, called `collection_list`, and **named
  collections explicitly** when the default fan-out failed it.
- The plan-based arms (continuation AND headless) take the plan's
  retrieval as given — and that retrieval is subject to the defects
  this same gate discovered: `corpus="all"` returning **zero** `rdr__`
  hits against 7,615 documents, and `src/nexus/` implementation source
  being unreachable behind test files.

So continuation mode's reductions were faithful reductions **of
evidence that had already missed the primary sources**. The refusals it
produced were well-executed (naming stale premises, declining to invent
equation numbers, flagging unretrievable figures) — but a disciplined
answer to a proxy question still loses to an answer to the question.

This is the honest reading of R6, the RDR's own sharpest objection: on
today's corpus and today's retrieval, the caller *can* just do it
itself, and does it better, because it adapts and the plan does not.

## What the result does and does not establish

**Established:** continuation mode does not beat an adaptive caller
today (0/24, both strata). Phases 2-3 do not proceed.

**Also established, and worth keeping:** moving the reduction to the
caller is not itself harmful — continuation beat headless 15-6-3. The
placement change works; the *composed retrieval feeding it* is the
bottleneck.

**Not established:** that composed retrieval cannot beat adaptive
search once its retrieval defects are fixed. That is a different
experiment, and per the prereg's revision rule it would require a
**fresh pre-registration written before any new arm runs** — values
cannot be revised after results exist.

## Defects this gate surfaced (all filed, evidence-backed)

Two fixed and shipped during the run: `nexus-fe96p` (T1 stale-bearer
401) and `nexus-vzy2v` (the `claude -p` stdin race that degenerated
6/12 answers in the pilot wave). Five open: the payload-vs-arity
empty-evidence guard hole (P1, fix under review), `corpus="all"` prose
starvation, `src/nexus/` unreachability, the no-terminal-result
degenerate class, and budget under-enforcement at the bundle boundary.

## Methodology lessons for the next gate

1. **A committed question set contaminates its own corpus.** The frozen
   file is indexed in `rdr__1-1` and was retrieved as evidence by every
   arm (6/12 for continuation, q12 for caller-only, q04 for both).
   Exposure was symmetric so the comparison stands, but the next set
   should be held outside the indexed tree until the arms have run.
2. **Fix the retrieval substrate before measuring what reduces it.**
   This gate spent its budget discovering that the thing under test was
   downstream of a broken input.

## Costs

Set assembly and crowding labeling $13.59; pilot wave (invalidated by
the stdin race) plus two clean arm waves and 96 Opus judgments — the
projection was ~$56 for arms and judging.
