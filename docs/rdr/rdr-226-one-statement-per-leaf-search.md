---
title: "Per-Collection Search in One Statement per Leaf"
id: RDR-226
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-10-08
accepted_date:
related_issues: [nexus-tu8wp, nexus-tu8wp.6, nexus-3wh8d, nexus-92q1p]
related_rdrs: [RDR-225, RDR-192, RDR-217]
---

# RDR-226: Per-Collection Search in One Statement per Leaf

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Drafted 2026-10-08 against develop `c991266e2`. No product code has changed.

**Provenance.** Sam, 2026-10-08: making breadth cheap was the whole intent of
RDR-225's partitioning. RDR-225 put every collection of one (embedding model,
tenant) pair into one leaf with one HNSW graph, but the read path it shipped
still runs one SQL statement per collection. This RDR designs the collapse of
that fan-out into one statement per leaf, or a small number per leaf, without
losing what the per-collection route exists to guarantee.

### Terms used here

- **Collection**: a named set of chunks embedded by one model, for example
  `knowledge__dt-papers__voyage-context-3__v1`.
- **Leaf**: one physical partition of `nexus.chunks`, holding one embedding
  model's rows for one tenant (RDR-225). Every collection in one search request
  shares one model and one tenant, so one request reads one leaf.
- **HNSW**: the approximate nearest-neighbour graph index pgvector builds. A
  search walks the graph from an entry point toward the query.
- **Exact search**: read every candidate row, compute its distance to the
  query, and sort. Complete by construction, and its cost grows with the
  number of rows read.
- **Top-K**: the K rows nearest the query. **Per-collection top-K** means each
  collection's own K nearest, as opposed to the K nearest of all collections
  together.
- **Crowd-out**: in a single "nearest K of the union" query, one dense
  collection can take every slot, so a small collection returns nothing.
- **The route**: `POST /v1/vectors/search-per-collection`, the engine endpoint a
  default search calls once per embedding-model group (nexus-tu8wp.1).
- **Arm**: the route's unit of work today. One arm is one collection's search:
  its own database transaction, router probe and `plain_search_<dim>` statement.
- **Arm permit**: one slot of the cross-request semaphore that limits how many
  arms run at once on the connection pool (`NX_SEARCH_FANOUT_ARM_PERMITS`).
- **The router** (cardinality router, nexus-tu8wp.6): before each search
  statement, a bounded count of the physical rows the statement selects. At or
  below `NX_SEARCH_EXACT_MAX_ROWS` (called **T** below) the statement runs
  exact; above it, it walks HNSW.
- **Threshold**: a per-collection distance cutoff the client sends. A row whose
  distance is above it is dropped.
- **Overfetch multiplier**: the client's factor (4 for knowledge, docs and RDR
  collections, 2 for code) that sets how many rows it asks each collection for.
- **live(c)**: RDR-192's rule for which chunks a search may return, written in
  SQL as `EXISTS (SELECT 1 FROM nexus.chunk_live_owners(...))`.
- **Window function**: a SQL function computed over a set of rows related to
  the current row. `row_number() OVER (PARTITION BY collection ORDER BY
  distance)` numbers each collection's rows 1, 2, 3 ... in distance order, so
  `row_number <= K` keeps each collection's own top-K.
- **Detoast**: read a large value (here a vector of 1024 floats, about 4 KB)
  from its out-of-line TOAST storage. An exact scan detoasts every vector it
  scores.
- **Round trip**: one request from the engine to PostgreSQL and its reply.
- **Changeset**: one Liquibase schema migration step, run when the engine boots.
- **PITR fork**: a point-in-time copy of the production database, used for
  rehearsals (`deploy/RESTORE.md` in the conexus repo).
- **Bin**: in this design, a group of small collections searched by one exact
  statement.

## Problem Statement

A default search (`knowledge`, `code`, `docs`, `rdr`) sends two requests to the
route, one per embedding model, and the engine runs one arm per collection: 61
for the voyage-context-3 knowledge group and 28 for the second request
(measurement M1). Each arm is a separate transaction and statement, and only
five run at once. The cost of a request therefore grows with the number of
collections it names, not with the number of rows it returns.

### Enumerated gaps to close

#### Gap 1: Engine time grows with the number of collections, not with the work

The handler spends 441 to 1194 ms per request, almost all of it in the fan-out
SQL, and a 61-arm request takes about 1.2 s even when it returns 5 rows (M1).
Every arm pays a fixed cost: four round trips since nexus-wym0l (C6), a plan,
a router probe, a connection borrow and a permit wait. RDR-225 made every
collection of a request live in one leaf, so one statement could read all of
them, but the route still plans and runs them one at a time.

#### Gap 2: Five permits serialise the arms

The arms of one request share `NX_SEARCH_FANOUT_ARM_PERMITS` with every other
request: default half the pool (5 on the default pool of 10), ceiling pool
minus 2 (8) (C4). A 61-arm request runs in about 12 waves. Raising the permits
buys at most 8/5 on this term and takes connections from writes and plain
search (M1, "do not raise `NX_POOL_SIZE`; conexus re-measures first").

#### Gap 3: The guarantees that justify the arms must survive any collapse

The per-collection route exists so that each collection gets its own top-K
(no crowd-out), its own threshold, its own router decision, and its own failure
isolation (CHANGELOG nexus-tu8wp.1, nexus-tu8wp.2, nexus-tu8wp.6). The
nexus-tu8wp design rejected a one-statement form for five stated reasons (R1 to
R5, § Relationship to Prior RDRs). Any collapse has to answer each reason and
return the same response the client reads today, so that no client change is
needed.

## Relationship to Prior RDRs

Searched the RDR index and corpus for "fan-out", "per-collection", "top-k" and
"search latency". The per-collection route itself was designed in a bead, not
an RDR, so its design record (T2 `nexus/design-tu8wp-engine-per-collection-topk-2026-10-04`,
parts 1 and 2) is treated as an origin here.

| Prior record | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-225 | Origin (parent) | Partitioned `chunks` by model, then tenant, so one request reads one leaf. Its Phase 2 read path (`vectors-031-1`) kept the per-collection arms and added the model and tenant predicates to them. This RDR changes how the route uses that leaf, not the leaf. |
| nexus-tu8wp design (T2 28937, 28938) | Origin | Chose "Java-level parallel arms over the existing `plain_search_<dim>`" and rejected one SQL statement with `LATERAL` over the collections. Its reasons, quoted from part 1 Q2, with whether each still holds below. |
| nexus-tu8wp.6 (router) | Origin | Decides exact or HNSW per statement. This RDR keeps the decision per collection. |
| RDR-192 | Constraint | live(c) must stay the inlined `EXISTS (... chunk_live_owners(...))` form, never a view join. The new statement carries it verbatim. |
| RDR-217 | Adjacent (closed) | The client's lexical leg calls `/v1/vectors/hybrid-search`, not this route. Out of scope. |

The nexus-tu8wp design's reasons against one statement, and their status now:

