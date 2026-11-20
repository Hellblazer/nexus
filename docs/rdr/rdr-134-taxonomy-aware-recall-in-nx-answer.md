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
non-empty ones and no `paper` collection) and merge the hits. The engine's merge keeps the rows nearest by
vector distance (`PgVectorRepository.java:2335-2339`); the callers then re-sort what comes back by
`hybrid_score`, which adds boosts (`search_engine.py:2831-2833`). Querying every collection and merging is called
the fan-out below.

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
`src/nexus/plans/runner.py:1526`), but nothing chooses one from the
question.

#### Gap 2: no query-time topic selection, and the existing topic filter loses recall

Two pieces exist and are not composed. The engine can rank a collection's
topic centroids against a vector (`HttpCentroidStore.ann_query`,
`src/nexus/db/t2/http_centroid_store.py:249`; ANN is approximate nearest
neighbour search; nothing on the query path calls it). The engine can search
inside one topic of one collection (`search_topic_scoped`,
`src/nexus/db/http_vector_client.py:5025`). The client-side `topic=` argument
of `search_cross_corpus` is different: it builds a set of the topic's document
ids (`src/nexus/search_engine.py:1623-1636`) and filters the results with it
after an unscoped search (`:2603-2605`), so it can only remove hits, never
reach chunks the unscoped search missed.

### Terms used below

- **Routing**: for each collection, choosing topics from the question and
  searching only the chunks assigned to them.
- **Top-1 / own band**: keep a collection's single nearest topic, or every
  topic whose similarity is within a width (a similarity difference, 0.03 in
  the measurements) of that collection's nearest topic.
- **Per-collection k**: how many rows the engine takes from each collection
  before merging (`search_engine.py:1196-1228`).
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
  path (the router is in `runPlainSearchStatement`, `PgVectorRepository.java:1677-1682`
  and `:1781`); the topic-scoped functions are called directly, not through it
  (`searchTopicScopedWithTokens` passes its function to `runCombinedQuery`,
  `PgVectorRepository.java:5397-5406`, `:5686-5703`, which sets its own session settings and
  runs no router). Step 1 calls the new topic-set functions
  directly too (vectors-062); see Step 1 and Risks.
- RDR-232 (closed) narrows `nx_answer` by owner, corpus and subtree before
  search, and can confine a search with `chash_allow`. Routing runs inside
  the collections that narrowing leaves; a confined search is not routed.
- RDR-193 (closed 2026-11-16, `docs/rdr/rdr-193*.md` frontmatter) moves
  taxonomy discovery onto the engine. That keeps topics fresher; it does not
  change the routes used here.
- RDR-229 (accepted 2026-11-19, `docs/rdr/rdr-229*.md` frontmatter) retires the
  collection name as the key: its revision gives each collection a surrogate
  `collection_id` and turns the name into a label
  (`docs/rdr/rdr-229-*.md:58`; accepted in commit d38ed9b2a). It does not mention this RDR (a text search of its file for
  `RDR-134`, `topic_route`, `search_topic_set` and `taxonomy_coverage` found
  nothing). This RDR's new code keys by collection name in five places, so
  whichever RDR lands second adjusts all five: the six SQL functions take a collection name
  (`taxonomy_coverage_counts_<dim>(p_collection text)`,
  `taxonomy-023-coverage-counts-set-based.xml:33`, with `ct.collection = p_collection` at `:41`;
  `search_topic_set_<dim>(... p_collection text ...)`, `vectors-062-topic-set-search.xml:74`,
  with `c.collection = p_collection` at `:87` and `t.collection = p_collection` at `:96`); the coverage cache
  key is `(tenant, collection)` (`TaxonomyCoverage.java:103`); the centroid ranking is
  asked for by collection name (`TopicRouteArm.java:154`, `annQuery(tenant, qv, collection, ...)`); the
  request's `topic_route.collections` lists collection names (`TopicRoute.java:20-23`); and the
  engine's statuses are reported by collection (`VectorHandler.java:817-818`). The order in which
  the two land is not decided here: RDR-229 does not name this RDR, so the
  engine route's keys and the surrogate-key phases have no recorded
  sequencing (inferred from the text search above, not read in a decision).

## Context

### Background

The 2026-05-27 stub came from an `nx_answer` question about RDR-070 that
returned a summary of the session's startup text instead (nexus-n1908). That
honesty bug is fixed: `_nx_answer_is_empty_retrieval`
(`src/nexus/mcp/core.py:11202`) returns an explicit no-match when the
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
  `HttpVectorClient.search_per_collection` (called at `src/nexus/search_engine.py:2157`)
  → `POST /v1/vectors/search-per-collection`
  (`service/.../http/VectorHandler.java:319`, handler at `:916`). One request covers a group of
  collections that share an embedding model; the engine embeds the query once
  (`PgVectorRepository.java:3241`) with the query-type embedder for prose
  (voyage-context-3); the code and other Voyage models are embedded without
  an input type (134-research-11). It searches each collection and merges.
- `search-topic-scoped` refuses a `chash_allow` list
  (`VectorHandler.java:1177`, `:3093-3098`) and takes no metadata `where`
  (its handler reads `query`, `topic`, `collection` and `n_results`,
  `:1172-1187`).

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

