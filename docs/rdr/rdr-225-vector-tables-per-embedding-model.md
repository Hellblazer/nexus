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
related_rdrs: [RDR-152, RDR-155, RDR-156, RDR-164, RDR-169, RDR-191, RDR-192, RDR-194, RDR-204]
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

### Terms used here

- **HNSW**: the approximate nearest-neighbour graph index pgvector builds.
- **Exact search**: scanning every candidate row and sorting by distance.
- **Iterative scan** and **`ef_search`**: pgvector settings that keep a filtered HNSW walk going past its first candidates.
- **Tenant**: one isolated customer namespace.
- **RLS** (row-level security): Postgres policies that hide other tenants' rows. **FORCE** applies them to the table owner too. The **tenant GUC** is the per-transaction setting `nexus.tenant` that the policies read.
- **chash**: the SHA-256 of a chunk's text, which is its identity.
- **Manifest**: `catalog_document_chunks`, listing which chunks make up each document.
- **GC, reaper, quarantine**: the background paths that move unreferenced chunks aside and later delete them.
- **live(c)**: RDR-192's rule for which chunks a search may return. Hidden rows are chunks no live manifest row claims.
- **Leaf**: a physical partition table.
- **PITR fork**: a point-in-time copy of the production database, used for rehearsals.
- **Changeset**: one Liquibase schema migration step. The schema walk runs at engine boot.
- **The router** (cardinality router, nexus-tu8wp.6): sends a plain search to exact search when the selected collections hold at most a threshold of rows, and to HNSW otherwise.
- **Protocol labels** (A1t, A6, F7, H4 to H6, T1 to T3, E1): defined in § Evaluation Protocol. **ABBA** means running the two arms in the order A, B, B, A within a cycle, so neither always goes first.
- **SECURITY DEFINER**: a Postgres function that runs with its owner's privileges. **DETACH**: removing a partition from its parent.

### Enumerated gaps to close

#### Gap 1: Code and prose vectors share one HNSW graph

Links between vectors of different models are built from meaningless distances.
Measured on a production fork (E1, research-2), the two models' vectors are
disjoint: in a 400-vector sample, no vector's exact top-32 neighbours included
one from the other model.
The graph is therefore two islands joined only by arbitrary links, with a single
entry point in one of them. One graph holds two embedding spaces it cannot
compare. This RDR fixes that on principle and does not claim a recall gain from
it: HNSW's poor agreement with exact search on filtered queries (0.60 to 0.80,
F2) is filter- and graph-driven, and the split's effect on it is measured only as
non-inferiority (protocol step 3).

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
| RDR-164 | Adjacent | Collection rename, the canonical branch the write path keeps. |
| RDR-169 | Adjacent | Reference-only retention: `chunk_text` is nullable for those rows, a constraint each leaf inherits. |
| RDR-194 | Adjacent | The topic-assignment FK into chunks, which becomes four-column. |
| RDR-204 | Adjacent | Owns `nexus.embedding_models`, which this RDR reuses as the model key. |
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
standard-8 and memory-16. Raw JSON is in `~/nexus-evidence/rdr225-2026-10-05/` with `SHA256SUMS`.

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
  regions: 0.000 of a vector's exact top-32 neighbours come from the other
  model (400 samples).
- **F6 (Retired: superseded by the non-inferiority test).** A single-model graph for one tenant raises
  code__1-2's agreement on query 1 from 0.60 to at least 0.90, and cuts cold
  HNSW time by at least 2x. E2 also measures the tenant-only step separately.
- **F7 (Verified, research-14).** On `chunks` LIST-partitioned by
  `embedding_model` and then `tenant_id`, with literal model and tenant predicates
  under `force_custom_plan`, every search family is pruned at plan time to one
  (model, tenant) leaf.

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
- **✅ Verified** (spike), research-14. F7: on the model-then-tenant layout, every SQL search family prunes to exactly one (model, tenant) leaf. All 156 of 156 checks pass, after `assign_from_chashes` was given the new `embedding_model` column the design requires.
  *Source: ~/nexus-evidence/rdr225-2026-10-05/step2/f7_a6.json.*
- **❓ Assumed** (spike, pending step 2), research-15. H4: the partitioned layout, with its four-column FKs, costs at most 1.25x on every write path.
- **⚠️ Documented** (docs only), research-16. The literature crossover points (Veda, Compass) were measured warm, and VADER finds query-filter correlation.
  *Source: knowledge__dt-papers.*

### Evaluation Protocol (pre-registered, revision 6, frozen, 2026-10-05)