- **R1.** "The collection is a per-row outer parameter inside a LATERAL, so the
  planner makes ONE plan choice for all arms and cannot see per-collection
  selectivity; that is precisely the nexus-6nkn3 failure." *Still holds for
  HNSW plans.* It does not apply to exact plans: with index scans off, the
  right plan for a small collection is a primary-key-prefix bitmap scan or a
  leaf scan, whatever its size. This design puts only exact-routed collections
  in a shared statement.
- **R2.** "exactOnUnderReturn is per statement; a starved small arm inside one
  statement returns zero silently (the nexus-bq06h class)." *Still holds for
  HNSW.* An exact statement is complete and has no starved case. HNSW-routed
  collections keep their own statement and their own re-run.
- **R3.** "A LATERAL HNSW arm needs enable_seqscan/enable_sort pins to bind the
  index at all." *Does not apply*: the shared statement never uses HNSW.
- **R4.** "Arms run serially in one backend: ~92 arms against one 30 s statement
  bound." *Holds, and it is the real cost of collapsing.* This design keeps
  parallelism by splitting the exact work into bins (§ Technical Design) and
  bounds each bin's work by the router's own threshold.
- **R5.** "A new SQL function is a changeset carrying live(c): new inlining
  EXPLAIN evidence, PITR-fork walk." *Holds.* This design accepts the cost
  (§ Release).

## Context

### Background

T2 `nexus/search-latency-root-cause-2026-10-08` traced the cloud search time
first to the client (no HTTP keep-alive, a get-embeddings fan-out) and to the
stats view. Those were fixed or made opt-in (nexus-mz9jv, nexus-92q1p,
nexus-wym0l; engine-service-v0.1.152). What remains, measured the same day
(M1), is the route's fan-out SQL. M1's own "next levers" list names this RDR's
subject first: "have the engine search many small collections in one
statement".

### Technical Environment

- PostgreSQL 17, pgvector 0.8.2. Cloud: Crunchy Bridge standard-8 (2 vCPU,
  8 GB RAM) at RDR-225's writing; whether it has since moved is not recorded
  here (A4).
- `nexus.chunks` LIST-partitioned by `embedding_model`, then `tenant_id`; one
  HNSW index per vector column per leaf (RDR-225).
- Engine pool `NX_POOL_SIZE` default 10 (`Main.java:83`).
- Router threshold `NX_SEARCH_EXACT_MAX_ROWS`, code default 10000, documented
  as provisional (`PgSession.java:716`, `docs/configuration.md:84`). The value
  The cloud runs the default: `NX_SEARCH_EXACT_MAX_ROWS` is unset and the
  engine boots with `event=search_exact_router max_rows=10000` (A3, verified).

## Research Findings

### Investigation

Read: the route handler, the repository fan-out, the router and the session
settings at `c991266e2`; changesets `vectors-031-1` and `vectors-031-2`; the
client's request sizing; CHANGELOG entries nexus-tu8wp.1, .2, .6 and
nexus-wym0l; RDR-225's findings; T2 entries 29657, 29650, 29624, 28937 and
28938. No new measurement was taken for this draft.

#### What the route does today (code facts)

- **C1. Request validation and shape.** `VectorHandler.handleSearchPerCollection`
  (`VectorHandler.java:652-737`; contract in its Javadoc, `:591-651`). Request:
  `query`, `collections` (one model, at most 256), `per_collection_k` (1 to
  300), `limit` (1 to 1200), optional `thresholds`, `where`,
  `include_source_uri`, `include_embeddings`, `embeddings_limit`, `rerank`,
  `rerank_top_k`.
- **C2. One arm per collection.** `PgVectorRepository.searchPerCollection`
  (`PgVectorRepository.java:2147-2317`) embeds the query once, drops
  unregistered names (`registeredSurvivors`), requires one model
  (`requireHomogeneousModel`), settles dimension errors per collection
  (`:2208-2221`), then runs `runArm` for each runnable collection on
  `min(parallelism, pool, collections)` virtual-thread workers
  (`:2223-2258`).
- **C3. An arm.** `runArm` (`:2442-2460`) takes an arm permit, then calls
  `runPlainSearchStatement` (`:1422-1484`) with a one-element collection array.
  That opens one tenant transaction, sets the serving settings in one
  `set_config` statement (statement timeout, `hnsw.iterative_scan`,
  `hnsw.ef_search`, scan budget, `plan_cache_mode`), runs the router probe
  (`probeSelectedRows`, `:1501-1533`), and then either the exact plan
  (`disableIndexScanForExactFallback`, one `selectFrom(fn)`) or the HNSW plan
  wrapped in the empty-result exact re-run (`exactSelectFrom`,
  `exactOnUnderReturn`, `:6587-6603`).
- **C4. Permits.** `fanoutArmPermits`: default `max(1, pool/2)`, ceiling
  `max(1, pool - 2)`; the per-request `fanoutParallelism` default is the same
  half. The default was `pool - 2` in engine-service-v0.1.153 only, and was
  reverted after measurement (§ Decisions, item 4). The gate is shared by every
  request.
- **C5. Merge.** `FanoutMerger` (`:1760-1830`). As each arm finishes, its rows
  are cut by that collection's threshold (`distance > threshold` drops), and
  `raw_count`, `dropped`, `min_raw_distance` and `min_dropped_distance` are
  computed over the arm's full row set. Survivors go into a heap that keeps the
  best `limit` by `(distance, id, collection)`. Enrichment (`enrichSearchRows`)
  and the optional vector fill (`attachEmbeddings`, `:2341-2377`) run once
  after the merge. Rerank runs once in the handler over the merged rows.
- **C6. Round trips.** Since nexus-wym0l an arm makes 4 round trips instead of
  13 (CHANGELOG line 22): the tenant and serving settings in one statement, the
  exact route's four planner settings in another, plus the probe and the
  search.
- **C7. Time bounds and failure.** Each statement's bound is
  `min(search bound, fan-out budget left, request budget left)` (`armBound`,
  `:1962-1993`). A statement timeout at the search bound, the fan-out budget,
  and a dimension problem are isolated to the collection with a stable
  `error_kind`; a timeout at the request budget, pool or admission exhaustion,
  and other transient SQL failures fail the whole request with 503
  (`settleArmFailure`, `:2394-2420`; `fanoutFailure`, `:2512-2536`).
- **C8. The statement.** `plain_search_1024` (`vectors-031-read-path-model-tenant-predicates.xml:196-224`)
  is `LANGUAGE sql STABLE SECURITY INVOKER`, so it inlines. It filters by
  model, tenant, `collection = ANY(p_collections)`, non-null vector, live(c),
  and the optional metadata predicates, then `ORDER BY distance,
  encode(chash,'hex') LIMIT p_n`. The 384 and 768 forms are the same.
