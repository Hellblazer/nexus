---
title: "RDR-070 Phase 5: Taxonomy-Aware Recall in nx_answer — Teach the Composed-Retrieval Path to Read the Taxonomy It Already Has"
id: RDR-134
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-27
accepted_date: 2026-11-16
related_issues: [nexus-9napz, nexus-n1908]
related_rdrs: [RDR-070, RDR-075, RDR-156, RDR-179, RDR-191, RDR-193, RDR-227, RDR-229, RDR-232, RDR-243]
related_tests: []
implementation_notes: ""
---

# RDR-134: RDR-070 Phase 5 — Taxonomy-Aware Recall in nx_answer

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

An RDR (research, design, review) record is this project's design document.
This one was a stub from 2026-05-27, written when the taxonomy lived in
ChromaDB. It was rewritten on 2026-11-16 against the PostgreSQL engine after
Sam's "Measure first" before the gate, and his "Fix all 7, fix check,
re-gate" after gate round 1, which was answered with a second measurement. The measurements set the design: see Research
Findings, 134-research-10 and 134-research-13.

## Problem Statement

Nexus stores indexed text as chunks (passages of a few hundred words, each
with an embedding: a vector that places it by meaning) in collections, one
collection per source and kind (`rdr__…` for design records, `docs__…` for
documentation, `knowledge__…` for notes and papers, `code__…` for code).
Nexus also builds a taxonomy of every collection (RDR-070): the chunks are
clustered into topics; each topic has a label and a centroid (the mean
embedding of its chunks), and each chunk has a topic assignment. This runs on
every index and every store. The retrieval path never reads it.

`nx_answer` is the tool that answers an analytical question: it runs a saved
plan whose `search` steps query every collection in a corpus (for prose: every
`knowledge`, `docs`, `rdr` and `paper` collection; the measured install has 123
non-empty ones and no `paper` collection) and merge the hits by vector distance. Querying every collection and
merging is called the fan-out below.

The fan-out costs a lot. On the benchmark questions whose answers are known
RDRs, searching the single collection that holds the RDRs scores NDCG@3 0.591
(26 questions, leg 1, 134-research-10); the same kind of search over the 123
prose collections scores 0.335 (25 questions, NDCG capped at 1,
134-research-13). The two figures come from different question sets and
scorers and are not a paired comparison; the paired comparisons are in
Research Findings. NDCG@3 is a 0 to 1 score of how
well the top three results match the known answers. The right RDR is usually
still among the candidates; generic documents from other collections
(CHANGELOG.md, cli-reference.md, architecture.md, README.md) push it out of
the top three, because they are close to many questions.

### Enumerated gaps to close

#### Gap 1: nx_answer ignores the taxonomy entirely

No retrieval step uses topics. The built-in research, analyze and document
plans removed their `topic` binding on purpose (nexus-opraf): `topic:
$concept` passed free text where a real topic label is required, matched
nothing, and the topic filter returned no results
(`conexus/plans/builtin/research-default.yml:29-35`). The plan runner can
still forward a topic (`_apply_scope_to_args`,
`src/nexus/plans/runner.py:1525`), but nothing chooses one from the
question.

#### Gap 2: no query-time topic selection, and the existing topic filter loses recall

Two pieces exist and are not composed. The engine can rank a collection's
topic centroids against a vector (`HttpCentroidStore.ann_query`,
`src/nexus/db/t2/http_centroid_store.py:249`; ANN is approximate nearest
neighbour search; nothing on the query path calls it). The engine can search
inside one topic of one collection (`search_topic_scoped`,
`src/nexus/db/http_vector_client.py:4960`). The client-side `topic=` argument
of `search_cross_corpus` is different: it filters the results after an
unscoped search (`src/nexus/search_engine.py:2548-2550`), so it can only
remove hits, never reach chunks the unscoped search missed.

### Terms used below

- **Routing**: for each collection, choosing topics from the question and
  searching only the chunks assigned to them.
- **Top-1 / own band**: keep a collection's single nearest topic, or every
  topic whose similarity is within a width (a similarity difference, 0.03 in
  the measurements) of that collection's nearest topic.
- **Per-collection k**: how many rows the engine takes from each collection
  before merging (`search_engine.py:1195-1226`).
- **Coverage**: one minus the share of a collection's live chunks that have
  no assignment to that collection's own topics (projection rows from other
  collections do not count), as `taxonomy_coverage_counts_<dim>` counts them.
  A collection with no chunks has no coverage and is never routed.
- **`chash_allow`**: a list of chunk hashes a search is confined to (document
  confinement, RDR-232).
