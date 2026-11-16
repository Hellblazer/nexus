---
title: "Multi-hop answers read past the six-chunk generator budget"
id: RDR-250
type: Feature
status: draft
priority: low
author: Hal Hildebrand
reviewed-by: pending
created: 2026-11-15
accepted_date:
related_issues: ["nexus-io7tw", "nexus-bwct4"]
---

# RDR-250: Multi-hop answers read past the six-chunk generator budget

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

HERB is the benchmark corpus this RDR measures against. It is 1,559 Slack-style
artifacts about customer issues. Most of them are triage messages that name one
customer id each, in the form CUST-0115. A question asks which companies are
affected by an issue. A correct answer names the companies, and each company has
one or more customer ids. "issue5" means the five issue questions of the HERB
set. Recall is the share of the gold companies an answer finds. Precision is the
share of named companies that are gold. F1 is the harmonic mean of the two.

Two retrieval paths answer these questions. The first is a hand-written script
that runs one vector search (hop 1) and scores the customers it finds. The
second is `nx_answer`, the nexus command that plans retrieval, checks the
answer against its evidence (the coverage boundary, owned by RDR-232), and
generates a cited answer. RDR-147 built the second path. Its final measured
result is well below the first, and the two gaps below are different limits.

The bead description cites F1 0.7167 for the benchmark. That is the 2026-11-03
spike (T2 `nexus_rdr/147-research-9`). The final RDR-147 run scored F1 0.500
(T2 `nexus_rdr/147-research-17`). This RDR uses 0.500 as the `nx_answer`
baseline.

#### Gap 1: `nx_answer` checks six anchors, not the evidence it retrieved

Plan 10, `hybrid-factual-lookup` (`conexus/plans/builtin/hybrid-factual-lookup.yml`),
sets `vector_budget_chunks` to 6 by default. Its third step hydrates six vector
chunks, and the coverage boundary reads those six as its anchors. Only the
customer ids in those anchors are resolved to companies, so the answer can name
only the companies of the top six vector chunks. Code read at sha 1ecf3ca6d
confirms this: the boundary's input is the trailing operator run's hydrated
inputs, and the final run's anchor scan reports `anchors_scanned=6`.

The number 6 has an older origin. RDR-097 RF-6 split a 10-chunk generator
context 60/40 between a vector stream and a graph stream, to keep the generator
precise. RDR-147's resolution branch reused the 6 without sizing it for its own
purpose. No record sizes the anchor set.

Measured on the final RDR-147 run (both runs, issue5): recall 0.407, precision
0.800, F1 0.500. The harness also held 80 evidence chunks per question, with
7 to 10 customer ids among them. The boundary does not read those 80. They sit
in the harness envelope, not in the boundary's input.

Projected, not run. If the answer named the companies of the top N vector
anchors, issue5 would score as follows. The projection reproduces the measured
N=6 point exactly.

| N (`vector_budget_chunks`) | Recall | Precision | F1 | Basis |
| --- | --- | --- | --- | --- |
| 6 | 0.407 | 0.800 | 0.500 | Measured, RDR-147 final run |
| 10 | 0.507 | 0.800 | 0.582 | Projected |
| 20 | 0.667 | 0.967 | 0.764 | Projected |
| 40 | 0.967 | 0.715 | 0.817 | Projected |
| 80 | 1.000 | 0.657 | 0.791 | Projected; needs a search limit of at least 80 |

The projection holds the 0.95 precision gate only up to N=20. Past that, the
gate needs a precision filter. Sources: T2 `nexus_rdr/io7tw-hop1-recall-research-2026-11-15`
(follow-up section), which calculates these values from the recorded rows.

#### Gap 2: hop-1 vector search fills its top 20 with messages that name no id

Hop 1 is the first search, before any graph traversal. For the hand-written
script, measured on issue5: at k=20 recall is 0.667, precision 0.967 and F1
0.764 (T2 `nexus_rdr/io7tw-hop1-recall-research-2026-11-15`, Q3). Of the 100
hits across the five questions' top 20, 76 name no customer id: 59 are
uncited and 17 are cited discussion. The eight gold companies missing from the
top 20 first appear at ranks 22 to 57. Recall reaches 1.0 at k=60, where
precision is 0.698 and F1 0.820.