- **C9. Client sizing.** `_per_collection_request_sizes`
  (`src/nexus/search_engine.py:638-670`): `per_collection_k = max(5, n_results *
  mult)` capped at 300, where `mult` is the group's largest overfetch
  multiplier (`_overfetch_multiplier`, `:474-483`); `limit = max(300, 4n)`
  capped at 1200, or 1000 with rerank. Only finite thresholds are sent
  (`:1417-1424`; `http_vector_client.py:3681-3686`).
- **C10. Lexical and hybrid legs do not use this route.** "Lexical/hybrid search
  stays on `/hybrid-search`" (`VectorHandler.java:650`); the client's lexical
  leg calls `hybrid_search` per batch (`search_engine.py:1511-1553`).
- **C11. Exact and HNSW settings.** The exact plan turns index scans off and
  bitmap scans, sequential scans and sorts on (`PgSession.java:590-598`). The
  HNSW plan runs with `ef_search` floor 600 (`:91`), `max_scan_tuples` 200000
  (`:117`) and a fixed scan memory budget derived from `work_mem`, which the
  code comment records as 384 MB on the managed cloud (`:128-135`).

#### The response contract (what must not change)

Status 200, body:

```text
{ "results":          [row, ...]          // at most limit, best first by (distance, id, collection)
  "per_collection":   [ {collection, raw_count, dropped, min_raw_distance,
                         min_dropped_distance, error, error_kind}, ... ]   // one per surviving collection, request order
  "per_collection_k": <echo>, "limit": <echo>,
  // with rerank: the RerankStage fields; with include_embeddings: embedding_encoding, embedding_dim }
headers: X-Nexus-Usage-Tokens, X-Nexus-Skipped-Collections
```

A row is the `/search` row shape: `id`, `content`, `distance`, `collection`,
`retention`, the stored metadata flattened in, plus the enrichment fields
(`chash`, span, and `source_uri` when asked) and, when asked, `embedding_b64`.
`error_kind` is one of `dimension_mismatch`, `unsupported_dimension`,
`statement_timeout`, `fanout_budget_exhausted`. The client reads this envelope
in `search_engine.py:1462-1509`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| PostgreSQL window functions | Docs (PG 17 tutorial and function reference) | `row_number()` numbers rows within a partition; filtering on it needs an outer query. The executor's early stop for `row_number() <= N` ("run condition", added in PostgreSQL 15) is not described in those pages and is Assumed (A2). |
| PostgreSQL SQL-function inlining | Source of `vectors-031` header and its tests | The ten families inline because they are single-SELECT `LANGUAGE sql STABLE SECURITY INVOKER` functions; a SECURITY DEFINER SQL function does not inline. Whether a body with a window subquery and a self-join still inlines is Assumed (A1). |
| pgvector 0.8.2 | Not re-searched | The exact plan's cost is detoasting and scoring every selected vector (M3). |

### Key Discoveries

Measurements, each stated once here and cited by label elsewhere:

- **M1 (Verified, cloud, T2 [29657]).** Default search = 89 arms over two
  requests (61 knowledge, 28 code/docs/rdr). Handler 441 to 1194 ms, almost all
  fan-out SQL. 61-arm requests about 1.2 s even at 5 rows returned; 28-arm
  requests 0.65 to 1.0 s; 30-arm `query` requests 0.44 s. Query embed 100 to
  113 ms. Permits 5 (default), ceiling 8.
- **M2 (Verified, cloud, T2 [29650]).** Before the default-off change, one
  28-arm request carried 233 rows and one arm took 3654 ms once with no
  statement timeout; it did not recur.
- **M3 (Verified, local, T2 [29624]).** On loopback the route took 5 to 18 ms
  for one collection and about 100 ms for 24. Before nexus-wym0l an arm was
  about 14 serial round trips, 131 of a 13-collection request's 183 statements
  being `set_config`. Exact-routed arms detoast every vector: a 2500-row arm
  read 24.6k buffers, 25 ms hot.
- **M4 (Verified, PITR fork, RDR-225 research-7).** Exact search over all 98 of
  the main tenant's collections on standard-8: 17.7 s serial cold, 4.3 s warm.
  No plan read the whole heap.
- **M5 (Verified, fork, RDR-225 F4).** A 5.6k-row collection: exact 67 to 74 ms;
  HNSW timed out at 30 s cold.
- **M6 (Verified, fork, RDR-225 F2).** HNSW agreement with exact search on
  single-collection filters: 0.60 to 0.80.
- **M7 (Verified, fork, RDR-225 F3).** On standard-8 one pass over the 98
  collections read 3.3 to 4 GB even warm; memory-16 read none.
- **M8 (Verified, local, RDR-225 TS2).** Planning a search family pruned to one
  leaf: largest p95 1.64 ms at 300 tenants.

Inference from them:

- **I1 (Inferred).** In the 61-arm request each arm holds a permit for about
  1.2 s x 5 / 61, roughly 100 ms, if the permits stay full and the arms are of
  similar size. M3's loopback figures and M8's plan times are an order of
  magnitude below that.
- **I2 (Inferred, decisive and unmeasured).** M1's "cost per arm, not per row"
  is about rows RETURNED. Rows SCANNED are a different matter: an exact arm
  reads and detoasts its whole collection (M3), and the whole tenant all-exact
  costs 4.3 s warm serially (M4). So the 100 ms of I1 is some mix of a fixed
  per-arm cost (round trips through the pooler, plan, probe, permit and
  connection handoff) and per-row cost (reading and scoring every vector,
  possibly from disk on standard-8, M7). The split is not known. It decides
  the design: collapsing arms removes the fixed part, but one serial statement
  also removes the parallelism the per-row part currently enjoys. If per-row
  cost dominates, a single statement is slower than five parallel arms.

#### Registered findings

- **✅ Verified** (source search). The route's response contract is C1, C5 and
  C7, and the client reads only those fields (`search_engine.py:1462-1509`).
- **✅ Verified** (source search). Lexical and hybrid legs bypass the route (C10).
- **✅ Verified** (source search). Thresholds, raw counts and the two minimum
  distances are computed in Java per collection over the collection's full
  top-K (C5).
- **✅ Verified** (source search). The router decides per statement, and an arm's
  statement selects one collection, so today each collection is routed by its
  own row count (C3, `docs/architecture.md:681`).
- **✅ Verified** (source search). A grouped `row_number() OVER (PARTITION BY
  collection)` with `plain_search_<dim>`'s own order key (distance, then hex
  chash) returns each collection's exact top-K by construction, and the join
  back matches the chunks primary key (226-research-5; plan audit round 1).
- **✅ Verified** (measurement). M1: cost follows arms, not rows returned
  (226-research-6).
- **✅ Verified** (measurement). A3: T = 10000; 7 of 116 collections are HNSW,
  109 are groupable (226-research-7).
- **⚠️ Documented** (docs only). A2: the run condition does not stop the window
  sort early (226-research-8).
- **❓ Assumed** (A1, A4, A5 below; 226-research-9 to -11). Inlining of the new
  function, the cloud instance size, and the per-arm cost split (I2).

### Critical Assumptions

