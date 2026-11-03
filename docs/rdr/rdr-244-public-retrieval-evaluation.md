---
title: "Public retrieval evaluation others can run"
id: RDR-244
type: Feature
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-11-03
accepted_date:
related_issues: ["nexus-2k66t.2", "nexus-2k66t"]
related: [RDR-090, RDR-028, RDR-200]
---

# RDR-244: Public retrieval evaluation others can run

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Terms. **nx** is the Nexus command-line tool. **nx search** is plain vector
search: it ranks document chunks by embedding similarity, with no language model
reading them. **nx_answer** gathers chunks from many documents and has a language
model write an answer; it calls the Claude command-line tool (`claude -p`).
**bge-768** is the local embedding model Nexus uses by default. **Voyage**
(voyage-context-3) is a hosted embedding model Nexus can use instead.
**Recall@10** is the share of questions whose correct source is in the top 10
results. **MRR** (mean reciprocal rank) averages 1/rank of the first correct
result. **p50** and **p95** are median and 95th-percentile latency. **NDCG@3**
scores the top 3 results by rank order. **LitQA2** is a set of multiple-choice
questions about recent scientific papers, from the LAB-Bench suite. **Abstain**
means answering "insufficient information" instead of guessing.

## Problem Statement

Nexus does not publish a retrieval number that anyone outside the project can
reproduce. The internal bench has five queries on one collection, and its one
pre-registered pass criterion, set in RDR-090, failed at that size. The plugin
evaluations measure whether a skill triggers (12 of 15), not whether search
returns the right answer (Source: bead nexus-2k66t.2).

### Enumerated gaps to close

#### Gap 1: No code-path recall or latency number

The internal bench (`scripts/bench/runner.py`) scores NDCG@3 only. It has no
recall@k and no latency table. A user asking how good and how fast search is over
code has no published answer. Closed by a recall@10, MRR and p50/p95 table for
`nx search` on code, with pinned versions.

#### Gap 2: No answer-quality number for the paper path

Nexus has no measured accuracy for answering questions from scientific papers.
The only published figures for this task come from the benchmark's own authors
(see Alternatives). Closed by a stratified LitQA2 subset run through nx_answer,
reporting accuracy, precision and abstain rate.

#### Gap 3: No harness a third party can run

The query sets are not public. `bench/queries/golden_v1.yaml` holds 24
hand-labelled requests over this repository's own RDRs, and
`golden_heldout_v1.yaml` holds 29 more whose oracle labels are all still
`todo: true`. Nobody outside the project can reproduce a number. Closed by one
script with pinned versions, a local mode where that works, and a results format.

#### Gap 4: Failed internal results are not reported

RDR-090 pre-registered: a path difference of at least 0.05 in NDCG@3 on at least
3 of 5 queries. Two of five passed (`docs/rdr/rdr-090-realistic-agenticscholar-benchmark.md:127`;
`scripts/spikes/spike_rdr090_5q_summary.json`, per-query `deltas`). RDR-090 said
the bench "proceeds" at N=30, and no N=30 run exists in the repository (Source:
T2 `nexus_rdr/090-research-2` [708]). Closed by publishing the RDR-090 result
beside the new numbers, unedited.

## Relationship to Prior RDRs

I scanned the RDR filenames in `docs/rdr/` for bench, eval, retrieval, recall and
search. RDR-014 and RDR-028 matched by title only and were not read.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-090 (`rdr-090-realistic-agenticscholar-benchmark.md`) | Origin | Created the internal bench and the N=5 criterion, on the rationale that N=30 would decide. N=5 already failed and N=30 never ran. This RDR publishes the failure and proposes a larger, pre-registered test. |
| RDR-200 (`rdr-200-phase1-prereg.md`) | Precedent (read by name only) | The repository already keeps pre-registration files before a gate run. This RDR uses the same practice. |
| RDR-028 (code search recall) | Adjacent, title only | May already hold a code-recall measurement. Check it before Track A, so the new table does not silently repeat it. |

## Context

### Background

The internal bench compares three routes: path A (plain `nx search`), path B
(nx_answer under a fixed plan), and path C (nx_answer forced to plan each query).
RDR-090 ran them on five queries (spike summary, k=3):

- Path A: NDCG@3 mean 0.555, 9.0 s mean latency.
- Path B: 0.277, 48.5 s.
- Path C: 0.277, 36.8 s.

