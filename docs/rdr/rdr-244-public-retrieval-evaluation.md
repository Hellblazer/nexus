---
title: "Public retrieval evaluation others can run"
id: RDR-244
type: Feature
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-11-03
accepted_date: 2026-11-03
related_issues: ["nexus-2k66t.2", "nexus-2k66t"]
related: [RDR-090, RDR-155, RDR-200, RDR-225, RDR-232]
---

# RDR-244: Public retrieval evaluation others can run

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Terms. **nx** is the Nexus command-line tool. **nx search** is vector retrieval:
it ranks document chunks by embedding similarity, then applies ranking boosts
(see Technical Environment). No language model reads the chunks. A **chunk** is a piece of a document that Nexus stores and
embeds; a long document has several. **nx_answer** gathers chunks from many
documents and has a language model write an answer; it calls the Claude
command-line tool (`claude -p`). **bge-768** is the local embedding model Nexus
uses by default. **Voyage** (voyage-context-3) is a hosted embedding model
Nexus can use instead. **Document-level recall@10** is the share of queries
for which at least one correct document is among the top 10 distinct documents
returned. Chunks from the same document count once. **MRR** (mean reciprocal
rank) averages 1/rank of the first correct document. **NDCG@10** scores the
top 10 documents by rank order, using the relevance scores in the qrels file, so
a correct document near the top scores more than one near the bottom. **p50** and **p95** are
median and 95th-percentile latency. **CoIR** is a public benchmark of code
retrieval tasks, with a query set, a corpus and relevance judgements (qrels)
per dataset.

## Problem Statement

Nexus does not publish a retrieval number that anyone outside the project can
reproduce. The internal bench has five queries on one collection, and its one
pre-registered pass criterion, set in RDR-090, failed at that size. The plugin
evaluations measure whether a skill triggers (12 of 15), not whether search
returns the right answer (Source: bead nexus-2k66t.2).

### Enumerated gaps to close

#### Gap 1: No retrieval or latency number for vector search over code text

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
- Reranking: `src/nexus/commands/search_cmd.py` (lines 454 to 468) runs the
  server-side rerank only when a request has more than one target collection, or
  `--lexical`. Track A passes one collection and no `--lexical`, so no reranker
  runs, and the harness refuses any row that carries a rerank score. If a reranker runs, its model is
  `NX_RERANK_MODEL`, which defaults to `rerank-2.5` (lines 432 to 437).
- Ranking boosts: `apply_ranking_boosts` (`src/nexus/search_engine.py:2635`)
  applies a frecency blend when `--hybrid` or `search.hybrid_default` is on
  (`src/nexus/config.py:1407`, default False), then a bibliographic quality boost.
  `apply_topic_boost` runs only when taxonomy assignments exist for the results
  (`search_engine.py:2593` to 2607). The Track A design pins `hybrid_default` to
  false and writes no taxonomy assignments for the bench documents.
- nx search needs no API key for local embeddings. nx_answer and aspect extraction
  need the Claude command-line tool (Deferred).

## Research Findings

Research records: T2 `nexus_rdr/244-research-*`. Entries 1 to 7 are from the design
research of 2026-11-03 (landing pages, READMEs, licences and the PaperQA2 paper,
no dataset downloaded). Entries 8 to 10 are from the gate-r1 fix (source reads of
`src/nexus/` and `scripts/bench/`, and the CoIR paper). Entries 11 to 14 are from
the gate-r2 fix (source reads at d053ca061, and the Hugging Face dataset pages).
Entries 15 to 17 are Step 0 and Step 1 (2026-11-06): the pinned datasets downloaded
and read, the engine-side spike, and the harness findings.

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
| CoIR (arXiv 2407.02883, github.com/CoIR-team/coir, huggingface.co/CoIR-Retrieval) | Yes: arXiv HTML, repository README, Hugging Face org page and five dataset pages (research-9, -12) | Headline metric NDCG@10 (paper, Section 4). Ten datasets, eight tasks, seven domains. Repository licence Apache-2.0 (LICENSE, research-4). Per-dataset licences are the paper's table, not the repository licence. The dataset pages show no licence field. |
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
  An empty identity therefore merges every such chunk into one document. The design
  does not use the row path for identity. It takes each document key from the chunk
  hash through `docs_for_chashes`, as Track A identity and scoring sets out.
- **Verified** (research-8): identical chunk text in one collection collapses to one
  T3 row by design (`CLAUDE.md`, catalog/T3 split). Two CoIR documents with the
  same text share a row.
- **Moot** (research-8): whether `_display_path` is set for a note stored with
  `store put --title`. Identity no longer comes from the row path, so the answer
  decides nothing in the design.
- **Verified** (research-16): the collection token for local mode. The
  `t3_collection_name` docstring shows `voyage-context-3` for a bare subject; in
  local mode the name renders as `knowledge__<subject>__bge-base-en-v15-768__v1`.
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
- **Documented** (research-12): the Hugging Face dataset pages show no licence
  field in the extraction. The licence source is therefore the paper's table. A
  card-level check stays a Step 0 item.