- **Sensitivity exclusion**: the engine dropping chunks marked sensitive from
  a read (RDR-243).
- **Paths A and B**: the benchmark's `nx search` path and its `nx_answer`
  path (`scripts/bench/paths.py`).

## Relationship to Prior RDRs

- RDR-070 (closed) built discovery, assignment and centroids. This RDR is its
  missing consumer. RDR-075 (closed) projects one collection's chunks onto
  another's topics; this RDR does not use projection.
- RDR-156 (closed) built the topic-scoped search route this design reuses;
  the centroid ANN function it calls is `taxonomy_ann_query_<dim>`
  (changelog vectors-013).
- RDR-179 (closed), Phase 4, sequences this RDR and asks for a taxonomy
  on/off leg in the retrieval benchmark (Phase 4.2). This RDR adds it
  (`scripts/bench/taxonomy_ab.py`, `taxonomy_ab2.py`).
- RDR-227 (closed) plans small collections as exact scans and large ones on
  per-collection HNSW indexes (`pci_`). That router serves the plain-search
  path; the topic-scoped functions are called directly, not through it
  (`PgVectorRepository.java:5205`). Step 1 calls the new topic-set functions
  directly too (vectors-062); see Step 1 and Risks.
- RDR-232 (closed) narrows `nx_answer` by owner, corpus and subtree before
  search, and can confine a search with `chash_allow`. Routing runs inside
  the collections that narrowing leaves; a confined search is not routed.
- RDR-193 (accepted) moves taxonomy discovery onto the engine. That keeps
  topics fresher; it does not change the routes used here.
- RDR-229 (draft) stops using the collection name as the taxonomy key. The
  route asks for a collection's topics by whatever key the taxonomy tables
  use; whichever RDR lands second adjusts that one lookup.

## Context

### Background

The 2026-05-27 stub came from an `nx_answer` question about RDR-070 that
returned a summary of the session's startup text instead (nexus-n1908). That
honesty bug is fixed: `_nx_answer_is_empty_retrieval`
(`src/nexus/mcp/core.py:11116`) returns an explicit no-match when the
retrieval steps found nothing.

Sam's decisions, 2026-11-16 (T2 `nexus_rdr/134`): "would truly love to get
RDR-134, 171 done"; "Measure first", with a spend cap of 5 USD; after gate
round 1, "Fix all 7, fix check, re-gate".

### Technical Environment

- Taxonomy tables live in the engine's PostgreSQL database: `topics`,
  `topic_assignments`, `taxonomy_centroids` (one table since RDR-191,
  `taxonomy-007-unify-centroids.xml`). A centroid is stored in its
  collection's own embedding space: 1024 dimensions for Voyage (the hosted
  embedding provider; voyage-context-3 for prose, voyage-code-3 for code) or
  768 for the local bge model. Discovery clusters stored embeddings and never
  re-embeds (`src/nexus/commands/taxonomy_cmd.py:306-317`).
- Assignment runs on every stored batch (`taxonomy_assign_batch_hook`,
  `src/nexus/mcp_infra.py:2511`) and can be deferred or dropped after retries
  (deferral at `mcp_infra.py:2583-2586`, loss after retries at `:2597-2601`;
  `nx taxonomy drain` assigns them later, 134-research-14); discovery runs
  after `nx index`.
- The prose search path: `search` and `query` tools → `search_cross_corpus` →
  `HttpVectorClient.search_per_collection` (`src/nexus/search_engine.py:2111`)
  → `POST /v1/vectors/search-per-collection`
  (`service/.../http/VectorHandler.java:279`). One request covers a group of
  collections that share an embedding model; the engine embeds the query once
  (`PgVectorRepository.java:3117`) with the query-type embedder for prose
  (voyage-context-3); the code and other Voyage models are embedded without
  an input type (134-research-11). It searches each collection and merges.
- `search-topic-scoped` refuses a `chash_allow` list
  (`VectorHandler.java:1058-1063`) and takes no metadata `where`.

## Research Findings

### Investigation

Drift survey of the stub against develop: T2 `nexus_rdr/134-research-1` to
`-8`. Pass rule, written before the first run: `-9`. Measurements: `-10`
(leg 1) and `-13` (production set, after gate round 1). Embedding type:
`-11`. The empty-topic cause: `-12`. Sources re-read for the fix check:
`-14`.

Both measurements are retrieval-only paired comparisons on the cloud engine
(release 0.1.194). Spend: Voyage query embeddings only; leg 1 under 0.10
USD (`-10`), the production-set run is not separately recorded. No language
model was called.

