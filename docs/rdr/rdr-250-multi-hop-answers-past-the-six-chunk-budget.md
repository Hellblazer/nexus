---
title: "Multi-hop answers read past the six-chunk generator budget"
id: RDR-250
type: Feature
status: closed
closed_date: 2026-11-17
priority: low
author: Hal Hildebrand
reviewed-by: self
created: 2026-11-15
accepted_date: 2026-11-17
related_issues: ["nexus-io7tw", "nexus-bwct4"]
---

# RDR-250: Multi-hop answers read past the six-chunk generator budget

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

The title names the problem as found, at the old budget of 6; Step 1 raised the budget to 20, and Option E reads past it to the search step's 40 hits.

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

Since Step 1 (4a25a7c5a) the budget is 20, and the same limit holds one step
out: the boundary scans the search step's first 20 hits of 40, and hits 21 to
40 are never scanned. At 20, issue5 measured recall .667, precision .967, F1
.764 (250-research-2). That is the gap Option E addresses.

The number 6 has an older origin. RDR-097 set the defaults 6 and 4 as "a
60/40 split per RF-6", RF-6 dividing the model's input budget between a vector
stream and a graph stream (rdr-097 lines 81 and 107; 250-research-10). RDR-147's resolution branch reused the 6 without sizing it for its own
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
| 20 | 0.667 | 0.967 | 0.764 | Projected; later measured, the same (250-research-2) |
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

Gap 2 limits the script and any design that reads the hop-1 pool directly. At
budget 6 it did not limit `nx_answer` the way Gap 1 did. At budget 20 the two
meet: `nx_answer`'s anchors are the same hop-1 top 20, 76 of whose 100 issue5
hits name no id, so Gap 2 now limits `nx_answer` too. Option E works around it
by scanning past the top 20, not by improving hop-1 ranking; Gap 2 itself stays
open (bead nexus-io7tw).

## Relationship to Prior RDRs

The scan covered the RDRs that RDR-147 cites and the research record. The full
RDR index was not scanned for other overlapping budget or recall work.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-097 | Origin of the 6/4 budget | RDR-097 set the defaults 6 and 4 as "a 60/40 split per RF-6", RF-6 dividing the model's input budget between the vector and graph streams (rdr-097 lines 81 and 107; 250-research-10). It did not size an anchor set for the coverage boundary. |
| RDR-147 (closed 2026-11-06) | Origin of the resolution branch | Its branch reused the 6 without sizing. Its R6 left hop-1 recall as separate later work; this RDR names it as Gap 2 and leaves it open. Its R5 names the precision risk of answering from every held id. |
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

