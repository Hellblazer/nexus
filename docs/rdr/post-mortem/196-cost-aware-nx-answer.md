# RDR-196 Post-Mortem: Cost-Aware nx_answer

**Closed** 2026-08-21 · **Accepted** 2026-08-20 · **Arc** nexus-nyry9 (24 beads, all closed)
**Gate verdict** T2 `nexus_rdr/196-gate-arc-close`

## What the RDR set out to do

Adopt NOMA's build order against `nx_answer`: measure per-step cost first, then
route operators to model tiers on that evidence, then rank plans by cost and
enforce a budget. Five enumerated gaps, four phases, each gated by the
measurement the previous one produced.

## What shipped

All of it, and the measurement discipline held at every gate.

- **Phase 0/1 — measurement.** Per-step `{operator, source, model, tokens,
  cost_usd, elapsed_ms, ok}` rows behind the run row; `cost_usd` is the sum of
  steps instead of a hardcoded `0.0`. This is the arc's most durable artifact:
  before it, nobody could answer what `nx_answer` costs or where its time goes.
  Every finding below came from it, including the ones that are unflattering to
  the arc itself.
- **Phase 2 — model routing.** Six operators (filter, groupby, extract, rank,
  check, verify) run 14–20× cheaper at the cheap tier on pre-registered
  criteria. Four (aggregate, summarize, compare, generate) were **refuted** for
  the cheap tier by a blind pairwise-judging study built specifically because
  the proxy for them did not exist. Every dispatch now names its model
  explicitly rather than inheriting a CLI default.
- **Phase 3 — cost-ranked choice and budget.** A derived default cap (p90 over
  n=30 conformant runs), min-cost-within-confidence-band plan selection, and
  budget enforcement as a mid-run step-boundary stop.
- **Phase 4 —** closed not-needed, with its argument recorded in the RDR rather
  than only in a bead.

## The finding that matters more than the delivery

**Cost was not the binding constraint.** The arc's own telemetry says so.

`nx_answer` exists to make a repeat analytical question fast and cheap by
reusing a stored plan. Measured at close, that is not happening:

- Cost-ranked plan choice has made **zero decisions in production**.
  `candidate_count` is never above 1 — 0 of 10 probe questions returned more
  than one above-floor candidate.
- Every library hit measured was an **FTS verbatim-repeat sentinel**, not a
  semantic match. Grown plans store the originating question as their own match
  text, so a plan can essentially only match the exact string that created it.
  The library is a cache with a one-key-per-entry hit condition.
- 6 of 10 plausible novel questions matched **nothing at all** and would pay for
  an inline planner.
- Usage corroborates: 213 runs since 2026-04-24 (~1.8/day), with a third missing
  the plan gate.

So Phase 3 built a correct selector for a choice that never arises, and enforced
a budget on a machine that rarely runs. **Further cost work is premature until
plans generalize.** The gate on `nx_answer` being worth its ~80s p50 and ~$0.80
per call is `nexus-93cc6` (plan generality, RDR-100's ownership) — *not*
`nexus-se36l` (estimator calibration), which only governs whether a choice would
be correct once a choice exists.

## Deviations, recorded rather than laundered

1. **Estimates come from step shape, not per-plan history.** A legitimate
   re-scope (Sam's option (a)), recorded in the RDR's Sequencing note — but the
   `.p3c` bead text still promised "median cost of the matched plan's history"
   and was never reconciled until the Phase 3 critic caught it. *Lesson: a
   re-scope must be pushed into every downstream bead's text, not just the RDR.*
2. **Pre-flight refusal shipped as a warning.** The estimator predicts an
   identical $0.90433 for all four live plans (bundle-dominant takes the max
   operator price; every plan's dominant operator is `generate` at the strong
   tier), which makes refusal a step function rather than a per-plan judgement.
   Demoted by decision on that measurement.
3. **Cost-ranked choice is delivered in code and unrealized in fact** — a
   correctly built mechanism with an unmet precondition, not a scope reduction.

## What the process caught that green tests did not

- The Phase 3 critic caught deviation (1) and the refusal defect — both invisible
  to a passing suite, because correct code faithfully implementing a
  mis-specified thing passes its own tests.
- Asking for a *test* of a co-occurrence the reviewer had already traced as
  correct found a **real bug**: the warning line was prepended unconditionally,
  so the first co-occurrence of a warning and an exhaustion marker would have
  broken the CLI's leading-prefix contract and silently reclassified
  budget-exhausted runs as successes. The trace was right about composition and
  wrong about ordering. *Lesson: "traced as correct" and "tested" are different
  claims; ask for the second when the first is load-bearing.*
- A saturated quality proxy (1.000 on every pair) can only fail to refute, never
  rank. Two beads turned on recognising that — one flip accepted with the caveat
  on record, one phase closed because the measurement it required could not
  discriminate.

## Honest limits of the evidence

- The derived cap comes from n=30 runs in a single seeding session with
  partly cache-priced verbatim repeats. It is now live enforcement with **no
  automated re-derive tripwire** (`nexus-se36l`).
- The candidate-count probe is 10 hand-chosen questions, 4 of them exact seeded
  strings — a demonstration that multi-candidate sets do not arise in this
  library, not a random sample of production traffic.
- The Path B/C retrieval bench was **deliberately not run** (decision recorded in
  the RG-3 verdict) once plan choice proved unreachable and the estimator half
  of enforcement was demoted. Path A's NDCG@3 of 0.787 stands as the retrieval
  baseline of record; no Path B/C figure exists and none should be claimed.
- The inline planner's tier remains **unmeasured** by decision; `nexus-i8to5`
  (no StepRecord for the planner dispatch) is its prerequisite.

## Successors

| Bead | Why it outlives this RDR |
|---|---|
| `nexus-93cc6` | Plan generality — **the actual constraint**. RDR-100 owns it. |
| `nexus-7g0rg` | Scope-anchor drops a 0.94-confidence grown plan. RDR-100 owns it. |
| `nexus-9frst` | Grown-plan first-rematch flakiness; every failure is a repaid planner. |
| `nexus-e1ti4` | Virgin installs seed zero builtin plans — 100% miss on a fresh box. |
| `nexus-se36l` | Estimator returns one constant per plan; no re-derive tripwire. |
| `nexus-8uk28` | `check`'s `ok` bool gates branching on a saturated proxy. |
| `nexus-i8to5` | Planner dispatch invisible in per-step telemetry. |