Leg 1 (`taxonomy_ab.py`): 26 questions whose required sources are RDRs, from
the benchmark sets `spike_5q`, `golden_v1` and `golden_heldout_v1`. Two
collection sets: S1 is the nexus RDR collection alone; S2 is the 38 `rdr`,
`docs` and `knowledge` collections that have topics. Arms: OFF (each
collection searched, merged by distance); a global band (keep topics within
0.03 or 0.06 of the best similarity across all collections); top-1 and own
band per collection. The pass rule in `-9` named the global bands, a 0.10
band and top-3 per collection; the 0.10 band and top-3 were not run, and
top-1 and own band, which were run, were not on that list.

Production set (`taxonomy_ab2.py`): all 123 non-empty prose collections, as
`nx_answer` fans out. 37 have topics; for 36 the centroid ranking returned a
topic and they were routed, and the other 87 were searched unscoped in every
arm. 33 questions: the 25 graded by RDR and 8
graded by paper or note title (other projects' RDRs skipped). NDCG capped at
1 and documents deduplicated by file name. Arms: P0, the production
`search_cross_corpus` call; OFF; top-1 with two fallback rules.

### Key Discoveries

- **Verified** (`-13`): on the production set, top-1 routing raises NDCG@3 on
  the RDR questions from 0.335 to 0.470 (+0.135; 9 better, 0 worse) with
  recall@10 (share of the grade-3 sources among the top ten documents) 0.817
  to 0.823. Title questions: 0.500 to 0.538 (1 better, 0 worse). All 33:
  0.375 to 0.487, 10 better, 0 worse.
- **Verified** (`-13`): the production call `search_cross_corpus`, its rows
  ordered by distance, scores exactly the OFF arm. Unordered, as it returns
  them, the rows score 0.02. Callers re-sort by `hybrid_score`
  (`search_engine.py:2299-2303`), which adds boosts, so OFF is the
  production baseline without boosts; Step 9 measures the full path.
- **Verified** (`-10`): leg 1 found the same effect on the 38 collections with
  topics: 0.322 to 0.488 (top-1, 10 better, 0 worse) and 0.486 (own band, 10
  better, 1 worse). Capped at 1, the top-1 gain is 0.155.
- **Verified**: the gain comes from removing generic documents. In most
  improved questions (8 of 10) the OFF top three held CHANGELOG.md, cli-reference.md,
  architecture.md, README.md or SKILL.md and the routed top three held the
  answer.
- **Verified** (`-10`): centroid similarity cannot be compared across
  collections. The global band scored 0.140 and 0.205 (−0.18 and −0.12) and
  lost about half the recall: generic topics of one collection (0.60) outrank
  the right topic of another (0.49). That 0.60 collection is the orphaned
  `knowledge__1-1` below, which inflates the loss; on the 6 questions where
  the band kept none of its topics, the band still scored 0.064 against OFF
  0.323. This RDR ranks topics only within a collection and never drops a
  collection on similarity.
- **Verified** (`-10`): inside the single RDR collection (S1) routing gains
  only +0.021 to +0.030. The value is in the wide fan-out. Inferred, not
  measured: a request narrowed to a few collections gains less.
- **Verified** (`-13`): falling back on short rows did not erode the gain.
  No kept topic returned zero rows, so the zero-row fallback (F0) never
  fired; falling back when a topic returned fewer than 20 rows (Fk) fired on
  about 11 of 36 routed collections per question and gave identical scores.
  Production's per-collection k for prose is 40 at ten results
  (`search_engine.py:899-908`), so 20 is a looser bound than production's.
- **Verified** (`-12`): a taxonomy can outlive its chunks.
  `knowledge__1-1` holds 0 chunks (its rows were quarantined and then
  expired, `-14`) but keeps 21 topics, their centroids and assignments, so every
  topic search there returns nothing (leg 1 hit 16 of the 21). Topics are
  therefore not proof that a collection has assigned live chunks.
- **Verified** (`-13`), unrouted only: the production call answers in 2.5 s
  per question; the same 123 searches made one by one from the client take
  42 s. Most of the spike's latency is therefore client serialisation; the
  routed engine cost is not measured (see Critical Assumptions).
- **Documented** (`-11`): Voyage's query and document input types differ
  only by a prepended prompt; its documentation says embeddings made with and
  without an input type are compatible and recommends the query type for
  queries and the document type for documents. Centroids
  are means of document-type chunk embeddings, so ranking them with the
  search's query-type vector follows that recommendation (inferred, not
  measured). This applies to prose; code collections embed without an input
  type and are not routed. The spike ranked them
  with a document-type vector, because the only client embedding route
  (`embed_for_collection`, `http_vector_client.py:6736`) embeds as a
  document.