The bead's "4 to 6 of the 10 to 12 cited ids" overstates the target. The cited
artifacts also name non-gold companies, so an oracle that keeps only cited hits
caps precision near 0.83. This RDR measures company recall, not id recall.

Gap 2 limits the script and any design that reads the hop-1 pool directly. It
does not limit `nx_answer` in the way Gap 1 does. Fixing Gap 1 alone does not
close Gap 2, and the reverse also holds.

## Relationship to Prior RDRs

The scan covered the RDRs that RDR-147 cites and the research record. The full
RDR index was not scanned for other overlapping budget or recall work.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-097 | Origin of the 6/4 budget | RF-6 sized 6 and 4 as a 60/40 split of a 10-chunk generator context, for generator precision. It did not size an anchor set for the coverage boundary. |
| RDR-147 (closed 2026-11-06) | Origin of the resolution branch | Its branch reused the 6 without sizing. Its R6 left hop-1 recall as separate later work, which this RDR takes up. Its R5 names the precision risk of answering from every held id. |
| RDR-232 | Adjacent | Owns the coverage boundary that reads the anchors. Option E below changes that boundary's input. |

## Context

### Background

RDR-147 replaced an earlier design in November 2026. Its final run answered
issue5 at F1 0.500 and was judged on join completeness and precision, with
recall and F1 reported but not gated. Its R6 recorded hop-1 recall as a
separate problem and said it needed its own design before code. Bead
nexus-io7tw was revived from deferred on 2026-11-14 in a board re-groom, and
this draft is the design it asked for.

### Technical Environment

- nexus `develop`, with conexus 7.96.0 at sha 1ecf3ca6d (the RDR-147 MVV build).
- Embeddings: bge-768 (`bge-base-en-v15-768`). Collection
  `knowledge__herb-searchflow__bge-base-en-v15-768__v1`, 1,565 chunks.
- Entity keys: `entity.collections` maps `knowledge__herb-*` to `CUST-\d{4}`.
- The engine caps a query at 300 rows (`MAX_QUERY_RESULTS`).

## Research Findings

### Investigation

On 2026-11-15, read-only queries ran against the local RDR-147 MVV sandbox
(T2 `nexus_rdr/io7tw-hop1-recall-research-2026-11-15`). There were no cloud
calls and no code changes. The follow-up read plan 10 with `nx plan show 10`
and traced the anchor cut in code at sha 1ecf3ca6d. No live `nx_answer` run was
made. Every `nx_answer` figure in this RDR is either the RDR-147 measurement or
a projection from recorded rows.

### Key Discoveries

Each finding carries one label. **Measured** means a run in the record.
**Simulated** means computed from search distances, with no engine filter
built. **Projected** means computed from recorded rows under a stated
assumption, with no run. **Code-read** means read from source at the named sha.

- Plan 10 hydrates 6 vector chunks and 4 graph chunks by default. HERB has no
  catalog links, so the graph step contributes nothing. (Code-read, plan 10
  at sha 1ecf3ca6d.)
- The coverage boundary reads only the 6 hydrated chunks, so its anchors are
  the vector top six. (Code-read, `runner.py` near 4827–4855 and
  `coverage_ledger.py` `build_anchors` near 743.)
- The RDR-147 final run scored issue5 F1 0.500, recall 0.407, precision 0.800.
  (Measured, T2 `nexus_rdr/147-research-17`.)
- The RDR-147 final run's 8 in-domain answers had join completeness 1.000:
  every answer named every company its anchor keys resolved to. (Measured,
  147-research-17.) That holds at N=6 only.
- At k=20 the script's hop-1 recall is 0.667 and precision 0.967. Recall is
  1.0 at k=60. (Measured, script, io7tw research Q3.)