Path A's lead comes from two queries: Q3 (factual) A 0.917, B and C 0; Q4
(comparative) A 0.469, B and C 0. Q2 scored 0 on every path because its labels
used different words from the indexed text. Q5 scored 0.469 on every path, so it
separates nothing. The criterion needed three passing queries and got two.

Competitors publish vendor-run answer-quality numbers, which are disputed but
exist. Nexus publishes none (Source: bead nexus-2k66t.2).

### Technical Environment

- conexus 7.94.1 (`pyproject.toml:14`); engine tag `engine-service-v0.1.176`.
- Local default embedding: fastembed bge-768, around `src/nexus/config.py:875`.
- Managed embedding: voyage-context-3 at $0.18 per million tokens (docs.voyageai.com/docs/pricing, updated 2026-09-29).
- PDF ingestion: `nx index pdf` (`src/nexus/commands/index.py:3051`).
- nx_answer and aspect extraction need the Claude command-line tool. Storage and
  plain search need no API key (T2 research).

## Research Findings

Research record: T2 `nexus/2k66t2-eval-design-research-2026-11-03`. Design research
only: landing pages, READMEs, licences and the PaperQA2 paper were read; no
dataset or paper was downloaded, and no nx_answer or indexing run was done.

### Investigation

Four questions were investigated: what LitQA2 is and how PaperQA2 scored it (the paper, the dataset card); what CoIR offers a code-retrieval track (paper, repository); what the internal bench and RDR-090 already measure (`scripts/bench/`, `scripts/spikes/spike_rdr090_5q_summary.json`, `docs/rdr/rdr-090-realistic-agenticscholar-benchmark.md:127`); and what one run costs, from live nx_answer telemetry (T2 `nexus/rdr232-unread-anchor-withhold-cost-2026-11-03`). The sources and what each settled are in the table below; the findings are recorded per item in T2 `nexus_rdr/244-research-*`.

### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| PaperQA2 (FutureHouse) | Yes, HTML paper (arxiv.org/html/2409.13740v2) | LitQA2 accuracy 66.0%; precision 85.2% ±1.1 (n=3); insufficient-information answers 21.9% ±0.9. Authors ran their own system. |
| LAB-Bench / LitQA2 (`huggingface.co/datasets/futurehouse/lab-bench`) | Yes, dataset card | Card shows 199 rows; the paper says 248 questions. Repository licence CC-BY-SA-4.0 (LICENSE text not read). |
| CoIR (code retrieval; arXiv 2407.02883, github.com/CoIR-team/coir) | Yes, per research and a follow-up search | Apache-2.0; 10 datasets per most sources (one source says 8 tasks); data on Hugging Face. |
| BEIR SciFact, NFCorpus | Yes, per research | Licence not stated in the beir README; check allenai/scifact. |
| CodeSearchNet | Yes, per research | MIT repository; per-function licences in `_licenses.pkl`. |

### Key Discoveries

- **Documented**: PaperQA2's 66.0% is accuracy over all questions, and precision
  (correct over answered) is 85.2%. The bead's "precision vs accuracy" wording
  mixes them up. An "insufficient information" answer scores as unsure, except
  where the ideal answer is null, where it scores correct (arxiv.org/html/2409.13740v2).
- **Documented**: the paper's human baseline (n=9) has precision 73.8 ±9.6 and
  accuracy 67.7 ±11.9. The paper reports no significant accuracy difference from
  PaperQA2 (p=0.66). It is vendor-written.
- **Documented**: LitQA2 names no corpus, and PaperQA2 works only from files the
  user holds. Whether the dataset's DOIs are open access is unverified.
- **Documented**: the LitQA2 option count is not stated in the pages read.
- **Verified** (read from the repository): RDR-090's criterion passed 2 of 5 against 3 required.
- **Assumed**: indexing 248 papers at about 10,000 tokens each is about 2.5 million
  tokens, or about $0.45 at $0.18 per million. The per-paper token count is an
  estimate and needs measuring.

### Critical Assumptions

- [ ] The LitQA2 subset can be used under CC-BY-SA without redistributing paper text. **Status**: Unverified. **Method**: Docs Only.
- [ ] The 199-row and 248-question versions give the same stratified sample. **Status**: Unverified. **Method**: Docs Only.
- [ ] Open-access full text can be obtained legally for the sampled papers. **Status**: Unverified. **Method**: Docs Only.
- [ ] nx_answer runs with local embeddings and no Voyage key. **Status**: Unverified. **Method**: Spike.
- [ ] Track A runs at no API cost. **Status**: Verified for search and storage (research); CoIR indexing time at full scale unverified. **Method**: Spike.