### Critical Assumptions

- [x] Topic routing improves cross-collection retrieval — **Status**:
  Verified (`-10`, `-13`) — **Method**: Spike.
- [x] Per-topic centroids are enough; no per-collection summary embedding is
  needed — **Status**: Verified (`-10`, `-13`) — **Method**: Spike.
- [ ] Ranking centroids with the query-type vector does at least as well as
  the document-type vector the spike used — **Status**: Documented (`-11`),
  not measured — **Method**: the Minimum Viable Validation, which runs the
  engine route.
- [ ] One routed engine request costs at most 1.5 times the latency of the
  same request unrouted — **Status**: Unverified; 1.5 is a target, not an
  estimate (the route adds a centroid query and a joined search per routed
  collection) — **Method**: Minimum Viable Validation.
- [ ] The gain carries through to `nx_answer`'s answers and holds for
  questions whose answer is a generic document — **Status**: Unverified —
  **Method**: Steps 8 and 9.

## Proposed Solution

### Approach

Add topic routing to the engine's multi-collection search, behind a request
field. For each collection in the request that has enough topic coverage, the
engine ranks that collection's topic centroids against the query vector it
already computed, keeps the nearest topic (or an own band), searches only
inside the kept topics, and falls back to the collection's unscoped search
when they return nothing. Results merge exactly as today. `nx_answer`'s
`search` and `query` steps send the field, except steps scoped to another
domain or a topic; nothing else does until it is measured.

### Technical Design

**Request.** `POST /v1/vectors/search-per-collection` gains an optional
field `topic_route`: `{"mode": "top1" | "band", "width": <similarity
difference, band only>}`. Absent means today's behaviour, byte for byte.

**Engine, per collection, when `topic_route` is present:**

1. Coverage check: route the collection only if it has chunks and its
   coverage is at least 0.9. Coverage comes from
   `taxonomy_coverage_counts_<dim>` (`taxonomy-022`, bodies replaced by
   `taxonomy-023`): one row of
   (has_taxonomy, chunks, unassigned) over the collection's live chunks, the
   same live-chunk filter the plain search applies. It is cached per tenant
   and collection, and dropped by every store, assignment, topic, centroid or
   manifest write that reaches the engine's routes (the two catalog
   background jobs drop the tenant when they end); writers that do not reach
   those routes rely on a ten-minute ceiling. Below the threshold, with no chunks, or
   with no centroids, the collection is searched unscoped with status
   `no_topics`. A routed collection at coverage 0.9 still hides up to a
   tenth of its chunks from the routed search; that remainder is an accepted
   risk (see Risks), and 0.9 is a starting value that Step 5a checks.
2. Use the query vector the request already computed for this
   collection's model group (query type for prose).
3. Rank the collection's centroids with `taxonomy_ann_query_<dim>`, 10
   nearest.
4. Keep the nearest topic (`top1`), or all within `width` of it (`band`).
   Similarity is compared only within the collection.
5. Search the collection's chunks restricted to chunks assigned to the kept
   topics, with the same per-collection k, threshold, sensitivity exclusion
   and live-chunk filter as the unscoped search.
6. Fallback: if step 5 returns zero rows, use the collection's unscoped
   search, status `fallback`. Otherwise status `routed`.

Merging across collections stays as it is. The response carries the status
of each collection.

**How routing treats the request's other fields:**

| Field | Treatment |
| --- | --- |
| `chash_allow` | Whole request unrouted, status `skipped` (a design choice: a confined request is already narrow) |
| metadata `where` | Whole request unrouted, status `skipped` |
| per-collection `thresholds` | Applied to routed rows as to unscoped rows |
| `lexical` | The lexical (full-text) arm is not routed; it merges as today |
| `rerank` | Reranks the merged rows, routed and unscoped alike |
| `include_embeddings`, `content_chars`, `include_source_uri`, `embeddings_limit` | Apply to every returned row |
| `rerank_top_k`, `rerank_max_candidates`, `limit` | Apply to the merged rows as today |
| `exclude_sensitive` | Applied inside the routed search as in the unscoped one |

**Capability.** The engine reports `vector_topic_route_supported` in
`GET /version`, and the public cloud edge adds it to its `/version`
allowlist; without that, the cloud client reads the flag as absent
(`-14`). The client reads it the way it reads `supports_chash_allow` (an
outage reads as unsupported, which is safe here: the request goes
unrouted).

