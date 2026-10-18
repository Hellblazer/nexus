---
title: "Per-Collection HNSW Indexes Above the Router Threshold"
id: RDR-227
type: Architecture
status: closed
closed_date: 2026-10-12
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

Drafted 2026-10-09 against develop `ebd9c4deb`. When drafted, no product code had
changed.

**Status at close (2026-10-12).** Product code has since shipped: Phase 2a in
engine-service-v0.1.157, Phase 2b in engine-service-v0.1.158, and the router
default of 20000 in engine-service-v0.1.159 (Revision History). What each gap
reads now is under "Enumerated gaps to close". In short: Gap 1 and Gap 2 are
closed by measurement for a collection that holds a valid per-collection index;
Gap 3 is not closed; and the end state, T = B = 20000 with no margin, serves a
collection above 20000 rows from the `ef_search = 1000` leaf walk on every path
that lacks its index (Failure Modes).

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

*Closing note (2026-10-12): these are the gaps as drafted. Gap 1 and Gap 2 are
closed where a valid per-collection index exists (Gap 1: T2
`nexus_rdr/227-research-12` and `-13`; Gap 2: `-13`). **Gap 3 is not closed** (see
its status below). The text of each gap is left as drafted.*

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
27,893 puts `code__1-72` below the Gap 1 target (A4). A T between 27,893 and 29,355
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

**Status, 2026-10-12: Gap 3 is NOT closed. It is a residual.**

- **No mitigation shipped.** Sam deferred `pg_prewarm` on 2026-10-09
  (`nexus_rdr/227-prewarm-decision`; A6 below). The bead that would carry it,
  nexus-43ulx.26, is in state deferred and stays deferred as the carrier of the
  design. No changeset, no bundle change and no prewarm statement exist.
- **Reopen trigger, as recorded** (`nexus_rdr/227-prewarm-decision`, bead
  nexus-43ulx.26 comment of 2026-10-09 22:02): after the Phase 2b deploy, the
  cloud's `top_statements` show a material first-search penalty on a `pci_` index
  after an engine or database restart. The record says to carry that number.
- **The trigger has not been exercised.** The only post-deploy measurement,
  `nexus_rdr/227-research-13`, ran 4 min 53 s after `service_started` on an engine
  redeploy (`max_hnsw_statement` 25, 40 and 50 ms, minimum, median, maximum).
  The record describes an engine restart and records no restart of the database,
  so it says nothing about the cost after a database restart or a failover. The
  from-disk first touch of a partial index is unmeasured (Phase 0 measured 86 to
  189 ms with the OS page cache intact; `nexus_rdr/227-research-9` did not measure
  a cold-cache first search).
- **What is and is not covered.** A search warms only its own walk. A database
  failover without an engine restart is outside the design, as the deferred bead
  states.

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
  Design, Builder). Leaf ownership checked in Phase 1 Step 2
  (`nexus_rdr/227-research-10`, 2026-10-09): `nexus_admin` owns all 24 chunks
  leaves on the managed instance. **Method**: Source read plus deployment check.
- [deferred: nexus-43ulx.26] **A6.** `pg_prewarm` is available on the managed instance and in the local
  PG bundle, and prewarming a per-collection index makes its first search warm.
  **Status**: ❌ Not held as stated (Phase 1 spike, `nexus_rdr/227-research-9`,
  2026-10-09): on the managed instance `pg_prewarm` 1.2 is available but not
  installed; the local bundle does not ship it (`build_pg_bundle.sh` builds only
  `pg_trgm`). With the OS page cache warm, prewarming a 195 MB partial index cut
  the first search from about 45 to about 35 ms when the index fit in
  `shared_buffers` (512 MB), and by about 4 ms at the local default of 128 MB; a
  cold-cache first search was not measured. Sam, 2026-10-09: prewarm deferred
  (`nexus_rdr/227-prewarm-decision`): no `CREATE EXTENSION` changeset and no
  bundle change in this RDR. **Closing state (2026-10-12)**: not verified as
  stated and not pursued; bead nexus-43ulx.26 stays deferred as the carrier, with
  the reopen trigger recorded under Gap 3. **Method**: Spike (Phase 1).

## Proposed Solution

### Approach

For each collection of at least B rows (Lifecycle), keep a partial HNSW index on
its leaf restricted to that collection. A one-collection arm on such a collection then walks a graph of
its own rows only: no collection filter, no crowd-out, HNSW speed (the
live-row check and any caller filter still apply). The router sends an arm exact
at or below T, and to the collection's own index above it. A collection above T
without a valid index, during its build or after a failed one, takes the leaf
walk at `ef_search = 1000`.

### Technical Design

