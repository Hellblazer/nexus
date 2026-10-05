---
title: "One Vector Table per Embedding Model, with Tenant Isolation"
id: RDR-225
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-10-05
accepted_date:
related_issues: [nexus-tu8wp, nexus-tu8wp.6]
related_rdrs: [RDR-152, RDR-155, RDR-156, RDR-191, RDR-192]
---

# RDR-225: One Vector Table per Embedding Model, with Tenant Isolation

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Drafted 2026-10-05 against develop `a798f07ea`. No product code has changed. The
research that decides the design is still running on forks of production; its
open items are marked **Assumed** below.

**Provenance.** On 2026-10-05, investigating slow cloud searches, a literature
review found that every 1024-dimension vector in the engine sits in one HNSW
index, whichever model embedded it. Sam: "the mixing of embeddings is extremely
bad and that needs fixed." Sam also asked why chunks are not partitioned by
tenant, and stated that the cloud serves a handful of tenants.

## Problem Statement

An HNSW index (Hierarchical Navigable Small World) is a graph. Each vector is a
node, linked to the vectors nearest to it, and a search walks those links
greedily from one fixed entry point toward the query. The links mean something
only when distances between the vectors mean something.

The engine stores every 1024-dimension embedding in one column,
`nexus.chunks.embedding_1024`, under one index, `idx_chunks_embedding_1024`
(`vectors-004-unify-chunks.xml`). Two different Voyage models write into it:
`voyage-code-3` for `code__*` collections, and `voyage-context-3` for `docs__*`,
`rdr__*` and `knowledge__*`. Both produce 1024 numbers, but they are different
embedding spaces. The distance between a code vector and a prose vector is not a
measure of anything. The engine already knows this: `requireHomogeneousModel` in
`PgVectorRepository` refuses to search two models' collections with one query
vector. The index does not know it, and it links code and prose vectors to each
other anyway. Every tenant's rows share the same graph too.

### Enumerated gaps to close

#### Gap 1: Code and prose vectors share one HNSW graph

Links between vectors of different models are built from meaningless distances.
If the two models' vectors fall in separate regions, which is likely, the graph
is two near-islands joined by a few arbitrary links, with a single entry point
in one of them. A query from the other model must cross those links, and the
greedy walk can stop at a poor local match. If the models instead mix, every
neighbourhood carries noise links. Either way the walk is longer and recall is
lower than a single-model graph would give. A filtered search also spends visits
on the other model's nodes, none of which can ever match. Measured on a
production fork with real queries (§ Research Findings, F2), HNSW's top-40
agreed with exact search on only 0.60 to 0.975 of results.

#### Gap 2: Tenants share one graph, one heap and one cache

Row-level security keeps tenants from reading each other's rows. It does
nothing for performance: another tenant's rows raise the share of every
filtered walk that is wasted, and its pages compete for the same shared buffers.
On the production fork, one pass over our main tenant's 98 collections read
3.3 to 4 GB "warm" on an 8 GB host and evicted itself (F3).

#### Gap 3: The layout gives the planner no way to scope a search

The search functions filter by `collection = ANY(...)` and by tenant through
row-level security. Neither can steer the scan to a smaller graph, because
there is no smaller graph. Partitioning was rejected in RDR-156 and RDR-191
for a reason that does not apply to these keys (see § Relationship to Prior
RDRs), and nobody has evaluated it since.

## Relationship to Prior RDRs

Searched the RDR corpus for "partition", "tenant isolation", "unify chunks" and
"HNSW index".

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-152 | Origin (tenancy) | Chose a `tenant_id` column plus FORCE row-level security as the tenancy model: "schemas partition domains, not tenants." Security only. Performance isolation was never addressed. The rationale holds for security; this RDR keeps RLS and adds physical isolation under it. |
| RDR-155 | Origin (pgvector store) | Moved T3 onto pgvector with native RLS. Did not consider per-model graphs. |
| RDR-156 | Origin (rejected partitioning) | Rejected a partitioned `chunks` table with dimension as the key. |
| RDR-191 | Origin (one table, one index per dim) | Unified the per-dimension tables into `nexus.chunks` with three typed columns. Its fact V1, "Partitioning is impossible", was verified for partitioning **by dimension**: an untyped `vector` parent cannot carry an HNSW index, and a typed child with a different type cannot be attached. A tenant or model key keeps the same typed `vector(1024)` column in every partition, so V1 does not apply. This RDR does not reopen dimension partitioning. |
| RDR-192 | Adjacent (closed) | Liveness predicate `live(c)`. Hidden rows stay in the graph; the migration here is a natural point to drop them physically. |
| nexus-tu8wp.6 | Adjacent (bead, not an RDR) | The cardinality router: exact search when the selected collections are small. It takes single-collection searches off the graph and is the near-term fix. It does not fix the graph for the HNSW paths that remain. |

## Context

### Background

The cloud engine's per-collection search (nexus-tu8wp) made a filtered,
single-collection HNSW walk the common case. Cold first touches took 5 to 43 s
in production, and one collection timed out at 30 s. A research pass and an
analysis against twelve indexed papers (T2
`nexus/research-pgvector-filtered-search-optimizations-2026-10-05`,
`nexus/analysis-filtered-search-vs-literature-2026-10-05`; T3 catalog 1.11.696
and 1.11.697) explained the cost as wasted heap reads during filtered walks. That
work also surfaced the mixed-model graph.

### Technical Environment

- PostgreSQL 17 with pgvector 0.8.2. The cloud runs on Crunchy Bridge
  standard-8 (2 vCPU, 8 GB RAM, about 2.4 GB shared_buffers), one replica.