Sam: "let us prove out our design first this time rather than just reflexively
jumping." Five critique rounds preceded this revision (T2
`nexus/critique-rdr225-evaluation-protocol-2026-10-05`, `-round2-` to `-round5-`).

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
- **Equivalence.** A6 runs the engine's search functions with only the added `embedding_model` and `tenant_id` predicates. Their diff is recorded.
- **Plan check.** An untimed EXPLAIN pass precedes the timed pass. Planning is deterministic, so a plan that differs from the declared one is a result, not noise. The declared plans are: the (model, tenant) leaf's HNSW index, or the PK-prefix bitmap scan for exact.
  - A flip in A6 fails that statement class for A6 (DESIGN-F7).
  - A flip in A1t is a harness error, fixed before any decision.
  - Flips are counted per arm.
  - No timing runs under EXPLAIN ANALYZE.
- **Execution path.** Both arms run through one SQL replay harness. It issues each engine statement, as `runPlainSearchStatement`, `hybridSearch` and the combined, aspect and graph-hop paths generate it, under the settings above. The router decision is replicated by the same bounded count, taken over the arm's own table: `chunks` for A1t, and for A6 the query model's partition of `chunks`.
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
  - the plan's relation list, which must name exactly one leaf (plan-time pruning emits no "Subplans Removed");
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
- Comparator: today's `chunks` against the model-and-tenant partitioned `chunks`, with the four-column FKs. HNSW is present in both.
- Same seeded vectors as F7.
- Paths: bulk 10k, single insert, update, delete, rename.
- Runs are paired and alternated ABAB, 5 pairs per path. The CI is a t-interval on the log ratio across pairs.
- **Pass:** the CI upper bound of the time ratio is at most 1.25 on every path, with the extra ms per chunk stated. The step-3 fork repeats H4 on real vectors, descriptively.
- **Fail label:** DESIGN-H4.

**Addendum (2026-10-05, after gate round 2): tenant scale.** It is part of
step 2's hard gate, with these thresholds set by this addendum:

| Check | Pass rule | Fail label |
| --- | --- | --- |
| **T1, tenant creation.** Time `create_tenant_partitions` for one new tenant, with 2, 100 and 300 existing tenants, and record the locks it takes on the referencing tables (`pg_locks`) | p95 creation time at most 2 s at 300 tenants, and no lock held on a referencing table for more than 2 s | DESIGN-T1 |
| **T2, planning.** Plan time of each search family with literal keys, at 2 and at 300 tenants | p95 at 300 tenants at most 2x the 2-tenant p95, and at most 25 ms | DESIGN-T2 |
| **T3, deferred FK.** The `purge_trash` pattern: `SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED`, then delete and re-insert within one transaction, against the partitioned parent | commits, and an orphaned manifest row fails at commit | DESIGN-T3 |

#### Step 3: non-inferiority on a fork (standard-8)

**Arms and builds.**
- **A1t:** today's single table with both models, LIST-partitioned by tenant, plus the router.
- **A6:** `chunks` LIST-partitioned by model, then by tenant, plus the router.

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

- Copy production into the layout step 3 selects, under write freeze (addendum, 2026-10-05: the design freezes writes; the text said "under concurrent writes"), build the indexes, cut over.
- Recorded: wall time, peak extra disk, WAL, replica lag, freeze length, per-model and per-tenant exact row reconciliation, a rollback exercised as a PITR restore on the fork, and backup size.
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
- [x] F7: plan-time pruning on literal `embedding_model` and `tenant_id`
  leaves one leaf's index on every search family — **Status**: Verified
  (research-14) — **Method**: Spike
- [ ] The manifest, topic and orphaned-at FKs work as four-column FKs to the
  partitioned parent, deferrable as today, at a write cost of at most 1.25x — **Status**: Unverified, in protocol step 2
  (research-15) — **Method**: Spike
- [ ] Tenant creation can create a tenant's leaves in its transaction, within
  the step-2 cost budget, and search planning stays bounded with 300 tenants
  present — **Status**: Unverified, in protocol step 2 — **Method**: Spike

## Proposed Solution

### Approach

**Decided (Sam, 2026-10-05).**
- On vectors: "We need vector tables that are per embedding"; "just because they have the same dims doesn't mean jack."
- On tenants: "tenant isolation is a *must*, I don't think it's up for question."
- After gate round 1: per-model physical tables are realised as **partitions of one logical table**, and tenant partitions are **created at tenant creation**.

**The storage key is the embedding model.** Dimension is a property of the model, never a key on its own.
- `nexus.chunks` stays one logical table.
- It is LIST-partitioned by `embedding_model`, and each model partition is LIST-partitioned by `tenant_id`.
- Every (model, tenant) leaf is a physical table with its own heap and its own HNSW, full-text and trigram indexes. All leaves still share one buffer pool.
- Two models never share a graph, whatever their dimension. Two tenants never share a heap or a graph.
- Row-level security stays as the security boundary on every leaf.

