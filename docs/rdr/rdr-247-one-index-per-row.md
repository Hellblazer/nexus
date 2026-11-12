---
title: "One Index per Row: Index Each Vector Once, Not in Both the Leaf Graph and a Per-Collection Graph"
id: RDR-247
type: Architecture
status: deferred
priority: high
author: wren and linda (for Sam)
created: 2026-11-10
related_issues: [nexus-f3yqd.8, nexus-f3yqd]
related_rdrs: [RDR-225, RDR-227, RDR-229, RDR-230, RDR-246]
reviewed-by:
---

# RDR-247: One Index per Row: Index Each Vector Once, Not in Both the Leaf Graph and a Per-Collection Graph

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

**Status of this draft.** Sam called this an emergency on 2026-11-10. The
options are measured (M2 to M5 and the M3 legs; M1 was not run, see Gap
dispositions). Option C, sequenced is chosen: build every per-collection index
first while the leaf graph still serves, then drop the leaf graph. The gate is
in progress, after the round 1 critique. No code is written before the gate.

## Problem Statement

Vectors live in `nexus.chunks`, partitioned first by embedding model and then by
tenant (RDR-225). Each of those partitions, a **leaf**, carries one HNSW
(hierarchical navigable small world) graph index over every row in it. RDR-227
then added a second, **partial** HNSW index per large collection (a collection
with at least 20,000 rows; these are the `pci_` indexes), so that a search
inside one large collection walks a graph holding only that collection's rows.

So every row of a large collection is in two graphs. That costs three things:

1. **Twice the index work on every insert** into a large collection: each new
   row is added to the leaf graph and to its collection's graph.
2. **A bulk backfill on every existing store.** An engine that adds `pci_`
   indexes to a store that never had them must build one graph per large
   collection from rows that are already indexed once. On Sam's work machine
   (several collections of 0.5 to 1 million 768-dimension rows) that was hours
   of writes at about 90 MB/s, made worse by restarts throwing builds away
   (fixed in engine 0.1.186, nexus-f3yqd; measurement in T2
   `nexus/f3yqd-pci-restart-rebuild-measurement-2026-11-10`).
3. **Twice the index disk** for large collections. A graph costs about 4.1 KB
   per row at 768 dimensions (M4: the leaf's own graph is 6.15 GB over 1.50M
   rows, and a 500k-row `pci_` is 2.05 GB). RDR-227 Phase 0 had estimated
   8.2 KB per row.

The second graph also exposes the store to every way a `pci_` build can be
thrown away. Two were measured on 2026-11-10: an engine restart ended the build
in flight (fixed in 0.1.186), and a self-retrying changeset kept the boot
terminating builds on every restart. `aspects-005-2` has an `onFail="CONTINUE"`
precondition, so it stays unrecorded until the retired aspect queue is empty,
and the boot counted it as schema work (marginalia's kept-logs run:
`pci_builders_terminated count=3 reason=schema_changesets_pending n=1`; fix for
0.1.187). Each is a full rebuild of an index the rows already had.

Both of the alternatives that would have avoided this were rejected without a
measurement. RDR-225 Alternative 4 (a partition per collection) was rejected for
"the partition count, for no gain the router does not already give".
RDR-227 Alternative 3 (a partition per large collection) was not pursued
because "RDR-225 rejected the wider form" and because crossing the size
threshold moves the collection's rows. No research finding measures partition
count at collection granularity (T2
`nexus/vector-index-single-layout-input-linda-2026-11-10`).

### Enumerated gaps to close

#### Gap 1: A large collection's rows are indexed twice

Every row of a collection at or above the build threshold B (20,000 rows on
develop) is in the leaf graph and in the collection's `pci_` graph. The design
must index each row in exactly one graph, or justify with measurements why a
second graph is worth its cost.

#### Gap 2: The upgrade path for an existing store is a hidden, heavy job

Whatever layout this RDR chooses, an existing store has to reach it. Sam's rule
(nexus-f3yqd.2) is that a data move is a visible, resumable background job with
progress, never a boot step. Each option's upgrade must be stated in those
terms, with its measured cost on a laptop disk.

#### Gap 3: The partition-count objection was never measured

The one measurement of partition count is from tenant leaves (RDR-225
research-15: planning 0.62 ms at 302 partitions against 0.41 ms at 2; creating a
tenant 27 ms). Whether a partition per collection is too many partitions has to
be measured on a store the size of Sam's before it can rule an option out.

#### Gap 4: Collection identity is still read out of the collection name

A collection's name, `<content_type>__<owner>__<embedding_model>__v<n>`
(RDR-103), carries four facts that a relational store should hold in columns.
The name was ChromaDB's only stable key; here it is the leaf routing input, the
`pci_` anchor's key and a partition key candidate, so the index layout this RDR
chooses and the place collection identity lives are one design. The question to
answer: does collection identity (content type, owner, embedding model,
version) move out of the name into columns of a collection table, designed
together with the index layout, so that the name becomes an opaque label? Sam
folded nexus-j4gl6 into this RDR on 2026-11-10 for that reason (its iteration
plan: columns authoritative, readers through one resolver, new writes free of
the encoded shape, the four-segment gate retired). RDR-229 (the collection name
becomes a label) is the prior draft on the reader side and is read with it.

## Relationship to Prior RDRs

- **RDR-225** (vector tables per embedding model) created the leaf layout and
  its leaf-wide HNSW. This RDR revisits its Alternative 4.
- **RDR-227** (per-collection HNSW above the router threshold) added the `pci_`
  partial indexes. This RDR revisits its Alternatives 3 and 4.
- **RDR-230** (event-keyed reindex and recall canary) reads index state; any
  layout change has to keep its canary meaningful.
- **RDR-246** / nexus-f3yqd.2 (data moves as background jobs, dml_utils) is the
  mechanism any row-moving upgrade must use.

## Context

### Background

The **router** sends a search inside one small collection (under T = 20,000 rows
on develop) to an exact scan, because an exact scan of that many rows is fast and
has perfect recall. A search inside one large collection uses its `pci_` index
when it is valid. The leaf-wide graph is one partitioned index on the `chunks`
parent (`idx_chunks_embedding_{384,768,1024}`, vectors-004 lines 319-327), so
one `DROP` on the parent removes it from every leaf. No main code, Liquibase
changeset, health check or `nx doctor` reads it by name; 13 tests do. The
consumer inventory (linda, T2 `nexus/rdr247-leaf-graph-consumers`, file and
line on every row, develop 757384b0c) finds these paths still walking it:

- `/search` over several collections when their union has more than 20,000
  rows: no partial index is implied by `collection = ANY` of several names;
