---
title: "RDR-070 Phase 5: Taxonomy-Aware Recall in nx_answer — Teach the Composed-Retrieval Path to Read the Taxonomy It Already Has"
id: RDR-134
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-27
accepted_date:
related_issues: [nexus-9napz, nexus-n1908]
related_rdrs: [RDR-070, RDR-075, RDR-080, RDR-156, RDR-179, RDR-193, RDR-229, RDR-232]
related_tests: []
implementation_notes: ""
---

# RDR-134: RDR-070 Phase 5 — Taxonomy-Aware Recall in nx_answer

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

This RDR was a stub from 2026-05-27, written when the taxonomy lived in
ChromaDB. It was rewritten on 2026-11-16 against the PostgreSQL engine, after
a measurement Sam asked for before the gate ("Measure first"). The measurement
changed the design: see Research Findings, 134-research-10.

## Problem Statement

Nexus builds a taxonomy of every indexed collection (RDR-070). A collection's
chunks are clustered into topics; each topic has a label and a centroid (the
mean embedding of its chunks), and each chunk has a topic assignment. This
work runs on every index and every store. The retrieval path never reads it.
`nx_answer`, the analytical entry point, runs plans whose `search` steps fan
out over every collection in a corpus (for prose: every `knowledge`, `docs`,
`rdr` and `paper` collection) and merge the hits by vector distance.

The measured cost of that blind fan-out is large. On 26 benchmark questions
whose answers are known RDRs, searching the one collection that holds the
RDRs scores NDCG@3 0.591. Searching 38 prose collections the way `nx_answer`
does scores 0.322. (NDCG@3 is a 0 to 1 score of how well the top three
results match the known answers.) The right RDR is usually still in the
candidates; it is pushed out of the top three by generic documents
(CHANGELOG.md, cli-reference.md, architecture.md, README.md) that are close
to many questions and live in other collections.

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
`src/nexus/db/t2/http_centroid_store.py:249`; its only caller is chunk
assignment). The engine can search inside one topic of one collection
(`search_topic_scoped`, `src/nexus/db/http_vector_client.py:4960`). The
client-side `topic=` argument of `search_cross_corpus` is different: it
filters the results after an unscoped search
(`src/nexus/search_engine.py:2548-2550`), so it can only remove hits, never
reach chunks the unscoped top-k missed.

#### Gap 3: clustered search is off at the MCP surface, and its docstring says otherwise

`search_cross_corpus` defaults `cluster_by` to `"semantic"`
(`_CLUSTER_DEFAULT`, `src/nexus/search_engine.py:1407`). The MCP `search`
tool defaults it to `""` (`src/nexus/mcp/core.py:3969-3971`) while its
docstring says the default is semantic (RDR-179, finding 179-R24 item 5).

## Relationship to Prior RDRs

- RDR-070 (closed) built discovery, assignment and centroids. This RDR is its
  missing consumer.
- RDR-156 (closed) built the engine routes this design reuses: centroid ANN
  (approximate nearest neighbour) search and topic-scoped search.
- RDR-179 (closed), Phase 4, sequences this RDR and asks for a taxonomy
  on/off leg in the retrieval benchmark (Phase 4.2). This RDR adds that leg
  (`scripts/bench/taxonomy_ab.py`).
- RDR-232 (closed) narrows `nx_answer` by owner and corpus before search.
  Topic routing runs inside the collections that narrowing leaves; the two
  compose.
- RDR-193 (accepted) moves taxonomy discovery onto the engine. That keeps
  topics fresher; it does not change the routes used here.
- RDR-229 (draft) stops using the collection name as the taxonomy key. The
  route here asks for a collection's topics by whatever key the taxonomy
  tables use; if RDR-229 lands first, the route follows its key.

## Context

### Background

The 2026-05-27 stub came from an `nx_answer` question about RDR-070 that
returned a summary of the session's startup text instead (nexus-n1908). That
honesty bug is fixed: `_nx_answer_is_empty_retrieval`
(`src/nexus/mcp/core.py:11116`) returns an explicit no-match when the
retrieval steps found nothing. A topic route that finds nothing therefore
cannot make `nx_answer` invent an answer; it can only make retrieval worse,
which the benchmark measures.