- **Documented** (research-12): corpus sizes as the viewer shows them: codetrans-dl
  816; codefeedback-mt 66.4k; codefeedback-st 157k; synthetic-text2sql 106k.
  codetrans-contest's corpus was not read. The corpus id field is `_id`.
- **Verified** (research-11): a `knowledge__*` document written through
  `store put` queues a Claude-CLI aspect extraction. The autostart variable
  `NX_ASPECT_WORKER_AUTOSTART` gates only the local worker's start, not the queue
  row. No per-collection opt-out exists. The per-call hook registry of
  `fire_note_chains` does allow one.
- **Verified** (research-13): a one-collection search with no flags runs no
  reranker. Topic boost applies only where taxonomy assignments exist.
- **Verified** (research-14): a shared chunk row carries one writer's
  `source_path`. One chunk row maps to several catalog documents
  (`docs_for_chashes`). The qrels sample shows score 1 only; "graded relevance"
  has no source and is dropped from the terms.
- **Verified** (research-10): `REQUIRED_ENGINE_VERSION` is (0, 1, 175). The tag
  v0.1.176 is not what the code requires.
- **Verified** (research-10): `elapsed_s` in `scripts/bench/paths.py` (lines 102 to
  108) is wall-clock time around a subprocess call to `nx search`. It includes the
  start-up of the nx command, not only search time.

### Critical Assumptions

- [x] Each CoIR dataset in the subset carries a licence that allows publishing a derived manifest and results. **Status**: Verified for the subset rule (Sam, 2026-11-06: "upstream licence is fine"). The basis is the paper's licence table and the upstream sources, which state Apache-2.0; the Hugging Face cards state no licence at all (research-15). The check ran in Step 0, before the pre-registration commit. **Method**: Docs Only.
- [x] A search result row's chunk hash maps, through `docs_for_chashes`, to the loaded documents and their `<dataset>:<id>` titles, so that a hit maps to a CoIR document id. **Status**: Verified (Step 0, research-16: every row mapped, none out of collection; the harness's engine test maps every row). **Method**: Spike.
- [x] The local-mode collection name renders from the subject, and `--corpus <full collection name>` scopes a search to that collection alone. **Status**: Verified (Step 0, research-16: the name renders as `knowledge__<subject>__bge-base-en-v15-768__v1`; the harness refuses one that does not carry the token). **Method**: Spike.
- [x] The subset's chunks index within the run-time cap at $0 with local bge-768. **Status**: Verified (Step 0, research-16: mean write time 0.03 to 0.18 s per document by dataset, maximum 1.10 s; at the highest mean, about 15 minutes for 5,000 against the 30-minute cap, which the run enforces). **Method**: Spike.
- [x] Track A runs at no API cost: the bench loader's writes enqueue no aspect row and start no Claude-CLI process. **Status**: Verified (research-16 and -17: on a fresh tenant the loader leaves every `aspects` tuple count at zero and starts no `claude` process, while the same put with the default hooks queues one task). **Method**: Spike, and the harness's engine test.
- [x] With no taxonomy assignments for the bench documents and no reranker, no ranking boost reorders the bench results. **Status**: Verified (research-17: `frecency_score` is present on every row but inert, because the non-hybrid score is the normalised vector distance alone; the quality boost needs `bib_citation_count`, which bench notes lack). The harness refuses a row that carries a rerank, bibliographic or boost signal, and any taxonomy assignment on a bench chunk. **Method**: Spike.

## Proposed Solution

### Approach

Two tracks were proposed on 2026-11-03. Only Track A is in this RDR. Track B is in
the Deferred section and is not part of the plan, tests or validation below.

**Track A, code path (no language model).** Score `nx search` over a pinned subset
of CoIR datasets, with bge-768 embeddings run locally. Report NDCG@10 (the CoIR
headline metric), document-level recall@10, MRR, and latency as p50 and p95. No
API key, and no aspect extraction (Critical Assumption 5). The data licence is per
dataset (Critical Assumption 1), not Apache-2.0.

**RDR-090 alongside.** The published page carries the N=5 result, its failed
criterion and its recorded cause, beside the Track A table.

### Technical Design

Interfaces, then the Track A design. Each design point below either cites a
source read or is marked as new work.

- Input: a query-set file (dataset, query id, text, qrels scores, licence tag);
  a corpus manifest (dataset, Hugging Face revision, document id, checksum); a
  pinned version record.
- Output: `results.json` with track, one query-set hash per dataset, versions and pins, per-query
  ranked document keys with their qrels scores, and summary statistics with sample
  sizes and intervals.

**Track A ingestion (new work).** No Nexus command loads a CoIR dataset, and the
loader does not use `nx store put`: its hook chain queues a Claude-CLI aspect
extraction for every `knowledge__*` document (research-11). The loader calls
`put_note` (`src/nexus/catalog/note_write.py:634`) with the document text, the
collection and the title `<dataset>:<coir-document-id>`. It then calls
`fire_note_chains(outcome, content, hooks=HookRegistry())`
(`note_write.py:1022`) with an empty registry. A fresh `HookRegistry` has no hooks
until `install_default_hooks` is called (`hook_registry.py:54` to 66, 526 to 556),
and the loader does not call it. So no aspect row, no taxonomy assignment and no
document hook runs for the bench documents. The loader then calls `stamp_note`, as
`nx store put` does after its chains (`src/nexus/commands/store.py:233`), so the
document's indexing fence is lifted. The title is the identity (research-8).
A `put_note` status other than stored stops the run.