- Measurements before the build: nexus `develop`, conexus 7.96.0 at sha 1ecf3ca6d (the RDR-147 MVV build).
- The build and its MVV: develop 7c44ceee8 (conexus 8.3.0), engine 0.1.194, on a fresh local sandbox.
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
- The MVV, 2026-11-16, on a fresh local sandbox (engine 0.1.194, develop
  7c44ceee8, plan 10 default budget 20) with `NX_ANSWER_POOL_KEY_SCAN` on, for
  2.65 USD charged (Sam's go; T2 `nexus_rdr/250-mvv-2026-11-16`, 250-research-8),
  of which 1.0058 was booked against a refused first attempt that cost about
  0.006, so real spend was about 1.65.
  issue5 recall .827, precision .967 (macro mean over the five questions; pooled
  over named companies, 19 of 20, .950), F1 .878, join completeness 1.0: all three
  items of the pre-registered bar (250-research-7) pass. A control on the same
  sandbox with the switch off scored .667/.967/.764, the budget-20 figure to
  three decimals. (Measured.)
  - The filter kept 4 pool keys, all gold (Q2 ComputeWorks and InnovateAI, Q3
    AITransformers and TechCorp), and rejected 12, of which 8 are not gold. Of the
    4 gold rejects, Q3 SmartData was already named; the other three cost recall
    (Q2 CloudSync, Q5 DataSolutions and CloudSync). The F1 gain is Q2 and Q3 only;
    Q1, Q4 and Q5 score as the control (250-research-9).
  - The filter made one assessment call per question, 0.0008 to 0.0016 USD.
  - Held-out key8, reported only: recall .517, precision .228, F1 .298 over
    the 8; the resolution branch was entered on rows 33 and 42. The pool phase
    ran on rows 33 and 39, with 3 candidates, and kept none. "Entered" here is
    the harness's `resolution_entered` flag, which is set only when phase A
    resolves an anchor key: row 39's branch ran and scanned 20 anchors, found
    no anchor key, and so is recorded as not entered, while its pool phase still
    ran (250-research-10). No switch-off control ran for
    key8 on this sandbox, so key8 carries no evidence on the filter
    (250-research-9, 250-research-10).
  - A first attempt on the RDR-147 sandbox measured nothing: its engine
    (0.1.180) refuses the sensitivity-excluding search the 8.3.0 client needs.

### Critical Assumptions

- [x] The companies of the top N anchors are the companies the answer names, at N above 6. Status: Verified at N=20 on issue5 (paid run, 2026-11-15); N=40 and above unmeasured. Method: Spike.
- [x] Generation names exactly the anchor-key companies when N is above 6. Status: Verified at N=20 in 19 of 20 answers over four runs; the one miss did not enter the resolution branch and named ids. Method: Spike.
- [deferred: nexus-w8d30] A precision filter keeps precision at or above 0.95 on the widened pool. Status: Observed once, unreplicated, so not checked: in one MVV pass on issue5 the filter kept 4 of 4 gold keys on 2 of 5 questions, and pooled precision was .950, at the bar (250-research-5, 250-research-8, 250-research-9). Replication is not measured: the budget-20 repeat (250-research-3) predates the filter, and its variance was the resolution branch's entry (250-research-10). Method: Spike.
- [deferred: nexus-w8d30] Option E's gain holds beyond the five issue questions. Status: Unverified. The held-out key8 run kept no pool key and had no switch-off control (250-research-9), so it says nothing either way; the turn-on criteria (250-turn-on-criteria) require replicates and a set outside HERB. Method: Spike.
- [deferred: nexus-io7tw] Option A's gain is not specific to HERB's template overlap. Status: Unverified. This RDR does not claim it generalises. Method: Spike on a second corpus.
- [deferred: nexus-io7tw] Option B assumes entity keys sit in retrievable announcement chunks. Status: Unverified in general; true on HERB. Method: Spike.

## Proposed Solution

### Approach

Sam chose 2026-11-15 (ask `rdr250-lever`, option 2): a budget of 20 as plan 10's
default now, then Option E (anchor scan over all evidence) with a precision filter.
The record supports two separate levers, one per gap; this choice is Gap 1's.

The budget of 20 shipped as the default of plan 10 and of plan 8
(`entity-resolution`) in 4a25a7c5a (Phase 1 Step 1). It
needed no code: measured at F1 0.764 with precision 0.967, inside the gate
(250-research-2, 250-research-3).

This RDR's design is Option E with a precision filter (Phase 1 Steps 2 and 3). For
Gap 1, Option E widens the key scan from
the generator's 20 inputs to the search step's 40 hits. The generator's 20
chunks do not change; each kept pool key adds one joined item, within the
`max_repair_items` cap. Unfiltered it is
projected at precision 0.715 (250-research-4), below the 0.95 gate, so the
filter is part of the design, not an option. Steps 2 and 3 are built and on
develop behind a switch that is off; the MVV has run and passed (250-research-8).
With the switch off, what this RDR delivers on the default path is Step 1 alone;
Option E reaches users only when the switch is turned on (Open Question 7).

Gap 2 is not addressed by this RDR. For it, Option A has the largest measured F1
inside `nx_answer` (0.866), but its precision (0.853) fails the gate without a
filter, and its size depends on HERB's template. Option B is the cleanest
key-based lever, and it needs a new engine filter.

### Technical Design

Line numbers below were read at develop 01a80c2d1 on 2026-11-16, before the build; the build (7c44ceee8) moved some of them, and the symbol names are the stable reference.

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
chunks that carry a candidate key go through `CoverageBoundary._assess`, the
boundary's own assessment pass (its instruction, facet groups, `_pack` packing,
reply parsing and allowance checks), against the question's facets. A candidate
key is kept when a `supports` record cites its chunk's `item_id`. A chunk cited
only as contradicting does not keep its key, even when another item covers the
same facet (a covered facet's `evidence_ids` holds contradicting ids too, so it
is not the test). Only the cited chunk is joined; another pool chunk carrying
the same kept key is not. Keys found in the anchors are not filtered, so the measured budget-20 behaviour
is the floor. The filter adds one assessment pass, one call per pack. A distance cutoff
was rejected: it scored F1 at most 0.825 in simulation. Two kinds of citation
differ here: the benchmark's cited artifacts, which also name non-gold
companies, so an oracle keeping only them caps precision near 0.83
(250-research-5), and the assessor's `supports` record, which judges a chunk
against the question's facets. The filter uses the second. In the MVV it kept 4
of 4 gold keys and pooled precision was .950 (250-research-8, 250-research-9).

