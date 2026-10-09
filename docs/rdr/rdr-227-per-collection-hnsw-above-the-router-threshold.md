---
title: "Per-Collection HNSW Indexes Above the Router Threshold"
id: RDR-227
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-10-09
accepted_date:
related_issues: [nexus-43ulx, nexus-nqsa7, nexus-vpa9q, nexus-tao37]
related_rdrs: [RDR-225, RDR-226]
---

# RDR-227: Per-Collection HNSW Indexes Above the Router Threshold

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Drafted 2026-10-09 against develop `ebd9c4deb`. No product code has changed.

**Terms used here.** A *leaf* is one partition of `nexus.chunks` holding every
collection of one (embedding model, tenant) pair, with one HNSW graph over all
of them (RDR-225). An *arm* is one collection's search inside a
`search-per-collection` request. The *router* sends an arm to an exact scan when
the collection holds at most T rows (`PgSession.searchExactMaxRows`,
`NX_SEARCH_EXACT_MAX_ROWS`), and to the leaf's HNSW index otherwise.

## Problem Statement

A one-collection HNSW search inside a leaf walks a graph built over every
collection in that leaf. A filter keeps only the target collection's rows. For
a large collection beside larger siblings, the walk can stop before it reaches
the collection's true nearest rows. The router's exact path fixes recall but
costs too much for the largest collections. Today neither path gives a large
collection both correct results and HNSW speed.

### Enumerated gaps to close

#### Gap 1: A filtered leaf walk loses a large collection's nearest rows

`code__1-72__voyage-code-3__v1` (27,893 rows) returned best distance 0.34842 at
per-collection k 40 to 100, while its true nearest row (0.30542, page rank 7)
appeared only at k 120 and above (managed cloud, 2026-10-09, deterministic). The
PITR-fork measurement found no plan switch: the leaf walk is filtered to one
collection, the filter discards about 80% of the visited tuples, and
`hnsw.iterative_scan = relaxed_order` stops at the first k admitted rows. Recall
against exact was 0.925 at k 40 and 0.983 at k 120 (T2
`conexus/nqsa7-fork-hnsw-vs-exact-2026-10-09`, `nexus_rdr/226-research-13`).

#### Gap 2: The exact path is too slow for the largest collections under load

nexus-nqsa7 raised the router default to 60000, so every collection of the
tenant became exact. On engine v0.1.155 (2026-10-09 09:37-09:38Z) the large exact
code arms took 1.1 to 2.0 s each under 5-permit concurrency (`code__1-1` 58,588
rows 1.65-2.0 s, `code__1-2` 45,525 rows 1.28-1.56 s, `code__1-20` 29,355 rows
1.1-1.2 s), against 0.3 to 0.6 s alone on the fork. They held the shared permits,
so the knowledge request waited 6.5-7.2 s for permits in total, and a warm default
search got about 1 s slower (2.64-3.31 s to 3.89-4.26 s on the same 7.75.0
client). The interim is the router default T = 30000 in engine-service-v0.1.156
(Sam, 2026-10-09), which returns the two largest collections to the lossy leaf
walk.

#### Gap 3: A cold HNSW index is slow on first touch

On a fresh fork, the first HNSW search of a large collection took 2.2 to 3.6 s
(5.4 to 7.2 s on three arms in the RDR-226 Step 0.2 run), while exact never
exceeded 1.1 s cold. A restarted or failed-over database pays this. Any design
that leans on HNSW for large collections must state its cold cost. Phase 0
measured a per-collection index's first touch after a database restart at 86 to
189 ms (OS page cache intact, so a lower bound); a first touch read from disk
should be of the same order as the leaf index's 2 to 7 s.

## Relationship to Prior RDRs

- **RDR-225** put every collection of a (model, tenant) pair into one leaf with
  one HNSW graph. That is what makes the walk filtered (Gap 1). This RDR keeps
  the leaf and adds, for large collections only, an index scoped to one
  collection.
- **RDR-226** groups the small, exact collections of a leaf into one statement.
  Its Phase 0 found that a grouped statement does no less database work and that
  most arm time in production is outside the statement
  (`nexus_rdr/226-research-14`). It does not address large collections; this RDR
  does. The two share the router threshold T as their boundary.

