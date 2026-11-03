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
related: [RDR-090, RDR-155, RDR-200, RDR-225, RDR-232]
---

# RDR-244: Public retrieval evaluation others can run

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Terms. **nx** is the Nexus command-line tool. **nx search** is plain vector
search: it ranks document chunks by embedding similarity, with no language model
reading them. A **chunk** is a piece of a document that Nexus stores and
embeds; a long document has several. **nx_answer** gathers chunks from many
documents and has a language model write an answer; it calls the Claude
command-line tool (`claude -p`). **bge-768** is the local embedding model Nexus
uses by default. **Voyage** (voyage-context-3) is a hosted embedding model
Nexus can use instead. **Document-level recall@10** is the share of queries
for which at least one correct document is among the top 10 distinct documents
returned. Chunks from the same document count once. **MRR** (mean reciprocal
rank) averages 1/rank of the first correct document. **NDCG@10** scores the
top 10 documents by rank order, with graded relevance, so a correct document
near the top scores more than one near the bottom. **p50** and **p95** are
median and 95th-percentile latency. **CoIR** is a public benchmark of code
retrieval tasks, with a query set, a corpus and graded relevance judgements
(qrels) per dataset.

## Problem Statement

Nexus does not publish a retrieval number that anyone outside the project can
reproduce. The internal bench has five queries on one collection, and its one
pre-registered pass criterion, set in RDR-090, failed at that size. The plugin
evaluations measure whether a skill triggers (12 of 15), not whether search
returns the right answer (Source: bead nexus-2k66t.2).

### Enumerated gaps to close

#### Gap 1: No code-path retrieval or latency number

The internal bench (`scripts/bench/runner.py`) scores NDCG@3 only. It has no
document-level recall and no latency table. A user asking how good and how fast
search is over code has no published answer. Closed by an NDCG@10, document-level
recall@10, MRR and latency table for `nx search` over a pinned CoIR subset, with
pinned versions.

#### Gap 2: Deferred