Each dataset loads into its own collection (Decisions, 2026-11-03), under its own
`docs/collections.md` Rule 1 subject: `deep-learning-code-translation`,
`contest-code-translation`, `single-turn-code-feedback`, `multi-turn-code-feedback`
and `text-to-sql`. Each names the subject area the dataset's documents are about, and
no two datasets share one.

The run is isolated from every user store. The harness boots its own engine on
loopback (`tests._engine_substrate.ensure_engine`), mints a tenant for the run
(`mint_test_tenant`) and points nx at it, so the collections join no user's fan-out
or `docs_for_chashes` lookup. It refuses an engine that is not on loopback. Before
the first write to a dataset's collection it refuses a collection that already holds
content, which is new loader behaviour. The loader is the only writer. Nothing is
torn down: the engine and its tenant are discarded after the run, and the Step 0
spike ran on its own engine, so no spike document reaches a run.

A document whose text is empty, whitespace only, or over 16,384 UTF-8 bytes cannot be stored
(`put_note` refuses over `QUOTAS.MAX_DOCUMENT_BYTES`, `store_hook.py:620`). It is
excluded, and so is every sampled query whose relevant document it is; excluded
queries are not replaced, and both counts are in `results.json`.

**Track A collection name.** The name is what the resolver renders from the
subject (`t3_collection_name`). The results file records the full rendered name,
and it must carry the local model token `bge-base-en-v15-768`; the harness also
reads the engine's `GET /version` and refuses one whose `embedding_mode` is not
`onnx-local`, whose `embedding_profile` is not `bge`, or whose release is below
0.1.175. Each query's search uses its dataset's full name as `--corpus`, so no other
collection is searched.

**Track A query run.** For each query in the subset, call
`nx search --corpus <its dataset's full name> -m 100 --json -- "<query text>"`, with no `--hybrid`,
`--lexical` or `--no-rerank` flag, and record the ordered rows. The `--` keeps a
query that starts with a dash from being read as an option. The query text is passed
whole: the harness neither cuts it nor checks its length (`QUOTAS.MAX_QUERY_STRING_CHARS`
is not enforced, research-17). How the embedder treats a query past its token window
is not measured; a search that fails stops the run. `-m 100` is fixed
by this design, so enough chunks return to fill ten distinct documents when a
document has several chunks. The `-m` value and the flags are part of the pin.
No distance threshold applies to a bge collection
(`search_engine._threshold_for_collection_model`).

The run pins its settings in one record, written to `results.json`: the git sha and
whether the tree was clean, Python, platform and pyarrow versions, the release,
version, embedding mode and profile from the engine's `/version` answer, the model, `-m`, the flags, `search.hybrid_default`, the
threshold, the retrieval unit and the rank-order rule. Per-row boost fields are not
written: a row carrying a rerank, bibliographic or boost signal stops the run instead,
so every scored row carries none. `main` points the run at a fresh config directory,
which is the controlled config, so `search.hybrid_default` is false there by default;
the refusal guards a run started any other way. The run refuses to start
when `search.hybrid_default` is true, when `NX_VOYAGE_API_KEY` is set, or when the
checkout has uncommitted changes. A search that exits non-zero, prints one of nx's
own `Warning:` or `note:` lines, returns no rows, or runs past 300 s stops the run.

**Track A identity and scoring.** Each row's document keys come from its chunk hash
through `docs_for_chashes` (see Shared chunks below), keeping only documents whose
catalog collection is the dataset's own collection (`docs_for_chashes` is
tenant-wide, `http_catalog_client.py:4310`). Each such document's key is its
catalog title, `<dataset>:<id>`. A row that yields no in-collection key is an error:
the run fails and names every such row. It is not bucketed (`dedupe_by_doc` would
merge them).

- Rank order: the order `nx search` prints its rows, because that order (boosted
  score, file diversity cap, collection interleave) is the system under test. Within
  a run of consecutive rows with equal distance, rows are ordered by their sorted
  document keys, so a tie does not depend on arrival order.
- Chunks to documents: keep the first row for each key in rank order. This is the
  deduplication to document level. Several chunks of one document count once.
- Identical-text documents (Sam, 2026-11-06): each class of byte-identical loaded
  documents counts as one document, named by its smallest id, ranked once and judged
  with the highest grade among its members. Under CoIR's own scoring a retrieved twin
  of the relevant document is a miss, so this raises scores relative to CoIR wherever
  twins exist (in the full corpora, every codetrans-dl test query and 5,517 of 5,851
  synthetic-text2sql ones have one, research-16; fewer twins survive a 5,000-document
  sample), and the results file reports the classes and the
  documents merged per dataset.
- K=10: the ranked list is the first ten distinct keys in rank order. If fewer than
  ten distinct keys return, the query is scored on those, and the count is
  reported with the query.