- **Index.** `CREATE INDEX CONCURRENTLY <name> ON <leaf> USING hnsw
  (embedding_<dim> nexus.vector_cosine_ops) WHERE collection = '<collection>'`, with the
  name `pci_` plus a hash of (model, tenant, collection), to fit the 63-byte
  identifier limit (Index name, below). The operator class is schema-qualified:
  pgvector is installed in the `nexus` schema, which the builder's admin session
  does not have on its `search_path`, so the bare name does not resolve. Build parameters match the
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
  **Update, 2026-10-12.** The prerequisite was asked and answered.
  - *Answer* (`nexus_rdr/227-research-10`, conexus-7a, 2026-10-09): the all-in-one
    rotation (`deploy/migrate/bootstrap-engine-db.sh`, which covers `nexus_admin`,
    `nexus_svc` and `NX_SERVICE_TOKEN`) restarts the engine. The targeted
    single-role `nexus_admin` rotation (conexus-cdli, last used 2026-08-25) does
    not: it writes the secret, runs `ALTER ROLE` and re-renders the engine's env
    file. With credentials read once at boot, a targeted rotation would leave
    builds failing authentication until the next restart.
  - *Fix, in the conexus repository.* conexus filed conexus-jmxp (P1) for a
    mandatory engine recreate. As reported by conexus-01 on 2026-10-12, it is done
    and closed (2026-10-09). The targeted `nexus_admin` path of conexus's
    `engine-credential-rotation` skill (`.claude/skills/engine-credential-rotation/SKILL.md`
    in that repository) has a mandatory step 5: a same-tag engine recreate through
    the engine-redeploy document, not a `docker restart` (the TLS sidecar is
    included), then a `builder_state` check. It merged as conexus PR 500; conexus
    PR 507 then recorded engine-service-v0.1.158 as the first builder-carrying tag
    and the shipped status field names. This is a runbook step in a skill. No
    script enforces it.
  - *Never exercised against a live builder.* conexus-01 reports that no targeted
    `nexus_admin` rotation has happened since the builder went live on 2026-10-10;
    the engine's database secret was last changed on 2026-09-05 (Secrets Manager
    metadata, read 2026-10-12, no value read). Not checked: the role's password
    change time on the database itself.
  - *If the step is skipped*, builds and drops stop. Three places show it,
    each read in the code on 2026-10-12: the `builder_state` field of the status
    object reads `auth_failed` (set at `PciBuilderSession.java:205`; the status reads the
    builder's live state, `PciReconciler.java:130`);
    the "Per-collection indexes" row of `nx doctor` fails hard on `auth_failed`
    (`src/nexus/health.py:4474`); and the engine logs
    `event=pci_builder_auth_failed` at WARN on every pass, carrying the SQLSTATE and
    a hint to restart (`PciBuilderSession.java:206`). Existing valid indexes keep
    routing, because the read half runs as `nexus_svc`, but a collection that
    crosses B meanwhile takes the `ef_search = 1000` walk (Failure Modes).
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
    this job, because it runs only on a search, only when T > 0, and flags
    nothing at or below T (`nexus_rdr/227-research-7`).
  - *Sweep, read half.* Every engine, with no lock, once after boot and every
    10 minutes (`NX_SEARCH_PCI_SWEEP_SECONDS`, an integer from 60 to 3600): enumerate the
    per-collection indexes on every leaf by name prefix `pci_` in
    `pg_class`/`pg_index` (catalogs are not row-secured), parse each index's
    collection from its deparsed predicate (`pg_get_expr(indpred, indrelid)`,
    which reads `(collection = '<name>'::text)`), and replace the router's
    valid-index set with the `indisvalid` ones. Until the first read the set is
    empty. The read half runs on its own pooled connection as `nexus_svc`, which
    can read the catalogs, and it runs whatever `NX_SEARCH_PCI` says. The read is
    bounded: it runs under the engine's 30 s sweep statement bound
    (`nexus_rdr/227-batch-a-fix-round`), and the wait for the read lock that
    serialises it with the builder's post-change refresh is bounded by the same
    time, so a stuck read never stalls the builder. A failed read keeps the
    previous set, and the set **expires**: with no successful read for three
    periods plus the read bound (`3 * NX_SEARCH_PCI_SWEEP_SECONDS + 30 s`) it
    answers as empty, so every statement walks at `ef_search = 1000` (the safe
    direction) rather than routing on a frozen set that may list a dropped index.
    `event=pci_sweep_set_expired` logs once and `event=pci_sweep_set_recovered` at
    the next good read. The status object's `expired` says so (Day 2 Operations).
  - *Sweep, DDL half.* Only the lock holder (One builder, below). For each leaf
    it resolves the leaf's tenant from the partition bound, then, in one explicit
    transaction on the admin session (`SET LOCAL` has no effect outside one,
    `nexus_rdr/227-research-7`), sets `SET LOCAL nexus.tenant` to that tenant and
    counts rows per collection on the leaf. The owner role does NOT see only
    that tenant's chunk rows: `vectors-029` added `chunks_gate_probe_owner_read`,
    a permissive `SELECT ... USING (true)` policy on `nexus.chunks` for the
    migrating role (`nexus_admin` in production, the builder's role), so the
    admin session reads every tenant's chunks whatever `nexus.tenant` says
    (`nexus_rdr/227-research-11`, finding A; this corrects the earlier reading
    in `nexus_rdr/227-research-5`). Counting is per-tenant because the count
    carries an explicit `tenant_id` predicate, and the `current_setting` check
    cannot catch a count that omits it. `catalog_collections` has no such policy:
    it is FORCE ROW LEVEL SECURITY to the owner, so a registry read under the
    wrong tenant is an empty list, which is why the read also carries its own
    `tenant_id` predicate (a superuser or BYPASSRLS admin role is not filtered).
    Each count runs under a 30 s statement timeout, per statement and not per leaf
    (`set_config('statement_timeout', ...)`; PostgreSQL restarts the clock at each
    statement). Measured on local NVMe (`nexus_rdr/227-research-11`): the worst
    single count took 0.55 s cold, and a whole leaf's counting transaction took
    11 to 20 s cold, about 45 s projected at 200 saturating collections, so the
    transaction as a whole is not bounded at 30 s. A per-leaf bound (PostgreSQL 17
    `transaction_timeout`, 300 s proposed) is not built. The registry is read under
    the same tenant. The pass then drops by the rules below and builds the missing
    indexes. Building and dropping run outside that transaction, in autocommit.
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

  *As shipped (2026-10-12):* T = B = 20000 (engine-service-v0.1.159), so the
  "below B" case cannot occur at the default and the third row is the walk for a
  collection that crossed 20000 and has no valid index yet. Failure Modes lists
  every path into that row and what the walk recalls.

  A statement over several collections cannot use a partial index (its
  predicate does not imply one collection) and keeps today's leaf walk.

  The set is refreshed after each local build **and each local drop**; a peer
  engine converges at its next read, within `NX_SEARCH_PCI_SWEEP_SECONDS`. The
  staleness windows, by direction:
  - *After a build*, a peer does not see the new index until its next read (up
    to one period), so it walks that collection at `ef_search = 1000`: latency
    only.
  - *After a drop*, a peer still lists the index for up to one period plus the
    30 s read bound, so it routes the collection to the serving `ef_search` with
    no per-collection graph behind it. That is harmful only when the drop is not a
    count drop (an index superseded, or dropped by an operator) or when T is below
    half of B; after a count drop the collection holds under B/2 rows and a router
    that is on sends it to exact.
  - *With no successful read at all*, the set expires to empty after three
    periods plus the read bound (Lifecycle, sweep, read half).
  The setting's maximum, 3600 s, bounds the first two: it was lowered from 86400
  in the change that added the read half because the period is also how long a
  peer can route on a stale answer.
- **Plan mode.** The plain-search arm reaches a per-collection index through
  `runPlainSearchStatement`'s GUC batch, which sets
  `plan_cache_mode = force_custom_plan` (`PgSession.setSearchPlanCacheMode`, called
  from `runPlainSearchStatement` in `PgVectorRepository.java`;
  `HnswServingGucParityTest` pins that every site setting `hnsw.iterative_scan`
  also sets the plan mode); a generic plan skips the partial index and walks
  every tenant leaf (A1). The sampled check below binds `p_collections` the way
  the arm does, since a literal can prove the predicate where a bound array
  cannot. An EXPLAIN pin guards it in tests, and in
  production a sampled arm on an indexed collection runs `EXPLAIN` first and
  logs `event=pci_plan_check used=<bool>`, so a silent fall back to the leaf walk
  shows up. Sampling is per index, not only a global counter (Day 2 Operations).
- **Cold start.** A per-collection index's first touch after a restart was 86 to
  189 ms with the OS cache intact (a lower bound); from disk it should be of the
  order of the leaf index's 2 to 7 s (not measured for a partial index). A
  `pg_prewarm` step by the lock holder on each valid index was designed here and
  is **deferred** (Sam, 2026-10-09, `nexus_rdr/227-prewarm-decision`; A6): the
  managed instance has the extension available but not installed, the local
  bundle does not ship it, and the measured warm-cache benefit is about 10 ms
  (about 4 ms at the local 128 MB default). There is no `CREATE EXTENSION`
  changeset, no bundle change, and no prewarm statement in the builder. Reopen
  only on a cloud number: after the Phase 2b deploy, a material first-search
  penalty on a `pci_` index after a restart, read from `top_statements`. One
  search warms only its own walk, not the graph, and a database failover without
  an engine restart is not covered; both stay Gap 3 residuals. (2026-10-12: the
  deferral stands and the reopen trigger has not been exercised; see Gap 3.)
- **Schema.** No Liquibase changeset creates these indexes; they are runtime
  objects keyed on data. The reconciler needs no registry table: the `pci_`
  prefix and each index's predicate let `pg_class` and `pg_index` answer which
  exist, for which collection, and which are valid. Only the builder creates
  `pci_` names. Only an index whose name matches `^pci_[0-9a-f]{24}$` (the shape
  the builder makes), whose access method is `hnsw`, whose predicate is exactly
  `(collection = '<name>'::text)` and whose leaf and parent bounds are
  single-value LIST bounds counts as one of its own. Every other index whose name
  starts with `pci_` (an operator-made `pci_foo`, a valid `REINDEX CONCURRENTLY`
  product `pci_<hash>_ccnew`, a non-`hnsw` index, a hand-edited predicate, a
  PostgreSQL that deparses differently) is **unparsed**: counted in the status
  object, never routed to, never built over, never dropped. This extends Sam's
  decision 4 (`nexus_rdr/227-planner-decisions-confirmed`) and replaces the earlier
  text that treated an operator-made `pci_` index like the builder's own. One
  carve-out (Sam, 2026-10-18, `nexus_rdr/227-decision-4-amendment-reindex-leftovers`,
  nexus-rn1yd): an index named `^pci_[0-9a-f]{24}_cc(new|old)[0-9]*$` that is
  `hnsw`, passes every other condition above and has `indisvalid` false is a
  **`REINDEX CONCURRENTLY` leftover** of a builder index. The reconciler drops it
  (`DROP INDEX CONCURRENTLY IF EXISTS`, the drop's existing 5 s lock timeout, reason
  `reindex_leftover`) with no check of `pg_stat_progress_create_index`: a reindex
  still running holds `ShareUpdateExclusiveLock` on the leaf, the drop times out
  against it, and the next pass judges again. Because the snapshot can be a pass
  old, the drop re-reads `pg_index.indisvalid` for that name on its own session
  immediately before the `DROP` and skips it, logging `pci_drop_skipped_stale` and
  not `pci_index_dropped`, when the index is valid now or gone. A leftover is still not one of the
  builder's own for routing: never routed to, never counted against
  `NX_SEARCH_PCI_MAX_PER_LEAF`, never taken as "this collection has an index", and
  counted by the status object in `reindex_leftover` instead of `unparsed`. A valid
  `_ccnew` or `_ccold` is never touched.
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
crossing of T. Not pursued: A1 held, and RDR-225 rejected the wider form of this layout.

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
- A background index build after a collection reaches B. While B < T the
  collection keeps routing exact during the build; above T it takes the
  `ef_search = 1000` leaf walk until its index is valid.
  **Correction, 2026-10-12.** The B < T case above is the configuration this RDR
  planned through Phase 2b (B 20000, T 30000). It is no longer the configuration:
  since engine-service-v0.1.159 the code default is T = B = 20000 (cloud since
  2026-10-10, first through a runtime setting). With T = B there is no exact window:
  a collection takes the `ef_search = 1000` walk from the moment its probed count
  passes 20000 until its index is valid. Failure Modes lists the paths and the
  measured recall.
- Insert cost into an indexed collection (a second HNSW insert per row) was not
  measured; Phase 1 measures it on the substrate, bulk load included.
  **Measured since (2026-10-12), by two methods that disagree in size and have
  not been reconciled.**
  - *Substrate, chas, through the engine's write path*
    (`nexus_rdr/227-research-11`, bead nexus-43ulx.30): `POST
    /v1/catalog/manifest/write_many`, client wall time per request, JSON encoding
    included, one collection alternated between indexed and unindexed rounds as it
    grew from 25,000 to about 33,000 rows. Single-row p50 rose from 6.8 to 7.1 ms
    to 9.7 to 10.3 ms (+43%); a 128-chunk batch took 1.8 to 1.9 times as long, and
    bulk throughput fell from about 350 to about 185 rows per second.
  - *Fork, production-shaped data* (`nexus_rdr/227-research-12`, bead
    nexus-43ulx.31): server-side statement time, one row every 0.2 s, from an
    engine about 90 ms from the database. p50 1.76 and 0.86 ms on indexed
    `code__1-72` against 1.21 and 1.02 ms on unindexed `code__1-3` in the same leaf;
    p95 3.7 to 4.6 against 3.4 to 3.6 ms; the record calls it inside run-to-run
    noise. 128-chunk batches were not measured on the fork.
  - *Reading.* The two measure different things (request wall time against
    statement time) at different batch sizes, so the single-row +43% and "inside
    noise" do not directly contradict each other, and neither run explains the
    other. The bulk figure
    (roughly half the throughput) comes from the first run alone and bears on the
    indexing path of every large indexed collection; no real indexing run was
    timed with and without an index.
- The long-lived engine process now keeps the admin credentials it read at boot
  and opens an admin session, where before it closed the admin pool after
  migration. That session runs a fixed statement set and nothing else: its own
  session settings (`application_name`, `statement_timeout`, `lock_timeout`),
  the advisory lock, catalog reads and leaf resolution, per-tenant row counts and
  registry reads inside a transaction under `SET LOCAL nexus.tenant` for the leaf
  it is reconciling, `CREATE INDEX CONCURRENTLY IF NOT EXISTS` and
  `DROP INDEX CONCURRENTLY IF EXISTS` on `pci_` names it derived or read. The
  collection is `%L`-quoted. This is a deliberate
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
- **Cold first touch** (Gap 3). Not mitigated in this RDR: `pg_prewarm` is
  deferred (A6, `nexus_rdr/227-prewarm-decision`), with a stated trigger to
  reopen it. The trigger has not been exercised (Gap 3, status).

### Failure Modes

A missing, invalid or unused per-collection index degrades to the leaf walk,
never to an error. A failed build leaves an invalid index that the builder holding
the lock drops by name; the next pass rebuilds it. A stale admin password stops builds and
drops, visibly (`builder_state`), until the engine restarts.

**The fallback walk against the Gap 1 target, as shipped (2026-10-12).** The
paragraph above says the walk is not an error. It is also below the Gap 1 target
of 0.99. Since engine-service-v0.1.159 the router default T equals B (both 20000,
`PgSession.java:771`, whose javadoc says "There is no margin between the two
thresholds"), so a collection above 20000 rows without a valid index of its own
is served by the shared index at `ef_search = 1000`, not by an exact scan.
Single-collection arms reach that walk on these paths:

- A collection crosses 20000 and its index is not yet built: until the next DDL
  pass, up to 600 s at the default `NX_SEARCH_PCI_SWEEP_SECONDS` (the first pass
  after a boot starts within 60 s), plus the build itself (9 to 21 s per index on
  the fork, `nexus_rdr/227-research-12`; about 13 s for 30,000 rows and 44 s for
  60,000 locally, `nexus_rdr/227-research-11`).
- A boot, between startup and the first sweep read, while the router's set is empty.
  Measured once: about 10 ms at the 2026-10-10 14:54 boot (`nexus_rdr/227-research-13`).
- The router's set has expired: no successful read for `3 * NX_SEARCH_PCI_SWEEP_SECONDS
  + 30 s`, so it answers empty (Lifecycle, sweep read half).
- A build in flight, or a failed build: the index is invalid until it finishes, and
  a failed one is retried no sooner than 10 minutes later, doubling to 24 hours.
- The leaf is at `NX_SEARCH_PCI_MAX_PER_LEAF`, or `NX_SEARCH_PCI=0`.
- An install whose builder cannot run: no admin credentials and an app role
  without the privilege (`builder_state = no_privilege`), or a stale admin
  password (`auth_failed`). This path is permanent until fixed, and it is the
  one where the default change matters: under v0.1.158 with T = 30000 a
  collection of 20,000 to 30,000 rows was served by an exact scan (the reference
  that recall is measured against), and under v0.1.159 it is served by the walk.
  The release note (`nexus_rdr/227-v0159-release-note`) and `docs/configuration.md`
  (`NX_SEARCH_EXACT_MAX_ROWS`) say so. Local installs are not in this class: the
  local daemon passes `NX_DB_ADMIN_*` and local builds finish (60,000 rows in
  44 s at 64 MB `maintenance_work_mem`, `nexus_rdr/227-research-11`).

*Recall of that walk.* The measured worst case is 0.975 on `code__1-72` (27,893
rows) for the nqsa7 query, with no top-1 miss (`nexus_rdr/227-research-12`, comparison
arm "no index, ef_search 1000"; the Phase 0 A4 run, `nexus_rdr/227-research-2`).
The records disagree on the k: the release note places 0.975 at k 120, while
A4 above records 0.975 at k 40 and 0.983 at k 120, and research-12 records
0.975 as the minimum with nqsa7 below 0.99 at every k. All three put it under
the 0.99 target. It is a worst case over
three collections that were measured, not a bound: `code__1-1` and `code__1-2`
reached the target at `ef_search = 1000` (mean 0.998 to 0.999), `code__1-20` read 1.0
on 20 own-topic queries (T2 `conexus/rdr227-step3b-fork-2026-10-10`, M4(b)), and a collection of another shape crossing
20000 has not been measured. Prose collections at `ef_search = 1000` without an
index read 0.9833 (`docs__1-2`) and 0.975 (`rdr__1-79`), but both sit below 20000
and are routed exact as of 2026-10-10. A prose collection that grows past 20000
leaves that exception (Phase 2b, Prose, correction of 2026-10-12).

*Remedy for an operator who wants such collections exact.* Set
`NX_SEARCH_EXACT_MAX_ROWS=30000`, which restores the v0.1.158 routing (release note;
`docs/configuration.md`). A collection between B and T then keeps routing exact
while its index builds (the B < T case in Consequences). The cloud has an
external constraint on that value: conexus's STEP-6 recall leg goes red if the
cloud's T rises above 28595, the row count of `gate-xr789` `code__1-20`, which
the router would then send exact and leave the gate without HNSW coverage
(`nexus_rdr/227-continuation`, `nexus_rdr/227-v0159-release-note`, bead
nexus-43ulx.35 comment of 2026-10-10 15:57). conexus-7a is told before any change
to the cloud's T, a rollback to 30000 included.

## Implementation Plan

### Prerequisites

- [x] A1 to A4 measured (Phase 0, 2026-10-09).
- [x] A5: the cloud engine's admin credentials confirmed present after boot
  (conexus-1c, 2026-10-09).
- [x] conexus's `nexus_admin` rotation procedure restarts the engine
  (offered by conexus-1c, 2026-10-09; confirmation pending). Done 2026-10-09 as a
  mandatory runbook step in conexus's skill, reported by conexus-01 on 2026-10-12;
  no script enforces it and it has not yet been used against a live builder
  (Credentials are restart-bound, update).
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
and the migrator's termination of `nexus-pci-builder-` backends. The prewarm
is deferred (A6).
Step 3: substrate measurements: insert and bulk-load cost into an indexed
collection, build time with the Builder's timeouts, local build time at the local
bundle's `maintenance_work_mem`, and recall at the Gap 1 target on own-topic
queries for `code__1-20`, on a prose collection and on a second tenant. Tests: the planner's choice (EXPLAIN pin), recall
against exact on a seeded leaf with a large collection beside larger siblings,
and the degraded paths (no index, invalid index).
*Done (2026-10-09 to 2026-10-10).* Step 3a, substrate measurements on chas: insert
and bulk-load cost, local build time, counting cost (`nexus_rdr/227-research-11`,
bead nexus-43ulx.30). Step 3b, a production fork run by conexus-7a: recall with the
index, build time, counting cost, prose and second tenant (`nexus_rdr/227-research-12`,
bead nexus-43ulx.31); two items were not measured there (live `pci_plan_check`,
anything under concurrent fan-out load), and in-region counting time was measured
later on the cloud (bead nexus-43ulx.33).

### Phase 2: Release

- **Phase 2a.** One engine cut with Step 1 only (`ef_search = 1000` above T, T
  stays 30000). Measure a warm default search live on it, the same way the
  v0.1.154 to v0.1.156 numbers were taken.
  *Done (2026-10-09).* Shipped as engine-service-v0.1.157, live 19:41:29Z; 16 warm
  default searches measured 19:51:03 to 19:51:40Z with published client 7.76.0
  (`nexus_rdr/227-research-8`, bead nexus-43ulx.6). The criterion held in 14 of 16
  searches, and Sam decided GO for Step 2 (`nexus_rdr/227-phase2b-decision`, bead
  nexus-43ulx.7).
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

  **Gate as planned, above, versus gate as applied (2026-10-12).** The text above
  is kept as what was planned: Step 3's recall runs on `code__1-20` own-topic, a
  prose collection and a second tenant must each pass the Gap 1 target. That is
  not the gate that was applied. Bead nexus-43ulx.34, written at planning, carries a
  different gate (every collection in (B, T], whatever its kind, but no prose or second-tenant run), and the pre-change check and Sam's go were made
  against the bead's gate. The T2 records and bead comments reviewed for this
  fold show no separate decision to set the RDR's three runs aside; this section
  is where the substitution is recorded.

  - *Gate applied* (bead nexus-43ulx.34 description and comments of 2026-10-10):
    every live collection with a count in (B, T] holds a valid `pci_` index and met
    recall >= 0.99 at k 40 to 120 with that index, in Step 3b or a follow-up fork
    run; if any fails, T stays 30000.
  - *Collections it covered* (conexus-7a, production, physical row counts,
    2026-10-10): exactly three in (20000, 30000], each with a valid index: nexus
    `code__1-20` (29,355 rows; fork recall 1.0 at every k on 20 own-topic queries),
    nexus `code__1-72` (27,893; minimum 0.9917, the nqsa7 query at k 120 only, no
    top-1 miss) and `gate-xr789` `code__1-20` (28,595). Above 30000 were only nexus
    `code__1-1` and `code__1-2`, both indexed (`nexus_rdr/227-research-12`, `-13`).
  - *Prose.* No prose collection reaches 20000 (`docs__1-2` 10,308 rows,
    `rdr__1-79` 14,300). The prose run was made on a second fork boot at B = T =
    10000. With the index, `docs__1-2` and `rdr__1-79` both read a minimum of
    0.9833 at the tail (means 0.9992), under 0.99. The applied gate did not
    require them to pass: they sit below B, where the router sends them to an
    exact scan, so they are outside (B, T]. Both records warn that they matter only
    if B and T are ever lowered to 10000, and bead nexus-43ulx.34 comment of
    2026-10-10 09:09 says not to lower T below 20000 on this evidence.
    **Correction, 2026-10-12 (close critique round 2).** That inference covers
    the thresholds, not growth. The exception holds only while no prose
    collection is at or above B. The row counts above are a dated reading and
    nothing checks them. When a prose collection passes 20000 rows it is built
    and routed to its own index, and is then served at about the recall
    measured here, or by the `ef_search = 1000` walk (0.975 on `rdr__1-79`)
    until its build finishes. `rdr__1-79` at 14,300 rows is the nearest case.
    Prose index recall at 20000 rows or more was not measured: the 0.9833 was
    read at 10,308 and 14,300 rows. A prose collection reaching B is the
    trigger to run the Gap 1 recall check on it.
  - *Second tenant.* The run was `gate-xr789` `code__1-20`, 20 queries sampled from
    its own stored embeddings, recall 1.0 at every k. The record states this is
    document-to-document, so 1.0 is expected, and calls it weak evidence. A
    second-tenant own-topic run with real query vectors was not made.
  - *Margin.* With T = B there is no margin. The trade was stated to Sam (Batch C
    critique S7, bead nexus-43ulx.34 comment of 2026-10-10 04:03) and he said to
    proceed ("go on the lowering of the threshold", about 12:50Z; then "proceed"
    on the no-margin trade, recorded 13:06). The bead reads that as "accept and
    record"; this section is the record.
  - *Result.* The cloud's T went from 30000 to 20000 on 2026-10-10: boot with
    `search_exact_router max_rows=20000` at 14:54:40Z on engine-service-v0.1.158,
    through a runtime setting conexus added (`NX_SEARCH_EXACT_MAX_ROWS`; conexus PR
    513). The measurement is `nexus_rdr/227-research-13`; its figures are under
    Performance Expectations. The code default followed in engine-service-v0.1.159
    (bead nexus-43ulx.35; Sam's go on that was given about 16 minutes after T = 20000
    went live, so the soak period the bead named as a precondition was waived by
    that go).

### Day 2 Operations

The engine's `GET /v1/status` gains a `per_collection_indexes` object, served
from values the engine already holds (the read half's last catalog read and the
reconciler's cached status); the route runs no catalog query. It has two scopes,
kept in separate places so no reader takes one for the other:

```json
"per_collection_indexes": {
  "valid": 4, "invalid": 0, "unparsed": 0, "reindex_leftover": 0,
  "last_read_at": "2026-10-10T08:00:00Z", "expired": false, "sweep_seconds": 600,
  "this_engine": {
    "builder_state": "ok", "building": 0, "failing": 0,
    "last_ddl_pass_at": "2026-10-10T07:30:00Z",
    "pass_started_at": null, "pass_in_progress": false
  }
}
```

A standby engine, and an engine whose admin credentials were refused, read:

```json
"this_engine": {
  "builder_state": "standby", "building": null, "failing": null,
  "last_ddl_pass_at": "2026-10-10T07:30:00Z",
  "pass_started_at": null, "pass_in_progress": false
}
"this_engine": {
  "builder_state": "auth_failed", "building": null, "failing": null,
  "last_ddl_pass_at": null, "pass_started_at": null, "pass_in_progress": false
}
```

- *Global* (the read half's last read; the same on every engine, up to when each
  last read): `valid`; `invalid` (a failed or in-flight build); `unparsed`
  (Schema: counted, never routed, built or dropped); `reindex_leftover` (invalid
  `_ccnew`/`_ccold` leftovers the reconciler drops, nexus-rn1yd; not also in
  `unparsed`); `last_read_at` (null before
  the first read); and `expired`, true when the set is older than three periods
  plus the read bound and the router is answering as empty, so an operator can
  see a frozen set. The counts stay the last read's while `expired`.
  `sweep_seconds` is `NX_SEARCH_PCI_SWEEP_SECONDS`, the period of the passes, so a
  client can judge how old `last_ddl_pass_at` may be before it is a stall (`nx
  doctor` warns at three periods plus 15 minutes).
- *This engine* (`this_engine`): `builder_state`, one of `ok`, `auth_failed`,
  `no_privilege`, `off` and `standby` (`standby`: a peer holds the builder lock;
  `no_privilege` and `auth_failed` are shown while they last, not only in the log);
  `building`, 0 or 1, the lock holder's own build in flight; `failing`, the
  collections on this engine with three or more consecutive failed builds;
  `last_ddl_pass_at`, when this engine's last pass that held the lock, ran to
  its end and counted at least one leaf finished (a pass that skipped every leaf
  on a count timeout or a failed read does not move it); and `pass_started_at`
  with `pass_in_progress`, the pass in flight, so a long first pass (several
  serial builds) is visible while `last_ddl_pass_at` is still null.
  `building`, `failing` and `pass_started_at` are **null on an engine that does
  not hold the lock** (`pass_in_progress` is false there): building and failing
  are the holder's own state, and another role's `pg_stat_progress_create_index`
  rows are not visible, so a count of builds in flight across engines is not
  reported. `last_ddl_pass_at` is this engine's own history and stays after the
  engine becomes a standby; it is null only before the engine's first completed
  pass. This replaces the earlier design of a global building count read from
  `pg_stat_progress_create_index` (`nexus_rdr/227-fix-check-ca4ca8962`). A
  non-holder reports `standby` (or `off`, or `auth_failed`) with those nulls.

The counts are global, not per tenant, like the existing `reaper` object on the
same unauthenticated route. The object is additive and goes in the wire ledger;
a client against an engine that predates it finds no key and reports
not-applicable. Each sweep also logs `event=pci_sweep valid= invalid= unparsed= reindex_leftover=`,
and `event=pci_sweep_set_changed added_count= removed_count= added= removed=` when
the router's set changed between two reads (`leaf:collection:index` entries, cut at
20 with the exact counts; the first read after a boot lists what the boot found, and
a read that changed nothing logs nothing). The DDL half's
`event=pci_reconcile_pass` line carries `builder_state=`, `failing=`,
`sum_count_ms=` and `max_count_ms=`, the sum and the longest of the per-leaf
counting transactions of the pass, which is the figure to read against the 30 s
count timeout; the per-leaf `event=pci_count_done` stays at DEBUG (one line per
leaf per pass is a lot at 20,000 collections). An authentication failure logs `event=pci_builder_auth_failed` on
every pass until it clears. `nx doctor` reads the object over HTTP, so it works on
a managed install as well as a local one.

**Who builds, and what a peer shows.** Only the engine holding the builder
advisory lock builds or drops, but the lock is held per pass (seconds, except
while a long build runs), and every engine runs its own pass each period. So with
more than one engine **every engine normally reads `builder_state = ok`**, and
`standby` is transient: an engine reads it when its pass found the lock held by a
peer's overlapping pass, which is most likely during the first DDL pass after a
deploy, when the holder builds serially for minutes to hours. Read every engine:
expect `ok` everywhere in steady state, treat a `standby` that persists as a peer
holding a long pass, and remember that `building`, `failing` and `pass_started_at`
are per engine. A standby engine still refreshes its router set from its own read
half, and its `valid` converges to the holder's within `NX_SEARCH_PCI_SWEEP_SECONDS`
of a build or drop. The global `invalid` can be a peer's build in flight, which a
standby cannot tell from a failed one, so `nx doctor` passes a standby with `invalid`
above zero when its read is fresh and says so. Doctor warns on the holder when a
pass has been in flight for more than an hour, and when `builder_state = ok` with
no completed pass for three periods plus 15 minutes. After a `nexus_admin`
rotation and an engine restart, check `builder_state = ok`, a `last_ddl_pass_at`
later than the restart on the engine you restarted, and no
`event=pci_builder_auth_failed` since `service_started`. A status answers for the
engine the load balancer chose: to check a rotation, address the restarted engine,
not the balancer.

**The plan check samples per index.** A sampled indexed arm runs `EXPLAIN` on its
own statement and logs `event=pci_plan_check used=<bool> index= collection=`. An
arm is sampled when (1) it is the first eligible arm for an index after the router's
set gained it (a boot, a build, a rebuild after a drop), (2) it is the first for
that index in the last 15 minutes, or (3) it is the 1000th eligible arm counted
across all indexes. The 1-in-1000 counter is **process-global**, so a busy
collection takes most of its samples and a quiet one almost none; the per-index
triggers are what cover the quiet ones, and after a deploy there is one line per
index within the first request that reaches it. `used=false` logs at WARN with
`top=` (the plan's first node) and `scans=` (the scans the planner chose, with the
index names), so the cause is in the line. The sample's own time (a savepoint, the
`EXPLAIN` and a release) is not counted as the arm's statement time.

**Retry is per engine.** The retry state (the 10 minute to 24 hour backoff and
the `failing` count) is in memory on the engine that ran the build and is lost on
restart. The builder lock serialises passes but is held per pass, so with more
than one engine the holder alternates. A failed concurrent build leaves an
invalid index that must be dropped before the name can be built again, and a peer
with no failure entry drops it and rebuilds on its first pass after the drop.
Across N engines a failing build is therefore retried at most once per sweep
period by whichever engine holds the lock: bounded, but not backed off
exponentially, and `failing` is per engine, so read it on every engine. A build
ended because a schema migration or the engine's shutdown terminated the builder
session (or the network dropped) is not a failure and is not charged. The
reconciler's first pass starts after a random delay of up to 60 seconds or a
tenth of the period, whichever is shorter.

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
  populated collection) is logged and never drops an index. Under the admin role,
  which reads every tenant's chunks, two tenants with the same collection name are
  counted separately, and a leaf's registry never lists another tenant's row.
- Invalid index: a failed concurrent build is dropped by name and rebuilt;
  routing ignores it meanwhile (the EXPLAIN pin also asserts `indisvalid`).
- Tenant targeting: two tenants with the same collection name each get an index
  on their own leaf.
- Two reconcilers: two engines against one database build each index once.
- A pass during a build: the second engine skips; the build's index is not
  dropped.
- Rename: after a canonical rename the old index is dropped and the new name is
  built on the next DDL pass.
- Hysteresis: a collection between B/2 and B keeps its index.
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
- Status: the `per_collection_indexes` object reports the read half's counts,
  `expired` and `last_read_at` globally, and `builder_state`, `building`,
  `failing`, `last_ddl_pass_at`, `pass_started_at` and `pass_in_progress` for
  this engine; a non-holder reports `standby` with null `building`, `failing`
  and `pass_started_at` and its own `last_ddl_pass_at` (null only before its
  first pass); a role without privilege and a wrong admin password show as
  `no_privilege` and `auth_failed` while they last; the key is absent when no
  source is wired.
- Plan check: the sampled EXPLAIN logs `used=false` when the index is skipped,
  with the plan's top node and the scans it chose; the first arm of each index and
  one arm per index per 15 minutes are sampled besides the 1-in-1000 counter, the
  sample's time stays out of the arm's statement time, and the shipped cadence
  (`production()`) is pinned.

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
**Correction, 2026-10-12.** It is derived in the next paragraph. The warm large
arm's measured maximum there is 50 ms, which is at this bound, not under it.

**Measured after T = 20000 (2026-10-10, `nexus_rdr/227-research-13`).** Default
search, client wall time: mean 1.88 s, median 1.95, range 1.51 to 2.30 (16 timed
searches after one discarded warm-up, 8 fixed queries in 2 passes; recorded 4 min
53 s after `service_started`). The code request's slowest statement
(`max_statement`) had minimum, median and maximum of 263, 325.5 and 425 ms, against
a median of about 800 ms at T = 30000. Indexed HNSW arms (4 on every search,
`ef1000_arms` 0): `max_hnsw_statement` 25, 40 and 50 ms. The first entry of
`top_statements` became an exact arm below B, `code__1-3` (16,088 rows, no index),
at about 330 ms.

*The like-for-like pair* is Phase 2a at T = 30000 (engine-service-v0.1.157,
`nexus_rdr/227-research-8`: mean 2.33 s, median 2.38, range 1.87 to 2.74) against
this run (mean 1.88 s, median 1.95): the same published client 7.76.0, the same 8
queries, one process, sequential, warm. The mean differs by 0.45 s, and the two
ranges overlap between 1.87 and 2.30 s. The earlier figures are not comparable:
engine-service-v0.1.156 read 3.67 s on client 7.75.0, so the client changed with
the engine, and the draft's 2.64 to 3.31 s baseline was also taken on 7.75.0.
*Limits, as the record states them:* 35 searches in total, one sequential client,
no concurrent load from other clients, and a recall check on live of one query
(nqsa7 on `code__1-72`, which returned its true nearest row at distance
0.3054208805 through the index). Fork recall is `nexus_rdr/227-research-12`.
Run 1 of the session (mean 1.83 s) ran 53 s after boot, before the window was
announced to conexus, and is not the recorded result.

## Finalization Gate

### Contradiction Check

Gap 2's cost and Gap 3's cold cost pull in opposite directions. Phase 0
measured cold first touch after a restart (a lower bound); the from-disk cost of
a partial index stays unmeasured and is Gap 3's residual, with `pg_prewarm` (A6)
as the mitigation.
**Correction, 2026-10-12.** The clause naming `pg_prewarm` as the mitigation is no
longer true: Sam deferred it on 2026-10-09 (`nexus_rdr/227-prewarm-decision`), so
Gap 3 has no mitigation in the shipped system and stays a residual (Gap 3, status).

### Assumption Verification

A1 to A4 were verified by the Phase 0 fork spikes on 2026-10-09
(`nexus_rdr/227-research-2`, `-3`); A5 by a source read and a deployment check
the same day (`-4`), with leaf ownership left to Phase 1 Step 2. A6 is open and
is a Phase 1 spike.
**Correction, 2026-10-12.** Leaf ownership was confirmed in Phase 1 Step 2 (all 24
leaves owned by `nexus_admin`, `nexus_rdr/227-research-10`). A6 ran as the Phase 1
spike on 2026-10-09 (`nexus_rdr/227-research-9`, bead nexus-43ulx.9), did not hold
as stated (`pg_prewarm` is available but not installed on the managed instance and
is absent from the local bundle) and is deferred, not open: bead nexus-43ulx.26.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| Partial HNSW index (`USING hnsw ... WHERE`) | pgvector 0.8.2 / PG 17 | Docs |
| Partial-index use through `= ANY(one-element array)` | PostgreSQL 17 planner | Spike (A1, Phase 0): used under `force_custom_plan`, not under a generic plan |
| `pg_prewarm` | PostgreSQL 17 contrib | Spike (A6, Phase 1, 2026-10-09, `nexus_rdr/227-research-9`): available, not installed on the managed instance, not in the local bundle; not used (deferred, nexus-43ulx.26). Was "Assumed (A6), Spike in Phase 1" |
| `CREATE INDEX CONCURRENTLY` on a partition | PostgreSQL 17 | Docs |

### Scope Verification

The Minimum Viable Validation ran in Phase 0 and passed (A1, A2). Phase 1 Step 3
adds the `code__1-20` own-topic, prose and second-tenant recall runs that Phase
2b's lower T depends on.
**Correction, 2026-10-12.** Phase 2b's lower T was applied under the gate in bead
nexus-43ulx.34, not under these three runs as written. `code__1-20` own-topic ran
and passed (1.0 at every k). The prose run ran at B = T = 10000 and read a
minimum of 0.9833 at the tail, under 0.99; it was set aside because prose
collections sat below B and were routed exact on that date. That holds only
until a prose collection reaches B (Phase 2b, Prose). The second-tenant run was 20
self-queries from stored embeddings, which the record calls weak evidence. The
applied gate, the collections it covered and Sam's go are under Phase 2b
(`nexus_rdr/227-research-12`, `nexus_rdr/227-research-13`).

### Cross-Cutting Concerns

- **Tenancy**: indexes are per tenant leaf. Index DDL is not governed by RLS,
  so tenant scoping comes from the leaf the reconciler names. In cloud and local
  installs the role that builds the index is not the serving role (A5); in the
  dev posture without admin credentials it is (Builder, No admin credentials).
  The admin session's row counts are tenant-scoped by `SET LOCAL nexus.tenant`.
- **Local mode**: the same code path applies. The local daemon passes
  `NX_DB_ADMIN_*` to the engine (`storage_service_daemon.py`, the `NX_DB_ADMIN_*` keys of the engine environment), so the
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
- T2 `nexus_rdr/227-research-8` (Phase 2a live measurement),
  `-9` (A6, `pg_prewarm`), `-10` (leaf owners, credential rotation),
  `-11` (substrate: insert, build and counting cost), `-12` (production fork:
  recall with the index, prose, second tenant), `-13` (Phase 2b live
  measurement at T = 20000)
- T2 `nexus_rdr/227-phase2b-decision` (Phase 2a GO, 2026-10-09),
  `nexus_rdr/227-prewarm-decision` (deferral, 2026-10-09),
  `nexus_rdr/227-planner-decisions-confirmed`,
  `nexus_rdr/227-phase2b-release-note` (engine-service-v0.1.158),
  `nexus_rdr/227-v0159-release-note` (engine-service-v0.1.159),
  `nexus_rdr/227-continuation`, `nexus_rdr/227-close-critique-2026-10-12`
- Beads nexus-43ulx (epic, open), nexus-nqsa7; nexus-43ulx.6, .7 (Phase 2a and
  its GO), .8 (credential prerequisites), .26 (`pg_prewarm`, deferred), .33, .34
  (Phase 2b gate and T = 20000), .35 (code default); conexus-jmxp (conexus
  repository, closed 2026-10-09 per conexus-01)

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
- 2026-10-09: Stale-site corrections from fix check `ca4ca8962` (B, not T, as the build
  threshold in the Approach and Consequences; terminate, not cancel, in Step 2;
  B/2 in the hysteresis test; probe wording). Remaining counted items carried to
  Phase 1 planning (`nexus_rdr/227-fix-check-ca4ca8962`).
- 2026-10-09: Phase 2a shipped: engine-service-v0.1.157 (tag created 19:07Z on
  `cf0e09582`, live 19:41:29Z) carried Step 1, `ef_search = 1000` above T with T
  at 30000. Measured 19:51Z with client 7.76.0 (`nexus_rdr/227-research-8`):
  criterion met in 14 of 16 searches.
- 2026-10-09: Phase 2b GO from Sam (`nexus_rdr/227-phase2b-decision`). `pg_prewarm`
  spike (`nexus_rdr/227-research-9`) and `nexus_admin` prerequisites
  (`nexus_rdr/227-research-10`); `pg_prewarm` deferred by Sam
  (`nexus_rdr/227-prewarm-decision`), so Gap 3 stays open.
- 2026-10-10: Status-object amendments (nexus-43ulx.23): opclass is
  `nexus.vector_cosine_ops`; the Schema rule for `pci_` names narrowed to
  `^pci_[0-9a-f]{24}$` on an `hnsw` index with everything else unparsed (extends
  Sam's decision 4); the set is refreshed after builds and drops and peers
  converge within `NX_SEARCH_PCI_SWEEP_SECONDS` (maximum 3600); the sweep's
  bounded read and three-period expiry (`nexus_rdr/227-batch-a-fix-round`) and the
  staleness windows recorded; `pg_prewarm` deferred
  (`nexus_rdr/227-prewarm-decision`); Day 2 Operations rewritten to the served
  object (building from the lock holder only, `standby` added, `expired` added;
  `nexus_rdr/227-fix-check-ca4ca8962`).
- 2026-10-10: Step 3a and 3b measurements (`nexus_rdr/227-research-11`, `-12`).
- 2026-10-10: Phase 2b shipped: engine-service-v0.1.158 (tag created 10:50Z on
  `243e6851e`, live 11:16:44Z) carried Step 2, the reconciler and builder. The first
  DDL pass built 5 indexes with no failures (bead nexus-43ulx.33). T stayed 30000.
- 2026-10-10: the cloud's T moved from 30000 to 20000 on engine-service-v0.1.158
  (Sam's go about 12:50Z; boot at 14:54:40Z through a runtime setting conexus
  added), measured in `nexus_rdr/227-research-13`. The gate applied differs from
  the Phase 2b text (Phase 2b, "Gate as planned versus gate as applied").
- 2026-10-10: the code default `DEFAULT_SEARCH_EXACT_MAX_ROWS` moved from 30000 to
  20000, equal to B, in engine-service-v0.1.159 (tagged 17:00Z on `abc96883c`,
  published 17:17:53Z, live in the cloud 17:34:46Z; pinned by conexus 7.77.0;
  bead nexus-43ulx.35). Rollback is a setting.
- 2026-10-12: Close-time fold (answers `nexus_rdr/227-close-critique-2026-10-12`).
  Gap 3 recorded as not closed; Phase 2b's applied gate recorded beside the
  planned one; the shipped end state (T = B = 20000) and its fallback paths and
  recall recorded under Failure Modes; the credential-rotation prerequisite
  answered; stale sites corrected (line 21, insert cost, default-search figure,
  Contradiction Check, A6, the API table, References); two unchecked boxes
  resolved. No change to the frontmatter; the lifecycle move is separate.
- 2026-10-12: Close critique round 2 (T2 `nexus_rdr/227-close-critique-2026-10-12-r2`: partial, 0 critical, 1 significant). The prose exception is corrected: it holds only while no prose collection is at or above B, and a prose collection reaching B is the trigger for the Gap 1 recall check. Three stale pointers and one unstruck sentence fixed.
- 2026-10-12: Closed as Implemented on Sam's decision, with the round-2 close critique at partial and its one finding corrected (T2 `nexus_rdr/227-close-override-2026-10-12`). Gap 3 stays open under the deferred bead nexus-43ulx.26; the prose threshold watch is bead nexus-qqoak. Post-mortem: `docs/rdr/post-mortem/rdr-227-per-collection-hnsw-above-the-router-threshold.md`.
- 2026-10-18: Decision 4 carve-out for `REINDEX CONCURRENTLY` leftovers (Sam,
  `nexus_rdr/227-decision-4-amendment-reindex-leftovers`, bead nexus-rn1yd): the
  Schema paragraph and the status object above carry it.
