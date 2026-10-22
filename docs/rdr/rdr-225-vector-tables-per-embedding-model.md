---
title: "One Vector Table per Embedding Model, with Tenant Isolation"
id: RDR-225
type: Architecture
status: closed
closed_date: 2026-10-22
priority: high
author: Sam
reviewed-by: self
created: 2026-10-05
accepted_date: 2026-10-05
related_issues: [nexus-tu8wp, nexus-tu8wp.6]
related_rdrs: [RDR-152, RDR-155, RDR-156, RDR-164, RDR-169, RDR-191, RDR-192, RDR-194, RDR-204, RDR-226, RDR-227, RDR-230]
---

# RDR-225: One Vector Table per Embedding Model, with Tenant Isolation

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Drafted 2026-10-05 against develop `a798f07ea`. No product code has changed. The
research that decides the design is still running on forks of production; its
open items are marked **Assumed** below.

*As built (2026-10-22).* The paragraph above is the 2026-10-05 draft state,
kept as written. Product code has since changed (next paragraph). No
**Assumed** marker remains in the body (`grep -n Assumed` finds only the
paragraph above); the items it covered were resolved or dropped, as the dated
notes and the Revision History record.

*Reconciled 2026-10-22 (nexus-3wh8d.33).* The text above and most of the
sections below are the 2026-10-05 and 2026-10-06 design, kept as written.
The design was built and deployed to the cloud on 2026-10-07 (`vectors-030-1`,
engine-service-v0.1.150, nexus-3wh8d.20), and the retired tables were dropped
on 2026-10-21 (`vectors-036-1`, engine-service-v0.1.162, nexus-3wh8d.22).
Dated "as built" notes mark where the text below was corrected or where what
shipped differs from what was planned. The Revision History lists them.

**Provenance.** On 2026-10-05, investigating slow cloud searches, a literature
review found that every 1024-dimension vector in the engine sits in one HNSW
index, whichever model embedded it. Sam: "the mixing of embeddings is extremely
bad and that needs fixed." Sam also asked why chunks are not partitioned by
tenant, and stated that the cloud serves a handful of tenants.

**Decision, 2026-10-06 (Sam): the up-front evaluation experiment is dropped.**
Protocol steps 3 and 4 (§ Evaluation Protocol) are not run. The design is built
as chosen: partitions by embedding model, then by tenant. The reasons:
- Once the router ships (engine-service-v0.1.149), every collection is under its
  60,000-row threshold, so about 82% of logged production searches (452 of 552
  in the census) use exact search in both layouts and never touch an HNSW index.
- The protocol could not decide on production's real shape. Building its harness
  exposed cells that were UNKNOWN by construction, and each fix opened another.
- The split is adopted on principle (one graph per embedding space), and tenant
  isolation is a must; neither waited on the experiment.

Quality is checked instead on the production-fork rehearsal (Phase 3, Step 1):
real logged queries run before and after the migration, and their results and
latency are compared. If they do not hold, the deploy does not go ahead, and
rollback is a tag flip or a PITR restore. The shelved harness work is archived
outside the repo (`~/nexus-evidence/rdr225-shelved-2026-10-06/`). The protocol
text below is kept as history.

*As built (2026-10-22).* The before-and-after check on real logged queries was
not run; see Minimum Viable Validation for what was run instead.