- Shared chunks: identical chunk text in one collection is one T3 row
  (`CLAUDE.md`), and that row maps to every catalog document whose text contains
  it (`docs_for_chashes`, `src/nexus/catalog/http_catalog_client.py:4310`). The row's
  `source_path` belongs to one writer, so it cannot identify the document. The
  scorer therefore takes the document keys from the chunk hash through
  `docs_for_chashes`, not from the row's path. A row credits every class in its
  group that has not yet been seen, in sorted key order, at consecutive ranks
  starting at that row's position; a group that straddles the tenth place is cut at
  ten. This is generous to the system when documents share text, and the results file
  says so. The run counts rows that credited more than one class inside the top ten,
  per query and per dataset. `_resolve_paths_via_catalog` in
  `scripts/bench/paths.py` keeps only the first sorted document; the new scorer does
  not reuse it.
- Metrics: NDCG@10 over the qrels scores (the CoIR headline), gain `2^g - 1`. Step
  0 found every qrels score in the pool is 1, so this equals linear gain; a dataset
  whose kept grades are anything else is dropped and named before its ingest. Only
  positive judgements are kept. Document-level recall@10: the share of queries
  with at least one relevant document in the top ten. MRR: the mean of 1/rank of the
  first relevant document, with 0 when none is in the returned list.

**Track A subset rule.** The pool is the five datasets the paper's table lists as
Apache 2.0: Synthetic Text2SQL, CodeTransOcean-DL, CodeTransOcean-Contest,
CodeFeedback-ST and CodeFeedback-MT. The licence source is that table and the
upstream sources, which state the same licence; no Hugging Face card states one
(research-15), and the owner ruled that basis sufficient (Sam, 2026-11-06). The check
ran in Step 0, before the pre-registration commit. A dataset is clean when the
paper's table and its upstream source state the same licence, and that licence
allows publishing a derived manifest and results. Every pool dataset is clean (all
Apache-2.0), so none leaves the subset. Each pool entry carries its licence
in the harness, and the run refuses to start, naming the dataset, if one lacks it.
Nothing is substituted.

The subset is the whole pool, and each dataset's corpus is capped at 5,000
documents. The cap is applied by a seeded sample (`random.Random(seed)`, seed 2026)
that keeps every document relevant to a sampled query. The measured corpus sizes
are 816 (codetrans-dl), 66.4k (codefeedback-mt), 106k (synthetic-text2sql) and
157k (codefeedback-st); Step 0 read codetrans-contest at 1,008. With the cap,
the total is at most 25,000 documents, and only codetrans-dl and codetrans-contest
load whole. Because every relevant document is kept and only non-relevant ones are
dropped, sampling can only make retrieval easier than CoIR's full corpora, and the
results page says so. The owner took the cap on 2026-11-06 ("take the 5,000 cap"). The run-time cap is
30 minutes of wall-clock time on each dataset's ingest (owner, 2026-11-03,
nexus-2k66t.2.7: "cap as proposed"), set from the Step 0 write time. A dataset over
the cap is dropped from the published table and named there and in `results.json`;
nothing is substituted. The
non-commercial datasets (APPS, CosQA,
CodeSearchNet) stay out of the default subset. Adding one is an owner decision,
because it changes what the public data licence statement can say.

Queries: per dataset, draw up to 50 queries with `random.Random(seed)` over the
sorted query ids of the qrels **test** split, seed 2026. If a dataset has fewer than
50, all are used. Every pool dataset has a test split (codetrans-dl 180,
codetrans-contest 221, codefeedback-st 31,306, codefeedback-mt 13,277,
synthetic-text2sql 5,851 queries); a dataset without exactly one test-split file
stops the run. Each dataset's sample manifest (ids, judgements, exclusions, identical-text
classes, and the SHA-256 of every loaded document's and kept query's text) is written
beside `results.json`, and its hash is that dataset's query-set hash, so a changed text changes
the hash even when the ids do not.

**Track A latency.** Latency is the wall-clock time of one `nx search` subprocess,
measured by the harness around the call, after one discarded warm-up query per
dataset collection (a test-split query outside the sample). So this is warm,
client-side, end to end: it includes the nx command's start-up. Engine-only time is
reported only if the command prints it, which is unverified. A cold-start figure is
not reported unless it is measured separately. p50 and p95 are taken per dataset, by
nearest rank; a figure pooled over datasets is labelled secondary.

**Statistics.** Every metric, interval and latency percentile is reported per
dataset. Each has a 95% bootstrap interval over queries: 10,000 resamples,
percentile method with nearest-rank endpoints, seed 2026. The interval covers the
choice of queries, not the corpus draw.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Track A search call | `scripts/bench/runner.py` path A (`run_path_a`) | Reuse the subprocess pattern only. Path A requests `-m K*3` with K=3 and takes the first K documents, so it cannot return ten documents for K=10. New call with `-m 100` and the K=10 rule. |
| Document identity | `scripts/bench/paths.py` `_chunk_path` | Replace. Not reused: identity comes from the chunk hash through `docs_for_chashes`, keyed by catalog title, with the empty-key error (Track A identity and scoring). The row path is not read. |
| Recall@10, MRR and NDCG@10 scorer | `scripts/bench/metrics.py` (NDCG at k=3 for RDR-090) | Not extended: a new scorer (`scripts/bench/coir_score.py`) with NDCG@10 at gain `2^g - 1`, document-level recall@10 and MRR, so `metrics.py` keeps RDR-090's NDCG@3 unchanged. |
| Corpus load | none for CoIR | New loader: `put_note`, `fire_note_chains` with an empty `HookRegistry`, and `stamp_note`, all existing (`note_write.py`). Not `nx store put`, which fires the aspect hook. Per-call cost measured in the spike. |
| Timing | `scripts/bench/paths.py` `elapsed_s` (per call, wall-clock) | Not reused: the harness times the same thing (wall-clock around the `nx search` subprocess) with its own `perf_counter`, as defined in Technical Design (warm, client-side), per call. |
| Results format | none | New. |