**Client and scope of the default.** `search_per_collection` and
`search_cross_corpus` gain a `topic_route` argument, default off. The plan
runner sets it on `search` and `query` steps unless the step's scope names
a `taxonomy_domain` other than `prose` or a `topic` (an unscoped step is
routed: most plans carry no scope, and only three builtin templates declare
`prose`, so a prose-only rule left routing almost unreachable; Step 9 probe,
`134-research-17`), carried the way `chash_allow` is: popped from the
step's args and held in a context variable that the `search` and `query`
tools read when they call `search_cross_corpus` (`runner.py:3562-3567`,
`-14`), so the dispatcher never drops it. Only collections of the prose
prefixes are routed, even when a step's corpus is `all`. A `query` step
that takes the catalog route (`author`, `content_type`, `follow_links`,
`subtree`) and a `search_metadata_scoped` step do not reach
`search_cross_corpus` and are not routed. The setting is
`search.topic_route_nx_answer` (`off` | `top1` | `band:<width>`, default
`off` until Step 9). `nx search`, the MCP `search` tool and code collections
stay unrouted; turning them on needs their own measurement.

**Benchmark ordering.** `search_cross_corpus` returns rows in collection
order (`-13`); the benchmark orders by distance, while callers order by
`hybrid_score`.

### Existing Infrastructure Audit

| Proposed component | Existing module | Decision |
| --- | --- | --- |
| Query vector | Engine query embedder in the search request | Reuse |
| Centroid ranking | `taxonomy_ann_query_<dim>`, `TaxonomyCentroidRepository.annQuery` | Reuse |
| Topic-restricted search | `search_topic_scoped_<dim>`, one per dimension (384, 768, 1024; first defined in `catalog-006`, redefined since, latest in `vectors-055`) | Extend: a set of topic ids, three functions |
| Coverage | `taxonomy_coverage_counts_<dim>` (`taxonomy-022`, set-based bodies in `taxonomy-023`; `taxonomy_unassigned_chashes_<dim>` pages chashes capped at 1000 with no count) | New, three functions, with a per-collection cache |
| Multi-collection request and merge | `/v1/vectors/search-per-collection` | Extend with `topic_route` |
| Capability flag | `GET /version`, `supports_chash_allow` pattern | Reuse the pattern |
| Benchmark leg | `scripts/bench/taxonomy_ab.py`, `taxonomy_ab2.py` | New (RDR-179 Phase 4.2) |

### Decision Rationale

Routing inside the engine's existing request keeps one query embedding and
the engine's parallel fan-out; the unrouted figures (2.5 s in the engine,
42 s from the client) show that the fan-out's latency lives there. Ranking topics within each collection is the variant
both measurements support; ranking across collections is refuted. The
coverage check and the zero-row fallback make a missing, orphaned or partly
assigned taxonomy below 0.9 coverage fall back to today's search for that
collection. Scoping
the default to `nx_answer` keeps unmeasured paths (`nx search`, the MCP
`search` tool) unchanged; inside `nx_answer` the search layer routes prose
collections only, and Step 9 measures the plans the questions match.

## Alternatives Considered

### Alternative 1: Route on the client with the existing routes

**Description**: `search_cross_corpus` embeds the query, calls centroid ANN
per collection, then `search_topic_scoped` per kept topic.

**Pros**: no engine change; it is what the benchmark does.

**Cons**: the query is embedded again for each topic search, and the client
makes one round trip per collection and topic; the unrouted fan-out alone
took 42 s from the client against 2.5 s in the engine.

**Reason for rejection**: latency. It stays as the benchmark's reference
implementation.

### Alternative 2: Route by collection, not by topic

**Description**: rank collections by their best centroid similarity and
search only the top collections.

**Pros**: fewer searches.

**Cons**: centroid similarity is not comparable across collections; the
global band lost 0.12 to 0.18 NDCG@3 and about half the recall.

**Reason for rejection**: refuted by `-10`.

### Alternative 3: Use the client-side `topic=` filter

**Description**: pick a topic and pass it as `topic=` to
`search_cross_corpus`.

**Pros**: already wired through the plan runner.

**Cons**: it filters after an unscoped search, so it cannot recover a buried
result; it takes one label for all collections, and labels are per
collection.

**Reason for rejection**: wrong mechanism for recall.

### Briefly Rejected

- Per-collection summary embeddings: not needed; topic centroids carry the
  signal.
- Rerank instead of route: reranking reorders the merged candidates but
  cannot bring in an answer that generic documents kept out of each
  collection's k.
- A second, document-type query embedding for centroid ranking: kept only as
  the response if the Minimum Viable Validation misses (Step 6).

## Trade-offs

### Consequences

