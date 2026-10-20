---
title: "Event-Keyed REINDEX and a Recall Canary for Per-Collection HNSW Indexes"
id: RDR-230
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-10-18
accepted_date:
related_issues: ["nexus-4lnn1", "nexus-s9wav", "nexus-dxsjc", "nexus-rn1yd"]
related_rdrs: [RDR-192, RDR-217, RDR-225, RDR-227]
---

# RDR-230: Event-Keyed REINDEX and a Recall Canary for Per-Collection HNSW Indexes

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Drafted 2026-10-18 against develop `58056a574`. No product code has changed.
The content source of record is T2 `nexus/4lnn1-hnsw-maintenance-policy-proposal`
(the proposal) and Sam's decisions on it, T2 `nexus/4lnn1-decisions-2026-10-18`
(all five recommendations accepted). This RDR is a follow-on to RDR-227 and
carries the proposal's recommendations R2 and R3 only.

**Terms used here.** An *HNSW index* is an approximate nearest-neighbour index:
a graph over the stored vectors that a search walks from an entry point toward
the query, visiting a bounded number of candidates (`ef_search`). It is fast and
usually, but not always, returns the true nearest rows. *Recall* is the
fraction of the true top-k (found by an exact scan of every row) that the HNSW
search returned. A *leaf* is one partition of `nexus.chunks` holding every
collection of one (embedding model, tenant) pair, with one HNSW graph over all of
them (RDR-225). A *per-collection index* (`pci_` index) is the partial HNSW index
RDR-227 builds on a leaf for one collection of at least B = 20000 rows, named
`pci_` plus a hash. The *builder* is RDR-227's engine component that creates and
drops those indexes on a direct admin connection, under a PostgreSQL advisory
lock (a named lock any session can take; only one holds it) so that only one
engine at a time does it. A *pass* is one run of the builder. `REINDEX INDEX
CONCURRENTLY` rebuilds an index from the table without blocking reads or writes:
it builds a second copy beside the first (named `<index>_ccnew`), swaps the two,
and drops the old one (`<index>_ccold`). A *dead tuple* is a row version that a
delete or update has replaced, still on disk until `VACUUM` removes it; HNSW
graphs keep entries for dead tuples until a `VACUUM` repairs the graph around
them.

## Problem Statement

RDR-227 gave each large collection its own HNSW graph and measured that graph's
recall when it was freshly built. Nothing has measured one since, and nothing
would notice if one decayed. Meanwhile every per-collection graph absorbs its
own collection's deletes (a repository re-index deletes and reinserts changed
chunks; the reaper moves rows to quarantine; purges remove them), and the builder
has no way to rebuild a graph short of dropping it.

The original bead, nexus-4lnn1 (2026-08-14), was written for one global HNSW
index per dimension across all tenants, searched at `ef_search` 40 with a small
scan budget and no router. Every one of those has changed since (Context), and
Sam re-scoped the bead on 2026-10-18 (decision 1): the recall-drift half is
retired for everything except aged per-collection indexes, and the bead becomes
bloat and latency maintenance plus a recall canary. The gaps below are what
remains for the per-collection indexes and, for size only, the leaf graphs.

### Enumerated gaps to close

#### Gap 1: No aged per-collection index has ever been measured, and no detector would see one decay

Every recall number RDR-227 holds (0.99 to 1.0 at k 40 to 120) was taken on a
fresh build (`nexus_rdr/227-research-3`, `-12`). The five `pci_` indexes on the
managed cloud (tenant nexus: `code__1-1` about 58k rows, `code__1-2` 45.5k,
`code__1-20` 29.4k, `code__1-72` 27.9k; tenant gate-xr789: `code__1-20` 28.6k)
have served since 2026-10-10 through their collections' churn, and no one has
measured them. The only standing recall signal is conexus's STEP-6 parity check,
which compares each query's results against an earlier HNSW answer (a Jaccard
overlap), not against an exact scan. The proposal (section 2) shows that signal
moves on any rewrite of the heap or index because near-tie neighbourhoods
re-roll, so it is a noise detector for that class and not a decay detector
(conexus-kbdp already lists kw-006 as a known borderline query).

#### Gap 2: Deletes accumulate in per-collection graphs, and the builder cannot rebuild one in place

The builder's statement set is fixed by RDR-227: session settings, the advisory
lock, catalog reads, per-tenant counts, `CREATE INDEX CONCURRENTLY` and `DROP
INDEX CONCURRENTLY` on `pci_` names (RDR-227 Trade-offs, "a deliberate widening
of the admin path"). Its only rebuild is drop-and-build, during which the
collection is served by the shared leaf graph at `ef_search = 1000` (worst
measured recall 0.975 on `code__1-72`, `nexus_rdr/227-research-12`). An operator
who runs `REINDEX` by hand does so outside the builder's lock and
`application_name`, so a boot migration that alters the leaf queues behind it
and every read of the leaf queues behind the migration (RDR-227 Lifecycle,
Migrations). What bloat costs is measured: conexus-h2ol (2026-08-29) saw 30,404
dead tuples (about 8%) put `/v1/vectors/search` p95 at 2654 ms against 1037 ms
after `VACUUM`, and that `VACUUM` spent 367.6 s on the HNSW indexes; recall
parity was identical before and after.

#### Gap 3: A bulk delete leaves an indexed collection's graph full of dead entries until autovacuum reaches the leaf

A purge, a quarantine sweep or a re-index can remove a large share of one
collection's rows at once. Autovacuum is triggered per leaf by the leaf's whole
dead-tuple count, so a delete that is large for one collection can be small for
its leaf and wait. pgvector's own guidance for heavy deletes is
`REINDEX INDEX CONCURRENTLY` followed by `VACUUM`, which nothing in the engine
does today. The one post-delete `VACUUM` the engine runs is purge-trash's
(`CatalogRepository.applyPostPurgeVacuum`, nexus-0ys55), which vacuums the whole
partitioned table and rebuilds no graph.

#### Gap 4: Leaf graphs grow with no size trigger at all

The per-(model, tenant) leaf graphs still serve every multi-collection statement,
the hybrid HNSW-first leg, scoped searches and the `ef_search = 1000` fallback
walk. They are exposed less than `pci_` indexes because they are searched at
`ef_search` 600 or more, but they bloat the same way, and nothing rebuilds them
except a schema migration that rewrites the leaf.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-227 (per-collection HNSW) | Origin, closed | Owns the builder, its lock, the read half, the status object and the `pci_` naming rules. This RDR adds statements to the builder's fixed set, a sub-object to its status object, and a canary on the lock holder. Its Gap 1 recall method is reused unchanged. |
| RDR-225 (vector tables per model) | Precedent, deployed | Defines the leaves and their HNSW indexes. Its per-leaf recall analysis (`nexus_rdr/225-recall-per-leaf-analysis`) found build mode recall-neutral and a per-leaf graph better than the old global one (0.966 against 0.919 at `ef_search` 200). |
| RDR-192 (live-chunk visibility, scan budget) | Precedent | Set `hnsw.max_scan_tuples` 200000 and the 16 MB scan budget that restored recall 1.000 at 98% correlated-dead rows in a synthetic test (`nexus/rdr-192-livec-recall-extended-2026-09-30`), which is why dead tuples cost latency rather than recall at serving settings. |
| RDR-217 (client lexical retrieval leg) | Stale premise, not edited | Its Approach (Phase 1, about line 290) says "a bulk-built HNSW index retrieves measurably better than an incrementally grown one ... (nexus-4lnn1)". Later measurements contradict that half (Research Findings); the autovacuum observation in the same sentence stands. RDR-217 is left as written. |

**Out of scope, handled elsewhere.** The proposal's R1, per-leaf autovacuum
storage parameters through Liquibase in both modes (scale factor 0.05, threshold
1000, insert scale factor 0.05, analyze scale factor 0.02), is bead nexus-s9wav.
R4, bloat visibility per leaf and per `pci_` index in `/v1/status` and one
`nx doctor` row over HTTP, is bead nexus-dxsjc; that row is also where a red
canary from this RDR is surfaced to an operator. The prerequisite that the
builder drop its own invalid `_ccnew`/`_ccold` leftovers landed as nexus-rn1yd
(`4a7794543`, ledger entry `f90da05f8`; Sam's carve-out
`nexus_rdr/227-decision-4-amendment-reindex-leftovers`). The proposal's R5 (do
nothing for exact-path collections beyond R1) needs no work.