- `nexus.chunks`: PK `(tenant_id, collection, chash)`, three nullable typed
  columns `embedding_384/768/1024` with a CHECK that exactly one is set, FORCE
  RLS. Heap about 8.9 GB. HNSW `m=16, ef_construction=64`, 1024 index about
  3.1 GB.
- Search settings: `hnsw.iterative_scan = relaxed_order`, `ef_search =
  max(200, k)`, `max_scan_tuples = 200000`, statement timeout 30 s, custom plans.
- A collection's model is recorded in `catalog_collections.embedding_model`
  (`CollectionRegistry`), not on the chunk row.
- The cloud serves a handful of tenants (Sam, 2026-10-05). The `nexus` tenant
  holds about 312k of the roughly 430k 1024-d rows.
- Local installs embed with bge-768 by default, a single model per
  dimension, so they are affected only when Voyage reaches a local engine.

## Research Findings

### Investigation

Fork measurements by conexus-0e on PITR forks of production, 2026-10-05,
standard-8 and memory-16. Raw JSON is to be copied to durable storage named in the run record.

### Key Discoveries

- **F1 (Verified, source).** Both 1024-d models share `embedding_1024` and
  `idx_chunks_embedding_1024` across all tenants (`vectors-004-unify-chunks.xml`
  lines 269 and 326; `PgVectorRepository.requireHomogeneousModel`).
- **F2 (Verified, fork).** Three real queries, embedded the way the engine
  embeds them, k=40, single-collection filter. HNSW top-k agreement with exact
  search was 0.60 on dt-papers and code__1-2 (query 1), 0.625 on code__1-20
  (query 2) and 0.80 on code__1-15 (query 3). The candidate list is 200, so the
  loss is filter- and graph-driven, not ef-driven.
- **F3 (Verified, fork).** On standard-8, one pass over the main tenant's 98
  collections read 3.3 to 4 GB even warm. On memory-16 the warm pass read zero
  blocks.
- **F4 (Verified, fork).** code__1-15 (5.6k rows) takes HNSW in the engine's
  own plan: 30 s timeout cold, 2.8 to 4.4 s partly warm. Exact search takes
  67 to 74 ms. dt-papers, about the same size, is fine. Literature (VADER,
  catalog 1.12.157) attributes such differences to query-filter correlation.
- **F5 (Verified by E1, research-2).** Code and prose vectors form separate
  regions: under 5% of a vector's exact nearest neighbours come from the other
  model.
- **F6 (Retired: superseded by the non-inferiority test).** A single-model graph for one tenant raises
  code__1-2's agreement on query 1 from 0.60 to at least 0.90, and cuts cold
  HNSW time by at least 2x. E2 also measures the tenant-only step separately.
- **F7 (Assumed; protocol step 2, research-14).** A table LIST-partitioned by
  `tenant_id`, with an HNSW index created on the parent, gives each partition its
  own HNSW index. A query run as `nexus_svc` under the tenant GUC is pruned at
  execution time to one partition's HNSW scan, with iterative scan in effect.

#### Registered research findings (T2 `nexus_rdr/225-research-1` to `-16`)

- **✅ Verified** (source search), research-1. The two 1024-d models share one column and one HNSW index across all tenants.
  *Source: vectors-004 lines 269 and 326; `requireHomogeneousModel`.*
- **✅ Verified** (spike), research-2. E1: the cross-model neighbour fraction is 0.000 over 400 samples, so the models are disjoint islands.
  *Source: conexus-0e fork, 2026-10-05.*
- **✅ Verified** (spike), research-3. The index holds two tenants. `gate-xr789` (116,491 rows) is a re-import of `nexus` (314,181 rows), and 14.6% of nearest neighbours are its near-duplicates.
  *Source: the same fork.*
- **✅ Verified** (spike), research-4. F2: on real queries, HNSW agreement with exact is 0.60 to 0.80 on single-collection filters.
  *Source: fork B.*
- **✅ Verified** (spike), research-5. F3: standard-8 evicts itself on a full pass. memory-16 and memory-32 stay resident.
  *Source: the forks.*
- **✅ Verified** (spike), research-6. F4: code__1-15 times out under HNSW at 30 s cold. Exact takes 67 to 74 ms.
  *Source: fork B.*
- **✅ Verified** (spike), research-7. All-exact over 98 collections on standard-8 costs 17.7 s serial cold and 4.3 s warm. No plan seq-scanned the whole heap.
  *Source: the fourth fork.*
- **✅ Verified** (source search), research-8. Census: under the per-collection route at 60k, 10.1% of logged searches use HNSW. That is 13 code and 27 prose distinct queries.
  *Source: the transcripts.*
- **✅ Verified** (source search), research-9. H5 inventory: 11 search families, 8 GC families, and these mixed-model objects: `chunks`, `taxonomy_centroids`, `live_chunks`, `collection_vector_stats` and both HNSW index families.
  *Source: the inventory.*
- **✅ Verified** (source search), research-10. RDR-191 V1 rejected partitioning only by dimension.
  *Source: rdr-191 lines 170 to 178.*
- **✅ Verified** (source search), research-11. The router covers plain search only. Hybrid uses HNSW above 5000 text matches. Taxonomy has its own index. The default threshold is 10000.
  *Source: PgVectorRepository and PgSession at 0e3aaf60f.*
- **✅ Verified** (source search), research-12. `search_telemetry` has no path, scope or model column.
  *Source: telemetry-001 lines 100 to 109.*
- **✅ Verified** (spike), research-13. `pg_stats` hides FORCE-RLS tables from non-superusers.
  *Source: the fork.*
