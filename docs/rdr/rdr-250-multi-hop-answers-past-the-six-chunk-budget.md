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

### Critical Assumptions

- [ ] The companies of the top N anchors are the companies the answer names, at N above 6. Status: Unverified (only N=6 is measured). Method: Spike.
- [ ] Generation names exactly the anchor-key companies when N is above 6. Status: Unverified. Method: Spike (a paid run; see Minimum Viable Validation).
- [ ] A precision filter keeps precision at or above 0.95 on the widened pool. Status: Unverified. No LLM filter has been measured. Method: Spike.
- [ ] Option A's gain is not specific to HERB's template overlap. Status: Unverified. This RDR does not claim it generalises. Method: Spike on a second corpus.
- [ ] Option B assumes entity keys sit in retrievable announcement chunks. Status: Unverified in general; true on HERB. Method: Spike.

## Proposed Solution

### Approach

No lever is chosen. The decision belongs to Sam (see Open Questions for Sam
below). The record supports two separate levers, one per gap.

The draft's lean, for Sam to accept or reject: for Gap 1, Option E is the
largest lever in the record, and it changes the coverage boundary's anchor input
without touching the generator's budget. It needs a precision filter before it
can meet the gate, so it cannot ship on the projection alone. A cheaper interim
step is to pass `vector_budget_chunks: 20` per call, with no code change. That
is projected at F1 0.764 with precision 0.967, inside the gate, but it is also
unmeasured. For Gap 2, Option A is a flag-level change with a measured gain on
the script, but its size depends on HERB's template. Option B is the cleanest
key-based lever, and it needs a new engine filter.

### Technical Design

Not written. If Option E is chosen, the change is to the coverage boundary's
anchor input: scan the entity-key pattern across the search step's pool (the 40
search hits and their hydrations) instead of the trailing run's six inputs. The
generator's context budget stays at 6. The sites are `runner.py` near 4827–4855
(`_boundary_evidence`, `_read_rest_of_run`) and `coverage_ledger.py`
`build_anchors` near 743, at sha 1ecf3ca6d.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Lexical leg for hop 1 (Option A) | `search --lexical`, the engine's FTS and trigram union (shared probe from nexus-bbtc2) | Reuse: a flag exists; its effect inside plans is unmeasured |
| Per-call anchor budget (interim step) | Plan 10 optional binding `vector_budget_chunks` | Reuse: already a binding |
| Anchor scan over the search pool (Option E) | `coverage_ledger.py` `build_anchors`, the anchor scan | Extend: change the input from the six inputs to the search pool |
| Key-bearing filter (Option B) | None | New: an engine content-pattern filter |
| Entity-key pattern | `entity.collections` in config | Reuse for B and E |

### Decision Rationale

Gap 1 and Gap 2 have separate binding limits, so one lever does not fix both.
Gap 1 caps `nx_answer` at the six-anchor cut. Gap 2 caps the script, and any
design that reads the hop-1 pool directly. A lever for Gap 1 therefore does
not need to improve hop-1 recall, and the reverse also holds. The choice between
levers depends on the measured precision cost of each, and the record has none
for a filter.

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
- Its effect inside `nx_answer` plans is unmeasured.

**Why not chosen yet**: Its generality is unproven, and it has no `nx_answer` measurement.

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

**Why not chosen yet**: The filter is the unknown, and it is unmeasured.

### Alternative 4: Option E, anchor scan over all evidence, decoupled from the generator budget

**Description**: The coverage boundary scans the entity-key pattern across all
hydrated evidence (the search pool), not only its six terminal inputs. It
resolves the keys it finds. The generator still gets six chunks.

**Pros**:

- Projected issue5 F1 0.817 (recall 0.967, precision 0.715), against 0.500 measured.
- The largest projected lever for `nx_answer` in the record.
- The generator's context does not grow.

**Cons**:

- Without a filter, precision falls to 0.715, below the 0.95 gate. A precision filter (Option C) is needed to hold the gate.
- Changes coverage boundary code, which RDR-232 owns.
- Assumes the generator names the companies the scan finds.

**Why not chosen yet**: It is the largest projected lever, but the projection rests on the assumptions above, and it has no measured run.

### Briefly Rejected

- **Option D, per-artifact chunk expansion**: Its effect on HERB is expected to be about zero. Each id-bearing artifact is one 290 to 410 character chunk, and 37 such artifacts each name one id (measured). Thread or same-day expansion adds 13 to 16 ids per question, close to all 16 ids in the corpus, so precision falls to about 0.46 (estimated from the expansion counts, not run).

## Trade-offs

### Consequences

- Option E changes a coverage boundary RDR-232 owns, and it needs a precision filter to meet the gate.
- A per-call budget of 20 widens the rank and generate context, and the assessor's packs. The cost is not measured.
- Changing the plan 10 default changes every `hybrid-factual-lookup` call. A per-call binding changes only one.