**Resolution, in two phases.** Phase A resolves and joins the anchors' keys
exactly as the switch-off path does, before any pool work, so the switch-on
result is never below the switch-off one. Phase B then fetches the pool, runs
the filter, and resolves the kept pool keys in a second `resolve` call. Pool
keys get what the `max_repair_items` cap (12, `coverage_repair.py:110`) leaves
after phase A's tokens and joined items (the larger of the two), and overflow is
named in `tokens_dropped`. The cap
does not bind on HERB at 40 (at most 11 keys, 250-research-6). A joined item for
a pool key uses the pool chunk's text as its anchor text, so it enters the
generator the way an anchor key's joined item does today. The generator's own
input of 20 chunks does not change.

**Budget and failure.** Phase B is bounded by the boundary's remaining answer
time and USD, after reserving what the later reassessment's planned calls need.
Its window is the repair's fetch deadline minus 3 s kept for the second
`resolve` and its fetch, and one call is sized as the first assessment's latency
per wave of calls, at least the boundary's minimum call time. If that window, or
the USD left after the reassessment's reserve, cannot fit one call, phase B is
skipped before the pool fetch (`filter_skipped_deadline`,
`filter_skipped_budget`) rather than started and cut. When phase A's resolve
failed (`resolution_failed`) or the anchors already fill the cap (`cap_full`),
phase B does not run, so no call is spent that cannot change the answer.
A reply with no record is a failed call, as the assessor's own instruction says ("a reply with no record is read as a failed assessment", coverage_ledger.py around line 322), named `filter_empty_reply` (nexus-w8d30); a reply whose records the host accepted, none citing support, is the reject verdict. The build first read an empty reply as a reject, which dropped every candidate silently (post-mortem). When some filter
calls fail, keys that completed calls cited as support are kept and the failed
packs' candidates are rejected (`filter_partial_failure`). No pool key is kept
when the fetch fails, the assessment raises or every call fails
(`filter_failed`), or the phase raises anything else (`pool_error`). Candidates
the pack size cap left unread are rejected (`filter_unread`). Every rejected
candidate is named in `pool_keys_rejected`, and phase A's result stands in every
case.

**Switch.** `NX_ANSWER_POOL_KEY_SCAN`, default off. Off, the code path is not
entered and the report carries no pool fields. On, the scan runs only where the
resolution branch runs: `NX_ANSWER_RESOLUTION_REPAIR` on, a facet left
unsupported, and an `entity.collections` pattern for the scope. A pool key can
resolve when the anchors name no key. Turning it on by default is a
later decision. The MVV has passed; the criteria for that decision are Sam's,
adopted 2026-11-17 (T2 `nexus_rdr/250-turn-on-criteria`, Open Question 7), and
one of them (a set outside HERB) cannot be measured on HERB. It stays off until
they are met and the flip is approved.

