---
title: "RDR-200 Phase 1 Pre-Registration (nexus-4e75w.2 deliverable)"
parent_rdr: RDR-200
parent_bead: nexus-4e75w.2
related_beads: ["nexus-4e75w.7", "nexus-5mft0.3", "nexus-rv9xp"]
created: 2026-09-01
status: frozen-pending-question-set
---

# RDR-200 Phase 1 Pre-Registration

Frozen gate protocol for the Phase 1 ship gate (`nexus-4e75w.7`): a
stratified three-arm blind comparison — continuation vs. headless vs.
caller-only — that decides whether `nx_answer` continuation mode ships
or Alternative 4 (search-and-reason, no composed retrieval) stands as
the answer. Written so the gate can be **run** by someone who was not
party to the design discussion, without interpreting anything.

## 1. Provenance

- **Design of record:** `docs/rdr/rdr-200-nx-answer-continuation-mode.md`
  (RDR-200), accepted 2026-09-01. Finalization gate PASSED on the
  second run (0 critical, 0 significant) — T2
  `nexus_rdr/200-gate-latest`; full critique T2
  `nexus/critique-rdr-200-gate-2026-09-01` [23912].
- **Pinned parameters:** Sam, 2026-09-01, T2
  `nexus/decision-rdr200-oq-pins-2026-09-01` [23922] (OQ-2/OQ-6/OQ-7 plus
  the tiering addendum). This file is the pre-registration **of
  record** — the RDR's own requirement is that these values are pinned
  before Phase 1 runs, never after.
- **Revision rule:** every value in this document is frozen at the
  commit that introduces it. A change is permitted **only before any
  arm runs**, and only as a dated entry under a `## Revision History`
  section appended to the bottom of this file (none yet). No value here
  is ever revised after Phase 1 results exist — that is the failure
  mode this artifact exists to prevent.

## 2. Question set

~24 questions (the recall thread's harness-size precedent), drawn from
the shapes the 214-row `answer_runs` history shows genuinely benefit
from composed retrieval: the May paper-corpus questions and RDR
research questions (RDR-200 §Implementation Plan, Phase 1 ship gate).

The set does not exist yet. This document defines the procedure it
must be assembled under; assembly is a follow-up step, gated on this
file existing, that must complete **before any arm runs**:

- **Location:** `docs/rdr/rdr-200-phase1-questions.md` (or
  `docs/rdr/rdr-200-phase1-questions.jsonl` if a structured form proves
  more workable for the harness — whichever form is chosen, it is
  committed to the repo, not left in T2 or a scratch file).
- **Crowding procedure** (verbatim from RDR-200, §Implementation Plan,
  Phase 1 gate mechanics): for each candidate question, run the plain
  flat `search` tool and take its top-10. The judge model (§4 below)
  labels each of the 10 results relevant or irrelevant to the question.
  The question's **crowding score** is the irrelevant fraction of that
  top-10. **Crowded** means score ≥ 0.5 (OQ-7 pin). Every question not
  crowded is **clean**.
- **Stratum minimum:** each of the crowded and clean strata must hold
  at least 8 questions (OQ-7 pin). If either stratum falls short after
  drawing from the benefiting shapes above, the set is **extended**
  (more questions added, drawn from the same shapes) until both strata
  clear the minimum — the set is never shrunk or rebalanced by
  discarding questions to hit the minimum artificially.
- **Freeze point:** stratum assignment is computed once, by the
  set-assembly step, and is **frozen with the question set** before any
  arm runs. Nobody reassigns a question's stratum after seeing arm
  results — assignment belongs to set assembly, not to whoever later
  interprets the gate.

## 3. Arms

Three arms, run against the same frozen question set:

1. **Continuation** — `nx_answer(continuation=True)`; the calling
   session receives `{plan, hydrated_bundles, reduction_spec}` and
   performs the final reduction in-context.
2. **Headless** — `nx_answer(continuation=False)` (today's default):
   plan-match/inline-plan, execute, reduce server-side inside the
   `claude -p` operator dispatch, return a finished answer.
3. **Caller-only** — the calling session does flat `search`/`query`
   itself and reasons over the results directly, with no `nx_answer`
   plan machinery at all.

### Arm identity

Per RDR-200 §Implementation Plan ("Arm identity"): the **same model**
plays the continuation-arm reducer (the session doing the in-context
reduction after receiving the envelope) and the caller-only-arm
reasoner (the session doing flat search-and-reason). This is what keeps
the comparison isolating *retrieval strategy* rather than confounding
it with raw model capability. That model is **the session model the
default flip would serve** — described in the RDR as "Fable-class at
time of writing" — and must be **named explicitly** (not just
"Fable-class") in this document once resolved. **See §7 — this value
is not yet resolved; flagged as a review item, not filled in here.**

The same-model constraint binds the continuation and caller-only arms
**only**. It does not bind the headless arm's internal operator
dispatch, which is governed separately by the tiering pin below, nor
does it bind the judge (§4), which is required to be a *different*
model from the caller-arm model by construction.

### Tiering pin (Sam, 2026-09-01, T2 [23922] addendum)

The headless arm runs the **production default** tiering —
`NX_OPERATOR_MODEL_TIERING` unset — at **both** dispatch sites that
consult it, because the two sites can select differently under the
same nominal (unset) setting:

- **Isolated-operator branch** (`src/nexus/plans/runner.py`,
  `_default_dispatcher`, the `!= "0"` branch around line 1326):
  per-operator resolution via
  `nexus.operators.model_tiers.resolve_model_for_default_path` — an
  operator in `FLIPPED_OPERATORS` resolves to the cheap alias,
  everything else resolves to `STRONG_DEFAULT_ALIAS`. Nothing on this
  path inherits the box CLI default.
- **Bundle path pin** (`runner.py`, around line 2018, the
  `dispatch_bundle` kwargs construction): every bundle is pinned
  unconditionally to `STRONG_DEFAULT_ALIAS` on any setting except the
  `"0"` kill switch — bundles are "strong by construction" (they fuse
  synthesis operators).

Resolved model identities under this pin, as of `src/nexus/operators/model_tiers.py`
at commit `3c3c10321` (the tip this gate protocol is written against —
**re-verify against the code at set-freeze time**, per §7):

| Operator | `OPERATOR_MODEL_TIER` | In `FLIPPED_OPERATORS`? | Resolved model (isolated path, default tiering) |
|---|---|---|---|
| `operator_extract` | cheap | yes | `haiku` |
| `operator_filter` | cheap | yes | `haiku` |
| `operator_groupby` | cheap | yes | `haiku` |
| `operator_rank` | cheap | yes | `haiku` |
| `operator_check` | cheap | yes | `haiku` |
| `operator_verify` | cheap | yes | `haiku` |
| `operator_aggregate` | strong | no | `opus` (`STRONG_DEFAULT_ALIAS`) |
| `operator_summarize` | strong | no | `opus` (`STRONG_DEFAULT_ALIAS`) |
| `operator_generate` | strong | no | `opus` (`STRONG_DEFAULT_ALIAS`) |
| `operator_compare` | strong | no | `opus` (`STRONG_DEFAULT_ALIAS`) |

Any operator dispatched inside a bundle (≥2 contiguous operators,
`OperatorBundleSlice`) resolves to `opus` (`STRONG_DEFAULT_ALIAS`)
regardless of the table above, per the bundle-path pin.

**Rationale** (verbatim intent, RDR-200): headless-as-it-actually-runs
is the incumbent the design must beat; a laboratory-matched headless
arm that ran every operator at "strong" would measure a configuration
nobody actually runs in production.

**Frozen question set constraint:** the set's plans (wherever a plan is
pre-authored rather than inline-planned per question) must carry **no
step-author `model` override** — an explicit override always wins over
the tiering branch and would silently unpin the arm from the production
default this pin is meant to hold constant.