- `/hybrid-search`'s dense leg and its lexical arm, which have no router;
- metadata-, aspect- and graph-hop-scoped search, which are multi-collection
  and have no router;
- a single collection above T with no valid `pci_` index.

Topic-scoped search is single-collection, so a `pci_` index already serves it.
Taxonomy (`assign_from_chashes`, `cross_preview`, its ANN) reads chunks by chash
and walks only the centroid index, never the chunk graph.

### Technical Environment

PostgreSQL 17 with pgvector 0.8.x, bundled for local installs; the managed
service runs the same engine. Local installs run on laptop disks, where a bulk
index build competes with the user's own work. Embeddings are 384, 768 or 1024
dimensions, one model per collection.

## Research Findings

### Investigation

The decision rests on the measurements below (M2 to M5, and M3's depth, overlap,
share, fan-out and drop legs), each run on a lab store, not assumed, by marginalia
on chas, with the results in T2 under `nexus/rdr247-measurement-*`.

### Measurement plan

All runs use marginalia's upgrade-boot harness (nexus-f3yqd.1) on chas, on a
real disk (not tmpfs), with the bundled PG at its defaults plus the 0.1.186
builder memory. The **reference store**: 3 collections of 500k, 750k and 1M
768-d rows, plus 200 small collections of 1k to 15k rows, in one tenant; a second
run with 5 tenants of the same shape.

- **M1, partition count (Gap 3).** Not run. It measured partition count for
  Option B, which was rejected on its row moves, so the count does not decide
  anything here (see Gap dispositions).
- **M2, insert throughput (Gap 1).** Rows per second into a large collection
  with zero, one and two HNSW graphs covering the row (exact only; `pci_` only;
  leaf plus `pci_`), batch sizes 1, 100 and 1,000.
- **M3, search latency and recall per arm.** For a small collection: exact scan
  versus leaf graph. For a large collection: `pci_` graph versus leaf graph at
  `ef_search` 100 and 1000. For a 5-collection search: one leaf statement versus
  per-collection arms merged. Recall against exact top-10, latency p50 and p95
  under the fan-out load RDR-227 used.
- **M4, build cost at size.** One `pci_` build at 500k and 1M rows, at the
  default `maintenance_work_mem` and at the 0.1.186 builder setting
  (min(1 GB, RAM/8)): wall time and bytes written (cgroup io.stat and WAL).
  This also gives the honest time for a first upgrade on a store like Sam's.
- **M5, upgrade cost per option (Gap 2).** For each option below, from a store
  that holds the leaf graph plus a valid `pci_` for every large collection (what
  engine 0.1.184 and later build): total time, bytes written, peak extra disk,
  whether the store stays searchable throughout, and what a restart in the
  middle costs. A pre-PCI store (7.76.0 and earlier, leaf graph only) needs no
  run of its own: it already is Option D's end state, and Option C costs it the
  `pci_` builds M4 measures plus the leaf drop M5 measures.

### Key Discoveries

M4 is measured (T2 `nexus/rdr247-measurement-M4`, marginalia: chas lab-m, 8
cores, 16 GB, ext4 on a real disk, PG 17.5, 2 parallel maintenance workers, the
builder's own statement, each build alone with the engine stopped):

| Rows | `maintenance_work_mem` | Wall time | WAL | Device writes | Index |
|---|---|---|---|---|---|
| 500k | 64 MB (default) | 1,037 s | 1.70 GB | 27.7 GB | 2.05 GB |
| 500k | 1 GB (0.1.186) | 684 s | 1.70 GB | 21.0 GB | 2.05 GB |
| 1M | 64 MB (default) | 2,392 s | 3.40 GB | 63.3 GB | 4.10 GB |
| 1M | 1 GB (0.1.186) | 1,950 s | 3.40 GB | 52.5 GB | 4.10 GB |

- The 1 GB setting saves 34% (500k) and 18% (1M) of wall time and 24% and 17%
  of device writes. WAL does not change: about 0.83 times the index size. Both
  graphs outgrow 1 GB, so the saving falls as the collection grows.
- Device writes are 10 to 15 times the index size, well above the 5.6 times
  estimated from the 80k run. A first `pci_` build of a 1M-row collection on a
  laptop disk is 32 to 40 minutes and 52 to 63 GB of writes, depending on the
  memory setting.
- Gap 1 in bytes: the leaf's own HNSW is 6.15 GB over 1.50M rows (about 4.1 KB a
  row), and a `pci_` index is also about 4.1 KB a row. Each per-collection index
  duplicates its collection's share of the leaf graph about one to one.
- Seeding through a leaf whose HNSW is maintained on insert ran 137 to 180 rows a
  second (M4 runs), so inserts into a large leaf graph are slow (feeds M2).

M5 and M2 are measured (T2 `nexus/rdr247-measurement-M5`, `-M2`, marginalia,
chas, a 500k-row large collection on a leaf of about 541k rows):

| Option | DDL time | Disk freed | Single-collection search | Multi-collection search |
|---|---|---|---|---|
| Option C, one-statement drop of the leaf graph | 0.29 s | 2.2 GB | `pci_`, 24 ms, unchanged | leaf to exact, about 15 ms to 928 ms p50 (about 60 times) |
| D, drop the `pci_` graphs | 0.21 s | 2.2 GB | `pci_` to leaf, about 15 ms | leaf, unchanged |

- The one-statement drop takes AccessExclusiveLock on 18 relations, every
  partition of `nexus.chunks`. With `lock_timeout` 5 s and retry behind a 30 s
  holder it timed out five times and succeeded at 30.4 s, and each waiting
  attempt stalled ALL searches, single and multi, for up to 5 s, because they
  queue behind the lock request. The production-safe shape of the one-statement
  drop still hurts every search during each failed attempt. A plain drop
  interrupted midway rolls back whole (it is transactional) and boots clean.
- The per-leaf variant works: a plain HNSW not attached to the partitioned index
  was created `CONCURRENTLY` on one leaf (1,168 s for about 500k rows, not in
  `pg_inherits`) and dropped `CONCURRENTLY` in 0.38 s.
- D interrupted midway left no INVALID index; the next boot with
  `NX_SEARCH_PCI_MAX_PER_LEAF=0` built nothing and search kept answering from
  the leaf at 15 to 16 ms. (The probes log only from PostgreSQL's first answer,
  so the database-down window of a restart is not in the series.)
- M2, insert throughput at batch 1,000: two graphs 74.7 rows a second, `pci_`
  only 150.6, exact only 2,416. Each graph about halves throughput; WAL per row
  68, 41 and 23 KB. At batch 1, commit cost dominates (21 to 36 rows a second).