- 76 of 100 top-20 hits across the five questions carry no id. (Measured, io7tw research Q2.)
- Option A (`--lexical`) at k=20: recall 0.960, precision 0.813, F1 0.876.
  The k=10 value of F1 0.892 came from a run with `-m 100`, so the prefix
  depends on that setting. (Measured, io7tw research Q4-A.)
- Option B (key-bearing leg), top 8: F1 0.857. (Simulated, io7tw research Q4-B.)
- Option E (anchor scan over all evidence): F1 0.817, precision 0.715.
  (Projected, from the MVV rows.)
- `MAX_REPAIR_ITEMS` is 12 (`coverage_repair.py:89`). At most 11 keys occur
  at N≤80, so it does not bind here. (Code-read, io7tw research follow-up.)
- Paid `nx_answer` runs, 2026-11-15, on the RDR-147 sandbox. Each config ran
  once, for 3.37 USD in all, and the baseline call reproduced the RDR-147 row
  exactly. (Measured, T2 `nexus_rdr/io7tw-paid-mvv-2026-11-15`.) Scores below
  are recall, precision and F1.

  | Config | issue5 | key8 held-out F1 | agg2 |
  |---|---|---|---|
  | `vector_budget_chunks` 6 (RDR-147 run) | .407 / .800 / .500 | .135 | 0 |
  | `vector_budget_chunks` 20 | .667 / .967 / .764 | .145 | 0 |
  | 20 plus the lexical leg on step 1 | .920 / .853 / .866 | .358 | 0 |

  - At 20, every answer named exactly its anchor-key companies, and
    `anchors_scanned` was 20, so the projection held to three decimals.
  - The lexical leg brings other companies' keys into the anchors, and the
    answer names them. That drops precision below the 0.95 gate.
  - Two of the lexical key8 rows ran on grown plans, not plan 10.
  - Passing `vector_budget_chunks` as a binding changes which plan matches
    unless the strategy is pinned: two questions went to plan 8 (same scores).
- The budget-20 run repeated three times, 2026-11-15, same conditions, for
  1.53 USD (Sam: "yes, run the repeat"; T2
  `nexus_rdr/io7tw-paid-mvv-repeat-2026-11-15`). issue5 F1 .764, .764, .764
  and .614; mean over the four runs .726. Precision .967 in three, .767 in one.
  - Retrieval did not vary: every plan-10 row returned the same anchor keys as
    the first run.
  - The low run lost one question. Its evidence held all five gold keys, but
    the resolution branch was not entered, so the answer named customer ids
    instead of companies and scored 0. That variance belongs to the branch's
    entry, not to the budget.
  - The strategy pin still missed on one question in two of the runs (the
    inline planner answered it, with the same score). No plan was saved.

### Critical Assumptions

- [x] The companies of the top N anchors are the companies the answer names, at N above 6. Status: Verified at N=20 on issue5 (paid run, 2026-11-15); N=40 and above unmeasured. Method: Spike.
- [x] Generation names exactly the anchor-key companies when N is above 6. Status: Verified at N=20 in 19 of 20 answers over four runs; the one miss did not enter the resolution branch and named ids. Method: Spike.
- [ ] A precision filter keeps precision at or above 0.95 on the widened pool. Status: Unverified (250-research-5). No model filter has been measured; the MVV measures the support-verdict filter Technical Design names. Method: Spike.
- [ ] Option A's gain is not specific to HERB's template overlap. Status: Unverified. This RDR does not claim it generalises. Method: Spike on a second corpus.
- [ ] Option B assumes entity keys sit in retrievable announcement chunks. Status: Unverified in general; true on HERB. Method: Spike.

## Proposed Solution

### Approach

Sam chose 2026-11-15 (ask `rdr250-lever`, option 2): a budget of 20 as plan 10's
default now, then Option E (anchor scan over all evidence) with a precision filter.
The record supports two separate levers, one per gap; this choice is Gap 1's.

The budget of 20 shipped as plan 10's default in 4a25a7c5a (Phase 1 Step 1). It
needed no code: measured at F1 0.764 with precision 0.967, inside the gate
(250-research-2, 250-research-3).