## Proposed Solution

### Approach

Two tracks, one harness, one results file.

**Track A, code path (no language model).** Score `nx search` on CoIR code retrieval
with bge-768. Report recall@10, MRR, and p50 and p95 latency. No API key, Apache-2.0 data.

**Track B, paper path (language model).** Run a stratified sample of about 50
LitQA2 questions through nx_answer. Report accuracy, precision and abstain rate,
as PaperQA2 does. The benchmark is the PaperQA2 authors', so the results page
labels it vendor-authored.

**RDR-090 alongside.** The published page carries the N=5 result, failed
criterion included, beside both tracks.

### Technical Design

Interfaces only.

- Input: a query-set file (id, text, gold labels, licence tag); a corpus manifest
  (identifier, source URL, checksum); a pinned version record.
- Track A: for each query, call `nx search`; record the ranked identifiers and
  wall-clock time. The scorer computes recall@10 and MRR.
- Track B: for each question, call nx_answer under a fixed plan; record the answer,
  its cited sources and wall-clock time. The scorer maps the answer to a letter or
  to "abstain", then computes accuracy, precision and abstain rate.
- Output: `results.json` with track, query-set hash, versions, per-item results,
  and summary statistics with sample sizes.

`scripts/bench/metrics.py` computes NDCG only, so a recall@k and MRR scorer is new.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Track A search call | `scripts/bench/runner.py` path A | Reuse; add per-call timing. |
| Recall@10 and MRR scorer | `scripts/bench/metrics.py` has NDCG only | Extend; keep NDCG for RDR-090. |
| Track B answer call | `scripts/bench/runner.py` path B | Reuse with a fixed plan, not the plan library. |
| Corpus fetch and index | `nx index pdf` (`src/nexus/commands/index.py:3051`) | Reuse; fetching is new and sits outside the engine. |
| Results format | none | New. |

### Decision Rationale

Track A uses a public code set because it costs nothing, needs no language model,
and covers the part of Nexus most people use. Track B uses LitQA2 because it is the
only public benchmark found that reports precision, accuracy and abstain
separately, so Nexus's number can sit beside PaperQA2's on the same terms. It is
the PaperQA2 authors' own benchmark, so it is the weaker of the two.

The internal bench is not the public result: its queries are private and five
queries cannot separate the paths.

### Alternatives Considered

- **BEIR SciFact and NFCorpus.** Pros: free, retrieval-only, 300 and 323 queries, widely reported. Cons: no answer-path measure; licence not stated in the BEIR README. Rejection reason: not rejected, deferred as a second IR check for a later RDR.
- **CodeSearchNet.** Pros: about 2M code pairs and a 99-query human set. Cons: per-function licences make a derived set hard to publish. Rejection reason: CoIR covers code retrieval under one licence (Apache-2.0).
- **Full LitQA2 (248 questions).** Pros: the complete benchmark PaperQA2 reports on. Cons: about $176 and about 5.5 hours serial at p50 80 s; vendor-authored. Rejection reason: deferred with Track B by the owner decision of 2026-11-03.
- **LoCoMo, LongMemEval.** Pros: memory benchmarks with published comparisons. Cons: memory-system vendors dispute the published numbers. Rejection reason: excluded by the bead (nexus-2k66t.2).

## Decisions for the Project Owner

**Decided 2026-11-03** (Sam; relayed by linda and recorded on nexus-2k66t.7):

- Publish: yes.
- Spend: $0 for now.
- Track A leads, and is the only track built now.
- The harness is search-only: no nx_answer, no Claude command-line tool.
- Track B and RDR-090's N=30 are deferred until LitQA2's blockers (open-access status, 248 versus 199 rows, option count) are answered; reopening them is a new owner decision.

The questions as they were put:

1. **Publish.** Publishing the harness and results means releasing question data under CC-BY-SA (share-alike applies to any derived question data). It does not redistribute paper text, but whether a derived question set counts as an adaptation is a licence reading the owner should confirm.
2. **Spend.** About $35 for a 50-question LitQA2 subset; about $176 for all 248; about $0.45 indexing with Voyage, or $0 with local embeddings. Track A is free.
3. **Which track leads.** Track A is cheap and has no vendor-authorship issue. Track B answers the question users ask most, but it is the weaker benchmark.
4. **Whether the harness may use nx_answer.** nx_answer needs the Claude command-line tool. A search-only harness needs no Claude account, but Track B cannot run.
5. **Whether to run RDR-090's N=30.** It was specified and never run.

## Trade-offs

### Consequences

- A public number for search and for paper QA, with the caveats printed beside it. A weak number is published too, and that is the point.
- Track B takes about 80 s per question at p50, so a full run is hours. That figure comes from repository questions, not paper questions, and must be re-measured.
- The harness is a maintained artefact. Pins go stale when conexus or the engine changes, so each results file records its versions.

### Risks and Mitigations

- **Risk**: the LitQA2 licence or open-access status blocks Track B. **Mitigation**: resolve the two unverified assumptions before any spend. Track A still publishes.
- **Risk**: the sample is too small to separate the paths. **Mitigation**: report confidence intervals with every number; claim no difference the intervals do not support.
- **Risk**: the numbers read as a competitive claim. **Mitigation**: label Track B vendor-authored; publish the RDR-090 failure beside it.

### Failure Modes

Only the modes that change a decision:

- **Timeout scored as abstention.** An nx_answer run that hits the 300 s limit must be recorded as an error and left out of the accuracy denominator, with the count reported. Scoring it as abstention would inflate the abstain rate.
- **Query set changes between runs** (199 versus 248 rows). The results file records a query-set hash, so the change shows as a different hash and the run is labelled as a different set.

## Implementation Plan

### Prerequisites

- [ ] (Track B, deferred) LitQA2 licence and open-access status checked (Critical Assumptions 1 and 3).
- [ ] (Track B, deferred; Track A uses local bge-768) Voyage free-tier status confirmed. Research: docs state no free tier for voyage-context-3.
- [ ] (Track B, deferred) 248-versus-199 question count resolved from the dataset card and paper.
- [ ] (Track B, deferred) LitQA2 multiple-choice option count confirmed.
- [x] Owner decisions 1 to 5 recorded (2026-11-03, nexus-2k66t.7).
- [ ] Pre-registered criteria committed before the first run (see Validation).

### Minimum Viable Validation

Track A on a small code subset, end to end: the harness runs `nx search`, scores
recall@10 and MRR, writes `results.json`, and a second run from the same pins
reproduces the numbers. It proves the harness before any money is spent, and it is
in scope for the first implementation step.

### Phase 1: Code Implementation

#### Step 1: Track A harness

Add a recall@k and MRR scorer beside `scripts/bench/metrics.py`. Add per-call timing
to path A in `scripts/bench/runner.py`. Write the `results.json` writer.

#### Step 2: Track A run

Run the CoIR subset with pinned versions. Record the numbers and their intervals.

#### Step 3: Track B harness

Add the fixed nx_answer plan, the letter mapping, and the abstain and timeout
handling. Run only the pre-registered subset, and only after the prerequisites are met.

### Phase 2: Operational Activation

#### Activation Step 1: Publish

Publish the harness, the query-set manifest and each `results.json`, with the
RDR-090 result beside them. Waits for owner decision 1.

### Day 2 Operations

The only persistent output is result files. The paper corpus download is a local
cache, not a Nexus collection, and is re-fetchable from its identifiers.

### New Dependencies

- CoIR data (Hugging Face; Apache-2.0).
- LitQA2 dataset (CC-BY-SA-4.0); legal review before any derived set is published.
- No new runtime dependency for the harness.

## Test Plan

- **Scenario**: Track A scorer on a hand-computed three-query example — **Verify**: recall@10 and MRR match the hand values exactly.
- **Scenario**: Track A run twice with the same pins — **Verify**: identical summary numbers.
- **Scenario**: Track B mapping on fixed sample answers, including an "insufficient information" answer on a null-ideal question — **Verify**: scored as the research's rule states.
- **Scenario**: nx_answer timeout — **Verify**: recorded as an error, excluded from the accuracy denominator, count reported.
- **Scenario**: changed query-set file — **Verify**: the hash differs and the run is labelled as a different set.

## Validation

### Testing Strategy