- M3, recall on the shared leaf graph against the collection's own `pci_` graph
  (marginalia, chas, stores in RAM; T2 `nexus/rdr247-measurement-M3`). Recall
  is recall@10 against an exact search over 50 queries. Only arms whose plan
  names an HNSW index count as graph numbers: the planner moved the `pci_` arm
  to the primary-key btree at `ef_search` 400 and above, and the leaf arm to a
  sequential scan at 600 and above. `ef_search` stops at 1000 in pgvector.
  - Real bge-768 embeddings of public repositories, an 82k-row leaf. A
    topically distinct collection at 3.2% of the leaf (2,639 rows): leaf .998
    at `ef_search` 40, 100 and 200 (p95 1.4, 1.8 and 2.8 ms), 1.0 at 400;
    `pci_` 1.0 (p95 1.0 to 1.5 ms). A collection at 40.6%: leaf .966, .99,
    .996, .998 at 40, 100, 200, 400; `pci_` .988, .988, .996, .998. Five
    collections in one statement: leaf .974 to .996, about twice as fast as
    merging per-collection searches.
  - Synthetic clustered sets (221k-row leaf) were much harsher: a 3% collection
    reached only .94 (tight clusters) and .874 (loose) at `ef_search` 600 on
    the leaf, against 1.0 on `pci_`. Their independent random centres put
    every collection's points among foreign ones at similar distances, which
    real text does not do, so the real leg decides.
  - The hard case real data shows is topical overlap: a 12-row collection of
    C code (pgvector) inside a large one (postgres) got leaf recall 0, .1 and
    .26 at 40, 100 and 200. At 12 rows it is below every `pci_` threshold and
    production serves it by exact scan, so it is not a case for a layout
    without per-collection graphs.
  - The overlap leg (real bge-768, the nexus repo, a 58,919-row leaf; two
    slices of the code collection moved into collections of their own, rows
    and manifest together, each with a `pci_`):
    - One subdirectory (`src/nexus/plans`, 1,377 rows, 2.3%), a realistic
      overlap: leaf .924, .966, .996 at `ef_search` 40, 100, 200 (p95 1.9,
      3.6, 6.2 ms); `pci_` 1.0 at about 1 ms.
    - A random 3% slice (1,767 rows) interleaved through its neighbour, the
      worst case: leaf .238, .43, .70 at 40, 100, 200; `pci_` 1.0.
    - At `ef_search` 400 the planner left the graph for a sequential scan on
      both slices. The large collection (94.6%) matched its own graph.
    So the leaf graph serves a realistically overlapping small collection at
    `ef_search` about 200, single-digit milliseconds and about six times the
    `pci_` latency, and fails a small collection whose rows are interleaved
    with a larger one's. A mixed bag of notes drawn from the same sources as a
    large collection can approach that worst case.
  - The share sweep (the nexus repo again, a 59,088-row leaf; a random slice
    interleaved through the code collection at each share, with its own
    `pci_`; 200 held-out rows as queries, none of them stored; every leaf arm
    on the leaf HNSW). Leaf recall at `ef_search` 40, 100, 200, 400, leaf p95
    3, 4.6, 8 and 12 to 13.6 ms:
    - 60.9%: .98, .998, .998, .998 (`pci_` the same).
    - 25%: .886, .994, .998, 1.0 (`pci_` .98, .994, .998, 1.0).
    - 10%: .426, .856, .978, .99 (`pci_` .992 to 1.0, p95 1.4 to 3.9 ms).
    - 3%: .114, .26, .55, .948 (`pci_` .994 to 1.0, about 1.3 ms).
    - 1%: .034, .094, .182, .41 (`pci_` .998).
    For interleaved rows at a serving `ef_search` of 100 to 200, the boundary
    lies between 10% and 25% of the leaf. A rule on share alone is
    conservative for a topically distinct small collection, which reached
    .998 on the leaf at 3% in the public leg.
  - The planner finding bears on RDR-227 Phase 0 too: Phase 0 measured the leaf
    graph at `ef_search` 1000, where these runs show the planner choosing an
    exact scan instead. Phase 0's recall at 1000 may have been an exact scan's.
  - The depth check (Sam's choice: interpolated points, in RAM on lab-m; T2
    `nexus/rdr247-measurement-M3-depth`). The 59k nexus store grown to a
    998k-row leaf by interpolating random pairs within each collection, shares
    held; held-out real queries; ground truth an exact search over the full
    leaf; every plan read. Leaf recall at `ef_search` 40, 100, 200, 400 (leaf
    p95 5, 8.5, 17, 23 ms):
    - 61%: .866, .958, .982, .988.
    - 25%: .564, .864, .95, .974 (`pci_` .94, .968, .998, 1.0).
    - 10%: .336, .558, .836, .912 (`pci_` .934 to 1.0).
    - 3%: .07, .184, .366, .612 (`pci_` .922 to 1.0).
    - 1%: .034, .058, .112, .24 (`pci_` .968 to 1.0).
    At 1M rows the shared graph loses recall at every share at a serving
    `ef_search`, even for the 61% collection (.958 at 100, against .998 at
    59k), and the share at which it matches a collection's own graph rises
    with the leaf's size: above 25%, nearer half. A boundary on share alone
    does not hold at depth. Limits: interpolated points, worst-case
    interleaving, one leaf; topically distinct small collections were not
    re-tested at depth; the 61% collection had no own graph in this run, so its
    own-graph recall at depth is not measured. The run wrote 0 MB and peaked at
    14.2 of 16 GB.

What M5 and M2 do to the options: Option C, the one-statement drop, buys back
insert throughput and disk. It costs about 60 times on multi-collection search
when the leaf graph is gone with no fan-out, and it takes an exclusive lock that
stalls every search while it waits. Option D buys the same insert throughput and
disk with a sub-second drop that searches do not queue behind. Its open question
was recall on large collections, which M3 answers (above): the shared graph
loses recall at depth.

Earlier findings and the consumer inventory:

- The only partition-count number is per tenant (RDR-225 research-15, above).
- The `pci_` build at the PostgreSQL default memory writes about 2.7 to 4 times
  the bytes of an in-memory build at 80k rows (T2
  `nexus/f3yqd-pci-restart-rebuild-measurement-2026-11-10`); M4 (above) measured
  the 500k and 1M builds.