Sam's decisions, 2026-11-16: "would truly love to get RDR-134, 171 done";
then "Measure first", with a spend cap of 5 USD, confirmed in session baton.
If topics had not helped, the result would have gone to Sam instead of a
gate.

### Technical Environment

- Taxonomy tables live in the engine's PostgreSQL database: `topics`,
  `topic_assignments`, `taxonomy_centroids` (one table since RDR-191,
  `taxonomy-007-unify-centroids.xml`). A centroid is stored in its
  collection's own embedding space: 1024 dimensions for Voyage
  (voyage-context-3 for prose, voyage-code-3 for code) or 768 for the local
  bge model. Discovery clusters stored embeddings and never re-embeds
  (`src/nexus/commands/taxonomy_cmd.py:306-317`).
- Assignment runs on every stored batch (`taxonomy_assign_batch_hook`,
  `src/nexus/mcp_infra.py:2511`); discovery runs after `nx index`.
- The prose search path: `search` tool → `search_cross_corpus` →
  `HttpVectorClient.search_per_collection` (`src/nexus/search_engine.py:2111`)
  → `POST /v1/vectors/search-per-collection`
  (`service/.../http/VectorHandler.java:279`). One request covers a group of
  collections; the engine searches each and merges.
- The engine embeds query text itself (`PgVectorRepository`,
  `service/.../vectors/PgVectorRepository.java:367-369`); no query-embedding
  cache was found.
- `search-topic-scoped` refuses a `chash_allow` list
  (`VectorHandler.java:1058-1063`) and takes no metadata `where`.

## Research Findings

### Investigation

Drift survey of the stub against develop (2026-11-16): T2
`nexus_rdr/134-research-1` to `-8`. Measurement: `134-research-9` (the pass
rule, written before the run) and `134-research-10` (the result).

The measurement is a retrieval-only paired comparison, run on the cloud
engine (release 0.1.194) with `scripts/bench/taxonomy_ab.py`. Questions: the
benchmark sets `spike_5q`, `golden_v1` and `golden_heldout_v1`, keeping the
26 distinct questions whose required sources are RDRs (the answer key grades
the named RDR 3). Two collection sets: S1 is the nexus RDR collection alone;
S2 is all 38 `rdr`, `docs` and `knowledge` collections with topics. Arms:

- OFF: each collection searched, hits merged by distance.
- ON, global band: rank every collection's topic centroids against the query;
  keep topics within 0.03 or 0.06 of the best similarity across all
  collections; search inside each kept topic; merge.
- ON, per collection: keep each collection's best topic (top-1), or its
  topics within 0.03 of that collection's own best (own band); search inside
  them; merge.

Spend: Voyage query embeddings only, under 0.10 USD. No language model was
called.

### Key Discoveries

- **Verified** (134-research-10): per-collection routing raises S2 NDCG@3
  from 0.322 to 0.488 (top-1, +0.166, 10 questions better, 0 worse) or 0.486
  (own band, +0.164, 10 better, 1 worse). Recall@10 (share of the grade-3
  RDRs among the top ten documents) rises from 0.811 to 0.830 and 0.843.
  This passes the rule declared in 134-research-9.
- **Verified**: the gain comes from removing generic documents. In the
  questions that improved, the OFF top three usually held CHANGELOG.md,
  cli-reference.md, architecture.md, README.md or SKILL.md, and the ON top
  three held the RDR.
- **Verified**: centroid similarity cannot be compared across collections.
  The global band scores 0.140 and 0.205 (−0.18 and −0.12) and loses about
  half the recall. For the tumbler question, generic `knowledge__1-1` topics
  score 0.60 against the query while the right `rdr__1-1` topic scores
  0.49. Choosing collections by centroid similarity is therefore refuted;
  this RDR routes within each collection and never drops a collection on
  similarity.
- **Verified**: inside the single RDR collection (S1) routing helps little:
  +0.021 to +0.030 NDCG@3. The value is in the cross-collection fan-out,
  which is what `nx_answer` runs.
- **Verified**: some topics return nothing. All 21 topics of
  `knowledge__1-1` (marked "[no projection]" by `nx taxonomy status`)
  returned zero rows from `search_topic_scoped` whenever chosen, 170 calls in
  all. A control run of OFF without that collection scored 0.322, identical
  to OFF, so the gain is not an artefact of losing it. Routing must fall
  back to an unscoped search for such a collection.
