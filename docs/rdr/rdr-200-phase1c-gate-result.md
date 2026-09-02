---
title: "RDR-200 Phase 1c Ship-Gate Result (scoped plan runner, held-out set)"
parent_rdr: RDR-200
parent_prereg: docs/rdr/rdr-200-phase1c-prereg.md
question_set: docs/rdr/rdr-200-phase1c-questions.md
previous_results: [docs/rdr/rdr-200-phase1-gate-result.md, docs/rdr/rdr-200-phase1b-gate-result.md]
created: 2026-09-02
kind: companion
verdict: FAIL — Alternative 4 stands
---

# RDR-200 Phase 1c Ship-Gate Result

**FAIL on the caller-only leg; PASS on the headless leg.** Alternative 4
stands for the third time. The decision returns to Sam with the data.

## The rule, and the measurement against it

| comparison | continuation | opponent | tie |
|---|---|---|---|
| headless | 12 | 10 | 0 |
| calleronly | 2 | 22 | 0 |
| headless/crowded | 8 | 4 | 0 |
| headless/clean | 4 | 6 | 0 |
| calleronly/crowded | 1 | 11 | 0 |
| calleronly/clean | 1 | 11 | 0 |

Rule: continuation >= headless (PASSED, 12 vs 10) AND strictly >
caller-only (FAILED, 2 vs 22). Zero order flips across all 46 pairs.
Two headless gaps (q15, q22: no answer text on either attempt) are
excluded from the headless comparison, as in Phase 1b. First time
continuation won any caller-only pair (q02_calleronly, q14_calleronly); still 1 of
12 in each stratum.

Trend across the three gates, vs caller-only: 0/24, 0/24, 2/24. Vs
headless: 15-6-3, 11-9-4, 12-10-0.

## Method integrity

Held-out set: 24 questions authored against verified anchors, never in
the repo until this commit; the crowding probe and every arm reported
zero self-retrieval of gate artifacts. Judge claude-opus-5, arms
claude-fable-5-1, caller-only MCP-only from the first pass (all 24
files report non_mcp_accesses empty). Judges read only their pair
files. Telemetry: 17 continuation handoffs, all reported.

## What changed and what did not (T2 [24082])

Reach against the question's anchor: continuation 13 yes / 6 partial /
5 no (Phase 1b: 7/7/10); headless 15/7/2 (9/4/11); caller-only 23/1/0.
The nexus-rl59s fix is visible in the artifacts: query() listings now
span 58 collections where Phase 1b's spanned 11, and rdr__ rows appear
in plan-arm evidence for the first time. Reach roughly doubled and the
gate still failed, because the plan path now loses on three other
things:

- **Degeneracy worsened:** 11 questions / 23 arm-runs (Phase 1b 9/15):
  listing reroute 9, zero-evidence fallback 4, bare hydration envelope
  4, budget-warning-only body 4, budget exhaustion 2. All four dominant
  classes return non-empty well-formed strings, so the server-side
  degenerate counter still does not see them (nexus-x79ne).
- **Zero-evidence fallbacks are plan-side, falsified in-run:** q15/q16/
  q21 returned "no evidence" in one arm while the other arm or the
  caller retrieved the anchor from the same corpus. Those raws carry no
  plan id, step basis, or corpus arg, so which plan-side factor cannot
  be read (recording defect, bead filed).
- **Plan shapes discard evidence by construction** (q23: three
  independent summarize steps under "return only the final step",
  two-thirds of the retrieved-and-reduced material thrown away; q24
  answers only the fourth sub-question). Judges decided most pairs on
  coverage of multi-part questions; the caller answers every part it
  can retrieve, a fixed plan answers what its last step emits.
- **Evidence noise:** the question text enters the rank step's own
  candidate list (5 of 15 composed runs); nexus's operator eval
  fixtures and docs/rdr/rdr-200-phase1-questions.md retrieved as
  evidence for paper questions.

Budget: the same $1.053 cap in both arms, over-cap warnings on most
runs after q13; the "already spent" figure is not monotone across
question order, so it is a rolling measure, not a shared draining
ledger.

## What the three gates establish

- Moving the reduction to the caller is not harmful (won or tied the
  headless leg in all three gates).
- Composed retrieval as a fixed plan does not beat an adaptive caller
  on this corpus, before or after the scoring and scoping fixes. The
  caller's advantage is now coverage and adaptation (rephrase on empty,
  name a second collection, answer every sub-question), not corpus
  reach.
- Any further attempt is a plan-runner capability (adaptive retrieval,
  fan-in plan shapes, evidence hygiene), not an RDR-200 change, and
  would need a fourth pre-registration.

## Costs

Crowding $14.04 (36 Opus labelings); arms and judging on session
tokens (8 arm agents, 4 judges, 1 analyst); headless server spend in
answer_runs.