- From the consumer inventory (linda):
  - Dropping the leaf index cannot make an answer wrong. With no usable index
    the plan is an exact sort, so the risk is latency only (the vectors-004
    sequential-scan class).
  - A `pci_` index is used only when the planner can prove
    `collection = 'x'`, which takes a one-element array, an inlined SQL
    function and `force_custom_plan`. The single-collection serving arm sets
    all three. Every route without a router also sets `force_custom_plan`,
    verified by reading: hybrid ranking (`PgVectorRepository` line 4038,
    before any branch), the scoped and graph-hop routes (lines 5446 and 5513),
    the plain arm (line 1773) and taxonomy ANN
    (`TaxonomyCentroidRepository` line 249). So a per-arm split needs no new
    plan-mode pin, provided each call stays in the transaction that sets it
    and the functions stay inlinable (`CombinedQueryParityTest` pins that for
    the scoped routes).
  - `DEFAULT_SEARCH_PCI_MAX_PER_LEAF = 16` (`PgSession` line 860): the 17th
    large collection on a leaf gets no `pci_` index, and without the leaf graph
    it would be scanned exactly for ever.
- Exact-scan cost, two environments. On the cloud, with a buffer pool smaller
  than the vectors, one 29k-row collection took about 0.8 s under 5-permit load:
  about 27 µs a row (linda's inventory, T2 `nexus/rdr247-leaf-graph-consumers`).
  Extrapolated, not measured: about 8 s at 300k rows and about 27 s at 1M, which
  is near the 30 s arm timeout. In RAM on chas, a forced sequential scan of the
  whole 998k-row leaf, filtered to one collection (M3 depth), took p95 0.98 s for
  a 250k-row collection and 1.35 s for a 609k-row one, which is about 2.2 µs a
  collection row at 609k. The scan reads the whole leaf, so its cost follows the
  leaf size. The figures differ about twelvefold per row because the cloud run
  is under load and has a small buffer pool and the RAM run is not. Only the RAM
  figures are measured at 250k and 609k, so A2 is verified in RAM only.
- Found and fixed during this RDR (linda, nexus-f3yqd.8, shipped in engine
  0.1.187: e53bacbaf, cb1f8a41a, 0c6f9e1c5): the hybrid selective by-chash
  branch ranked a fixed set of up to 1001 chashes without turning index scans
  off, so the planner could walk the leaf graph for it. Every branch of the
  selective hybrid rank now disables index scans first, so neither option
  needs it as a prerequisite.

### Critical Assumptions

- **A1.** Every consumer of the leaf-wide graph can be served either per
  collection or by an exact scan, at acceptable latency. Verified by the
  consumer inventory plus M3.
- **A2.** A large collection whose own index is missing or building can be
  served by an exact scan with a timeout, at acceptable latency for 1M rows.
  Verified by M3 depth in RAM only. The cloud figure (27 µs a row under load)
  puts a 1M-row collection near the 30 s timeout, so the cloud case is open.
- **A3.** A partition per collection does not slow planning or collection
  creation past the M1 pass lines. Not verified: M1 was not run, because Option B
  was rejected on row moves. A3 applies only to that rejected option.

## Proposed Solution

### Approach

Four layouts and one variant were considered. Each is judged on Gaps 1 to 3,
and each upgrade must be a visible, resumable job or nothing at boot (Sam's
rule, nexus-f3yqd.2).

- **Option A, keep two indexes but make them zero-impact.** Keep the leaf graph
  and the `pci_` graphs. Make the backfill invisible to the user: build at low
  priority (one build at a time, paced), with the 0.1.186 builder memory so it
  does not spill, reported in `boot_work`, and kept through restarts (0.1.186).
  Leaves Gap 1 open by design: every large-collection insert still does two
  graph inserts, and the disk cost stays.
- **Option B, a partition per collection.** Each collection, or each large
  collection, gets its own partition with one plain HNSW index; small
  collections share a default partition with one graph. One graph per row.
  Upgrade rewrites every chunk row once (a dml_utils background job, with the
  heap doubled during the copy); a collection crossing the threshold, and a
  rename, moves rows; creating a collection is partition DDL, which needs the
  table owner (RDR-227's A5 builder problem).
- **Option C, one-statement drop.** Small collections are served exact (the
  router already does this for single-collection searches); each large
  collection has only its `pci_` graph. Upgrade drops the leaf graph in one
  statement: it frees disk and moves no rows. PostgreSQL cannot drop a leaf
  index on its own, and it cannot drop a partitioned index concurrently, so the
  statement takes AccessExclusive on every partition of `nexus.chunks` for every
  model and tenant at once (M5: 18 relations). Rejected; see Decision Rationale.
- **Option C, sequenced (chosen).** The end state of Option C, reached in two
  jobs instead of one statement. Job 1 builds the `pci_` of every large
  collection while the leaf graph still serves. Job 2 runs only after every
  collection at or above B has a valid `pci_` for a dimension. It drops that
  dimension's parent index with `DROP INDEX IF EXISTS`, under a `lock_timeout`
  of 250 ms or less, with a backoff that doubles to a cap and a bounded number
  of attempts. Nothing runs at boot.
- **Option D, leaf graph only.** Drop the `pci_` indexes and walk the leaf graph
  at `ef_search` 1000 above T, with T at 30,000 (RDR-227 Alternative 4). Cheapest
  upgrade. Rejected on recall at depth.

Option C, sequenced is the choice. It indexes each large-collection row once
(Gap 1), moves no rows (Gap 3), and frees the leaf graph's disk when job 2
completes. Its multi-collection cost is measured below. The rejected layouts and
the rejected per-leaf variant of Option C are in Alternatives Considered, and
Gap dispositions says how the choice closes each gap.

#### Routing while the leaf graph exists

Until job 2 completes for a dimension, routing is unchanged from today. A large
collection without a valid `pci_` is served by the leaf graph, and
multi-collection search keeps its current path. The per-collection fan-out for
the routes that have no router today, and the exact fallback for a large
collection with no valid graph, take effect only after job 2 has completed for
that dimension. The engine detects whether the parent index exists, so it knows
which path applies. A store that never gets job 2 keeps today's routing, and
pays no fan-out cost. Raising the per-leaf cap (Technical Design 2) queues builds
for collections past the sixteenth on a leaf, reported in `boot_work`.

#### Measured cost of the fan-out

Multi-collection cost under Option C, sequenced at 1M rows, measured with a bench
model of the engine's fan-out (pool of ten, `force_custom_plan`, waves of five
arms, eight permits shared across requests, the 20 s budget never hit), not the
engine's routes themselves, on interpolated points, one leaf (T2
`nexus/rdr247-measurement-M3-depth`, section B). Recall, then p95 for one request
and for five concurrent, in ms:

| Set | Leaf graph | Option C, sequenced fan-out |
|---|---|---|
| {25%, 3%}, `ef_search` 100 | .898, 10.2 / 13.4 | .99, 14.7 / 13.4 |
| {25%, 3%}, `ef_search` 200 | .972, 17.4 / 24.8 | 1.0, 20.0 / 24.2 |
| 5 collections, 100 | .99, 10.1 / 12.5 | .992, 12.4 / 32.4 |
| 5 collections, 200 | .996, 15.9 / 23.6 | .996, 21.5 / 59.0 |
| all 7 (two waves), 100 | .99, 10.1 / 13.8 | .992, 14.3 / 27.8 |
| all 7, 200 | .996, 16.8 / 24.3 | .996, 21.3 / 58.6 |
| small union (10k rows) | .058 and .112, unusable | router exact, 1.0 |

The fan-out costs 1.2 to 1.45 times the leaf graph for one request, and 1 to
2.6 times at five concurrent requests, where the shared permits queue the arms.
Its recall is equal where the leaf graph does well, and better when an
interleaved small collection is in the set. M5's "about 60 times" was the
one-statement drop with no fan-out: one statement over four collections on a
541k-row leaf, with the leaf graph gone, fell to an exact scan. The run's
router-exact figure (p95 118 ms, 635 ms at five concurrent) is pessimistic. That
arm forced a sequential scan of the whole 1M leaf, while the engine's exact path
for a small union reads the collections by key, as the tiny per-collection arms
did in milliseconds.

#### The upgrade: build first, drop last, nothing at boot

PostgreSQL cannot drop a partitioned index concurrently or one leaf at a time,
so each drop takes AccessExclusive on the parent and every partition. The order
keeps every collection indexed throughout:

1. Job 1 is a visible, resumable job. It builds the `pci_` of every large
   collection while the leaf graph still serves. It uses 0.1.186's paced,
   in-memory builder and reports in `boot_work`. Build times in RAM: 31 s for
   100k rows, 78 s for 250k and 689 s for 609k (marginalia's run report, not in
   the T2 record). On a laptop disk a first 1M-row build takes 32 to 40 minutes
   (M4).
2. Job 2 starts only when every collection at or above the threshold has a valid
   `pci_`. It runs `DROP INDEX IF EXISTS` on the dimension's parent index. The
   operator starts it, or it is scheduled after boot, and `/v1/status` reports it
   (pending, done, attempts, last lock timeout). Measured under live search load
   (T2 `nexus/rdr247-measurement-M3-drop`, five searchers, baseline p95 7.1 ms):
   with no long reader the drop takes its lock on the first try in 5 to 7 ms.
   Behind a 30 s reader, every failed attempt stalls the searches that overlap
   it for the length of the lock timeout (257 to 265 ms at a 250 ms timeout, 493
   to 510 ms at 500 ms), with no search errors. The total time tracks the
   longest reader (about 30 s), not the timeout. So job 2 uses a `lock_timeout`
   of 250 ms or less, a backoff that doubles from 100 ms to a cap, and a bounded
   number of attempts, after which it reports and resumes later. A reader the
   engine does not control (an anti-wraparound autovacuum on a leaf, an outside
   session, a backup) only makes it retry.

Job 2 frees the disk later than a boot drop would. In exchange, once job 2 has
run for a dimension, a large collection without a valid `pci_` is ranked
exactly, not by the leaf graph. That exact path costs about 1 s at 250k rows
(p95 0.98 s, 1.8 s at five concurrent; M3 depth and M3 drop) and 1.35 s at 609k
rows alone (M3 depth), with the arm timeout of 30 s as the bound.

### Technical Design

Option C, sequenced (linda's draft for parts 1 to 3, T2
`nexus/rdr247-c-prime-design-linda-draft`; line numbers are from develop
757384b0c and are re-checked at implementation).

**1. A fan-out behind every dense leg.** Today hybrid ranking's dense leg
(manifest search with a text gate, `text_gated_search_hnsw_first`) and the
scoped routes (metadata, aspect and graph-hop, through
`runCombinedQueryWithChash`) reach the leaf graph with no router; plain search
and manifest search without text already pass the router. Under Option C,
sequenced every dense leg works the same way:

1. Probe each requested collection's row count (the router's probe).
2. Rank a collection at or under the exact threshold (`NX_SEARCH_EXACT_MAX_ROWS`,
   20,000) exactly, all of them in one statement when the whole union is under
   it.
3. Give each collection above it an arm: one short transaction on its own
   connection, with `force_custom_plan` and a one-element collection list, so
   the planner proves `collection = 'x'` and uses that collection's `pci_`.
4. Merge by distance; one leaf holds one embedding model, so distances compare.

The arms reuse the existing fan-out (nexus-tu8wp.1, `TenantScope.fanoutArmGate`)
at its shipped defaults: five arms at a time per request, a cross-request cap
of the pool less headroom, a 20 s budget. Graph-hop's reached set is a small
fixed set of chashes and is ranked exactly under the threshold (inferred; to be
confirmed by measurement). The fixed-set hybrid rank is already exact (0.1.187).
These arms are active only after job 2 has completed for the dimension (Approach,
routing).

**2. The per-leaf cap is raised.** `DEFAULT_SEARCH_PCI_MAX_PER_LEAF = 16`
(`PgSession` line 860) is raised to a high value, not removed. Without a raise, a
seventeenth large collection would be ranked exactly for ever once the leaf graph
is gone. Raising it to no cap is not proposed: the planning and insert cost of
many partial indexes per leaf is not measured, and the current maximum setting
is 1,000 (`SEARCH_PCI_MAX_PER_LEAF_MAX`, `PgSession` line 867). Disk falls rather than grows: each
large collection's `pci_`, about 4.1 KB a row (M4), replaces its share of the leaf
graph, and job 2 frees the leaf graph itself (M5 freed 2.2 GB on a 541k-row leaf).
The build threshold B and the build concurrency stay.

**3. A collection without a valid graph (A2).** Once job 2 has run for a
dimension, a collection at or above B with no valid `pci_` is ranked exactly
under the arm's statement timeout (30 s, `PgSession` line 192). The response
marks the arm degraded, and `/v1/status` and `nx doctor` list it as building
(collection, rows, started). Before job 2, that collection stays on the leaf
graph, as today. The exact cost is in Research Findings. The builder keeps large
collections out of that window:

- never drop before replace: a rebuild builds under a temporary name and swaps
  only when the new index is VALID;
- a FAILED_BUILD is rebuilt in the same pass (ab251481a);
- a canonical rename of a large collection copies, keeping its index.

Settings that would open holes once the leaf graph is gone for a dimension are
refused at boot, naming the setting: `NX_SEARCH_PCI=0`,
`NX_SEARCH_EXACT_MAX_ROWS=0`, and a build threshold above the router threshold.
Before job 2 those settings are allowed, as today, because the leaf graph still
covers them. A builder without the privilege to build is reported in
`/v1/status` and `nx doctor` as "no index for N collections", never left exact
silently.