- **❓ Assumed** (spike, pending step 2), research-14. F7: partition pruning works under RLS on every search family.
- **❓ Assumed** (spike, pending step 2), research-15. H4: the identity-table FK costs at most 1.25x on every write path.
- **⚠️ Documented** (docs only), research-16. The literature crossover points (Veda, Compass) were measured warm, and VADER finds query-filter correlation.
  *Source: knowledge__dt-papers.*

### Evaluation Protocol (pre-registered, revision 6, frozen, 2026-10-05)

Sam: "let us prove out our design first this time rather than just reflexively
jumping." Three critique rounds preceded this revision (T2
`nexus/critique-rdr225-evaluation-protocol-2026-10-05`, `-round2-`, `-round3-`).

**Rulings that shape it (Sam, 2026-10-05):**
- Tenant isolation is required and not subject to evaluation.
- The per-model split is adopted on principle and on E1, with the protocol proving it works rather than that it pays: "prove it works, not a benefit." The step-1 census showed a benefit gate cannot be decided on real queries. Only 10.1% of logged searches still use HNSW on the per-collection route, which is 13 distinct code queries and 27 prose (T2 `nexus/rdr225-step1-census-and-h5-inventory-2026-10-05`).
- Queries are logged plus synthetic.

**What this protocol must show, before any production code:**
- the design is feasible on every vector path;
- it is not worse than the tenant-partitioned mixed layout;
- its write cost is bounded;
- the migration completes safely.

**Evidence already in hand (exploratory, not re-tested):**
- **E1:** 400 sampled vectors had a cross-model neighbour fraction of 0.000, so the two models are disjoint islands. 14.6% of neighbours lay in the duplicate tenant `gate-xr789`.
- **Census:** in the run record.
- **Fork measurements:** T2 `conexus/tu8wp-fork-measurements-2026-10-05`.
- **Raw data:** `~/nexus-evidence/rdr225-2026-10-05/` with `SHA256SUMS`.

**Integrity.**
- Fixed before the first confirmatory run, and never edited after: the margins, decision rules and outcome labels below.
- Recorded here before step 3: the hashes of the query file, the query vectors, the harness commit and this section's commit.
- Raw output goes under `~/nexus-evidence/`, never `/tmp`.

#### Harness fidelity (every arm)

- **Role and session.** Queries run as `nexus_svc` with FORCE RLS and the production policy on every table and partition. They use the serving settings `runPlainSearchStatement` applies, taken from that method as source of truth:
  - `hnsw.iterative_scan = relaxed_order`;
  - `hnsw.ef_search = max(200, k)`;
  - the scan budget (`max_scan_tuples`, `scan_mem_multiplier`);
  - `plan_cache_mode = force_custom_plan`;
  - statement timeout of 120 s for measurement, with the count over 30 s reported.
- **Router.** The engine runs the router with `NX_SEARCH_EXACT_MAX_ROWS=60000`, set explicitly and recorded. The code default is 10000.
- **Build.** Every HNSW build uses `m=16`, `ef_construction=64` and one `maintenance_work_mem` and worker count. ANALYZE runs after every build. Insertion order is seeded with `ORDER BY hashtext(chash || seed)`.
- **Equivalence.** A6 runs per-model versions of the engine's search functions that are textually identical to today's except for the table they name. Their diff is recorded.
- **Plan check.** An untimed EXPLAIN pass precedes the timed pass. Planning is deterministic, so a plan that differs from the declared one is a result, not noise. The declared plans are: that tenant partition's HNSW index, or the PK-prefix bitmap scan for exact.
  - A flip in A6 fails that statement class for A6 (DESIGN-F7).
  - A flip in A1t is a harness error, fixed before any decision.
  - Flips are counted per arm.
  - No timing runs under EXPLAIN ANALYZE.
- **Execution path.** Both arms run through one SQL replay harness. It issues each engine statement, as `runPlainSearchStatement`, `hybridSearch` and the combined, aspect and graph-hop paths generate it, under the settings above. The router decision is replicated by the same bounded count, taken over the arm's own table: `chunks` for A1t, the query model's table for A6.
  - Before step 3, the harness is validated on a fork against the engine itself, for 20 queries per path on today's layout. It must give identical result ids and the same routed counts (`routedExactCount` and `routedHnswCount` deltas).
- **Recorded per statement:** tuples visited, whether `max_scan_tuples` fired, whether the empty-result exact fallback (nexus-bq06h) fired, and the router's decision.

#### Step 1: census (done)

Recorded in the run record. Corrections adopted from round 3:
- Hybrid search uses HNSW only when its text gate matches more than 5000 chunks (`SELECTIVE_GATE_MAX`).
- Taxonomy searches `taxonomy_centroids`, its own table, which itself mixes models. Here it is a design line only (H5), not measured in step 3.

"Every model" in this protocol means the two 1024-d cloud models, voyage-code-3 and voyage-context-3.

#### Step 2: local feasibility, a hard gate with no fork

The substrate is the bundled PG 17 with pgvector 0.8.2 (versions recorded; any other pgvector version invalidates the step). Two tenants are loaded with the same seeded 300k unit vectors per model, so the planner sees production-scale partitions without `enable_seqscan=off`.

**F7, pruning and isolation.**
- Method: for every search family in the H5 inventory (11 SQL families) and every Java search member, run an untimed EXPLAIN (ANALYZE, BUFFERS) as `nexus_svc` under the tenant GUC. Observe:
  - "Subplans Removed" on the partitioned Append;
  - `pg_stat_user_indexes.idx_scan` deltas per partition index, before and after.
- Positive control: a query that names the other tenant's GUC touches only that tenant's partition.
- Negative control: a query with no tenant GUC returns zero rows and touches no partition index.
- Leakage check: no row of the other tenant is ever returned.
- Inlining: the harness confirms once that the `LANGUAGE sql STABLE` search functions inline, which pruning depends on.
- **Pass:** every family touches only the session tenant's partition of its model, and all three controls hold.
- **Fail label:** DESIGN-F7.