These are findings of the client-side spike (`taxonomy_ab.py`,
`taxonomy_ab2.py`: the client ranks the centroids and calls the topic search
itself, collection by collection) on the cloud engine 0.1.194. They are
in-sample: the 25 RDR questions are among leg 1's 26, and the design was chosen
on them. The engine route did not reproduce the gain (`-18`) and the
`nx_answer` A/B failed (`-17`); see Results. Read "Measured" below as
measured in the spike, not as established for the shipped route.

- **Measured** (`-13`): on the production set, top-1 routing raises NDCG@3 on
  the RDR questions from 0.335 to 0.470 (+0.135; 9 better, 0 worse) with
  recall@10 (share of the grade-3 sources among the top ten documents) 0.817
  to 0.823. Title questions: 0.500 to 0.538 (1 better, 0 worse). All 33:
  0.375 to 0.487, 10 better, 0 worse.
- **Verified** (`-13`): the production call `search_cross_corpus`, its rows
  ordered by distance, scores exactly the OFF arm. Unordered, as it returns
  them, the rows score 0.02: `search_cross_corpus` returns the rows grouped by
  embedding model and then by collection, not by distance
  (`search_engine.py:2342-2351`). Callers re-sort by `hybrid_score`
  (`search_engine.py:2831-2833`), which adds boosts, so OFF is the
  production baseline without boosts; Step 9 measures the full path.
- **Measured** (`-10`): leg 1, also client-side and in-sample, saw the same
  effect on the 38 collections with topics: 0.322 to 0.488 (top-1, 10 better, 0 worse) and 0.486 (own band, 10
  better, 1 worse). `-10` and `-16` report the uncapped top-1 gain, +0.166; with each question's
  NDCG capped at 1 it is 0.155 (0.322 to 0.477), recomputed from the question
  scores in `bench/results/rdr134-taxonomy-ab-leg1-2026-11-16.json` (`-10`
  says only that NDCG can exceed 1). The own band was
  measured at width 0.03 only, and in leg 1 only (the production-set arms were
  P0, OFF and top-1); in the gated rerun (`-16`) its worst question fell
  0.3066. The engine's `band` mode (`TopicRouteArm.java:199-202`) was not run
  against the engine in either engine measurement: the Minimum Viable
  Validation sent `top1` (`scripts/bench/topic_route_engine_arm.py:48`) and the
  Step 9 arms are `off` and `top1` (`scripts/bench/topic_route_nx_answer_ab.py:63`).
  Band mode is unvalidated.
- **Measured** (client-side, in-sample): the gain comes from removing generic
  documents. In most improved questions (8 of 10) the OFF top three held CHANGELOG.md, cli-reference.md,
  architecture.md, README.md or SKILL.md and the routed top three held the
  answer. The same mechanism cuts the other way when the answer is a generic
  document: in Step 9, routing in `nx_answer` pushed the labelled generic
  document out of the top three on three of the 15 generic questions (GD01,
  GD12, GD14; see Results).
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
- **Measured** (`-13`, client-side): falling back on short rows did not erode the gain.
  No kept topic returned zero rows, so the zero-row fallback (F0) never
  fired; falling back when a topic returned fewer than 20 rows (Fk) fired on
  about 11 of 36 routed collections per question and gave identical scores.
  Production's per-collection k for prose is 40 at ten results
  (`search_engine.py:901-909`, `:1196-1228`), so 20 is a looser bound than production's.
- **Verified** (`-12`): a taxonomy can outlive its chunks.
  `knowledge__1-1` holds 0 chunks (its rows were quarantined and then
  expired, `-14`) but keeps 21 topics, their centroids and assignments, so every
  topic search there returns nothing (leg 1 hit 16 of the 21). Topics are
  therefore not proof that a collection has assigned live chunks.
- **Verified** (`-13`), unrouted only: one `search_cross_corpus` call
  answered in 2.5 s per question, measured from the client; the same 123
  searches made one by one from the client took 42 s. Most of the spike's
  latency is therefore client serialisation. The 2.5 s is the call's time at
  the client, not time spent inside the engine. The routed engine cost was
  measured later (see Results).
- **Documented** (`-11`): Voyage's query and document input types differ
  only by a prepended prompt; its documentation says embeddings made with and
  without an input type are compatible and recommends the query type for
  queries and the document type for documents. Centroids
  are means of document-type chunk embeddings, so ranking them with the
  search's query-type vector follows that recommendation (inferred, not
  measured). This applies to prose; code collections embed without an input
  type and are not routed. The spike ranked them
  with a document-type vector, because the only client embedding route
  (`embed_for_collection`, `http_vector_client.py:6801`) embeds as a
  document.

### Critical Assumptions

Status as of the close-out (2026-11-19). At the gate the first two were
ticked as verified and the other three were open; the measurements in Results
changed all five.

- [ ] Topic routing improves cross-collection retrieval — **Status**: held in
  the client-side spike only, and in-sample (the 25 RDR questions are among
  leg 1's 26; `-10`, `-13`). The engine route did not reproduce it: 0.385
  against the 0.44 bar (`-18`). The `nx_answer` A/B failed (`-17`). —
  **Method**: Spike, then the Minimum Viable Validation and Step 9.
- [ ] Per-topic centroids are enough; no per-collection summary embedding is
  needed — **Status**: held in the client-side spike only, in-sample (`-10`,
  `-13`); the engine route's shortfall against the spike is unexplained (see
  Results). — **Method**: Spike.