- **Verified**: the spike's shape is slow. Run as sequential client calls,
  per-collection routing took 45 s per question against 16 s for OFF (38
  centroid lookups and 37 topic searches on top). A client that first
  fetches the query vector and then searches makes the engine embed the
  query twice. The design below moves routing into the engine's existing
  multi-collection request.
- **Documented** (RDR-179, Phase 4.1): RDR-179 resolved the stub's
  granularity question (topic or collection) to per-topic. The measurement
  agrees and adds that topics must be ranked within their own collection.

### Critical Assumptions

- [x] Topic routing improves cross-collection retrieval — **Status**:
  Verified (134-research-10) — **Method**: Spike.
- [x] Per-topic centroids are enough; no per-collection summary embedding
  is needed — **Status**: Verified (134-research-10: the winning arms use
  only topic centroids) — **Method**: Spike.
- [ ] The retrieval gain carries through to `nx_answer`'s answers —
  **Status**: Unverified — **Method**: the benchmark's `nx_answer` path
  (Path B) with routing on and off, Step 6. Why later: `nx_answer`'s prose
  retrieval is the search measured here, so the retrieval result is its
  direct input; the answer-level run costs model calls and is cheap once the
  engine route exists.
- [ ] One routed engine request costs at most 1.5 times the latency of the
  same request unrouted — **Status**: Unverified — **Method**: the Minimum
  Viable Validation. The spike's 45 s figure is sequential client calls and
  does not answer this.
- [ ] The result holds beyond RDR-answer questions — **Status**: Unverified
  — **Method**: Steps 5 and 6. The answer key names only RDRs, which favours
  removing generic documents.

## Proposed Solution

### Approach

Add topic routing to the engine's multi-collection search, behind a request
field. For each collection in the request, the engine ranks that
collection's topic centroids against the query vector it already computed,
keeps the best topic (or the topics within a small band below that
collection's own best), searches only inside them, and falls back to an
unscoped search of the collection when the collection has no topics or the
kept topics return fewer hits than requested. Results merge exactly as
today. `search_cross_corpus` sends the field when a setting or a caller asks
for it; `nx_answer`'s plans inherit it through their `search` steps. The
default stays off until the answer-level benchmark (Step 6), then turns on.

### Technical Design

**Request.** `POST /v1/vectors/search-per-collection` gains an optional
field `topic_route`: `{"mode": "top1" | "band", "width": <float, band
only>}`. Absent means today's behaviour, byte for byte.

**Engine, per collection, when `topic_route` is present:**

1. Use the query vector the request already computes for this collection's
   embedding model. No second embedding.
2. Rank the collection's centroids with the existing centroid ANN query
   (`taxonomy_ann_query_<dim>`), 10 nearest.
3. Keep topic ids: the nearest (`top1`), or all within `width` of the
   nearest's similarity (`band`). Similarity is compared only within the
   collection.
4. Search the collection's chunks restricted to chunks assigned to the kept
   topics (the join `search_topic_scoped_<dim>` already does, extended from
   one label to a set of topic ids), with the same per-collection k,
   threshold, sensitivity exclusion and live-chunk filter as the unscoped
   search.
5. Fallback: if the collection has no centroids, or step 4 returns fewer
   rows than the per-collection k, run the unscoped search for that
   collection and use its rows. The response records, per collection,
   `routed`, `fallback` or `no_topics`.

Merging, threshold and reranking across collections stay as they are.

**Where routing is skipped.** A request with `chash_allow` (document
confinement, RDR-232) or a metadata `where` is searched unscoped, because
the topic join supports neither; the response says `skipped`. Code
collections are routed the same way when a caller asks, but `nx_answer`'s
prose domain does not include them.

**Client.** `HttpVectorClient.search_per_collection` and
`search_cross_corpus` gain a `topic_route` argument. A setting
`search.topic_route` (`off` | `top1` | `band:<width>`) supplies the default;
the MCP `search` tool and `nx search` gain the same option. The plan runner
needs no change: `search` steps pass through `search_cross_corpus`. A client
sends the field only to an engine that advertises the capability.

**Gap 3.** The MCP `search` tool's `cluster_by` default stays `""` (it is a
display choice, and the topic route does not depend on it); its docstring
is corrected to say so.