This RDR's design is Option E with a precision filter (Phase 1 Step 2). For
Gap 1, Option E is the largest lever in the record: it widens the key scan from
the generator's 20 inputs to the search step's 40 hits without growing the
generator's context. Unfiltered it is projected at precision 0.715
(250-research-4), below the 0.95 gate, so the filter is part of the design, not
an option, and the switch stays off until the MVV passes.

Gap 2 is not addressed by this RDR. For it, Option A has the largest measured F1
inside `nx_answer` (0.866), but its precision (0.853) fails the gate without a
filter, and its size depends on HERB's template. Option B is the cleanest
key-based lever, and it needs a new engine filter.

### Technical Design

Sites are on develop at 01a80c2d1; line numbers are as read on 2026-11-16.

**Today.** The coverage boundary's resolution branch, `_resolve_anchors`
(`coverage_ledger.py:1257`), scans only `first.ledger.anchors`. `build_anchors`
(`coverage_ledger.py:743`) makes those anchors from `BoundaryView.evidence_texts`,
the hydrated inputs of the answer step's closure (`runner.py` `_boundary_evidence`
at 2280, read through `_read_rest_of_run` at 4341). In plan 10 those inputs are
step 3's `store_get_many(ids=$step1.ids, limit_per_source=$vector_budget_chunks)`,
the first 20 of the search step's hits (an integer `limit_per_source` "truncates
every stream to its first N entries", `mcp/core.py:7277`). The search step returns up to 40 ids
(`limit: 40`); ids 21 to 40 are never scanned (250-research-1).