## Context

### Background

The router (nexus-tu8wp.6) exists because a filtered HNSW walk is both slower and
less complete than an exact scan for small collections. T was 10000 by default
until nexus-nqsa7 raised it to 60000 for recall. The 2026-10-09 measurements show
T is a recall lever and a cost lever at once, and that no single value of T gets
both right for this tenant's 7 collections between 10k and 60k rows.

### Technical Environment

- PostgreSQL 17 with pgvector 0.8.2; HNSW with `vector_cosine_ops`.
- Serving settings per arm: `hnsw.iterative_scan = relaxed_order`,
  `hnsw.ef_search = max(600, k)` (max 1000), `hnsw.max_scan_tuples = 200000`,
  `plan_cache_mode = force_custom_plan`.
- `plain_search_<dim>` is a `LANGUAGE sql STABLE` function the planner inlines;
  the arm passes a one-element `p_collections`.
- The managed instance's buffer pool cannot hold the tenant's vectors (RDR-226
  M7), so exact cost is per row scanned and I/O-sensitive.

## Research Findings

### Investigation

Measured 2026-10-09, managed cloud and a PITR fork of production. Sources: T2
`conexus/nqsa7-fork-hnsw-vs-exact-2026-10-09` [29744],
`conexus/rdr226-step02-fork-grouped-exact-2026-10-09`,
`nexus/search-telemetry-and-perk-measurements-2026-10-09` [29728], and the
v0.1.155 arm-phase read (conexus scratchpad `l155.out`).

### Key Discoveries

- **✅ Verified** (measurement). Gap 1's mechanism: same plan at k 100 and 120,
  filter discards ~80% of the leaf walk, `relaxed_order` stops at k admitted rows.
- **✅ Verified** (measurement). Exact arm cost alone on the fork at k 120: 95 to
  581 ms warm, 110 to 1046 ms first touch, every plan a primary-key bitmap scan,
  ~11.4 buffer hits per row. Under 5-permit concurrency on the cloud: 1.1 to
  2.0 s for the three largest.
- **✅ Verified** (measurement). Warm HNSW arms on the same collections: 6 to 9 ms.
- **✅ Verified** (measurement). Cold HNSW first touch: 2.2 to 3.6 s (fork,
  nqsa7) and 5.4 to 7.2 s (fork, RDR-226 Step 0.2).
- **✅ Verified** (Phase 0, fork, `nexus_rdr/227-research-2`). Positive
  control: the fork reproduces production's code-group contention at 10
  concurrent workers, not 5, because each worker waits a ~90 ms round trip
  between arms; every Phase 0 cost is quoted at 10 workers with 5 and 15 as
  brackets.
- **✅ Verified** (Phase 0, `nexus_rdr/227-research-2`). A4: `ef_search = 1000`
  removes the top-1 miss but leaves the nqsa7 repro at recall 0.975 (k 40) and
  0.983 (k 120); `strict_order` alone is no better than serving.
- **✅ Verified** (Phase 0, `nexus_rdr/227-research-3`). A1, A2, A3: a partial
  index per collection is used by the bound, custom-planned arm (7 to 9 ms
  exec), reaches recall >= 0.99 on every own-topic query including the nqsa7
  repro, and builds CONCURRENTLY in 10 to 21 s without blocking writes.
- **❓ Assumed** (A5 below).

### Critical Assumptions

- [x] **A1.** The planner uses a partial index `WHERE collection = 'X'` for an
  inlined `plain_search_<dim>` arm whose `p_collections` is the one-element
  array `{X}` (predicate implication through `= ANY` of a one-element constant
  array), with `p_collections` BOUND as jOOQ binds it, under
  `plan_cache_mode = force_custom_plan`. A literal `'{X}'` in the SQL text can
  prove the implication while a bound array cannot, so only the bound form
  counts. **Status**: ✅ Verified (Phase 0, `nexus_rdr/227-research-3`): with
  bound parameters under `force_custom_plan` all four indexed arms used their
  partial index, 7 to 9 ms exec. Under a GENERIC plan the arm does not use it
  and walks every tenant leaf's HNSW, so the path must stay custom-planned.
  **Method**: Spike (Phase 0).