The paper-path gap (no answer-quality number for nx_answer on scientific papers)
is out of this RDR's scope by the owner decision of 2026-11-03. Its design and
blockers are in [Deferred: Track B](#deferred-track-b-and-rdr-090-n30).

#### Gap 3: No harness a third party can run

The query sets are not public. `bench/queries/golden_v1.yaml` holds 24
hand-labelled requests over this repository's own RDRs. Nobody outside the
project can reproduce a number. Closed by one script with pinned versions, a
local mode, and a results format, run on a public benchmark whose data anyone
can fetch.

#### Gap 4: Failed internal results are not reported

RDR-090 pre-registered: a path difference of at least 0.05 in NDCG@3 on at least
3 of 5 queries. Two of five passed (`docs/rdr/rdr-090-realistic-agenticscholar-benchmark.md:127`;
`scripts/spikes/spike_rdr090_5q_summary.json`, per-query `deltas`). The
pre-registered bench was then not run at N=30 (Source: T2 `nexus_rdr/090-research-2`
[708]). The cause of the two zero paths is recorded there and is part of the
result (see Background). Closed by publishing the RDR-090 result beside the new
numbers, with its figures unedited and its recorded cause stated beside them.

## Relationship to Prior RDRs

I checked the RDR files in `docs/rdr/` for bench, eval, retrieval, recall and
search by title and by a grep for the measurement terms. The rows below say
which were read.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-090 (`rdr-090-realistic-agenticscholar-benchmark.md`) | Origin | Created the internal bench and the N=5 criterion, on the rationale that N=30 would decide. N=5 already failed and N=30 never ran. This RDR publishes the failure with its recorded cause and does not repeat the bench. |
| RDR-200 (`rdr-200-phase1-prereg.md`) | Precedent (read by name only) | The repository already keeps pre-registration files before a gate run. This RDR uses the same practice. |
| RDR-028 (code search recall, closed) | Unrelated | Read for its title and status. It is about language-registry unification. It holds no recall or latency measurement (a grep for recall@, measure, nDCG and latency finds none). Not a prior result for this RDR. |
| RDR-155 (pgvector T3 consolidation) | Adjacent: recall@10 on approximate search | Reports recall@10 = 1.0 and p95 0.4 to 1.5 ms for an approximate nearest-neighbour index against exact search (line 100). That is index recall, not relevance recall. This RDR defines its recall@10 as document-level relevance recall and says so in the results. |
| RDR-225 (vector tables per embedding model) | Adjacent: recall@10 on approximate search | Reports recall@10 0.90, then 1.00, on `code-filtered-001` at a given `ef_search` (lines 93 to 95). Same distinction as RDR-155. |
| RDR-232 (nx_answer answer-shape eligibility) | Adjacent, closed | Its labelled golden query sets are in `bench/queries/` (`golden_oracle_labels_v1.yaml` and others). Their contents were not read for this RDR. Track A does not use them. |

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

The B and C zeros are not a measure of answer synthesis alone. The same record
(T2 `nexus_rdr/090-research-2`) names two causes. Path B matched a plan saved for
another project (plan #52), which hardcodes that project's collection, and
returned that project's documents for Nexus questions. A plan saved from one
question (plan #67) matched two unrelated questions, so path C returned the same
RDR for both. The spike ran on the Chroma-era substrate; Chroma is no longer a
substrate in any mode (`CLAUDE.md`). So the 0.277 figures are confounded by plan
routing and are not evidence about current nx_answer quality. They are published
with that caveat.

Competitors publish vendor-run answer-quality numbers, which are disputed but
exist. Nexus publishes none (Source: bead nexus-2k66t.2).

### Technical Environment

- conexus 7.94.1 (`pyproject.toml:15`).
- Engine: `REQUIRED_ENGINE_VERSION = (0, 1, 175)` (`src/nexus/engine_version.py:709`).
  The git tag `engine-service-v0.1.176` exists, but the code does not require it,
  so this RDR does not pin 0.1.176. Each results file records the engine version
  the running service reports, and that version must be at least 0.1.175.
- Local default embedding: fastembed bge-768, around `src/nexus/config.py:875`.
- Server-side reranking rides every retrieval call, and the engine picks the model.
  `src/nexus/commands/search_cmd.py` (the comment above the rerank code, around line
  454): "server-side rerank rides the retrieval calls". The model is set by
  `NX_RERANK_MODEL` and defaults to `rerank-2.5` (around line 432). Each results
  file records the reranker in effect.
- nx search needs no API key for local embeddings. nx_answer and aspect extraction
  need the Claude command-line tool (Deferred).

## Research Findings

Research records: T2 `nexus_rdr/244-research-*`. Entries 1 to 7 are from the design
research of 2026-11-03 (landing pages, READMEs, licences and the PaperQA2 paper,
no dataset downloaded). Entries 8 to 10 are from the gate-r1 fix (source reads of
`src/nexus/` and `scripts/bench/`, and the CoIR paper). No dataset was downloaded
and no indexing or search run was done for Track A.

### Investigation

Track A's design needed four facts: how a document reaches `nx search` and what
identity its rows carry (research-8); what CoIR's headline metric and per-dataset
terms are (research-9); what the engine pin is and which prior RDRs measure recall
(research-10); and what the existing bench's path A already measures (research-8,
`scripts/bench/paths.py`). The earlier design research covered PaperQA2, LitQA2
and the telemetry of one nx_answer run; those findings are in the Deferred section.

### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| CoIR (arXiv 2407.02883, github.com/CoIR-team/coir, huggingface.co/CoIR-Retrieval) | Yes: arXiv HTML, repository README, Hugging Face org page (research-9) | Headline metric NDCG@10 (paper, Section 4). Ten datasets, eight tasks, seven domains. Repository licence Apache-2.0 (LICENSE, research-4). Per-dataset licences are the paper's table, not the repository licence: see Key Discoveries. Hugging Face dataset-card licence fields were not read. |
| CodeSearchNet | Yes, paper table and earlier repository read | Paper states MIT, non-commercial research for the CoIR-derived set. Per-function licences are in the CodeSearchNet repository (`_licenses.pkl`), read earlier and not re-read for this revision. |
| nexus store and search code | Yes, source (research-8) | Store identity, search flags, row fields and dedupe as recorded there. |
| engine pin | Yes, source (research-10) | `REQUIRED_ENGINE_VERSION` (0, 1, 175). |
| BEIR SciFact, NFCorpus | Yes, per research | Licence not stated in the beir README; check allenai/scifact. |

### Key Discoveries

- **Verified** (research-8): `nx store put` takes one file or stdin. With stdin it
  needs `--title`, and a note's catalog identity is (collection, title). Quote from
  `put_cmd`: "catalog identity for a note is (collection, title) uniformly". So a
  CoIR document can get a stable identity from its title.
- **Verified** (research-8): `nx index md` takes one file (`dir_okay=False`), so
  identity is the file path, one file per call. No command takes a directory of
  text records as one document set.
- **Verified** (research-8): `nx search` takes `--corpus`, a corpus prefix or a
  full collection name. It has no `--collection` option. A subject is scoped by
  passing its full collection name to `--corpus`.
- **Verified** (research-8): a result row's path comes from `_display_path`, then
  `source_path`, then `file_path` (`scripts/bench/paths.py` lines 42 to 52). In
  `scripts/bench/metrics.py`, `dedupe_by_doc` treats an empty path as one bucket.
  An empty identity therefore merges every such chunk into one document.
- **Verified** (research-8): identical chunk text in one collection collapses to one
  T3 row by design (`CLAUDE.md`, catalog/T3 split). Two CoIR documents with the
  same text share a row.
- **Unverified** (research-8): whether `_display_path` is set for a note stored
  with `store put --title`. The catalog file path is not set by that path, so the
  row may carry an empty path. The spike decides.
- **Unverified** (research-8): the collection token for local mode. The
  `t3_collection_name` docstring shows `voyage-context-3` for a bare subject. The
  rendered name in local mode is not shown and the spike must print it.
- **Documented** (research-9): CoIR's headline metric is NDCG@10. The paper says:
  "Following BEIR ([Thakur et al., 2021]), we use NDCG@10 to evaluate the
  performance of retrieval models." It gives no k for MAP, Recall or Precision and
  does not mention MRR.
- **Documented** (research-9): the paper's table gives these licences per dataset.
  APPS, CosQA and CodeSearchNet: MIT, non-commercial research. Synthetic Text2SQL,
  CodeTransOcean-DL, CodeTransOcean-Contest, CodeFeedback-ST and CodeFeedback-MT:
  Apache 2.0. StackOverflow QA: CC-BY-SA 3.0. CodeSearchNet-CCR: none stated.
  The repository's Apache-2.0 licence does not license the data. The earlier
  statement that CoIR's data is Apache-2.0 is withdrawn.
- **Unverified** (research-9): the Hugging Face dataset-card licence fields for the
  ten repositories. The org page fetch showed none. Each card must be read before
  a dataset is used.
- **Unverified** (research-9): the corpus sizes (the paper's statistics are an
  image), and the id fields in corpus and qrels files (the CoIR page shows a corpus
  entry with a `text` field and names no id field).
- **Verified** (research-10): `REQUIRED_ENGINE_VERSION` is (0, 1, 175). The tag
  v0.1.176 is not what the code requires.
- **Verified** (research-10): `elapsed_s` in `scripts/bench/paths.py` (lines 102 to
  108) is wall-clock time around a subprocess call to `nx search`. It includes the
  start-up of the nx command, not only search time.

### Critical Assumptions

- [ ] Each CoIR dataset in the subset carries a licence that allows publishing a derived manifest and results. **Status**: Unverified (paper table read; Hugging Face cards not read). **Method**: Docs Only (each dataset card, and the paper's table).
- [ ] A search result row carries the document's title or its catalog path, so that a hit maps to a CoIR document id. **Status**: Unverified. **Method**: Spike (one dataset, twenty documents, `nx search --json`).
- [ ] The local-mode collection name renders from the subject, and `--corpus <full collection name>` scopes a search to that collection alone. **Status**: Unverified. **Method**: Spike.
- [ ] The subset's chunks index within the run-time cap at $0 with local bge-768. **Status**: Unverified. **Method**: Spike (per-document index time and chunk counts).
- [x] Track A runs at no API cost for search and storage with local embeddings. **Status**: Documented (research-1 to -7; configuration). Not run. **Method**: Docs Only; the spike confirms it.

## Proposed Solution

### Approach

Two tracks were proposed on 2026-11-03. Only Track A is in this RDR. Track B is in
the Deferred section and is not part of the plan, tests or validation below.

**Track A, code path (no language model).** Score `nx search` over a pinned subset
of CoIR datasets, with bge-768 embeddings run locally. Report NDCG@10 (the CoIR
headline metric), document-level recall@10, MRR, and latency as p50 and p95. No
API key. The data licence is per dataset (Critical Assumption 1), not Apache-2.0.

**RDR-090 alongside.** The published page carries the N=5 result, its failed
criterion and its recorded cause, beside the Track A table.

### Technical Design

Interfaces, then the Track A design. Each design point below either cites a
source read or is marked as new work.

- Input: a query-set file (dataset, query id, text, graded qrels, licence tag);
  a corpus manifest (dataset, Hugging Face revision, document id, checksum); a
  pinned version record.
- Output: `results.json` with track, query-set hash, versions and pins, per-query
  ranked document ids with grades, and summary statistics with sample sizes and
  intervals.

**Track A ingestion (new work).** No Nexus command loads a CoIR dataset. The
harness adds a loader that, for each corpus document in the subset, runs one
`nx store put - --title <key> --collection <subject>` with the document text on
stdin, where `<key>` is `<dataset>:<coir-document-id>`. The title is the identity
(research-8). The subject is `code-retrieval`: a durable topic, not a dataset or a
run (`docs/collections.md` Rule 1). Before the first write, the loader runs
`nx collection list` and stops if a `code-retrieval` collection already holds other
content, since search would fan across it (Rule 2). The collection is written only
by the loader. One call per document is a known cost: the spike measures it. If
the measured cost is too high for the subset, a bulk write path is new work, and
this RDR does not design it.

**Track A collection name.** The name is what the resolver renders from the
subject (`t3_collection_name`). The results file records the full rendered name,
and the spike prints it for local mode (Critical Assumption 3). The search call
uses that full name as `--corpus`, so no other collection is searched.

**Track A query run.** For each query in the subset, call
`nx search "<query text>" --corpus <full name> -m 100 --json`, with no `--hybrid`,
`--lexical` or `--no-rerank` flag, and record the ordered rows. `-m 100` is fixed
by this design, so enough chunks return to fill ten distinct documents when a
document has several chunks. The `-m` value and the flags are part of the pin.

**Track A identity and scoring.** Each row's key is its document key from the
title, or from `_display_path` if the title is not returned (Critical
Assumption 2). A row with an empty key is an error: the run fails and reports
the count. It is not bucketed (`dedupe_by_doc` would merge them).

- Chunks to documents: keep the first row for each key in rank order. This is the
  deduplication to document level. Several chunks of one document count once.
- K=10: the ranked list is the first ten distinct keys in rank order. If fewer than
  ten distinct keys return, the query is scored on those, and the count is
  reported with the query.
- Identical text: identical chunk text in one collection is one T3 row (`CLAUDE.md`).
  The loader counts duplicate texts per dataset in the manifest. A shared row
  credits the first document in sorted key order, and the count is in the results
  file. Each document's grade is its own qrels grade.
- Metrics: NDCG@10 with graded qrels (the CoIR headline). Document-level recall@10:
  the share of queries with at least one relevant document in the top ten. MRR:
  the mean of 1/rank of the first relevant document, with 0 when none is in the
  returned list. `scripts/bench/metrics.py` has `ndcg_at_k(grades, gt, k)`, so NDCG
  at k=10 reuses it if its gain formula matches CoIR's (unverified; the spike
  compares against a hand-computed value).

**Track A subset rule.** The eligible pool is the five datasets that the paper
lists as Apache 2.0: Synthetic Text2SQL, CodeTransOcean-DL, CodeTransOcean-Contest,
CodeFeedback-ST and CodeFeedback-MT. A dataset is dropped from the pool if its
Hugging Face card does not show Apache 2.0. The subset is the three smallest
datasets in that pool by corpus size, measured in the spike. The three are chosen
by the rule, not by their results. The non-commercial datasets (APPS, CosQA,
CodeSearchNet) are out of the default subset. Adding one is an owner decision,
because it changes what the public data licence statement can say.

Queries: per dataset, draw up to 50 queries with `random.Random(seed)` over the
sorted query ids, seed 2026. If a dataset has fewer than 50, all are used. The
query set file records its hash.

**Track A latency.** Latency is the wall-clock time of one `nx search` subprocess,
measured by the harness around the call, after one discarded warm-up query against
the same collection. So this is warm, client-side, end to end: it includes the nx
command's start-up. Engine-only time is reported only if the command prints it,
which is unverified. A cold-start figure is not reported unless it is measured
separately. p50 and p95 are taken over all query calls in the run.

**Statistics.** Each reported number has a 95% bootstrap interval over queries:
10,000 resamples, percentile method, seed fixed.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Track A search call | `scripts/bench/runner.py` path A (`run_path_a`) | Reuse the subprocess pattern only. Path A requests `-m K*3` with K=3 and takes the first K documents, so it cannot return ten documents for K=10. New call with `-m 100` and the K=10 rule. |
| Document identity | `scripts/bench/paths.py` `_chunk_path` | Reuse the field order (`_display_path`, `source_path`, `file_path`). Add the title key and the empty-key error. |
| Recall@10, MRR and NDCG@10 scorer | `scripts/bench/metrics.py` (NDCG at k=3 for RDR-090) | Extend: keep NDCG@3 for RDR-090, add document-level recall@10 and MRR, and call NDCG at k=10. Its gain formula is checked against CoIR in the spike. |
| Corpus load | none for CoIR | New loader: one `store put` per document with a title key. Per-call cost measured in the spike. |
| Timing | `scripts/bench/paths.py` `elapsed_s` (per call, wall-clock) | Reuse the timer. Define it as in Technical Design (warm, client-side) and record it per call. |
| Results format | none | New. |

### Decision Rationale

Track A uses a public code benchmark because it costs nothing, needs no language
model, and tests the code search path that most Nexus users run. The internal bench
cannot serve as the public result: its queries are private, and five queries cannot
separate the paths. Track A's data licence is per dataset, and the subset rule above
keeps the default subset to datasets the paper lists as Apache 2.0.

### Alternatives Considered

- **BEIR SciFact and NFCorpus.** Pros: free, retrieval-only, 300 and 323 queries, widely reported. Cons: licence not stated in the BEIR README. Rejection reason: not rejected; deferred as a second IR check for a later RDR.
- **CodeSearchNet (full, or as a CoIR subset).** Pros: about 2M code pairs and a 99-query human set. Cons: the CoIR paper lists its CodeSearchNet data as MIT, non-commercial research, and the CodeSearchNet repository lists per-function licences (`_licenses.pkl`). Rejection reason: a publishable derived set would need the non-commercial terms resolved first. It is out of the default subset by the subset rule; it is not rejected for good.
- **LoCoMo, LongMemEval.** Pros: memory benchmarks with published comparisons. Cons: memory-system vendors dispute the published numbers. Rejection reason: excluded by the bead (nexus-2k66t.2).
- **Full LitQA2 and the nx_answer track.** Moved to Deferred with Track B.

## Decisions for the Project Owner

**Decided 2026-11-03** (Sam; relayed by linda and recorded on nexus-2k66t.7):

- Publish: yes.
- Spend: $0 for now.
- Track A leads, and is the only track built now.
- The harness is search-only: no nx_answer, no Claude command-line tool.
- Track B and RDR-090's N=30 are deferred until LitQA2's blockers (open-access status, 248 versus 199 rows, option count) are answered; reopening them is a new owner decision.

**Open after the gate-r1 fix** (new; this RDR does not decide them):

1. **Licence result on publishing.** The paper lists the default subset as Apache 2.0, but the Hugging Face cards are unread. If a card contradicts the paper, that dataset drops out by the subset rule, and the publish decision stands for the rest. If the owner wants a non-commercial dataset in the public subset, that is a separate decision.
2. **Shared corpus or per-dataset corpus.** The design loads the subset into one collection, so one search can return documents from any subset dataset. CoIR's own protocol searches each dataset's corpus alone. The shared design is harder than CoIR's protocol, so its numbers are not comparable to CoIR's published numbers. The per-dataset alternative needs one collection per dataset, which the collection rules (Rule 1) do not allow for a dataset name. The design takes the shared option until the owner decides.

## Trade-offs

### Consequences

- A public number for code search over a CoIR subset, with its caveats printed beside it. A weak number is published too, and that is the point.
- The published numbers are not CoIR's published numbers (shared corpus, subset, warm latency), and the results page says so.
- The harness is a maintained artefact. Pins go stale when conexus or the engine changes, so each results file records its versions.

### Risks and Mitigations

- **Risk**: a dataset's licence card contradicts the paper. **Mitigation**: the card is read before the dataset enters the subset (Critical Assumption 1).
- **Risk**: the sample is too small to separate settings. **Mitigation**: report intervals with every number; claim no difference the intervals do not support.
- **Risk**: the RDR-090 result is read as a judgement on answer synthesis. **Mitigation**: publish the recorded causes beside it (plan leakage, plan-library pollution, Q2 vocabulary, Chroma-era substrate).
- **Risk**: the plan library routes a query to a stored plan, as in RDR-090. **Mitigation**: Track A calls `nx search` only, which does not route through the plan library.

### Failure Modes

Only the modes that change a decision:

- **Empty document identity merged into one document.** A row with no key would be bucketed by `dedupe_by_doc` and score as one document. The run fails and reports the count instead.
- **Fewer than ten documents returned.** A query whose returned rows hold fewer than ten distinct documents is scored on those, and the count is reported. It is never padded.
- **Duplicate chunk text.** Two documents share one row. The first in sorted key order is credited, and the count is in the results file.
- **Query set changes between runs.** The results file records a query-set hash, so a change shows as a different hash and the run is labelled as a different set.

## Implementation Plan

### Prerequisites

- [x] Owner decisions 1 to 5 recorded (2026-11-03, nexus-2k66t.7).
- [ ] Each subset dataset's Hugging Face card read for its licence (Critical Assumption 1).
- [ ] Pre-registration file committed before the first run: subset, seed, query count, scoring rule, interval method, and the decision the owner will take from the result (see Validation).

### Minimum Viable Validation

Track A on the subset, end to end, on a few documents first: the loader writes
documents with title keys, `nx search` returns rows whose keys match, the scorer
computes NDCG@10, recall@10 and MRR, `results.json` is written, and a second run
from the same pins reproduces the ranked key lists and metric values exactly (see
Test Plan). Latency is recorded and not compared. The spike that settles Critical
Assumptions 2 to 4 is the first step, and it is a condition of starting Step 1.

### Phase 1: Code Implementation

#### Step 0: Spike (Track A unknowns)

For one dataset, twenty documents: write them with title keys, run
`nx search --json` on five queries, and record: the rendered collection name, the
key returned for each row, the chunk count per document, and the per-document write
time. For the subset: the corpus sizes and the duplicate-text count. Results go to a
T2 research entry, and the step's outcome decides whether Step 1 needs the bulk
write path (new work).

#### Step 1: Track A harness

Add the loader, the scorer (document-level recall@10, MRR, NDCG@10 at k=10) beside
`scripts/bench/metrics.py`, the K=10 dedupe with the empty-key error, the timing
definition above, and the `results.json` writer.

#### Step 2: Track A run

Run the subset with pinned versions, and record the numbers and their intervals.

### Phase 2: Operational Activation

#### Activation Step 1: Publish

Publish the harness, the query-set manifest, the dataset manifest with the licence
line for each dataset, and each `results.json`, with the RDR-090 result beside them.
Waits for the licence check (Open decision 1) and for the first run's results.

### Day 2 Operations

The only persistent output is result files. The corpus is loaded into the `code-retrieval`
collection, which is a Nexus collection, and is re-loadable from the dataset
revision recorded in the manifest.

### New Dependencies

- CoIR datasets (Hugging Face, `CoIR-Retrieval`); per-dataset licences as above.
- No new runtime dependency for the harness.

## Test Plan

- **Scenario**: Track A scorer on a hand-computed three-query example — **Verify**: NDCG@10, document-level recall@10 and MRR match the hand values exactly.
- **Scenario**: K=10 dedupe — a result list of 30 chunks from 4 documents — **Verify**: four documents scored, count reported, no padding.
- **Scenario**: empty document key in a result row — **Verify**: the run fails and reports the count; no bucket.
- **Scenario**: Track A run twice with the same pins and the same loaded collection — **Verify**: identical ranked key lists and identical NDCG@10, recall@10 and MRR values. Latency is excluded from equality and reported as a distribution.
- **Scenario**: changed query-set file — **Verify**: the hash differs and the run is labelled as a different set.
- **Scenario**: a subset dataset with no recorded licence — **Verify**: the run refuses to start and names the dataset.

## Validation

### Testing Strategy

Pre-register before any run: the subset (by the rule above, with the measured sizes),
the query sample (seed 2026, 50 per dataset), the scoring rule (NDCG@10 with graded
qrels, document-level recall@10, MRR), the interval method (95%, 10,000 resamples,
percentile), and the decision the owner will take from the result. Commit that file
before the first run and do not change it afterwards. RDR-090 set its criterion before
its run and then reported the failure; this RDR does the same whichever way the
result falls.

1. **Scenario**: Track A on the pinned CoIR subset.
   **Expected**: NDCG@10, document-level recall@10, MRR, and latency p50 and p95, each with an interval, published whether or not the result looks favourable, with the shared-corpus and warm-latency caveats printed beside them.

### Performance Expectations

Latency is measured, not estimated. The research figure (search about 9 s per call
on RDR-090's spike, Path A) includes a different harness and a different corpus, and
it is not quoted as a result. The Track A latency is measured on the subset, by the
definition in Technical Design.

## Finalization Gate

> Complete each item with a written response before
> marking this RDR as **Accepted**. Written responses
> prevent rubber-stamping and produce a review record.

### Contradiction Check

Three earlier statements are withdrawn or corrected. The data licence is per dataset
from the paper's table (research-9), not Apache-2.0. The headline metric is NDCG@10
(research-9), not recall alone. The engine pin is `REQUIRED_ENGINE_VERSION` (0, 1,
175), not tag v0.1.176 (research-10).

### Assumption Verification

Four Track A Critical Assumptions remain unverified: the dataset licences (1), the
search row identity (2), the local collection name and scoping (3), and the index
cost (4). Assumption 1 gates publication of the subset. Assumptions 2 to 4 gate the
first run. None of them is verified by a design; each is closed by the Step 0 spike
or by a card read.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `nx store put <file or -> --title --collection` | nexus | Source Search (`src/nexus/commands/store.py`, `put_cmd`; research-8) |
| `nx search --corpus <full name> -m N --json` | nexus | Source Search for flags and row fields (`search_cmd.py`; research-8). Row identity: Spike (Critical Assumption 2). |
| `t3_collection_name` (local mode token) | nexus | Source Search (docstring shows the voyage token); Spike for local mode (Critical Assumption 3) |
| `nx index md` | nexus | Source Search (`index.py`, one file per call); not used by Track A |
| voyage-context-3 pricing | Voyage AI | Not used by Track A (Deferred) |

### Scope Verification

The Minimum Viable Validation is in scope for the first implementation step and is
a condition of accepting Track A.

### Cross-Cutting Concerns

- **Versioning**: conexus version, engine version as the running service reports it (at least `REQUIRED_ENGINE_VERSION`), reranker, embedding profile, collection full name, `-m` value and flags, CoIR dataset revisions, all recorded in every results file.
- **Build tool compatibility**: N/A (a script).
- **Licensing**: per dataset, from the paper's table and the card read (Critical Assumption 1). Repository licence Apache-2.0 does not apply to the data. Owner decision 1 (open).
- **Deployment model**: N/A; runs from a checkout.
- **Incremental adoption**: N/A (Track A is the whole RDR).
- **Secret/credential lifecycle**: none. Track A uses no key.
- **Memory management**: N/A beyond the subset collection.

### Proportionality

One design and one set of owner decisions. The plan is short because the design is a
harness, not a system change.

## Deferred: Track B and RDR-090 N=30

Not in this RDR's plan, tests or validation. The owner decision of 2026-11-03
defers it; reopening it is a new owner decision. The design is kept so the decision
can be taken with its facts.

**Gap 2 (deferred).** Nexus has no measured accuracy for answering questions from
scientific papers. The only published figures for this task come from the
benchmark's own authors.

**Design.** A stratified LitQA2 subset of about 50 questions run through nx_answer
under a fixed plan, with accuracy, precision and abstain rate reported, as PaperQA2
does. The results page labels the benchmark vendor-authored. Mapping: the answer is
mapped to a letter or to "abstain"; an nx_answer run that hits the 300 s limit is
recorded as an error and left out of the accuracy denominator, with the count reported
(scoring it as abstention would inflate the abstain rate).

**Terms (for this section).** **LitQA2** is a set of multiple-choice questions about
recent scientific papers, from the LAB-Bench suite. **Abstain** means answering
"insufficient information" instead of guessing.

**Published figures for the comparison (documented).** PaperQA2's accuracy is 66.0%,
precision (correct over answered) is 85.2% ±1.1 (n=3), and insufficient-information
answers are 21.9% ±0.9 (arxiv.org/html/2409.13740v2). The bead's "precision vs
accuracy" wording mixes the two. The paper's human baseline (n=9) has precision 73.8
±9.6 and accuracy 67.7 ±11.9, with no significant difference from PaperQA2 (p=0.66).
The paper is vendor-written.

**Blockers, recorded for the decision.**

- LitQA2 licence is CC-BY-SA-4.0 (dataset card; LICENSE text not read). Whether a derived question set is an adaptation is a licence reading the owner should confirm. The card's 199 rows and the paper's 248 questions are not reconciled. The option count is not stated in the pages read. Whether the dataset's DOIs are open access is unverified (Docs Only). LitQA2 names no corpus, and PaperQA2 works only from files the user holds.
- Cost (research, from live telemetry of repository questions): about 0.71 USD per nx_answer call pooled (T2 `nexus/rdr232-unread-anchor-withhold-cost-2026-11-03`, 9 calls, 8.6972 USD, and `nexus/rdr232-jypmj23-measurement-2026-10-30`, 6 calls, 1.8886 USD); about 80 s per question at p50. Paper questions are not measured. A 50-question subset is about $35; all 248 about $176.
- Voyage: docs state no free tier for voyage-context-3 at $0.18 per million tokens (docs.voyageai.com/docs/pricing, updated 2026-09-29). A search summary said 200M free tokens; the pricing page overrides it (research-7). Track B's indexing with Voyage would be about $0.45; local embeddings cost $0.
- nx_answer needs the Claude command-line tool (`claude -p`), so Track B needs a Claude login; a search-only harness does not.

**Deferred work.** The fixed-plan nx_answer harness (the letter mapping, abstain and
timeout handling); the Track B scenarios (mapping on fixed answers, including an
"insufficient information" answer on a null-ideal question; the nx_answer timeout);
the Track B validation scenario (accuracy, precision and abstain rate with
intervals, labelled vendor-authored); and RDR-090's N=30 run, specified and never
run. Track B's audit row is also deferred: it would reuse `scripts/bench/runner.py`
path B with a fixed plan, which the current runner does not provide.

**Track B Critical Assumptions (deferred).** The LitQA2 subset can be used under
CC-BY-SA without redistributing paper text (Unverified; Docs Only). The 199-row and
248-question versions give the same stratified sample (Unverified; Docs Only).
Open-access full text can be obtained legally for the sampled papers (Unverified;
Docs Only). nx_answer runs with local embeddings and no Voyage key (Unverified;
Spike).

## References

- Research: T2 `nexus_rdr/244-research-1` to `244-research-10`; design research T2 `nexus/2k66t2-eval-design-research-2026-11-03`.
- Bead: `nexus-2k66t.2` (`bd show nexus-2k66t.2`); epic `nexus-2k66t`.
- CoIR: arxiv.org/abs/2407.02883 (HTML: arxiv.org/html/2407.02883); github.com/CoIR-team/coir; huggingface.co/CoIR-Retrieval.
- RDR-090: `docs/rdr/rdr-090-realistic-agenticscholar-benchmark.md:127`; spike summary `scripts/spikes/spike_rdr090_5q_summary.json`; T2 `nexus_rdr/090-research-2` (plan leakage, plan-library pollution, Q2 vocabulary).
- RDR-155 line 100 and RDR-225 lines 93 to 96 (approximate-search recall@10); RDR-028 (unrelated; closed).
- Internal bench: `scripts/bench/runner.py`, `scripts/bench/paths.py` (`run_path_a`, `elapsed_s`), `scripts/bench/metrics.py` (`dedupe_by_doc`, `ndcg_at_k`), `bench/queries/golden_v1.yaml`.
- Store and search: `src/nexus/commands/store.py` (`put_cmd`); `src/nexus/commands/search_cmd.py` (`--corpus`, `-m`, rerank); `src/nexus/corpus.py` (`t3_collection_name`); `docs/collections.md`; `docs/cli-reference.md`.
- Engine pin: `src/nexus/engine_version.py:709`; `pyproject.toml:15`.
- Deferred: LitQA2 / LAB-Bench dataset card, huggingface.co/datasets/futurehouse/lab-bench; PaperQA2, arxiv.org/html/2409.13740v2; Voyage pricing, docs.voyageai.com/docs/pricing.

## Revision History

- 2026-11-03: Draft created from the design research; tightened to remove repetition and reduce the Alternatives and Failure Modes sections.
- 2026-11-03: Owner decisions recorded: publish, $0, Track A (CoIR, search-only) leads; Track B and RDR-090 N=30 deferred.
- 2026-11-03: Gate r1 fixes (critique T2 `nexus_rdr/244-gate-critique-2026-11-03-r1`): Track A ingestion, identity, K=10, subset and pin design added (C1); Track B, its assumptions and its validation moved to Deferred (S1); RDR-090 result carries its recorded causes (S2); CoIR metric corrected to NDCG@10 and data licences made per dataset, CodeSearchNet rejection restated (S3); RDR-028 marked unrelated, RDR-155 and RDR-225 added as adjacent recall references (S4); engine pin corrected to 0.1.175, latency defined, pins and reproducibility test restated (S5). Research `nexus_rdr/244-research-8` to `-10`.