- [ ] **A1.** A `LANGUAGE sql STABLE SECURITY INVOKER` function whose body is a
  window subquery joined back to `nexus.chunks` inlines into the caller, so the
  literal model and tenant prune to one leaf at plan time — **Status**:
  Unverified — **Method**: Spike (EXPLAIN pin, as `ReadPathLeafPruningIntegrationTest`
  does for the existing families).
- [x] **A2.** Whether PostgreSQL 17 stops numbering a collection's rows past K
  under `PARTITION BY collection` — **Status**: Documented, answered no
  (226-research-8). On PostgreSQL 15 and later the run condition puts WindowAgg
  into pass-through mode, so the window sort still pays the bin's full row
  count. Correctness does not depend on it. Phase 1 pins that the run
  condition is present and Phase 0's cost model does not credit an early stop.
- [x] **A3.** The production router threshold and the collection row counts —
  **Status**: Verified (conexus, read-only, 2026-10-08 ~14:50Z; T2 conexus
  `nexus-tenant-collection-counts-2026-10-08`) — The threshold is the code
  default, 10000. The corpus is tenant `nexus` (tenant `default` holds no
  chunks): 116 collections, 319,121 physical rows (owned and unowned, as the
  router counts them), 83 voyage-context-3 and 33 voyage-code-3. By size:

  | rows | collections | rows total |
  |---|---|---|
  | 100 or fewer | 43 | 1,123 |
  | 101 to 1,000 | 41 | 15,598 |
  | 1,001 to 5,000 | 17 | 45,259 |
  | 5,001 to 10,000 | 8 | 55,236 |
  | 10,001 to 60,000 | 7 | 201,905 |

  At T = 10000, 7 collections stay on their own HNSW arm (code__1-1 58,436,
  code__1-2 45,525, code__1-20 29,355, code__1-72 27,893, code__1-3 16,088,
  rdr__1-79 14,300, docs__1-2 10,308) and 109 collections holding about
  117,000 rows are exact and groupable. At T = 60000 none would be HNSW. Nine of
  the 116 are `quarantine-*` collections; whether the default search reaches
  them is checked in Phase 0.
- [ ] **A4.** The cloud instance class (standard-8 or larger). M7 shows the
  buffer pool on standard-8 cannot hold the tenant's vectors, which makes
  per-row cost I/O-bound — **Status**: Unverified — **Method**: conexus.
- [ ] **A5.** The per-arm cost split of I2 — **Status**: Unverified —
  **Method**: Spike (Phase 0).

## Proposed Solution

### Approach

Keep each collection's routing decision exactly as today, and change only how
the exact-routed collections are executed.

1. **Probe once per request.** One statement counts, for every runnable
   collection, its physical rows in the leaf, bounded at T + 1 each.
2. **Large collections keep their arm.** A collection above T is searched by
   today's arm: its own `plain_search_<dim>` HNSW statement, its own
   empty-result exact re-run, its own timeout. Nothing about it changes except
   that its in-transaction probe is skipped, since step 1 already decided.
3. **Small collections share exact statements.** Collections at or below T go
   into bins. Each bin is one exact statement that returns every member's own
   top-K, using `row_number() OVER (PARTITION BY collection ORDER BY distance,
   id)`. With few small rows there is one bin, which is one statement for the
   whole leaf; with many there are up to as many bins as arm permits, run in
   parallel.
4. **Merge as today.** Each bin's rows are handed to the existing merger one
   collection at a time, so thresholds, statistics, the global cut, enrichment,
   the vector fill and rerank are untouched.

With the router off (T = 0) every collection is large and the route behaves
exactly as today. An engine setting, `NX_SEARCH_GROUPED_EXACT` (default on),
turns step 3 off without a redeploy.

### Technical Design

**Goal: an unchanged response.** The route returns the contract in § Research
Findings, field for field, and the client is not changed. For every
exact-routed collection the rows are identical by construction: the same
predicates, the same distance expression, the same tie order (distance, then
hex chash) as the single-collection statement, and an exact search has one
correct answer. HNSW-routed collections run today's statement. Fields that can
change, and how:

- `per_collection[].error` and `error_kind` for a collection in a bin whose
  statement timed out. The values stay in the existing set. The bin's members
  are re-run one by one as single-collection exact arms while the fan-out
  budget remains, so a member is marked only when its own re-run also fails or
  the budget runs out (see Failure Modes). A timed-out bin costs its members
  extra latency, not results, unless the budget is gone.
- The engine's log lines. `event=search_per_collection` keeps `arms` as the
  number of collections searched and gains `statements`, `bins`,
  `grouped_collections`, `hnsw_arms` and `probe_ms`.
  `event=vector_search_statement_slow` gains the route label `grouped`.
  conexus's CloudWatch queries read these lines, so the change is announced to
  them before deploy (Phase 3).
- Diagnostic counters (`routedExactCount`, `exactFallbackCount`) count
  statements, so their totals drop. A new counter counts grouped statements and
  the collections they served. None of these is on the wire.

**Step 1, the probe.** One statement, run on the request thread in one tenant
transaction before any arm or bin borrows a connection: for each collection in
`unnest(collections)`, a LATERAL bounded count `SELECT count(*) FROM (SELECT 1
FROM chunks WHERE embedding_model = m AND tenant_id = t AND collection = c
LIMIT T + 1)`. It reads at most `min(rows, T + 1)` primary-key entries per
collection, the same total as today's per-arm probes. It is built with the jOOQ
DSL next to `probeSelectedRowsQuery`, and carries the same COUPLING note: its
predicate must select the rows the search function selects. Probe and search
are no longer one transaction. A write between them can move a collection
across T; that changes only which plan it gets, never its rows.

**Step 2, routing.** A collection with count above T is large. The rest are
small, including collections with count 0, which still need a statistics entry.

**Step 3, binning.** Let S be the summed counts of the small collections and P
the effective arm permits. The number of bins is
`B = clamp(ceil(S / T), 1, P)`. Collections are assigned largest first to the
lightest bin, ties in request order, so the assignment is deterministic. T is
the router's own unit for "an exact scan this size is cheap", so a bin scans at
most about T rows unless the permit cap binds; then each bin scans about S / P.
If Phase 0 shows T is the wrong unit for a bin, a separate setting is added;
none is added up front.

**Step 4, the grouped statement.** A new function family,
`plain_search_grouped_<dim>(p_query, p_collections, p_where, p_where_path, p_k,
p_embedding_model, p_tenant)` at 384, 768 and 1024, returning the same columns
as `plain_search_<dim>` (`id, content, collection, distance, metadata,
retention`). Shape:

```text
-- Illustrative. Inner: narrow rows, ranked per collection, predicates copied verbatim from plain_search_<dim>.
SELECT encode(r.chash,'hex') AS id, k.chunk_text, r.collection, r.distance, k.metadata, k.retention
  FROM (SELECT c.collection, c.chash, (c.embedding_1024 <=> p_query)::float8 AS distance,
               row_number() OVER (PARTITION BY c.collection
                                  ORDER BY c.embedding_1024 <=> p_query, encode(c.chash,'hex')) AS rn
          FROM nexus.chunks c
         WHERE <model, tenant, collection = ANY, vector not null, live(c), where, where_path>) r
  JOIN nexus.chunks k ON (k.embedding_model, k.tenant_id, k.collection, k.chash)
                       = (p_embedding_model, p_tenant, r.collection, r.chash)
 WHERE r.rn <= p_k
 ORDER BY r.collection, r.distance, id
```

- The inner query sorts narrow rows (collection, chash, distance), about 100
  bytes each, so the window sort stays small. Only the at most K rows per
  collection that survive are joined back for their text and metadata, by
  primary key, from pages the inner scan just read.
- It is `LANGUAGE sql STABLE SECURITY INVOKER`, like `plain_search_<dim>`, so
  it inlines (A1), the engine's `force_custom_plan` makes the model and tenant
  literals, and the planner prunes to one leaf.
- It runs under the exact settings (index scans off), so the plan is a bitmap
  scan on the leaf's primary-key prefix, or a scan of the leaf when the bin is a
  large share of it, never the HNSW index.
- live(c) stays the inlined `EXISTS` form RDR-192 requires.
- It is a SQL function, not a jOOQ query in Java, because every vector-ranked
  statement in the engine is one, the existing EXPLAIN pins and live(c) evidence
  attach to functions, and the predicate then sits beside `plain_search_<dim>`
  in one changeset where a reviewer sees both. The alternative is Open
  Question 3.

**Step 5, running a bin.** A bin is an arm with a different statement: it takes
an arm permit, opens a tenant transaction, sets the same serving settings
(the `HnswServingGucParityTest` pairing holds at this call site too), turns
index scans off, and runs the grouped statement with the bound `armBound`
gives. It reads the result with a lazy cursor. Rows arrive ordered by
collection, so when a collection's last row is read its rows go to
`FanoutMerger.accept` with that collection's threshold, exactly as an arm's
rows do now. Members with no rows get `accept` with an empty list after the
cursor ends. Java holds one collection's rows at a time plus the merge heap,
which keeps today's memory bound (`workers x per_collection_k` rows in flight).

**What each guarantee becomes:**

| Guarantee | Today | This design |
| --- | --- | --- |
| (a) Own top-K, no crowd-out | One statement per collection, `LIMIT k` | Small: `row_number() <= k` per collection, exact. Large: unchanged |
| (b) Per-collection threshold and stats | Java merger per arm (C5) | Unchanged: the merger is fed per collection |
| (c) Router: exact when few rows, HNSW otherwise | Per arm, its own count | Per collection, its own count, from the request probe. Each exact statement scans at most about T rows unless the permit cap binds |
| (d) Overfetch multiplier | Client sets `per_collection_k` (C9) | Unchanged |
| (d) Server rerank | Once over merged rows, in the handler | Unchanged |
| (d) Lexical and hybrid legs | Not on this route (C10) | Not on this route |
| Empty-result exact re-run (nexus-bq06h) | HNSW arms | HNSW arms. Exact statements are complete and need none |
| Failure isolation | Per collection | Per collection for HNSW arms; per bin for small collections (Failure Modes) |

**Statements per request.** Today: one probe and one search per collection. In
this design: one probe, B bins and one statement per large collection. For M1's
61-collection request with no large collection and S at most T, that is 2
statements.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Request probe | `probeSelectedRowsQuery` (`PgVectorRepository.java:1522`) | Extend: a per-collection form beside it; the single-collection form stays for `/search` |
| Bin execution | `runArm`, `runPlainSearchStatement` | Extend: the same transaction and settings with a different statement |
| Grouped statement | `plain_search_<dim>` (`vectors-031-1`) | New sibling function family, same predicates |
| Merge, thresholds, stats | `FanoutMerger` | Reuse unchanged |
| Permits, budgets, failure mapping | `fanoutArmPermits`, `armBound`, `settleArmFailure`, `acquireArmSlot` | Reuse; a timed-out bin re-runs its members as ordinary arms, each settled by `settleArmFailure` |
| Kill switch | none | New engine setting `NX_SEARCH_GROUPED_EXACT` |

### Decision Rationale

The grouped exact statement removes the fixed per-arm cost for the collections
that make up most of a default search, without putting more than one collection
into any HNSW walk, so the recall argument that created the per-collection
route is untouched. Binning by the router's threshold keeps parallelism when the
exact work is large (I2), so the design does not bet on the unmeasured split:
when the small collections are few rows it is one statement, and when they are
many rows it is at most P statements in parallel, never worse in statement
count than today. Exact results are unique, so equivalence with today's route
holds by construction for every grouped collection and can be checked exactly.

## Alternatives Considered

### Alternative 1: One HNSW walk with iterative scan over `collection = ANY(...)`

**Description**: One HNSW statement over all the request's collections in the
leaf, with a window or a client-side split to recover per-collection lists.

**Pros**: One statement and one graph walk per request.

