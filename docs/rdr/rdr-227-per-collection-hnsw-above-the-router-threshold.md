---
title: "Per-Collection HNSW Indexes Above the Router Threshold"
id: RDR-227
type: Architecture
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-10-09
accepted_date: 2026-10-09
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
Target: recall against exact >= 0.99 at k 40 to 120 on real query vectors
written for the collection's own topics, the same bar for every design this RDR
compares. Own-topic, because a far query's rows from that collection rarely reach
a fan-out's merged page (inferred, not measured).

#### Gap 2: The exact path is too slow for the largest collections under load

nexus-nqsa7 raised the router default to 60000, so every collection of the
tenant became exact. On engine v0.1.155 (2026-10-09 09:37-09:38Z) the large exact
code arms took 1.1 to 2.0 s each under 5-permit concurrency (`code__1-1` 58,588
rows 1.65-2.0 s, `code__1-2` 45,525 rows 1.28-1.56 s, `code__1-20` 29,355 rows
1.1-1.2 s), against 0.3 to 0.6 s alone on the fork. They held the shared permits,
so the knowledge request waited 6.5-7.2 s for permits in total, and a warm default
search got about 1 s slower (2.64-3.31 s to 3.89-4.26 s on the same 7.75.0
client). The interim is the router default T = 30000 in engine-service-v0.1.156
(Sam, 2026-10-09), which returns the two largest collections to the leaf walk.
Under T = 30000 the floor moves to the mid-size exact arms, `code__1-20` (29,355
rows) and `code__1-72` (27,893 rows), at about 0.8 s each under concurrency
(statement maxima 710 to 989 ms, live on v0.1.156, `nexus_rdr/227-research-1`),
and a warm default search stays 0.5 to 0.7 s slower than on v0.1.154. Lowering T below
27,893 brings back Gap 1's miss on `code__1-72`. A T between 27,893 and 29,355
would move only `code__1-20` off the exact path, whose recall at
`ef_search = 1000` was not measured; Phase 1 Step 3 measures it.

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
- **RDR-226** proposed grouping the small, exact collections of a leaf into one
  statement, and was abandoned at its Phase 0 stop gate (2026-10-09). Its
  Phase 0 found that a grouped statement does no less database work and that
  most arm time in production is outside the statement
  (`nexus_rdr/226-research-14`). It does not address large collections; this RDR
  does. The per-arm time outside the statement is nexus-pgm7u.
- **RDR-225** Alternative 3 rejected partial HNSW indexes per MODEL for fragile
  planning: the planner uses a partial index only when it can prove the query
  implies the predicate, and a miss there was a silent sequential scan. Here the
  leaf's full HNSW index stays, so when stats drift makes the planner skip a
  partial index the arm falls back to today's leaf walk, not to a scan; a
  generic plan would instead walk every tenant's leaf (A1). A1 verified the
  proof holds on the engine's bound, custom-planned call, and a sampled runtime
  check (Technical Design, Plan mode) watches it. RDR-225 Alternative 4 rejected a
  partition per collection for partition count; this RDR's Alternative 3 is a
  narrower version of that layout (large collections only) and is not pursued.

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
- **✅ Verified** (source read, `nexus_rdr/227-research-4`). The engine reads
  `NX_DB_ADMIN_*` once at boot (`Main.java:461-466`); a builder that reuses them
  is bound to the password at boot (A5).