**The pool.** The key-scan pool is the search step's ids that are not already
an anchor. The boundary reads them from `BoundaryView.step_outputs` (the search
step's `ids`, the same list `$step1.ids` names in plan 10) and fetches their
texts in one call through `BoundaryView.fetch`, an accounted `sql` step with no
model call. A pool chunk is scanned with the same `find_key_tokens` call and
`strip_source_note` rule as an anchor. Pool chunks with no key are dropped at
once: they are not anchors and never reach the generator. Which step is the
search step: the first step of the run whose tool is `search` and whose output
carries `ids`. A plan with no such step has no pool, and the branch behaves
exactly as today.

**The precision filter.** A key found only in the pool is a candidate. It is
kept only when the coverage assessment cites its chunk as support. The pool
chunks that carry a candidate key are packed with the existing `_pack` and sent
through `BoundaryView.check`, the same assessment call the boundary already
makes, against the question's facets. A candidate key is kept when its chunk's
`item_id` appears in the `evidence_ids` of a facet the reply marks `covered`.
Keys found in the anchors are not filtered, so the measured budget-20 behaviour
is the floor. The filter adds one assessment call per pack. A distance cutoff
was rejected: it scored F1 at most 0.825 in simulation, and citation alone caps
precision near 0.83 (250-research-5). Whether support-citation reaches 0.95 is
unmeasured, so the MVV measures it.

**Resolution.** Kept candidate keys join the anchors' keys, in anchor-first
order, before the `max_repair_items` cap (12, `coverage_repair.py:110`). The cap
does not bind on HERB at 40 (at most 11 keys, 250-research-6). A joined item for
a pool key uses the pool chunk's text as its anchor text, so it enters the
generator the way an anchor key's joined item does today. The generator's own
input of 20 chunks does not change.

**Failure is closed.** If the fetch fails, the filter call fails or returns
malformed records, or the remaining deadline or USD allowance is below one
assessment call's mean cost (`assessment_cost_estimate`), no candidate key is
kept and the branch runs on the anchors alone, as today. The reason is named in
the report.

**Switch.** `NX_ANSWER_POOL_KEY_SCAN`, default off. Off, the code path is not
entered and the report carries no pool fields. Turning it on by default is a
later decision, after the MVV.

**Report.** The resolution fields gain `pool_scanned` (pool chunks fetched),
`pool_candidate_keys`, `pool_keys_kept` and `pool_keys_rejected` (both by name),
`pool_filter_calls`, and `pool_reason` when the pool was skipped or closed.
`anchors_scanned` keeps its meaning (the anchors only), so existing readers of it
are unchanged.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Lexical leg for hop 1 (Option A) | `search --lexical`, the engine's FTS and trigram union (shared probe from nexus-bbtc2) | Reuse: a flag exists; inside plan 10 at budget 20 it measured precision 0.853 (250-research-2) |
| Anchor budget of 20 (Step 1) | Plan 10 binding `vector_budget_chunks` | Reuse: default changed to 20 in 4a25a7c5a |
| Key scan over the search pool (Option E) | `coverage_ledger.py` `_resolve_anchors`, `find_key_tokens` | Extend: scan the pool's chunks as well as the anchors |
| Pool chunk texts (Option E) | `BoundaryView.fetch`, the repair's accounted chunk fetch | Reuse |
| Precision filter (Option E) | `BoundaryView.check` and `_pack`, the coverage assessment call; `FacetFinding.evidence_ids` | Reuse: one more assessment call per pack, over candidate-key chunks only |
| Key-bearing filter (Option B) | None | New: an engine content-pattern filter |
| Entity-key pattern | `entity.collections` in config | Reuse for B and E |

### Decision Rationale

Gap 1 and Gap 2 have separate binding limits, so one lever does not fix both.
Gap 1 caps `nx_answer` at the anchor cut (6 before 4a25a7c5a, 20 since). Gap 2
caps the script, and any design that reads the hop-1 pool directly. A lever for
Gap 1 therefore does not need to improve hop-1 recall, and the reverse also
holds. Option E is chosen because it is the largest Gap 1 lever and it reuses
the boundary's fetch and assessment calls. Its precision depends on a filter the
record has not measured, so the switch stays off until the MVV measures it.

## Alternatives Considered

### Alternative 1: Option A, the existing lexical leg

**Description**: Run hop 1 with `--lexical`, which unions full-text and trigram
hits with the vector hits.

**Pros**:

- Measured on the script at k=20: F1 0.876, recall 0.960, precision 0.813.
- Uses an existing flag, with no new surface.

**Cons**:

- The gain is partly a product of HERB's template. The triage message and the
  question share phrases such as "affects the <component>", so lexical matching
  favours id-bearing messages (id-bearing slots in the top 20 rise from 24 to 42 of 100).
- Inside `nx_answer` at budget 20 it measured precision 0.853, below the 0.95
  gate: the lexical leg brings other companies' keys into the anchors
  (250-research-2).

**Why not chosen**: It addresses Gap 2, not Gap 1, its precision fails the gate
without a filter, and its generality is unproven.

### Alternative 2: Option B, a key-bearing leg

**Description**: Rank only chunks that match the collection's entity-key
pattern (`CUST-\d{4}`), then take the top few.

**Pros**:

- Simulated F1 0.857 at top 8 (recall 0.877, precision 0.853). Top 5 gives F1 0.801.
- Distance-gap and absolute-threshold cutoffs did no better (F1 at most 0.825 in the simulation).

**Cons**:

- Needs a new engine content-pattern filter. Nothing like it exists.
- Assumes keys sit in retrievable chunks, which holds on HERB and is not shown elsewhere.
- Simulated from distances, not run.

**Why not chosen yet**: It needs a new engine surface before it can be measured for real.

### Alternative 3: Option C, deeper k with an LLM precision filter

**Description**: Retrieve k=60, then have an LLM judge whether each id-bearing chunk is relevant before it is resolved.

**Pros**:

- Unfiltered k=60 is measured at recall 1.0, so the ceiling depends only on the filter.
- Unfiltered k=30 is already F1 0.885 on the script (measured).

**Cons**:

- Distance filters fail. Even the oracle that keeps only cited hits reaches precision only 0.834.
- The filter's precision effect is unmeasured. One assessment call per pack adds cost.
- The RDR-147 coverage re-check cited 35 of 45 joined items as supporting (147-research-10). That measures support, not precision.

**Why not chosen**: Its filter is taken into Option E (a support verdict from the
existing assessment call), but its deeper k is not: Option E's pool is the
search step's 40 hits, so no retrieval setting changes.

### Alternative 4: Option E, anchor scan over all evidence, decoupled from the generator budget

**Description**: The coverage boundary scans the entity-key pattern across all
search hits (the pool), not only the generator's inputs (6 then, 20 now). It
resolves the keys it finds. The generator's own input does not grow.

**Pros**:

- Projected issue5 F1 0.817 (recall 0.967, precision 0.715), against 0.500 measured.
- The largest projected lever for `nx_answer` in the record.
- The generator's context does not grow.

**Cons**:

- Without a filter, precision falls to 0.715, below the 0.95 gate. A precision filter (Option C) is needed to hold the gate.
- Changes coverage boundary code, which RDR-232 owns.
- Assumes the generator names the companies the scan finds.

**Chosen**: Sam, 2026-11-15 (ask `rdr250-lever`, option 2), with the precision
filter in Technical Design. It has no measured run; the MVV is its first.

### Briefly Rejected

- **Option D, per-artifact chunk expansion**: Its effect on HERB is expected to be about zero. Each id-bearing artifact is one 290 to 410 character chunk, and 37 such artifacts each name one id (measured). Thread or same-day expansion adds 13 to 16 ids per question, close to all 16 ids in the corpus, so precision falls to about 0.46 (estimated from the expansion counts, not run).

## Trade-offs

### Consequences

- Option E changes a coverage boundary RDR-232 owns, and it needs a precision filter to meet the gate.
- The budget of 20, now plan 10's default (4a25a7c5a), widens the rank and generate context and the assessor's packs for every `hybrid-factual-lookup` call. Cost per call measured 0.03 to 0.17 USD, not materially above budget 6 (T2 `nexus_rdr/io7tw-paid-mvv-2026-11-15`, Findings).
- The filter adds one assessment call per pack of candidate-key chunks, only when the switch is on and the pool has candidate keys.

### Risks and Mitigations

- **Risk**: The five issue questions overfit. The k=10 and k=30 peaks are fitted to five questions.
  **Mitigation**: A held-out check beyond the five (Minimum Viable Validation).
- **Risk**: HERB's template overlap inflates the lexical gain.
  **Mitigation**: Report the lexical result on the held-out set and on a second corpus before relying on it.
- **Risk**: Precision falls below 0.95 past N=20.
  **Mitigation**: The precision filter, and the switch stays off until the MVV measures it.
- **Risk**: The support verdict keeps a non-gold company's key, because cited artifacts also name non-gold companies (citation alone caps precision near 0.83, 250-research-5).
  **Mitigation**: The MVV reports kept and rejected pool keys by name, so a filter that keeps them shows in the record, not only in the score.
- **Risk**: The resolution branch is sometimes not entered at all (budget-20 repeat, run d: one question scored 0, 250-research-3). That variance is outside this design, and it would show in the MVV as a lost question.
  **Mitigation**: The MVV reports `resolution_entered` per row, so a lost question is told apart from a precision loss.

### Failure Modes

A widened pool with no filter answers more companies and loses precision. The
MVV's precision gate is the visible check; the MVV reports precision split by
`generated_on_join`, so a loss shows as a gate failure, not a silent change.

A failed pool fetch or filter call keeps no candidate key, so the answer falls
back to today's anchors-only behaviour and the report names `pool_reason`.

## Implementation Plan

### Prerequisites

- [x] Sam picks the lever or levers: option 2, 2026-11-15
- [ ] Sam gives the go for the paid run
- [ ] Critical Assumptions verified, or a held-out set chosen

### Minimum Viable Validation

One paid `nx_answer` run on HERB with `NX_ANSWER_POOL_KEY_SCAN` on, on the
develop code. It uses the RDR-147 MVV harness: a fresh sandbox, bge-768, plan
10 at its default budget of 20 with search limit 40, and `entity.collections`
set to `CUST-\d{4}`. It reports join completeness, per-run precision over the
answers that name a company, recall and F1 on issue5, `resolution_entered` per
row, the pool fields (keys kept and rejected by name), and the filter's added
cost per question (`pool_filter_calls` times the mean assessment-call cost). Its baselines are
the budget-20 measurement, F1 0.764 (250-research-2), and the RDR-147 F1 0.500
(147-research-17).

The same run must include a held-out check beyond the five issue questions.
The questions are chosen and frozen before the run. They must not be used to
pick N or the filter threshold. The 20 valid held-out questions of the RDR-147
MVV (branch entered 0) are one candidate. The record does not say whether they
were used for tuning, so that needs checking before use.

The paid run is spend and it calls the cloud, so it needs Sam's explicit go and
an announced window. For scale, the two-run RDR-147 MVV cost 1.20 USD
(147-research-17). Pass criteria are Sam's to set, before the run (Open
Question 5 carries a proposal). Changing them after the run counts as changing
promotion criteria.