**Why one logical table.** RDR-191 unified the chunk tables so that the document manifest could carry a foreign key to its chunks: "a foreign key targets exactly one table." PostgreSQL lets a foreign key reference a partitioned table. The manifest therefore keeps a single, checked reference to the exact chunk row, and RDR-191's guarantee is unchanged. Gate round 1 rejected the alternative, separate tables behind an identity table, because nothing would then enforce that every identity row has its vector row (§ Alternatives Considered).

**Tenants.** A tenant exists from its first token row. An AFTER INSERT trigger on `service_tokens` creates the tenant's leaves (one per model, in `chunks` and `taxonomy_centroids`) if they do not exist. The trigger covers every writer of a token row: `issueToken`, `ensureBootstrapToken` and `rotateTokens`. The migration also creates leaves for the bootstrap tenant `default` unconditionally. Every tenant is isolated from its first write, and there is no shared DEFAULT partition.

**What is shown before building, and what is not claimed.**
- The evaluation protocol shows this layout works: pruning on every search family, bounded write cost, a safe migration, and recall and latency no worse than tenant isolation alone.
- It does not claim the per-model split raises recall. E1 (research-2) found no shared true nearest neighbour in a 400-vector sample. F2's recall loss is filter- and graph-driven, and the protocol holds the split only to non-inferiority.
- The split is adopted on principle: one graph per embedding space.

### Technical Design

Proven by protocol steps 2 to 4 before production code. Working names are
settled in Phase 1. Where the code is quoted, the source is develop
`a798f07ea`.

**Model key.** `nexus.embedding_models` already exists (catalog-036:61-81, RDR-204), with columns `(embedding_model, dimension, provider)` and the tokens `voyage-code-3`, `voyage-context-3`, `bge-base-en-v15-768` and `minilm-l6-v2-384`. `catalog_collections.embedding_model` references it (hygiene-002:291). This RDR adds no registry.
- **Model column.** `nexus.chunks` gains `embedding_model text NOT NULL`. The engine writes it from the collection's `catalog_collections` row, which it must now pass explicitly: `requireHomogeneousModel` validates the model but returns nothing today.
- **Model FK.** A new UNIQUE `(tenant_id, name, embedding_model)` on `catalog_collections` backs a composite FK `(tenant_id, collection, embedding_model)` from `chunks`, ON DELETE RESTRICT, as `chunks_collection_fk` is today (fk-004). It replaces `chunks_collection_fk`. A chunk therefore cannot be filed under a model other than its collection's.
- **Dimension CHECK.** Each model partition carries a CHECK that its model's vector column is the only non-null one, for example `embedding_1024 IS NOT NULL AND embedding_768 IS NULL AND embedding_384 IS NULL` for the 1024-d models. A row whose vector dimension disagrees with its model cannot be stored. During migration, rows from hygiene-002's "disputed" collections that would fail the CHECK are not allowed to wedge the upgrade (the 2026-08-16 never-wedge directive). They are copied into a per-tenant holding collection under the model their stored dimension implies, and listed in the walk log by count and collection. Reconciliation counts them on that side.
- **Columns.** Every leaf carries all three typed vector columns, with an HNSW index on each, two of them always empty. Step 2's tenant-scale measurement includes this cost.

**Keys.** PostgreSQL requires a partitioned table's primary key to include its partition keys, so the PK becomes `(tenant_id, collection, chash, embedding_model)`. `(tenant_id, collection, chash)` still identifies one chunk, because a collection has exactly one model (the composite FK above). Every table that references a chunk gains `embedding_model text NOT NULL`, written by the same code that writes its collection. NOT NULL is required: the FKs are MATCH SIMPLE, so a NULL would exempt the row from the check. Their FKs become four-column, with the same actions, deferrability and names.

**Everything that names `nexus.chunks` changes.** The complete list is generated and pinned in Phase 1 Step 1 by H5's script. It is not hand-kept. Known at `a798f07ea`:

| Object | Kind | New form |
|---|---|---|
| `fk_catalog_chunks_chunk` (catalog-029) | FK into chunks | 4-column; ON UPDATE CASCADE, NO ACTION, DEFERRABLE INITIALLY IMMEDIATE. Same name: Java issues `SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED` (CatalogRepository.java:8297), and so do plpgsql (catalog-029:125, 251) |
| `topic_assignments_chunk_fk` (taxonomy-012) | FK into chunks | 4-column; ON UPDATE CASCADE, ON DELETE CASCADE; same name |
| `chunk_orphaned_at_chunk_fk` (vectors-021) | FK into chunks | 4-column; ON UPDATE CASCADE, ON DELETE CASCADE; same name |
| `chunks_collection_fk` (fk-004) | FK out of chunks | replaced by the composite model FK |
| Every `ON CONFLICT (tenant_id, collection, chash)` target whose arbiter is `nexus.chunks`, and every `INSERT INTO nexus.chunks` column list: the Java upsert at PgVectorRepository.java:1168 and plpgsql in vectors-022, vectors-024, catalog-033, catalog-042, catalog-043, hygiene-008 and others the script finds. `chunk_orphaned_at` keeps its own three-column arbiter, as do other tables whose PKs do not change. Writers of the referencing tables (for example the manifest upsert, CatalogRepository.java:5067) supply `embedding_model` | write sites | Rewritten. The conflict target becomes the four-column PK and the insert supplies `embedding_model`. Redefined once, at migration step 7.8, against the swapped table |
| `stamp_chunks_on_manifest_delete` and `_update` (vectors-021-3) | trigger functions inserting into `chunk_orphaned_at` and joining `chunks` | Rewritten to carry `embedding_model` from the manifest row and to join on all four columns |
| `nexus.live_chunks`, `nexus.collection_vector_stats` (vectors-019:1118-1190), `nexus.diag_chash_conformance` (taxonomy-011-8) | views over `chunks` | Dropped before the swap and recreated over the new table in the same transaction. They bind by OID, so they would otherwise follow the retired table. `diag_chash_conformance` may be owned by a superuser (taxonomy-011), so its drop and recreate runs as that owner, or is a documented operator step if the migration role cannot |
| `chunks_gate_probe_owner_read` policy (vectors-029); the `tenant_isolation` policy; FORCE RLS | RLS | Created on every model partition and every leaf. PostgreSQL does not inherit RLS state or policies to partitions (the step-2 prototype applied them leaf by leaf) |
| `chunks_content_retention_consistent` (vectors-014), `chunks_chash_octet_check` (vectors-004:313), `idx_chunks_tenant_chash` (vectors-004:333), the HNSW, GIN and trigram indexes | constraints and indexes | Declared on the parent so every leaf inherits them. On the retired table they are renamed with a `_retired_225` suffix before the swap, because index and constraint names are schema-wide |
| Grants on `nexus.chunks` (grants-nexus-svc.xml, grants-nexus-diag.xml) | grants | Re-issued on the new parent. These are `runAlways` changesets, so they re-run on every walk |
| `ChunksIsolationCheck.verifyAtStartup` (Main.java:131), the doctor RLS canary (`_RLS_TENANT_TABLES`, health.py:3408) | isolation checks | Extended to assert FORCE RLS and the policy on every model partition and leaf |
| `taxonomy_centroids` (taxonomy-007) and its upsert at TaxonomyCentroidRepository.java:124 | mixed-model table | Gains `embedding_model`, partitioned the same way. The upsert's conflict target includes the model. A centroid whose dimension changes (nexus-2qryr) is deleted and re-inserted, not upserted, because an upsert cannot move a row across partitions |

**Tenant creation.**
- A tenant exists from the moment its first token row is written. Four sites write token rows: `issueToken` (TokenStore.java:401, for `/v1/tenants/create`, `/v1/service-tokens/issue` and `/v1/data-tokens/mint`), `ensureBootstrapToken` (:242, the `default` tenant at every boot, after the walk) and `rotateTokens` (:585).
- An AFTER INSERT trigger on `service_tokens` calls `nexus.create_tenant_partitions(tenant)` in the inserting transaction, so every site is covered.
- The function is idempotent: it creates only leaves that do not exist. A second token for the same tenant costs one catalog check.
- For each model partition of `chunks` and of `taxonomy_centroids`, it creates the tenant's leaf, enables and forces RLS, and creates the policies.
- It is SECURITY DEFINER with a fixed `search_path`, EXECUTE revoked from PUBLIC and granted to the engine role only. It quotes with `format('%I')` and `%L`. Leaf names combine the model's short code with a 16-hex-character hash of the tenant (for example `chunks_vcode3_t_<hash>`), and a collision check against the existing leaf's bound refuses a second tenant hashing to the same name. Concurrent first tokens for one tenant are serialised by a transaction-scoped advisory lock on the tenant. New leaves receive the same grants as the parent, including MAINTAIN for the purge VACUUM.
- Creating a partition of a referenced partitioned table takes locks on the referencing tables. Step 2 (addendum) measures those locks and their duration.
- Removing a tenant is DETACH and DROP of its leaves (runbook).

**New model.** Adding a model is a changeset that inserts the `embedding_models` row and calls `nexus.create_model_partition(model)`. That function creates the model partition with its dimension CHECK, then a leaf for every existing tenant. A write for a model with no partition is refused by the engine before any SQL, naming the model.