- **❓ Assumed** (A6 below).

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
- [x] **A5.** The engine can build and drop these indexes. `nexus_svc` does not
  own the leaves, and DDL is not governed by RLS, so `nexus_svc` cannot
  `CREATE INDEX` on a leaf at all. The builder needs either a `nexus_admin`
  connection in the engine or a `SECURITY DEFINER` function owned by the leaf
  owner that builds only the index this RDR names. `CREATE INDEX CONCURRENTLY`
  cannot run inside a transaction or a function, which rules out the second form
  as written. **Status**: ✅ Verified (source read plus deployment check,
  conexus-1c, 2026-10-09): the cloud engine carries `NX_DB_ADMIN_URL`, `_USER`
  and `_PASS` as `nexus_admin` for its whole lifetime (rendered into its env by
  conexus's `render-engine-env.sh`), and both URLs go direct to the database on
  :5432, never through a pooler. The engine reads them once at boot
  (`nexus_rdr/227-research-4`), so a builder that uses them is bound to the
  password the engine booted with; the design makes that explicit (Technical
  Design, Builder). Not shown: that `nexus_admin` owns the leaves on the managed
  instance; Phase 1 Step 2 checks it first. **Method**: Source read plus
  deployment check.
- [ ] **A6.** `pg_prewarm` is available on the managed instance and in the local
  PG bundle, and prewarming a per-collection index makes its first search warm.
  **Status**: Unverified. **Method**: Spike (Phase 1).

## Proposed Solution

### Approach

For each collection above T, keep a partial HNSW index on its leaf restricted to
that collection. A one-collection arm on such a collection then walks a graph of
its own rows only: no collection filter, no crowd-out, HNSW speed (the
live-row check and any caller filter still apply). The router sends an arm exact
at or below T, and to the collection's own index above it. A collection above T
without a valid index, during its build or after a failed one, takes the leaf
walk at `ef_search = 1000`.

### Technical Design

- **Index.** `CREATE INDEX CONCURRENTLY <name> ON <leaf> USING hnsw
  (embedding_<dim> vector_cosine_ops) WHERE collection = '<collection>'`, with the
  name `pci_` plus a hash of (model, tenant, collection), to fit the 63-byte
  identifier limit (Index name, below). Build parameters match the
  leaf index's (m 16, ef_construction 64). The collection is written with
  `format('%L')`, never concatenated.
- **Leaf, not collection.** Collection names repeat across tenants (Phase 0's
  first build landed on another tenant's leaf with the same `code__1-1`). The
  builder resolves the (model, tenant) leaf first and names it in the DDL; it
  never addresses the partitioned parent.
- **Builder (A5).** `nexus_svc` cannot create an index on a leaf it does not
  own, and `CREATE INDEX CONCURRENTLY` cannot run inside a function, so a
  `SECURITY DEFINER` wrapper is out. The non-concurrent build inside such a
  function was rejected: it blocks the leaf's writes for the whole build (10 to
  21 s per index measured). The builder opens a direct connection (never through
  the pooler: a concurrent build needs a session) with the `NX_DB_ADMIN_*` values
  `Main` read at boot, in autocommit (a concurrent build cannot run in a
  transaction block, so not through jOOQ's transaction wrapper), and closes it
  after each pass. It sets `application_name` to `nexus-pci-builder-` plus the
  engine's per-boot id, the form `BackendReaper` uses (`nexus_rdr/227-research-7`),
  so the engine's shutdown terminates its own builder backend. A concurrent
  build waits for older transactions, and those waits are lock waits, so the
  `CREATE INDEX CONCURRENTLY` statement runs with no `lock_timeout` and a
  30-minute `statement_timeout`; `DROP INDEX CONCURRENTLY` runs with a 5 s
  `lock_timeout`; a drop that times out is retried on the next pass. Both values
  are design choices; Phase 1 Step 3 measures build time under them.
  The socket timeout sits above the statement timeout.
- **Credentials are restart-bound.** The engine reads `NX_DB_ADMIN_*` once at
  boot (`nexus_rdr/227-research-4`), so after an in-place `nexus_admin` rotation
  the builder authenticates with the old password until the engine restarts.
  This RDR therefore requires an engine restart after a `nexus_admin` rotation.
  T2 `nexus/engine-credential-model-admin-startup-only` already makes the
  engine's env rewrite a precondition of the next boot, since every boot re-runs
  migration as admin. This RDR amends that entry's conclusion that rotating the
  admin password without an engine restart is safe: rotating without a restart
  now stops index maintenance until the restart.
  conexus owns the rotation procedure and offered to add the restart
  (2026-10-09); their confirmation is a prerequisite. An authentication failure sets
  `builder_state = auth_failed` in the status object (Day 2 Operations) and logs
  `event=pci_builder_auth_failed` on every pass until it clears, so a stale
  credential is visible, never silent.
- **No admin credentials.** `Main` falls back to the app credentials for
  migration when `NX_DB_ADMIN_*` are absent; the builder does the same. If that
  role cannot create an index on the leaf (`insufficient_privilege`), the builder
  sets `builder_state = no_privilege`, logs once, and builds nothing.
- **Lifecycle.** One reconciler per engine, at most one builder per database.
  - *Build threshold.* Builds are driven by the reconciler's own row counts, not
    by searches, so a collection gets its index before the router ever sends it
    to one. `NX_SEARCH_PCI_BUILD_MIN_ROWS` (B, an integer >= 1, default 20000) is
    the build threshold; it is independent of the routing threshold T, and B <= T
    is what lets T be lowered to B later (Phase 2b). The routing probe cannot do
    this job, because it counts at most T+1 rows and never reports a collection
    below T (`nexus_rdr/227-research-7`).
  - *Sweep, read half.* Every engine, with no lock, once after boot and every
    10 minutes (`NX_SEARCH_PCI_SWEEP_SECONDS`, an integer >= 60): enumerate the
    per-collection indexes on every leaf by name prefix `pci_` in
    `pg_class`/`pg_index` (catalogs are not row-secured), parse each index's
    collection from its deparsed predicate (`pg_get_expr(indpred, indrelid)`,
    which reads `(collection = '<name>'::text)`), and replace the router's
    valid-index set with the `indisvalid` ones. Until the first read the set is
    empty. The read half runs on its own pooled connection as `nexus_svc`, which
    can read the catalogs, and it runs whatever `NX_SEARCH_PCI` says.
  - *Sweep, DDL half.* Only the lock holder (One builder, below). For each leaf
    it resolves the leaf's tenant from the partition bound, then, in one explicit
    transaction on the admin session (`SET LOCAL` has no effect outside one,
    `nexus_rdr/227-research-7`), sets `SET LOCAL nexus.tenant` to that tenant and
    counts rows per collection on the leaf. `nexus.chunks` is FORCE ROW LEVEL
    SECURITY, so the owner role sees only that tenant's rows
    (`nexus_rdr/227-research-5`). It reads the collection registry under the same
    tenant. It then drops by the rules below, builds the missing indexes, and
    prewarms. Building and dropping run outside that transaction, in autocommit.
  - *Build and retirement rules.* Build when a collection that is live in the
    registry (`superseded_by` empty) counts at least B rows on its leaf and has no
    `pci_` index. Drop when its count falls below B/2, when the registry no longer
    lists it, or when its `superseded_by` is set. A count of zero for a
    collection the registry lists as live and populated is treated as a counting
    failure: logged, never acted on. The canonical rename re-homes the chunk rows
    and supersedes the old name (`nexus_rdr/227-research-5`), so the old index is
    dropped and the new name is built on the next pass. A supersede keeps the old
    rows (`nexus_rdr/227-research-6`) and is dropped by `superseded_by`. A COPY
    rename leaves the old collection live with its rows
    (`nexus_rdr/227-research-7`), so its index correctly stays. Quarantine moves
    rows to a separate quarantine collection, so the source count falls.
  - *One builder.* The DDL half takes `pg_try_advisory_lock` on its admin
    session (the migrator's precedent, `nexus_rdr/227-research-5`) on its own key,
    never `SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY` (`nexus_rdr/227-research-6`),
    and, like that key, outside the int4 range that the repositories'
    `hashtext` transaction locks occupy; it holds the lock for the pass. A second engine fails the try and skips the DDL
    half; its read half still refreshes its router set. Because the work comes
    from the counts, not from a per-engine queue, whichever engine holds the lock
    sees all of it. Builds run one at a time, with
    `CREATE INDEX CONCURRENTLY IF NOT EXISTS`.
  - *Failed versus in flight.* Only the lock holder builds, and it holds the lock
    for the whole build. So an INVALID `pci_` index that the lock holder finds
    outside its own build is a failed build (its builder's session ended); the
    holder drops it with `DROP INDEX CONCURRENTLY IF EXISTS` on that exact name.
    Routing counts only `indisvalid` indexes. A collection whose build fails is
    retried no sooner than 10 minutes later (the next pass at or after that),
    doubling to a 24-hour cap; after three consecutive failures the holder's
    status counts it as failing (Day 2 Operations).
  - *Migrations.* A boot migration that alters a leaf would queue behind an
    in-flight build, and every later read and write of that leaf would queue
    behind the migration. So the migrator, before it walks, terminates any
    backend whose `application_name` starts with `nexus-pci-builder-`
    (`pg_terminate_backend`, which works on the same role without superuser,
    `nexus_rdr/227-research-7`). Terminating the session, not cancelling the
    statement, ends the whole pass and releases its advisory lock, so no further
    build starts; the interrupted build leaves an invalid index that a later pass
    drops and rebuilds. The builder never runs during its own engine's migration,
    which completes before the reconciler starts.
  - *Caps and switches.* At most `NX_SEARCH_PCI_MAX_PER_LEAF` indexes per leaf
    (an integer >= 0, default 16; 0 builds none). `NX_SEARCH_PCI=0` (`1`, the
    default, enables it; any other value refuses boot) turns the DDL half off: no
    builds and no drops. The read half still runs, so existing valid indexes stay
    in the router set and keep serving. All four settings are validated at boot
    like `NX_SEARCH_EXACT_MAX_ROWS`.
- **Routing.** The router decides exact versus HNSW as today, by the probe
  against T. With `NX_SEARCH_EXACT_MAX_ROWS=0` there is no probe and every
  single-collection arm takes HNSW: an indexed collection at the serving
  `ef_search`, any other at `ef_search = 1000`. The partial index needs no separate route: on a single-collection
  HNSW statement the planner chooses it under `force_custom_plan` (A1). The
  router keeps an in-memory set of valid (leaf, collection) indexes, refreshed
  by every engine's read half and after each local build, and uses it only to
  choose `ef_search`. Set membership is known before the arm's statement, so the
  choice goes into the existing settings batch (an arm that the probe then sends
  exact ignores `ef_search`), and no arm pays a catalog round trip:

  | Probe | Valid per-collection index | Route |
  | --- | --- | --- |
  | <= T | any | exact (today) |
  | > T | yes | HNSW, serving `ef_search`; the planner walks the partial index |
  | > T | no (below B, none yet, building, failed, capped, or no privilege) | HNSW leaf walk at `ef_search = 1000` (Alternative 4 as the fallback; single-collection arms only) |

  A statement over several collections cannot use a partial index (its
  predicate does not imply one collection) and keeps today's leaf walk.
- **Plan mode.** The plain-search arm reaches a per-collection index through
  `runPlainSearchStatement`'s GUC batch, which sets
  `plan_cache_mode = force_custom_plan` (`PgVectorRepository.java:1458-1461`;
  `HnswServingGucParityTest` pins that every site setting `hnsw.iterative_scan`
  also sets the plan mode); a generic plan skips the partial index and walks
  every tenant leaf (A1). The sampled check below binds `p_collections` the way
  the arm does, since a literal can prove the predicate where a bound array
  cannot. An EXPLAIN pin guards it in tests, and in
  production one arm in 1000 on an indexed collection runs `EXPLAIN` first and
  logs `event=pci_plan_check used=<bool>`, so a silent fall back to the leaf walk
  shows up.
- **Cold start.** A per-collection index's first touch after a restart was 86 to
  189 ms with the OS cache intact (a lower bound); from disk it should be of the
  order of the leaf index's 2 to 7 s (not measured for a partial index). The
  lock holder runs `pg_prewarm` on its admin session (the leaf owner, which
  Phase 1 Step 2 confirms) on
  each valid per-collection index after boot and after each build, if the
  extension is installed (A6). One search would warm
  only its own walk, not the graph. A database failover without an engine
  restart is not covered and stays a Gap 3 residual.
- **Schema.** No Liquibase changeset creates these indexes; they are runtime
  objects keyed on data. The reconciler needs no registry table: the `pci_`
  prefix and each index's predicate let `pg_class` and `pg_index` answer which
  exist, for which collection, and which are valid. Only the builder creates
  `pci_` names; an operator-made index with that prefix is treated like the
  builder's own and falls under the build and retirement rules.
- **Index name.** `pci_` plus the first 24 hex digits of the SHA-256 of model,
  tenant and collection joined by a NUL byte.

### Existing Infrastructure Audit

- Router and probe: `PgVectorRepository.runPlainSearchStatement`,
  `probeSelectedRows`, `PgSession.searchExactMaxRows`.
- Leaf naming and creation: RDR-225's partition functions
  (`vectors-030-model-tenant-partition-functions.xml`).
- Arm timing: `event=search_per_collection_arm_phases` (cacef2f92), which reads
  any change's effect on statement time and permit waits.

### Decision Rationale

Exact is correct but costs 0.4 to 2 s per large arm under load. The leaf walk at
`ef_search = 1000` is fast and meets the target on every tested query of the two
largest collections, but not on `code__1-72` (A4), so T cannot drop below the
mid-size collections on it alone. A per-collection index meets the target on all
three (A2), which is what lets T drop. Phase 0's statement sums at 10 workers:
exact 4.8 to 5.9 s, `ef_search = 1000` on the three target arms 2.2 to 3.3 s,
indexes on four arms 2.5 to 3.2 s (`nexus_rdr/227-research-6`). On statement time
the indexes add little over `ef_search = 1000`; what they add is the recall that
lets the mid-size arms leave the exact path. Whether that is worth the
reconciler is measured, not assumed: Phase 2a ships `ef_search = 1000` first, and
Phase 2b ships the indexes only after a measured default search on Phase 2a.

## Alternatives Considered

### Alternative 1: Keep T = 60000 (exact for all)

Correct for this tenant today. Costs ~1 s per warm default search (Gap 2) and
grows with collection size. Rejected as the end state; it was the nqsa7 stopgap.

### Alternative 2: Keep T = 30000 (the v0.1.156 interim)

Removes the two slowest arms, keeps `code__1-72` exact. Leaves `code__1-1` and
`code__1-2` on the filtered leaf walk at the serving `ef_search`, the walk that
missed the nearest row on `code__1-72`; Phase 0 reported serving recall only in
aggregate, and its one top-1 miss is the `code__1-72` repro (inferred from the
matching 0.925). Combined with Alternative 4 it meets the target on every
tested query; see Alternative 4.

### Alternative 3: Sub-partition the leaf by collection for large collections

Give each large collection its own partition, with its own HNSW index, so
partition pruning routes the arm. Same search behaviour as Approach, but moving a
collection between partitions moves its rows, which costs a large write per
crossing of T. Kept as the fallback if A1 fails.

### Alternative 4: More walk effort for single-collection arms

Raise `ef_search` to 1000 or use `strict_order` for large single-collection
arms. No DDL. Phase 0 measured it first (A4, `nexus_rdr/227-research-2`):
`ef_search = 1000` has no top-1 miss, mean recall 0.998 to 0.999, and passes
the Gap 1 target on every `code__1-1` and `code__1-2` query, at 69 and 171 ms.
Its one failure is the nqsa7 repro on `code__1-72` (0.975 to 0.983), a
collection that T = 30000 keeps exact. So `ef_search = 1000` above T with
T = 30000 meets the target on every tested query, with no DDL. What it leaves
is the mid-size exact arms (`code__1-20` 29,355 rows, `code__1-72` 27,893 rows,
about 0.8 s each under load, `nexus_rdr/227-research-1`), which set the latency
floor on v0.1.156, and `code__1-72` cannot leave the exact path on
`ef_search = 1000` alone without the miss T exists to prevent (`code__1-20` was
not measured own-topic). Per-collection indexes are what let them leave it (A2).
On 2026-10-09 Sam chose to continue to per-collection indexes after A4 failed
the stop rule. This RDR keeps `ef_search = 1000` as the route for an arm above T
without a valid index (Routing) and ships it first (Phase 2a).

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
  collection takes the `ef_search = 1000` leaf walk.
- Insert cost into an indexed collection (a second HNSW insert per row) was not
  measured; Phase 1 measures it on the substrate, bulk load included.
- The long-lived engine process now keeps the admin credentials it read at boot
  and opens an admin session, where before it closed the admin pool after
  migration. That session runs a fixed statement set and nothing else: its own
  session settings (`application_name`, `statement_timeout`, `lock_timeout`),
  the advisory lock, catalog reads and leaf resolution, per-tenant row counts and
  registry reads inside a transaction under `SET LOCAL nexus.tenant` for the leaf
  it is reconciling, `CREATE INDEX CONCURRENTLY IF NOT EXISTS` and
  `DROP INDEX CONCURRENTLY IF EXISTS` on `pci_` names it derived or read, and
  `pg_prewarm`. The collection is `%L`-quoted. This is a deliberate
  widening of the admin path, recorded here.
- A nexus_admin rotation requires an engine restart (Credentials are
  restart-bound).

### Risks and Mitigations

- **The planner stops using a partial index** (stats drift, a path without
  `force_custom_plan`). A1 holds today. Mitigation: the sampled plan check, and
  the fallback is the leaf walk, not a scan.
- **Index build load on the managed instance.** Builds start at the first DDL
  pass after Phase 2b deploys, for every live collection at or above B. Mitigation: concurrent builds, one at a time
  (A3: 10 to 21 s each, no write blocking). To defer them, deploy with
  `NX_SEARCH_PCI=0` and restart without it at a chosen time.
- **Cold first touch** (Gap 3). Mitigation: `pg_prewarm` after boot and after
  each build (A6).

### Failure Modes

A missing, invalid or unused per-collection index degrades to the leaf walk,
never to an error. A failed build leaves an invalid index that the builder holding
the lock drops by name; the next pass rebuilds it. A stale admin password stops builds and
drops, visibly (`builder_state`), until the engine restarts.

## Implementation Plan

### Prerequisites

- [x] A1 to A4 measured (Phase 0, 2026-10-09).
- [x] A5: the cloud engine's admin credentials confirmed present after boot
  (conexus-1c, 2026-10-09).
- [ ] conexus's `nexus_admin` rotation procedure restarts the engine
  (offered by conexus-1c, 2026-10-09; confirmation pending).
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

Step 1: the `ef_search = 1000` route for single-collection arms above T, which
needs no DDL and is the fallback for every collection without an index; it ships
alone as Phase 2a. Step 2: confirm on the managed instance (a read-only
`pg_class.relowner` query by conexus) that `nexus_admin` owns the leaves, which
A5's credential check did not show; then the reconciler and builder as specified
(Lifecycle), the router's index set, the status object, the sampled plan check,
the migrator's cancel of `nexus-pci-builder` backends, and the prewarm (A6).
Step 3: substrate measurements: insert and bulk-load cost into an indexed
collection, build time with the Builder's timeouts, local build time at the local
bundle's `maintenance_work_mem`, and recall at the Gap 1 target on own-topic
queries for `code__1-20`, on a prose collection and on a second tenant. Tests: the planner's choice (EXPLAIN pin), recall
against exact on a seeded leaf with a large collection beside larger siblings,
and the degraded paths (no index, invalid index).

### Phase 2: Release

- **Phase 2a.** One engine cut with Step 1 only (`ef_search = 1000` above T, T
  stays 30000). Measure a warm default search live on it, the same way the
  v0.1.154 to v0.1.156 numbers were taken.
- **Phase 2b.** Only if Phase 2a's measured default search shows the mid-size
  exact arms still setting the floor (that is, still the largest arm statements
  under concurrency): one engine cut with Step 2, gated on Step 3. T stays 30000
  in the cut and B is 20000, so the first DDL pass builds indexes for every live
  collection at or above 20000 rows, `code__1-72` and `code__1-20` included,
  while those two keep routing exact (Risks). Once both hold valid indexes and
  Step 3's recall runs (`code__1-20` own-topic, prose, second tenant) pass the
  Gap 1 target, T is lowered to B by the `NX_SEARCH_EXACT_MAX_ROWS` setting, and
  the code default follows in a later cut. Lowering T only after their indexes
  are valid keeps them off the `ef_search = 1000` walk, which misses on
  `code__1-72` (A4).

### Day 2 Operations

The engine's status response gains a `per_collection_indexes` object: valid,
invalid and building counts, read from `pg_index` and
`pg_stat_progress_create_index` so every engine reports the same numbers, and the
answering engine's own `builder_state` (`ok`, `auth_failed`, `no_privilege`,
`off`), failing count and last DDL pass time, which are per engine and labelled
so. The counts are global, not per tenant, like the existing `reaper` object on
the same unauthenticated route. It
is additive and goes in the wire ledger. Each sweep also logs `event=pci_sweep`
with the same counts. `nx doctor` reads the object over HTTP, so it works on a
managed install as well as a local one.

### New Dependencies

None.

## Test Plan

- EXPLAIN pin: an arm on an indexed collection uses its partial index.
- Recall: seeded leaf where one large collection sits beside larger siblings;
  the arm's recall against exact meets the Gap 1 target.
- Degraded: no index, invalid index, index being built.
- Lifecycle: reaching B builds; dropping below B/2, deleting or superseding
  drops; a COPY rename keeps the index; a collection between B and T has an index
  and still routes exact.
- Counting: a count taken without the tenant set (zero rows for a live,
  populated collection) is logged and never drops an index.
- Invalid index: a failed concurrent build is dropped by name and rebuilt;
  routing ignores it meanwhile (the EXPLAIN pin also asserts `indisvalid`).
- Tenant targeting: two tenants with the same collection name each get an index
  on their own leaf.
- Two reconcilers: two engines against one database build each index once.
- A pass during a build: the second engine skips; the build's index is not
  dropped.
- Rename: after a canonical rename the old index is dropped and the new name is
  built on the next DDL pass.
- Hysteresis: a collection between T/2 and T keeps its index.
- Credentials: a wrong admin password sets `builder_state = auth_failed` and
  builds nothing.
- Router refresh: the engine that loses the lock has the same valid-index set
  as the winner after its next read half.
- Off and privilege: `NX_SEARCH_PCI=0` and a role without privilege build and
  drop nothing; existing valid indexes keep serving; unindexed arms above T take
  `ef_search = 1000`.
- Router off: with `NX_SEARCH_EXACT_MAX_ROWS=0`, indexed arms take the serving
  `ef_search` and the rest `ef_search = 1000`.
- Settings: invalid `NX_SEARCH_PCI`, `NX_SEARCH_PCI_BUILD_MIN_ROWS`,
  `NX_SEARCH_PCI_SWEEP_SECONDS` or `NX_SEARCH_PCI_MAX_PER_LEAF` refuse boot.
- Shutdown: stopping the engine terminates its builder backend.
- Cap: a leaf at `NX_SEARCH_PCI_MAX_PER_LEAF` builds no more.
- Retry: a failing build backs off and is counted as failing after three tries.
- Migration: a migrator terminates an in-flight builder session, and no later
  build in that pass starts.
- Status: the `per_collection_indexes` object reports the counts above.
- Plan check: the sampled EXPLAIN logs `used=false` when the index is skipped.

## Validation

### Testing Strategy

Engine integration tests on a substrate leaf seeded to reproduce the crowd-out;
fork measurements for scale.

### Performance Expectations

Warm large arm under 50 ms, against 1.1 to 2.0 s exact under load (Phase 0 at
10 workers: indexed arms 10 to 22 ms against exact 0.4 to 1.2 s; code-group
statement total 2.5 to 3.2 s against 4.8 to 5.9 s). The default-search figure
is not derived yet: Phase 0 measured statement time, not a client search, and
measures it after T is lowered in Phase 2b.

## Finalization Gate

### Contradiction Check

Gap 2's cost and Gap 3's cold cost pull in opposite directions. Phase 0
measured cold first touch after a restart (a lower bound); the from-disk cost of
a partial index stays unmeasured and is Gap 3's residual, with `pg_prewarm` (A6)
as the mitigation.

### Assumption Verification

A1 to A4 were verified by the Phase 0 fork spikes on 2026-10-09
(`nexus_rdr/227-research-2`, `-3`); A5 by a source read and a deployment check
the same day (`-4`), with leaf ownership left to Phase 1 Step 2. A6 is open and
is a Phase 1 spike.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Partial HNSW index (`USING hnsw ... WHERE`) | pgvector 0.8.2 / PG 17 | Docs |
| Partial-index use through `= ANY(one-element array)` | PostgreSQL 17 planner | Spike (A1, Phase 0): used under `force_custom_plan`, not under a generic plan |
| `pg_prewarm` | PostgreSQL 17 contrib | Assumed (A6), Spike in Phase 1 |
| `CREATE INDEX CONCURRENTLY` on a partition | PostgreSQL 17 | Docs |

### Scope Verification

The Minimum Viable Validation ran in Phase 0 and passed (A1, A2). Phase 1 Step 3
adds the `code__1-20` own-topic, prose and second-tenant recall runs that Phase
2b's lower T depends on.

### Cross-Cutting Concerns

- **Tenancy**: indexes are per tenant leaf. Index DDL is not governed by RLS,
  so tenant scoping comes from the leaf the reconciler names. In cloud and local
  installs the role that builds the index is not the serving role (A5); in the
  dev posture without admin credentials it is (Builder, No admin credentials).
  The admin session's row counts are tenant-scoped by `SET LOCAL nexus.tenant`.
- **Local mode**: the same code path applies. The local daemon passes
  `NX_DB_ADMIN_*` to the engine (`storage_service_daemon.py:1292-1294`), so the
  builder can run. Local build time at the local bundle's
  `maintenance_work_mem` is a Phase 1 Step 3 measurement; a build that does not
  fit in it is much slower.
- **Credentials**: a `nexus_admin` rotation now requires an engine restart
  (Credentials are restart-bound).

### Proportionality

Phase 0 was three fork measurements and could have ended the work at
Alternative 4. It showed that `ef_search = 1000` above T = 30000 meets the target
on every tested query with no DDL, and this RDR ships that first and alone
(Phase 2a). On statement time the indexes add little over it (Decision
Rationale); what they add is the recall that lets the mid-size collections leave
the exact path. The reconciler, the admin session and runtime DDL ship only if
Phase 2a's measured default search shows those exact arms still set the floor
(Phase 2b).

## References

- T2 `conexus/nqsa7-fork-hnsw-vs-exact-2026-10-09` [29744]
- T2 `conexus/rdr226-step02-fork-grouped-exact-2026-10-09`
- T2 `nexus_rdr/226-research-13`, `nexus_rdr/226-research-14`
- T2 `nexus/search-telemetry-and-perk-measurements-2026-10-09` [29728]
- T2 `nexus_rdr/227-research-1` to `-7`,
  `nexus/engine-credential-model-admin-startup-only` [23506],
  `conexus/rdr227-phase0-fork-2026-10-09`
- Beads nexus-43ulx, nexus-nqsa7

## Revision History

- 2026-10-09: drafted.
- 2026-10-09: Phase 0 method revised from the fork owner's review (bound
  parameters, real query vectors, concurrency control); A5 added.
- 2026-10-09: Phase 0 results recorded (A1 to A4 verified; A4 fails the stop
  rule); Sam chose per-collection indexes; Builder, plan-mode and cold-start
  design filled in.
- 2026-10-09: Gate round 1 — BLOCKED (2 Critical, 9 Significant, 2 ship-blocker(s)); commit `c59e89e7d`; critique `nexus_rdr/227-gate-critique-2026-10-09-r1`.
- 2026-10-09: Round 1 fixes: restart-bound admin credentials, reconciler
  lifecycle (trigger, sweep, hysteresis, advisory lock, failed versus in-flight),
  routing table with `ef_search = 1000` as the no-index route, status object,
  Alternative 4 restated, RDR-226 abandoned (research `-4`, `-5`).
- 2026-10-09: Gate round 2 — PASSED (0 Critical, 2 Significant, 0 ship-blocker(s)); commit `809a7dea8`; critique `nexus_rdr/227-gate-critique-2026-10-09-r2`.
- 2026-10-09: Accepted. Post-accept fold of the round-2 Significants and the fix
  check's counted defects: release split into Phase 2a/2b, read half of the sweep
  on every engine, own lock key, timeouts and backoff, predicate-based mapping,
  registry-based retirement, migrator cancels builds, like-for-like comparison
  (research `-6`).
- 2026-10-09: Post-accept fix-check fix: builds driven by the reconciler's row
  counts against a build threshold B independent of T (the probe-driven trigger
  could not build collections below T), COPY rename keeps its index, counts in an
  explicit transaction, migrator terminates builder sessions, per-boot builder
  name, status semantics (research `-7`).