**Report.** The resolution fields gain `pool_scanned` (pool texts scanned:
fetched, non-empty, deduplicated and in scope),
`pool_candidate_keys`, `pool_keys_kept` and `pool_keys_rejected` (both by name),
`pool_filter_calls` (calls started, counted on every path, a cut call included),
`pool_filter_cost_usd` (the known assessment cost spent during the filter, read
through a `BoundaryView.assessment_cost_spent` hook the runner fills; a cut call
reports no cost), and `pool_reason` when
the pool was skipped or closed. The run summary's resolution counts carry the
same figures, so telemetry shows them. `anchors_scanned` and `anchor_key_ids`
keep their meaning (the anchors only), so existing readers are unchanged.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Lexical leg for hop 1 (Option A) | `search --lexical`, the engine's FTS and trigram union (shared probe from nexus-bbtc2) | Reuse: a flag exists; inside plan 10 at budget 20 it measured precision 0.853 (250-research-2) |
| Anchor budget of 20 (Step 1) | Plan 10 and plan 8 binding `vector_budget_chunks` | Reuse: both defaults changed to 20 in 4a25a7c5a |
| Key scan over the search pool (Option E) | `coverage_ledger.py` `_resolve_anchors`, `find_key_tokens` | Extend: scan the pool's chunks as well as the anchors |
| Pool chunk texts (Option E) | `BoundaryView.fetch`, the repair's accounted chunk fetch | Reuse |
| Precision filter (Option E) | `CoverageBoundary._assess`, the boundary's assessment pass, and its `supports` records | Reuse: one assessment pass, one call per pack, over candidate-key chunks only |
| Key-bearing filter (Option B) | None | New: an engine content-pattern filter |
| Entity-key pattern | `entity.collections` in config | Reuse for B and E |

### Decision Rationale

Gap 1 caps `nx_answer` at the anchor cut (6 before 4a25a7c5a, 20 since). At 20
the anchors are the hop-1 top 20, so Gap 2's ranking limit binds the same hits.
A lever can widen past the cut (Option E) or improve the ranking (Options A and
B). Inside `nx_answer` the record has Option A at F1 .866 with precision .853,
unfiltered and failing the gate, and Option E at F1 .878 with precision .967,
filtered (250-research-2, 250-research-8): comparable F1, and only E holds the
precision gate. Option A with a filter was never measured. Option E is chosen
(Sam, ask `rdr250-lever`) because it keeps every Gap 1 candidate under one
filter and reuses the boundary's fetch and assessment calls. Its precision was
observed once in the MVV and not replicated, so the switch stays off until the
adopted turn-on criteria (`250-turn-on-criteria`) are met.

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

**Why not chosen**: Inside `nx_answer` its F1 (.866) is comparable to Option
E's (.878), but its precision (.853) fails the gate without a filter, a filtered
Option A was never measured, and its generality is unproven.

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
- The filter's precision was observed once, in the MVV (250-research-8), not replicated. One assessment call per pack adds cost.
- The RDR-147 coverage re-check cited 35 of 45 joined items as supporting (147-research-10). That measures support, not precision.

**Why not chosen**: Its filter is taken into Option E (a support verdict from the
existing assessment call), but its deeper k is not: Option E's pool is the
search step's 40 hits, so no retrieval setting changes.

### Alternative 4: Option E, anchor scan over all evidence, decoupled from the generator budget

**Description**: The coverage boundary scans the entity-key pattern across all
search hits (the pool), not only the generator's inputs (6 then, 20 now). It
resolves the keys it finds. The generator's 20 chunks do not change; each kept
pool key adds one joined item, within the `max_repair_items` cap.

**Pros**:

- Projected unfiltered issue5 F1 0.817 (recall 0.967, precision 0.715), against 0.500 at budget 6. Measured with the filter in the MVV: F1 .878 against the budget-20 control's .764, so of the gain from .500, Step 1 gave +.264 and Option E +.114 (250-research-8).
- Comparable measured F1 to Option A (.878 against .866) at higher precision (.967 against .853); the measured Option E gain came from 4 kept keys on 2 questions, in one pass.
- The generator's 20 chunks do not change. Each kept key adds one joined item, within the cap: 8 to 15 percent more rank-and-generate cost on the two MVV questions that kept keys (250-research-9, 250-research-10).

**Cons**:

- Without a filter, precision falls to 0.715, below the 0.95 gate. A precision filter (Option C) is needed to hold the gate.
- Changes coverage boundary code, which RDR-232 owns.
- Assumes the generator names the companies the scan finds.

**Chosen**: Sam, 2026-11-15 (ask `rdr250-lever`, option 2), with the precision
filter in Technical Design. Built (Steps 2 and 3) and measured once: the MVV
passed the pre-registered bar (250-research-8).

### Briefly Rejected