**Read path.**
- The search families keep their per-dimension function bodies. vectors-019 wrote them once per dimension; they are not generated per model.
- Each body gains `embedding_model = $model AND tenant_id = $tenant` predicates, so that under `force_custom_plan` the planner prunes to one leaf at plan time. RLS is kept as well.
- The engine passes the model it already resolves through `CollectionRegistry`.
- The router's probe (`probeSelectedRows`, on branch `feature/nexus-tu8wp.6-cardinality-router`, which merges first; see Prerequisites) adds the same predicates.
- `hybrid_search_<dim>` has no caller and is left as it is.

**Write path, rename, quarantine.**
- Inserts land in their leaf through the partition keys. Every write site in the inventory above supplies `embedding_model` and uses the four-column conflict target.
- **Rename** (canonical branch): insert the new registry row, then UPDATE the children's `collection`. Model and tenant are unchanged, so the UPDATE stays within the leaf, and the manifest follows by ON UPDATE CASCADE.
- **Rename** (cross-model COPY branch, CatalogRepository ~9096-9104): the explicit manifest UPDATE also sets `embedding_model` to the target collection's model.
- **Quarantine**: `reaper_quarantine_chunks` DELETE … RETURNING into INSERT (vectors-024). It registers the quarantine sibling with the origin's model, so the row stays in its model partition. Its INSERT is among the rewritten write sites.
- **Cross-model migration** registers a new collection under the target model. There is no in-place re-embed, and none is added.

**Migration** (one engine release, a schema-carrying tag):
- All of steps 2 to 7 run in **one transactional changeset**. A failure anywhere rolls back everything, so the old layout and the live referencing tables are untouched, and a retry starts clean.
- Writes are frozen for the walk.
  - **Locally:** the engine is not yet serving (Main.java:113-123).
  - **In the cloud:** the freeze mechanism and the deploy topology (rolling or stop-start) are confirmed with conexus before the walk, recorded as an open ask in T2 22408 §2(d). That confirmation is a Phase 3 prerequisite.

The steps:
1. Freeze writes.
2. Create the partitioned `chunks_new` with its PK and constraints but without secondary indexes, its model partitions (each with its dimension CHECK), and the tenant leaves for `default` and every tenant with a token row or a chunk. The same goes for `taxonomy_centroids_new`. `create_tenant_partitions` takes the target parent as a parameter, so it serves the migration (`_new` tables) and normal operation alike.
3. Copy per (model, tenant), with `embedding_model` taken from `catalog_collections`. Rows hidden by RDR-192's liveness rule (chunks that no live manifest row claims) are copied unchanged. A row that fails its dimension CHECK fails the changeset.
4. Add `embedding_model` to the referencing tables as nullable, backfill it, then SET NOT NULL. The manifest triggers keep their old text until step 7.8. Old `chunks` has no `embedding_model`, so their three-column joins still work over the backfill.
5. Create the parent's indexes, which builds them on every leaf after the copy. Then ANALYZE every leaf and parent. Autovacuum never analyzes a partitioned parent, and a bulk-copied table without statistics turns the planner off HNSW (vectors-004 Step 5b, BUG-0148).
6. Reconcile row counts exactly per (model, tenant). A mismatch fails the changeset.
7. Swap:
   1. Drop the three inbound FKs and the three views.
   2. Rename the old table's indexes and constraints with the `_retired_225` suffix.
   3. Drop the old table's outbound `chunks_collection_fk`, so it cannot block registry deletes.
   4. Rename `chunks` to `chunks_retired_225`, and `chunks_new` to `chunks`.
   5. Rename the new indexes to the old names.
   6. Add the four-column inbound FKs with their old names (NOT VALID, then VALIDATE), and the outbound composite model FK on `chunks`. Rename and swap `taxonomy_centroids` the same way.
   7. Recreate the views, policies and grants.
   8. Redefine, once and only here, the manifest triggers and every rewritten write-site function, against the swapped table.
8. Unfreeze writes.

**Failure and rollback.**
- A failed walk exits the engine at boot (`Main.java:116-121`, `System.exit(1)`), with the old layout intact because the changeset is one transaction.
  - **In the cloud:** what keeps serving depends on the topology conexus confirms.
  - **Locally:** the install is down until the previous engine version is reinstalled.
- After the changeset commits, the change is **IRREVERSIBLE** in place.
  - **Cloud rollback** is a restore from PITR (point-in-time recovery) to before the walk. Writes made since the walk are lost.
  - **Local rollback** does not exist. `chunks_retired_225` is kept for 14 days only as a data-recovery source, and a later changeset drops it.
- **Local disk preflight.** Before the walk, the engine checks that free disk is at least 2.2x the chunks table plus its indexes (inferred, not measured: 1x for the copy, 1x while the retired table is kept, 0.2x headroom). If it is not, the engine refuses to start, naming the shortfall. Step 4 of the protocol measures the real peak.
- A local install has the `default` tenant (and any it creates), so it holds eight leaves (one per model partition of `chunks` and of `taxonomy_centroids`), mostly empty. It gains nothing in isolation and still migrates, so that every install runs one schema.