**H5, scoping.**
- A script diffs the changelogs and Java against the pinned inventory, to check that no vector path is missing from it. Each family is then run once on the local per-model layout as a functional smoke test.
- `taxonomy_centroids` gets a written design line.
- **Pass:** the diff is empty, every family runs and returns rows from one model's tenant partition only, and the taxonomy design line exists.
- **Fail label:** DESIGN-H5.

**H4, write path.**
- Comparator: today's `chunks` (HNSW present) against the identity table plus per-model tables (HNSW present).
- Same seeded vectors as F7.
- Paths: bulk 10k, single insert, update, delete, rename.
- Runs are paired and alternated ABAB, 5 pairs per path. The CI is a t-interval on the log ratio across pairs.
- **Pass:** the CI upper bound of the time ratio is at most 1.25 on every path, with the extra ms per chunk stated. The step-3 fork repeats H4 on real vectors, descriptively.
- **Fail label:** DESIGN-H4.

#### Step 3: non-inferiority on a fork (standard-8)

**Arms and builds.**
- **A1t:** today's single table with both models, LIST-partitioned by tenant, plus the router.
- **A6:** per-model tables, each LIST-partitioned by tenant, plus the router.

Each arm is built twice, with seeds 1 and 2. Build pair i is A1t build i with A6 build i, and the two pairs are measured in two phases:
- Phase i has A1t build i and A6 build i resident together, so cycles can alternate ABBA.
- The phase is dropped before the next begins.
- Peak disk must stay under 70% of the fork volume. If it would not, phases use one arm at a time, and the record states that the latency CI spans separate time blocks, repeats the block order, and logs read throughput per cycle.

**Ground truth.**
- Exact search over the scope's live rows, computed once per (query, scope) in a separate pass after the timed runs.
- Both arms hold identical live row sets per scope, asserted by a chash-set hash.
- Ties are within 1e-6. The denominator is min(10, matching rows).
- For synthetic queries, the source chunk is removed from both lists.

**Queries.**
- **Router on.** The gate population under the per-collection route: unrouted paths, plus hybrid queries whose text gate matches more than 5000 chunks. All logged queries in that population are used, and synthetic queries fill the set to the floor.
- **Router off.** All 434 distinct logged queries plus synthetic queries to the floor, run as plain search with `NX_SEARCH_EXACT_MAX_ROWS=0`, so every statement walks its graph. Recall is reported with and without statements where the exact fallback fired.
- **Synthetic generation.**
  - Chunks are sampled with a fixed seed: proportional to the census path-and-collection frequency for the router-on set, and stratified by collection size band for the router-off set.
  - One question per chunk, from a fixed prompt (hashed) with claude-haiku-4-5, assigned the path its stratum represents.
- **Floors.** 150 per model (router on) and 300 per model (router off), raised if the pilot needs more. The pilot is the first 50 router-off queries per model, on phase 1, and is exploratory. The required n is 9604·s², where s is the pilot's standard deviation of the paired recall difference.

**Statistics.** All CIs are two-sided 95%.
- Recall uses a cluster bootstrap-t: clustered on collection for single-collection statements, and on the query for multi-collection and unrouted-path queries.
- Latency is the geometric-mean ratio A6/A1t, with a cluster bootstrap-t over restart cycles.
- **Decision set:** pooled logged plus synthetic queries. States are also computed on the logged-only subset. A logged-only BAD makes that cell BAD. Any other logged-only state, including a sign flip, is reported but decides nothing.
- **Minimum clusters:** a cell with fewer than 5 clusters is UNKNOWN. Per-cell cluster counts are reported.
- **Pilot:** pilot queries are excluded from the decision set.
- **Stratum guard:** scopes over 10k rows are also tested alone, at a margin of −0.05. A model with no such scope has no stratum-guard cell (N/A).

**Cold latency.**
- A cycle is a restart, then a 16 GB sequential scan to flush the OS cache, then a panel of 16 queries: 8 per model, 4 per router setting, drawn at random from the decision set.
- Both arms run the panel ABBA within the cycle. The cold value is each arm's first touch of a query in the cycle. The arm that goes first alternates across cycles.
- 6 cycles per phase.
- **Router.** In the harness, T (60000, or 0 for router off) is a parameter that mirrors the engine's `NX_SEARCH_EXACT_MAX_ROWS`. A1t's probe counts both models' rows in scope and A6's counts one model's. That difference is part of the design under test.

**Controls.**
- **Degraded arm (positive control).** On phase 1, the router-off set is rerun on A6 with `hnsw.ef_search=40` and `hnsw.iterative_scan=off`. For each model, its router-off recall state must be BAD against A1t. If it is not, the query set cannot detect a margin-sized drop. The degraded arm is a control only and never enters the outcome table's cells.
- **Ceiling check.** This applies to the router-on set, since the degraded arm already proves detectability for router-off. If both arms' mean recall@10 is at least 0.98 there, recall is uninformative.
- Control failures count only when there is no BAD (see the table).

**Per-endpoint states (each model, each router setting, each build pair).**
- Recall: **OK** if the CI lower bound is at least −0.02; **BAD** if the CI upper bound is below −0.02; **UNKNOWN** otherwise. The same rule applies at −0.05 for the stratum guard, where BAD counts as BAD.
- Latency: **OK** if the CI upper bound is at most 1.10; **BAD** if the CI lower bound is above 1.10; **UNKNOWN** otherwise.

**Outcome (exactly one):**