**4. The upgrade.** See Approach: job 1 builds every `pci_` first in a visible
job while the leaf graph serves; job 2 drops the dimension's parent index once
coverage is complete; nothing runs at boot. Job 2 is operator-started until open
item 7 below is closed. On a fresh install, vectors-004 still creates the leaf
graph, and the same two jobs apply.

**Open item 7: many small collections.** After job 2, a multi-collection search
over many small collections whose union exceeds the exact threshold has no
measured rule. Example: 200 collections of 1k to 15k rows each, whose union is
far over 20,000 rows. Each one is under the threshold, but the union is not, and
Technical Design 1 does not say whether the request goes out as one statement (a
scan of a large leaf, the case the fan-out exists to avoid) or as one arm per
collection (200 arms, far more than a wave of five). M3 tested at most seven
collections per request. Job 2 stays off by default until this case is measured
and a rule is chosen, for example a per-request cap on exact rows.

**Tests to rewrite** (they name `idx_chunks_embedding`):
`PgVectorRepositoryRawSqlPlanShapeTest`, `PlainSearchTextGatedSearchExplainTest`,
`ChashProbePlanShapeTest`, `CombinedQueryParityTest`, `GraphHopParityTest`,
`HybridSearchFunctionParityIntegrationTest`, `HybridSelectiveGateTest`,
`DenseGateScanBudgetIntegrationTest`, the `ChunkLiveOwnersMsz9iScale` and
`RecallExtended` integration tests, `P225MigrationWalkIntegrationTest`,
`VectorsUnifyChunksIntegrationTest` and `db.changelog-test-reindex-index.xml`.
`HnswServingGucParityTest` must cover the new arms.

### Existing Infrastructure Audit

- The router and the exact path: `PgSession` (`DEFAULT_SEARCH_EXACT_MAX_ROWS`),
  `PgVectorRepository`.
- The `pci_` build, keep and adopt path: `PciReconciler`, `PciReconcilePlanner`,
  `PciBuilderSession`, `PciCatalog` (engine 0.1.186).
- Progress reporting: `boot_work` in `GET /v1/status` (nexus-f3yqd.3).
- Background data moves: RDR-246 / nexus-f3yqd.2 (dml_utils, pg_background).

### Decision Rationale

Option C, sequenced is the only layout here that serves single-collection search
at scale and indexes each large-collection row once. At 1M rows the shared leaf
graph loses recall at every share at a serving `ef_search` (M3 depth: .958 for a
61% collection, .864 for 25%, at 100), while a collection's own graph holds .968
to .998, so D and D′ fail where the stores that hurt live. Option A keeps the
second graph, and with it Gap 1 (two graph inserts a row, about half the insert
throughput, M2) and the disk.

Option C, the one-statement drop, is rejected for its lock. It takes
AccessExclusive on every partition of `nexus.chunks` with no coverage check and
no bound on the wait, and each waiting attempt stalled every search (M5: the
5 s attempts stalled both probes up to 5 s, and the drop succeeded at 30.4 s).

Option C, sequenced differs in two things. It checks coverage first: every large
collection has a valid `pci_` before the drop starts. Then it waits for the lock
in a bounded, retried way, so the worst added search latency per attempt is the
lock timeout (M3 drop: 257 to 265 ms at a 250 ms timeout), not an unbounded wait.
It still takes the same parent-wide lock, once, after coverage is complete. Its
fan-out costs 1.2 to 1.45 times the leaf graph per request and 1 to 2.6 times at
five concurrent, with equal or better recall (M3 depth, section B).

### Gap dispositions

- **Gap 1 (large collections indexed twice): closed by Option C, sequenced.**
  After job 2, each large collection's rows are in one graph, its `pci_`. Small
  collections have none.
- **Gap 2 (hidden, heavy upgrade): the `pci_` builds remain a visible,
  resumable background job** (the 0.1.186 builder, reported in `boot_work`). The
  drop is a bounded job (job 2). Nothing runs at boot. The measured costs are in
  Approach.
- **Gap 3 (partition count): moot.** Option B, the only option that adds
  partitions, was rejected on row moves, so M1 and A3 were not needed.
- **Gap 4 (collection identity in the name): decision pending with Sam (ask
  rdr247-gap4).** Keep collection identity in RDR-229, whose Phase 3 rebuilds
  each per-collection index on a collection_id key as a later visible job, or
  land that key first. Deferring it means one more full `pci_` rebuild per large
  collection after RDR-229 Phase 3: 32 to 40 minutes and 52 to 63 GB of device
  writes per 1M-row collection (M4).

## Alternatives Considered

The options are described under Approach; each rejected one, with its measured
reason:

- **Option A, keep both graphs, invisible build.** Pros: multi-collection
  search stays on one graph; no drop. Cons: every large-collection row is still
  inserted into two graphs (about half the insert throughput, M2) and the disk
  stays used. Rejected: it leaves Gap 1 open by design.
- **Option B, a partition per collection.** Pros: one graph per row. Cons: the
  upgrade rewrites every chunk row, and a collection crossing the threshold or
  being renamed moves rows; creating a collection becomes partition DDL that
  needs the table owner. Rejected: row moves are the shape Sam objected to on
  2026-11-10, and Option C, sequenced gets one graph per row without them. Row
  moves would be acceptable only as an RDR-246 background job with progress; that
  is not the choice.
- **Option C, one-statement drop.** Pros: one graph per row, no row moves. Cons:
  the drop takes an exclusive lock on every partition of `nexus.chunks` with no
  coverage check and no bound on the wait, and each waiting attempt stalled every
  search (M5). Rejected for the lock. Option C, sequenced reaches the same end
  state.
- **Per-leaf variant of Option C (the earlier C′).** Each leaf's graph is rebuilt
  as a plain HNSW index not attached to the partitioned parent, so each can be
  dropped `CONCURRENTLY` per leaf and per tenant. Pros: no parent-wide lock (M5:
  a plain per-leaf HNSW was created in 1,168 s on about 500k rows and dropped in
  0.38 s). Cons: it needs a full rebuild of every leaf graph before the drop, which
  is the Gap 2 cost (1,168 s per leaf of about 500k rows, M5), and every new leaf
  needs a different create path. Rejected: Option C, sequenced reaches the same
  end state with a bounded lock wait and no rebuild.