## Context

### Background

What changed since the bead was filed, each with its record:

- `ef_search` floor 600 (`PgSession.DEFAULT_EF_SEARCH_FLOOR`; nexus-4ktfm, then
  nexus-3wh8d.31).
- Scan budget: `max_scan_tuples` 200000 and a fixed 16 MB scan memory
  (nexus-wbfpw.47; RDR-192).
- RDR-225: one HNSW graph per (model, tenant) leaf, so tenants no longer crowd
  each other.
- The router (nexus-tu8wp.6): a single-collection search on a collection of at
  most T = 20000 rows runs an exact scan, so its results do not depend on any
  graph. In tenant nexus that is 111 of 116 collections (counts of 2026-10-08 to
  10), including `knowledge__dt-papers` (6,717 rows), where the August query
  kw-006 lives.
- RDR-227: each collection of at least B = 20000 rows gets its own partial HNSW
  index (engine-service-v0.1.158 and v0.1.159). Above T without a valid index,
  the arm walks the leaf at `ef_search = 1000`.

### Technical Environment

- PostgreSQL 17, pgvector 0.8.2, HNSW `m` 16 and `ef_construction` 64,
  `nexus.vector_cosine_ops`.
- Builder: `PciBuilderSession` (direct admin connection, autocommit,
  `application_name` `nexus-pci-builder-<boot id>`, advisory lock
  `PCI_BUILDER_ADVISORY_LOCK_KEY`, DDL text rendered by the server's `format()`
  with `%I` and `%L`, sent through two methods registered in
  `RawSqlGateTest.SANCTIONED_STATEMENTS`, `runBuild` and `runDrop`).
- Planner: `PciReconcilePlanner`, a pure function from one leaf's indexes,
  counts and registry rows to an ordered list of drops and builds.
- Reconciler: `PciReconciler`, which counts per leaf in one transaction under
  `SET LOCAL nexus.tenant`, plans, executes, and serves the status object's
  `this_engine` half.