| Condition | Outcome |
|---|---|
| Any BAD in any outcome cell (model × router setting × build pair, plus the stratum guard) | **FAIL-RECALL**, **FAIL-LATENCY** or **FAIL-BOTH**. The split is deferred. |
| No BAD; every state OK; both controls valid | **PASS** |
| No BAD; any UNKNOWN | **INCONCLUSIVE**. There is one escalation, run inside the same phase before it is dropped: double the synthetic queries and cycles for the affected cells, then re-evaluate. If the result is still not PASS or FAIL: **DEFER-UNDERPOWERED**, and the split is deferred. |
| No BAD; no UNKNOWN; a control invalid (degraded arm not BAD, or the ceiling hit) | **DEFER-INSENSITIVE**. The query set cannot resolve the margin, and the split is deferred. |

Every non-PASS outcome defers the per-model split only. Tenant partitioning proceeds with the mixed layout inside each partition. The build-pair difference within each arm is reported as the noise floor.

**Descriptive only:**
- Robustness-0.9@10 (the fraction of queries whose recall@10 is at least 0.9) and the fraction below recall 0.5, for both arms.
- A1t against production as it stands, which measures what tenant partitioning plus the rebuild buys.
- A6's recall gain where it exists.

#### Step 4: migration rehearsal (H6), its own fork

- Copy production into the per-model, tenant-partitioned layout under concurrent writes, build the indexes, cut over.
- Recorded: wall time, peak extra disk, WAL, replica lag, freeze length, per-model and per-tenant exact row reconciliation, a rollback procedure exercised, and backup size.
- **Pass:** it completes, reconciliation is exact, peak disk stays under 70% of the volume, and the rollback is exercised.
- **Fail label:** DESIGN-H6.
- The local-install migration is rehearsed separately on the local substrate.

**Overall:** the design is ready for acceptance when step 2 passes, step 3 passes (or the split is deferred and the tenant-only design is accepted instead), and step 4 passes.

**Thresholds, initialled by Sam 2026-10-05 ("accept as written"):**
- non-inferiority margins: −0.02 recall@10 and 1.10 latency;
- the H4 write-cost ceiling of 1.25;
- the 70% disk ceiling;
- the query floors of 150 and 300 per model.

**Protocol constants, frozen with the above:** 95% two-sided CIs; cluster bootstrap-t; one escalation step (2x) then DEFER-UNDERPOWERED; 6 cycles per phase with a 16-query panel, alternating first arm; two builds per arm; the degraded-arm control (ef_search=40, iterative scan off); the ceiling at 0.98; the stratum guard at −0.05 for scopes over 10k rows; the plan-flip rule; queries run under the main tenant `nexus`, with the second tenant present in every arm.

**Estimated fork time:** step 3 about 8 hours, about 12 with the one escalation, step 4 about 7 hours. Each fork needs Sam's go in conexus's session.

### Critical Assumptions

- [x] F5: the models occupy separate regions — **Status**: Verified
  (research-2, 0.000 cross-model neighbours) — **Method**: Spike
- [x] F6: a per-model graph measurably improves recall — **Status**: Retired.
  Sam ruled the split is shown to work, not to pay. It is replaced by the
  protocol's non-inferiority test (step 3).
- [ ] F7: execution-time partition pruning under RLS leaves one partition's
  index on every search family — **Status**: Unverified, in protocol step 2
  (research-14) — **Method**: Spike
- [ ] The manifest and topic FKs stay expressible through `chunk_identity` at a
  write cost of at most 1.25x — **Status**: Unverified, in protocol step 2
  (research-15) — **Method**: Spike
- [x] The search functions can name their model's table: they are generated
  per registry row, as `plain_search_<dim>` is generated per dimension today
  (research-9, research-11) — **Status**: Verified — **Method**: Source Search

## Proposed Solution

### Approach

**Decided (Sam, 2026-10-05): one vector table per embedding model.** "We need
vector tables that are per embedding"; "just because they have the same dims
doesn't mean jack." The storage key is the embedding **model**. Dimension is a
property of the model, never the key on its own.

- Each model gets its own table with its own typed vector column and its own
  HNSW index. Today that means `voyage-code-3` (1024), `voyage-context-3`
  (1024), bge-768 (768) and MiniLM (384); the table names are settled in the
  technical design. Vectors from two models can then never share a graph,
  whatever their dimension.
- A new model is a new table, created by a changeset, never a new column on an
  existing table.
- Every search already resolves its model through `CollectionRegistry`, and
  `requireHomogeneousModel` already refuses mixed-model searches. The search
  functions become per model rather than per dimension, so a search reaches
  exactly one graph.
- Row-level security stays as the security boundary on every table.
- **Tenant isolation is required (Sam, 2026-10-05: "tenant isolation is a
  *must*, I don't think it's up for question").** Each per-model table is
  LIST-partitioned by tenant, so every (tenant, model) pair has its own heap
  and its own HNSW graph. This works because each table's vector column is
  typed, so RDR-191 V1 does not apply. It is not subject to the evaluation
  protocol's accept/defer gate. The protocol proves only that it works (F7:
  pruning to one partition, under RLS, on every search path) and that the
  migration is feasible (H6). Fork evidence (E1, 2026-10-05): the second cloud
  tenant, `gate-xr789`, is a re-import of the main tenant's corpus (116k of
  431k 1024-d rows), and 14.6% of a main-tenant vector's exact top-32
  neighbours were its near-duplicates. Every filtered walk crosses them, and
  RLS discards them afterwards.

**The tension this reopens.** RDR-191 unified the per-dimension tables
specifically so the document manifest (`catalog_document_chunks`) could carry a
foreign key to the chunks: "a foreign key targets exactly one table." Splitting
by model brings back several chunk tables. The technical design must keep the FK
expressible. The candidate is a thin identity table holding one row per chunk
`(tenant_id, collection, chash, embedding_model)`, without vectors. The manifest
foreign-keys to it, and each per-model vector table foreign-keys to it, both
written in the same transaction. RDR-191 rejected a trigger-maintained registry
table; this one would use no trigger, so its objection (a trigger on the hottest
write path) needs to be re-checked rather than assumed.