- **Option D and D′, the leaf graph only (all or for large shares).** Pros:
  cheapest upgrade, multi-collection search on one graph. Cons: at 1M rows the
  shared graph loses single-collection recall at every share (M3 depth: .958
  for a 61% collection and .864 for 25% at `ef_search` 100, against .968 to
  .998 on a collection's own graph), and the share needed grows with the leaf.
  Rejected on recall.

### Briefly Rejected

- **Partial HNSW per model** (RDR-225 Alternative 3): the planner uses a partial
  index only when it can prove the query implies the predicate, and a miss was a
  silent sequential scan. Still relevant to Option C, sequenced, where each large
  collection's index is partial; RDR-227 A1 verified the proof holds on the
  engine's bound, custom-planned call.

## Trade-offs

### Consequences

- Each large-collection row is inserted into one graph, not two. Insert
  throughput for those rows roughly doubles (M2, at batch 1,000: about 75 rows a
  second with two graphs, 151 with one). The leaf graph's disk is freed when job
  2 completes.
- Multi-collection search costs 1.2 to 1.45 times the leaf graph per request and
  up to 2.6 times at five concurrent. At 1M rows that is p95 12 to 22 ms for one
  request and 13 to 59 ms at five concurrent (M3 depth, section B). The small
  collections in a set are ranked exactly; their cost after job 2 is open item 7
  in Technical Design.
- Single-collection search keeps its own graph's recall as the store grows. At
  `ef_search` 100, each collection's own graph gives .968 to .998 (M3 depth),
  against .058 to .864 on the shared graph for the 25% collection and smaller.
- More indexes exist per leaf (no cap of sixteen), each the size of the leaf
  graph's share it replaces (about 4.1 KB a row, M4). Their planning and insert
  cost is unmeasured.
- Once job 2 has run for its dimension, a collection newly crossing the build
  threshold is ranked exactly until its graph is built, and it is reported as
  degraded: 0.11 s p95 for a 10k-row collection and 0.98 s at 250k (M3 depth).

### Risks and Mitigations

- **Job 2 removes the safety net.** Before job 2, a large collection with a broken
  or missing `pci_` falls back to the leaf graph. After job 2 for its dimension,
  it falls back to an exact scan (about 1 s at 250k rows and 1.35 s at 609k, M3
  depth). Mitigated by never-drop-before-replace, same-pass rebuild of a failed
  build, the copy path on rename, the boot refusal of settings that open holes
  (once the leaf graph is gone), and reporting the collection as degraded in
  `/v1/status` and `nx doctor`.
- **Job 2 stalls searches and writes while it waits.** Each failed attempt queues
  the searches and writes that start during it behind its lock request, for the
  length of the lock timeout (M3 drop: 257 to 265 ms at a 250 ms timeout, no
  errors). Mitigated by a `lock_timeout` of 250 ms or less, capped backoff and a
  bounded attempt count.
- **One lock covers every tenant.** One parent index covers each dimension (384,
  768, 1024), so job 2's drop takes AccessExclusive on every partition of
  `nexus.chunks`, for every model and tenant, at once. Coverage is checked across
  all tenants for that dimension, so one tenant's large collection without a valid
  `pci_` holds the drop back for every tenant. On the managed service, that is
  every tenant.
- **The drop is one-way for older engines.** After job 2 for a dimension, an
  engine that expects the leaf graph serves its unrouted multi-collection routes
  by exact scan. Requirement: job 2 starts only when every engine attached to the
  store is at or above the version that ships this design. Recovery is a rebuild
  of the leaf graph. M5 measured 1,168 s to build one plain HNSW on a 541k-row
  leaf; the rebuild of the parent index over every leaf is not measured.
- **The lock hold on the managed service is unmeasured.** M5 measured one 541k-row
  leaf. The lock hold with many tenant leaves must be measured before job 2 is
  enabled on the managed service.
- **Concurrency.** At five concurrent multi-collection requests at `ef_search`
  200 the fan-out is 2.4 to 2.5 times the leaf graph; the pool and arm-permit
  defaults are the lever if that edge bites.

### Failure Modes

- A builder dies or lacks privilege: before job 2 the collection stays on the leaf
  graph, reported; after job 2 for its dimension it is exact and degraded. Job 2
  does not start while any large collection in that dimension lacks a valid graph.
- Job 2 never gets its lock (a long reader the engine does not control: an outside
  session, an anti-wraparound autovacuum, or a `pg_dump` that holds AccessShare):
  the job stops after its bounded attempts, reports, and resumes later. The store
  keeps both graphs meanwhile, which is today's state.
- A crash during job 2: `DROP INDEX` is transactional, so the index is either
  gone or intact, and `IF EXISTS` makes the retry harmless.
- An older engine attached after job 2: its unrouted multi-collection routes run
  as exact scans (see Risks and Mitigations).
- A setting that would leave a collection without a graph after job 2 for its
  dimension: refused at boot, naming the setting.

## Implementation Plan

### Prerequisites

- The consumer inventory (linda), T2 `nexus/rdr247-leaf-graph-consumers`: done.
- Measurements M2 to M5, run with marginalia's harness. M1 was not run (Gap
  dispositions).

The inventory names six steps, in order, before the leaf index can go (Option C
in either form):

1. An engine-side per-arm split, with the router in front of every dense leg
   (RDR-226 Phase 0 found a split adds statements, not database work).
2. Raise the per-leaf cap of 16 `pci_` indexes (Technical Design 2).
3. Builder guards: refuse `NX_SEARCH_PCI=0` and `NX_SEARCH_EXACT_MAX_ROWS=0`
   once the leaf index is gone, and report a large collection with no index in
   `nx doctor` and `/v1/status`.
4. Never drop a valid `pci_` index until its replacement is valid, and rebuild a
   failed one in the same pass (done in 0.1.186). This keeps a 1M-row
   collection off the exact path.
5. The by-chash index-scan fix (done: nexus-f3yqd.8, engine 0.1.187).
6. The `DROP` itself, as an ordered RDR-246 background job that waits until every
   collection at or above B has a valid `pci_` index.

### Minimum Viable Validation

The harness shows, on the reference store, the chosen layout's upgrade from a
0.1.184 store and from a 0.1.186 store, with bytes written, time, searchability
throughout and the effect of a restart in the middle, against Option A as the
baseline.

### Phase 1: Code Implementation

1. The per-collection fan-out behind hybrid ranking's dense leg and the scoped
   routes, with graph-hop exact under the threshold (Technical Design 1).
2. Raise `DEFAULT_SEARCH_PCI_MAX_PER_LEAF` (Technical Design 2).
3. The degraded-arm reporting and the boot refusals (Technical Design 3).
4. The coverage check and job 2, off by default until Phase 2 and until open item
   7 is closed (Approach, upgrade step 2).

### Phase 2: Operational Activation