- [ ] Ranking centroids with the query-type vector does at least as well as
  the document-type vector the spike used — **Status**: Documented (`-11`),
  not measured; Step 6, which would have measured it, was not run.
- [ ] One routed engine request costs at most 1.5 times the latency of the
  same request unrouted — **Status**: measured, not met as measured: 1.66
  times (`-18`), a figure that does not isolate the cost of routing (see
  Results). — **Method**: Minimum Viable Validation.
- [ ] The gain carries through to `nx_answer`'s answers and holds for
  questions whose answer is a generic document — **Status**: measured, not
  met: the 15 generic questions lost 0.158 NDCG@3 on a partial run that Sam
  accepted as a FAIL (`-17`). — **Method**: Steps 8 and 9.

## Proposed Solution

### Approach

Add topic routing to the engine's multi-collection search, behind a request
field. For each collection in the request that has enough topic coverage, the
engine ranks that collection's topic centroids against the query vector it
already computed, keeps the nearest topic (or an own band), searches only
inside the kept topics, and falls back to the collection's unscoped search
when they return nothing. Results merge exactly as today. With
`search.topic_route_nx_answer` on, `nx_answer`'s `search` and `query` steps
send the field, except steps scoped to another domain or a topic; nothing else
does until it is measured. The setting is off and stays off (see Results), so
as shipped nothing sends the field.

### Technical Design

**Request.** `POST /v1/vectors/search-per-collection` gains an optional
field `topic_route`: `{"mode": "top1" | "band", "width": <similarity
difference, band only>, "min_coverage": <0 to 1, optional>, "collections":
[<names>]}` (`TopicRoute.java:17-23`). Absent means today's behaviour, byte
for byte. The first draft of this design had a request-wide field with `mode`
and `width` only; as built, the field has two more members:

- `collections` is a carrier that says which collections of the request are
  routed. The engine reads an absent (or JSON null) `collections` as "route
  every collection of the request", and an empty list as "route none", with a
  response identical to a request without `topic_route` (`TopicRoute.java:26-29`,
  `:195-197`; `PgVectorRepository.java:3141-3142`). A collection the list does not name is searched unscoped
  with status `skipped`; every name must also be in the request's own
  `collections`, or the request is a 400 (`TopicRoute.java:134-191`). The carrier exists
  because on a local bge install code and docs collections share one model
  group, so a request-wide field could not keep code unrouted
  (`TopicRoute.java:22-23`). The client always sends an explicit list of the
  request's prose-prefix names (`knowledge__`, `docs__`, `rdr__`, `paper__`),
  or no field at all when none is eligible (`src/nexus/topic_route.py:33`,
  `:89-123`; `src/nexus/db/http_vector_client.py:4724`, `:4758-4759`).
  The engine also refuses to route a collection whose registered content type
  is `code`, whether or not the list names it (`PgVectorRepository.java:3350`,
  `:590-592`).
- `min_coverage` replaces the fixed 0.9 with a per-request value; absent
  means the engine default 0.9 (`TopicRoute.java:17-20`).

**Engine, per collection, when `topic_route` is present:**