Pre-register before any run: the query set, the sampling rule (stratification
variable and seed), the scoring rule, the statistics reported, and the decision the
owner will take from the result. Commit that file before the first run and do not
change it afterwards. RDR-090 set its criterion before its run and then reported the
failure; this RDR does the same whichever way the result falls.

1. **Scenario**: Track A on the pinned CoIR subset.
   **Expected**: recall@10, MRR, p50 and p95 with confidence intervals, published whether or not the result looks favourable.
2. **Scenario**: Track B on the pinned stratified LitQA2 subset.
   **Expected**: accuracy, precision and abstain rate with intervals, labelled vendor-authored.

### Performance Expectations

Latency is measured, not estimated. The research's figures (search about 9 s on
RDR-090's spike; nx_answer about 80 s at p50) come from repository queries and must
be re-measured on paper questions before they are quoted.

## Finalization Gate

> Complete each item with a written response before
> marking this RDR as **Accepted**. Written responses
> prevent rubber-stamping and produce a review record.

### Contradiction Check

No contradictions between the research findings and the proposed solution. One
discrepancy is resolved: the bead's 66.0% is PaperQA2's accuracy; precision is 85.2%.

### Assumption Verification

Four Critical Assumptions remain unverified. None blocks Track A. Track B is blocked
until assumptions 1 and 3 are verified.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `nx search` | nexus (conexus 7.94.1) | Docs Only; a spike is needed to confirm timing output |
| `nx index pdf` | nexus | Source Search (`src/nexus/commands/index.py:3051`) |
| nx_answer under a fixed plan | nexus | Docs Only; local-mode operation unverified |
| voyage-context-3 pricing | Voyage AI | Docs Only (docs.voyageai.com/docs/pricing, 2026-09-29) |

### Scope Verification

The Minimum Viable Validation is in scope for the first implementation step and is
a condition of accepting Track A.

### Cross-Cutting Concerns

- **Versioning**: conexus and engine versions recorded in every results file.
- **Build tool compatibility**: N/A (a script).
- **Licensing**: CC-BY-SA-4.0 on LitQA2 (share-alike on derived data); Apache-2.0 on CoIR; SciFact and CodeSearchNet to check. Owner decision 1.
- **Deployment model**: N/A; runs from a checkout.
- **Incremental adoption**: Track A can publish without Track B.
- **Secret/credential lifecycle**: nx_answer uses the Claude command-line tool's own login; the harness stores no key.
- **Memory management**: N/A beyond the corpus cache.

### Proportionality

One design and one set of owner decisions. The plan is short because the design is a
harness, not a system change.

## References

- Research: T2 `nexus/2k66t2-eval-design-research-2026-11-03`.
- Bead: `nexus-2k66t.2` (`bd show nexus-2k66t.2`); epic `nexus-2k66t`.
- PaperQA2: arxiv.org/html/2409.13740v2.
- LitQA2 / LAB-Bench dataset card: huggingface.co/datasets/futurehouse/lab-bench.
- CoIR: arxiv.org/abs/2407.02883; github.com/CoIR-team/coir.
- Voyage pricing: docs.voyageai.com/docs/pricing (updated 2026-09-29).
- RDR-090: `docs/rdr/rdr-090-realistic-agenticscholar-benchmark.md:127`; spike summary `scripts/spikes/spike_rdr090_5q_summary.json`.
- Internal bench: `scripts/bench/runner.py`, `scripts/bench/metrics.py`, `bench/queries/golden_v1.yaml`, `bench/queries/golden_heldout_v1.yaml`.
- Indexing: `src/nexus/commands/index.py:3051`. Defaults: `src/nexus/config.py:875`. Pin: `pyproject.toml:14`.
- nx_answer cost telemetry (T2 research): `nexus/rdr232-unread-anchor-withhold-cost-2026-11-03` (9 calls, 8.6972 USD) and `nexus/rdr232-jypmj23-measurement-2026-10-30` (6 calls, 1.8886 USD); pooled mean about 0.71 USD per call, over repository questions.
- Landscape survey: T3 collection `nexus`, `landscape-survey-2026-10-07` (1/4) and (3/4). (2/4) and (4/4) did not resolve by exact title.

## Revision History

- 2026-11-03: Draft created from the design research; tightened to remove repetition and reduce the Alternatives and Failure Modes sections.
- 2026-11-03: Owner decisions recorded: publish, $0, Track A (CoIR, search-only) leads; Track B and RDR-090 N=30 deferred.