The build job runs on upgrade (0.1.186's builder, now with no cap). Once
`/v1/status` reports full coverage, job 2 runs, started by the operator until
open item 7 is closed. On the managed service it runs only in an announced
window, and only after its lock-hold measurement.

### Day 2 Operations

`/v1/status` and `nx doctor` show coverage (collections at or above the
threshold with and without a valid graph), builds in progress, degraded arms,
and job 2's state and attempts.

### New Dependencies

None. Job 2 runs as a background job in RDR-246's machinery.

## Test Plan

- Rewrite the tests that name `idx_chunks_embedding` (Technical Design list) so
  they assert the per-collection plan, and extend `HnswServingGucParityTest`
  to the new arms.
- Parity: each multi-collection route returns the same top-k from the fan-out
  as an exact search on a seeded store, including an interleaved small
  collection.
- The degraded path: a collection with no graph is answered exactly, marked
  degraded, within the arm timeout.
- The drop job: refuses while coverage is incomplete; under a held lock it
  retries with backoff, bounds its attempts and reports; it is idempotent.
- The harness upgrade run as a release gate (nexus-f3yqd.1), from a 0.1.184 and
  a 0.1.186 store, with bytes written, searchability throughout and a restart
  in the middle.

## Validation

### Testing Strategy

The measurements above, then the harness gate.

### Performance Expectations

At a 1M-row leaf: single-collection search on its own graph at the current
latency (about 1 to 4 ms); multi-collection search within 1.5 times the leaf
graph per request and 2.6 times at five concurrent; an exact arm under 1 s up to
250k rows; the drop's added search latency at most its lock timeout per
attempt.

## Finalization Gate

### Contradiction Check

Gate round 1 (critique T2 `nexus_rdr` `247-gate-critique-2026-11-12-r1`) found
Option C defined two incompatible ways, Gap 4 dropped, and status and option
text that argued for rejected options. This revision fixes them: one definition
of Option C, sequenced; the per-leaf variant moved to Alternatives; Gap 4 has a
disposition; the stale status, measurement and prerequisite text is removed.

### Assumption Verification

- A1 (every leaf-graph consumer can be served per collection or exactly):
  verified by linda's inventory and M3 depth section B.
- A2 (an exact scan with a timeout is acceptable while a graph is missing):
  verified in RAM only, by M3 depth (0.98 s p95 at 250k rows, 1.35 s at 609k),
  with the builder rules keeping large collections out of that window. The cloud
  figure (27 µs a row under load) leaves the 1M case open.
- A3 applies only to Option B, which was rejected. M1 was not run.

#### API Verification

pgvector 0.8.x partial index planning (RDR-227 A1), which the per-collection
arms rely on, checked against the bundled version; PostgreSQL's refusal of
`DROP INDEX CONCURRENTLY` and of per-leaf drops on a partitioned index (M5).

### Scope Verification

This RDR covers the vector index layout of `nexus.chunks` and its upgrade path.
Taxonomy centroids have their own index and are out of scope.

### Cross-Cutting Concerns

The managed service runs the same engine, so the layout change applies there
too. Its upgrade runs on managed hardware, not a laptop, and is measured
separately before a cloud deploy. The lock hold of job 2 with many tenant leaves
is a required measurement before job 2 is enabled there.

RDR-230 (event-keyed reindex and recall canary): the canary reads only `pci_`
indexes (linda's inventory: PciCanary and PciPlanCheck read `pci_` only), so it
stays meaningful. A plan that names the leaf index is reported as not this
`pci_`. RDR-230's leaf-graph REINDEX size rule and its leftover rules become dead
for a dimension once job 2 has run, and this RDR's implementation retires them.
Raising the per-leaf cap means more partial indexes per leaf, whose planning and
insert cost is not measured, so the cap stays high rather than absent until that
cost is measured.

### Proportionality

The change touches search routing, the builder's cap and a new job; it reuses
the existing fan-out, builder and status reporting, and moves no rows.

## References

- T2 `nexus/rdr247-measurement-M2`, `-M3`, `-M3-depth`, `-M3-drop`, `-M4`, `-M5`
- T2 `nexus/rdr247-leaf-graph-consumers` (linda's consumer inventory)
- T2 `nexus/rdr247-c-prime-design-linda-draft` (Technical Design parts 1 to 3)
- T2 `nexus/vector-index-single-layout-input-linda-2026-11-10`
- T2 `nexus/f3yqd-pci-restart-rebuild-measurement-2026-11-10`
- RDR-225 Alternatives 3 and 4; RDR-227 Alternatives 3 and 4, Phase 0.

## Revision History

- 2026-11-10: draft created (wren, linda), from Sam's emergency call on
  nexus-f3yqd.8. Options, gaps and the measurement plan; findings pending.
- 2026-11-11: M3 folded in (marginalia's three legs). The real-embedding leg
  supersedes the synthetic legs' provisional red; the overlap case and the 1M
  confirmation stay open.
- 2026-11-11: M3 overlap leg folded in; plain D fails a small interleaved
  collection, so the lean is D′ (leaf graph for large-share collections, own
  index or exact for small ones). The boundary waits for the 1M run.
- 2026-11-11: share sweep folded in; the boundary lies between 10% and 25% of
  the leaf for interleaved rows. The depth check waits on Sam.
- 2026-11-11: the lean history, moved here from Approach. Linda and wren first
  leaned to the one-statement drop (Option C, conditional on A1 and A2). M5 then
  showed that the leaf graph is a partition of a parent index, so the drop takes
  the parent-wide lock, and the lean moved to Option D. The overlap and share legs
  moved it to D′.
- 2026-11-12: depth check folded in (1M interpolated rows). The shared graph
  loses recall at scale at every share, so D′ is refuted; the lean is reopened
  between the per-leaf variant and A, pending multi-collection latency under the
  fan-out at 1M.
- 2026-11-12: the fan-out's cost at 1M measured as a bench model of the engine's
  fan-out (1.2 to 2.6 times the leaf graph, equal or better recall). The lean
  went to a sequenced drop. The lean text of that day used C′ for two designs:
  the per-leaf variant, now in Alternatives, and the sequenced parent drop, which
  this revision names Option C, sequenced.
- 2026-11-12: Gate round 1 — BLOCKED (2 Critical, 8 Significant, 0 ship-blocker(s)); commit `d6740de4d`; critique `nexus_rdr/247-gate-critique-2026-11-12-r1`.
- 2026-11-12: Deferred (Sam, by dialog): no longer an emergency, since the killed-build bug behind it was fixed in 0.1.186 and 0.1.187. Keep both indexes until RDR-229 is ready on its own merits; then RDR-247 rides with it as one visible job: stamp collection ids before each build, build each per-collection index on the id, then the bounded drop. Cost basis: T2 `nexus/rdr247-cost-comparison-2026-11-12`.