**Cons**: The walk orders by global distance under one `LIMIT` and one
`max_scan_tuples` budget, so a small or distant collection is reached last or
not at all. This is the crowd-out the route was built to end (CHANGELOG
nexus-tu8wp.1, "a dense collection can no longer crowd a small one out of a
flat `LIMIT`"), and the nexus-atylb split drift the client floor existed to
paper over (`_desired_candidate_count` docstring). Filtered HNSW already agrees
with exact search only 0.60 to 0.80 on one collection (M6); widening the filter
to many collections adds the crowd-out on top.

**Reason for rejection**: It gives up guarantee (a), the reason the route exists.

### Alternative 2: `LATERAL` per collection inside one statement

**Description**: `unnest(collections) AS c CROSS JOIN LATERAL (SELECT ... WHERE
collection = c ORDER BY distance LIMIT k)`.

**Pros**: One round trip set per request. The subquery is planned once and run
per collection, so per-arm planning goes away too. For exact-routed collections
its plan is fine (a top-K sort per collection), and it reads the same rows as
the window form.

**Cons**: One plan for every collection (R1), so it cannot mix HNSW and exact
collections; a starved HNSW collection inside it returns nothing silently (R2);
binding HNSW inside a LATERAL needs planner pins (R3); and it runs serially
(R4). Restricted to exact-routed collections it is a valid variant of this
design's Step 4, with no advantage over the window form except a per-collection
top-K sort instead of one sort of narrow rows.

**Reason for rejection**: For HNSW it fails R1 to R3. For exact it is kept as the
fallback form of Step 4 if Phase 1's EXPLAIN shows the window form spilling or
choosing a poor scan (Risks).

### Alternative 3: Raise the permits

**Description**: Set `NX_SEARCH_FANOUT_ARM_PERMITS` to its ceiling of 8.

**Pros**: A configuration change, no code.

**Cons**: At best 8/5 on the parallel term, cost still linear in collections,
and three more connections held by searches out of a pool of 10. M1 asks that
the pool size not be raised before conexus re-measures.

**Reason for rejection**: Not a design. Usable as an interim lever before this
ships; adopted as the interim lever (Sam, 2026-10-08, § Decisions).

### Briefly Rejected

- **Group small collections into one exact statement, keep arms for large
  ones, with a single bin always.** This is the design with B fixed at 1. It
  is rejected only in its fixed form: when the small collections hold many
  rows, one serial statement loses the parallelism the arms had (I2, M4).
- **Apply the thresholds in SQL.** The statistics contract needs the dropped
  rows (`dropped`, `min_dropped_distance`, C5). SQL would have to compute them
  with window aggregates for no saving, since at most K rows per collection
  cross the wire either way.
- **Partition per collection.** Rejected in RDR-225 (Alternative 4) for leaf
  count.

## Trade-offs

### Consequences

- Positive: statements per request fall from about twice the number of
  collections to `1 + B + L` (L large collections), and the fixed per-arm cost
  with them.
- Positive: exact-routed results are unchanged, and checkable exactly.
- Negative: a timed-out bin costs its members a second, per-collection pass,
  so a bad bin can be slower than today's arms for that request; members are
  marked failed only when the re-run also fails or the budget runs out.
- Negative: a new function family, so a changeset and a PITR-fork walk
  rehearsal before deploy (§ Release).
- Negative: two places now encode "which rows a search selects" for the
  grouped path (the probe and the function), as the router already does for
  the single path.

### Risks and Mitigations

- **Risk: the planner handles the window badly over a partitioned leaf.**
  Literal model and tenant prune to one leaf at plan time, as for every family
  since `vectors-031-1` (M8), so the window sees one table. The remaining risks
  are that the function does not inline (A1), so pruning moves to executor
  start-up and plan time grows with the leaf count, and that the inner scan
  becomes a scan of the whole leaf when a bin covers a large share of it.
  **Mitigation**: an EXPLAIN pin per dimension: one leaf, no "Subplans
  Removed", no HNSW index, a WindowAgg with its run condition (A2), the
  primary-key join back. A leaf scan at a large share is legitimate and is
  bounded by T rows per bin.
- **Risk: HNSW plus filter recall when many collections share one walk.** This
  is why nexus-tu8wp moved to per-collection arms: a filtered walk with one
  `LIMIT` lets a dense collection crowd out a small one, and even a
  single-collection filtered walk agrees with exact search only 0.60 to 0.80
  (M6). **Mitigation, by construction**: no HNSW walk in this design covers
  more than one collection. Collections that share a statement share an exact
  scan, which has no recall question. The HNSW statements are byte-identical to
  today's.
- **Risk: `statement_timeout` is per statement, and a statement now covers
  several collections.** A bin's bound is `armBound`'s, as an arm's is. Its work
  is bounded: about T rows when the permit cap does not bind. M5 puts a
  5.6k-row exact scan at about 70 ms; M4 puts the whole tenant at 4.3 s warm and
  17.7 s cold serially, so even the capped case (S / P rows per bin) is well
  under 30 s on the measured data. **Mitigation**: the slow-statement line
  names the bin's collections, and its members are re-run one by one while the
  request's fan-out budget remains (Sam, 2026-10-08). Only a member whose own
  re-run fails, or that the budget never reaches, is reported with an
  `error_kind`.
- **Risk: `work_mem` for the window sort.** The sort is per bin over narrow
  rows: about 100 bytes times the bin's rows, so about 1 MB at T = 10000. Local
  installs run 4 MB `work_mem`, the cloud 384 MB (C11). Concurrent bins each
  hold their own. A sort past `work_mem` spills to a temporary file and stays
  correct. **Mitigation**: the EXPLAIN pin records sort method and memory at
  T rows; if it spills locally, the LATERAL form (top-K sort per collection,
  Alternative 2) replaces the window.
- **Risk: Java memory.** Without care a bin returns every member's K rows at
  once, up to 256 x 300 wide rows. **Mitigation**: the lazy cursor and
  per-collection hand-off (Step 5).
- **Risk: the per-collection probe plans badly on an unvacuumed leaf.** The
  single probe takes a leaf scan on an unvacuumed leaf at 26% share
  (`probeSelectedRowsQuery` Javadoc, `:1511-1520`). Inside a LATERAL the inner
  scan is parameterised by collection, where a full leaf scan per collection
  would be costed as many leaf scans. **Mitigation**: an EXPLAIN pin on a
  vacuumed and an unvacuumed leaf; `NX_SEARCH_EXACT_MAX_ROWS=0` turns the
  probe and the grouping off.

### Failure Modes

- **A bin times out at the search bound.** Its members are re-run one by one,
  each as today's single-collection exact arm, while the request's fan-out
  budget remains (Sam, 2026-10-08). A member that still cannot run gets
  `statement_timeout`, or `fanout_budget_exhausted` when the budget ran out
  first. Visible in `per_collection[]` and the slow-statement log with route
  `grouped`. The client reports them in `failed_collections` as today.
- **A bin runs out of fan-out budget.** Each member gets
  `fanout_budget_exhausted`.
- **A bin fails transiently, or the request budget expires.** The whole request
  fails with 503, as an arm's failure does today (C7).
- **The probe fails.** The whole request fails with the existing typed mapping;
  there is no partial result, as today when an arm's probe fails.
- **A grouped statement returns a wrong row set.** Silent unless tested. Guarded
  by the equivalence tests (Test Plan) and the fork comparison; turned off by
  `NX_SEARCH_GROUPED_EXACT=0` without a redeploy.
- **The function is missing (an engine started against a database whose walk
  did not run it).** The engine's Liquibase walk runs before it serves, so this
  needs a failed walk, which stops the boot.

## Implementation Plan

### Prerequisites

- [ ] A1 to A5 verified: A1 and A2 by Phase 1's EXPLAIN pins, A3 and A4 by
  conexus, A5 by Phase 0.
- [x] Sam's answers to the questions that change behaviour (§ Decisions).

### Minimum Viable Validation

On a PITR fork of production, two engines built from the same commit run
against the same fork, one with `NX_SEARCH_GROUPED_EXACT=1` and one with `=0`.
Replaying M1's two request shapes and the logged queries RDR-225's rehearsal
used, every response is equivalent (§ Evaluation) and the 61-collection
request's engine time falls. conexus runs it; the fork is their instrument
(`deploy/RESTORE.md`).

### Phase 0: Measure the split (decides the default, may stop the work)

- **Step 0.1, local.** On a production-shaped substrate (the T2 [29624] method:
  a dedicated PG 17 container, the product schema, 61 knowledge-shaped and 28
  code-shaped collections at sizes taken from A3), time per collection: probe,
  statement, rows scanned, buffers. Then time one grouped statement over the 61
  against the sum and against five parallel arms.
- **Step 0.2, fork (conexus).** The row counts of M1's collections, the cloud's
  T (A3), and `EXPLAIN (ANALYZE, BUFFERS)` of the grouped statement over the
  61-collection set, warm and cold, next to the serial sum of the
  single-collection statements.
- **Exit.** Numbers written to T2. If the grouped statement over the 61 set,
  warm, is not below half of M1's 61-arm handler time, stop and report: the
  fixed per-arm cost is not where the time goes, and the lever is elsewhere
  (I/O, instance size).

### Phase 1: Schema

#### Step 1: Tests first

- `GroupedSearchEquivalenceIntegrationTest`: for each dimension, seeded
  collections of mixed sizes, ties included; for each collection,
  `plain_search_grouped_<dim>` rows equal `plain_search_<dim>(q, [c], k)` under
  the exact settings, same ids, same order, same distances. Fails with the
  `PARTITION BY` removed (the crowd-out control: one dense collection, one
  small).
- `GroupedSearchPlanShapeIntegrationTest`: one leaf, no "Subplans Removed", no
  HNSW index, the run condition, the primary-key join back, sort method and
  memory at T rows.
- `GroupedSearchLiveCIntegrationTest`: the RDR-192 matrix (manifest-less,
  tombstoned owner, shared chunk), as `nexus_svc` without BYPASSRLS; a second
  tenant's same-named collection never contributes.

#### Step 2: Changeset

`vectors-033-1` (the next free number; `vectors-032` shipped in
engine-service-v0.1.152): the three functions, grants to `nexus_svc`, a
post-condition that each name has one signature, rollback by DROP, no DATA
EFFECT. `JooqRecordReflectionFeatureTest`'s expected record count moves in the
same change (a `RETURNS TABLE` function adds a jOOQ record).

### Phase 2: Engine

#### Step 1: Tests first

- `PgVectorSearchPerCollectionGroupedIntegrationTest`: the route with grouping
  on against grouping off over the same seeded set returns identical envelopes
  (rows, order, every `per_collection` field). Mixed small and large
  collections. Zero-row collections get their entry.
- Statement count: a 61-collection request with all collections small issues
  one probe and B grouped statements, counted at the database
  (`pg_stat_statements` or a statement listener), and fails if an arm per
  collection runs.
- Bins: the assignment for fixed counts and permits is deterministic, and B
  follows `clamp(ceil(S / T), 1, P)`.
- Failure, bin timeout (Sam's decision 2), three cases:
  (a) a SEARCH-limited bin timeout with budget left re-runs every member as a
  single-collection exact arm and returns their rows;
  (b) a member whose own re-run also times out gets `statement_timeout`;
  (c) a budget that runs out before or during the re-run gives the members not
  yet run `fanout_budget_exhausted`.
  The re-run's permit discipline is pinned: the bin releases its permit before
  its members take their own, each through `acquireArmSlot`, so bin and member
  holders never exceed P together.
- Failure, other: a transient failure in a bin fails the request with 503; the
  request-budget case fails with the deadline shape.
- Concurrency: simultaneous bin and arm holders never exceed P (counted, not
  inferred from exit codes).
- Switches: `NX_SEARCH_EXACT_MAX_ROWS=0` and `NX_SEARCH_GROUPED_EXACT=0` each
  reproduce today's statement pattern.
- The existing route tests (`PgVectorSearchPerCollectionIntegrationTest`,
  `PgVectorSearchPerCollectionExactFallbackIntegrationTest`,
  `PgVectorCardinalityRouterIntegrationTest`, `Rdr192EngineLivenessMatrixIntegrationTest`,
  `ReadPathLeafPruningIntegrationTest`) pass unchanged.

#### Step 2: Code

The request probe, routing, binning, bin execution with the lazy cursor, the
member re-run on a bin timeout (release the bin's permit, then re-run each
member through `acquireArmSlot` as today's exact arm while the fan-out budget
remains), the merger hand-off, the setting, the log fields and the counter. Then
`scripts/mvnw-leased.sh test` with every `*GateTest` and the `integration`
group. `docs/architecture.md` (cardinality router paragraph) and
`docs/configuration.md` (the new setting) are updated in the same change.

### Phase 3: Release

#### Step 1: Rehearsal and deploy

- The tag carries a changeset, so it gets a PITR-fork walk rehearsal before
  deploy, as every changeset-carrying tag does (AGENTS.md § Engine-service
  release). The walk is additive: one new changeset, no data effect. Its
  expected counts are read from the changelog at cut time.
- On the same fork, the Minimum Viable Validation and § Evaluation.
- Announce the new log fields and the `grouped` route label to conexus before
  deploy.
- Rollback: `NX_SEARCH_GROUPED_EXACT=0` on the running engine, or a tag flip;
  the functions left behind are unused.
- No wire change and no client change, so no wire-ledger entry.
  `REQUIRED_ENGINE_VERSION` moves with the next client release, as for every
  engine tag.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `plain_search_grouped_<dim>` functions | Catalog query | N/A | A later changeset, if the setting is retired off | The post-condition and EXPLAIN pins | Cluster backups |
| `NX_SEARCH_GROUPED_EXACT` | Boot log line | Boot log line | Remove once Phase 3's comparison has run in production for a release | Boot log line | N/A |

### New Dependencies

None.

## Evaluation

Kept proportionate: the design changes execution, not the answer, so the
evaluation checks that the answer did not move and that the time did.

**Result equivalence (fork, conexus's instrument).** Two engines, grouping on
and off, one fork. For each replayed request: identical `results` ids in
identical order; distances equal within 1e-9; identical `per_collection`
entries. A difference in an exact-routed collection is a defect. A difference
in an HNSW-routed collection cannot come from this change (its statement is
unchanged) and is recorded separately as HNSW non-determinism, if any is seen.

**Latency at the measured shapes.** M1's 61-collection and 28-collection
requests, and the 30-collection `query` request, replayed against both engines
in ABBA order (on, off, off, on), warm, at least 20 requests per engine per
shape, plus one cold pass after a restart. Read from the engine's own
`event=search_per_collection_request handler_ms` and
`event=search_per_collection fanout_ms`. Decision rule: ship when the
61-collection median falls and no shape's median rises above 1.10 times the
per-arm path (RDR-225's latency margin).

**Non-goals.**

- The flat `/v1/vectors/search` route and its summed-count router.
- `/v1/vectors/hybrid-search` and the client's lexical leg (RDR-217).
- The combined-query families (`search_metadata_scoped`, `_aspect_scoped`,
  `search_graph_hop`, `search_topic_scoped`).
- The HNSW-routed statements, the empty-result re-run, and HNSW settings.
- The router threshold's value, the pool size and the permit defaults.
- The client: request sizing, thresholds policy, grouping by model, fallback.
- The `vector_stats` cost on the search path and pruning tiny collections
  before the request (M1's other levers).
- Any recall claim. The design preserves results; it does not improve them.

## Test Plan

- **Scenario**: a dense collection and a small one in one bin — **Verify**: the
  small one returns its own top-K (crowd-out control).
- **Scenario**: ties at the K boundary — **Verify**: the same rows as the
  single-collection statement (order by distance, then hex chash).
- **Scenario**: grouping on vs off over mixed small and large collections —
  **Verify**: identical envelopes.
- **Scenario**: a collection with zero rows, and one with only hidden (not
  live) rows — **Verify**: present in `per_collection` with `raw_count` 0.
- **Scenario**: a threshold that drops some of a bin member's rows —
  **Verify**: `dropped` and `min_dropped_distance` as today.
- **Scenario**: a second tenant with same-named collections — **Verify**: never
  contributes, as `nexus_svc` without BYPASSRLS.
- **Scenario**: a bin past its statement bound; past the fan-out budget; a
  transient failure; the request budget — **Verify**: the per-member and
  whole-request outcomes in Failure Modes.
- **Scenario**: router off, and grouping off — **Verify**: today's statement
  pattern.
- **Scenario**: EXPLAIN of the grouped function and the request probe, on a
  vacuumed and an unvacuumed leaf — **Verify**: one leaf, no HNSW, the run
  condition, an index or index-only scan per collection in the probe.
- **Scenario**: 61 small collections — **Verify**: one probe plus B statements
  at the database.

## Validation

### Testing Strategy

The Test Plan scenarios, Phase 0's measurements, and § Evaluation on the fork.
Done means: equivalence holds on the fork, the decision rule passes, and every
engine test group is green.

### Performance Expectations

None claimed before Phase 0. The design's premise is I1 and I2: if the fixed
per-arm cost is a large share of M1's time, grouping removes it; if per-row
cost dominates, binning keeps today's parallelism and the gain is small, and
Phase 0 says so before any code.

## Finalization Gate

### Contradiction Check

One tension is open and stated rather than resolved: M1 reads "cost per arm,
not per row", while M3 and M4 show exact arms pay per row scanned. I2 explains
the two as different row counts (returned against scanned), and Phase 0 is
there to settle which dominates.

### Assumption Verification

A1 to A5 are unverified. A1 and A2 are verified by Phase 1's EXPLAIN pins
before engine code; A3 and A4 by conexus before Phase 0's fork step; A5 is
Phase 0.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `row_number() OVER (PARTITION BY ... ORDER BY ...)` filtered in an outer query | PostgreSQL 17 | Docs |
| Run condition on `row_number() <= N` under `PARTITION BY` | PostgreSQL 17 | Assumed (A2), Spike planned |
| Inlining a SQL function whose body has a window subquery and a join | PostgreSQL 17 | Assumed (A1), Spike planned |
| jOOQ lazy fetch inside a transaction | jOOQ | Assumed, verified by the Phase 2 memory test |

### Scope Verification

The Minimum Viable Validation is Phase 3 Step 1's fork comparison, in scope
and run before deploy.

### Cross-Cutting Concerns

- **Versioning:** an engine tag with one additive changeset; no wire or client
  change.
- **Build tool compatibility:** jOOQ codegen for the new functions and the
  record-count guard.
- **Licensing:** N/A.
- **Deployment model:** cloud via the PITR walk rehearsal and deploy; local
  installs get it through `REQUIRED_ENGINE_VERSION`.
- **IDE compatibility:** N/A.
- **Incremental adoption:** the setting turns the grouping off per engine.
- **Secret/credential lifecycle:** N/A.
- **Memory management:** the window sort per bin (Risks), the Java lazy cursor
  (Step 5).

### Proportionality

The document is sized to a change of execution strategy on the hottest read
path, with an equivalence argument and one measurement gate. The evaluation is
deliberately smaller than RDR-225's protocol, because exact results can be
compared exactly.

## Decisions and Open Questions

Decided by Sam, 2026-10-08:

1. **Phase 0 is the stop gate as written**: the grouped statement over the 61
   set must be below half of the 61-arm handler time. If it is not, evaluate
   why before deciding anything else.
2. **A bin that times out re-runs its members one by one** while the request's
   fan-out budget remains (§ Failure Modes).
3. **`NX_SEARCH_GROUPED_EXACT` is removed** after one release in production.
4. **Interim lever: raise the arm permits to their ceiling of 8 now.** Sam is
   the only tenant, so the shared-pool concern does not bind. The cloud has no
   deploy knob for it, so the engine's built-in defaults move from half the
   pool to the ceiling, `max(1, pool - 2)` (8 at pool 10), for BOTH
   `NX_SEARCH_FANOUT_ARM_PERMITS` and the per-request
   `NX_SEARCH_FANOUT_CONCURRENCY`; raising only the permits would not speed up a
   lone search, which the per-request limit holds to 5. It ships in the next
   engine tag, ahead of this RDR (Sam's go in conexus's session, T2 conexus
   [29662]). This moves P in the bin formula too.

   **Outcome, measured and reverted (2026-10-08).** engine-service-v0.1.153
   shipped the change. In the cloud the arms then became DB-throughput-bound.
   Effective parallelism rose from 3-4.5 to 4.5-6.3, but mean arm time rose by
   the same factor and sum_arm_ms by 40-90%. The 61-collection call was
   unchanged, the 28-collection call got worse, and only the 30-collection query
   gained about 15% (T2 `nexus/search-fanout-permits-8-result-2026-10-08`
   [29699]). Sam reverted the defaults to half the pool for the next engine tag.
   This bears on the design: under contention, per-row DB work, not per-arm
   overhead, may dominate, which is the question A5 and Phase 0's stop gate
   exist to answer.
5. **The grouped query is a new stored SQL function family**
   (`plain_search_grouped_<dim>`), installed by a Liquibase changeset beside
   the ten families RDR-225 redefined. The engine calls it; the client sends
   no SQL and does not change. The changeset needs conexus's PITR-fork walk
   before deploy.

Answered by conexus:

6. **The production router threshold** is answered (A3): 10000, so 7
   collections keep HNSW arms and 109 are groupable.

## References

- T2 `nexus/search-latency-default-off-2026-10-08` [29657],
  `nexus/search-latency-after-v0152-2026-10-08` [29650],
  `nexus/search-latency-root-cause-2026-10-08` [29624]
- T2 `nexus/design-tu8wp-engine-per-collection-topk-2026-10-04` [28937] and
  `-part2` [28938]
- RDR-225 § Research Findings (F2, F3, F4, research-7, TS2) and § Technical
  Design, Read path
- CHANGELOG.md: nexus-wym0l (line 22), nexus-tu8wp.1, .2, .6 (lines 107-109)
- `service/src/main/java/dev/nexus/service/vectors/PgVectorRepository.java`,
  `service/src/main/java/dev/nexus/service/http/VectorHandler.java`,
  `service/src/main/java/dev/nexus/service/db/PgSession.java`
- `service/src/main/resources/db/changelog/vectors-031-read-path-model-tenant-predicates.xml`
- `src/nexus/search_engine.py`, `src/nexus/db/http_vector_client.py`
- `docs/architecture.md:681`, `docs/configuration.md:84-87`

## Revision History

- 2026-10-08: Created (draft).