## 4. Judging

- **Judge model:** an Opus-tier model, **distinct from** the
  continuation/caller-only arms' Fable-class model (OQ-6 pin). This
  also satisfies the crowding-procedure requirement in §2, which
  reuses the same judge model for relevance labeling.
- **Blinding:** arm labels are hidden from the judge — each answer is
  presented without indicating which of the three arms produced it.
- **Rubric** (written before any arm runs, covering):
  - **Answer quality** — does the answer address the question,
    correctly and completely, given what was retrievable?
  - **Citation correctness** — for the continuation and headless arms
    (both of which carry a citation/evidence schema), do cited sources
    actually support the claims attributed to them? The **caller-only**
    arm has no schema to conform to and is judged on **content only**
    (RDR-200 §Implementation Plan, gate mechanics) — it is not
    penalized for lacking a citation structure it was never asked to
    produce.
- **Verdict rule:** simple majority across judged pairs. **Ties are
  awarded against continuation** (OQ-6 pin) — i.e. the null hypothesis
  the gate must overturn is Alternative 4 ("continuation does not
  clear the bar"), not the reverse. No margin beyond simple majority is
  pinned; OQ-6 names this explicitly as decided at "simple majority",
  not a larger-margin threshold.

## 5. Pass / fail

**Pass condition:** continuation is **at least as good as** headless
**and strictly better than** caller-only, both measured as win-rates
per §4's majority rule with ties against continuation.

**Stratified prediction** (RDR-200 §Implementation Plan,
"Stratification"): the entropy argument in the RDR's Problem Statement
predicts continuation's margin over caller-only is **largest in the
crowded stratum**. The gate must not pass on an average dominated by
the clean stratum while the claimed value lives in the crowded one — a
pass requires the margin to actually appear in the crowded stratum,
not just in the pooled average.

**Refutation:** if the margin over caller-only does not appear even in
the crowded stratum, the entropy rationale is refuted on today's
corpus. Per OQ-1 (decided: build), Alternative 4 is the gate's
fallback in exactly this case — Phases 2-3 do not proceed, and the
build/park decision returns to Sam with the evidence.

**No-signal protocol** (pre-registered, per RDR-200 and the
`nexus-rv9xp` precedent): a stratum below its 8-question minimum (§2),
or a win-rate that is statistically indistinguishable from a tie,
yields **INCONCLUSIVE — which is not a pass**. INCONCLUSIVE never
silently defaults to "proceed"; it returns to Sam with the data, exactly
as a stratum-below-minimum result would.

## 6. Telemetry side

Reported alongside the blind-judging result, not traded off against it:

- **Zero telemetry-dark runs.** Every continuation dispatch writes its
  handoff row before the envelope returns (RDR-200 §Telemetry) — this
  is asserted, not merely hoped for, over the gate's own run population.
- **Envelope-size distribution and fallback rate.** The distribution of
  envelope sizes across the gate's continuation-arm runs, and the rate
  at which the size cap forced a headless fallback instead of a
  continuation handoff.
- **Unreported-rate metric and its decomposition** (OQ-2, pinned at
  25%): the published metric is the **unreported rate** — the fraction
  of handoff rows (`final_text` marker `handed-off`, in the four-way
  `_split_three_way` split) with no paired `nx_answer_report` completion
  row. The metric alone conflates two different failures:
  - **non-reduction** — the caller received the envelope and never
    reduced it at all;
  - **non-reporting** — the caller reduced it and simply never called
    `nx_answer_report`.

  The pre-registered decomposition audit samples unreported handoffs
  (session transcripts where available) and classifies each into one
  of these two buckets **before** the OQ-2 threshold is treated as
  final for the Phase 2 flip decision. The audit is written against
  `src/nexus/commands/answer_runs.py`'s existing row classes —
  `_split_three_way`'s four-way split (`executed_ok`, `executed_failed`,
  `handed-off`, `degenerate`) and `_classify_degenerate_row`'s
  sub-classes (`redacted`, `planner_error`, `error`, `other`) — so the
  decomposition is checkable against code the reader can run, not a
  fresh ad hoc taxonomy.

  **Precedent, stated as a caution rather than a prediction:** the
  `nx-answer-degenerate-row-taxonomy` census (T2
  `nexus/nx-answer-degenerate-row-taxonomy-2026-08-20`) found 24 of 39
  "degenerate" rows (62%) were the benign `trace=False` redaction
  class, not errors. "Unreported" carries the identical trap — a large
  unreported count could equally mean the caller reduced fine and
  simply never reported, which is a telemetry-completeness problem, not
  a continuation-mode failure. The decomposition audit exists
  specifically so the 25% threshold is interpreted against the right
  bucket, not against the raw unreported count.

  The audit may **revise the 25% number before Phase 1 runs**; it may
  never revise it after (§1 revision rule).