**If step 3 is not PASS** (the per-model split is deferred):
- `nexus.chunks` is LIST-partitioned by `tenant_id` only, with no model level.
- The PK and the referencing FKs keep three columns, and no `embedding_model` column, composite model FK or dimension CHECK is added.
- `create_tenant_partitions` creates one leaf per tenant (and one per tenant for `taxonomy_centroids`), and there is no `create_model_partition`.
- The migration drops step 4 and the model parts of steps 2, 3 and 6.
- The views, policies, grants, isolation checks, ANALYZE, swap, failure handling and the Day 2 tenant rows are as above.
- The Day 2 "Model partitions" row and the model-mismatch test do not apply.
- Step 4 of the protocol rehearses whichever layout step 3 selects.

**Router.** The router stays (nexus-tu8wp.6). Its threshold is set from the protocol's measurements, outside this RDR.

## Alternatives Considered

### Alternative 1: Separate per-model tables behind an identity table

**Description**: One independent table per model, plus a thin `chunk_identity`
table that the manifest and topic FKs point at.

**Pros**: Each model's table is fully independent, and no NULL vector columns
are carried.

**Cons**: The manifest-to-identity and vector-to-identity directions can be
enforced by FKs. The other direction, "every identity row has exactly one vector
row", cannot be. Enforcing it takes a constraint trigger on the hottest write
path, which is the objection RDR-191 recorded against `chunks_registry`.
Otherwise RDR-191's guarantee is lost, and its detection checks were retired in
catalog-030.

**Reason for rejection**: Gate round 1 (critique r1, Critical 2). Sam chose
partitions of one table.

### Alternative 2: A column per model in the one unpartitioned table

**Cons**: Every model's rows share one heap and one cache. The table grows a
column per model. Tenants are not isolated.

**Reason for rejection**: It fails the tenant-isolation requirement and keeps
the shared heap.

### Alternative 3: Partial HNSW indexes per model

**Cons**: The planner uses a partial index only when it can prove the query's
WHERE implies the index predicate. vectors-004 measured a silent ~250x
sequential scan when that failed. There is no tenant isolation.

**Reason for rejection**: Fragile planning, and no isolation.

### Alternative 4: Partition per collection

**Cons**: Hundreds of leaves per tenant. The router already serves small
collections exact.

**Reason for rejection**: The partition count, for no gain the router does not
already give.

### Briefly Rejected

- **Partition by dimension**: impossible in pgvector 0.8.2 (RDR-191 V1).
- **A DEFAULT tenant partition**: a tenant there shares a heap and a graph, so
  it is not isolated (Sam, 2026-10-05).
- **Leave the graph and rely on the router**: fixes only single-collection
  plain search.

## Trade-offs

### Consequences

- Each (model, tenant) pair gets its own graph and heap, so another tenant's rows
  never enter a filtered walk. The buffer pool is still shared.
- No recall improvement is claimed. The split is held to non-inferiority
  (protocol step 3).
- Every chunk-referencing table carries a redundant `embedding_model` column,
  checked by FK.
- Tenant creation does DDL (leaf creation), at a cost measured in step 2.
- A large, effectively one-way migration of the chunks table.

### Risks and Mitigations

- **Risk**: plan-time pruning fails for some family, and a query scans every
  leaf. **Mitigation**: F7 in step 2, plus an EXPLAIN-pinned test per family.
- **Risk**: hundreds of test tenants make tenant creation or planning slow.
  **Mitigation**: step 2 measures both. Pruning on literal keys keeps planning
  to one leaf.
- **Risk**: the migration runs out of disk or WAL. **Mitigation**: the step 4
  rehearsal, the engine-release Step 5b derivation, and the local disk
  preflight.

### Failure Modes

- **A write for a tenant with no leaf.** `issueToken` creates the leaves, so this needs a bypassed token path or a dropped leaf.
  - PostgreSQL raises SQLSTATE 23514, "no partition of relation found for row". `HttpUtil.sendTypedDbError` would map that to 409 as a caller error. The engine maps this specific error to a 500 naming the tenant, because it is an engine invariant, not a caller mistake.
  - *Recovery:* call `create_tenant_partitions`. The doctor row lists tenants with tokens against leaves.
- **A chunk filed under the wrong model, or with the wrong vector dimension.** The composite model FK and the dimension CHECK refuse it at write time.
- **A pruning regression.** A function edit can stop plan-time pruning, so that a search touches every leaf.
  - RLS still filters, so it shows only as cost.
  - *Detection:* EXPLAIN-pinned tests per family.