1. Coverage check: route the collection only if it has centroids, has
   chunks, and its coverage is at least `min_coverage`, which is 0.9 unless
   the request sets another value (`TaxonomyCoverage.java:75`,
   `TopicRoute.java:54`, `TopicRouteArm.java:147`). Coverage comes from
   `taxonomy_coverage_counts_<dim>` (`taxonomy-022`, bodies replaced by
   `taxonomy-023`): one row of
   (has_taxonomy, chunks, unassigned) over the collection's live chunks, the
   same live-chunk filter the plain search applies. The reading is cached
   per tenant and collection. The engine, not the client, invalidates the
   cache: a route that stores chunks or changes an input of the count (the
   manifest, a document's tombstone, an assignment, a topic, a centroid)
   drops that collection's entry after it commits, and the next read
   recounts (`TaxonomyCoverage.java:32-39`). The client does not signal the
   engine, because the engine cannot see which assignments the client
   deferred or lost, and a lost signal would leave a stale high reading that
   hides chunks (`TaxonomyCoverage.java:32-38`). The routes that drop are:
   `/upsert-chunks`, `/gc/restore-rereferenced` and `/gc/quarantine-restore`
   in `VectorHandler`; the `TaxonomyHandler` routes that write topics,
   assignments or centroids; the `CatalogHandler` routes that write manifest
   rows, documents or collections; and the two catalog background jobs
   (reconcile commit, manifest fill), which drop the whole tenant when they
   end (`TaxonomyCoverage.java:40-44`; this list is the class's own comment. A search of the engine source found
   `CoverageInvalidation` used in `VectorHandler`, `TaxonomyHandler`,
   `CatalogHandler` and `CatalogManifestFillRoutes`; each route in the list
   was not traced). Not dropping, and left to the
   ten-minute ceiling alone: `/rename_collection`, the tenant import commit,
   the re-embed jobs, and the engine's own writers (the reaper, gc expiry)
   (`TaxonomyCoverage.java:45-47`); `/store-delete` does not drop either,
   because it deletes only chunks no manifest row names, which neither count
   reads (`TaxonomyCoverage.java:44-45`). An entry older than ten minutes is
   recounted whatever invalidation did, the cache holds at most 10,000
   entries (cleared on overflow), and a count that fails or times out is
   not cached (`TaxonomyCoverage.java:77-79`, `:50-51`, `:59-61`). Below the
   threshold, with no chunks, or with no centroids, the collection is
   searched unscoped with status `no_topics`. A routed collection at
   coverage 0.9 still hides up to a tenth of its chunks from the routed
   search; that remainder is an accepted risk (see Risks). The 0.9 is the
   engine's default, not a tuned value: see Results for what the two
   measurements say about it.
2. Use the query vector the request already computed for this
   collection's model group (query type for prose).
3. Rank the collection's centroids with `taxonomy_ann_query_<dim>`, 10
   nearest.
4. Keep the nearest topic (`top1`), or all within `width` of it (`band`).
   Similarity is compared only within the collection.
5. Search the collection's chunks restricted to chunks assigned to the kept
   topics, with the same per-collection k, threshold, sensitivity exclusion
   and live-chunk filter as the unscoped search.
6. Fallback: if step 5 returns zero rows or fails, use the collection's
   unscoped search, status `fallback` (`TopicRouteArm.java:169-182`).
   Otherwise status `routed`. The zero-row test runs before the client's
   threshold: a routed search whose rows the threshold then drops all is
   still `routed`, with `dropped` equal to `raw_count`, and is not retried
   unscoped (`TopicRouteArm.java:81-85`).

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
| `sensitivity` (the wire field; its one accepted value is `"exclude"`, `ChunkSensitivity.java:34-44`) | Applied inside the routed search as in the unscoped one: the handler turns it into the engine's `excludeSensitive` flag (`VectorHandler.java:3100-3107`) and the routed arm passes that flag to `search_topic_set_<dim>` (`TopicRouteArm.java:261-263`); the client sends `"sensitivity": "exclude"` (`http_vector_client.py:2955`, `:4324`) |

**Capability.** The engine reports `vector_topic_route_supported` in
`GET /version`, and the public cloud edge adds it to its `/version`
allowlist; without that, the cloud client reads the flag as absent
(`-14`; the engine writes the key at `VersionHandler.java:766-767`, the client
spells it once at `http_vector_client.py:73`). The name follows the feature,
not the route's `vector_search_` prefix that
`vector_search_chash_allow_supported` carries (`VersionHandler.java:760-762`).
The client reads it with `supports_topic_route`, built like
`supports_chash_allow`: only a JSON `true` counts, the answer is cached for
300 s, and a failed probe reads as unsupported, which is safe here because the
request goes unrouted (`http_vector_client.py:4183-4205`). A failed probe is
cached as `false` for the same 300 s. Whether the public edge allowlists the
flag is not readable from this tree; `134-research-17` records the flag as
`true` on the public `/version` of the cloud engine 0.1.197.

**Client and scope of the default.** `search_per_collection` and
`search_cross_corpus` gain a `topic_route` argument, default off. The plan
runner sets it on `search` and `query` steps unless the step's scope names
a `taxonomy_domain` other than `prose` or a `topic` (an unscoped step is
routed: most plans carry no scope, and only three builtin templates declare
`prose`, so a prose-only rule left routing almost unreachable; Step 9 probe,
`134-research-17`), carried the way `chash_allow` is: popped from the
step's args and held in a context variable that the `search` and `query`
tools read when they call `search_cross_corpus` (`runner.py:3565-3573`,
`-14`), so the dispatcher never drops it. Only collections of the prose
prefixes are routed, even when a step's corpus is `all`. A `query` step
that takes the catalog route (`author`, `content_type`, `follow_links`,
`subtree`) and a `search_metadata_scoped` step do not reach
`search_cross_corpus` and are not routed. The setting is
`search.topic_route_nx_answer` (`off` | `top1` | `band:<width>`, default
`off`, `src/nexus/config.py:1437`; it stays off, see Results). `nx search`, the MCP `search` tool and code collections
stay unrouted; turning them on needs their own measurement.

**Benchmark ordering.** `search_cross_corpus` returns rows in collection
order (`-13`; `search_engine.py:2342-2351`); the benchmark orders by
distance, while callers order by `hybrid_score`
(`search_engine.py:2831-2833`).

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
the engine's parallel fan-out; the unrouted figures (2.5 s for one
`search_cross_corpus` call and 42 s for the same searches made one at a
time, both measured from the client, `-13`) show that one batched request is
far cheaper than one request per collection. They do not say how much of the
2.5 s is spent inside the engine. Ranking topics within each collection is the variant
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
took 42 s from the client against 2.5 s for one batched `search_cross_corpus`
call (also measured from the client, `-13`).

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

The design intended `nx_answer`'s prose search to rank specific documents
above generic ones. That held in the client-side spike on in-sample RDR
questions; it did not hold on the engine route (0.385 against the 0.44 bar)
and in `nx_answer` the generic-answer questions lost 0.158 NDCG@3 (see
Results), so routing is off. If routing were on:

- Topic coverage becomes part of retrieval quality for routed collections:
  below 0.9 a collection is searched as today, and above it up to a tenth of
  its chunks are outside the routed search.
- Each routed collection costs one centroid query and one joined search
  inside the engine.

### Risks and Mitigations