- [x] **A2.** An unfiltered walk over one collection's own graph returns that
  collection's exact top-k at the serving settings (recall >= 0.99 at k 40 to
  120 on real query vectors, the nqsa7 repro plus 18 natural-language queries
  over the three collections; queries seeded from the collection's own rows never
  missed on nqsa7 and over-state recall). **Status**: ✅ Verified (Phase 0,
  `nexus_rdr/227-research-3`): `code__1-1`, `code__1-2` and `code__1-72` reach
  >= 0.99 on every own-topic query at serving settings, the nqsa7 repro
  included. `code__1-20`, queried only off-topic, read 0.975 to 0.983 on 1 to 5
  of 19 queries at k >= 60 with no top-1 miss (ordinary HNSW approximation on
  far queries). **Method**: Spike (Phase 0).
- [x] **A3.** Building one partial HNSW index over 30k to 60k rows takes minutes,
  not hours, and `CREATE INDEX CONCURRENTLY` on a leaf does not block writes.
  **Status**: ✅ Verified (Phase 0, `nexus_rdr/227-research-3`): 10 to 21 s and
  229 to 480 MB per index (about 8.2 KB per row) at the instance defaults (about
  655 MB `maintenance_work_mem`, 2 workers; raising them changed nothing). The
  build held `ShareUpdateExclusiveLock` only; an insert into the same leaf every
  0.2 s kept committing (max 0.6 s, 0 errors). **Method**: Spike (Phase 0, fork).
- [x] **A4.** A cheaper alternative does not already close Gap 1: a larger
  `ef_search` (1000) or `strict_order` for single-collection arms on large
  collections. **Status**: ✅ Verified (Phase 0, `nexus_rdr/227-research-2`):
  `ef_search = 1000` leaves the nqsa7 repro below 0.99 (0.975 to 0.983);
  `strict_order` is no better than serving. Sam, 2026-10-09: continue to
  per-collection indexes. **Method**: Spike (Phase 0).