- **A migration failure.** The single changeset rolls back and the engine exits at boot, with the data unchanged.
  - *Diagnosis:* the reconciliation and CHECK reports in the walk log.
- **A bad cutover after commit.**
  - Cloud: PITR restore. Writes since the walk are lost, and the runbook says so before anyone runs it.
  - Local: no rollback. Data recovery is from `chunks_retired_225` for 14 days.
- **Missing statistics after the copy.** The planner leaves HNSW. Migration step 5 analyzes every leaf and parent, and a test asserts the post-migration plans.
- **A write site missed in the rewrite.** Its `ON CONFLICT (tenant_id, collection, chash)` fails with "no unique or exclusion constraint matching". The H5 script's pinned inventory is the guard, and the Test Plan runs every write family.

## Implementation Plan

### Prerequisites

- [ ] The cardinality router (`feature/nexus-tu8wp.6-cardinality-router`) is merged to develop.
- [ ] Protocol step 2 PASS: F7, H5 and H4, plus the step-2 addendum (tenant creation, planning at 300 tenants, the deferrable 4-column FK).
- [ ] Protocol step 3 outcome recorded. PASS keeps the model level; any other outcome uses the tenant-only layout.
- [ ] Protocol step 4 (H6, under write freeze) PASS.
- [ ] conexus confirms the cloud freeze mechanism and deploy topology (T2 22408 §2(d)).

### Minimum Viable Validation

The real changeset migrates a fork of production. On it:
- The engine serves the protocol's frozen query set through its own search functions.
- Each plan touches one (model, tenant) leaf.
- Recall against exact search is non-inferior to tenant-only partitioning, by the protocol's rule.
- Row reconciliation per (model, tenant) is exact.

### Phase 1: Schema

#### Step 1: Pinned inventory

Run H5's script to generate and pin every object naming `nexus.chunks` or
`taxonomy_centroids`: FKs, triggers, views, policies, grants, write sites and
conflict targets. Each item gets its new form, as in the Technical Design table.

#### Step 2: Changesets for the new objects

- The UNIQUE on `catalog_collections`.
- `create_tenant_partitions` and `create_model_partition`.
- The `service_tokens` AFTER INSERT trigger. The redefinitions of write sites and manifest triggers are part of the migration changeset (step 7.8), not a separate earlier changeset.
- The same treatment for `taxonomy_centroids`.

#### Step 3: The migration changeset

The single transactional changeset in the Technical Design (steps 1 to 8), plus
the local disk preflight.

### Phase 2: Engine

#### Step 1: Write path and tenant creation

- `issueToken` calls `create_tenant_partitions`.
- Every chunk-writing and chunk-referencing write supplies `embedding_model`.
- The Java upsert's conflict target becomes the four-column PK.
- The no-leaf error is mapped to a 500.
- The jOOQ record-count guard is bumped in the same change as the schema.

#### Step 2: Read path

Add the model and tenant predicates to every search family and to the router
probe.

#### Step 3: Checks

- Extend `ChunksIsolationCheck` and the doctor RLS canary to every model partition and leaf.
- Add the doctor rows comparing tenants against leaves and models against partitions.

### Phase 3: Release

#### Step 1: Rehearsal and deploy

A PITR-fork walk rehearsal at production scale (engine-release Step 5b), marked
IRREVERSIBLE, with the freeze length from H6 and the topology conexus
confirmed.

#### Step 2: Tenant-removal runbook

#### Step 3: Drop `chunks_retired_225` after 14 days

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Tenant leaves (`chunks`, `taxonomy_centroids`) | In scope: doctor row | In scope: rows per (model, tenant) | In scope: runbook DETACH and DROP | In scope: doctor compares token tenants against leaves | Cluster backups |
| Model partitions | In scope: catalog query | In scope | Deferred: retiring a model is its own changeset | In scope: doctor compares `embedding_models` against partitions | Cluster backups |
| `chunks_retired_225` | N/A | N/A | In scope: Phase 3 Step 3 | N/A | Cluster backups during the window |

## Test Plan

- **Every search family under the tenant GUC.** Verify: the plan shows exactly one (model, tenant) leaf, pinned per family.
- **No tenant GUC, and tenant B's GUC.** Verify: zero rows, and no tenant-A row is ever returned.
- **A first token through each of the three paths (tenants/create, service-tokens/issue, data-tokens/mint), then a write.** Verify: the leaves exist and the write lands in them. Verify too that a second token for the same tenant is a no-op.
- **300 tenants created.** Verify: creation time, locks and search planning time are within the step-2 addendum's bounds.
- **Every write family: insert, upsert, delete, canonical and cross-model rename, quarantine, restore, GC, and `purge_trash` with deferred FKs.** Verify:
  - the four-column FKs and conflict targets hold;
  - cascades fire;
  - `SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED` works against the partitioned parent.