- `nx_answer`'s prose search ranks specific documents above generic ones.
- Topic coverage becomes part of retrieval quality for routed collections:
  below 0.9 a collection is searched as today, and above it up to a tenth of
  its chunks are outside the routed search.
- Each routed collection costs one centroid query and one joined search
  inside the engine.

### Risks and Mitigations

- **Risk**: a question whose answer is a generic document (for example "what
  changed in 8.3.0", answered by CHANGELOG.md) gets worse. **Mitigation**:
  Step 8 adds such questions; the default turns on only if they hold (Step 9).
- **Risk**: the kept topic is the wrong one and still returns rows, so the
  answer is hidden. **Mitigation**: the coverage check, top-1 per collection
  rather than across collections, and the measured rate: 0 of 33 questions
  got worse, and recall rose. Accepted as a residual risk, watched by Step 9.
- **Risk**: in a routed collection above 0.9 coverage, unassigned chunks
  (up to a tenth) are not searched. **Mitigation**: the cache drop on every
  store and assignment write, `nx taxonomy drain` as the repair, and
  Step 5a's check of the threshold. Accepted as a residual risk.
- **Risk**: a filtered search under an approximate index returns fewer rows
  than asked: the centroid ANN filters one shared HNSW index by collection,
  and the topic search may walk a collection's `pci_` index. The planner,
  not RDR-227's router, chooses that index (`PciPlanCheck`), and the direct
  call forces a custom plan as the router does, so the exposure is the same
  as `search_topic_scoped`'s (Step 1). **Mitigation**: short rows did not change any score in
  the measurement (F0 equals Fk, `-13`); the zero-row fallback covers the
  empty case; Step 5a reports how often routed collections come back short
  against an exact scan.
- **Risk**: RDR-229 changes the taxonomy key. **Mitigation**: one lookup,
  adjusted by whichever RDR lands second.

### Failure Modes

- No chunks, no centroids, or coverage below 0.9: status `no_topics`,
  unscoped search.
- Kept topics return nothing: status `fallback`, unscoped search.
- Centroid query fails: the engine logs it and searches that collection
  unscoped; the request does not fail.

## Implementation Plan

### Prerequisites

- [x] nexus-n1908, empty-retrieval honesty (shipped).
- [x] Retrieval measurements (`-10`, `-13`).

### Minimum Viable Validation

On the cloud engine, the benchmark run through the engine route (Step 5a)
reproduces the production-set result within 0.03: RDR questions NDCG@3 at
least 0.44 against OFF 0.335, with no question worse by more than 0.1, and
request latency at most 1.5 times the unrouted request. If it misses on
quality, Step 6 runs; if it misses on latency, routing stays off and the
numbers go to Sam. If it misses on quality and Step 6 does not recover it,
the numbers, the coverage values and the short-row counts go to Sam with
routing left off.

### Phase 1: Engine route and client option

#### Step 1: Topic-set search functions

Three SQL functions, one per dimension, variants of
`search_topic_scoped_<dim>` that take a set of topic ids, with the plain
search's per-collection k, threshold and sensitivity exclusion, and a
recorded decision on whether they go through RDR-227's router. One
Liquibase changeset (the engine's schema-migration format). Tests on the
substrate (the real engine with PostgreSQL) first.

Decided at implementation (nexus-7fku2.1.1, changeset vectors-062, reviewed):
`search_topic_set_<dim>` takes `p_topic_ids bigint[]`, and an empty or NULL
set returns no rows. The threshold is not in SQL: the per-collection merge
applies it after the arm's top-k, as for the plain search, so `rawCount`,
`dropped` and Step 3's zero-row fallback share one basis. The functions are
called directly, as `search_topic_scoped` is, not through RDR-227's router.
The router does not choose the `pci_` index; the planner does. So the
short-row exposure equals the single-topic function's, and Step 5a measures
it. A direct call forgoes the router's exact-scan route for small sets, the
widest `ef_search` when a collection has no `pci_` index, and the
empty-result exact re-run. Step 3 decides whether the topic arm needs any of
them, and needs its own arm method, since the existing direct call
(`runCombinedQuery`) is not a fan-out arm.

#### Step 2: Coverage

A cached per-collection coverage ratio from
`taxonomy_coverage_counts_<dim>` (`taxonomy-022`; `taxonomy-023` counts
set-based), dropped as engine step 1
says. Tests:
an orphaned taxonomy (topics, no chunks) is never routed; a half-assigned
collection reads below 0.9; projection rows do not count as assigned; a
stored batch whose assignment is deferred lowers the cached ratio.

#### Step 3: `topic_route` on search-per-collection

Engine steps 1 to 6 above and the per-collection status in the response.
Tests: routed; `no_topics` (no centroids; low coverage); `fallback` (zero
rows); absent field gives today's response byte for byte.

#### Step 4: Field interactions and capability

The field table above, one test per row, and `vector_topic_route_supported`
in `GET /version`.

#### Step 5: Edge allowlist

Add `vector_topic_route_supported` to the public cloud edge's `/version`
allowlist (the conexus edge). Test: the cloud client reads the flag.

#### Step 5a: Client argument and benchmark arm

`topic_route` on `search_per_collection` and `search_cross_corpus`, read only
when the engine reports the capability; the client logs each response's
per-collection statuses as the structlog event `search_topic_route` (test:
one event per routed request); a benchmark arm that calls the engine route
and records latency, statuses, coverage values and short-row counts against
an exact scan. Run the Minimum Viable Validation.

#### Step 6: Only if the validation misses on quality

Rank centroids with a second, document-type query embedding per model group
and rerun Step 5a. Record the cost of the extra embedding.

### Phase 2: nx_answer wiring and default

#### Step 7: Plan runner wiring

The runner carries `topic_route` from `search.topic_route_nx_answer` to
`search` and `query` steps whose scope names no other domain and no topic
(amended 2026-11-19 from "with `taxonomy_domain: prose`" after the Step 9
probe), through the context variable described above. Default `off`. Tests: an end-to-end plan run
whose engine request carries the field; a code collection in a `corpus:
all` step is not routed; a catalog-route `query` step is not routed.

#### Step 8: Broaden the questions

Add at least ten questions whose answer is a generic document (a changelog,
a README, a CLI reference), labelled by hand, beside the existing title
questions.

#### Step 9: nx_answer A/B and default

The benchmark's Path B with the setting off and `top1`, on all questions,
within a spend cap Sam sets then. The default becomes `top1` only if NDCG@3
over all questions rises by at least 0.03 with more questions better than
worse, and the generic-answer questions lose no more than 0.02 on average.
The result is recorded in this RDR.

### Day 2 Operations

The per-collection status in each search response is logged by the client
(structlog event `search_topic_route`, Step 5a), so a collection with
topics that keeps reading `no_topics` or `fallback` shows in the logs.
Repairs: `nx taxonomy drain -c <collection>` for unassigned chunks,
`nx taxonomy rebuild -c <collection>` for stale topics, and `nx taxonomy
reset -c <collection>` for an orphaned taxonomy. Setting
`search.topic_route_nx_answer off` turns routing off without a release.

### New Dependencies

None.

## Test Plan

- Engine and edge: Steps 1 to 5, each with its named tests.
- Client: Step 5a (the argument reaches the request only with the
  capability; the log event) and Step 7 (end-to-end plan run; code and
  catalog-route steps not routed).
- Benchmark: Steps 5a and 9.

## Finalization Gate

### Contradiction Check

The stub's design (MiniLM centroids, a collection-level prefilter, the
client-side `topic=`) is replaced throughout. The global-band result is
stated as refuted in Key Discoveries and Alternative 2; the design ranks
topics only within a collection. The stub's Gap 3 (the `cluster_by`
docstring) is dropped: the tool's own description already says the default
is off, the one stale sentence is in `_search_render`
(`src/nexus/mcp/core.py:3451`), and it is unrelated to recall.