- **Option D, per-artifact chunk expansion**: Its effect on HERB is expected to be about zero. Each id-bearing artifact is one 290 to 410 character chunk, and 37 such artifacts each name one id (measured). Thread or same-day expansion adds 13 to 16 ids per question, close to all 16 ids in the corpus, so precision falls to about 0.46 (estimated from the expansion counts, not run).

## Trade-offs

### Consequences

- Option E changes a coverage boundary RDR-232 owns, and it needs a precision filter to meet the gate.
- The budget of 20, now the default of plan 10 and plan 8 (4a25a7c5a), widens the rank and generate context and the assessor's packs for every `hybrid-factual-lookup` and `entity-resolution` call that does not bind the budget. Cost per call measured 0.03 to 0.17 USD, not materially above budget 6 (T2 `nexus_rdr/io7tw-paid-mvv-2026-11-15`, Findings).
- The filter adds one assessment call per pack of candidate-key chunks, only when the switch is on and the pool has candidate keys.

### Risks and Mitigations

- **Risk**: The five issue questions overfit. The k=10 and k=30 peaks are fitted to five questions, and the MVV's pass bar is issue5 only.
  **Mitigation**: Open. The held-out key8 check ran in the MVV and kept no pool key, with no switch-off control, so it neither confirms nor refutes the gain (250-research-9). The adopted turn-on criteria (250-turn-on-criteria) require replicates and a set outside HERB before the default changes.
- **Risk**: HERB's template overlap inflates the lexical gain.
  **Mitigation**: Report the lexical result on the held-out set and on a second corpus before relying on it.
- **Risk**: Precision falls below 0.95 past N=20.
  **Mitigation**: The precision filter, which held .950 pooled precision in one MVV pass (250-research-9), and the switch stays off by default.
- **Risk**: The support verdict keeps a non-gold company's key. The benchmark's cited artifacts also name non-gold companies (that oracle caps near 0.83, 250-research-5); the filter judges support instead, and kept no non-gold key in one MVV pass (250-research-9), which is not yet a replicated result.
  **Mitigation**: The MVV reports kept and rejected pool keys by name, so a filter that keeps them shows in the record, not only in the score.
- **Risk**: Answer cost. In the MVV the whole answer cost a mean 0.111 USD with the pool on against 0.056 for the control, almost all of it in the generation step. The run order explains most of it: the pool pass ran first (05:58 to 06:02 UTC) and the control second (06:02 to 06:05). On Q1, Q4 and Q5, where no pool key was kept, the control's generation input was the same prompt the pool pass had just sent (same joined item counts, 6, 6 and 4), and those control calls cost 0.031 to 0.042 USD, almost all output. On Q2 and Q3, where pool keys changed the prompt, both passes paid full input, and generation cost 0.095 against 0.083 and 0.087 against 0.081 USD: 8 to 15 percent more, in line with 6 joined items against 4. That the cheap control calls were prompt-cache reads is inferred from the cost split, not read: the run records carry no cache token counts. The timestamps, per-call costs and joined item counts are in 250-research-9; 250-research-8, written before the diagnosis, calls the gap unexplained. The filter itself adds 0.0008 to 0.0016 USD a question.
  **Mitigation**: A cost comparison runs the two arms interleaved or with a cold cache on both sides. The like-for-like figure is the Q2 and Q3 pair above.
- **Risk**: Step 1 (4a25a7c5a) raised the `vector_budget_chunks` default from 6 to 20 in two plans, `hybrid-factual-lookup` (plan 10) and `entity-resolution` (plan 8), for every call that does not bind the budget, once an install runs `nx plan reseed` (250-research-10). RDR-097 set the 6 as "a 60/40 split per RF-6" between the vector and graph streams. The evidence for 20 is HERB issue5; on the key8 held-out set budget 20 scored F1 .145 against .135 at 6 (250-research-9). The other HERB set measured, agg2 (two counting questions), scored 0 at both budgets, and no lookup set outside HERB was measured.
  **Mitigation**: None in this RDR. The default of 20 rests on HERB evidence only; a lookup set outside HERB, run at 6 and at 20, would test it.
