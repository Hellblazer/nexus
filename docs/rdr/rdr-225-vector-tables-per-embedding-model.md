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
- **F5 (Assumed, fork E1 pending).** Code and prose vectors form separate
  regions: under 5% of a vector's exact nearest neighbours come from the other
  model.
- **F6 (Assumed, fork E2 pending).** A single-model graph for one tenant raises
  code__1-2's agreement on query 1 from 0.60 to at least 0.90, and cuts cold
  HNSW time by at least 2x. E2 also measures the tenant-only step separately.
- **F7 (Assumed, fork feasibility pending).** A table LIST-partitioned by
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

### Evaluation Protocol (pre-registered, revision 5, 2026-10-05)

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
- **Decision set:** pooled logged plus synthetic. Logged-only is the agreement check: if it disagrees in sign with pooled, the result is INCONCLUSIVE.
- **Stratum guard:** scopes over 10k rows are also tested alone, at a margin of −0.05.

**Cold latency.**
- A cycle is a restart, then a 16 GB sequential scan to flush the OS cache, then a panel of 16 queries: 8 per model, 4 per router setting, drawn at random from the decision set.
- Both arms run the panel ABBA within the cycle.
- 5 cycles per phase.

**Controls.**
- **Degraded arm (positive control).** On phase 1, the router-off set is rerun on A6 with `hnsw.ef_search=40` and `hnsw.iterative_scan=off`. The rule must return FAIL against A1t. If it does not, the query set cannot detect a margin-sized drop, and the outcome is INCONCLUSIVE.
- **Ceiling check.** If both arms' mean recall@10 is at least 0.98 on a set, recall is uninformative there and the outcome is INCONCLUSIVE.

**Per-endpoint states (each model, each router setting, each build pair).**
- Recall: **OK** if the CI lower bound is at least −0.02; **BAD** if the CI upper bound is below −0.02; **UNKNOWN** otherwise. The same rule applies at −0.05 for the stratum guard, where BAD counts as BAD.
- Latency: **OK** if the CI upper bound is at most 1.10; **BAD** if the CI lower bound is above 1.10; **UNKNOWN** otherwise.

**Outcome (exactly one):**

| Condition | Outcome |
|---|---|
| Any BAD, in any model, setting or build pair | **FAIL-RECALL** or **FAIL-LATENCY** (recall listed first if both). The split is deferred. |
| No BAD; every state OK; both controls valid | **PASS** |
| No BAD; any UNKNOWN, or a control invalid | **INCONCLUSIVE**. One escalation: double the synthetic queries and cycles for the affected cells, then re-evaluate. Still not PASS or FAIL afterwards: **DEFER-UNDERPOWERED**, and the split is deferred. |

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

**Protocol constants, frozen with the above:** 95% two-sided CIs; cluster bootstrap-t; one escalation step (2x) then DEFER-UNDERPOWERED; 5 cycles per phase with a 16-query panel; two builds per arm; the degraded-arm control (ef_search=40, iterative scan off); the ceiling at 0.98; the stratum guard at −0.05 for scopes over 10k rows; the plan-flip rule; queries run under the main tenant `nexus`, with the second tenant present in every arm.

**Estimated fork time:** step 3 about 8 hours, step 4 about 7 hours. Each fork needs Sam's go in conexus's session.

### Critical Assumptions

- [ ] F5: the models occupy separate regions, or mix enough to poison
  neighbourhoods — **Status**: Unverified — **Method**: Spike (fork E1)
- [ ] F6: a per-model graph measurably improves recall and cold time —
  **Status**: Unverified — **Method**: Spike (fork E2)
- [ ] F7: runtime partition pruning on `current_setting('nexus.tenant')` leaves
  one partition's HNSW scan, with iterative scan — **Status**: Unverified —
  **Method**: Spike (fork)
- [ ] The manifest FK stays expressible with per-model tables, through an
  identity table written in the same transaction, without a trigger and without
  a measurable write-path cost — **Status**: Unverified — **Method**: Spike
- [ ] The search functions (`plain_search_<dim>`, text-gated, combined,
  metadata-scoped, taxonomy) can name the model so the planner picks the right
  partition or partial index at plan time — **Status**: Unverified —
  **Method**: Source Search + Spike

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

To be completed after the fork results. Questions it must settle:

- The model key: a new `embedding_model` column, written from the collection's
  registry entry at insert, or a derived key.
- Partition creation for a new tenant: an ops step or changeset at tenant
  creation, given the handful of tenants. Engine runtime DDL is not wanted.
- Search-function changes so the model key reaches the plan as a constant,
  under `plan_cache_mode = force_custom_plan`.
- The migration: copy `nexus.chunks` into the partitioned layout, rebuild
  indexes per partition, then swap. Includes the copy-peak disk, WAL budget and
  the rollback decision required by the engine-release skill's Step 5b.
  RDR-192's hidden rows could be dropped in the same copy.
- Interaction with the cardinality router (nexus-tu8wp.6): with small per-model,
  per-tenant graphs, the router's threshold may change.

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

To be completed with the technical design.

## Implementation Plan

### Prerequisites

- [ ] Fork results E1, E2 and the partition feasibility check (F5 to F7)
- [ ] All Critical Assumptions verified

### Minimum Viable Validation

On a production fork: the partitioned layout, migrated from a copy of
production, serves the B query set through the engine's own search functions
with one partition's HNSW per query. Recall against exact and cold latency must
be at least as good as F6's measurement.

### Phase 1: Code Implementation

To be planned after the design is settled.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Per-tenant/model partitions | To design | To design | To design | To design | Covered by cluster backups |

## Test Plan

To be completed with the technical design. It must include an EXPLAIN-pinned
plan test per search function and a recall gate against exact ground truth
using Robustness-δ@K (catalog 1.12.162).

## Finalization Gate

To be completed before acceptance.

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