### Assumption Verification

Two critical assumptions are verified (`-10`, `-13`). Three are open, each
with the step that measures it: query-type ranking and latency (Minimum
Viable Validation, Step 5a), answer-level gain and generic-answer questions
(Steps 8 and 9).

#### API Verification

| API call | Library | Verification |
| --- | --- | --- |
| `HttpCentroidStore.ann_query` | nexus | Source read, `http_centroid_store.py:249`; exercised by the spike |
| `HttpVectorClient.search_topic_scoped` | nexus | Source read, `http_vector_client.py:4960`; exercised by the spike |
| `search_cross_corpus` | nexus | Source read, `search_engine.py:1423`; exercised as the production baseline (`-13`) |
| `/v1/vectors/search-per-collection` | nexus engine | Source read, `VectorHandler.java:279`; query embedder `PgVectorRepository.java:3117` |
| Voyage `input_type` | Voyage AI | Documentation (`-11`) |

### Scope Verification

The Minimum Viable Validation (the engine route reproduces the production-set
gain within the latency bound) is Step 5a of Phase 1 and is executed, not
deferred.

### Cross-Cutting Concerns

- **Versioning**: an optional request field and a `/version` capability.
- **Build tool compatibility**: none.
- **Licensing**: none.
- **Deployment model**: engine release, then client release; the client
  sends the field only to an engine that reports it, through the edge
  allowlist (Step 5).