### Risks and Mitigations

- **Risk**: The five issue questions overfit. The k=10 and k=30 peaks are fitted to five questions.
  **Mitigation**: A held-out check beyond the five (Minimum Viable Validation).
- **Risk**: HERB's template overlap inflates the lexical gain.
  **Mitigation**: Report the lexical result on the held-out set and on a second corpus before relying on it.
- **Risk**: Precision falls below 0.95 past N=20.
  **Mitigation**: A precision filter, measured before any default changes.

### Failure Modes

A widened pool with no filter answers more companies and loses precision. The
MVV's precision gate is the visible check; the MVV reports precision split by
`generated_on_join`, so a loss shows as a gate failure, not a silent change.

## Implementation Plan

### Prerequisites

- [ ] Sam picks the lever or levers (see Open Questions for Sam)
- [ ] Sam gives the go for the paid run
- [ ] Critical Assumptions verified, or a held-out set chosen

### Minimum Viable Validation

One paid `nx_answer` run on HERB, with the decoupled anchor scan (Option E) on
the develop code. It uses the RDR-147 MVV harness: a fresh sandbox, bge-768, k=20,
and `entity.collections` set to `CUST-\d{4}`. It reports join completeness,
per-run precision over the answers that name a company, and recall and F1 on
issue5. Its baseline is the RDR-147 measured F1 0.500 (147-research-17).

The same run must include a held-out check beyond the five issue questions.
The questions are chosen and frozen before the run. They must not be used to
pick N or the filter threshold. The 20 valid held-out questions of the RDR-147
MVV (branch entered 0) are one candidate. The record does not say whether they
were used for tuning, so that needs checking before use.

The paid run is spend and it calls the cloud, so it needs Sam's explicit go and
an announced window. For scale, the two-run RDR-147 MVV cost 1.20 USD
(147-research-17). Pass criteria are not set in this draft. Changing them after
the run counts as changing promotion criteria, so Sam signs them off first.

### Phase 1: Code Implementation

Not written. Steps follow the lever Sam chooses.

### Phase 2: Operational Activation

Omitted. Nothing is deployed by this RDR.

### Day 2 Operations

No persistent resource is created by the draft. An Option B filter would be a
new engine surface, and its Day 2 operations would be written then.

### New Dependencies

None.

## Test Plan

- **Scenario**: The coverage boundary's anchors come from the search pool, not the six inputs. **Verify**: On a fixed fixture, the anchor count equals the widened pool's key count, and `anchors_scanned` reports the pool size.
- **Scenario**: A widened pool with no filter fails the precision gate. **Verify**: On a deliberately noisy pool, the gate fails. This is the non-vacuity check: the gate must be able to fail.

## Validation

### Testing Strategy

1. **Scenario**: The MVV above, on the chosen lever.
   **Expected**: Join completeness and precision at or above 0.95 on the in-domain answers, with recall and F1 reported beside the 0.500 baseline.
2. **Scenario**: The held-out check.
   **Expected**: Results reported on questions chosen before the run, with no tuning on them.

### Performance Expectations

No latency figures are recorded for the lexical leg inside `nx_answer`. None are
estimated here.

## Finalization Gate

Not run. This draft does not go to `/conexus:rdr-gate` until Sam picks a lever.

### Open Questions for Sam

1. Which lever or levers to build. Options E and B need code. Option A is a flag. The per-call budget of 20 needs no code, but it is unmeasured.
2. Whether to spend on the paid MVV run now, and in which window.
3. Whether the 20 held-out questions in the RDR-147 MVV were used for tuning. If they were, a new held-out set is needed.
4. Whether changing the plan 10 default is in scope, or only per-call bindings.

## References

- T2 `nexus_rdr/io7tw-hop1-recall-research-2026-11-15`, with its follow-up section (evidence for this RDR).
- T2 `nexus_rdr/147-research-9` (the 0.7167 spike), `147-research-10` (the coverage re-check), `147-research-17` (the final MVV, F1 0.500).
- `docs/rdr/rdr-147-multihop-entity-resolution-retrieval.md` (R5, R6, the MVV bar).
- `docs/rdr/rdr-097-hybrid-retrieval-plan-template.md` (RF-6, around lines 81 and 107).
- `conexus/plans/builtin/hybrid-factual-lookup.yml` (plan 10 defaults and steps).
- Code at sha 1ecf3ca6d: `runner.py` near 1823 and 4827–4855; `coverage_ledger.py` `build_anchors` near 743 and the anchor scan near 1297; `coverage_repair.py:89`.
- Bead `nexus-io7tw`, and `nexus-bwct4` (RDR-147).

## Revision History

- 2026-11-15: Draft created from nexus-io7tw and its research record. Not gated, not accepted.