### Technical Design

This design is proven by protocol steps 2 to 4 before any production code is
written. Every identifier below is a working name, settled in Phase 1.

**Storage layout.**
- `nexus.embedding_models` registry: one row per model, `(model, dim, table_name)`, written only by changesets. It is today's model knowledge (`catalog_collections.embedding_model`, `DimTables`), rekeyed by model instead of dimension.
- `nexus.chunk_identity`: one row per chunk, `(tenant_id, collection, chash, embedding_model)`.
  - PK `(tenant_id, collection, chash)`; no vector, no text.
  - LIST-partitioned by `tenant_id`, with FORCE RLS and the production `tenant_isolation` policy.
  - This is the single target that a foreign key needs (RDR-191's reason for one table). The two FKs that point at `nexus.chunks` today are re-pointed at it with their semantics unchanged:
    - the manifest FK (catalog-029): ON UPDATE CASCADE, NO ACTION on delete, DEFERRABLE INITIALLY IMMEDIATE;
    - the topic-assignment FK (taxonomy-012): ON UPDATE CASCADE, ON DELETE CASCADE.
- One vector table per model, for example `nexus.vectors_voyage_code_3`, `nexus.vectors_voyage_context_3`, `nexus.vectors_bge_768` and `nexus.vectors_minilm_384`.
  - Columns: today's `chunks` columns (`chunk_text`, the generated `chunk_tsv`, `metadata`, `created_at`) plus exactly one typed `embedding vector(dim)`.
  - PK `(tenant_id, collection, chash)`, with an FK to `chunk_identity` ON UPDATE CASCADE ON DELETE CASCADE.
  - LIST-partitioned by `tenant_id`, FORCE RLS, the same policy.
  - HNSW (`m=16`, `ef_construction=64`), GIN `chunk_tsv` and trigram indexes are declared on the parent, so every tenant partition gets its own.
- **Tenant partitions** are created by a changeset or a tenant-onboarding runbook step, never by engine DDL at runtime. There is no DEFAULT partition, so a write for a tenant with no partition fails loudly rather than landing in a shared heap.
- **Taxonomy** (`taxonomy_centroids` mixes models today, research-9) is split the same way: one centroid table per model, LIST-partitioned by tenant.

**Read path.**
- Every search family in the H5 inventory (research-9) gets a per-model version that is textually identical except for the table it names. The functions are generated per registry row, as `plain_search_<dim>` is generated per dimension today.
- `hybrid_search_<dim>` has no caller and is dropped.
- The engine resolves a collection's model through `CollectionRegistry` (it already does, for `requireHomogeneousModel`) and dispatches by model instead of dimension. `DimTables` becomes a model-keyed table map.
- The cardinality router's probe (`probeSelectedRows`) counts the model's table.
- Tenant pruning comes from the RLS predicate at execution time (F7, research-14).
- The views `live_chunks` and `collection_vector_stats` become per-model, or are rewritten over `chunk_identity` where they need no vector.

**Write path.**
- One transaction inserts the identity row and the model row (H4, research-15), then the manifest row as today.
- Re-embedding a collection under another model moves its rows from one model table to another in one transaction; the identity row's `embedding_model` is updated in the same transaction.
- Collection rename and quarantine moves (an UPDATE of `collection`) cascade from `chunk_identity` to the model rows and to the manifest.

**GC, quarantine, reaper.** The 8 GC/quarantine/hygiene families (research-9) operate on `chunk_identity` where they need no vector. Where they do need one, they operate per model through the registry. The cascades delete model rows when their identity row goes.

**Migration (one engine release, a schema-carrying tag).**
1. Create the new tables and partitions for every tenant present.
2. Copy from `nexus.chunks` per (tenant, model): the identity rows, then the model rows. Rows hidden by RDR-192 are copied as they are; dropping them is a separate decision.
3. Build the indexes after the copy, per partition.
4. Re-point the two FKs: add them NOT VALID to `chunk_identity`, VALIDATE, then drop the old ones.
5. Reconcile row counts exactly per (tenant, model). On any mismatch, abort before the swap.
6. The new engine reads and writes only the new tables. `nexus.chunks` is renamed to `chunks_retired_225`, kept for a 14-day rollback window, and dropped by a later changeset.

- Writes are frozen for the copy. The freeze length, peak disk and WAL come from the H6 rehearsal.
- The migration is **IRREVERSIBLE** once the retired table is dropped. Until then, rollback is the previous engine reading `chunks_retired_225` under its old name. Writes made in the new layout during the window are lost, and the rollback runbook states this.
- Local installs run the same changesets on their bundled PG (one model in the default bge-768 setup).

**Router.** The router stays, with its threshold set from the protocol's measurements (nexus-tu8wp.6). Whether small per-model graphs justify a lower threshold is decided after step 3, outside this RDR.

## Alternatives Considered

### Alternative 1: A column per model in the one table

**Description**: `embedding_code_1024` and `embedding_context_1024`, each with
its own HNSW, under the existing one-of CHECK.

**Pros**: keeps one table and RDR-191's manifest FK unchanged; an index per
model.

**Cons**: the table keeps growing a column per model. Every model's rows share
one heap and one cache, so a scan of one model's rows still reads pages full of
another model's rows. It treats a model as an attribute of a chunk row, when it
is the identity of the vector space.

**Reason for rejection**: Sam's decision for per-model tables (§ Approach).

### Alternative 1b: LIST partition one table by model

**Description**: keep one logical `chunks` table and partition it by
`embedding_model`, giving a heap and an index per model.

**Cons**: every partition must carry the same columns, so all three typed
vector columns (or an untyped one, which RDR-191 V1 shows cannot be indexed)
appear in every partition, mostly empty. The single logical table keeps the FK
simple, which is its one advantage. It is worth comparing against the
identity-table design in the technical design.

### Alternative 2: Partial HNSW indexes per model

**Description**: add `embedding_model` and create `... WHERE embedding_model =
'voyage-code-3'` per model.

**Pros**: no table rewrite into partitions; indexes can be built concurrently.

**Cons**: the planner uses a partial index only when it can prove the query's
WHERE implies the index predicate. vectors-004 measured a silent ~250x sequential
scan when that failed. Fixes Gap 1 only.

### Alternative 3: Partition per collection

**Cons**: hundreds of partitions per tenant times three dimensions; the
cardinality router already serves small collections exact. Rejected.

### Briefly Rejected

- **Partition by dimension**: impossible in pgvector 0.8.2 (RDR-191 V1).
- **Leave the graph, rely on the router**: fixes only single-collection paths.
  Multi-collection, hybrid, combined and taxonomy searches keep walking a
  mixed graph.

## Trade-offs

### Consequences

- Recall and latency improve on every HNSW path, not only single-collection
  ones (pending F6).
- A tenant's cache footprint becomes its own.
- A large, effectively one-way migration of the chunks table.
- Partition maintenance becomes an operation: one partition per new tenant.

### Risks and Mitigations

- **Risk**: runtime pruning fails, and a query scans every partition.
  **Mitigation**: F7 spike; an EXPLAIN-pinned test on each search function.
- **Risk**: the migration runs out of disk or WAL mid-copy on the cloud.
  **Mitigation**: the Step 5b freeze-window derivation and a PITR-fork rehearsal.

### Failure Modes

- **A write for a tenant with no partition.** It fails with Postgres's "no partition of relation found for row" error, surfaced as a 5xx naming the tenant. *Recovery:* the onboarding runbook creates the partition. `nx doctor` gains a row that lists the tenants in `chunk_identity` against the partitions of each model table.
- **A write for a model with no registry row or table.** It is refused by the engine before any SQL, naming the model, as `requireHomogeneousModel` refuses today. *Recovery:* a changeset that adds the model.
- **A pruning regression.** For example, a function edit that stops inlining makes a search touch other tenants' partitions. It is invisible in results, because RLS still filters, and visible only as cost. *Detection:* EXPLAIN-pinned tests per search family (Test Plan) that assert one partition's index.
- **A migration count mismatch, or a validation failure.** The migration aborts before the swap. The old table stays live and the engine version does not change. *Diagnosis:* the reconciliation table in the migration log.
- **Rollback after a bad cutover, within the window.** The previous engine is redeployed against `chunks_retired_225`, renamed back. Writes made since the cutover are lost, and the runbook says so before anyone runs it.
- **A cascade surprise.** A collection UPDATE cascades through three tables. *Test:* rename and quarantine round trips (Test Plan).

## Implementation Plan

### Prerequisites

- [ ] Protocol step 2 PASS: F7, H5 and H4 (research-14 and research-15 verified)
- [ ] Protocol step 3 outcome recorded: PASS keeps the per-model split, and any other outcome defers it while tenant partitioning proceeds
- [ ] Protocol step 4 (H6 migration rehearsal) PASS

### Minimum Viable Validation

On a fork of production, migrated by the real changesets: the engine serves the
protocol's frozen query set through its own search functions. Each query touches
one model's tenant partition, recall against exact is non-inferior to A1t by the
protocol's rule, and the per-(tenant, model) row reconciliation is exact.

### Phase 1: Schema

#### Step 1: Registry, identity table and per-model tables

Changesets for `embedding_models`, `chunk_identity` and the per-model tables,
with partitions for the existing tenants, RLS and indexes. Includes the
per-model centroid tables.

#### Step 2: FK re-pointing and the retired-table rename

The two FKs move to `chunk_identity`, with semantics unchanged.

### Phase 2: Engine dispatch by model

#### Step 1: `DimTables` to a model-keyed map; write path

Write path: the identity row plus the model row in one transaction.

#### Step 2: Per-model search functions and router probe

Generate the per-model search functions. Retarget the router probe. Drop
`hybrid_search_<dim>`.

#### Step 3: GC, quarantine, reaper and taxonomy per model

Over `chunk_identity` and the registry.

### Phase 3: Migration and release

#### Step 1: Copy, build, reconcile and swap changesets

Rehearsed on a PITR fork (engine-release Step 5b): representative scale,
IRREVERSIBLE marked, freeze window from H6.

#### Step 2: Tenant-onboarding runbook and `nx doctor` partition row

#### Step 3: Drop `chunks_retired_225` after 14 days

A later changeset.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Tenant partitions | In scope: `nx doctor` row | In scope: row counts per (tenant, model) | In scope: runbook DETACH and DROP on tenant removal | In scope: doctor compares tenants against partitions | Cluster backups |
| Per-model tables and registry | In scope: registry query | In scope | Deferred: retiring a model is its own changeset when needed | In scope: doctor checks registry against tables | Cluster backups |
| `chunks_retired_225` | N/A | N/A | In scope: Phase 3 Step 3 | N/A | Cluster backups during the window |

## Test Plan

- **Scenario:** each search family on the new layout under the tenant GUC. **Verify:** EXPLAIN shows only that tenant's partition of the query's model (pinned per family).
- **Scenario:** a session with no tenant GUC, and a session for tenant B. **Verify:** zero rows, and no tenant-A row ever returned.
- **Scenario:** insert, delete and re-embed across models; collection rename and quarantine round trip. **Verify:** the identity, model and manifest rows stay consistent; the cascades fire; the deferred manifest FK behaves as in catalog-029.
- **Scenario:** a write for an unknown tenant, and for an unregistered model. **Verify:** a loud refusal naming the tenant or the model.
- **Scenario:** a migration on a seeded store with two tenants and two models, including hidden rows. **Verify:** exact per-(tenant, model) reconciliation; abort on an injected mismatch leaves the old table live.
- **Scenario:** the recall and latency non-inferiority of protocol step 3. **Verify:** the frozen rule's outcome.
- **Scenario:** a local install (bge-768 only). **Verify:** migration and search work with a single model table.

## Finalization Gate

### Contradiction Check

Revision 3's arms and gate (H8) were replaced at revision 4 by Sam's rulings, and
the Approach and protocol now state the same thing. Tenant partitioning is
required and ungated, and the per-model split is shown to work, not to pay. No
contradictions found between research findings, design and protocol.

### Assumption Verification

Two assumptions remain: research-14 (F7 pruning) and research-15 (H4 write cost).
Both are tested in protocol step 2, a prerequisite of implementation. Every other
load-bearing finding is verified (research-1 to -13).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| LIST partitioning with HNSW declared on the parent | PostgreSQL 17, pgvector 0.8.2 | Spike (step 2) |
| Execution-time partition pruning on `current_setting('nexus.tenant')` | PostgreSQL 17 | Spike (step 2) |
| Typed `vector(dim)` per table (RDR-191 V1 is not triggered) | pgvector 0.8.2 | Source search (research-10) |

### Scope Verification

The MVV runs in protocol step 4 and Phase 3 Step 1 on a production fork, through
the real changesets and engine. It is in scope and not deferred.

### Cross-Cutting Concerns

- **Versioning:** a schema-carrying engine tag. Client unchanged (no wire change). The paired-release rules apply only if a later client change rides with it.
- **Build tool compatibility:** jOOQ codegen gains the new tables and functions. The record-count guard is bumped in the same change.
- **Licensing:** N/A.
- **Deployment model:** cloud via conexus's PITR walk rehearsal and deploy. Local via the bundled PG at engine boot.
- **IDE compatibility:** N/A.
- **Incremental adoption:** none. One cutover per install, with a 14-day rollback window.
- **Secret/credential lifecycle:** N/A.
- **Memory management:** index builds per partition bound `maintenance_work_mem` use. The copy streams per (tenant, model).

### Proportionality

The design sections are sized to a storage-layout change with a one-way
migration. The protocol section is long because it is the proof the owner asked
for before building.

## References

- T2 `nexus/research-pgvector-filtered-search-optimizations-2026-10-05` (parts 1-3)
- T2 `nexus/research-devonthink-filtered-vector-search-2026-10-05`
- T2 `nexus/analysis-filtered-search-vs-literature-2026-10-05` (parts 1-3)
- T3 catalog 1.11.696, 1.11.697; papers 1.12.154 to 1.12.164, 1.14.57
- `service/src/main/resources/db/changelog/vectors-004-unify-chunks.xml`
- `docs/rdr/rdr-191-unify-chunk-tables-enable-manifest-fk.md` § Constraints, V1

## Revision History

- 2026-10-05: Created (draft).
- 2026-10-05: Evaluation protocol revision 2, answering T2 critique-rdr225-evaluation-protocol-2026-10-05 (not-justified, 4 critical, 11 significant). Sam ruled the protocol may reject the design (accept/defer gate H8) and that the query set uses both logged and synthetic queries.
- 2026-10-05: Evaluation protocol revision 3, answering T2 critique-rdr225-evaluation-protocol-round2-2026-10-05 (not-justified, 4 critical, 9 significant). Sam ruled: materiality margin 0.10 mean recall@10; path census from transcripts.
- 2026-10-05: Sam ruled tenant isolation required, not subject to the gate. LIST partitioning by tenant joins the design. The gate H8 now compares tenant-partitioned mixed (A1t) against tenant-partitioned per-model (A6), both with the router, because the migration happens for isolation either way. E1 recorded: 0.000 cross-model neighbour fraction (400 samples), and 14.6% of neighbours in the duplicate tenant gate-xr789.
- 2026-10-05: Evaluation protocol revision 4. The step-1 census showed the benefit gate cannot be decided on real queries (10.1% gate population, 13 code and 27 prose distinct queries). Sam ruled: prove it works, not a benefit. H8 is replaced by a non-inferiority test (A6 vs A1t, router on and router off), the feasibility checks (F7, H5, H4) become a local hard gate, and round 3's harness-fidelity, router-threshold, hybrid and taxonomy corrections are adopted. Evidence copied to ~/nexus-evidence/rdr225-2026-10-05/.
- 2026-10-05: Evaluation protocol revision 5, answering T2 critique-rdr225-evaluation-protocol-round4-2026-10-05 (3 critical, 11 significant; 'ready after small text edits'). Disjoint OK/BAD/UNKNOWN states with an exactly-once outcome table and DEFER-UNDERPOWERED; co-resident build pairs for ABBA cycles; degraded-arm positive control and ceiling check; concrete F7 observables and controls; H5 diff script and smoke run; H4 comparator and CI; SQL replay harness validated against the engine; synthetic generation rule; pilot-set floors; frozen protocol constants.
- 2026-10-05: Protocol revision 6 (frozen), applying the round-5 fixes without a further critique round, per Sam ('fix all the things and button this RDR up'). Technical Design, Failure Modes, Implementation Plan, Day 2, Test Plan and Finalization Gate written.