**Observability.** The per-collection routing status goes into search
telemetry, so a collection that always falls back (as `knowledge__1-1` does
today) can be listed by `nx taxonomy status`.

### Existing Infrastructure Audit

| Proposed component | Existing module | Decision |
| --- | --- | --- |
| Query vector | Engine query embedder in the search request | Reuse |
| Centroid ranking | `taxonomy_ann_query_<dim>`, `TaxonomyCentroidRepository.annQuery` | Reuse |
| Topic-restricted search | `search_topic_scoped_<dim>` (`catalog-006-combined-query-functions.xml`) | Extend: a set of topic ids instead of one label |
| Multi-collection request and merge | `/v1/vectors/search-per-collection` | Extend with `topic_route` |
| Benchmark leg | `scripts/bench/taxonomy_ab.py` | New (RDR-179 Phase 4.2) |

### Decision Rationale

Routing inside the engine's existing request keeps one embedding per query
and lets the engine search collections as it does today, which is the only
way the latency can approach the unrouted search. Ranking topics within each
collection is the variant the measurement supports; ranking across
collections is the one it refutes. The fallback makes a stale or missing
taxonomy cost nothing worse than today's search.

## Alternatives Considered

### Alternative 1: Route on the client with the existing routes

**Description**: `search_cross_corpus` embeds the query, calls centroid ANN
per collection, then `search_topic_scoped` per kept topic.

**Pros**: no engine change; it is what the spike does.

**Cons**: the query is embedded again by every topic search; one round trip
per collection and per topic; measured 45 s per question against 16 s.

**Reason for rejection**: latency. It stays as the benchmark's reference
implementation.

### Alternative 2: Route by collection, not by topic

**Description**: rank collections by their best centroid similarity and
search only the top collections.

**Pros**: fewer searches.

**Cons**: centroid similarity is not comparable across collections; the
measured global band lost 0.12 to 0.18 NDCG@3 and about half the recall.

**Reason for rejection**: refuted by 134-research-10.

### Alternative 3: Use the client-side `topic=` filter

**Description**: pick a topic and pass it as `topic=` to
`search_cross_corpus`.

**Pros**: already wired through the plan runner.

**Cons**: it filters after an unscoped top-k, so it cannot recover a buried
result; it takes one label for all collections, and labels are per
collection.

**Reason for rejection**: wrong mechanism for recall.

### Briefly Rejected

- Build per-collection summary embeddings: not needed; topic centroids carry
  the signal.
- Rerank instead of route: reranking reorders the merged candidates but
  cannot bring in an RDR that generic documents kept out of each
  collection's k.

## Trade-offs

### Consequences

- Prose search across many collections ranks specific documents above
  generic ones.
- A collection's taxonomy becomes part of retrieval quality, so stale topics
  now matter; the fallback limits the damage to "no better than today".
- Each routed request does one centroid query per collection inside the
  engine.

### Risks and Mitigations

- **Risk**: a question whose answer is a generic document (for example
  "what changed in 8.3.0", answered by CHANGELOG.md) gets worse.
  **Mitigation**: Step 5 adds such questions to the benchmark; routing turns
  on by default only if they hold (Step 6).
- **Risk**: a topic-restricted search under an HNSW index (the approximate
  vector index) returns fewer rows than asked, because filtering happens
  during an approximate search. **Mitigation**: the fallback to the unscoped
  search when rows are short; Step 4 measures how often it fires.
- **Risk**: RDR-229 changes the taxonomy key. **Mitigation**: the route
  looks topics up by the same key the taxonomy tables use; whichever RDR
  lands second adjusts one lookup.

### Failure Modes

- Taxonomy missing or stale for a collection: fallback, status `no_topics`
  or `fallback`; visible in telemetry.
- Centroid query fails: the engine logs it and searches that collection
  unscoped; the request does not fail.

## Implementation Plan

### Prerequisites

- [x] nexus-n1908, empty-retrieval honesty (shipped).
- [x] Retrieval measurement (134-research-10).

### Minimum Viable Validation

On the cloud engine, `taxonomy_ab.py` run through the new engine route
(instead of client calls) gives S2 NDCG@3 within 0.02 of 0.488 for `top1`,
with request latency at most 1.5 times the unrouted request.

### Phase 1: Engine route and client option