### Decision Rationale

Track A uses a public code benchmark because it costs nothing and needs no
language model. It measures vector search over code text stored as note documents.
It does not measure the code index that `nx index repo` builds (`docs/collections.md`). The internal bench
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

**Decided 2026-11-03, nexus-2k66t.8** (Sam; relayed by linda):

- Corpus shape: one collection per dataset, so each dataset's results compare to its published CoIR numbers. Each dataset costs one ingest. Each collection's subject is a Rule 1 subject area, one per dataset (Track A ingestion).
- Licence: each dataset's Hugging Face card licence is verified before publishing. Superseded on 2026-11-06 (below), because no card states one.

**Decided 2026-11-03, nexus-2k66t.2.7** (Sam, in elsinore's pane, "cap as proposed"):

- Run-time cap: 30 minutes of wall-clock time on each dataset's ingest, set from the Step 0 write time. A dataset over the cap is dropped and named; nothing is substituted.

**Decided 2026-11-06** (Sam, in elsinore's pane):

- Licence: "upstream licence is fine". The basis is the paper's table and the upstream sources (research-15).
- Corpus cap: "take the 5,000 cap", with the seeded sample above.
- Identical-text documents count as one document (Track A identity and scoring).

**Open**: the decision the owner will take from the result, stated in the pre-registration before the run (Validation).

## Trade-offs

### Consequences

- A public number for code search over a CoIR subset, with its caveats printed beside it. A weak number is published too, and that is the point.
- The published numbers are not CoIR's published numbers (sampled corpus, subset, identical-text merge, chunk-level retrieval, warm latency), and the results page says so.
- The harness is a maintained artefact. Pins go stale when conexus or the engine changes, so each results file records its versions.

### Risks and Mitigations

- **Risk**: a dataset's licence is not what the paper's table says. **Mitigation**: the upstream source was read against the table before the pre-registration (Critical Assumption 1); the cards state none.
- **Risk**: the sample is too small to separate settings. **Mitigation**: report intervals with every number; claim no difference the intervals do not support.
- **Risk**: the RDR-090 result is read as a judgement on answer synthesis. **Mitigation**: publish the recorded causes beside it (plan leakage, plan-library pollution, Q2 vocabulary, Chroma-era substrate).
- **Risk**: the plan library routes a query to a stored plan, as in RDR-090. **Mitigation**: Track A calls `nx search` only, which does not route through the plan library.
- **Risk**: a loader write queues model work in a shared queue. **Mitigation**: the empty hook registry, and the Step 0 aspect check.

### Failure Modes

Only the modes that change a decision:

- **Empty document identity merged into one document.** A row with no key would be bucketed by `dedupe_by_doc` and score as one document. The run fails and reports the count instead.
- **Fewer than ten documents returned.** A query whose returned rows hold fewer than ten distinct documents is scored on those, and the count is reported. It is never padded.
- **Duplicate chunk text.** Documents with identical text are one class; distinct documents that share a chunk share one row, which credits every class in its group in key order (Track A identity and scoring), and the shared-row count is in the results file.
- **Aspect enqueue.** A loader write that queues an aspect task would start Claude-CLI work in whatever session drains the queue. The empty registry prevents it, and the run's tenant is its own, so no other session drains it. Step 0 item 1 checked it, and the harness's engine test (integration-marked) checks it with a positive control whenever it runs, which on CI is when the affected-integration job selects it.
- **Oversized or empty document.** It cannot be stored; it and its queries are excluded and counted, not substituted (Track A ingestion).
- **Unexpected search behaviour.** A rerank, boost or bibliographic signal on a row, a taxonomy assignment on a bench chunk, an nx warning, no rows, or a timeout stops the run instead of being scored.
- **Query set changes between runs.** The results file records a query-set hash, so a change shows as a different hash and the run is labelled as a different set.

## Implementation Plan

### Prerequisites

- [x] Owner decisions 1 to 5 recorded (2026-11-03, nexus-2k66t.7).
- [x] Each pool dataset's licence checked before the pre-registration commit (Critical Assumption 1; Sam, 2026-11-06).
- [x] Corpus cap decided: 5,000 documents per dataset by the seeded sample (Sam, 2026-11-06).
- [x] Run-time cap decided: 30 minutes of wall-clock ingest per dataset, an over-cap dataset dropped and named (Sam, 2026-11-03, nexus-2k66t.2.7).
- [ ] Step 2 write opt-in: the run writes outside pytest, so the production-write guard needs an owner decision (a scoped `NX_ALLOW_PROD_WRITE` or a harness exemption); Sam's Step 0 grant covered the spike only.
- [ ] Pre-registration file (`docs/rdr/evidence/rdr-244/preregistration.md`) committed before the first run: subset, seed, query count, caps, scoring rule, interval method, and the decision the owner will take from the result (see Validation).

### Minimum Viable Validation

Track A on the subset, end to end, on a few documents first: the loader writes
documents with title keys, `nx search` returns rows whose keys match, the scorer
computes NDCG@10, recall@10 and MRR, `results.json` is written, and a second run
from the same pins reproduces the ranked key lists and metric values exactly (see
Test Plan). Latency is recorded and not compared. The Step 0 spike settles Critical
Assumptions 2 to 6, and Step 1 does not start until its aspect check (item 1 of
Step 0) passes.

### Phase 1: Code Implementation

#### Step 0: Spike (Track A unknowns)

Run the checks in order. The first check uses one dataset (codetrans-dl) and twenty
documents; the others use the pool.

Step 0 ran on its own loopback engine with a minted tenant, under a write opt-in
scoped to that engine (Sam, 2026-11-06), and its results are in research-15 and -16.

1. **Aspect check.** Before and after a test ingest of twenty documents through the
   loader's path, record the `aspects` tuple subspace counts (available, claimed,
   dead, consumed). The queue moved to the tuple space with RDR-235, so
   `pending_count`, `list_pending` and `list_waiting` no longer apply. On the run's
   own tenant the counts cover every state, so a task that a running aspect worker
   had claimed or drained would still show; the check holds whether or not a worker
   runs.
   All four must stay zero, and no `claude` process may start. A non-zero result
   stops Track A (see Track A ingestion).
2. **Identity.** Print the rendered collection name for local mode. For five
   queries, run `nx search --json -m 100` and record each row's chunk hash, the
   documents `docs_for_chashes` returns for it, their titles, the chunk count per
   document, and any row that yields no key.
3. **Boosts.** Print each row's boost fields. Confirm `search.hybrid_default` is false
   in the pinned config.
4. **Shared chunks.** Count, per dataset, the rows whose chunk hash maps to more
   than one document.
5. **Cost and size.** Record the per-document write time through the loader. Read the
   corpus size of codetrans-contest. Read each pool card's licence field directly.
6. **Qrels.** Record every distinct score value in each pool dataset's qrels file.
7. **Subjects.** Name one Rule 1 subject per dataset, five distinct, with the paired
   datasets (CodeFeedback ST and MT, CodeTransOcean DL and Contest) on separate
   subjects; if Rule 1 cannot supply five, that is an owner decision. Result: Track A
   ingestion.

Results go to T2 research entries (research-15 and -16). The write time confirmed
the 30-minute run-time cap, and the qrels values fixed the gain form.

#### Step 1: Track A harness

Add the loader (`put_note`, `fire_note_chains` with an empty registry, `stamp_note`),
the scorer (document-level recall@10, MRR, NDCG@10 at k=10), the K=10 dedupe with the
empty-key error, the timing definition above, and the `results.json` writer. Done in
`scripts/bench/coir_run.py`, `coir_sample.py` and `coir_score.py`, with
`tests/test_bench_coir_*.py`; the scorer is new rather than an extension of
`scripts/bench/metrics.py`, which keeps RDR-090's NDCG@3.

#### Step 2: Track A run

Run the subset with pinned versions, and record the numbers and their intervals.

### Phase 2: Operational Activation

#### Activation Step 1: Publish

Publish the harness, the query-set manifest, the dataset manifest with the licence
line for each dataset, and each `results.json`, with the RDR-090 result beside them.
The licence check is done (Decisions, 2026-11-06), so publishing waits only for the
first run's results.

### Day 2 Operations

The only persistent output is result files and their sample manifests. The corpus
is loaded into one collection per dataset on the run's own engine, which is discarded
afterwards, and is re-loadable from the dataset revisions and manifests recorded.

### New Dependencies

- CoIR datasets (Hugging Face, `CoIR-Retrieval`); per-dataset licences as above.
- No new runtime dependency for conexus. The harness itself needs `pyarrow` to read
  the parquet files (`uv run --with pyarrow`), and a checkout with the gate jar built,
  because it boots its engine through `tests._engine_substrate`.

## Test Plan

- **Scenario**: Track A scorer on a hand-computed three-query example — **Verify**: NDCG@10, document-level recall@10 and MRR match the hand values exactly.
- **Scenario**: K=10 dedupe — a result list of 30 chunks from 4 documents — **Verify**: four documents scored, count reported, no padding.
- **Scenario**: empty document key in a result row — **Verify**: the run fails and reports the count; no bucket.
- **Scenario**: Track A run twice with the same pins and the same loaded collections — **Verify**: identical ranked key lists and identical NDCG@10, recall@10 and MRR values. Latency is excluded from equality and reported as a distribution.
- **Scenario**: changed query-set file — **Verify**: the hash differs and the run is labelled as a different set.
- **Scenario**: a pool dataset with no recorded licence — **Verify**: the run refuses to start and names the dataset. (Under the 2026-11-06 ruling no licence finding drops a dataset, so there is no licence drop or empty-pool path to test.)
- **Scenario**: loader ingest of twenty documents against the real engine, with the empty registry — **Verify**: the four `aspects` tuple counts stay zero and no `claude` process starts, while the same put with the default hooks queues a task (integration, not a mock).
- **Scenario**: two documents with identical text, ingested into one dataset's collection — **Verify**: one class, named by the smaller id, credited once; shared-row count 0 for that row.
- **Scenario**: rows tied on distance, and a row shared by several classes — **Verify**: ties ordered by key, shared classes credited in key order at consecutive ranks, cut at ten.
- **Scenario**: a document over 16,384 bytes or empty — **Verify**: it and its queries are excluded and counted.
- **Scenario**: each refusal (hybrid default on, Voyage key set, dirty checkout, engine off loopback, non-bge collection or engine, engine below 0.1.175, no nx, a parquet glob matching zero or several files, an nx warning, no rows, a non-zero exit, an occupied collection) — **Verify**: the run stops and names the cause.
- **Scenario**: an ingest that passes the run-time cap — **Verify**: the loader raises at the first document past the cap and writes nothing more.
- **Scenario**: a result row whose documents are held only in another collection — **Verify**: those documents are dropped, and a row left with no key is the empty-key error.
- **Scenario**: a result row with no chunk hash match in `docs_for_chashes` — **Verify**: the run fails and names the row, not a silent drop.

## Validation

### Testing Strategy

Pre-register before any run: the subset (by the rule above, with the measured sizes),
the query sample (seed 2026, 50 per dataset), the corpus cap and its sample rule,
the run-time cap, the scoring rule (NDCG@10 over the qrels scores, document-level
recall@10, MRR), the interval method (95%, 10,000 resamples, percentile), and the
decision the owner will take from the result. Commit that file
before the first run and do not change it afterwards. RDR-090 set its criterion before
its run and then reported the failure; this RDR does the same whichever way the
result falls.

1. **Scenario**: Track A on the pinned CoIR subset.
   **Expected**: NDCG@10, document-level recall@10, MRR, and latency p50 and p95, each with an interval, published whether or not the result looks favourable, with the per-dataset sampled-corpus (easier), identical-text merge (higher), chunk-level retrieval and warm-latency caveats printed beside them.

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

Earlier statements withdrawn or corrected. The data licence is per dataset from the
paper's table (research-9), not Apache-2.0. The headline metric is NDCG@10
(research-9), not recall alone. The engine pin is `REQUIRED_ENGINE_VERSION` (0, 1,
175), not tag v0.1.176 (research-10). "Track A runs at no API cost" was marked
verified; it is unverified, because the loader's writes do queue aspect rows under
`nx store put` (research-11). The loader now uses the Python path with an empty
registry. "Graded relevance" has no source (research-14). The claim that Track A
measures the path most users run is dropped.

### Assumption Verification

All six Track A Critical Assumptions were verified in Step 0 (2026-11-06): the
dataset licences (1, by the owner's ruling on the paper's table and upstream
sources), the search row identity (2), the local collection name and scoping (3),
the index cost and run time (4), the zero aspect enqueue (5), and the absence of
boosts (6). Assumption 1 gated subset membership as well as publication; 2 to 6
gated the first run.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `put_note` and `fire_note_chains(hooks=)` | nexus | Source Search (`src/nexus/catalog/note_write.py:634`, `:1022`; research-11). Not `nx store put`, which fires the aspect hook. |
| `stamp_note` | nexus | Source Search (`src/nexus/commands/store.py:233`, after the chains); called by the loader, and by the harness's engine test. |
| `nx search --corpus <full name> -m N --json` | nexus | Source Search for flags and row fields (`search_cmd.py`; research-8). Row identity: Spike (Critical Assumption 2). |
| `t3_collection_name` (local mode token) | nexus | Source Search (docstring shows the voyage token); Spike for local mode (Critical Assumption 3) |
| `nx index md` | nexus | Source Search (`index.py`, one file per call); not used by Track A |
| voyage-context-3 pricing | Voyage AI | Not used by Track A (Deferred) |

### Scope Verification

The Minimum Viable Validation is in scope for the first implementation step and is
a condition of accepting Track A.

### Cross-Cutting Concerns

- **Versioning**: conexus version and git sha, the engine's release, embedding mode and profile as `GET /version` reports them (at least `REQUIRED_ENGINE_VERSION`), collection full name, `-m` value and flags, CoIR dataset revisions, all recorded in every results file. No reranker runs (a rerank score on any row stops the run), so none is recorded.
- **Build tool compatibility**: N/A (a script).
- **Licensing**: per dataset, from the paper's table and the upstream sources; the dataset cards state no licence (Critical Assumption 1). Repository licence Apache-2.0 does not apply to the data. Decided: the paper's table and the upstream sources are the basis (Sam, 2026-11-06).
- **Deployment model**: N/A; runs from a checkout.
- **Incremental adoption**: N/A (Track A is the whole RDR).
- **Secret/credential lifecycle**: none. Track A uses no key.
- **Memory management**: N/A beyond the dataset collections.

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

- Research: T2 `nexus_rdr/244-research-1` to `244-research-14`; design research T2 `nexus/2k66t2-eval-design-research-2026-11-03`.
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
- 2026-11-03: Gate r2 fixes (critique T2 `nexus_rdr/244-gate-critique-2026-11-03-r2`): the loader uses `put_note` and `fire_note_chains` with an empty registry, because `store put` queues Claude-CLI aspect rows and `NX_ASPECT_WORKER_AUTOSTART` does not stop the queue write; Step 0 aspect check added; Critical Assumption 5 corrected to unverified (C1). Reranker and boosts stated as they run for one collection, with topic assignments and hybrid pinned (S-a). Shared-chunk credit rule specified (S-b). Pool capped at 5,000 documents per dataset by a seeded sample, with the measured sizes (S-c). Empty-pool fallback and Step 0 wording fixed (S-d). Path-most-users claim dropped (S-e). "Graded" dropped from the qrels source (S-f). Research `nexus_rdr/244-research-11` to `-14`.
- 2026-11-03: Gate round 3 — PASSED (0 Critical, 8 Significant, 0 ship-blockers); commit `6516100dd`; critique `nexus_rdr/244-gate-critique-2026-11-03-r3`.
- 2026-11-03: Owner rulings (nexus-2k66t.8; Sam, relayed by linda): one collection per dataset, so results compare to CoIR's published numbers (Technical Design, Decisions); each dataset's Hugging Face card licence verified before publishing, and a dataset that is not clean drops from the subset (Subset rule, Prerequisites, Critical Assumption 1, Decisions). The run-time cap stays open (nexus-2k66t.2.7).
- 2026-11-03: Gate r3 residual, hybrid_default and pinned settings: nexus-2k66t.2.1.
- 2026-11-03: Gate r3 residual, Step 0 aspect check: nexus-2k66t.2.2.
- 2026-11-03: Gate r3 residual, stamp_note: nexus-2k66t.2.3.
- 2026-11-03: Gate r3 residual, shared-chunk key order and rank: nexus-2k66t.2.4.
- 2026-11-03: Gate r3 residual, docs_for_chashes scope: nexus-2k66t.2.5.
- 2026-11-03: Gate r3 residual, documents over 16 KB: nexus-2k66t.2.6.
- 2026-11-03: Gate r3 residual, run-time cap: nexus-2k66t.2.7.
- 2026-11-03: Gate r3 residual, Infrastructure Audit row and Key Discoveries path identity: fixed in text (Infrastructure Audit row, Key Discoveries); fix check pending the commit that carries this revision.
- 2026-11-03: Gate round 4 — PASSED (0 Critical, 9 Significant, 0 ship-blockers); commit `8f0c4c42b`; critique `nexus_rdr/244-gate-critique-2026-11-03-r4`; fix check `nexus_rdr/244-fix-check-8f0c4c42b` (three dispatches, PASS).
- 2026-11-03: Gate r4 residual, collection subject per dataset: nexus-2k66t.2.8.
- 2026-11-03: Gate r4 residual, spike isolation and teardown: nexus-2k66t.2.9.
- 2026-11-03: Gate r4 residual, "not clean" and freeze timing: nexus-2k66t.2.10.
- 2026-11-03: Gate r4 residual, per-dataset reporting and warm-up: nexus-2k66t.2.11.
- 2026-11-03: Gate r4 residual, qrels split: nexus-2k66t.2.12.
- 2026-11-03: Gate r4 residual, run-time cap parameter and prerequisites (overlaps nexus-2k66t.2.7): nexus-2k66t.2.13.
- 2026-11-03: Gate r4 residual, stale identifiers: nexus-2k66t.2.14.
- 2026-11-06: Wording amended to the reviewed Step 1 harness (`ef65b6787`; review gate r2, T2 `nexus_rdr/244-step1-gate`), standing grant 3. Residuals nexus-2k66t.2.1 to .2.14: pinned-settings record and refusals (.1); aspect check on the `aspects` tuple counts, `list_waiting` being retired (.2); `stamp_note` (.3); output-order ranking with key tiebreak and key-ordered shared credit cut at ten (.4); in-collection `docs_for_chashes` filter (.5); oversized and empty documents excluded with their queries (.6); both caps decided, with Prerequisites (.7, .13); one Rule 1 subject per dataset as a Step 0 item (.8); a self-provisioned loopback engine and tenant, no teardown (.9); licence basis and its timing before the pre-registration (.10); per-dataset reporting and one warm-up per collection (.11); the test split (.12); stale identifiers (.14). Owner rulings of 2026-11-06 recorded: licence basis, 5,000 cap, identical-text classes. Critical Assumptions 1 to 6 verified (research-15 to -17).
- 2026-11-03: Gate r4 residual, docs_for_chashes scope sharpened by one collection per dataset: nexus-2k66t.2.5.
- 2026-11-03: Gate r4 residual, round 3 items still open in text (hybrid_default, aspect check, stamp_note, shared-chunk order, oversized documents): nexus-2k66t.2.1 to .2.4 and .2.6.
- 2026-11-03: Accepted. Sam, in elsinore's pane, typed "accept RDR-244" on 2026-11-03. Gate round 4 PASSED; fix check `nexus_rdr/244-fix-check-8f0c4c42b`.