- **Risk**: The resolution branch is sometimes not entered at all (budget-20 repeat, run d: one question scored 0, 250-research-3). That variance is outside this design, and it would show in the MVV as a lost question.
  **Mitigation**: The MVV reports `resolution_entered` per row, so a lost question is told apart from a precision loss.

### Failure Modes

A widened pool with no filter answers more companies and loses precision. The
MVV's precision gate is the visible check; the MVV reports precision split by
`generated_on_join`, so a loss shows as a gate failure, not a silent change.

A failed pool fetch, a filter whose every call failed, or a failed second
`resolve` keeps no pool key, so the answer is phase A's, today's anchors-only
result, and the report names `pool_reason`. A partial filter failure keeps only
the keys completed calls cited as support.

## Implementation Plan

### Prerequisites

- [x] Sam picks the lever or levers: option 2, 2026-11-15
- [x] Sam gives the go for the paid run: 2026-11-16, with the bar signed as written
- [x] The filter assumption observed once in the MVV (250-research-8), and the held-out set frozen before it (250-research-7)

### Minimum Viable Validation

One paid `nx_answer` run on HERB with `NX_ANSWER_POOL_KEY_SCAN` on, on the
develop code. It uses the RDR-147 MVV harness: a fresh sandbox, bge-768, plan
10 at its default budget of 20 with search limit 40, and `entity.collections`
set to `CUST-\d{4}`. It reports join completeness, per-run precision over the
answers that name a company, recall and F1 on issue5, `resolution_entered` per
row, the pool fields (keys kept and rejected by name), and the filter's added
cost per question (`pool_filter_cost_usd`, with `pool_filter_calls` beside it). Its baselines are
the budget-20 measurement, F1 0.764 (250-research-2), and the RDR-147 F1 0.500
(147-research-17).

The same run must include a held-out check beyond the five issue questions.
The questions are chosen and frozen before the run. They must not be used to
pick N or the filter threshold. The set run was key8, frozen by hash in
250-research-7 (Open Question 3).

Stop rule: if the first two questions skip phase B on time
(`filter_skipped_deadline`, with `pool_need_s` above `pool_room_s`), the run
stops before more spend and the window is widened first. The budget-20 rows
record about 41 s per answer and no per-call assessment latency, so whether one
filter call fits is read from the first rows, not assumed.

The paid run was spend, so it ran on Sam's explicit go, for 2.65 USD charged,
about 1.65 real (250-research-8, 250-mvv-2026-11-16). Its pass criteria were Sam's, signed before the
run (Open Question 5, 250-research-7). Changing them after the run would count
as changing promotion criteria.

### Phase 1: Code Implementation

#### Step 1: The `vector_budget_chunks` default of plans 10 and 8 from 6 to 20

Shipped as 4a25a7c5a: `conexus/plans/builtin/hybrid-factual-lookup.yml` and `entity-resolution.yml`, plus a test, no code. Installs get it at their next `nx plan reseed`.

#### Step 2: The pool key scan, behind the switch

Built: fe4a44e7f and 5d15ed57b on develop. The pool scan reads the search
step's ids from `step_outputs`, fetches the non-anchor ones through
`view.fetch`, scans them with `find_key_tokens` and adds the report fields,
behind `NX_ANSWER_POOL_KEY_SCAN`, default off. It shipped with the filter of
Step 3; no filterless state shipped (250-research-9).

#### Step 3: The precision filter

Built: fe4a44e7f and 5d15ed57b, then the two-phase rework and its review fixes
a305a16b2, b07678b76, 621237e40 and 9d9944fdb on develop. Candidate-key chunks
go through the boundary's assessment pass (`_assess`), a key is kept only when
a `supports` record cites its chunk, and kept keys resolve in phase B within the
cap left after the anchors. Budget and failure as Technical Design states.

#### Step 4: The MVV

