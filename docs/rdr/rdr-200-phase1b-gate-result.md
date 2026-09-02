---
title: "RDR-200 Phase 1b Ship-Gate Result (re-gate on the repaired retrieval substrate)"
parent_rdr: RDR-200
parent_prereg: docs/rdr/rdr-200-phase1b-prereg.md
phase1_result: docs/rdr/rdr-200-phase1-gate-result.md
question_set: docs/rdr/rdr-200-phase1-questions.md
created: 2026-09-01
status: complete
verdict: FAIL — Alternative 4 stands
---

# RDR-200 Phase 1b Ship-Gate Result

**The re-gate FAILED its pre-registered rule on both legs.** Alternative 4
(accept the disposition; Phases 2-3 do not proceed) stands. Per the
pre-registration the decision returns to Sam with the data.

## The rule, and the measurement against it

Frozen rule (Phase 1b prereg §5, inherited from Phase 1): continuation
must be **>= headless AND strictly > caller-only**, ties awarded against
continuation, margin over caller-only predicted largest in the crowded
stratum.

| Comparison | continuation | opponent | tie | Rule |
|---|---|---|---|---|
| vs **headless** | 11 | 9 | 4 | >= headless — **FAILED** (11 vs 13 with ties against) |
| vs **caller-only** | 0 | 24 | 0 | > caller-only — **FAILED** |

Stratified on the re-scored strata (10 crowded / 14 clean):

| Stratum | vs headless | vs caller-only |
|---|---|---|
| crowded (10) | 4 / 5 / 1 | **0 / 10 / 0** |
| clean (14) | 7 / 4 / 3 | **0 / 14 / 0** |

Phase 1 for comparison: vs headless 15 / 6 / 3 (passed), vs caller-only
0 / 24 / 0 (failed). The caller-only leg is unchanged to the question.
The headless leg moved from a pass to a fail: 5 of the 11 headless pairs
continuation did not win were identical degenerate outputs on both
sides (q06, q08, q12, q19 listings or failure notices, q01 a budget
warning), which the rule counts as ties against continuation.

## Method integrity

96 blinded comparisons (24 questions x 2 opponents x both position
orders), judged by `claude-opus-5`; arm answers by `claude-fable-5-1`
for both in-context arms. **Zero order flips across all 48 pairs.**
Judges read only their pair files. All 19 continuation handoffs have a
handoff row and a paired `nx_answer_report` row in telemetry (38 rows):
zero telemetry-dark, zero unreported. 5 of 24 continuation calls fell
back to a composed answer (q06, q07, q08, q12, q19); no envelope hit the
size cap.

**One protocol deviation, recorded in the prereg Revision History
before any caller-only verdict was tallied:** the first caller-only
pass for q13-q24 answered q14-q24 from direct repository reads and
injected session memory rather than from the indexed corpus through
the retrieval tools. Those eleven answers were set aside unjudged and
re-run under an explicit MCP-only restriction; only the re-run answers
were judged. The re-run arm still won all eleven. q01-q13 were MCP-only
in the first pass.

## Why continuation lost again: reach, not reduction

The prereg §5 retrieval-reach measurement (T2
`nexus/analysis-rdr200-phase1b-retrieval-reach-2026-09-01` [24044];
per-question table in the gate artifacts) asks, for each question,
whether the primary source the caller-only arm's answer rests on
appeared anywhere in the plan-based arms' evidence:

| arm | primary source present | partial | absent |
|---|---|---|---|
| continuation | 7 | 7 | 10 |
| headless | 9 | 4 | 11 |
| caller-only | 24 | 0 | 0 |

The discriminator is question shape, not stratum: paper-corpus
questions reached their paper in 2 of 12 in both plan arms, and both of
those were the degenerate `query()`-listing path, not a composed plan.
The pyramid/SFC family (q01-q05, q13) returned zero paper text across
all twelve plan-arm runs. The caller-only arm named `knowledge__dt-papers`
explicitly on nine questions and reached it every time.

The corpus-scoping cause is now pinned to code. A plan retrieval step
gets its corpus from, in order: a plan-declared `corpus`; the plan's
`scope.taxonomy_domain` through `_DOMAIN_TO_CORPUS` in
`src/nexus/plans/runner.py` (`prose` maps to `knowledge,docs,rdr,paper`,
`code` to `code`); a caller `scope`; else the tool default, which is
`knowledge,code,docs` for `search` and `knowledge` for `query`. Neither
default includes `rdr__`. The caller-only arm named an `rdr__`
collection on 10 of 24 questions. The frozen question file lives only
in `rdr__1-1` and still leaked into 13 of 24 continuation and 12 of 24
headless evidence sets, so some plan steps do carry the `prose` domain
and do reach `rdr__`; what they retrieve from it is the question file
itself rather than the decisive RDR, which is a query-phrasing problem
(the step queries are the question text or a planner paraphrase of it,
and the file that quotes the question verbatim wins that match).

**What the two shipped fixes did and did not change.** Crowding moved:
6 of 24 questions changed stratum, the crowding probe's self-retrieval
went from present to 0/24, and the flat top-10 for the corpus.py and
core.py probes now leads with the implementation file. Neither fix
touched plan-step corpus scoping or step query phrasing, and the reach
numbers say those are what decide the gate. This is the Phase 1
mechanism finding, now with the residual isolated to the plan runner.

## Defects this gate surfaced (evidence in the gate artifacts)

- **Degenerate outcomes are under-counted server-side.** 9 of 24
  questions produced a degenerate plan-arm output (query() listing
  reroute on q06/q08/q19 both arms; empty hydration q12 both arms;
  budget-warning-only body q01 twice, q03, q13, q15; planner timeout
  and budget exhaustion q07). `nx answer-runs` reports
  `degenerate_count` 1 for the window. Bead filed.
- **Plan retrieval is not stable across invocations.** q07 drew two
  different dynamic plans (4 steps vs 6) from the same plan id; q24's
  continuation and headless runs retrieved different evidence for the
  same question. Bead filed.
- **The gate's own artifacts contaminate the corpus** (`rdr__1-1`
  indexes `docs/rdr/`). Symmetric across arms, so the comparison
  stands, but the headless q10 answer is literally the leaked Phase 1
  crowding record for q10. The next set must be held out of the tree,
  as Phase 1 already recommended; Phase 1b reused the frozen set by
  decision for comparability.
- **Continuation envelopes as recorded carry no retrieval provenance**
  (15 of 24 raws have no collection or distance fields on evidence
  items), so the reach measurement had to infer collections from
  content. The envelope's `hydrated_bundles` provenance is specified in
  RDR-200 but the arms did not see it in the rendered instruction.
  Bead filed.

## What the result establishes

- Continuation mode does not beat an adaptive caller on today's
  substrate even with the scoring-layer defects repaired (0/24, both
  strata, both gates). Phases 2-3 do not proceed.
- The caller-only arm's advantage is reproducible and mechanical: it
  names collections and rephrases; a plan does neither. The fix, if one
  is attempted, is in plan-step corpus scoping and step query phrasing,
  not in scoring.
- Moving the reduction to the caller is still not harmful in itself:
  where both plan arms retrieved the same evidence, continuation won or
  tied; it lost the headless leg only through the degenerate-output
  ties.
- Any further measurement requires a fresh pre-registration written
  before new arms run, and a held-out question set.

## Costs

Crowding re-score $9.54 (24 Opus labelings). Arms and judging ran on
session tokens (8 arm agents, 6 judge agents, 1 analyst); the headless
arm's server-side operator spend rides `answer_runs`.