## 7. What is NOT frozen here

- **Judge rubric wording** may be refined (without changing what it
  measures — answer quality and citation correctness, per §4) until
  the question set is frozen at set-assembly.
- **The arm-identity model name** (§3, "Arm identity") is stated in the
  RDR only as "Fable-class at time of writing" and is **not resolved to
  a concrete model id in this document**. This is flagged as a review
  item: either (a) resolve it now, in a follow-up edit to this file
  before set-assembly, or (b) resolve it at set-assembly time and
  record it there — RDR-200 requires it be "named in the
  pre-registration," which this document is, but does not say which of
  (a)/(b) discharges that requirement. Whoever assembles the question
  set must record the concrete session-model id used for the
  continuation-reducer/caller-only-reasoner arm before that arm runs,
  as a dated addition under this file's (currently empty) Revision
  History.
- **The tiering table in §3** is transcribed from
  `src/nexus/operators/model_tiers.py` at commit `3c3c10321`. If the
  table or `STRONG_DEFAULT_ALIAS` changes before the gate runs (e.g. a
  future re-tiering study), the resolved-model column must be
  re-verified against the code at set-freeze time and corrected here
  via a dated Revision History entry — the *tiering pin itself*
  (production default, unset `NX_OPERATOR_MODEL_TIERING`, both dispatch
  sites) is frozen; the concrete model names it currently resolves to
  are not independently pinned and will drift with the table.
- Everything else in this document (question-set procedure, crowding
  procedure, stratum minimums, pass/fail rule, no-signal protocol,
  unreported-rate decomposition target) is frozen at this file's
  initial commit.

## Revision History

- 2026-09-01 (before any arm ran) — **Arm session-model id recorded**:
  the continuation-arm reducer and the caller-only-arm reasoner both
  run on **`claude-fable-5`** (the orchestrating session's model — the
  session model the default flip would serve, satisfying §3's
  same-model constraint). Judge confirmed at set-freeze as
  **`claude-opus-5`** (measured from every labeling envelope, distinct
  from the caller arm per OQ-6). Question set frozen at
  `docs/rdr/rdr-200-phase1-questions.md` (24 questions, 12 crowded /
  12 clean); model_tiers.py verified unchanged at assembly. No other
  value changed.