- **Risk**: a question whose answer is a generic document (for example "what
  changed in 8.3.0", answered by CHANGELOG.md) gets worse. **Mitigation**:
  Step 8 added such questions and Step 9 ran them: they did not hold (3 of
  15 worse, mean -0.158), so the default stays off.
- **Risk**: the kept topic is the wrong one and still returns rows, so the
  answer is hidden. **Mitigation**: the coverage check, top-1 per collection
  rather than across collections. The risk was realised. The client-side
  spike found 0 of 33 questions worse (`-13`, in-sample). The engine route
  had 3 of the 25 RDR questions worse than OFF, 2 of them by more than 0.1
  (G15, H01), and RDR recall@10 fell from 0.777 to 0.737 (`-18`). In the Step 9
  partial run 4 of 26 paired questions were worse (GD01, GD12, GD14, G15) and
  1 better (`-17`). Routing stays off.
- **Risk**: in a routed collection above 0.9 coverage, unassigned chunks
  (up to a tenth) are not searched. **Mitigation**: the cache drop on the
  engine routes listed under engine step 1 (a store through `/upsert-chunks`,
  an assignment, a topic, a centroid, a manifest write), the ten-minute ceiling
  for writers that do not drop (`TaxonomyCoverage.java:40-51`),
  `nx taxonomy drain` as the repair, and Step 5a's check of the threshold
  (see Results). Accepted as a residual risk.
- **Risk**: a filtered search under an approximate index returns fewer rows
  than asked: the centroid ANN filters one shared HNSW index by collection,
  and the topic search may walk a collection's `pci_` index. The planner,
  not RDR-227's router, chooses that index (`PciPlanCheck`), and the direct
  call forces a custom plan as the router does, so the exposure is the same
  as `search_topic_scoped`'s (Step 1). **Mitigation**: short rows did not change any score in
  the measurement (F0 equals Fk, `-13`); the zero-row fallback covers the
  empty case; Step 5a reports how often routed collections come back short
  against an exact scan.
- **Risk**: RDR-229 changes the taxonomy key. **Mitigation**: whichever RDR
  lands second adjusts the five places that key by collection name, listed
  under RDR-229 in Relationship to Prior RDRs.

### Failure Modes

Statuses are `routed`, `no_topics`, `fallback` and `skipped`
(`src/nexus/tables/topic-route-status.toml:11-17`). What happens depends on
what went wrong (`TopicRouteArm.java:44-59`):

- No chunks, no centroids, or coverage below `min_coverage` (0.9 by default):
  status `no_topics`, unscoped search (`TopicRouteArm.java:147-149`).
- The coverage lookup throws or answers nothing, or the centroid ranking
  fails or is cut by its time bound: status `no_topics`, unscoped search, and
  `topic_route_error` names what failed. The request does not fail
  (`TopicRouteArm.java:131-143`, `:155-162`). A failed count is not cached
  (`TaxonomyCoverage.java:59-61`, `:173-176`).
- Kept topics return nothing: status `fallback`, unscoped search
  (`TopicRouteArm.java:172`).
- The topic search itself errors, a statement timeout under the search bound
  included: status `fallback`, unscoped search, with `topic_route_error`
  (`TopicRouteArm.java:174-181`).
- The fan-out budget is spent before a stage starts, or a topic-search
  statement is cut by the fan-out bound: that collection reports
  `fanout_budget_exhausted` with no route status and is not retried unscoped
  (`TopicRouteArm.java:51-53`).
- The request budget is spent, the connection pool or admission is exhausted,
  or the database reports a transient failure (SQLSTATE class 08, 53, 57P,
  40001, 40P01 or 55P03): the whole request fails, as an unrouted request would
  (`TopicRouteArm.java:54-57`, `:227-240`).

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
empty-result exact re-run. Step 3 decided that the topic arm adopts none of
them (`TopicRouteArm.java:75-77`; the zero-row fallback covers the empty
case, and short pages were measured in Step 5a-ii, see Results), and gave the
topic arm its own method, `TopicRouteArm.attempt` (`:123`), since the
existing direct call (`runCombinedQuery`) is not a fan-out arm.

#### Step 2: Coverage

A cached per-collection coverage ratio from
`taxonomy_coverage_counts_<dim>` (`taxonomy-022`; `taxonomy-023` counts
set-based), dropped as engine step 1
says. These are three more SQL functions (`_384`, `_768`, `_1024`;
`taxonomy-022-coverage-counts.xml:68`, `:112`, `:156`), so with Step 1's
three the design adds six, in the changesets `vectors-062-1` and
`taxonomy-022-1`; `taxonomy-023-1` replaces the three bodies without adding a
function (`taxonomy-023-coverage-counts-set-based.xml:33`, `:80`, `:127`). Tests:
an orphaned taxonomy (topics, no chunks) is never routed; a half-assigned
collection reads below 0.9; projection rows do not count as assigned; a
stored batch whose assignment is deferred lowers the cached ratio.

#### Step 3: `topic_route` on search-per-collection

Engine steps 1 to 6 above and the per-collection status in the response.
As built, the two reads that come before the topic search, the coverage count
(when it is not cached) and the centroid ranking, are each limited to half of
what the arm's time budget has left, so a slow cold read cannot use up the
budget the unscoped search needs; a read cut by that share degrades the
collection to `no_topics` with a `topic_route_error`
(`TopicRouteArm.java:61-69`, `:243-245`). Besides `topic_route_status`, each
`per_collection` entry can carry `topic_route_min_coverage`,
`topic_route_error`, `topic_route_coverage` (the coverage ratio read, when the
collection has centroids and chunks) and `topic_route_topics` (the number of
topics kept, when the centroid ranking ran) (`VectorHandler.java:857-863`;
`TopicRouteArm.java:98-104`). Tests: routed; `no_topics` (no centroids; low coverage); `fallback` (zero
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
per-collection statuses as the structlog event `search_topic_route`: one event
per `search_cross_corpus` call that asked for routing, including a call that
went out unrouted, where the event names the reason
(`src/nexus/search_engine.py:2417-2418`, `src/nexus/topic_route.py:197-235`); a
benchmark arm that calls the engine route
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
(`src/nexus/mcp/core.py:3474`), and it is unrelated to recall.

### Assumption Verification

At the gate, two critical assumptions were verified in the client-side spike
(`-10`, `-13`) and three were open, each with the step that measures it. Since
then: the spike's gain is in-sample and the engine route did not reproduce it
(`-18`); latency was measured and not met as measured (`-18`); answer-level
gain and generic-answer questions were measured and not met (`-17`);
query-type ranking is still unmeasured because Step 6 was not run. See the
Critical Assumptions list for each one's status and Results for the numbers.

#### API Verification

| API call | Library | Verification |
| --- | --- | --- |
| `HttpCentroidStore.ann_query` | nexus | Source read, `http_centroid_store.py:249`; exercised by the spike |
| `HttpVectorClient.search_topic_scoped` | nexus | Source read, `http_vector_client.py:5025`; exercised by the spike |
| `search_cross_corpus` | nexus | Source read, `search_engine.py:1425`; exercised as the production baseline (`-13`) |
| `/v1/vectors/search-per-collection` | nexus engine | Source read, `VectorHandler.java:319` and `:916`; query embedder `PgVectorRepository.java:3241` |
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
- **Incremental adoption**: off by default, and it stays off: Step 9 failed,
  so the setting is not turned on (see Results). Were it on, it would apply
  to `nx_answer` steps not scoped to another domain or a topic.
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
- T2 `nexus_rdr/134-research-1` to `-18`; benchmark results in
  `bench/results/`: `rdr134-taxonomy-ab-leg1-2026-11-16.json`,
  `rdr134-taxonomy-ab-control-2026-11-16.json`,
  `rdr134-taxonomy-ab-prod-2026-11-16.json`,
  `rdr134-taxonomy-ab-leg1-gated-2026-11-17.json`,
  `rdr134-taxonomy-ab-prod-gated-2026-11-17.json` (`-16`),
  `rdr134-mvv-2026-11-19.json` (`-18`). The Step 9 rerun's rows,
  `rdr134-step9-ab-rerun.json`, are not in `bench/results/`; `-17` records
  them.
- Voyage AI API reference, `input_type` (docs.voyageai.com/reference).
- Dehghankar, Asudeh, Mittal, Shetiya, Das. 2026. "Random-Access Ranked
  Retrieval and Similarity Search." Catalog tumbler `1.12.6`. Source of the
  band-selection idea; the measurement shows a band works within a
  collection and fails across collections.
- Beads: `nexus-9napz` (superseded by this RDR), `nexus-n1908`
  (prerequisite, closed).

## Results

Both pre-declared checks ran on 2026-11-19 and both failed. The outcome:
`search.topic_route_nx_answer` stays `off`, its default
(`src/nexus/config.py:1437`). The engine route (`topic_route` on
search-per-collection, the topic-set and coverage functions, the
`vector_topic_route_supported` flag) and the client option stay in the code,
and nothing uses them by default.

### Run order and approval

The subsections below follow the order the runs happened, which is not the
order of the Implementation Plan. The Step 9 `nx_answer` A/B (Phase 2) ran
first: its probes began at 15:48Z and its rerun ran from 17:28Z to 18:40Z. The
Minimum Viable Validation (Step 5a-ii, Phase 1) ran after it, from 19:58Z to
20:24Z. Bead nexus-7fku2.1.9 says "If the validation fails and is not recovered,
Phase 2 does not start" (`bd show nexus-7fku2.1.9`). The push-train
coordinator (halcyon) scheduled the A/B to run once engine 0.1.197 was
deployed, before the validation had run (its messages to this session on
2026-11-19). Sam approved the A/B run and its spend, up to 100 USD
(`134-research-17`). He did not separately approve running it before the
validation. The validation later failed on its own, so the early A/B did not
change the outcome, but the order was not the plan's.

### What was in the sample

Read the numbers below with this in mind.

- The 25 RDR questions are in-sample. They are among leg 1's 26, and the
  design was chosen on them (`-10`, `-13`).
- The 8 title questions are not unseen: `-13` measured them before either
  check ran. The Minimum Viable Validation driver's own note calls them out of
  sample (`scripts/bench/topic_route_engine_arm.py:19-20`); that holds only in
  the sense that they did not choose the design. So the Minimum Viable
  Validation has no unseen question, and it is a reproduction test, not a
  generalisation test.
- The 15 generic-answer questions (Step 8's questions; `bench/queries/generic_docs_v1.yaml`
  and `generic_docs_v2.yaml`) are the Step 9 questions that were not used to
  choose the design. But GD01 to GD08 were seen in the first, partial Step 9
  run, before Sam changed the primary ordering, and the driver's scope notes say
  so (`scripts/bench/topic_route_nx_answer_ab.py:94-97`). GD09 to GD15 were
  not seen then (inferred from that run stopping after 8 of 73 questions, not
  read per question).
- In the Step 9 rerun exactly one title question ran (the rerun file's
  summary, `by_kind.title.n` = 1, in `rdr134-step9-ab-rerun.json`). It scored
  NDCG@3 0.0 in both arms, so it says nothing either way.

### Step 9, nx_answer A/B (`134-research-17`)

Cloud engine 0.1.197, driver `scripts/bench/topic_route_nx_answer_ab.py`.
The rerun used develop 5b366f0d5, which holds the client's calibrated pool cut
(GH #1667), and the calibrated-primary driver (4a7bb89d6), 2026-11-19 17:28:06Z
to 18:40:17Z. It cost 18.31 USD (about 0.68 USD a question) and stopped at 27
of 73 questions because one envelope had a step without `cost_usd`, and the
driver does not run past a cost it cannot read. An earlier partial run (8 of 73
questions, 2.41 USD) was stopped when raw-distance ordering put every code row
ahead of every prose row on the plan-473 questions (see Revision History,
2026-11-19).

Spend over the whole A/B, from `-17`: a probe at 0.22 USD, the partial run at
2.41 USD, a probe at 0.35 USD and the rerun at 18.31 USD, about 21 USD in all
(21.29 USD by that sum), against the 100 USD cap Sam approved. Two more runs
have no recorded cost: a probe whose three calls the write guard refused, and a
full run stopped with at most one call in flight.

The rule: the default becomes `top1` only if NDCG@3 over all questions rises
by at least 0.03 with more questions better than worse, and the generic-answer
questions lose no more than 0.02 on average. The driver wants a complete run,
and its own verdict on this file reads "incomplete: no verdict". Sam accepted a
FAIL on the partial run instead of paying about 31 USD to finish. On the 26
paired questions the rule's three checks all read false.

| Measure (partial run) | Off | Top1 | Change |
| --- | --- | --- | --- |
| First clause: the 26 paired questions, NDCG@3, calibrated order | | | -0.089 (1 better, 4 worse, 21 tied) |
| Generic questions, NDCG@3, calibrated order (15 of 15 done) | 0.654 | 0.496 | -0.158 (0 better, 3 worse; CI95 -0.334 to 0.0) |
| Generic questions, recall@10 | 1.0 | 0.733 | -0.267 |
| RDR questions, in-sample (10) | 0.318 | 0.325 | +0.007 (1 better, 1 worse) |
| Title questions (1) | 0.0 | 0.0 | 0.0 |
| Routed questions only (24) | | | -0.096 (1 better, 4 worse, 19 tied) |
| Envelope order, secondary (26 paired) | | | -0.093 |
| Raw distance order (26 paired) | | | -0.056 |

The first clause asks for at least +0.03 over all questions; the 26 paired
questions read -0.089. That is not the whole 73 questions, and the other 46
were not run. The generic clause allows -0.02, and the measured -0.158 is far
below it, with 15 questions (the rule needs at least 10). The three generic
losses were GD01 1.0 to 0.0, GD12 0.631 to 0.0 and GD14 0.956 to 0.213; the
other loss was G15 (RDR), 0.613 to 0.387, and the one win was H02, 0.704 to 1.0.
One question (G07) errored with no chunks, and GD08's two arms ran different
plans. The NDCG figure with calibrated order is the pre-declared primary after
Sam's change; the other orders are printed, not decided on. Mean elapsed time
per question was 81.6 s off and 72.6 s top1. Reading: on the plans these
questions match, routing in `nx_answer` pushes the labelled generic document
out of the top three.

### Step 5a-ii, Minimum Viable Validation (`134-research-18`)

Cloud engine 0.1.197, 2026-11-19 19:58:40Z to 20:24:59Z, script
`scripts/bench/topic_route_engine_arm.py` at develop 8e3a8a432, results
`bench/results/rdr134-mvv-2026-11-19.json`. The routed arm was
`search_cross_corpus` with `topic_route={"mode": "top1"}` at the engine's
default gate of 0.9 (`topic_route_engine_arm.py:48`). Spend was Voyage query
embeddings only, estimated from request counts at well under the 5 USD cap
(not metered).

| Bar (set before the run) | Measured | Result |
| --- | --- | --- |
| RDR-question NDCG@3 at least 0.44 (25 questions) | 0.385 | fails |
| No question worse than OFF by more than 0.1 | two: G15 0.613 to 0.387, H01 0.631 to 0.0 | fails |
| Latency at most 1.5 times the unrouted request | 1.66 times (mean 2.02 s routed, 1.21 s unrouted) | fails as measured |

Two baselines are in play. The bar text says "OFF 0.335", which is `-13`'s
figure, where the production call (P0) and the exact per-collection scan
(OFF) scored the same. In this run P0 scored 0.335 and OFF, the exact scan,
scored 0.315. The engine route's 0.385 is above both and below the bar, which
is the client spike's 0.470 less 0.03. Against OFF, 6 questions were better
and 3 worse (counted from the results file, engine route against OFF). Recall@10
on the RDR questions fell, from 0.777 (P0 and OFF) to 0.737 (from the results
file). The 8 title questions scored NDCG@3 0.500 in every arm, and their
recall@10 fell from 0.667 (P0 and OFF) to 0.542 (from the results file).

The latency figure is soft. It is one run with the arms in a fixed order (P0,
then the routed arm, then the gate-off arm), and the gate-off arm, which
routes more collections, ran faster (1.48 times). P0 takes the default
batched path, while the routed arm is forced onto the per-collection route, so
1.66 times is the routed route plus the switch of path, not the cost of
routing alone. The bar fails as measured; the figure does not isolate what
routing costs. The quality bar fails by itself, so the verdict does not rest
on latency.

The 0.9 gate: the pre-declared rule was that if the gate-off arm reached
0.44 and the 0.9 arm did not, the threshold would be lowered. The gate-off
arm (`min_coverage` 0.0) scored the same 0.385 with the same two losers, so
the gate is not what failed. This agrees with `-16`, the client-side
measurement: at 0.9 the gate cost nothing (leg 1 gain +0.1664 gated and
ungated alike, production set 0.470 both ways), and at 0.96 it cost a little.
The coverage values leave a gap from about 0.51 to 0.95 (`-16`; the
engine run found 21 distinct values from 0.04 to 1.0, with none between 0.80
and 0.95), so no run located the best threshold inside it. The 0.9 stays as
the engine's default (`TaxonomyCoverage.java:75`), a request can set another
(`TopicRoute.java:17-20`), and nothing here tuned it.

Short routed collections, summed over the questions: 203 at 0.9 and 479 with
the gate off. A collection counts as short when the engine route returned
fewer rows than the exact scan did for it, up to the request's per-collection
k (`topic_route_engine_arm.py:14-16`). `-13` counted differently (a topic
returning fewer than 20 rows: about 11 of 36 routed collections per question),
so the two counts do not compare.

The engine route reached 0.385 where the client spike reached 0.470. The
differences the bead lists as candidates were not tested: per-collection k
(20 in the spike, 40 in production), thresholds, rerank, and the document-type
centroid ranking of Step 6.

### Step 6 was not run

Step 6 (rank centroids with a second, document-type query embedding) was not
run. The Minimum Viable Validation text sends a quality miss to Step 6. The
decision recorded for this close-out (session marlowe, 2026-11-19) is to stop
and close the RDR; the words recorded are "Step 6 NOT run, stop and close the
RDR" (the close-out note in T1 scratch; `134-research-17` and `-18` record the
two failed checks, not Step 6's decision). The reasons in this paragraph are
the recorder's reading of the evidence, not Sam's words (inferred, not read):
both checks had failed on quality (0.385 against 0.44, and Step 9's generic
loss); Step 6 targets quality; but the gate-off arm already matched the gated
one, and the `nx_answer` A/B had failed with the centroid ranking as built,
so the choice was not to fund an engine change and another measurement cut. The
Critical Assumption that query-type ranking does at least as well as the
document-type ranking is therefore still unmeasured.

### Band mode

Band mode was never validated. The engine implements it (`TopicRouteArm.java:199-202`),
and no engine run used it (see Key Discoveries).

### Effect on the Critical Assumptions

The Critical Assumptions list above now carries each one's status. In short:
the 1.5 times latency bound is not met as measured (1.66 times, and the figure
does not isolate routing); the carry-through to `nx_answer` and to
generic-answer questions is not met (generic -0.158); query-type ranking is
unmeasured (Step 6 not run); the client-side gain is in-sample and the engine
route did not reproduce it.

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
  first partial run: on the plan-473 questions, raw distance put every
  voyage-code-3 row ahead of every voyage-context-3 row in both arms
  (`134-research-17`). He chose it on those raw and envelope scores; the
  calibrated scores had not been computed. Raw distance and envelope order are printed, not
  decided on. The run is redone after the client's calibrated pool cut and
  the plan reseed.
- 2026-11-19: Close-out edit (nexus-7fku2.2.3 and the close-out bead): Results
  section added with the Step 5a-ii and Step 9 outcomes (`134-research-17`,
  `-18`), both failed, routing stays off, Step 6 not run by Sam's decision;
  wording amended to match the reviewed code: the `topic_route.collections`
  and `min_coverage` members, the coverage cache's invalidation and the 0.9
  default, the failure modes and statuses, the capability flag's name and
  probe, the six SQL functions, the RDR-229 key sites, and the leftovers (the
  2.5 s client figure, the router cite, the band figure, merge versus
  `hybrid_score` order).
- 2026-11-20: Close-out fix round (critique `nexus/critique-1421c17d4-nexus-7fku2.1.12-2026-11-20-r1`):
  statements outside Results that the results contradict are rewritten in
  place (Consequences, the Risks entry on a wrong topic, the Critical
  Assumptions, Assumption Verification, Incremental adoption, the Key
  Discoveries labels); the Results subsections follow run order and record that
  Step 9 ran before the Minimum Viable Validation; figures omitted before are
  added (title recall, the 26-question first clause, one title question, total
  Step 9 spend); the Step 6 reason no longer rests on latency; the event count,
  the sensitivity field, the Step 1 router decision, the clamp and the
  `topic_route_*` keys, and the stale line cites are corrected; RDR-229 is
  accepted and RDR-193 closed.