- [ ] **A5.** The engine can build and drop these indexes. `nexus_svc` does not
  own the leaves, and DDL is not governed by RLS, so `nexus_svc` cannot
  `CREATE INDEX` on a leaf at all. The builder needs either a `nexus_admin`
  connection in the engine or a `SECURITY DEFINER` function owned by the leaf
  owner that builds only the index this RDR names. `CREATE INDEX CONCURRENTLY`
  cannot run inside a transaction or a function, which rules out the second form
  as written. **Status**: ✅ Verified (source read plus deployment check,
  conexus-1c, 2026-10-09): the cloud engine carries `NX_DB_ADMIN_URL`, `_USER`
  and `_PASS` as `nexus_admin` for its whole lifetime (rendered into its env by
  conexus's `render-engine-env.sh`), and both URLs go direct to the database on
  :5432, never through a pooler. The reconciler uses them (Technical Design,
  Builder). **Method**: Source read plus deployment check.

## Proposed Solution

### Approach

For each collection above T, keep a partial HNSW index on its leaf restricted to
that collection. A one-collection arm on such a collection then walks a graph of
its own rows only: no filtering, no crowd-out, HNSW speed. The router sends an
arm exact at or below T, and to the collection's own index above it. Collections
without their own index, during the build or after a failed one, keep today's
behaviour (exact, or the leaf walk).

### Technical Design

- **Index.** `CREATE INDEX CONCURRENTLY <name> ON <leaf> USING hnsw
  (embedding_<dim> vector_cosine_ops) WHERE collection = '<collection>'`, with the
  name derived from (model, tenant, collection) the way RDR-225 names leaf
  objects, hashed to fit the 63-byte identifier limit. Build parameters match the
  leaf index's (m 16, ef_construction 64). The collection is written with
  `format('%L')`, never concatenated.
- **Leaf, not collection.** Collection names repeat across tenants (Phase 0's
  first build landed on another tenant's leaf with the same `code__1-1`). The
  builder resolves the (model, tenant) leaf first and names it in the DDL; it
  never addresses the partitioned parent.
- **Builder (A5).** `nexus_svc` cannot create an index on a leaf it does not
  own, and `CREATE INDEX CONCURRENTLY` cannot run inside a function, so a
  `SECURITY DEFINER` wrapper is out. The reconciler opens one direct connection
  (never through the pooler: a concurrent build needs a session, the same reason
  as the migration lock) with the `NX_DB_ADMIN_*` credentials `Main` already
  reads for `SchemaMigrator`, in autocommit (a concurrent build cannot run in a
  transaction block, so not through jOOQ's transaction wrapper), issues only
  `CREATE INDEX CONCURRENTLY` and `DROP INDEX CONCURRENTLY IF EXISTS` for names
  it derived, and closes it after each build. It holds no admin connection
  between builds, because conexus rotates `nexus_admin` in place without an
  engine restart; an authentication failure logs and retries on the next pass.
  An engine without admin credentials logs once and builds nothing; routing then
  behaves as today. A failed concurrent build leaves an INVALID index holding the
  name; the next pass drops exactly that name and rebuilds, and routing treats
  only `indisvalid` indexes as present. The non-concurrent alternative inside a `SECURITY DEFINER`
  function was rejected: it blocks the leaf's writes for the whole build (10 to
  21 s per index measured).
- **Lifecycle.** The engine reconciles indexes against collection sizes, under
  the role A5 settles: a
  collection that crosses above T gets an index built in the background; one that
  falls below T, is deleted, quarantined or renamed loses it. A collection keeps
  the exact path until its index is valid (`pg_index.indisvalid`).
- **Routing.** The router's probe already counts the collection; it also checks
  for a valid per-collection index. Exact at or below T; per-collection HNSW above
  T when an index exists; above T without one, exact if the collection is under
  a hard ceiling and the leaf walk otherwise (today's behaviour).
- **Plan mode.** The arm stays under `plan_cache_mode = force_custom_plan`
  (A1): a generic plan skips the partial index and walks every tenant leaf. An
  EXPLAIN pin guards it.
- **Cold start.** A per-collection index's first touch after a restart was 86 to
  189 ms with the OS cache intact; from disk it should match the leaf index's 2
  to 7 s. The engine prewarms each valid per-collection index with one search
  after boot and after each build. A database failover without an engine
  restart is not covered and stays a Gap 3 residual.
- **Schema.** No Liquibase changeset creates these indexes; they are runtime
  objects keyed on data. The reconciler needs no registry table: index names
  are derived from (model, tenant, collection), so `pg_class` and `pg_index`
  answer which exist and which are valid.

### Existing Infrastructure Audit

- Router and probe: `PgVectorRepository.runPlainSearchStatement`,
  `probeSelectedRows`, `PgSession.searchExactMaxRows`.
- Leaf naming and creation: RDR-225's partition functions
  (`vectors-030-model-tenant-partition-functions.xml`).
- Arm timing: `event=search_per_collection_arm_phases` (cacef2f92), which reads
  any change's effect on statement time and permit waits.

### Decision Rationale

A per-collection index is the only option that gives a large collection both its
exact top-k and HNSW cost, provided A1 and A2 hold. Exact is correct but costs
1 to 2 s per large arm under load. The leaf walk is fast but loses rows. A lower
T trades one for the other per collection.

## Alternatives Considered

### Alternative 1: Keep T = 60000 (exact for all)

Correct for this tenant today. Costs ~1 s per warm default search (Gap 2) and
grows with collection size. Rejected as the end state; it was the nqsa7 stopgap.

### Alternative 2: Keep T = 30000 (the v0.1.156 interim)

Removes the two slowest arms, keeps `code__1-72` exact. Leaves `code__1-1` and
`code__1-2` on the lossy leaf walk. Acceptable only until this RDR lands.

### Alternative 3: Sub-partition the leaf by collection for large collections

Give each large collection its own partition, with its own HNSW index, so
partition pruning routes the arm. Same search behaviour as Approach, but moving a
collection between partitions moves its rows, which costs a large write per
crossing of T. Kept as the fallback if A1 fails.

### Alternative 4: More walk effort for single-collection arms

Raise `ef_search` to 1000 or use `strict_order` for large single-collection
arms. No DDL. It may narrow Gap 1 but cannot remove the crowd-out that causes it.
Phase 0 measured it first (A4): `ef_search = 1000` fixes the top-1 miss and cuts
the large arms to 15 to 171 ms, but leaves the nqsa7 repro at 0.975 to 0.983.
Rejected by Sam on 2026-10-09 in favour of per-collection indexes.

### Briefly Rejected

- A separate permit pool for exact arms: frees the knowledge request, but a
  search waits for every request, so the slowest code arm still sets the total.
- A smaller per-collection k: an exact arm's cost is the scan of every row, not k.

## Trade-offs

### Consequences

- More indexes per leaf: one per large collection per tenant, so the count grows
  with tenants times large collections. Each costs about 8.2 KB of disk per row
  (Phase 0) and write amplification on every insert into that collection.
- A background index build after a collection crosses T, during which the
  collection keeps today's path.

### Risks and Mitigations

- **A1 fails** (the planner will not use the partial index through the inlined
  function). Mitigation: a dedicated `plain_search_one_<dim>` with a scalar
  collection parameter, or Alternative 3.
- **Index build load on the managed instance.** Mitigation: build concurrently,
  one at a time, in a maintenance window; measure on the fork first (A3).
- **Cold first touch** (Gap 3). Mitigation: prewarm after boot, or exact until
  first touch.

### Failure Modes

An invalid or missing per-collection index degrades to today's routing, never to
an error. A failed build leaves an invalid index that the reconciler drops and
retries.

## Implementation Plan

### Prerequisites

- [x] A1 to A4 measured (Phase 0, 2026-10-09).
- [x] A5: the cloud engine's admin credentials confirmed present after boot
  (conexus-1c, 2026-10-09).
- [ ] conexus's `nexus_admin` rotation procedure verifies the reconciler
  reconnects after a rotation (conexus-1c offered to add it).
- [x] Sam's decision on the stop rule: continue to per-collection indexes
  (2026-10-09).

### Minimum Viable Validation

On a PITR fork: build a partial HNSW index for `code__1-72`, `code__1-1` and
`code__1-2`. Run the nqsa7 repro query and the 18 natural-language query vectors
(`/tmp/rdr227-query-vectors-voyage-code-3.json`, voyage-code-3, `input_type`
null) through the inlined function with bound parameters at the serving
settings. Recall >= 0.99 at k 40 to 120, warm
arm time under 50 ms, and a stated cold first-touch time.

### Phase 0: Measure (decides the approach, may stop the work) — done 2026-10-09

- **Positive control, fork, first.** One fan-out emulated as production runs it:
  the 28 code arms and the 61 knowledge arms through a 5-worker pool sharing
  permits. It must reproduce v0.1.155/156 handler times before any alternative's
  number is trusted; solo numbers are recorded beside it so the concurrency
  factor shows.
- **Step 0.1, fork.** A4 first, under that concurrency: `ef_search = 1000` under
  `relaxed_order`, `strict_order` at the serving `ef_search`, and both, each with
  and without the router probe, at the engine's live `max_scan_tuples` and
  derived `scan_mem_multiplier` (`event=hnsw_scan_budget`).
- **Step 0.2, fork.** A1 and A2: build the three partial indexes, `EXPLAIN` the
  inlined arm with bound parameters to confirm the planner uses them, measure
  recall and warm time, and cold time after an instance restart (a lower bound,
  since the OS page cache may survive), beside cold time on the existing leaf
  index measured the same way.
- **Step 0.3, fork.** A3: build time and size for each index at the instance's
  `maintenance_work_mem` and `max_parallel_maintenance_workers` and at raised
  values; write blocking proven by a concurrent insert into the same leaf
  partition plus `pg_locks` sampling during each `CONCURRENTLY` build.
- **Exit.** Numbers to T2. If Step 0.1 reaches recall >= 0.99 at acceptable cost,
  implement Alternative 4 and stop. If A1 fails, revise toward Alternative 3.

### Phase 1: Engine

The index reconciler (admin connection per build, autocommit, invalid-index
recovery), the router's per-collection-index branch, and the prewarm after boot
and after each build. Tests: the planner's choice (EXPLAIN pin), recall
against exact on a seeded leaf with a large collection beside larger siblings,
and the degraded paths (no index, invalid index).

### Phase 2: Release

One engine cut. Index builds on the managed instance run in a stated window after
deploy (Phase 0: 10 to 21 s each, no write blocking). T then drops below the
mid-size collections Phase 0 measured indexed (`code__1-72` 27,893 rows,
`code__1-20` 29,355), so they leave the exact path too; the value below that is
set from Phase 1's substrate measurements of smaller indexed collections.

### Day 2 Operations

`nx doctor` reports per-collection indexes that are missing, invalid or orphaned.

### New Dependencies

None.

## Test Plan

- EXPLAIN pin: an arm on an indexed collection uses its partial index.
- Recall: seeded leaf where one large collection sits beside larger siblings;
  the arm's top-k equals exact.
- Degraded: no index, invalid index, index being built.
- Lifecycle: crossing T builds, dropping below T or deleting drops.
- Invalid index: a failed concurrent build is dropped by name and rebuilt;
  routing ignores it meanwhile (the EXPLAIN pin also asserts `indisvalid`).
- Tenant targeting: two tenants with the same collection name each get an index
  on their own leaf.

## Validation

### Testing Strategy

Engine integration tests on a substrate leaf seeded to reproduce the crowd-out;
fork measurements for scale.

### Performance Expectations

Warm large arm under 50 ms, against 1.1 to 2.0 s exact under load (Phase 0 at
10 workers: indexed arms 10 to 22 ms against exact 0.4 to 1.2 s; code-group
statement total 2.5 to 3.2 s against 4.8 to 5.9 s). Warm default
search back to about its v0.1.154 time (2.0 s on develop, before the text cap),
with correct recall on every collection.

## Finalization Gate

### Contradiction Check

None open. Gap 2's cost and Gap 3's cold cost pull in opposite directions; the
design resolves them by measuring cold first touch before choosing (Phase 0).

### Assumption Verification

A1 to A4 were verified by the Phase 0 fork spikes on 2026-10-09
(`nexus_rdr/227-research-2`, `-3`); A5 by a source read and a deployment check
the same day.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Partial HNSW index (`USING hnsw ... WHERE`) | pgvector 0.8.2 / PG 17 | Docs |
| Partial-index use through `= ANY(one-element array)` | PostgreSQL 17 planner | Assumed (A1), Spike planned |
| `CREATE INDEX CONCURRENTLY` on a partition | PostgreSQL 17 | Docs |

### Scope Verification

The Minimum Viable Validation is in scope and runs in Phase 0.

### Cross-Cutting Concerns

- **Tenancy**: indexes are per tenant leaf. Index DDL is not governed by RLS,
  so tenant scoping comes from the leaf the reconciler names, and the role that
  builds the index is not the serving role (A5).
- **Local mode**: a local install has few large collections; the same code path
  applies.

### Proportionality

Phase 0 was three fork measurements and could have ended the work at
Alternative 4; it did not (A4).

## References

- T2 `conexus/nqsa7-fork-hnsw-vs-exact-2026-10-09` [29744]
- T2 `conexus/rdr226-step02-fork-grouped-exact-2026-10-09`
- T2 `nexus_rdr/226-research-13`, `nexus_rdr/226-research-14`
- T2 `nexus/search-telemetry-and-perk-measurements-2026-10-09` [29728]
- T2 `nexus_rdr/227-research-2`, `nexus_rdr/227-research-3`,
  `conexus/rdr227-phase0-fork-2026-10-09`
- Beads nexus-43ulx, nexus-nqsa7

## Revision History

- 2026-10-09: drafted.
- 2026-10-09: Phase 0 method revised from the fork owner's review (bound
  parameters, real query vectors, concurrency control); A5 added.
- 2026-10-09: Phase 0 results recorded (A1 to A4 verified; A4 fails the stop
  rule); Sam chose per-collection indexes; Builder, plan-mode and cold-start
  design filled in.