- Catalog read: `PciCatalog`, which classes every `pci_` index as parsed (the
  builder's own), unparsed, or a `REINDEX CONCURRENTLY` leftover.
- Serving: `PgVectorRepository.runPlainSearchStatement` sets the HNSW settings
  and `plan_cache_mode = force_custom_plan` in one batch; the exact route is the
  same statement after `PgSession.disableIndexScanForExactFallback`. The engine
  embeds query text itself (`PgVectorRepository.embedQuery`), so it holds every
  query vector it searches with.

## Research Findings

### Investigation

Read-only work on develop `15a0aafb0` for the proposal (repo, T2, docs; no
cloud or live-store queries) and a source read of the builder, planner,
reconciler, catalog read and `RawSqlGateTest` on `58056a574` for this draft.
Sources: the proposal's sections 1 to 5 and its Unknowns; RDR-227; T2
`nexus_rdr/225-recall-per-leaf-analysis`, `nexus/rdr-192-livec-recall-extended-2026-09-30`,
`nexus/p02-hnsw-incremental-recall-2026-09-25`, `nexus_rdr/227-research-12`;
conexus records [23758], [22578], [22526] and conexus-h2ol.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| nexus engine (`service/src/main`) | Yes | The builder's DDL set is exactly build and drop; the canary's two statements already exist as the serving and exact routes. |
| pgvector 0.8.2 | Docs (README, via the proposal) | `REINDEX INDEX CONCURRENTLY` then `VACUUM` for heavy deletes; HNSW `VACUUM` repairs neighbours of deleted elements and reuses their slots on insert. |
| PostgreSQL 17 | Docs | `REINDEX CONCURRENTLY` and `VACUUM` cannot run in a transaction block; storage parameters cannot be set on a partitioned parent. |

### Key Discoveries

- **✅ Verified** (measurement, three records). An incrementally grown graph
  does not recall worse than a bulk-built one. Build mode (in-memory against
  on-disk insert, parallel against serial) does not change recall
  (`nexus_rdr/225-recall-per-leaf-analysis`). A `REINDEX` reproduced every recall
  number to three decimals (`nexus/rdr-192-livec-recall-extended-2026-09-30`).
  Taxonomy P02 (`nexus/p02-hnsw-incremental-recall-2026-09-25`): at
  `ef_search` 400 or more, zero wrong results in every state tested
  (incremental, dead tuples present, vacuumed, reindexed). This contradicts the
  first half of RDR-217's premise and of nexus-4lnn1's mechanism (a).
- **✅ Verified** (measurement). Drift in the STEP-6 evidence is a step
  function keyed to rewrite events. kw-006 was byte-stable for 31 runs
  (2026-07-07 to 07-27) and stepped only on heap or index rewrites: the 08-14
  bulk rebuild (0.429 to 1.0), the 08-17 v0.1.79 walk (to 0.818), the 08-29 18:54Z
  autovacuum (to 0.333). Recall against exact on the same collection did not
  move. *Source: conexus [23758].* One step, 08-14 to 08-15, has no named event.
- **📖 Documented** (pgvector docs and source, via the proposal). HNSW `VACUUM`
  rewires every element that pointed at a deleted one, so a `VACUUM` that reaps
  graph entries is a discrete graph change; between rewrites nothing moves. The
  proposal restates mechanism (a) as: deletes plus vacuum repair perturb the
  graph; inserts and build mode do not measurably.
- **✅ Verified** (measurement, synthetic). Dead tuples consume the bounded
  scan, but the serving scan budget neutralises it (RDR-192, 76k to 100k rows);
  P02 saw dead tuples triple errors at `ef_search` 100 and cause none at 400 or
  more. What bloat costs today is latency and `VACUUM` time (conexus-h2ol,
  Gap 2).
- **✅ Verified** (measurement). A `pci_` index rebuilds in 9 to 21 s on the
  production fork (`nexus_rdr/227-research-12`); locally 60,000 rows took 44 s
  at 64 MB `maintenance_work_mem` (`nexus_rdr/227-research-11`). A leaf rebuild
  on the managed instance is unmeasured.
- **✅ Verified** (measurement). An exact scan of a 60k-row collection costs
  0.3 to 0.6 s alone (RDR-227 Gap 2), so ten exact queries on each of five
  indexes is about 25 s of database time.
- **✅ Verified** (source read). A document-to-document recall check overstates
  recall: queries seeded from a collection's own rows never missed on the nqsa7
  repro (RDR-227 A2), and the second-tenant run sampled from stored embeddings
  read 1.0 at every k, which `nexus_rdr/227-research-12` calls weak evidence.
- **✅ Verified** (source read). The builder cannot rebuild in place (Gap 2),
  and the engine already holds each query's vector server-side
  (`embedQuery`), so a ring of real query vectors costs no embedding call.
- **❓ Assumed.** Every item under Critical Assumptions.

- **✅ Verified** (spike, 2026-10-20). At 1024 dimensions an HNSW index is one
  8 KB page per element plus a metapage (20000 rows: 163,848,192 bytes = 20001 x
  8192), so bytes per row is slots over rows. Pure ingest 20k to 40k: 1.000.
  Delete 25%: 1.333; 50%: 2.000. Plain `VACUUM` does not shrink the file;
  reinsert after it refills every freed slot (back to 1.000). Delete and
  reinsert with no `VACUUM` between grows the file (1.25, then 1.75), and it
  stays there after `VACUUM` with no dead tuples. Plain `VACUUM` of a 40k-row
  table with one HNSW index took 163.5 s after 10k deletes and 268.1 s after 20k
  on a laptop. Not measured: recall at any step, other dimensions, autovacuum.
  *Source: local spike, PG 17.11 and pgvector 0.8.7 (`pgvector/pgvector:pg17`),
  m 16, `ef_construction` 64, seed 42; `nexus_rdr/230-research-11`.*
- **✅ Verified** (spike, 2026-10-20). `REINDEX INDEX CONCURRENTLY` keeps a
  leaf's child index attached to the parent index and keeps any index's
  comment. A REINDEX cancelled while loading tuples leaves `<index>_ccnew`
  (invalid, not ready, not attached, no predicate, no comment); the next
  REINDEX names its copy `_ccnew1`. While a REINDEX runs, `DROP INDEX
  CONCURRENTLY` under a 5 s lock timeout fails at 5.00 s on the leftover and on
  the running REINDEX's own in-flight copy alike. The in-flight copy is also
  invalid, so the `indisvalid` re-read cannot tell it from a dead leftover; the
  lock timeout is what protects it. `_ccold` was not produced. *Source: local
  spike as above; `nexus_rdr/230-research-13`.*

### Critical Assumptions

These are the proposal's Unknowns that this design depends on. Each says how it
gets verified; the first canary run on the cloud is the missing aged-index
measurement.

- [ ] **A1.** An aged `pci_` index on production data still meets the Gap 1 bar
  at serving `ef_search` (mean recall at least 0.99 at k 40 to 120, no top-1
  miss). **Status**: Unverified; never measured. **Method**: the canary's first
  runs on the cloud in Phase 1, with REINDEX not yet shipped, so the reading is
  of the indexes as they aged. A red result is the falsification of "aged `pci_`
  graphs are fine at `ef_search` 600" and is acted on by hand in Phase 1.
- [ ] **A2.** Vacuum graph repair does not degrade recall at `ef_search` 600 on
  real vectors (P02 says not at 400 or more, on synthetic data). **Status**:
  Unverified. **Method**: the canary runs after each vacuum of a leaf that had
  deletes or non-HOT updates to reap, so Phase 1 yields before-and-after pairs on
  the cloud.
- [x] **A3.** Index bytes per row is a usable bloat signal: pure ingest keeps it
  under 1.4 times the fresh-build figure, and deletes push it up. **Status**:
  Verified by a local spike on 2026-10-20 (`nexus_rdr/230-research-11`), with a
  calibration. At 1024 dimensions each graph element takes one 8 KB page, so
  bytes per row is element slots divided by rows. Pure ingest to twice the built
  size reads exactly 1.000; deletes raise it to 1/(1 - fraction deleted); plain
  `VACUUM` leaves the size alone; reinsert after `VACUUM` reuses every freed slot
  and brings it back to 1.000. The size rule therefore sees space (an unrefilled
  shrink, or churn that outran `VACUUM`), never vacuumed churn, which is why the
  deletes rule (Technical Design 3) does not rely on size.
- [ ] **A4.** The managed instance's per-leaf state is in the range this design
  assumes: dead fractions, `last_autovacuum`, HNSW `VACUUM` duration per leaf
  after RDR-225, and leaf `REINDEX` time. **Status**: Unknown (not queried, per
  the proposal's brief). **Method**: a read-only query by conexus in Phase 1;
  leaf `REINDEX` time on a PITR fork in Phase 2 (Minimum Viable Validation).
- [x] **A5.** `REINDEX INDEX CONCURRENTLY` on a leaf's HNSW index keeps it
  attached to the partitioned parent index, and a comment on an index survives
  `REINDEX INDEX CONCURRENTLY`. **Status**: Verified by a local spike on
  2026-10-20 (`nexus_rdr/230-research-13`) on both an attached leaf index and a
  `pci_` partial index; the name, the predicate and the parent's validity are
  unchanged. The substrate tests in Phase 1 (comment) and Phase 2 (attachment)
  still pin it in the engine's own roles and schemas.
- [ ] **A6.** Enough own-topic queries reach the lock holder for each indexed
  collection to give a verdict at least daily. **Status**: Unverified; with
  several engines behind a load balancer the holder sees about 1/N of the
  queries, and a collection nobody queries is never measured. **Method**: the
  status object's `unmeasured` count after a week of Phase 1 on the cloud.

Two Unknowns are not assumptions of this design and are named so they are not
lost: per-path routing of scoped and hybrid searches onto leaf graphs was not
traced (it decides how exposed the leaves are, Gap 4), and conexus-h2ol's
outcome (whether storage parameters were set by hand on the pre-RDR-225 table,
which the RDR-225 walk would have discarded, and whether conexus's bloat probe
reads leaves or the empty partitioned parent) belongs to nexus-s9wav,
nexus-dxsjc and conexus.

## Proposed Solution

### Approach

Rebuild a per-collection graph when something happened to it, never on a
calendar, and measure it against an exact scan with the queries people actually
ask. The builder already holds the only lock and the only admin session allowed
to change these indexes, so it does both: after its drops and builds in a pass,
it checks each index's anchor (what the index looked like when built), rebuilds
the ones whose triggers fired with `REINDEX INDEX CONCURRENTLY`, and runs the
recall canary on the ones that are due. Every threshold below is provisional
(Sam, decision 2) and is tuned from the canary's readings.

The canary ships first (Phase 1) with no REINDEX at all, because its first runs
are the measurement A1 lacks and because the thresholds are to be tuned from
it. Event-keyed REINDEX ships second (Phase 2).

### Technical Design

**1. The anchor: what an index looked like when built.** After every successful
build and every successful REINDEX, the builder writes the index's anchor as
its PostgreSQL comment, on the same session:

```text
COMMENT ON INDEX <leaf schema>.<index> IS
  '{"nexus_pci":1,"built_at":"2026-10-20T08:00:00Z","rows":58588,"bytes":480116736,"origin":"built"}'
```

`rows` is the collection's row count on its leaf, counted uncapped right after
the build (rows present in the table, not RDR-192's owned-chunk sense of
"live"); `bytes` is `pg_relation_size` of the index. A comment lives and dies
with its index, needs no table, no changeset and no row-level security, and is
readable by any role through `obj_description`, so the read half can report it.
An index the builder did not anchor (the five built before this RDR ships, or
one an operator rebuilt by hand) is *adopted* on the first pass that sees it:
the builder writes `"origin":"adopted"` with the current rows and bytes and the
adoption time as `built_at`. An adopted index is already aged, so its size
trigger compares against the install's fresh-build reference (item 3), not
against its own adopted figure.

*Why not a table.* A table would carry tenant data (collection names), so it
would need FORCE row-level security, grants, the four hand-kept tenant-table
enumerations and a PITR-fork rehearsal of its changeset, to hold three numbers
per index whose lifetime is the index's. If Sam prefers a table, it is one
Liquibase changeset after `vectors-033`, and that changeset is rehearsed on a
fork before the cloud deploy like every other (Open Question 2; Sam chose the
comment on 2026-10-20).

**2. What the builder reads each pass.** For each leaf that holds a valid parsed
`pci_` index, inside the leaf's existing counting transaction (same tenant
setting, same per-statement 30 s timeout):

- the leaf's `pg_stat_user_tables` row: `n_tup_del`, `n_tup_upd`,
  `n_tup_hot_upd`, `n_live_tup`, `last_autovacuum`, `last_vacuum`;
- for each `pci_` index, its anchor and `pg_relation_size`;
- only when the leaf's deletes or non-HOT updates (`n_tup_upd - n_tup_hot_upd`)
  advanced since this engine last looked, or a stats reset moved them backward,
  or the index's last check is older than 24 hours: the collection's uncapped
  row count and `written_since`, the count of its rows whose `last_written_at`
  is later than the anchor's `built_at`. `last_written_at` is set on insert and
  refreshed by every client re-write of a chunk, never by maintenance updates
  (vectors-020), so `written_since` counts the graph entries the collection
  gained since the build.

Both counts carry the same explicit `tenant_id` predicate as the existing count,
for the reason RDR-227 records (the admin role reads every tenant's chunks).
Their cost is that of an exact scan of the collection's rows (0.3 to 0.6 s at
60k), paid only when something was deleted or a day passed.

**3. REINDEX triggers (Phase 2).** For a parsed, valid `pci_` index on the
holder's pass:

| Trigger | Rule (provisional) | Then |
| --- | --- | --- |
| Size | `bytes_now / rows_now >= 1.4 x anchor.bytes / anchor.rows`; for an adopted index, against the install reference for its dimension | REINDEX |
| Deletes since build | `max(0, anchor.rows + written_since - rows_now) >= 0.25 x rows_now` | REINDEX |
| Bulk delete | `rows_now` fell by at least 10% since this engine's previous check of the index | REINDEX, then `VACUUM (ANALYZE)` of the leaf |
| Red canary | item 5 | REINDEX, then re-measure with the same queries |

For a leaf's own HNSW index: the size rule at 1.5 times the install reference
for its dimension, with the leaf's rows read from `n_live_tup` (an estimate is
enough for a ratio). The *install reference* for a dimension is the median
bytes per row over this install's anchors with `"origin":"built"` at that
dimension; with none, 8.2 KB per row for 1024 dimensions (RDR-227 A3, measured
on fresh builds; it is the 8 KB page each element takes at that dimension, A3
here) and no reference for any other dimension, in which case the
leaf rule is inactive and the status says `unanchored`. After a leaf REINDEX the
builder anchors the leaf index the same way.