### Phase 1: Code Implementation

#### Step 1: Plan 10's `vector_budget_chunks` default from 6 to 20

Shipped as 4a25a7c5a. `conexus/plans/builtin/hybrid-factual-lookup.yml`, no code. Installs get it at their next `nx plan reseed`.

#### Step 2: The pool key scan, behind the switch

Waits for this RDR's acceptance. In `_resolve_anchors`: read the search step's
ids from `step_outputs`, fetch the non-anchor ones through `view.fetch`, scan
them with `find_key_tokens`, and add the report fields. Behind
`NX_ANSWER_POOL_KEY_SCAN`, default off. With no filter yet, every candidate key
is dropped and named in `pool_keys_rejected`, so this step changes no answer.

#### Step 3: The precision filter

Pack the candidate-key chunks with `_pack`, send them through `view.check`
against the question's facets, keep a key whose chunk is cited in a `covered`
facet's `evidence_ids`, and join kept keys before the `max_repair_items` cap.
Fail closed as Technical Design states.

#### Step 4: The MVV

The paid run above, after Sam's go, his sign-off on the pass criteria, and an
announced window. Turning the switch on by default is a separate decision after it.

### Phase 2: Operational Activation

Omitted. Nothing is deployed by this RDR.

### Day 2 Operations

No persistent resource is created. The one operator control is the
`NX_ANSWER_POOL_KEY_SCAN` switch; unset, behaviour is today's. An Option B
filter would be a new engine surface, and its Day 2 operations would be written
then.