- **A chunk written with a model or vector dimension that disagrees with its collection.** Verify: refused.
- **A new model added by changeset.** Verify: the partition and a leaf per existing tenant exist, and a write lands.
- **Migration of a seeded two-tenant, two-model store, including hidden rows and a disputed-dimension row.** Verify:
  - exact reconciliation;
  - ANALYZE ran;
  - the views and policies are on the new table;
  - an injected failure leaves every table unchanged.
- **Recall and latency non-inferiority.** Verify: protocol step 3's rule.
- **A local install.** Verify: the preflight, the migration and search.

## Finalization Gate

### Contradiction Check

These have been checked against each other after the round-2 fix check (T2
`nexus_rdr/225-fix-check-c0feaa2b1`):
- Step 4 and the migration both specify a write freeze.
- Tenant creation is hooked on `issueToken`, which covers all three token paths.
- The step-2 addendum defines the tenant-scale bounds.
- The F7 finding text matches research-14.
- The fallback paragraph names what changes.

No contradiction was found among the findings, the design and the protocol.

### Assumption Verification

- **Verified:** research-14 (F7: 156 of 156 checks on the model-then-tenant layout).
- **Open, tested by step 2 as a prerequisite:**
  - research-15: write cost and the four-column FKs;
  - the step-2 addendum's tenant-creation and planning bounds.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| FK referencing a partitioned table, with a PK that includes the partition keys | PostgreSQL 17 | Spike (step 2) |
| Plan-time pruning on literal `embedding_model` and `tenant_id` under `force_custom_plan` | PostgreSQL 17 | Spike (step 2, research-14) |
| HNSW declared on a partitioned parent | pgvector 0.8.2 | Spike (step 2 prototype) |
| RLS is not inherited by partitions | PostgreSQL 17 | Spike (the step-2 prototype applied it per leaf) |

### Scope Verification

The MVV runs in protocol step 4 and Phase 3 Step 1 on a production fork,
through the real changeset and engine. It is in scope.

### Cross-Cutting Concerns

- **Versioning:** a schema-carrying engine tag. The client is unchanged.
- **Build tool compatibility:** jOOQ codegen and its record-count guard.
- **Licensing:** N/A.
- **Deployment model:**
  - cloud: conexus's PITR walk rehearsal, with the freeze and topology confirmed;
  - local: at boot, with the disk preflight.
- **IDE compatibility:** N/A.
- **Incremental adoption:** one cutover per install.
- **Secret/credential lifecycle:** N/A.
- **Memory management:** indexes are built per leaf after the copy, which bounds `maintenance_work_mem`.

### Proportionality

- The protocol is long because the owner asked for proof first.
- The design names every object the migration touches, through a generated and pinned inventory.

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
- 2026-10-05: Gate round 1 — BLOCKED (2 Critical, 7 Significant, 2 ship-blocker(s)); commit `71437ca05`; critique `nexus_rdr/225-gate-critique-2026-10-05-r1`.
- 2026-10-05: Fixes for gate round 1. Sam chose partitions of one logical `nexus.chunks` (by model, then tenant) over separate tables plus an identity table, which keeps RDR-191's manifest FK guarantee, and tenant leaves created at tenant creation. Inventory, migration (freeze, ANALYZE, failure, rollback, local preflight), fallback layout, write-path facts, contradictions and terms corrected.
- 2026-10-05: Fixes for the round-2 fix check (T2 `nexus_rdr/225-fix-check-c0feaa2b1`, FAIL). Tenant leaves now come from `issueToken`, which covers all three token paths. Step 4 now rehearses under write freeze (dated addendum). A step-2 addendum defines T1 to T3 for tenant scale. The Technical Design is rewritten to cover: the full chunks inventory (conflict targets, write sites, triggers, views, policies, grants, names), RLS per leaf, NOT NULL model columns, the dimension CHECK, new-model onboarding, taxonomy, the cross-model rename, a single transactional migration with the cloud topology as a prerequisite, and the precise fallback layout. Stale F7 text, overclaims and citations are corrected.
- 2026-10-05: Accepted by Sam with the gate overridden ("Accept now with residuals"). The counted defects of fix check 2 (T2 `nexus_rdr/225-fix-check-2296ed853`) are folded into the text as known fixes:
  - a `service_tokens` trigger, plus `default`-tenant leaves in the migration;
  - manifest-trigger and write-site redefinition only at step 7.8;
  - nullable-backfill-NOT NULL;
  - the outbound composite FK and `taxonomy_centroids` swap in step 7.6;
  - a scoped ON CONFLICT rule;
  - the third view;
  - leaf grants, the function contract, and holding-collection disposition for disputed rows (never-wedge).

  The remaining items are implementation beads.