*Deletes since build is an estimate, deliberately low.* Worked through, it
equals the rows present at build that have since been deleted plus those that a
client re-wrote (each re-write leaves one dead graph entry), which is the
quantity wanted. It misses three things: a row inserted after the build and
deleted again (in neither count), a maintenance update that is not HOT (heap-only
tuple: an update PostgreSQL can make without touching any index), which adds a
graph entry without refreshing `last_written_at`, and a row moved in by a
maintenance path that keeps `last_written_at`. All three make the rule fire
later, never sooner.

*The bulk-delete rule is detected, not signalled.* The delete sources (the
reaper's quarantine move and expiry, `drop-orphan-collections`, purge-trash, a
repository re-index, the canonical rename's re-home) run on whichever engine
took the request, which may not hold the builder lock, so a signal from them
would be lost on any other engine. Comparing counts on the holder sees every
source. The previous count is in the holder's memory, so after a restart or a
change of holder the first check has no previous value and only the
deletes-since-build rule (which survives in the anchor) can fire; that is the
backstop. Two of the proposal's named sources need no REINDEX: a canonical
rename supersedes the old name and `drop-orphan-collections` removes it from the
registry, and RDR-227's rules drop the old index in both cases (a fresh build
follows under the new name). Their dead tuples are left to autovacuum, which
nexus-s9wav sets to act at 5% per leaf.

*Never on ingest or a migration walk.* No rule keys on inserts (they are
recall-neutral as measured), and pure ingest holds bytes per row at 1.000 of
the build (A3, measured). The builder never runs during a migration (RDR-227),
and a walk that rewrites leaves rebuilds every index fresh, with a fresh anchor.

*Limits on how often.* REINDEXes run after the pass's drops and builds, one at
a time, at most two per pass (provisional), largest index first; the rest wait
for the next pass. A red-canary REINDEX runs at most once per index per 24
hours; if the re-measure is still red, the index is reported
`red_after_reindex` and not rebuilt again until a later canary run is green or
the 24 hours pass. The size and deletes rules reset themselves, because the
REINDEX writes a fresh anchor.

**4. The REINDEX itself.** On the pass's admin session, with the build's
settings (no `lock_timeout`, 30-minute `statement_timeout`; the statement waits
for older transactions the way a concurrent build does):

```text
REINDEX INDEX CONCURRENTLY <leaf schema>.<index>
COMMENT ON INDEX <leaf schema>.<index> IS <new anchor>
VACUUM (ANALYZE) <leaf schema>.<leaf>        -- bulk-delete trigger only
```