*As built (2026-10-22): the router basis.* The 60,000-row threshold in the
first reason above was the step-1 census's harness parameter, not the router
that shipped (T2 `nexus/rdr225-step1-census-and-h5-inventory-2026-10-05`:
"Client develop dfa8ad852; router tu8wp6 [branch commit, never on a remote]; T=60000."). The router
merged with a provisional default of 10,000 (`git show 7f111743c`: "the default
10000 is provisional until the fork measurement sets it") and first shipped in
engine-service-v0.1.149 (`git tag --contains 7f111743c`). The cloud deploy ran
10,000 (`docs/runbooks/rdr-225-cloud-deploy.md:421-422`: "default 10,000 at this
deploy and 30,000 since nexus-nqsa7 (60,000 in engine-service-v0.1.155 only)").
The default was 60,000 for engine-service-v0.1.155 only, 30,000 from v0.1.156,
and is 20,000 now (`PgSession.java:781-787`; `CHANGELOG.md:282` and `:314`).
The 82% used 552 as its denominator, which is the census's count of calls
extracted; the census counted 503 searches ("552 calls extracted; 503
searches") and 452 plain searches, which is 452 of 503, 89.9%. The census also
records "The largest collection holds 55,635 stored rows (code__1-1), under
T.", so at 10,000, 20,000 and 30,000 that collection sits above the threshold;
"every collection is under its threshold" held at 60,000 only. The decision
stands as Sam made it. This note records that the basis it was stated on did
not match the deployed router.

*As built (2026-10-22): what the protocol did and did not show.* Sentences
below that say the protocol shows, proves or holds the split to
non-inferiority describe the plan, and each carries a pointer to this note.
Step 3 was dropped on 2026-10-06, so the split was never tested against
tenant-only partitioning. Step 4 was replaced by the PITR-fork walk (T2
`nexus_rdr/225-rehearsal-2026-10-07`). What stands in their place is the
post-deploy cloud gate: recall@10 0.90 on `code-filtered-001` at `ef_search`
200, then 1.00 on all 12 queries at the engine's `ef_search` floor of 600
(nexus-3wh8d.31, close reason: "recall@10 1.00 on all 12 incl code-filtered-001
(was 0.90 at ef 200)"). T2 `nexus_rdr/225-recall-per-leaf-analysis` bears on the
cause. On synthetic data a (model, tenant) leaf scored 0.966 against 0.919 for
the global table at ef 200 ("the per-leaf graph is not worse than the global
one. It is better"), and it infers that the production miss was "ordinary HNSW
tail variance on a near-tied neighbourhood" (inferred, not measured on
production). Its caveats: the corpus is synthetic and "harder than production";
the comparison is a leaf against the old global table, not against tenant-only
partitioning. The cause of the miss is not established, so this file does not
attribute it to the per-leaf graph alone.

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
- **Protocol labels** (A1t, A6, F7, H4 to H6, TS1 to TS3, E1): defined in § Evaluation Protocol. **ABBA** means running the two arms in the order A, B, B, A within a cycle, so neither always goes first.
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
non-inferiority (protocol step 3). *As built (2026-10-22): step 3 was dropped, so
the split's effect on it was never measured; see the Decision note.*

#### Gap 2: Tenants share one graph and one heap

Row-level security keeps tenants from reading each other's rows. It does
nothing for performance: another tenant's rows raise the share of every
filtered walk that is wasted, and its pages compete for the same shared buffers.
On the production fork, one pass over our main tenant's 98 collections read
3.3 to 4 GB "warm" on an 8 GB host and evicted itself (F3). This RDR closes the
shared graph and the shared heap. The buffer pool stays shared by every
partition; its size is a hosting decision (memory-16, F3), outside this RDR.

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
| RDR-152 | Origin (tenancy) | Chose a `tenant_id` column plus FORCE row-level security as the tenancy model: schemas partition **domains** (rdr-152:355), not tenants, and it addressed security only. Performance isolation was never addressed. The rationale holds for security; this RDR keeps RLS and adds physical isolation under it. |
| RDR-155 | Origin (pgvector store) | Moved T3 onto pgvector with native RLS. Did not consider per-model graphs. |
| RDR-156 | Origin (rejected partitioning) | Rejected a partitioned `chunks` table with dimension as the key. |
| RDR-191 | Origin (one table, one index per dim) | Unified the per-dimension tables into `nexus.chunks` with three typed columns. Its fact V1, "Partitioning is impossible", was verified for partitioning **by dimension**: an untyped `vector` parent cannot carry an HNSW index, and a typed child with a different type cannot be attached. A tenant or model key keeps the same typed `vector(1024)` column in every partition, so V1 does not apply. This RDR does not reopen dimension partitioning. |
| RDR-164 | Adjacent | Collection rename, the canonical branch the write path keeps. |
| RDR-169 | Adjacent | Reference-only retention: `chunk_text` is nullable for those rows, a constraint each leaf inherits. |
| RDR-194 | Adjacent | The topic-assignment FK into chunks, which becomes four-column. |
| RDR-204 | Adjacent | Owns `nexus.embedding_models`, which this RDR reuses as the model key. |
| RDR-192 | Adjacent (closed) | Liveness predicate `live(c)`. Hidden rows stay in the graph. The migration here copies them unchanged (step 3); dropping them physically is a separate decision. |
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
  standard-8 (2 vCPU, 8 GB RAM, about 2.4 GB shared_buffers), no replica (`is_ha=false`, `replicas=null`; conexus-eb, Crunchy API, 2026-10-05).
- `nexus.chunks`: PK `(tenant_id, collection, chash)`, three nullable typed
  columns `embedding_384/768/1024` with a CHECK that exactly one is set, FORCE
  RLS. Heap about 8.9 GB. HNSW `m=16, ef_construction=64`, 1024 index about
  3.1 GB. conexus-eb reported 9.7 GB used on a 25 GB volume before the
  resize, and 41.9 GB free on a 50 GB volume after it (2026-10-05). Those
  figures do not reconcile with the heap and index sizes above. conexus's
  post-resize size query settles which is right, and the disk preflight
  arithmetic below is not revised until it does.
  *As measured (2026-10-07 PITR-fork rehearsal, T2
  `nexus_rdr/225-rehearsal-2026-10-07`).* `pg_total_relation_size` of `chunks`
  plus `taxonomy_centroids`, indexes included: 8,891,383,808 bytes (8.89 GB).
  The three referencing tables: 658,915,328 bytes. The database: 9,457 MB
  (9.92 GB), close to conexus-eb's 9.7 GB. So 8.9 GB is the total for the two
  vector tables with their indexes, and the "heap 8.9 GB" plus "1024 index
  3.1 GB" above should not be added together. This note does not explain the
  difference between the two sets of figures any further.
- Search settings (2026-10-05): `hnsw.iterative_scan = relaxed_order`, `ef_search =
  max(200, k)`, `max_scan_tuples = 200000`, statement timeout 30 s, custom plans.
  *As built (2026-10-07, nexus-3wh8d.31):* the default `ef_search` floor is 600
  (`PgSession.DEFAULT_EF_SEARCH_FLOOR`, `PgSession.java:101`; the engine logs
  `event=hnsw_ef_search_floor` at boot, `Main.java:157`), shipped in
  engine-service-v0.1.151. The cloud served at 600 as of 2026-10-07 (nexus-3wh8d.31 close
  reason).
  *As built (2026-10-22).* A single-collection plain search whose collection
  has no per-collection index runs at `ef_search` 1000 regardless of this floor
  (`PgSession.java:97-99`: "The floor governs every statement except a
  single-collection plain search whose collection has no per-collection index:
  that one runs at {@link #EF_SEARCH_WIDEST} (1000) regardless of this floor
  (RDR-227)").
  The CHANGELOG dates that override to engine-service-v0.1.157
  (`CHANGELOG.md:282`: "walks the shared index at `hnsw.ef_search` 1000 (new in
  engine-service-v0.1.157"). The floor of 600 governs every other statement.
- A collection's model is recorded in `catalog_collections.embedding_model`
  (`CollectionRegistry`), not on the chunk row.
- The cloud serves a handful of tenants (Sam, 2026-10-05). The `nexus` tenant
  holds about 312k of the roughly 430k 1024-d rows.
- Local installs embed with bge-768 by default, a single model per
  dimension, so they are affected only when Voyage reaches a local engine.

## Research Findings

### Investigation

Fork measurements by conexus-0e on PITR forks of production, 2026-10-05,
standard-8 and memory-16. Raw JSON is in `~/nexus-evidence/rdr225-2026-10-05/` (`fork/` and `census/` under the top-level `SHA256SUMS`; `step2/` under its own).

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
- **F7 (Verified in part, research-14).** On `chunks` LIST-partitioned by
  `embedding_model` and then `tenant_id`, with literal model and tenant predicates
  under `force_custom_plan`, every chunk search family is pruned at plan time to
  one (model, tenant) leaf. The taxonomy families were prototyped on
  `taxonomy_centroids` partitioned by model only, so their tenant-level pruning
  is not yet shown; the Test Plan pins it.

#### Registered research findings (T2 `nexus_rdr/225-research-1` to `-17`)

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
  *Source: PgVectorRepository and PgSession at 7f111743c (the router as merged to develop).*
  *As built (2026-10-22).* The 10000 is the default at 7f111743c. It was 60000 in engine-service-v0.1.155, 30000 from v0.1.156, and is 20000 now (`PgSession.java:787`). The 60k of research-8, and the 10.1% quoted there and under "Rulings that shape it", is the census parameter T=60000, not a shipped default; see the Decision note.
- **✅ Verified** (source search), research-12. `search_telemetry` has no path, scope or model column.
  *Source: telemetry-001 lines 100 to 109.*
- **✅ Verified** (spike), research-13. `pg_stats` hides FORCE-RLS tables from non-superusers.
  *Source: the fork.*
- **✅ Verified in part** (spike), research-14. F7: on the model-then-tenant layout, all 156 of 156 checks pass, after `assign_from_chashes` was given the new `embedding_model` column the design requires. The 144 checks outside `taxonomy_ann_query` prune the chunk scan to exactly one (model, tenant) leaf. Thirty of the 156 plans touch `taxonomy_centroids` (12 `taxonomy_ann_query`, 12 `assign_from_chashes`, 6 `cross_preview`), which the prototype partitioned by model only, so tenant-level centroid pruning is not shown.
  *Source: ~/nexus-evidence/rdr225-2026-10-05/step2/f7_a6.json.*
- **✅ Verified in part** (spike), research-15. H4 write cost on the final layout (ratio new/old): single insert 1.01, update 0.85, delete 1.02, rename 0.95, all within the 1.25 rule. Bulk insert is 1.02 with CI [0.82, 1.28], over the rule's upper bound because of one disturbed pair; it was to be repeated on the step-3 fork as a prerequisite. Re-measured on 2026-10-21 on the as-built layout (nexus-3wh8d.32), it is 0.93, 95% CI [0.92, 0.94] over 24 pairs, under the rule (T2 `nexus_rdr/225-h4-bulk-insert-remeasure-2026-10-21`; raw data in `~/nexus-evidence/rdr-225-h4-bulk-insert-2026-10-21/`, 22 files, `SHA256SUMS` verifies); caveats: synthetic clustered vectors, one writer, `shared_buffers` 512 MB. The deferred FK behaves as today. 300 tenant creations take 27 ms each on average (p95 40 ms). Planning at 302 tenants takes 0.62 ms (0.41 ms at 2). TS1 and TS2 are thinner than the addendum's rules (see Step-2 results).
  *Source: T2 `nexus/rdr225-step2-local-feasibility-2026-10-05`; ~/nexus-evidence/rdr225-2026-10-05/step2/ (SHA256SUMS).*
- **⚠️ Documented** (docs only), research-16. The literature crossover points (Veda, Compass) were measured warm, and VADER finds query-filter correlation.
  *Source: knowledge__dt-papers.*
- **⚠️ Documented** (docs only), research-17. pg_vectorsearch (Timescale) adds PRISM, an IVF-style ANN index that indexes existing pgvector `vector` and `halfvec` columns in place. Its README claims HNSW-level recall faster and in a quarter of the memory at 100M vectors; that is its own benchmark, not measured here. Not adopted: it needs PostgreSQL 18 (the bundle and T3 are on PG17), it is alpha with no promised upgrade path, it would be another native extension in every platform's PG bundle (its README does not mention Windows) and on the managed provider, and it has no keyword or hybrid ranking. Memory already matters on small instances (F3), but this design's per-model, per-tenant leaves shrink each graph, and the recall losses in F2 and F4 are filter-driven, which an IVF index is not shown to fix. Revisit only if one leaf outgrows memory for its HNSW graph, after pgvector's own options (halfvec, quantization, IVFFlat).
  *Source: https://github.com/timescale/pg_vectorsearch README, read 2026-10-14.*

### Evaluation Protocol (pre-registered, revision 6, frozen, 2026-10-05)

*Steps 3 and 4 were dropped on 2026-10-06; see the Decision note at the top. Kept as history.*

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
- **Raw data:** `~/nexus-evidence/rdr225-2026-10-05/` (`fork/` and `census/` under the top-level `SHA256SUMS`; `step2/` under its own).

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
  *As built (2026-10-22): the 60000 was the setting for the step-3 harness, which did not run. The deployed default was 10,000, then 60,000 for one release, then 30,000, and is 20,000 now; see the Decision note.*
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
step 2's hard gate. Its thresholds are set by the author in this addendum and
are not among the thresholds Sam initialled. Sample sizes: TS1 over 300
creations; TS2 over every search family, each planned 20 times at each tenant
count.

| Check | Pass rule | Fail label |
| --- | --- | --- |
| **TS1, tenant creation.** Time `create_tenant_partitions` for one new tenant, with 2, 100 and 300 existing tenants. Record the locks it takes and, with an open reader holding a conflicting lock, its wait under `lock_timeout = 2s` | p95 creation time at most 2 s at 300 tenants, and a creation blocked behind a conflicting lock fails at the lock timeout (which bounds each acquisition, not the whole creation; the writers it holds up and the real bound are measured in Step-2 results) | DESIGN-TS1 |
| **TS2, planning.** Plan time of each search family with literal keys, at 2 and at 300 tenants | p95 at 300 tenants at most 2x the 2-tenant p95, and at most 25 ms. Amended 2026-10-12: the 2x rule fails a family only when the 300-tenant p95 is also more than 2 ms above the 2-tenant p95; the 25 ms cap is unchanged | DESIGN-TS2 |
| **TS3, deferred FK.** Under `SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED`, against the partitioned parent: (a) the `catalog-029-3` purge shape, deleting chunks while manifest rows exist and then removing those rows, commits; (b) a delete and re-insert of the chunk commits; (c) a delete that leaves a manifest row orphaned fails at commit | all three behave as stated | DESIGN-TS3 |

**Step-2 results (2026-10-05, research-14, research-15).** Each result is stated against the addendum's rule; where the evidence is thinner than the rule, that is said.
- **F7 pass:** 156 of 156 checks. 144 chunk-family checks prune to one (model, tenant) leaf. The 12 `taxonomy_ann_query` checks ran on model-only centroids, so pruning did not apply to them.
- **H5 pass.**
- **H4:** four paths pass. Bulk is over the upper bound and is repeated on the step-3 fork.
- **H4 bulk, re-measured (2026-10-21, nexus-3wh8d.32): pass.** Step 3 was dropped, so bulk insert was measured on the local substrate instead: the as-built layout, applied from the real changelog through vectors-036, against the pre-walk unpartitioned layout at vectors-029, 430,000 seed rows over 3 tenants and 3 models, the engine's 300-row upsert statement. New/old backend CPU 0.93, 95% CI [0.92, 0.94] over 24 interleaved pairs; with the manifest insert in the same transaction, 0.93 [0.92, 0.94] over 14 pairs. Dropping the pairs flagged for laptop load gives 0.92 [0.91, 0.92]. Wall-clock ratios agree but carry wide CIs from that load. The new layout writes about 8% more WAL per row. HNSW graph insertion dominates both layouts; RLS and partition routing are not measurable, and the smaller per-leaf graphs are why the new layout is cheaper (T2 `nexus_rdr/225-h4-bulk-insert-remeasure-2026-10-21`; raw data in `~/nexus-evidence/rdr-225-h4-bulk-insert-2026-10-21/`, 22 files, `SHA256SUMS` verifies).
- **TS1, not yet met as written.** One run of 300 creations, `chunks` only (two leaves per tenant), with a 3 s lock timeout and no 100-tenant point: p95 40 ms against a 2 s bound. The design's eight leaves per tenant, the 2 s timeout and the 100-tenant point are re-run in implementation (Test Plan).
- **TS1, measured in implementation (2026-10-06, nexus-3wh8d.7; `TenantPartitionCreationTs1MeasurementIntegrationTest`): met.** One new tenant across both parents (`chunks_new`, `taxonomy_centroids_new`) in one transaction, commit included, on a `nexus_svc` connection through the SECURITY DEFINER function, `lock_timeout` 2 s, as a non-superuser schema owner, on parents with the live column set and parent-level indexes (three HNSW, two GIN, one btree on chunks; three HNSW on centroids), the production RLS and grants, and three referencing tables with 4-column foreign keys. 310 creations per layout, fixed tenant names. With four models (eight leaves per tenant): about 2 tenants present, p50 14.5 ms, p95 19.5 ms (n = 2); about 100, p50 29.8 ms, p95 32.4 ms (n = 20); about 300, p50 56.3 ms, p95 69.3 ms (n = 20). Mean of the first 50 creations 17.6 ms, of the last 50 58.3 ms; least-squares slope 0.163 ms per tenant. A first run that also carried three disputed-dimension placeholder models (seven models, fourteen leaves per tenant) measured higher, but the placeholders were removed on 2026-10-06 and those figures describe a layout that no longer exists, so they are not restated. The four-model figures are far inside the 2 s bound at 300 tenants. The lock behaviour, the writers a pending creation holds up and the caller-side statement timeout are in Technical Design, Tenant creation, Locks and bounds. A drop of the roughly 2,500 leaves a finished run leaves behind, in one statement, ran out of shared memory at the default lock table (`max_locks_per_transaction` 64 times 100 slots); a migration that creates leaves for many tenants inside its one transaction holds every new relation's locks to the commit, so P1.3 sized that before the rehearsal. Measured (2026-10-06, nexus-3wh8d.8): the walk holds about 27 relation locks per leaf (1,899 locks for 70 leaves in the seven-model layout, which holds per leaf and so carries over). With four models and two parents a tenant holds 8 leaves, about 216 locks, so the default lock table (`max_locks_per_transaction` 64 times `max_connections` 100 = 6,400 slots) covers about 29 tenants. A production count above that needs a raised `max_locks_per_transaction`. *As built (2026-10-22): the cloud walk measured 979 locks for 32 leaves, about 30.6 per leaf (T2 `nexus_rdr/225-rehearsal-2026-10-07`), so the default lock table covers about 26 tenants, not 29; a local lock-capacity check is bead nexus-vxmcl.*
- **TS2, not yet met as written.** Two families at n=40: 0.62 ms against 0.41 ms. Every family at 20 plans is re-run in implementation.
- **TS2, measured in implementation (2026-10-07, nexus-3wh8d.15; `ReadPathLeafPruningIntegrationTest`): met as written.** Every search family at 384, 768 and 1024 (31 family and width pairs), four models, 2 against 300 tenants, 200 plans per set after 20 discarded warm-up plans, each figure the lowest p95 of 5 sets; the rule is the one above, p95 at 300 at most 2x the 2-tenant p95 and at most 25 ms, with no floor. Five consecutive runs passed. Per run, the worst ratio of 300-tenant to 2-tenant p95 was 1.26, 1.20, 1.25, 1.10 and 1.29, and the largest 300-tenant p95 was 1.47, 1.41, 1.51, 1.49 and 1.64 ms. Control: `plain_search_1024` with its tenant predicate removed reads 15.4 ms at 300 tenants against 0.9 ms at 2 and fails. The inlined families are timed as an EXPLAIN round trip (parse and plan, no execution) and the plpgsql ones (`text_gate_probe`, `assign_from_chashes`, `cross_preview`) as the call itself, so the figures include the client round trip.
- **TS2, rule amended (2026-10-12, Sam; nexus-ys8a4 item 6).** On develop `aa226556f` the Service CI Java job failed on one row, `assign_from_chashes` 1024: 1.232 ms at 300 tenants against 0.600 ms at 2 (2.05x). In the same table `taxonomy_ann_query` 1024 went from 0.680 to 1.300 ms (1.91x, passed) and `text_gate_probe` 768 went from 1.326 down to 0.669 ms. The same tree passed on PR 1602 and on a rerun. A family that plans in 0.6 ms moves by about 0.6 ms between the two phases on a shared box, in either direction, and that alone reaches 2x. The 2x rule now fails a family only when the 300-tenant p95 is also more than 2 ms above the 2-tenant p95. The control above (15.4 ms against 0.9 ms) still fails it. The 25 ms cap has no allowance.
- **TS3, in part.** Cases (b) and (c) pass; case (a), the purge shape, was not run and is in the Test Plan. Built 2026-10-06 (nexus-3wh8d.8, `P225MigrationWalkIntegrationTest`) against the real migrated tables: (a) `purge_trash` commits, (b) delete and re-insert commits, (c) a delete that orphans a manifest row fails with SQLSTATE 23503. A foreign key onto a partitioned table is cloned onto the referencing table once per partition, so the violation in (c) names the auto-generated clone (`catalog_document_chunks_..._fkeyN`), not `fk_catalog_chunks_chunk`; `SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED` still defers the clones. Error mapping that keys on the constraint name must key on the referencing table instead.

**Dated record of edits to frozen text (2026-10-05, after gate round 1).** These wordings were changed to describe the chosen layout, with no change to any rule, threshold or statistic:
- the critique-round count;
- the raw-data location;
- Equivalence;
- the Plan check's declared plans;
- the A6 router table (Execution path);
- the F7 observable, now a plan naming one leaf, because plan-time pruning emits no "Subplans Removed";
- the H4 comparator;
- the A6 arm definition;
- step 4's copy layout and rollback.

Step 4's freeze carries its own dated note.

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
  *As built (2026-10-22): the 60000 is the census and harness parameter, not a shipped default; see the Decision note.*

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

*As built (2026-10-22).* Steps 3 and 4 were dropped on 2026-10-06 (Decision note at the top). The RDR was accepted on 2026-10-05 (`accepted_date`) and built without them. What stands in for step 3's recall and latency check is the Phase 3 rehearsal's 12 searches and the cloud STEP-6 gate, which found a recall miss that the plan had no step to find (Minimum Viable Validation). What stands in for step 4 is the PITR-fork walk of the real changeset (T2 `nexus_rdr/225-rehearsal-2026-10-07`).

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
  protocol's non-inferiority test (step 3). *As built (2026-10-22): that test
  was dropped with step 3, so nothing replaced it; see the Decision note.*
- [x] F7: plan-time pruning on literal `embedding_model` and `tenant_id`
  leaves one leaf's index on the chunk search families — **Status**: Verified
  in part (research-14; tenant-level pruning of `taxonomy_centroids` is open
  and is a Test Plan item) — **Method**: Spike
- [x] The manifest and topic FKs work as four-column deferrable FKs to the
  partitioned parent at a write cost within 1.25x on four write paths
  (research-15). Bulk insert, re-measured on the as-built layout instead of the
  dropped step-3 fork, is 0.93, 95% CI [0.92, 0.94] (nexus-3wh8d.32). Still open: the
  orphaned-at FK and GC functions, which were not prototyped and are covered
  by the Test Plan — **Status**: Verified in part — **Method**: Spike
- [x] Tenant creation stays fast and planning stays flat at 300 tenants
  (research-15, TS1 and TS2). The evidence is thinner than the addendum's
  rules (see Step-2 results) — **Status**: Verified in part — **Method**: Spike

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

**Tenants.** A tenant exists from its first token row. An AFTER INSERT trigger on `service_tokens` creates the tenant's leaves (one per model, in `chunks` and `taxonomy_centroids`) if they do not exist. The trigger covers the three INSERT sites (TokenStore.java:242, 438, 585). The fourth write, `ensureBootstrapToken`'s UPDATE at :284, only rebinds rows to the constant `default` tenant, whose leaves the migration creates. The migration also creates leaves for the bootstrap tenant `default` unconditionally. Every tenant is isolated from its first write, and there is no shared DEFAULT partition.

**What is shown before building, and what is not claimed.**
- The evaluation protocol shows this layout works: pruning on every search family, bounded write cost, a safe migration, and recall and latency no worse than tenant isolation alone.
- It does not claim the per-model split raises recall. E1 (research-2) found no shared true nearest neighbour in a 400-vector sample. F2's recall loss is filter- and graph-driven, and the protocol holds the split only to non-inferiority.
- The split is adopted on principle: one graph per embedding space.

*As built (2026-10-22): the first bullet is the plan. Step 3 was dropped, so recall and latency were never shown to be no worse than tenant isolation alone; see the Decision note.*

### Technical Design

Proven by protocol steps 2 to 4 before production code. *As built (2026-10-22):
step 2 ran; step 3 was dropped and step 4 became the PITR-fork walk, so
"proven" holds for step 2 only; see the Decision note.* Working names are
settled in Phase 1. Where the code is quoted, the source is develop
`8bfa4d01e`, the commit the pinned inventory was generated against. Line
citations were refreshed against it on 2026-10-06 (`service/src/main` is
unchanged between it and origin/develop at `2060b96ba`).

**Model key.** `nexus.embedding_models` already exists (catalog-036:61-81, RDR-204), with columns `(embedding_model, dimension, provider)` and the tokens `voyage-code-3`, `voyage-context-3`, `bge-base-en-v15-768` and `minilm-l6-v2-384`. `catalog_collections.embedding_model` references it (hygiene-002:291). This RDR adds no registry.
- **Model column.** `nexus.chunks` gains `embedding_model text NOT NULL`. The engine writes it from the collection's `catalog_collections` row, which it must now pass explicitly: `requireHomogeneousModel` validates the model but returns nothing today.
- **Model FK.** A new UNIQUE `(tenant_id, name, embedding_model)` on `catalog_collections` backs a composite FK `(tenant_id, collection, embedding_model)` from `chunks`, ON DELETE RESTRICT, as `chunks_collection_fk` is today (fk-004). It replaces `chunks_collection_fk`. A chunk therefore cannot be filed under a model other than its collection's.
- **Dimension CHECK.** Each model partition carries a CHECK that its model's vector column is the only non-null one, for example `embedding_1024 IS NOT NULL AND embedding_768 IS NULL AND embedding_384 IS NULL` for the 1024-d models. A row whose vector dimension disagrees with its model cannot be stored. Legacy data shapes (a collection whose stored vectors disagree with its registered model, or hold several dimensions) are not supported: the migration walk copies each chunk under its collection's model, the CHECK refuses such a row, and the walk fails with PostgreSQL's own error and rolls back. Nothing is re-registered or renamed to make a row fit.
- **Columns.** Every leaf carries all three typed vector columns, with an HNSW index on each, two of them always empty. Step 2's tenant-scale measurement includes this cost.

**Keys.** PostgreSQL requires a partitioned table's primary key to include its partition keys, so the PK becomes `(tenant_id, collection, chash, embedding_model)`. `(tenant_id, collection, chash)` still identifies one chunk, because a collection has exactly one model (the composite FK above). Every table that references a chunk gains `embedding_model text NOT NULL`, written by the same code that writes its collection. NOT NULL is required: the FKs are MATCH SIMPLE, so a NULL would exempt the row from the check. Their FKs become four-column, with the same actions, deferrability and names.

**Everything that names `nexus.chunks` changes.** Phase 1 Step 1 generated an inventory of these objects with a lint over it; both were reverted (2026-10-06, `e01d1ea09`), so no pinned inventory guards the list. What guards a write site that was missed (2026-10-06): `PlpgsqlCheckGateTest` runs `plpgsql_check` over every function `vectors-030` defines, which fails on an arity mismatch and on a conflict target that matches no unique constraint, and the runtime write-family tests exercise each writer. `plpgsql_check` cannot see an INSERT that omits the NOT NULL `embedding_model` (an omitted column is not a static error). It also skips the statements in `gc_quarantine_orphans`, `gc_quarantine_orphans_bounded` and `reaper_quarantine_chunks` that read their own run-time temp tables, so those three are covered by their runtime tests only. Known at `8bfa4d01e`:

| Object | Kind | New form |
|---|---|---|
| `fk_catalog_chunks_chunk` (catalog-029) | FK into chunks | 4-column; ON UPDATE CASCADE, NO ACTION, DEFERRABLE INITIALLY IMMEDIATE. Same name: Java issues `SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED` (`CatalogRepository.deferManifestChunkFk`, :8298), and one live plpgsql site issues it unqualified: `purge_trash` in vectors-017-3 (:1064). The other plpgsql occurrences are not live: catalog-029:306 and catalog-033:99 are superseded bodies, and catalog-033:199 and vectors-017:1177 are rollbacks. Step 2 found that the bare name fails without the schema on `search_path`, so the live site is redefined schema-qualified (step 7.8) |
| `topic_assignments_chunk_fk` (taxonomy-012) | FK into chunks | 4-column; ON UPDATE CASCADE, ON DELETE CASCADE; same name |
| `chunk_orphaned_at_chunk_fk` (vectors-021) | FK into chunks | 4-column; ON UPDATE CASCADE, ON DELETE CASCADE; same name |
| `chunks_collection_fk` (fk-004) | FK out of chunks | replaced by the composite model FK |
| Every `ON CONFLICT (tenant_id, collection, chash)` target whose arbiter is `nexus.chunks`, and every `INSERT INTO nexus.chunks` column list: the Java upserts at PgVectorRepository.java:1158-1168 (column list :1158, conflict target :1168) and `CatalogRepository.upsertManifestChunkVectors` (the combined chunk-plus-owner write, insert at :5410), and the live plpgsql bodies: vectors-022 (`gc_quarantine_orphans` and `_bounded`), vectors-024 (`reaper_quarantine_chunks`), vectors-025 (`quarantine_restore_chunks`) and catalog-043 (`gc_restore_rereferenced` and `_bounded`), plus any others the script finds. The earlier bodies in catalog-033, catalog-042 and hygiene-008 are superseded by those and are not rewritten. `chunk_orphaned_at` keeps its own three-column arbiter, as do other tables whose PKs do not change. Writers of the referencing tables supply `embedding_model`: the manifest upsert (`CatalogRepository.insertManifestChunkRows`, :5051-5067), `assign_from_chashes_{384,768,1024}` (taxonomy-020), and the stamp triggers (next row) | write sites | Rewritten. The conflict target becomes the four-column PK and the insert supplies `embedding_model`. Redefined once, at migration step 7.8, against the swapped table |
| `stamp_chunks_on_manifest_delete` and `_update` (vectors-021-3) | trigger functions inserting into `chunk_orphaned_at` and joining `chunks` | Rewritten to carry `embedding_model` from the manifest row and to join on all four columns |
| `nexus.live_chunks`, `nexus.collection_vector_stats` (vectors-019:1118-1190), `nexus.diag_chash_conformance` (taxonomy-011-8) | views over `chunks` | Dropped before the swap and recreated over the new table in the same transaction. They bind by OID, so they would otherwise follow the retired table. `diag_chash_conformance` may be owned by a superuser (taxonomy-011), so its drop and recreate runs as that owner, or is a documented operator step if the migration role cannot |
| `chunks_gate_probe_owner_read` policy (vectors-029); the `tenant_isolation` policy; FORCE RLS | RLS | On the parent `chunks`, because queries through the parent use the parent's policies, and on every model partition and leaf, because PostgreSQL does not inherit RLS state or policies and a leaf can be queried directly. The step-2 prototype applied both. |
| `chunks_content_retention_consistent` (vectors-014), `chunks_chash_octet_check` (vectors-004:313), `idx_chunks_tenant_chash` (vectors-004:333), the HNSW, GIN and trigram indexes | constraints and indexes | Declared on the parent so every leaf inherits them. On the retired table, its indexes and index-backed constraints (PK, UNIQUE) are renamed with a `_retired_225` suffix before the swap, because those names are schema-wide. CHECK and FK constraint names are per table and need no rename |
| Grants on `nexus.chunks` (grants-nexus-svc.xml, grants-nexus-diag.xml) | grants | Re-issued on the new parent. These are `runAlways` changesets, so they re-run on every walk |
| `ChunksIsolationCheck.verifyAtStartup` (called at Main.java:141), the doctor RLS canary (`_RLS_TENANT_TABLES`, health.py:3419) | isolation checks | Extended to assert FORCE RLS and the policy on every model partition and leaf |
| `taxonomy_centroids` (taxonomy-007) and its upsert at TaxonomyCentroidRepository.java:119-124 | mixed-model table | Gains `embedding_model`, partitioned the same way (model, then tenant). The step-2 prototype proposed model-only partitioning because centroids are few. Tenant isolation is required, so the tenant level stays. The upsert's conflict target includes the model. A centroid whose dimension changes (nexus-2qryr) is deleted and re-inserted, not upserted, because an upsert cannot move a row across partitions |
| Java registries that name the tables: `DimTables` (`CHUNKS_TABLE_NAME` :45, `CENTROIDS_TABLE_NAME` :48, and the dimension-keyed `ChunkTable` and `CentroidTable` accessors every typed jOOQ write goes through), `CatalogRepository.COLLECTION_SCOPED_TABLES` (:8761-8767, which drives the rename and move UPDATEs and the emptiness checks), and the VACUUM allowlists `TenantScope.VACUUM_ALLOWED_TABLES` (:365) and `CatalogRepository.PURGE_VACUUM_TABLES` (:2719) | registries | Decided in P1.3 (2026-10-06, nexus-3wh8d.13). `DimTables`: the typed accessors still point at the two parents (jOOQ generates the parents only) and now carry `embedding_model`, which every typed INSERT supplies from the collection's `catalog_collections` row and every conflict target names; a write for a model with no partition is refused before any SQL. `COLLECTION_SCOPED_TABLES`: membership unchanged; it drives the rename and re-home UPDATEs, which stay inside the leaf when the models match, and the manifest UPDATE of a cross-model rename sets `embedding_model` with `collection`. `ChashRepository.renameCollection` and `rehomeCollection` answer a 409 with reason `collection_model_mismatch`, naming both models, when the move would file chunks under a collection of another model. Manifest writes to a collection with no registry row answer a 422 (`unregistered_collection`), and centroid upserts require a registry row. The VACUUM allowlists: see Day 2, the purge VACUUM |

**Tenant creation.**
- A tenant exists from the moment its first token row is written. Three methods insert token rows: `issueToken` (TokenStore.java:401-438, for `/v1/tenants/create`, `/v1/service-tokens/issue` and `/v1/data-tokens/mint`), `ensureBootstrapToken` (:242, the `default` tenant at every boot, after the walk) and `rotateTokens` (:585). `ensureBootstrapToken` also UPDATEs rows to `default` (:284).
- An AFTER INSERT trigger on `service_tokens` calls `nexus.create_tenant_partitions(parent, tenant)` for each of the two parents (`chunks`, `taxonomy_centroids`) in the inserting transaction, so every insert site is covered. The one signature, `(parent regclass, tenant text, force boolean DEFAULT true)`, creates the tenant's leaf under every model partition of `parent`. The migration calls it with the `_new` parents and `force = false` (steps 2 to 6 run NO FORCE because the walk's role has no BYPASSRLS; step 7.7 restores FORCE through `nexus.partition_sync_access`), and the trigger calls it with the live ones and the default. `force = false` is accepted only for a `*_new` parent and refused on any other, because `nexus_svc` can execute this SECURITY DEFINER function (2026-10-06, nexus-3wh8d.10).
- The function is idempotent: it looks a leaf up by its partition bound (`pg_inherits` plus the bound expression), not by name, and creates only those that do not exist. A second token for the same tenant costs one catalog check.
- For each model partition of `chunks` and of `taxonomy_centroids`, it creates the tenant's leaf, enables RLS (forces it when `force` is true), and copies the parent's own policies and grants onto it from `pg_policy` and `relacl`, so a policy or grant added to the parent reaches new leaves with no code change.
- It is SECURITY DEFINER with a fixed `search_path`, EXECUTE revoked from PUBLIC and granted to the engine role only. It quotes with `format('%I')` and `%L`. Leaf names combine the parent name, an 8-hex-character hash of the model token and a 16-hex-character hash of the tenant (for example `chunks_m<8hex>_t_<16hex>`). Both hashes are computed by the same SQL function in every caller (`create_tenant_partitions`, `create_model_partition` and the migration), so no column is needed for a short code, and a collision check against the existing leaf's bound refuses a second tenant hashing to the same name. Concurrent first tokens for one tenant are serialised by a transaction-scoped advisory lock on the tenant. New leaves receive the same grants as the parent, including MAINTAIN for the purge VACUUM.
- **Locks and bounds (measured 2026-10-06, nexus-3wh8d.7; `TenantPartitionFunctionsIntegrationTest`).** A creation takes, in this order: ACCESS EXCLUSIVE on a model partition (nothing is taken on the root parent), then ShareRowExclusive on `catalog_collections` and on each table that references the parent, then ACCESS EXCLUSIVE on the next model partition, in `relname` order; the advisory lock on the tenant comes first. The function sets `lock_timeout = 2s` as a function-level `SET`, which reverts when the function returns (`SET LOCAL` would leave 2 s on the rest of the token-insert transaction). **The timeout bounds each acquisition, not the creation.** With one blocker per relation released 1.4 s after the creation began waiting for it, a creation of two leaves under two model partitions succeeded after 7.1 s across five blocked acquisitions, and no single wait reached 2 s. So the worst case is the number of acquisitions times 2 s, plus the rest of the inserting transaction. For `chunks` it is one per model partition, one for the registry, one per referencing table and the advisory lock: four models and three referencing tables make 4 + 1 + 3 + 1 = 9. For `taxonomy_centroids`, which has no foreign keys, it is one per model partition and the advisory lock: 4 + 1 = 5. One token insert is therefore 14 acquisitions, 28 s, with four models. Uncontended, the whole creation takes tens of milliseconds (TS1 below). A creation that reaches a timeout fails the token insert (SQLSTATE 55P03, after 2.0 s on the acquisition it was stuck on), and the engine returns a retryable 503. **A pending creation does hold up other writers, across tenants.** A creation parked at an acquisition holds the earlier ones and queues the writers behind the one it waits for. Measured with the creation parked at each step in turn, a writer waited 1.99 to 2.0 s (the creation's own timeout) when it needed a lock the creation held or queued for: the model partition it was on, the registry (`catalog_collections`) once the creation held or waited for ShareRowExclusive there, and each referencing table likewise. Writers to a model partition the creation had not yet reached, and to referencing tables it had not yet reached, waited 5 to 9 ms. The registry and the referencing tables are shared by every tenant, so the hold-up is not limited to the new tenant. **No `statement_timeout` is set inside the function, because it would not work:** a function-level `SET statement_timeout = '300ms'` on a function that sleeps 1.2 s returned after 1.2 s, since PostgreSQL arms that timer when the statement starts. One bound on the whole creation is a caller-side `statement_timeout` set before the token INSERT: with 3 s set, the same staggered creation ended with SQLSTATE 57014 at 3.0 s. A trigger runs inside the inserting statement, so that timer covers it. The value is Phase 2 Step 1's to choose, since the engine owns the token-insert transaction. **Token-insert bound (chosen and measured 2026-10-06, nexus-3wh8d.13; `TenantTokenPartitionsIntegrationTest`).** `TokenStore` runs each token INSERT (`issueToken`, the `ensureBootstrapToken` insert, and the whole `rotateTokens` transaction) in its own transaction with `SET LOCAL statement_timeout = 1000 ms`, and gives up and retries: 3 attempts, 200 ms plus up to 200 ms of jitter between them, then `TenantCreationBusyException`, which every handler's typed-error ladder maps to a 503 with `Retry-After` and reason `tenant_creation_busy`. Nothing is issued when it fails (each attempt rolls back). The numbers: a healthy creation through `issueToken` on the `nexus_svc` role, four models (eight leaves per tenant), 40 new tenants on top of 47, took p50 24 ms, p95 36 ms, max 43 ms, so 1 s is more than 20 times the slowest healthy creation measured here and about 14 times the slowest p95 TS1 measured with four models (69.3 ms, about 300 tenants). With one open reader on the first model partition, a creation ended as a 503 after 3.6 to 3.8 s (three 1 s attempts and two pauses), and a writer to that model partition for another tenant, arriving 300 ms in, waited 735 to 737 ms: it was let through when the first attempt gave up, where without the bound it would have waited out every acquisition's 2 s lock timeout, up to 28 s in all. The pause between attempts is what lets writers queued behind the creation's pending ACCESS EXCLUSIVE request through; a single longer attempt would hold them for its whole length. The client retries a 503 on its own schedule, so no further engine-side retry was added. The bound is a `TokenStore.TenantCreationBound` value, so a deployment with many more models can raise it. Tenant creation is reachable by any `mint`-scoped credential (DataTokenHandler.java:150-203), and each new tenant creates eight leaves, so `mint` is an operator credential, and the doctor reports the leaf count.
- Removing a tenant: first delete the tenant's manifest, topic-assignment and orphaned-at rows, which reference its leaves; then DETACH and DROP its leaves; then delete its tokens (runbook, Phase 3 Step 2).

**New model.** Adding a model is a changeset that inserts the `embedding_models` row and calls `nexus.create_model_partition(parent, model, force DEFAULT true)` once per parent (`chunks`, `taxonomy_centroids`). That function creates the model partition with its dimension CHECK, then a leaf for `default`, every tenant that has a token row and every tenant that already has a leaf under a sibling model partition of the same parent (the walk's tenant set: a tenant with data and no token row got its leaves from the walk; 2026-10-06, nexus-3wh8d.10). It is idempotent by partition bound, and it runs as the migrating role, not as `nexus_svc`. A write for a model with no partition is refused by the engine before any SQL, naming the model (Phase 2 Step 1, Test Plan). A model with a new dimension needs a new typed column and is out of scope (RDR-191).

**Read path.**
- The search families keep their per-dimension function bodies. vectors-019 wrote them once per dimension; they are not generated per model.
- Each body gains `embedding_model = $model AND tenant_id = $tenant` predicates, so that under `force_custom_plan` the planner prunes to one leaf at plan time. RLS is kept as well.
- The engine passes the model it already resolves through `CollectionRegistry`.
- The router's probe (`probeSelectedRows`, on branch `feature/nexus-tu8wp.6-cardinality-router`, which merges first; see Prerequisites) adds the same predicates.
- `hybrid_search_<dim>` has no caller and is left as it is.

**Write path, rename, quarantine.**
- Inserts land in their leaf through the partition keys. Every write site in the inventory above supplies `embedding_model` and uses the four-column conflict target.
- **Rename** (canonical branch): insert the new registry row, then UPDATE the children's `collection`. Model and tenant are unchanged, so the UPDATE stays within the leaf, and the manifest follows by ON UPDATE CASCADE.
- **Rename** (cross-model COPY branch, `CatalogRepository.renameCollectionTxn`, :9102-9104): the explicit manifest UPDATE also sets `embedding_model` to the target collection's model.
- **Rename** (`ChashRepository.renameCollection`, :259-279, reached through `/v1/chash/*`): a second, independent implementation of the same re-home. It deletes colliding chunk rows, UPDATEs `chunks.collection` and UPDATEs the manifest's `collection`. Under the composite model FK, its UPDATE of `collection` succeeds only when the new collection has the same model. Decided in P1.3 (2026-10-06): after the colliding rows are dropped, a cross-model rename that would still leave rows at the source stops with a 409 `collection_model_mismatch` naming both models, where it used to re-file them under the target. This is a user-visible change in collection finalisation: a cross-model migration whose target holds only some of the source's chashes used to finish by re-filing the rest under the target, and now stops. The RDR-162 cross-model cascade is unaffected, because its target already holds every chash and nothing is left to move. `POST /v1/catalog/collections/rehome` refuses the same move with the same reason (2026-10-06, nexus-3wh8d.10). Its UPDATE stays within the leaf when the model matches.
- **Quarantine**: `reaper_quarantine_chunks` DELETE … RETURNING into INSERT (vectors-024). It registers the quarantine sibling with the origin's model, so the row stays in its model partition. Its INSERT is among the rewritten write sites.
- **Cross-model migration** registers a new collection under the target model. There is no in-place re-embed, and none is added.

**Migration** (one engine release, a schema-carrying tag):
- Steps 2 to 7 (the SQL steps) run in **one transactional changeset**. Steps 1 and 8, the freeze and unfreeze, are operational. Under the stop-start topology that T2 22410 records, the engine is not serving during the walk, so the freeze is inherent there. A failure anywhere rolls back everything, so the old layout and the live referencing tables are untouched, and a retry starts clean.
- Writes are frozen for the walk.
  - **Locally:** the engine is not yet serving (Main.java:114-132: the walk runs before the HTTP server binds).
  - **In the cloud:** conexus-eb confirmed on 2026-10-06 (T2 `nexus_rdr/225-cloud-topology`, which answers the open ask in T2 22408 §2(d)) that the deploy is stop-start. The engine-redeploy SSM document runs `docker stop -t 30 conexus-engine`, `docker rm -f`, then `docker run` of the new tag, with one engine replica and no blue-green. The new engine runs Liquibase before its HTTP server binds (Main.java:114-125), so it serves nothing during the walk and the freeze is inherent. The control plane stays up and writes only its own conexus database, which the walk does not touch. No engine-side freeze mechanism is needed while stop-start holds. If a walk ever had to run with the engine up, the freeze already used for cutovers applies: stop `conexus-engine`, `conexus-engine-tls` and `conexus-controlplane` over SSM, and confirm zero `nexus_svc`, `conexus_svc` and `conexus_cp` backends in `pg_stat_activity` before starting.

The steps:
1. Freeze writes.
- The walk's role has no BYPASSRLS (vectors-004). Steps 2 to 6 therefore run with `NO FORCE ROW LEVEL SECURITY` on the tables they read and write, as earlier walks did (hygiene-002:155-158, vectors-004:225-227). FORCE is restored on every table, parent and leaf, in step 7.7, before the changeset commits.
2. Create the partitioned `chunks_new` with its PK and constraints but without secondary indexes, its model partitions (each with its dimension CHECK), and the tenant leaves for `default`, every tenant with a token row, and every `tenant_id` present in `chunks` or `taxonomy_centroids` (one query). The same goes for `taxonomy_centroids_new`, so every tenant seen anywhere has a partition to copy into. `create_tenant_partitions` takes the target parent as a parameter, so it serves the migration (`_new` tables) and normal operation alike.
   *As built (2026-10-22, nexus-3wh8d.7).* This step also creates the UNIQUE `(tenant_id, name, embedding_model)` on `catalog_collections` that the composite model FK of step 7.6 needs. It is in the changeset's PART A, the "step-2 objects", ahead of the walk proper: `ADD CONSTRAINT catalog_collections_tenant_name_model_uq UNIQUE (tenant_id, name, embedding_model)` at `vectors-030-model-tenant-partition-functions.xml:127`, guarded by an existence check so a re-apply after a partial rollback does not fail. The earlier step list did not name it.
3. Copy per (model, tenant), with `embedding_model` taken from `catalog_collections`. Rows hidden by RDR-192's liveness rule (chunks that no live manifest row claims) are copied unchanged. A row whose vector column disagrees with its model fails the model partition's dimension CHECK, and the whole walk rolls back with PostgreSQL's own error; there is no pre-scan and no special message. Centroids are copied into `taxonomy_centroids_new` only when their collection has a registry row, with the model taken from that row. The others are derived data that taxonomy rebuilds; they are not copied and not logged, and they stay in `taxonomy_centroids_retired_225`.
4. Add `embedding_model` to the referencing tables as nullable, backfill it, then SET NOT NULL. The manifest triggers keep their old text until step 7.8. Old `chunks` has no `embedding_model`, so their three-column joins still work over the backfill.
5. Create the parent's indexes, which builds them on every leaf after the copy. Then ANALYZE every leaf and parent. Autovacuum never analyzes a partitioned parent, and a bulk-copied table without statistics turns the planner off HNSW (vectors-004 Step 5b, BUG-0148).
6. Reconcile row counts exactly per (model, tenant) for `chunks` and for `taxonomy_centroids` (the centroids that have a registry row). A mismatch fails the changeset.
7. Swap:
   1. Drop the three inbound FKs and the three views.
   2. Rename the old table's indexes and index-backed constraints (PK, UNIQUE) with the `_retired_225` suffix.
   3. Drop the old table's outbound `chunks_collection_fk`, so it cannot block registry deletes.
   4. Rename `chunks` to `chunks_retired_225`, and `chunks_new` to `chunks`.
   5. Rename the new indexes to the old names.
   6. Add the four-column inbound FKs with their old names (NOT VALID, then VALIDATE; their referencing tables are not partitioned). Add the outbound composite model FK on the partitioned `chunks` validated directly, since PostgreSQL does not accept NOT VALID for an FK whose referencing table is partitioned. Rename and swap `taxonomy_centroids` the same way.
   7. Recreate the views from their saved definitions, options and grants (the schema owner may drop a view another role owns, so a superuser-owned `diag_chash_conformance` comes back owned by the migrating role, the shape `taxonomy-011-8` gives it). The parents' policies and grants were copied from the old tables at step 2; `nexus.partition_sync_access` re-mirrors every model partition and leaf from them with FORCE, FORCE is restored on every table the walk toggled (the retired tables keep it), and the `service_tokens` AFTER INSERT trigger is attached. This is the only place the trigger is created.
   8. Redefine, once and only here, the manifest triggers and every rewritten write-site function, against the swapped table.
8. Unfreeze writes.

**Failure and rollback.**
- A failed walk exits the engine at boot (`Main.java:126-131`, `System.exit(1)`), with the old layout intact because the changeset is one transaction.
  - **In the cloud:** under stop-start nothing serves during the walk. The previous engine is restored by flipping the tag back, which is conexus's default rollback for an additive walk (T2 `nexus_rdr/225-cloud-topology`).
  - **Locally:** the install is down until the previous engine version is reinstalled.
- After the changeset commits, the change is **IRREVERSIBLE** in place.
  - **Cloud rollback** is a restore from PITR (point-in-time recovery) to before the walk. Writes made since the walk are lost. Per conexus-eb, today that is Crunchy Bridge continuous PITR: the operator records the UTC timestamp immediately before the flip and restores to it (a restore or fork to that time, then a repoint) only with Sam's go. The conexus session that runs the deploy owns the restore. A tag flip is the default only for an additive walk, and this walk rewrites the chunks table, so it falls on the PITR side. After conexus RDR-007's database move (self-managed Postgres, in progress), `pgBackRest restore --type=time` plays the same role.
  - **Downtime budget (a conexus proposal, pending Sam's confirmation):** up to about 15 minutes of engine downtime is acceptable in a normal deploy window when the fork rehearsal measured it. A longer walk becomes a scheduled freeze with a tenant notice and Sam's explicit go. No contractual SLA exists today. Until Sam confirms, this is a proposal and not a requirement of this RDR.
  - **Cloud disk:** conexus-eb reports 41.9 GB free on the 50 GB production volume (2026-10-05), which covers the roughly 15 GB copy estimate. Every schema-carrying tag is rehearsed first on a PITR fork, which measures the walk and the deltas.
  - **Local rollback** does not exist. `chunks_retired_225` is kept for 14 days only as a data-recovery source, and a later changeset drops it.
- **The Liquibase rollback of `vectors-030-1` is not a recovery path (2026-10-06).** It exists to keep the rollback test chain executable (`SchemaRollbackRoundTripIntegrationTest` rolls older changesets back through a partitioned `chunks`). It restores table shape only, leaves the step 7.8 function bodies in their post-walk form, and discards every row written since the walk. The engine never calls it. The recovery for a committed walk is PITR.
- **Local disk preflight.** Before the walk, the engine checks that free disk is at least 2.2x the chunks table plus its indexes: 1x for the copy, 0.2x headroom, and 1x for the WAL the copy generates (estimated as 1x the table). This is inferred, not measured, and the retired table already occupies its own space. If it is not, the engine refuses to start, naming the shortfall. Step 4 of the protocol measures the real peak.
  *As built (2026-10-22).* Step 4 was dropped, and the factor in the engine is still the inferred 2.2 (`LocalDiskPreflight.java:35-40` and `:64`, whose javadoc still says "inferred, not measured"). The Phase 3 rehearsal measured a peak instead (T2 `nexus_rdr/225-rehearsal-2026-10-07`): the database grew 7.43 GB (9.92 to 17.34 GB, kept for the retired tables) and the walk wrote up to 6.39 GB of WAL, a 13.8 GB worst case. That is 1.55 times the 8.89 GB of the two vector tables, under the engine's 2.2 times (the growth also includes the referencing-table backfill, which the engine's factor does not count). The measurement is of the cloud estate on a fork with WAL archiving on; no local install was measured (see Test Plan item 13).
- A local install has the `default` tenant (and any it creates), so it holds eight leaves per tenant (one per model partition of `chunks` and of `taxonomy_centroids`, four models each), mostly empty. It gains nothing in isolation and still migrates, so that every install runs one schema.

*Moot since 2026-10-06: step 3 was dropped and the per-model split is built (Decision note at the top). Kept as history.*

**If step 3 is not PASS** (the per-model split is deferred), these differ from the design above:
- `nexus.chunks` and `taxonomy_centroids` are LIST-partitioned by `tenant_id` only.
- The PK keeps its three columns, which already include the partition key `tenant_id`. The referencing tables, their FKs, the conflict targets and the manifest triggers therefore keep their current form. No `embedding_model` column, composite model FK, dimension CHECK or `create_model_partition` is added.
- The read path adds only the `tenant_id` predicate.
- `create_tenant_partitions` creates one leaf per tenant in each table.
- The migration has no step 4. Steps 2, 3 and 6 work per tenant. Step 7 swaps tables, views, policies and grants, with the three-column FKs re-added.
- Phase 2 Step 1 keeps only the token-path handling, the no-leaf and lock-timeout error mappings, and the jOOQ record-count guard. Step 7.3 still drops the old `chunks_collection_fk`, and step 7.6 re-adds it on the new table. The Day 2 "Model partitions" row and the model-mismatch test do not apply.
- Everything else is as above: views, policies, grants, isolation checks, ANALYZE, failure handling, the Day 2 tenant rows, and step 4 of the protocol rehearsing whichever layout step 3 selects.

**Router.** The router stays (nexus-tu8wp.6). Its threshold is set from the protocol's measurements, outside this RDR. *As built (2026-10-22): the protocol never set it; the default moved by later measurement (nexus-nqsa7, RDR-227) and is 20,000 today (see the Decision note).*

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
  (protocol step 3). *As built (2026-10-22): step 3 was not run, so the split was
  never held to it; see the Decision note.*
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
  preflight. *As built (2026-10-22): step 4 was dropped and became the PITR-fork
  walk rehearsal (T2 `nexus_rdr/225-rehearsal-2026-10-07`).*
- Accepted risk (Sam, 2026-10-06): voyage-code-3 and voyage-context-3 are both 1024-dimension, so the dimension CHECK cannot tell them apart. A chunk embedded by the other Voyage model inside a collection is filed under its collection's registered model without error. The walk does not detect this, and nothing is built to detect it.

### Failure Modes

- **A write for a tenant with no leaf.** The `service_tokens` trigger and the migration create every tenant's leaves, so this needs a dropped leaf or a disabled trigger.
  - PostgreSQL raises SQLSTATE 23514, "no partition of relation found for row". `HttpUtil.sendTypedDbError` would map that to 409 as a caller error. The engine maps this specific error to a 500 naming the tenant, because it is an engine invariant, not a caller mistake. The dimension CHECK also raises 23514, so the mapping keys on the message and constraint name, not the code alone.
  - *Recovery:* call `create_tenant_partitions`. The doctor row lists tenants with tokens against leaves.
- **A chunk filed under the wrong model, or with the wrong vector dimension.** The composite model FK and the dimension CHECK refuse it at write time. For the same reason, an upsert of `catalog_collections` that changes a collection's model is refused. As built (2026-10-06, nexus-3wh8d.13): `upsertCollection` has refused any model change on an existing row since RDR-204, with a 422 (`EmbeddingProfileConflictException`) whether or not chunks exist, and the ETL imports never overwrite a row. The one writer that can swap a model is the canonical rename reviving a tombstone, which `blockingTable` allows only onto a target holding no data; the composite FK then refuses it with 23503 if a chunk is somehow referenced. The registry cache entry of the revived name is evicted before it is re-read. Re-modelling a collection is a cross-model migration.
- **A tenant creation blocked by locks.** It fails at `lock_timeout` on the acquisition it is stuck on, and the token request returns a retryable 503. The timeout bounds each acquisition, so the whole creation can take up to the number of acquisitions times 2 s (Tenant creation, Locks and bounds).
- **A pruning regression.** A function edit can stop plan-time pruning, so that a search touches every leaf.
  - RLS still filters, so it shows only as cost.
  - *Detection:* EXPLAIN-pinned tests per family.
- **A migration failure.** The single changeset rolls back and the engine exits at boot, with the data unchanged.
  - *Diagnosis:* the reconciliation and CHECK reports in the walk log.
- **A bad cutover after commit.**
  - Cloud: PITR restore. Writes since the walk are lost, and the runbook says so before anyone runs it.
  - Local: no rollback. Data recovery is from `chunks_retired_225` for 14 days.
- **Missing statistics after the copy.** The planner leaves HNSW. Migration step 5 analyzes every leaf and parent. The migration test asserts statistics on every leaf and on both parents (2026-10-06). A separate test shows a plan with a literal tenant and model pruning to one leaf and using that leaf's HNSW index; it runs on a leaf filled and analysed after the walk, because the leaves the walk fills hold too few rows for the planner to prefer HNSW whatever their statistics.
- **A write site missed in the rewrite.** Its `ON CONFLICT (tenant_id, collection, chash)` fails with "no unique or exclusion constraint matching". The guard is `PlpgsqlCheckGateTest` over the functions `vectors-030` defines (it catches arity and conflict-target errors) and the runtime write-family tests; the Test Plan runs every write family. `plpgsql_check` cannot see an omitted NOT NULL column, so a writer that never lists `embedding_model` is caught only by the runtime tests (2026-10-06).

## Implementation Plan

### Prerequisites

- [x] The cardinality router (`feature/nexus-tu8wp.6-cardinality-router`) is merged to develop (7f111743c, efd0d709d).
- [x] Protocol step 2 PASS, apart from what is still open: F7, H5 and TS1-TS3 as recorded in Step-2 results, and H4 on four paths. *Ticked 2026-10-22.* TS1 was measured in implementation (nexus-3wh8d.7: p95 69 ms at 300 tenants, four models). F7 is pinned per search family and TS2 re-measured strictly (nexus-3wh8d.15: one-leaf pins per family, TS2 at 2x and 25 ms with no floor). H5's inventory was reverted; `PlpgsqlCheckGateTest` and the write-family tests guard the write sites instead (Technical Design, "Everything that names `nexus.chunks` changes"). The four-path H4 result is unchanged. Test validation: nexus-3wh8d.19, verdict PASS with gaps (T2 `nexus_rdr/225-test-validation-p2`).
- [x] ~~H4 bulk insert repeated on the step-3 fork.~~ Dropped with step 3 (2026-10-06). Re-measured on the local substrate instead (2026-10-21, nexus-3wh8d.32): new/old backend CPU 0.93, 95% CI [0.92, 0.94] over 24 pairs, under the 1.25 rule; see Step-2 results.
- [x] Taxonomy tenant-level pruning, TS1 to TS3 as written, and the orphaned-at FK and GC write families pass on the implementation substrate (Test Plan). *Ticked 2026-10-22.* Taxonomy and every other family are pinned to one (model, tenant) leaf (nexus-3wh8d.15); the orphaned-at FK has its own test (`98fe4501b`, nexus-3wh8d.19) and the GC, quarantine and restore families are among the roughly 40 mutation controls that validation falsified (T2 `nexus_rdr/225-test-validation-p2`). TS3 case (a), the purge shape, is built in `P225MigrationWalkIntegrationTest` (nexus-3wh8d.8, Step-2 results). Gaps that validation left open, each now a bead (all open, P3, filed 2026-10-22):
  - WP6: the 55P03 lock-timeout to 503 mapping is pinned by a unit classifier only (nexus-3wh8d.34);
  - M4: the router-probe latency test cannot fail (the rehearsal measured the probe instead) (nexus-3wh8d.35);
  - DP3: a refactor could turn the local disk preflight off with no Java test failing (nexus-3wh8d.36);
  - TS1: the tenant-creation measurement runs only in the Java integration group (nexus-3wh8d.37).

  The validation's fourth gap, the redundant tenant predicates in `assign_from_chashes`, is an equivalent pair that fails when removed together, and has no bead (T2 `nexus_rdr/225-test-validation-p2`).
- ~~Protocol step 3 outcome recorded.~~ Dropped (2026-10-06): the model level is kept; the tenant-only layout is not used.
- ~~Protocol step 4 (H6, under write freeze) PASS.~~ Dropped (2026-10-06): the Phase 3 rehearsal of the real changeset covers it.
- [x] conexus confirmed the cloud deploy topology and the freeze mechanism on 2026-10-06 (T2 `nexus_rdr/225-cloud-topology`): stop-start, so the freeze is inherent and no engine-side mechanism is needed while that holds.
- [x] Sam confirms the downtime budget (2026-10-06): Sam is the only tenant, so no tenant notice applies (*as built 2026-10-22: the only customer; the cloud also holds test tenants, four in the 2026-10-07 rehearsal, T2 `nexus_rdr/225-rehearsal-2026-10-07`*); about 15 minutes rehearsed is the default, and a longer walk needs only Sam's go.

### Minimum Viable Validation

The real changeset migrates a fork of production. On it (revised 2026-10-06):
- The engine serves a fixed set of real logged queries through its own search functions, before and after the migration.
- Results and latency hold against the before run; a material loss stops the deploy.
- Each plan touches one (model, tenant) leaf.
- Row reconciliation per (model, tenant) is exact.

*As built (2026-10-22): the MVV was reduced, and the regression it was meant to catch was found after the deploy.* What ran, item by item:
- **Before-and-after run of real logged queries: not run.** The rehearsal record states "FROZEN_QUERY_SET not defined by nexus; not run" (T2 `nexus_rdr/225-rehearsal-2026-10-07`, § 9.1 values). The runbook replaced the set and the rule with a 12-search smoke test run after the walk only: 3 queries over {voyage-code-3, voyage-context-3} and {nexus, gate-xr789}, all HTTP 200 with 10 results, client latency at most 2x the fork's. That decision is in `docs/runbooks/rdr-225-cloud-deploy.md` § 9.1, row FROZEN_QUERY_SET. On the fork the 12 searches ran before and after a VACUUM, not before and after the walk, so no pre-walk result exists to compare against. The runbook's § 7.3 step 3 now records this and keeps the original before-and-after design as labelled history.
- **"A material loss stops the deploy": not tested by any comparison.** The stop rule had nothing to compare with. The smoke test checks that searches answer and return 10 rows, not that they return the same rows.
- **The recall regression surfaced after the 2026-10-07 deploy.** conexus's STEP-6 gate on engine-service-v0.1.150 failed `recall_ac3` on `code-filtered-001` alone: recall@10 0.90 against 1.0 locally, at `ef_search` 200 (nexus-3wh8d.31). Each (model, tenant) leaf has its own graph, and one true neighbour (exact rank 9) was missed. The cause is not established: the per-leaf analysis scored a leaf above the global table on synthetic data and infers tail variance (Decision note, "what the protocol did and did not show"). A sweep on the production leaves restored 12/12 at ef 400 with latency flat up to 1000 (CHANGELOG, 7.74.0 entry for nexus-3wh8d.31). The fix is the engine's default `ef_search` floor of 600 (`7aaf2aa7b`), shipped in engine-service-v0.1.151 and flipped in the cloud at 21:59:49Z, after which STEP-6 showed recall@10 1.00 on all 12 queries.
- **One leaf per plan:** pinned per search family in engine tests (nexus-3wh8d.15). Whether the 2026-10-07 fork run checked it with EXPLAIN is inferred, not read: the rehearsal record shows an index-only scan on one leaf's primary key for the router probe, and says nothing about the other families.
- **Exact reconciliation per (model, tenant): done**, on the fork (chunks and centroids, T2 `nexus_rdr/225-rehearsal-2026-10-07`) and at the cloud walk ("counts exact", walk committed 16:15:55Z in 7 min 53 s; nexus-3wh8d.20 close reason).

### Phase 1: Schema

#### Step 1: Pinned inventory

Run H5's script to generate and pin every object naming `nexus.chunks`,
`taxonomy_centroids`, `catalog_document_chunks`, `topic_assignments` or
`chunk_orphaned_at`: FKs, triggers, views, policies, grants, write sites and
conflict targets. The last three are in scope because their writers must now
supply `embedding_model`. Each item gets its new form, as in the Technical Design table.

*2026-10-06: a generated inventory with a lint guard landed and was reverted
(e01d1ea09); the guard taxed every change near these tables. Its findings are
recorded on the Phase 1 and Phase 2 beads (nexus-3wh8d.8, .12, .13).*

#### Step 2: Changesets for the new objects

- The UNIQUE on `catalog_collections`.
- `create_tenant_partitions` and `create_model_partition`.
- Not in this list: the read functions' model and tenant predicates are `vectors-031-1` and `-2`, separate changesets after the walk (Phase 2 Step 2). None of the items above is a separate earlier changeset. They all belong to the single migration changeset: the `service_tokens` trigger is attached in step 7.7, the functions are created in step 2, and the write-site and manifest-trigger redefinitions are made in step 7.8. A rolled-back walk therefore leaves nothing attached to the old layout.
- The same treatment for `taxonomy_centroids`.

#### Step 3: The migration changeset

The single transactional changeset in the Technical Design (steps 2 to 7; steps 1 and 8 are operational), plus
the local disk preflight.

### Phase 2: Engine

#### Step 1: Write path and tenant creation

- Tenant creation, engine side. The migration changeset (Phase 1) creates the `service_tokens` AFTER INSERT trigger and `create_tenant_partitions`; this step covers the engine's three token INSERT sites that fire the trigger, and the error mappings below.
- The refusal of a write for a model with no partition, naming the model.
- Every `SET CONSTRAINTS` site, Java and plpgsql, schema-qualified.
- Every chunk-writing and chunk-referencing write supplies `embedding_model`.
- The Java upsert's conflict target becomes the four-column PK.
- The no-leaf error is mapped to a 500.
- A lock timeout (SQLSTATE 55P03) during token issuance is mapped to a retryable 503 in `TokenAdminHandler` and `DataTokenHandler`, not to `HttpUtil`'s default 500.
- The jOOQ record-count guard is bumped in the same change as the schema. The jOOQ exclude pattern (service/pom.xml `excludes`) lands with the schema change, in the Phase 1 changeset's commit, and covers partition leaves, model partitions, `chunks_retired_225` (until `vectors-036-1` dropped it and its exclude, nexus-3wh8d.22) and the `_new` names (`chunks_new`, `taxonomy_centroids_new` and what is created under them), so only the parents generate classes.

#### Step 2: Read path

Add the model and tenant predicates to every search family and to the router
probe.

*2026-10-07 (nexus-3wh8d.15, as built).* The definitions live in two further
changesets, `vectors-031-1` and `vectors-031-2`, after `vectors-030-1` and
before the `runAlways` grants; they are not part of the single walk. `-1`
redefines the ten families (`plain_search`, `text_gated_search_hnsw_first`,
`text_gated_search_by_chash`, `search_metadata_scoped`, `search_aspect_scoped`,
`search_graph_hop`, `search_topic_scoped`, `taxonomy_ann_query`,
`assign_from_chashes`, `cross_preview`) at 384, 768 and 1024: two trailing
parameters, `p_embedding_model` and `p_tenant`, an explicit predicate on both
on every scan, the old signatures dropped in the same changeset and the new
ones granted to `nexus_svc`, and a post-condition that fails the migration
unless each of the 30 names has one signature. The two plpgsql taxonomy
functions carry a function-level `SET plan_cache_mode = force_custom_plan`, so
their inner statements are planned with the call's values; the inlined SQL
functions get that from the engine's session setting. `-2` redefines
`text_gate_probe_<dim>`: definer plpgsql where `vectors-029`'s guard still
holds (the owner policy names exactly the migrating role and no RLS-subject
login role has its privileges, the same test and post-condition as
`vectors-029`), invoker SQL otherwise. The probe is plpgsql because a SQL
definer function is never inlined and its body is planned without the call's
values. The explicit tenant predicate is in addition to the session GUC
predicate, never instead of it. `taxonomy_ann_query` takes the SOURCE
collection's model in both branches, so cross-collection projection matches
centroids of that model only, as `assign_from_chashes` and `cross_preview`
already do. The two changesets run after `vectors-030-1` has committed, so each
is a failure point after the irreversible step; the deploy runbook lists them.
The walk's expected counts become `new_changesets=3`, `pending_at_start=15`.
The walk ends with ANALYZE and cannot VACUUM, so every new leaf is unvacuumed
until the post-boot VACUUM of the runbook (§ 5.9) or autovacuum has run; until
then the router's probe can take a sequential scan on a large leaf.

#### Step 3: Checks

- Extend `ChunksIsolationCheck` and the doctor RLS canary to every model partition and leaf.
- Add the doctor rows comparing tenants against leaves and models against partitions.

### Phase 3: Release

#### Step 1: Rehearsal and deploy

A PITR-fork walk rehearsal of the real changeset at production scale
(engine-release Step 5b), marked IRREVERSIBLE, with the freeze length from H6 and
the stop-start topology conexus confirmed. *As built (2026-10-22): H6 (step 4)
was dropped, so no H6 run supplied a freeze length; the rehearsal measured the
walk at 449 s (Revision History, 2026-10-07 line; T2
`nexus_rdr/225-rehearsal-2026-10-07`).* Its measured walk time is the input to the
downtime budget (confirmed by Sam 2026-10-06; see Prerequisites).

The deploy and abort procedure is [`docs/runbooks/rdr-225-cloud-deploy.md`](../runbooks/rdr-225-cloud-deploy.md)
(nexus-3wh8d.26): the pre-walk census with its abort thresholds, the prediction for the fork walk's
`schema_migration_complete` line, the UTC restore-point capture, and the abort decision keyed on the database state
(not on the absence of a log line: `vectors-030-1` can commit and a later `runAlways` changeset can still fail the
boot). After the walk commits a tag flip to the previous engine is not a rollback; the choices are fix-forward or PITR,
on Sam's go. The numbers the rehearsal must supply are listed there (nexus-3wh8d.27).

#### Step 2: Tenant-removal runbook

The runbook is [`docs/runbooks/rdr-225-tenant-removal.md`](../runbooks/rdr-225-tenant-removal.md) (nexus-3wh8d.21). `DropTenantPartitionsIntegrationTest` exercises it end to end, tokens included, and also pins the direct-DROP refusal (SQLSTATE `2BP01`).

2026-10-06: `nexus.drop_tenant_partitions(tenant)` exists (changeset `vectors-030-1`, nexus-3wh8d.7) and the runbook calls it, as the schema owner, for its first two statements: it deletes the tenant's manifest, topic-assignment and orphaned-at rows, then DETACHes and DROPs the tenant's leaf under every model partition of `chunks` and `taxonomy_centroids`, and returns the number of leaves dropped. It refuses `default`, a second call returns 0, and it is not SECURITY DEFINER and is granted to no engine role. It does not delete the tenant's tokens: the runbook deletes them afterwards as its own statement.

#### Step 3: Drop `chunks_retired_225` after 14 days

2026-10-21 (as built, nexus-3wh8d.22): changeset `vectors-036-1` (`vectors-036-drop-retired-225.xml`, after `vectors-035`, before the runAlways grants) drops `chunks_retired_225` and `taxonomy_centroids_retired_225`, each with its `_retired_225` indexes, constraints and policies. For each table that exists it logs `vectors-036-1: dropping nexus.<table> (N row(s))` with `RAISE NOTICE` (counted with FORCE RLS switched off, since the migrating role has no BYPASSRLS) and runs `DROP TABLE IF EXISTS`, with no CASCADE, so a dependent view fails the drop and leaves both tables in place. An absent table is skipped. The changeset comment states the DATA EFFECT (irreversible) and the local window: the time between the engine release carrying `vectors-030-1` and the one carrying this changeset, zero for an install that crosses both in one boot. The rollback recreates both tables empty in the shape `vectors-030-1` leaves, because `vectors-030-1`'s rollback raises when `nexus.chunks` is partitioned and a retired table is missing; `P225DropRetiredIntegrationTest` compares columns, indexes, constraints, policies, RLS and FORCE, reloptions, owner and triggers between the walked state and the rolled-back one. The jOOQ exclude for the retired names and the two schema-parity inventory entries are removed in the same change. Not yet run when this paragraph was written: the PITR-fork walk of the changelog through this changeset, and the cloud deploy. *Both are done (nexus-3wh8d.22 close reason, 2026-10-21):* conexus's PITR-fork walk was clean (434,642 chunk rows and 804 centroids, 0 dependent objects), and the changeset was deployed to the cloud at 21:39:47Z in engine-service-v0.1.162 (tag on `47e23a841`), with boot NOTICEs matching the fork, conexus's STEP-6 green (`gate-report-20261021T214114Z-v011.json`) and the nexus Step 6.1 client-path gate 12/12. Local installs get it with conexus 7.83.1, which pins `REQUIRED_ENGINE_VERSION` 0.1.162 (CHANGELOG 7.83.0 and 7.83.1).

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Tenant leaves (`chunks`, `taxonomy_centroids`) | In scope: doctor row (local install only; cloud: conexus's read-only query, runbook § 7.6) | In scope: rows per (model, tenant) | In scope: runbook DETACH and DROP | In scope: doctor compares token tenants against leaves (local install only; cloud: conexus's read-only query, runbook § 7.6) | Cluster backups |
| Model partitions | In scope: catalog query | In scope | Deferred: retiring a model is its own changeset | In scope: doctor compares `embedding_models` against partitions (local install only; cloud: conexus's read-only query, runbook § 7.6) | Cluster backups |
| `chunks_retired_225` | N/A | N/A | In scope: Phase 3 Step 3 | N/A | Cluster backups during the window |

**Where the doctor rows run (2026-10-07, nexus-3wh8d.18).** The tenant, model and leaf rows read the database through local admin credentials, so they run on a local install only; on a managed install they report "not applicable", and the engine has no server-side equivalent (its boot check, `ChunksIsolationCheck`, covers the RLS structure of both trees and says nothing about token tenants or registered models). In the cloud the "doctor compares token tenants against leaves" and "compares `embedding_models` against partitions" cells are conexus's read-only query, which the deploy runbook carries (§ 7.6). A cloud tenant with a token and no leaf is otherwise found by a 500 on its first write. Adding the comparison to the engine's status or boot check would be a wire-ledger change and is not built.

**Decided in P1.3 (2026-10-06): the scope of the purge VACUUM.** After `purge_trash` commits, `CatalogRepository.runPostPurgeVacuum` runs `VACUUM (ANALYZE) nexus.chunks` (and two other tables) through `TenantScope.vacuumAnalyze`, whose allowlist names the parent tables. On a partitioned parent that statement processes every leaf of every tenant, although the purge touched one tenant. Either every tenant's leaves are vacuumed, as the parent-level statement does, or only the purging tenant's leaves, which needs leaf names the allowlist can validate (they are generated hashes) and MAINTAIN on each leaf. Decision: the VACUUM stays at the parent for every tenant, the same cost as the old statement, which vacuumed the whole table. `grants-005` grants MAINTAIN on every partition (it loops over the partition tree), because VACUUM checks MAINTAIN on each partition it recurses into. `TenantScope.VACUUM_ALLOWED_TABLES` and `CatalogRepository.PURGE_VACUUM_TABLES` keep naming `nexus.chunks`. No per-tenant leaf list was built.

## Test Plan

- **Every search family under the tenant GUC.** Verify: the plan shows exactly one (model, tenant) leaf, pinned per family.
- **No tenant GUC, and tenant B's GUC.** Verify: zero rows, and no tenant-A row is ever returned.
- **A first token through each entry point (tenants/create, service-tokens/issue, data-tokens/mint, the boot `ensureBootstrapToken`), and a rotation; then a write.** Verify: the leaves exist and the write lands in them, and a second token for the same tenant is a no-op.
- **A fresh install.** Verify: the `default` tenant has its leaves from the migration, and its first write succeeds.
- **A tenant creation behind an open conflicting lock.** Verify: it fails at `lock_timeout` (2 s) with a retryable 503; other writers wait at most the creation's remaining time, as measured in Step-2 results, and the engine's own bound on the token insert is checked end to end.
- **300 tenants created.** Verify: creation time and search planning time are within TS1 and TS2.
- **Taxonomy search families under the tenant GUC.** Verify: the plan shows one (model, tenant) leaf of `taxonomy_centroids`.
- **Every write family: insert, upsert, delete, canonical and cross-model rename (including the `ChashRepository.renameCollection` route), quarantine, restore, GC, and `purge_trash` with deferred FKs.** Verify:
  - the four-column FKs and conflict targets hold;
  - cascades fire;
  - `SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED` works against the partitioned parent.
- **A chunk written with a model or vector dimension that disagrees with its collection.** Verify: refused.
- **A new model added by changeset.** Verify: the partition and a leaf per existing tenant exist, and a write lands. A write for an unregistered model is refused before any SQL, naming the model. This refusal is reachable only by a direct call to `ModelPartitions.require`: `catalog_collections.embedding_model` has a foreign key to `embedding_models`, so no write can name a collection whose model is unregistered (2026-10-06).
- **Migration of a seeded two-tenant, two-model store, including hidden rows and a centroid with no registry row.** Verify:
  - exact reconciliation;
  - ANALYZE ran;
  - the views and policies are on the new table;
  - an injected failure leaves every table unchanged.
  - a row whose vector dimension disagrees with its collection's model makes the walk fail and leaves every table unchanged;
- **Recall and latency non-inferiority.** Verify: protocol step 3's rule. *Dropped (2026-10-06, Decision note at the top); no bead covers it (phase-review cross-walk, T2 `nexus_rdr/225-phase-review`).* Replaced in practice by the post-walk 12-search smoke test and the cloud STEP-6 gate, which is where the recall miss of nexus-3wh8d.31 was found (Minimum Viable Validation). Neither is a non-inferiority test, and the cause of that miss is not established (Decision note).
- **A local install.** Verify: the preflight, the migration and search. *Evidence as of 2026-10-22 is indirect (nexus-3wh8d.9 is the only bead mapped to this item, and the cross-walk marks it partial).* It rests on three things, none a walk of a populated local install at a stated scale:
  - the preflight, in `P225DiskPreflightIntegrationTest` (`service/src/test/java/dev/nexus/service/`, nexus-3wh8d.9);
  - the `--package-upgrade` convergence run in the release battery, which is toy scale. For conexus 7.73.0 it upgraded a container install holding one T1 scratch row and no chunks, so `vectors-030-1` ran over an empty estate (CHANGELOG 7.73.0, "What proves it, and at what scale"). The 7.83.0 release battery and the published-bytes rerun of 7.83.1 exercise vectors-033 to vectors-036 (the retired-table drop), not the `vectors-030-1` walk, because engine 0.1.160 is already post-walk: they converged 7.82.0 plus engine v0.1.160 to v0.1.162, service healthy and T1 surviving, at the harness's toy seed (T2 nexus/release-7.83-battery-and-published-bytes-2026-10-21);
  - the walk at fixture scale, `P225MigrationWalkIntegrationTest` (12 chunks, 5 tenants, per the runbook § 9.1 row "local-install seed count").
  The runbook records "Not measured on a real local estate" (`docs/runbooks/rdr-225-cloud-deploy.md` § 9.1, row "local-install seed count"), and the engine's 2.2x factor is unmeasured (Local disk preflight). The at-scale proof is the cloud fork walk, not a local one.

## Finalization Gate

### Contradiction Check

The fix checks' counted findings and observations (T2
`nexus_rdr/225-fix-check-c0feaa2b1`, `-2296ed853`, `-f3ab64e51` and `-7fafc96c2`)
and the step-2 results are reconciled in the text. These are now stated once:
- the write freeze in step 4 and the migration;
- the token trigger covering the three token INSERT sites;
- the post-swap trigger redefinition;
- the nullable-then-NOT-NULL order;
- the fallback's differences;
- RLS on the parent and the leaves;
- the leaf count;
- the disk arithmetic.

Two things are open and recorded (as of the 2026-10-05 gate; see the 2026-10-22 status under Assumption Verification below):
- The per-model split is held only to non-inferiority, and no recall gain is claimed for it.
- Several step-2 measurements are thinner than the addendum's rules. They are re-run in implementation, as listed in Prerequisites.

*As built (2026-10-22): the first item stayed as it was. Step 3 was dropped, so the split was never held to non-inferiority; see the Decision note.*

### Assumption Verification

- **Verified in part:** research-14 (F7 on the chunk search families; taxonomy tenant-level pruning is open) and research-15 (H4 on four paths; TS1 and TS2 thinner than their rules; TS3 cases b and c).
- **Open, as Implementation Plan prerequisites:**
  - H4 bulk insert, repeated on the step-3 fork;
  - the taxonomy tenant-level pruning test;
  - TS1 to TS3 as written, and the orphaned-at FK and GC write families;
  - protocol steps 3 and 4.

  *Status on 2026-10-22.*
  - **Closed:** the taxonomy tenant-level pruning test (nexus-3wh8d.15); TS1 (nexus-3wh8d.7), TS2 (nexus-3wh8d.15) and TS3 case (a) (nexus-3wh8d.8); the orphaned-at FK test (nexus-3wh8d.19, `98fe4501b`). The GC write families are among the mutation controls of nexus-3wh8d.19, which passed with gaps (T2 `nexus_rdr/225-test-validation-p2`).
  - **Dropped, with the decision recorded:** protocol steps 3 and 4 (2026-10-06). Step 4 is replaced by the PITR-fork walk (T2 `nexus_rdr/225-rehearsal-2026-10-07`). Step 3 has no replacement that tests non-inferiority (Test Plan item 12).
  - **Closed after the cross-walk:** H4 bulk insert. The step-3 fork it was to run on was dropped with step 3, and the Phase 3 rehearsal did not measure it, so it was measured on the local substrate (nexus-3wh8d.32, 2026-10-21): 0.93, 95% CI [0.92, 0.94], under the rule. Nothing in this list is still open.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| FK referencing a partitioned table, with a PK that includes the partition keys | PostgreSQL 17 | Spike (step 2) |
| Plan-time pruning on literal `embedding_model` and `tenant_id` under `force_custom_plan` | PostgreSQL 17 | Spike (step 2, research-14) |
| HNSW declared on a partitioned parent | pgvector 0.8.2 | Spike (step 2 prototype) |
| RLS is not inherited by partitions; queries through the parent use the parent's policies | PostgreSQL 17 | Docs (PostgreSQL row-security and partitioning). The step-2 prototype applied RLS to the parent and every leaf, and the cross-tenant controls held |

### Scope Verification

The MVV is the Phase 3 Step 1 rehearsal: the real changeset and engine against a
PITR fork of production. Protocol step 4 (H6) rehearses the scripted migration
steps on a fork of its own and does not run the real changeset. Both are in scope.
*As built (2026-10-22): protocol step 4 (H6) was dropped, so the Phase 3 rehearsal of the real changeset is the only rehearsal that ran; see the Decision note.*

### Cross-Cutting Concerns

- **Versioning:** a schema-carrying engine tag. The client carries the doctor changes of Phase 2 Step 3 (the RLS canary extension and the tenant and model rows in `health.py`), so a client release goes with the engine. `REQUIRED_ENGINE_VERSION` is bumped to the new engine, as every engine release requires (AGENTS.md § Engine-service release), because local installs get the engine only through it. The paired-release choreography applies.
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
- The design names every object the migration touches. The generated inventory that was meant to pin the list was reverted (Phase 1 Step 1).

## Later work

*Added 2026-10-22, at close.* Three RDRs written after this one build on the
layout it shipped.

- **RDR-227 (closed 2026-10-12)** added per-collection partial HNSW indexes
  above the router threshold. It "keeps the leaf and adds, for large
  collections only, an index scoped to one collection" (rdr-227:125-129). It
  repairs the filtered-walk recall problem inside a leaf that many collections
  share: "a full page missed the collection's true nearest row (recall against
  exact 0.925 at k=40 on code__1-72, 27,893 rows ...). With its own index the
  same collection recalled 0.99 or better at k 40 to 120" (`PgSession.java:766-772`).
  Gap 3 above is therefore closed at leaf scope only: a search is pruned to one
  (model, tenant) leaf, but inside a leaf the collection filter still walks the
  shared graph unless the collection has its own index. RDR-227 does not reopen
  Alternative 3 or 4 as written: its own Alternative 3, "a narrower version of
  that layout (large collections only)", "is not pursued" (rdr-227:143-144).
  Alternative 4's reason, that the router already serves small collections
  exact, holds up to the router threshold, which has moved (Decision note).
- **RDR-226 (abandoned 2026-10-09)** proposed one statement per leaf and
  stopped at its Phase 0 gate. It "changes how the route uses that leaf, not
  the leaf" (rdr-226:153).
- **RDR-230 (draft)** adds event-keyed REINDEX and a recall canary for the
  per-collection indexes, and cites this RDR's per-leaf recall analysis
  (rdr-230:122).

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
- 2026-10-05: Evaluation protocol revision 4. The step-1 census showed the benefit gate cannot be decided on real queries (10.1% gate population, 13 code and 27 prose distinct queries). Sam ruled: prove it works, not a benefit. H8 is replaced by a non-inferiority test (A6 vs A1t, router on and router off), the feasibility checks (F7, H5, H4) become a local hard gate, and round 3's harness-fidelity, router-threshold, hybrid and taxonomy corrections are adopted. Evidence copied to ~/nexus-evidence/rdr225-2026-10-05/. *(As built, 2026-10-22: the "10.1% gate population" is the census at the harness parameter T=60000, not at a shipped router default; see the Decision note.)*
- 2026-10-05: Evaluation protocol revision 5, answering T2 critique-rdr225-evaluation-protocol-round4-2026-10-05 (3 critical, 11 significant; 'ready after small text edits'). Disjoint OK/BAD/UNKNOWN states with an exactly-once outcome table and DEFER-UNDERPOWERED; co-resident build pairs for ABBA cycles; degraded-arm positive control and ceiling check; concrete F7 observables and controls; H5 diff script and smoke run; H4 comparator and CI; SQL replay harness validated against the engine; synthetic generation rule; pilot-set floors; frozen protocol constants.
- 2026-10-05: Protocol revision 6 (frozen), applying the round-5 fixes without a further critique round, per Sam ('fix all the things and button this RDR up'). Technical Design, Failure Modes, Implementation Plan, Day 2, Test Plan and Finalization Gate written.
- 2026-10-05: Gate round 1 — BLOCKED (2 Critical, 7 Significant, 2 ship-blocker(s)); commit `71437ca05`; critique `nexus_rdr/225-gate-critique-2026-10-05-r1`.
- 2026-10-05: Fixes for gate round 1. Sam chose partitions of one logical `nexus.chunks` (by model, then tenant) over separate tables plus an identity table, which keeps RDR-191's manifest FK guarantee, and tenant leaves created at tenant creation. Inventory, migration (freeze, ANALYZE, failure, rollback, local preflight), fallback layout, write-path facts, contradictions and terms corrected.
- 2026-10-05: Fixes for the round-2 fix check (T2 `nexus_rdr/225-fix-check-c0feaa2b1`, FAIL). Tenant leaves now come from `issueToken`, which covers all three token paths. Step 4 now rehearses under write freeze (dated addendum). A step-2 addendum defines T1 to T3 for tenant scale. The Technical Design is rewritten to cover: the full chunks inventory (conflict targets, write sites, triggers, views, policies, grants, names), RLS per leaf, NOT NULL model columns, the dimension CHECK, new-model onboarding, taxonomy, the cross-model rename, a single transactional migration with the cloud topology as a prerequisite, and the precise fallback layout. Stale F7 text, overclaims and citations are corrected.
- 2026-10-05: Sam chose to accept with the gate overridden ("Accept now with residuals"). `nx rdr set-status` refused (gate-not-passed), so the RDR stayed draft and returned to the gate. The counted defects of fix check 2 (T2 `nexus_rdr/225-fix-check-2296ed853`) are folded into the text as known fixes:
  - a `service_tokens` trigger, plus `default`-tenant leaves in the migration;
  - manifest-trigger and write-site redefinition only at step 7.8;
  - nullable-backfill-NOT NULL;
  - the outbound composite FK and `taxonomy_centroids` swap in step 7.6;
  - a scoped ON CONFLICT rule;
  - the third view;
  - leaf grants, the function contract, and holding-collection disposition for disputed rows (never-wedge).

  The remaining items are implementation beads.
- 2026-10-06: The migration changeset built (nexus-3wh8d.8). Recorded: placeholder provider `disputed`; the step 2b rules for mixed-dimension collections, mismatched and unregistered centroids and centroid-only tenants; the `gc_audit` trail; step 7.7's additions; the walk's lock count (1,899 relation locks for 70 leaves, about 27 per leaf; the default lock table holds `max_locks_per_transaction` 64 times `max_connections` 100 = 6,400 slots, enough for about 230 leaves, which is about 16 tenants at seven models and two parents, so a production count above that needs a raised `max_locks_per_transaction`); a NULL `embedding_model` fails tuple routing with SQLSTATE 23514 ("no partition of relation found for row") before NOT NULL is checked; the rollback is the inverse of the swap while the retired tables exist, not a supported recovery.
- 2026-10-05: All open findings and observations from fix checks 1 and 2 and the step-2 results are addressed in the text. Changes: TS1 to TS3 relabelled, with sample sizes and results; dated record of the frozen-text edits; RLS on the parent and the leaves; lock_timeout and a retryable 503 on tenant creation; idempotency by partition bound; tenant removal order; new-model refusal and test; scoped constraint renames; schema-qualified `SET CONSTRAINTS` everywhere; T2 22410 topology; disk arithmetic; leaf count; precise fallback; H4 bulk as a prerequisite; taxonomy tenant pruning as a test; an honest Contradiction Check.
- 2026-10-05: Fixes for fix check `225-fix-check-f3ab64e51`:
  - one disposition for disputed collections (placeholder models, migration step 2b);
  - NO FORCE RLS during the walk;
  - the outbound FK validated directly;
  - taxonomy centroid copy and reconciliation;
  - the trigger and functions in the migration changeset only;
  - inventory scope covering the referencing tables;
  - the lock-timeout 503 mapping;
  - honest step-2 results and a complete record of dated edits;
  - corrections to the counts, wording and the history line.
- 2026-10-05: Gate round 2 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); commit `7fafc96c2`; critique `nexus_rdr/225-gate-critique-2026-10-05-r2`.
- 2026-10-06: Factual corrections from bead nexus-3wh8d.24, the P1.1 inventory critique and plan-audit round 2, with no change to any decision or to the Evaluation Protocol. Line citations refreshed against develop `8bfa4d01e` (Main.java, PgVectorRepository, CatalogRepository, health.py, TaxonomyCentroidRepository). Technical Design: row 1 names the one live `SET CONSTRAINTS` site; row 5 lists the live write-site bodies and drops the superseded ones; added the `ChashRepository.renameCollection` route, the `upsertManifestChunkVectors` writer, and the Java registries and VACUUM allowlists as items decided in P1.3; Day 2 gains the purge VACUUM scope question. Cloud topology recorded from conexus-eb (T2 `nexus_rdr/225-cloud-topology`): stop-start, freeze inherent, PITR owner, downtime budget confirmed by Sam (only tenant). Technical Environment: no replica, storage figures unreconciled. F7 and research-14/-15 relabelled Verified in part; the hidden-rows sentence, the doctor client change, the jOOQ exclude timing, Phase 2 Step 1's trigger scope, the disk preflight multiple, Scope Verification and the Contradiction Check count corrected. TS1 to TS3 and the H4 bulk repeat are implementation gates, as the Prerequisites already state; the frozen step-2 text is unchanged.
- 2026-10-06: Decision (Sam): protocol steps 3 and 4 dropped; the design is built as chosen and checked on the production-fork rehearsal with real logged queries before and after. Prerequisites, Minimum Viable Validation and Phase 1 Step 1 updated to match. Protocol text kept as history.
- 2026-10-06: Sam: legacy mixed-dimension data shapes are not supported; the walk fails on one instead of re-registering it. Removed the placeholder models, step 2b, the mixed-dimension and mismatched-centroid rules, the doctor listing of disputed collections and their Test Plan lines. Earlier dated entries that mention them describe superseded text.
- 2026-10-06: Accepted risk (Sam, 2026-10-06): voyage-code-3 and voyage-context-3 are both 1024-dimension, so the dimension CHECK cannot tell them apart. A chunk embedded by the other Voyage model inside a collection is filed under its collection's registered model without error. The walk does not detect this, and nothing is built to detect it.
- 2026-10-06: Phase 2 Step 1 built (nexus-3wh8d.13). Recorded in Technical Design, Tenant creation: the token-insert bound (1 s statement timeout, 3 attempts, 200 ms plus jitter) with its measurements. No decision changed.
- 2026-10-06: TS1 measured in implementation (nexus-3wh8d.7) and the lock wording corrected to the measured behaviour: the lock timeout bounds each acquisition, not the whole creation; a pending creation holds up writers to the registry, the referencing tables and the model partition it is on, across tenants; no statement timeout inside the function, a caller-side one bounds the whole creation. Signatures updated to `create_tenant_partitions(parent, tenant, force)` and `create_model_partition(parent, model, force)`; leaf RLS policies and grants are copied from the parent. No decision changed.
- 2026-10-06: Fix round after the Phase 1 code review and critique (nexus-3wh8d.10, .11, .14). Recorded, with no change to a decision of Sam's: what guards a missed write site now (`PlpgsqlCheckGateTest` and the runtime write-family tests, not the reverted inventory) and what `plpgsql_check` cannot see; the P1.3 decisions (purge VACUUM at the parent with MAINTAIN on every partition, the registries, 409 `collection_model_mismatch` on cross-model rename and re-home, 422 for manifest writes to an unregistered collection, centroid upserts requiring a registry row, and the user-visible change in `ChashRepository.renameCollection`); the Liquibase rollback restores table shape only and is not a recovery path; the lock figures restated for four models and two parents (about 29 tenants in the default lock table) and the seven-model figures withdrawn; the Failure Modes wording for plans and statistics reduced to what the tests show; `create_tenant_partitions` refuses `force = false` outside a `*_new` parent; `create_model_partition` covers every tenant that has a leaf under a sibling model; the unregistered-model refusal is reachable only by a direct call.
- 2026-10-06: Test-suite teardown (nexus-3wh8d.7, Sam's decision): `nexus.drop_tenant_partitions(tenant)` added to `vectors-030-1`, and the Python engine substrate calls it for each minted test tenant when the test ends. Reason: one tenant per test, never removed, took a worker's PG past 2,000 tenants and the token insert past its bound (503 `tenant_creation_busy` on 6,106 tests). No design decision changed.
- 2026-10-07: Phase 2 read path (nexus-3wh8d.15) and checks (.16) built; fix round after their code review and critique (nexus-3wh8d.17, .18). Recorded, with no change to a decision of Sam's: the read-path changesets `vectors-031-1` and `-2` (Phase 2 Step 2); the walk's expected counts (3 new, 15 pending); the unvacuumed-leaf state after the walk and the post-boot VACUUM; the local-only scope of the doctor rows (Day 2).
- 2026-10-12: TS2's 2x rule amended on Sam's decision: it fails a family only when the 300-tenant p95 is also more than 2 ms above the 2-tenant p95 (Step-2 results, TS2). One row had failed at 2.05x on a 0.6 ms baseline. The 25 ms cap is unchanged.
- 2026-10-14: Research finding 17 (pg_vectorsearch's PRISM index) recorded as documented, not adopted, on Sam's request. No change to a decision.
- 2026-10-07: Built and deployed to the cloud (nexus-3wh8d.20). engine-service-v0.1.150 carried `vectors-030-1` to `-031-2`; the walk committed at 16:15:55Z in 7 min 53 s with exact counts. The PITR-fork rehearsal of the same changeset ran earlier that day (T2 `nexus_rdr/225-rehearsal-2026-10-07`: 449 s, 434,617 chunks, 32 leaves). Recorded 2026-10-22 under Minimum Viable Validation and Local disk preflight.
- 2026-10-07: The cloud STEP-6 gate found recall@10 0.90 on one of twelve queries at `ef_search` 200 (nexus-3wh8d.31). The engine's default `ef_search` floor became 600 (engine-service-v0.1.151, flipped in the cloud at 21:59:49Z, STEP-6 then 1.00 on all 12). Recorded under Technical Environment and Minimum Viable Validation.
- 2026-10-21: `vectors-036-1` dropped `chunks_retired_225` and `taxonomy_centroids_retired_225` (nexus-3wh8d.22), shipped in engine-service-v0.1.162 and deployed to the cloud at 21:39:47Z after a clean PITR-fork walk. Phase 3 Step 3 updated.
- 2026-10-22: Reconciliation (nexus-3wh8d.33, text only; no decision changed and the frontmatter is untouched), from the phase-review cross-walk (T2 `nexus_rdr/225-phase-review`). Left open outside this file: the epic nexus-3wh8d acceptance still names the dropped protocol recall rule (recorded on the epic for Sam's close). Recorded: the MVV was reduced to a post-walk 12-search smoke test and the recall regression was found after the deploy; storage figures and the `ef_search` floor in Technical Environment; the measured disk peak against the inferred preflight factor; the UNIQUE on `catalog_collections` named in migration step 2 (`vectors-030:127`); Prerequisites ticked with their beads; Phase 3 Step 3 "not yet run" closed; Overall, Test Plan item 12 (dropped, no replacement test) and item 13 (indirect evidence, no measured local walk); the Assumption Verification list. The H4 bulk-insert lines are untouched and belong to nexus-3wh8d.32.
- 2026-10-22: H4 bulk insert result (nexus-3wh8d.32): re-measured on the as-built layout, new/old backend CPU 0.93, 95% CI [0.92, 0.94], under the 1.25 rule. Recorded in Research Findings, Step-2 results, the Finalization Gate, the prerequisites and Assumption Verification; the frozen protocol text is unchanged.
- 2026-10-22: Close-time correction (T2 `nexus_rdr/225-closure-critique-2026-10-22`; text only; no decision, no frozen protocol text and no frontmatter status changed). Recorded: the router basis of Sam's 2026-10-06 decision (the 60,000 threshold was the census parameter, the shipped default was 10,000, and the 82% denominator was 552 calls against 503 searches); as-built pointers at every site that says the protocol shows, proves or holds the split to non-inferiority, with the cloud-gate recall and the per-leaf analysis (cause of the nexus-3wh8d.31 miss not established); the four open test-gap beads (nexus-3wh8d.34 to .37); the `ef_search` 1000 override for a single-collection plain search without its own index; the Day 2 doctor-row scope; the H4 raw-data path under `~/nexus-evidence/`; a Later work section and `related_rdrs` entries for RDR-226, RDR-227 and RDR-230.