### New Dependencies

None.

## Test Plan

- **Scenario**: Switch off. **Verify**: On a fixed fixture whose search step returns 40 ids, the resolution fields equal today's and carry no pool field.
- **Scenario**: Switch on, a pool key the assessment cites. **Verify**: The key is resolved and joined; `pool_scanned` is the non-anchor id count, `pool_keys_kept` names it, and `anchors_scanned` still counts the anchors only.
- **Scenario**: Switch on, a pool key the assessment does not cite. **Verify**: The key is not resolved and is named in `pool_keys_rejected`. This is the non-vacuity check: the filter must be able to reject.
- **Scenario**: The filter call fails, returns malformed records, or the allowance is short. **Verify**: No candidate key is kept, the anchors' keys still resolve, and `pool_reason` names the cause.
- **Scenario**: A plan with no `search` step. **Verify**: No pool, and `pool_reason` says so.
- **Scenario**: More keys than `max_repair_items`. **Verify**: Anchor keys fill the cap first, and dropped keys are named in `tokens_dropped`.

## Validation

### Testing Strategy

1. **Scenario**: The MVV above, on the chosen lever.
   **Expected**: The pass criteria Sam signs off before the run (Open Question 5), reported beside the 0.764 and 0.500 baselines.
2. **Scenario**: The held-out check.
   **Expected**: Results reported on questions chosen before the run, with no tuning on them.

### Performance Expectations

No latency figures are recorded for the lexical leg inside `nx_answer`. None are
estimated here.

## Finalization Gate