The old index keeps serving until the swap, and the swap keeps the name, so the
router's set of valid indexes never loses the collection and no arm falls to
the `ef_search = 1000` walk (the reason RDR-227's drop-and-rebuild path is not
used). While the REINDEX runs, the read half sees an invalid `pci_<hash>_ccnew`
and counts it as `reindex_leftover`; on an engine whose builder is `ok` or
`standby` that count is shown and not warned on (nexus-rn1yd's doctor rule), and
the holder's status says a REINDEX is in flight (item 7). A REINDEX that fails
or is cut off (a migration terminates the builder backend) leaves an invalid
`_ccnew` or `_ccold`, which the existing leftover rule drops on a later pass.
For a leaf's own HNSW index that rule does not apply yet, because its name is
not a `pci_` name: this RDR extends the leftover carve-out to an invalid `hnsw`
index named `<the leaf's HNSW index>_cc(new|old)[0-9]*` with no predicate on a
leaf the builder resolved, through the same drop path as the `pci_` leftovers:
the re-read of `indisvalid` before the drop and its 5 s lock timeout. The re-read
does not protect a running build, whose copy is also invalid; the lock timeout
does, because a running REINDEX holds the leaf's lock and the drop times out
instead of removing it (measured, `nexus_rdr/230-research-13`)
(`PciBuilderSession.drop`; Open Question 3, approved by Sam on 2026-10-20 on that
condition). A `VACUUM` of a leaf holds `ShareUpdateExclusiveLock`
for its run, which makes autovacuum skip that leaf meanwhile; it scans every
index on the leaf, including the leaf graph, which is the slow part (conexus-h2ol:
367.6 s on the HNSW indexes of the pre-RDR-225 table).

**5. The recall canary (Phase 1).** For one `pci_` index, a run is the RDR-227
Gap 1 method on up to ten recent query vectors from the ring (item 6):

- the HNSW top-k through the serving statement path itself
  (`runPlainSearchStatement`'s settings batch: `relaxed_order`, serving
  `ef_search`, scan budget, `force_custom_plan`, bound parameters), at k 40, 60,
  80, 100 and 120, as nexus_svc under the tenant's scope;
- the exact top-120 through the same statement after
  `disableIndexScanForExactFallback`, whose first k rows are the exact top-k;
- a plan check on the first HNSW statement, as `PciPlanCheck` does. If the
  planner did not use the collection's index, the run measured the leaf walk,
  so it is discarded as `unmeasured` with reason `index_not_used` and logged.

Recall at k counts an HNSW row as a hit when it is in the exact top-k or its
distance equals the k-th exact distance (ties at the boundary are not misses).
A *top-1 miss* is the exact nearest row absent from the HNSW top-k at some k.
The run reports minimum and mean recall over its queries and k values, and the
top-1 miss count. It is **red** when mean recall is below 0.99 or any query has
a top-1 miss (Sam, decision 2: the Gap 1 bar); green otherwise. It needs at
least three distinct queries; with fewer the index stays `unmeasured` with
reason `no_queries`.

When it runs, on the lock holder's pass after drops, builds and REINDEXes:
after each build or REINDEX of the index; when its leaf's `last_autovacuum`
(or `last_vacuum`) advanced since this engine last looked AND the leaf's deletes
or non-HOT updates (`n_tup_del`, `n_tup_upd - n_tup_hot_upd`) advanced over the
same interval, at most once per index per 6 hours; and when its last run on this
engine is older than 24 hours. The second condition matters because nexus-s9wav
sets `autovacuum_vacuum_insert_scale_factor` 0.05 on every leaf, so
`last_autovacuum` now also advances on insert-only vacuums that reap nothing and
repair no graph; keying on the timestamp alone would run the canary on ingest,
which this design holds perturbs nothing.
The first observation of a leaf after a boot records the timestamp and does
not trigger. A run that cannot happen yet (too few queries) is held as pending
in memory and runs on the first pass that has the queries.

Cost and bounds: each statement takes one search permit, like an arm, so the
canary yields to user load and its load is counted where search load is; the
statements run one at a time; a pass spends at most 60 s on canary runs
(provisional) and carries the rest to the next pass. At ten queries the
exact statement dominates: about 0.3 to 0.6 s per query on a 60k collection plus
five HNSW statements of 10 to 50 ms, so about 25 to 35 s of database time for
the five cloud indexes, spread over two passes.

Every run logs `event=pci_canary index= collection= tenant= trigger= queries=
min_recall= mean_recall= top1_misses= state=`. The log is the durable record
used to tune the thresholds; the engine keeps only the latest result per index
in memory.

**6. The query ring.** For each (tenant, collection) whose collection is in the
router's valid-index set, the engine keeps the 32 most recent distinct query
vectors (identical vectors count once), in memory only: never written to the
database, never logged, dropped on restart. A query's vector enters collection
X's ring when the request searched X alone, or when X's arm returned the
nearest row among the request's arms that share X's embedding model. The second
condition is what "own-topic" means for a fan-out: RDR-227 measured that a far
query reads 0.975 to 0.983 on a correct index (`code__1-20`, A2), so admitting
every fan-out arm would turn ordinary approximation on far queries into red
canaries. Memory is bounded at 32 vectors of 4 KB (1024 dimensions) per key and
at 256 keys, least recently used first out, so under 33 MB at worst and about
640 KB for the cloud's five indexes. A canary run reads only its own tenant's
ring and runs under that tenant's scope, so a vector never crosses tenants.

*Cold start.* After a boot every ring is empty, and with several engines the
lock holder may be one that receives few queries. Until queries arrive the
canary reports `unmeasured` (reason `no_queries`), which is visible in the
status and is not green. It does not fall back to document-to-document queries,
which overstate recall (Research Findings). A red result keeps its query sample
in memory, so the re-measure after a red-canary REINDEX uses the same queries
and needs no new traffic. An index nobody queries is never measured; since no
one is served by it, that is acceptable, and A6 checks how common it is.

**7. Status (additive).** `GET /v1/status`'s `per_collection_indexes.this_engine`
gains a `maintenance` object, served from values the reconciler holds (no
catalog query on the route, RDR-227's rule):

```json
"maintenance": {
  "reindex": "on", "reindexing": null,
  "last_reindex_at": "2026-10-20T08:00:00Z", "reindexes": 2,
  "canary": {
    "green": 4, "red": 0, "unmeasured": 1, "last_run_at": "2026-10-20T08:01:10Z",
    "indexes": [
      {"index": "pci_3f9c0a1b2c3d4e5f6a7b8c9d", "state": "green", "reason": null,
       "queries": 10, "min_recall": 0.983, "mean_recall": 0.998, "top1_misses": 0,
       "trigger": "daily", "measured_at": "2026-10-20T08:01:10Z",
       "anchor": "built", "size_ratio": 1.07, "deletes_since_build": 0.03}
    ]
  }
}
```

`reindex` is `on` or `off`; `reindexing` is the index name in flight on the
holder or null. Entries are keyed by the opaque `pci_` name and carry no
collection or tenant name, because `/v1/status` is unauthenticated and its
existing counts are global (RDR-227 Day 2 Operations); the log lines carry the
names for operators. The list is capped at 64 entries, red first. Results are
this engine's own history and stay after it becomes a standby, like
`last_ddl_pass_at`; an engine that has never held the lock reports zeros and an
empty list. A client against an engine that predates the key finds none and
reports not-applicable. The key goes in the wire ledger
(`docs/wire-contract-pending.md`) and the client fixture
(`tests/fixtures/pci_status_bodies.json`); the doctor row that reads it, and
warns on a red canary, is nexus-dxsjc's.

**8. Switches.** REINDEX and the post-delete `VACUUM` are part of the builder's
DDL, so `NX_SEARCH_PCI=0` stops them along with builds. A new setting,
`NX_SEARCH_PCI_REINDEX` (`1`, the default once Phase 2 ships, or `0`; any other
value refuses boot, like the other `NX_SEARCH_PCI*` settings), stops REINDEX and
`VACUUM` alone while builds and the canary continue, so an operator can halt
maintenance without halting index builds. Thresholds are constants in one
policy class, changed by an engine cut: tuning is evidence-driven and rare, and
a setting per threshold would widen the configuration surface for no
operational need (Open Question 4; Sam kept one switch on 2026-10-20). The canary has no switch of its own; it
runs whenever the holder runs a pass.

### The widened admin statement set

RDR-227 recorded the builder's admin session as running a fixed statement set
and nothing else, as a deliberate widening of the admin path. This RDR widens it
again, and this section is that record. Added:

- `REINDEX INDEX CONCURRENTLY %I.%I`, on either a parsed builder index
  (`PciCatalog.isBuilderName`, valid, on the leaf it names) or the leaf's own
  HNSW index as the catalog read identifies it (access method `hnsw`, no
  predicate, attached to the parent's partitioned HNSW index). Never any other
  index.
- `COMMENT ON INDEX %I.%I IS %L`, on the same two kinds of index, with a 5 s
  `lock_timeout` like a drop.
- `VACUUM (ANALYZE) %I.%I`, on a leaf of `nexus.chunks` from the pass's catalog
  snapshot (`PciCatalog.Leaf`), never on another table.
- `DROP INDEX CONCURRENTLY IF EXISTS` on an invalid leftover of the leaf's own
  HNSW index (item 4), beside the existing `pci_` leftover drop.
- Reads: `pg_stat_user_tables`, `pg_relation_size`, `obj_description`, and the
  two counts of item 2, all through typed jOOQ (`DSL.function`, the existing
  count's query shape), so they need no sanction.

Each new raw statement is rendered by the server's `format()` with `%I` and
`%L`, exactly as `runBuild` and `runDrop` are, and goes out through one
dedicated method: `runReindex`, `runComment` and `runVacuum` in
`PciBuilderSession`, each registered in `RawSqlGateTest.SANCTIONED_STATEMENTS`
with `.execute(sql)` once and a `// SANCTIONED RAW` comment giving this RDR. The
leaf-leftover drop reuses `runDrop`. The canary needs no admin statement: it
runs the serving and exact statements on the pool as nexus_svc.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| REINDEX, comment, leaf VACUUM | `PciBuilderSession` (`runBuild`, `runDrop`, `render`, `run`) | Extend with three sanctioned methods and their name guards. |
| Triggers | `PciReconcilePlanner` | Extend: a REINDEX action, planned after drops and builds, as a pure function of the anchor, the stats and the counts. |
| Counts and stats | `PciReconciler` counting transaction | Extend with the uncapped count, `written_since` and the leaf stats row. |
| Leaf leftovers | `PciCatalog` leftover rule (nexus-rn1yd) | Extend to the leaf's own HNSW index name, through the same drop path (Open Question 3, approved). |
| Canary statements | `runPlainSearchStatement`, `disableIndexScanForExactFallback`, `PciPlanCheck` | Reuse unchanged; the canary calls the serving path, never a copy of it. |
| Query vectors | `embedQuery` | Reuse: the ring takes the vector the search already computed. |
| Status | `StatusHandler`, `PciReconciler.status()` | Extend `this_engine` with `maintenance`. |
| Post-delete VACUUM precedent | `CatalogRepository.applyPostPurgeVacuum`, `TenantScope.vacuumAnalyze` | Unchanged; the builder's leaf VACUUM is a separate, per-leaf statement. |

### Decision Rationale

Measurement says inserts and build mode do not move recall, rewrites do, and a
REINDEX is itself a rewrite. A rebuild is therefore worth its cost only when
something happened that costs latency or `VACUUM` time (deletes, a bulk delete,
a grown index) or when a measurement against exact says the graph is wrong.
Keying the rebuild to those events and measuring with an exact scan, rather
than with parity against an earlier HNSW answer, addresses both what bloat
demonstrably costs and the one exposure nobody has measured. Running both on
the builder keeps one owner of index DDL, one lock, and the migration
termination rule that already protects the leaves.

## Alternatives Considered

### Alternative 1: REINDEX on a calendar

**Description**: Rebuild every HNSW index on a fixed cadence (the bead's
original action).

**Reason for rejection**: a REINDEX re-rolls the near-tie neighbourhoods of
poorly fitting queries every time (kw-006 stepped on each rewrite and was
byte-stable between them, conexus [23758]) and buys no expected recall: build
mode and REINDEX were measured recall-neutral (`225-recall-per-leaf-analysis`,
`rdr-192-livec-recall-extended`, P02). A calendar pays build time and load on a
schedule for no measured benefit, and would make STEP-6 parity move on the
calendar. Sam, decision 2: no calendar REINDEX.

### Alternative 2: RDR-227's drop-and-rebuild path

**Description**: Drop the index and let the next pass build it.

**Reason for rejection**: the path exists and stays as RDR-227's failure path.
For maintenance it is worse than a concurrent REINDEX: the collection serves
from the leaf walk at `ef_search = 1000` (worst measured 0.975, below the Gap 1
bar) for up to one sweep period (600 s) plus the build, where REINDEX keeps the
old index serving until the swap.

### Alternative 3: Stored fixture query vectors per collection

**Description**: Embed a fixed query set per collection once and keep the
vectors in a PostgreSQL table for the canary (the proposal's source (b)).

**Reason for rejection**: it needs one Voyage pass per collection, persists
tenant-derived vectors in a new table (a changeset, row-level security, a fork
rehearsal), and a fixed set drifts away from what users ask. Sam chose the
in-memory ring (decision 4).

### Alternative 4: Document-to-document canary

**Description**: Use stored chunk embeddings of the collection as queries (the
proposal's source (c)).

**Reason for rejection**: it overstates recall. A query seeded from a
collection's own row is close to an indexed element by construction; RDR-227's
self-seeded queries never missed on the nqsa7 repro, and the gate-xr789 run read
1.0 at every k, which `nexus_rdr/227-research-12` calls weak evidence. It is not
used even as a cold-start fallback (Technical Design 6).

### Alternative 5: Do nothing

**Description**: Leave the indexes as built and rely on autovacuum (with
nexus-s9wav's settings) and STEP-6 parity.

**Reason for rejection**: defensible on recall for the exact-path collections
(111 of 116 in tenant nexus) and for leaf graphs at `ef_search` 600, and this RDR
does nothing for the exact path. It leaves aged `pci_` indexes unmeasured (A1),
leaves the latency and `VACUUM` time that conexus-h2ol measured unaddressed for
indexed collections, and keeps a parity signal that cannot tell decay from
near-tie noise.

### Briefly Rejected

- **STEP-6 parity as the detector**: it compares against an earlier HNSW
  answer, so it reads every rewrite as drift (Gap 1). It stays as a regression
  check.
- **A signal from each delete source**: the source may run on an engine that
  does not hold the lock (Technical Design 3).
- **A canary on its own lock, independent of the builder**: it would run where
  the builder is off or failing, but a red result acts through REINDEX, which
  needs the holder, and two owners of index state is what the builder lock
  exists to prevent. The cost is stated: with `NX_SEARCH_PCI=0`, `auth_failed` or
  `no_privilege` there is no canary, and the status already shows those states.
- **A canary on leaf graphs**: their statements are multi-collection, so there
  is no single own-topic exact reference to compare against cheaply, and they
  run at `ef_search` 600 or more. Size-keyed REINDEX only (Gap 4).

## Trade-offs

### Consequences

- The admin session runs three more raw statements (The widened admin statement
  set). Each is single-homed, `format()`-rendered and sanctioned in
  `RawSqlGateTest`.
- A REINDEX costs one extra copy of the index on disk while it runs (229 to
  480 MB for the cloud's `pci_` indexes, RDR-227 A3) and write amplification on
  that collection for its duration.
- The holder's pass runs longer: up to two REINDEXes (9 to 21 s each for `pci_`
  indexes, leaves unmeasured) and up to 60 s of canary. Peers read `standby`
  for longer, which RDR-227's status already explains.
- Each REINDEX re-rolls near-tie neighbourhoods. conexus is told before the
  first REINDEX on the cloud and re-gates STEP-6 after it, expecting near-tie
  re-rolls and not regressions (the proposal's ownership split).
- The engine holds recent query vectors in memory. They are tenant-derived data
  and are treated as such: never persisted, never logged, scoped per tenant.

### Risks and Mitigations

- **Risk**: the size trigger fires on ingest. **Mitigation**: measured not to
  (A3: 1.000 under pure ingest to twice the built size); the cloud's first
  readings come before Phase 2 enables REINDEX; the threshold is a constant in
  one class.
- **Risk**: the canary goes red because of ordinary approximation on far
  queries, and REINDEX loops. **Mitigation**: own-topic admission, the
  once-per-24-hours limit and the `red_after_reindex` state, which ends the
  loop and is itself the evidence that the cause is not graph age.
- **Risk**: a leaf REINDEX on the managed instance takes long enough to matter
  (unmeasured, A4). **Mitigation**: the Phase 2 fork rehearsal measures it; the
  builder runs it one at a time under the 30-minute statement timeout; the
  migrator terminates it like a build.
- **Risk**: a REINDEX cut off mid-way leaves an invalid duplicate that costs
  write amplification. **Mitigation**: the leftover rule drops it (nexus-rn1yd for
  `pci_`, item 4 for the leaf index).

### Failure Modes

- No queries: `unmeasured` with `no_queries`; visible, never green.
- Plan check finds the index unused: `unmeasured` with `index_not_used`, logged
  at WARN with the plan's top node, as RDR-227's plan check does.
- A canary statement times out or fails: that run is `unmeasured` with
  `error`; no REINDEX is triggered by a failed measurement.
- A REINDEX fails: `event=pci_ddl_failed op=reindex`, the old index keeps
  serving, the leftover is dropped later, and the trigger fires again on the
  next pass, with RDR-227's build backoff (10 minutes doubling to 24 hours) applied
  per index.
- The anchor comment is missing or unparsable: the index is adopted again on
  the next pass, and the log says `event=pci_anchor_adopted reason=`.

## Implementation Plan

### Prerequisites

- [x] Sam's decisions on the proposal (T2 `nexus/4lnn1-decisions-2026-10-18`).
- [x] The `pci_` leftover drop (nexus-rn1yd).
- [x] Sam's answers to Open Questions 1 to 4 (2026-10-20).

### Minimum Viable Validation

Phase 1, on the cloud with REINDEX absent: the canary reports a verdict for at
least four of the five `pci_` indexes within 24 hours of the deploy, and those
readings are recorded in T2 as the aged-index measurement (A1).

Phase 2, on a PITR fork of production: REINDEX the largest `pci_` index
(`code__1-1`) and one leaf's HNSW index under concurrent inserts into the same
leaf every 0.2 s (RDR-227 A3's method). Record time, lock waits and insert
latency; terminate the builder backend mid-REINDEX once and show the next pass
drops the leftover; show the canary before and after reads within the Gap 1 bar.

### Proposed beads

Not created; this list is for planning at acceptance. Each implementation bead
is test-first: its failing test lands first in the same branch.

**Phase 1: anchor and canary (no REINDEX)**

- P1.1 The anchor: `runComment` and its sanction; write after each build; adopt
  unanchored indexes; read it in the reconciler and the read half. Tests: a build
  writes a parseable anchor; an unanchored index is adopted once; a comment
  survives `REINDEX INDEX CONCURRENTLY` run by hand in the test (A5, first half).
- P1.2 The query ring: admission rule, dedupe, bounds, per-tenant keys, nothing
  persisted or logged. Tests: a single-collection search admits; a fan-out admits
  only the nearest arm of its model; a second tenant never sees the first's
  vectors; the bound evicts least recently used.
- P1.3 The canary runner on the holder: the Gap 1 method through the serving
  path, the plan check, triggers, pending runs, per-pass budget, the log line.
  Tests on a substrate leaf: green on a fresh index; red when the serving path is
  forced to a degraded walk by a test hook; `unmeasured` with too few queries and
  with the index unused; a canary statement takes a search permit.
- P1.4 The `maintenance` status object (canary half, `reindex` reads `off`):
  `StatusHandler`, the wire ledger, the client fixture. Test: the key is absent
  when no source is wired, as RDR-227's is.
- P1.5 Substrate measurement of A3: bytes per row under ingest to twice the
  built size, and under 25% and 50% churn, before and after `VACUUM`. Results in
  T2 and in the bead.
- P1.6 Engine cut and cloud run; first canary readings to T2 (A1); a read-only
  query by conexus for A4 (per-leaf dead fraction, `last_autovacuum`, `VACUUM`
  durations). A red reading here is acted on by hand and reported to Sam.
- P1.R Review gate (code-review-expert and substantive-critic; "Ship when the
  canary's tests are green on the substrate and the status fixture round-trips").

**Phase 2: event-keyed REINDEX**

- P2.1 Thresholds re-read against P1.5 and P1.6; Sam confirms or changes them
  (Open Question 1).
- P2.2 `runReindex` and `runVacuum` with their sanctions and name guards;
  `NX_SEARCH_PCI_REINDEX`; the planner's REINDEX action with the size, deletes,
  bulk-delete and red-canary triggers; re-anchor and re-measure; per-pass and
  per-index limits. Tests: pure ingest to twice the size does not trigger; 25%
  churn triggers; a 10% bulk delete triggers REINDEX then VACUUM; a red canary
  triggers once and a still-red re-measure reports `red_after_reindex`; the
  router's set never loses the collection during a REINDEX; a migration
  terminates a REINDEX and the next pass drops the leftover; `NX_SEARCH_PCI_REINDEX=0`
  builds and measures but never reindexes.
- P2.3 Leaf HNSW index: identification in the catalog read, the size rule, the
  leaf leftover extension through the shared drop path (Open Question 3). Test: REINDEX of a leaf index keeps
  it attached to the parent index (A5, second half).
- P2.4 Fork rehearsal (Minimum Viable Validation, Phase 2).
- P2.5 Engine cut; cloud enable after Sam's go, with conexus told first for the
  STEP-6 re-gate.
- P2.R Review gate, then the phase-review gate across both phases before close.

### Phase dependencies

P1.1 blocks P1.3 and P2.2. P1.2 blocks P1.3. P1.5 and P1.6 block P2.1, which
blocks P2.2 and P2.3. P2.4 blocks P2.5. nexus-s9wav (R1) is independent but
should land before P2.5, so autovacuum is already acting at 5% when REINDEX is
enabled; nexus-dxsjc (R4) consumes the status object and may land before or
after P1.4.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Anchor comments | `obj_description` on `pci_` indexes | the status entry's `anchor`, `size_ratio`, `deletes_since_build` | dropped with the index | adopted again if unparsable | N/A (derived) |
| Canary results | status `maintenance.canary` | `event=pci_canary` log lines | lost on restart by design | the doctor row (nexus-dxsjc) | logs |
| REINDEX | status `reindexing`, `last_reindex_at` | `event=pci_ddl_done op=reindex took_ms=` | N/A | `reindex_leftover` returns to 0 | N/A |

To stop maintenance without stopping builds, set `NX_SEARCH_PCI_REINDEX=0` and
restart the engine.

### New Dependencies

None.

## Test Plan

Listed with each bead above. Across them: every new admin statement is
exercised on a substrate leaf through the builder, never by a test-only path;
the canary is tested through the serving statement path, so a change to serving
settings changes what the canary measures (`HnswServingGucParityTest` already
pins that every site setting `hnsw.iterative_scan` also sets the plan mode); and
`RawSqlGateTest` fails until the three new methods are registered.

## Validation

### Testing Strategy

Engine integration tests on a substrate leaf seeded to reproduce the crowd-out
(RDR-227's fixture), plus the Phase 1 cloud readings and the Phase 2 fork
rehearsal for scale.

### Performance Expectations

Canary: about 25 to 35 s of database time per full run over the cloud's five
indexes, spread over passes by the 60 s budget. REINDEX of a `pci_` index: 9 to
21 s each on production-shaped data. Leaf REINDEX: unmeasured on the managed
instance (A4). No search path changes, so no serving latency change is expected
outside a REINDEX's write amplification.

## Open Questions for Sam

All four answered by Sam on 2026-10-20 ("go with your recommendations").

1. **Thresholds.** The draft carries your provisional values (1.4, 25%, 10%,
   leaf 1.5, mean recall 0.99, any top-1 miss) and adds four limits of its own:
   at most two REINDEXes per pass, a red-canary REINDEX at most once per index
   per 24 hours, an autovacuum-triggered canary at most once per index per 6
   hours, and a 60 s canary budget per pass. *Decided:* all confirmed as
   provisional; P2.1 re-reads them against Phase 1's measurements.
2. **Anchor storage.** The draft uses `COMMENT ON INDEX` (no changeset). A table
   would mean one changeset, row-level security and a fork rehearsal.
   *Decided:* the comment.
3. **Leaf leftovers.** A REINDEX of a leaf's own HNSW index can leave an invalid
   `_ccnew`/`_ccold` that is not a `pci_` name, so decision 4's carve-out does
   not cover it. The draft extends the carve-out to that one name pattern on
   leaves the builder resolved. *Decided:* approved, on the condition that the
   leaf leftover goes through the same drop path as the `pci_` leftovers (the
   `indisvalid` re-read and the 5 s lock timeout); the lock timeout is what keeps
   an index whose build is still running from being dropped (A5).
4. **Settings.** The draft adds one switch, `NX_SEARCH_PCI_REINDEX`, and keeps
   thresholds as constants changed by an engine cut. Settings per threshold are
   the alternative. *Decided:* one switch, thresholds as constants.

## Finalization Gate

### Contradiction Check

RDR-217's premise that a bulk-built index retrieves better contradicts the
measurements this design rests on; it is recorded under Relationship to Prior
RDRs and not edited. Within this RDR, "never on ingest" and the size rule do
not conflict: A3 measured pure ingest at 1.000 of the build.

### Assumption Verification

A3 and A5 are verified by a local spike (2026-10-20); P1.5 and the tests in
P1.1 and P2.3 pin them in the engine. A1, A2, A4 and A6 are unverified and are
verified by Phase 1's cloud run.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `REINDEX INDEX CONCURRENTLY` on a partial HNSW index and on an attached partition index | PostgreSQL 17, pgvector 0.8.2 | Docs; substrate tests P1.1 and P2.3 (A5) |
| `COMMENT ON INDEX`, `obj_description` | PostgreSQL 17 | Docs; test P1.1 |
| `VACUUM (ANALYZE)` on a leaf partition by its owner | PostgreSQL 17 | Docs; existing precedent `TenantScope.vacuumAnalyze` |
| `pg_stat_user_tables` per leaf | PostgreSQL 17 | Docs; the proposal's [23759] note that per-leaf counts are fractions, not totals |

### Scope Verification

Both Minimum Viable Validations are in the plan (P1.6, P2.4) and neither is
deferred.

### Cross-Cutting Concerns

- **Multi-engine**: only the builder-lock holder runs REINDEX, VACUUM and the
  canary. Retry state, previous counts, canary results and the ring are in that
  engine's memory and are lost on restart or when the holder changes; the
  anchor, which survives both, carries the deletes-since-build backstop. Each
  engine reports its own canary history.
- **Local and cloud parity**: the same engine code in both modes. The local
  daemon passes `NX_DB_ADMIN_*`, so the local engine holds the lock and runs all
  of it. Local builds and REINDEXes are slower at the bundle's 64 MB
  `maintenance_work_mem`. A local install on bge (768 dimensions) has no
  install reference until its first fresh `pci_` build, so its leaf size rule
  stays inactive until then.
- **Tenancy**: the canary runs as nexus_svc under the tenant's scope; the ring
  is per tenant; the status carries no names; the counts carry an explicit
  `tenant_id` predicate.
- **Deployment and rehearsal**: no Liquibase changeset, so nothing schema-wise
  to rehearse; the first REINDEX on production is rehearsed on a fork (P2.4).
  Sam chose the comment anchor (Open Question 2), so no changeset is added.
- **Credentials**: unchanged from RDR-227 (restart-bound admin credentials).
- **Versioning**: the status key is additive; the client half (doctor row) is
  nexus-dxsjc's and tolerates its absence.

### Proportionality

Phase 1 adds no DDL beyond a comment and gives the first measurement of aged
indexes, which may show that Phase 2 has little to do. Phase 2 adds one
statement kind that does real work (REINDEX) and one (VACUUM) used only after a
bulk delete, both on an existing session under an existing lock.

## References

- T2 `nexus/4lnn1-hnsw-maintenance-policy-proposal` (sections 1 to 5, Unknowns),
  `nexus/4lnn1-decisions-2026-10-18`.
- `docs/rdr/rdr-227-per-collection-hnsw-above-the-router-threshold.md`: Gap 1,
  Lifecycle (Migrations), Schema (the leftover carve-out), Trade-offs (the
  admin statement set), Day 2 Operations.
- T2 `nexus_rdr/225-recall-per-leaf-analysis`,
  `nexus/rdr-192-livec-recall-extended-2026-09-30`,
  `nexus/p02-hnsw-incremental-recall-2026-09-25`, `nexus_rdr/227-research-3`,
  `-11`, `-12`, `nexus_rdr/227-decision-4-amendment-reindex-leftovers`.
- conexus records [23758], [22578], [22526], [23759]; conexus-h2ol, conexus-kbdp.
- Source: `PciBuilderSession`, `PciReconciler`, `PciReconcilePlanner`,
  `PciCatalog`, `PciPlanCheck`, `PgVectorRepository.runPlainSearchStatement`,
  `RawSqlGateTest.SANCTIONED_STATEMENTS`, `vectors-020-chunks-last-written-at.xml`.
- Beads nexus-4lnn1 (this work), nexus-s9wav (R1), nexus-dxsjc (R4), nexus-rn1yd
  (leftover drop, landed).

## Revision History

- 2026-10-18: Drafted from the proposal and Sam's decisions on develop
  `58056a574`.
- 2026-10-20: Sam's answers to Open Questions 1 to 4 recorded (thresholds
  confirmed as provisional, comment anchor, leaf leftover carve-out through the
  shared drop path, one switch).
- 2026-10-20: A3 and A5 verified by a local spike (`nexus_rdr/230-research-11`,
  `-13`); the size rule recorded as a space signal, and the lock timeout, not
  the `indisvalid` re-read, as what protects a running build from the leftover
  drop.