#### Step 1: Topic-set search function

A SQL function variant of `search_topic_scoped_<dim>` that takes a set of
topic ids, with the per-collection k, threshold and sensitivity exclusion of
the plain search. Liquibase changeset; tests on the substrate first.

#### Step 2: `topic_route` on search-per-collection

Engine steps 1 to 5 above, the per-collection status in the response, the
skip rules for `chash_allow` and `where`, and the capability flag. Tests:
routed, fallback (no centroids; short rows), skipped, and absent-field byte
identity.

#### Step 3: Client and setting

`topic_route` on `search_per_collection` and `search_cross_corpus`; the
`search.topic_route` setting (default `off`); the option on the MCP `search`
tool and `nx search`; the `cluster_by` docstring fix (Gap 3).

#### Step 4: Benchmark through the route

`taxonomy_ab.py` gains an arm that calls the engine route and records
latency and the fallback rate; run the Minimum Viable Validation.

### Phase 2: Answer-level check and default

#### Step 5: Broaden the questions

Add at least ten questions whose answer is a guide, a changelog or a paper,
labelled by hand.

#### Step 6: nx_answer A/B

The benchmark's Path B with `search.topic_route` off and `top1`, on the
broadened set, within a spend cap Sam sets then. Routing turns on by default
only if NDCG@3 on the full set rises and the generic-answer questions lose no
more than 0.02.

#### Step 7: Default on

Set the default to `top1` (or `band` if Step 6 favours it), with the result
recorded in this RDR.

### Day 2 Operations

`nx taxonomy status` lists collections whose routed searches fall back, from
telemetry; `nx taxonomy rebuild <collection>` is the repair. The setting
`search.topic_route off` turns routing off without a release.

### New Dependencies

None.

## Test Plan

- Engine: routed search restricted to kept topics; fallback on no centroids
  and on short rows; skip on `chash_allow` and `where`; sensitivity exclusion
  inside routed search; absent `topic_route` gives today's response.
- Client: the setting and the argument reach the request; the capability
  check; the MCP tool and CLI option.
- Benchmark: Steps 4 and 6.

## Finalization Gate

### Contradiction Check

The stub's design (MiniLM centroids, a collection-level prefilter, the
client-side `topic=`) is replaced throughout. The global-band result is
stated as refuted in Key Discoveries and Alternative 2, and the design ranks
topics only within a collection.

### Assumption Verification

Two critical assumptions are verified by the spike (134-research-10). Three
are open and each has a step that measures it: answer-level gain (Step 6),
latency (Minimum Viable Validation), generality beyond RDR answers (Steps 5
and 6).

#### API Verification

| API call | Library | Verification |
| --- | --- | --- |
| `HttpCentroidStore.ann_query` | nexus | Source read, `http_centroid_store.py:249`; exercised by the spike |
| `HttpVectorClient.search_topic_scoped` | nexus | Source read, `http_vector_client.py:4960`; exercised by the spike |
| `HttpVectorClient.embed_for_collection` | nexus | Source read, `http_vector_client.py:6736`; exercised by the spike |
| `/v1/vectors/search-per-collection` | nexus engine | Source read, `VectorHandler.java:279` |

### Scope Verification

The Minimum Viable Validation (the route reproduces the spike's gain within
the latency bound) is Step 4 of Phase 1 and is executed, not deferred.

### Cross-Cutting Concerns

- **Versioning**: an optional request field; absent keeps the old behaviour.
- **Build tool compatibility**: none.
- **Licensing**: none.
- **Deployment model**: engine release, then client release; the client
  sends the field only to an engine that advertises it.
- **IDE compatibility**: none.
- **Incremental adoption**: off by default until Step 6.
- **Secret/credential lifecycle**: none.
- **Memory management**: none beyond one centroid query per collection.

### Proportionality

One SQL function variant, one request field, one setting, one benchmark
leg. The design reuses the RDR-156 routes rather than adding an index.

## References

- RDR-070, RDR-156, RDR-179 (Phase 4), RDR-193, RDR-229, RDR-232.
- T2 `nexus_rdr/134-research-1` to `-10`; benchmark results
  `bench/results/rdr134-taxonomy-ab-leg1-2026-11-16.json` and
  `bench/results/rdr134-taxonomy-ab-control-2026-11-16.json`.
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