Done 2026-11-16, after Sam's go and his sign-off on the pass criteria: passed
(250-research-8). Turning the switch on by default is a separate decision,
against the criteria Sam adopted 2026-11-17 (Open Question 7).

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
- **Scenario**: Switch on, a pool key the assessment cites. **Verify**: The key is resolved and joined; `pool_scanned` counts the in-scope pool texts, `pool_keys_kept` names it, and `anchors_scanned` still counts the anchors only.
- **Scenario**: Switch on, a pool key the assessment does not cite. **Verify**: The key is not resolved and is named in `pool_keys_rejected`. This is the non-vacuity check: the filter must be able to reject.
- **Scenario**: The filter call fails, returns malformed records, or the allowance is short. **Verify**: No candidate key is kept, the anchors' keys still resolve, and `pool_reason` names the cause.
- **Scenario**: A slow pool fetch or a filter cut by its deadline. **Verify**: The anchors' keys still resolve (no `failure`), and `pool_filter_calls` counts the cut call.
- **Scenario**: Two filter packs, one failing. **Verify**: Keys the completed pack supported are kept, and the failed pack's candidates are rejected with `filter_partial_failure`.
- **Scenario**: A plan with no `search` step. **Verify**: No pool, and `pool_reason` says so.
- **Scenario**: More keys than `max_repair_items`. **Verify**: Anchor keys fill the cap first, and dropped keys are named in `tokens_dropped`.

## Validation

### Testing Strategy

1. **Scenario**: The MVV above, on the chosen lever.
   **Expected**: The pass criteria Sam signed before the run (Open Question 5), reported beside the 0.764 and 0.500 baselines. Result: passed (250-research-8).
2. **Scenario**: The held-out check.
   **Expected**: Results reported on questions chosen before the run, with no tuning on them.

### Performance Expectations

No latency figures are recorded for the lexical leg inside `nx_answer`. None are
estimated here. With the pool on, MVV answers took 44 to 58 s against 36 to 44 s
for the control (250-research-8). The filter's own call time was not measured
separately: the run records do not mark which assessment call was the filter's.

## Finalization Gate

- **Contradictions**: The Approach, Technical Design and Implementation Plan name one design (Option E with a support-verdict filter, behind a switch). Option A's earlier "unmeasured" lines were updated to the paid measurement.
- **Assumptions**: The anchor-key assumption is verified at N=20 (250-research-2, 250-research-3). The filter's precision was observed once in the MVV, unreplicated (250-research-5, 250-research-8). The unfiltered Option E projection (250-research-4) stays assumed; it is the reason the filter is part of the design, and no threshold, cap or window in the design is derived from it. The switch stays off by default.
- **Scope**: Gap 1 only; Step 1 moved plan 8's default with plan 10's, on the same HERB evidence. Gap 2 (Options A and B) and the resolution branch's entry variance are named and left out.
- **Cross-cutting**: The change sits in the coverage boundary that RDR-232 owns. RDR-232 bounds the boundary's repair and reassessment "to one cycle" (rdr-232 line 2430), and records a repair as costing "about 5 search calls and one reassessment call" (line 603). Under the switch, the pool filter is one more assessment pass inside that one repair: the MVV saw 3 `operator_check` calls per issue5 answer with the pool on against 2 off (250-research-9); a key8 row whose pool phase made no filter call had none extra. A reader that treats two assessment passes per run as fixed sees 3 under the switch. The change reuses the boundary's fetch and assessment calls, adds no engine surface, and leaves `anchors_scanned` and the switch-off path unchanged.
- **Proportionality**: One switch, one fetch and one assessment call per pack, no new dependency. The paid MVV is Sam's go.

### Open Questions for Sam