- **Contradictions**: The Approach, Technical Design and Implementation Plan name one design (Option E with a support-verdict filter, behind a switch). Option A's earlier "unmeasured" lines were updated to the paid measurement.
- **Assumptions**: The anchor-key assumption is verified at N=20 (250-research-2, 250-research-3). The Option E projection (250-research-4) and the filter's precision (250-research-5) are recorded as assumed; the MVV is their first measurement, and nothing turns on by default before it.
- **Scope**: Gap 1 only. Gap 2 (Options A and B) and the resolution branch's entry variance are named and left out.
- **Cross-cutting**: The change sits in the coverage boundary that RDR-232 owns. It reuses the boundary's fetch and assessment calls, adds no engine surface, and leaves `anchors_scanned` and the switch-off path unchanged.
- **Proportionality**: One switch, one fetch and one assessment call per pack, no new dependency. The paid MVV is Sam's go.

### Open Questions for Sam

1. Answered 2026-11-15 (option 2): budget 20 as the default now, then Option E with a precision filter. Which lever or levers to build. Options E and B need code. Option A is a flag, and it fails the precision gate without a filter. A budget of 20 needs no code and passes the gate on issue5 (measured, one run).
2. Answered 2026-11-15: the paid run was made, for 3.37 USD (Key Discoveries). The budget-20 repeat was run the same day (three runs, Key Discoveries).
3. Whether the 20 held-out questions in the RDR-147 MVV were used for tuning. If they were, a new held-out set is needed.
4. Answered 2026-11-15: the plan 10 default changes (option 2).
5. The MVV's pass criteria, set before the run. Proposed: precision at or above 0.95 over the answered issue5 rows, F1 above the budget-20 measurement of 0.764, join completeness 1.0, and the held-out set reported. F1 rather than recall alone, so a widened pool cannot pass by naming more companies.
6. The resolution branch's entry variance (run d, 250-research-3) has no owner. It is outside this RDR. Whether it gets a bead is Sam's call.

## References

- T2 `nexus_rdr/io7tw-hop1-recall-research-2026-11-15`, with its follow-up section (evidence for this RDR).
- T2 `nexus_rdr/147-research-9` (the 0.7167 spike), `147-research-10` (the coverage re-check), `147-research-17` (the final MVV, F1 0.500).
- `docs/rdr/rdr-147-multihop-entity-resolution-retrieval.md` (R5, R6, the MVV bar).
- `docs/rdr/rdr-097-hybrid-retrieval-plan-template.md` (RF-6, around lines 81 and 107).
- `conexus/plans/builtin/hybrid-factual-lookup.yml` (plan 10 defaults and steps).
- Code at sha 1ecf3ca6d (the measurements): `runner.py` near 1823 and 4827–4855; `coverage_ledger.py` `build_anchors` near 743 and the anchor scan near 1297; `coverage_repair.py:89`.
- Code on develop at 01a80c2d1 (the design): `runner.py` `_boundary_evidence` 2280, `_read_rest_of_run` 4341; `coverage_ledger.py` `BoundaryView` 699, `build_anchors` 743, `_resolve_anchors` 1257, `_pack` 1975; `coverage_repair.py:110`; `mcp/core.py:7277`.
- T2 `nexus_rdr/250-research-1` to `250-research-6`.
- Bead `nexus-io7tw`, and `nexus-bwct4` (RDR-147).

## Revision History

- 2026-11-15: Draft created from nexus-io7tw and its research record. Not gated, not accepted.
- 2026-11-15: Sam chose ask `rdr250-lever` option 2; Approach, Phase 1 and Open Questions 1 and 4 record it, and plan 10's default budget moves to 20. The budget-20 repeat (three runs) is in Key Discoveries.
- 2026-11-16: Gate preparation (elsinore, with linda): Technical Design for Option E with the support-verdict precision filter; Approach records option 2 and Step 1 as shipped (4a25a7c5a); Finalization Gate written; Open Questions 5 (MVV pass criteria) and 6 (branch-entry variance); research records 250-research-1 to -6.