- **IDE compatibility**: none.
- **Incremental adoption**: off by default; on only for `nx_answer` steps
  not scoped to another domain or a topic, after Step 9.
- **Secret/credential lifecycle**: none.
- **Memory management**: one cached coverage ratio per collection.

### Proportionality

Six SQL functions (the topic-set search and the coverage count, one of each
per dimension), a coverage cache over the count, one request field, one capability flag and its edge
allowlist entry, one setting carried like `chash_allow`, one log event, and
two benchmark scripts.
The design reuses the RDR-156 routes rather than adding an index.

## References

- RDR-070, RDR-075, RDR-156, RDR-179 (Phase 4), RDR-191, RDR-193, RDR-227,
  RDR-229, RDR-232, RDR-243.
- T2 `nexus_rdr/134-research-1` to `-14`; benchmark results in
  `bench/results/`: `rdr134-taxonomy-ab-leg1-2026-11-16.json`,
  `rdr134-taxonomy-ab-control-2026-11-16.json`,
  `rdr134-taxonomy-ab-prod-2026-11-16.json`.
- Voyage AI API reference, `input_type` (docs.voyageai.com/reference).
- Dehghankar, Asudeh, Mittal, Shetiya, Das. 2026. "Random-Access Ranked
  Retrieval and Similarity Search." Catalog tumbler `1.12.6`. Source of the
  band-selection idea; the measurement shows a band works within a
  collection and fails across collections.
- Beads: `nexus-9napz` (superseded by this RDR), `nexus-n1908`
  (prerequisite, closed).

## Revision History

- 2026-05-27: Stub.
- 2026-11-16: Rewritten against the PostgreSQL engine after the retrieval
  measurement (Sam: "Measure first", cap 5 USD); the design moved from a
  client-side collection prefilter to per-collection topic routing inside
  the engine's multi-collection search.
- 2026-11-16: Gate round 1 — PASSED (0 Critical, 7 Significant, 0 ship-blocker(s)); blob `e521660724c2`; critique `nexus_rdr/134-gate-critique-2026-11-16-r1`.
- 2026-11-16: Round-1 Significants fixed on Sam's "Fix all 7, fix check,
  re-gate" (production-set measurement `-13`, embedding type `-11`,
  orphaned-taxonomy cause `-12`): coverage check, zero-row fallback, default
  scoped to `nx_answer` prose plans, field-interaction table, capability
  flag, steps split, Gap 3 dropped.
- 2026-11-16: Fix-check findings on the round-1 fixes (T2
  `nexus_rdr/134-fix-check-122803ad5cd7`, research `-14`): coverage defined
  over `taxonomy_unassigned_chashes_<dim>` with refresh on deferred
  assignments, the runner transport and edge allowlist named, the log event
  given a step, counts and citations corrected.
- 2026-11-16: Gate round 2 — PASSED (0 Critical, 2 Significant, 0 ship-blocker(s)); blob `48d312ee05a5`; critique `nexus_rdr/134-gate-critique-2026-11-16-r2`.
- 2026-11-16: Step 2 as built (nexus-7fku2.1.2, reviewed): coverage comes
  from new count functions `taxonomy_coverage_counts_<dim>` (`taxonomy-022`)
  over live chunks, because `taxonomy_unassigned_chashes_<dim>` returns
  capped pages with no count; the cache is dropped by every engine write
  route that changes a count input, with a ten-minute ceiling for the rest.
- 2026-11-17: Step 2 count made set-based (`taxonomy-023`, nexus-7fku2.1.2
  develop red): with no planner statistics on the manifest, `taxonomy-022`'s
  per-chunk owner probe read the collection's whole manifest per chunk and
  ran past the 30 s bound; the new bodies count by one aggregation.
- 2026-11-19: Step 7 routing no longer needs a prose-scoped plan (Sam,
  option b after the Step 9 probe, `134-research-17`): the probe's question
  matched `hybrid-factual-lookup`, which declares no scope, and its routed
  arm sent no route. A step now routes unless its scope names another domain
  or a topic; prose-only routing of collections stays in the search layer.
- 2026-11-19: Step 9's primary ordering (pre-declared in the A/B driver as
  raw carried distance) is now calibrated distance, by Sam's choice after the
  first partial run: on a mixed code and docs envelope, raw distance put
  every voyage-code-3 row ahead of every voyage-context-3 row in both arms
  (`134-research-17`). Raw distance and envelope order are printed, not
  decided on. The run is redone after the client's calibrated pool cut and
  the plan reseed.