1. Answered 2026-11-15 (option 2): budget 20 as the default now, then Option E with a precision filter. Which lever or levers to build. Options E and B need code. Option A is a flag, and it fails the precision gate without a filter. A budget of 20 needs no code and passes the gate on issue5 (measured; in the repeat, one of four runs fell to precision .767 when the resolution branch was not entered, 250-research-3).
2. Answered 2026-11-15: the paid run was made, for 3.37 USD (Key Discoveries). The budget-20 repeat was run the same day (three runs, Key Discoveries).
3. Answered 2026-11-16: the held-out set run was key8 (8 questions, frozen by hash in 250-research-7), not the RDR-147 20-question set, which has no scorer in the harness.
4. Answered 2026-11-15: the plan 10 default changes (option 2); 4a25a7c5a moved plan 8's with it (250-research-10).
5. Answered 2026-11-16 (signed as written, 250-research-7; the MVV passed, 250-research-8). The MVV's pass criteria, set before the run: precision at or above 0.95 over the answered issue5 rows, F1 above the budget-20 measurement of 0.764, join completeness 1.0, and the held-out set reported. F1 rather than recall alone, so a widened pool cannot pass by naming more companies.
6. The resolution branch's entry variance (run d, 250-research-3; in the MVV, 6 of 8 key8 rows did not enter it) is outside this RDR. It is tracked as a comment on bead nexus-io7tw (halcyon, 2026-11-17: no new bead during the burn-down).
7. Turning the switch on by default. Answered 2026-11-17: Sam adopted five turn-on criteria and funded one HERB measurement window (T2 `nexus_rdr/250-turn-on-criteria`, pre-registered before any run). The decision is halcyon's to carry to Sam, tracked as bead nexus-w8d30 (Sam authorized it 2026-11-17); the measurement runs under that bead. The default flip is a separate change.
   Parked 2026-11-18 (Sam). The window failed criterion C1. The pool scan reached R .787, P .943, F1 .837 against HERB gold, above the budget-20 F1 of 0.764, but pooled precision was .900, under the 0.95 bar (T2 `nexus_rdr/250-turn-on-run-2026-11-18`). The losses trace to one key, CUST-0086 (InnovateAI). The filter kept it on the AI Model & Search Algorithm question, where it is not gold, and rejected it on the backend-search-engine question, where it is. An offline replay with a stricter instruction, which required the item to name the question's component, also failed (0.115 USD, `nexus_rdr/250-offline-replay-2026-11-18`). A census of all of HERB (33 files, 374,515 strings, `nexus_rdr/250-key0086-census-2026-11-18`) shows the label can be derived from the corpus. HERB's gold follows the question's impact clause ("prevented retrieval of important historical messages"), not the component name written in each issue. 0086's issue text and its fix, PR 54, state that impact, and HERB's own engineer question (herb_index 31) puts PR 54 in the same group. The miss is our filter's reading, not a defect in the benchmark. The switch stays off, and nexus-w8d30 is deferred. Revival: a filter instruction that matches the question's impact clause, pre-registered and replayed on the seven saved payloads (about 0.12 USD), then the window's criteria, unchanged.

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
- 2026-11-16: Sam chose to build and measure before the gate. Technical Design follows the build after review round 1: two-phase resolution (anchors first, then the pool within its own bounds), counted filter calls and cost, partial verdicts, cap_full and the other skip reasons.
- 2026-11-16: The MVV ran (2.65 USD) and passed the pre-registered bar: issue5 .827/.967/.878, join completeness 1.0, against a same-sandbox control of .667/.967/.764. Key Discoveries, Critical Assumptions, Prerequisites, Performance Expectations, Risks (answer cost) and Open Question 5 record it.
- 2026-11-17: Gate round 1 — BLOCKED (1 Critical, 5 Significant, 0 ship-blocker(s)); blob `9049a3d75ef3`; critique `nexus_rdr/250-gate-critique-2026-11-17-r1`.
- 2026-11-17: Gate round 2 — PASSED (0 Critical, 4 Significant, 0 ship-blocker(s)); blob `238fe91bba26`; critique `nexus_rdr/250-gate-critique-2026-11-17-r2`.
- 2026-11-17: Accepted (Sam). Grant 3 wording amend to match the reviewed build: Step 1 and its sites name plan 8 with plan 10, and the Problem Statement states RDR-097's 6/4 origin in RDR-097's words.
- 2026-11-17: Close amend (grant 3; close critique partial, 3 Significant): Problem Statement states the post-Step-1 gap; the filter assumption unchecked and a held-out assumption added; overfit risk left open with the key8 result; Option E gain split from Step 1's; Open Question 7 records the adopted turn-on criteria and their owner; spend, "entered", RDR-232 citation and stale site lines corrected.
- 2026-11-17: Close amend 2 (grant 3; second close critique partial, 4 Significant): Gap 2 stated as binding `nx_answer` at budget 20; the Option E rationale compares measured figures instead of claiming the largest lever; the turn-on criteria cited at every site; title note added.
- 2026-11-18: Post-close amend (Sam's direction via halcyon): Open Question 7 records the failed turn-on window, the offline replay, the HERB census that finds key 0086's label derivable through the question's impact clause, the park, and the revival test.
