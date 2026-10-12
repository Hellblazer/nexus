# RDR-227 Post-Mortem: Per-Collection HNSW Indexes Above the Router Threshold

**Closed** 2026-10-12 · **Accepted** 2026-10-09 · **Epic** nexus-43ulx (35 children: 34 closed, nexus-43ulx.26 deferred) · **Shipped** engine-service-v0.1.157, v0.1.158 and v0.1.159 (2026-10-09 and 2026-10-10), conexus 7.77.0 pinned to v0.1.159

## What the RDR set out to do

Inside one (embedding model, tenant) partition every collection shares one HNSW
graph (HNSW is the approximate nearest-neighbour index pgvector builds). A
search of one collection therefore walks a graph that holds every other
collection's rows and filters as it goes. On `code__1-72` (27,893 rows) the
filter discarded about 80% of the walk and the search stopped before it reached
the true nearest row: recall against an exact scan was 0.925 at k = 40.

The interim fix was a router: collections under a row threshold T are scanned
exactly. That repaired recall and cost time, 1.1 to 2.0 s per large exact scan
under load on the cloud. The RDR proposed one partial HNSW index per collection
at or above a build threshold B, so a one-collection search walks a graph that
holds only that collection. It named three gaps to close: recall (Gap 1, target
0.99), warm latency (Gap 2), and the cold first search after a restart (Gap 3).

## Implementation status

Implemented, and live on the cloud since 2026-10-10. A builder in the engine
creates and retires the indexes on a 600 s pass under an advisory lock, the
router sends a collection to its index once that index is valid, `GET
/v1/status` reports the builder's state, and `nx doctor` has a row for it.
Gaps 1 and 2 are closed by measurement where an index exists. Gap 3 is not
closed (below).

## What the measurements say

- **Recall with the index.** On a production fork every code collection at or
  above B read 0.99 or better (T2 `nexus_rdr/227-research-12`). The row the
  original report missed is returned through the live index
  (`nexus_rdr/227-research-13`).
- **Latency.** The code request's slowest statement has a median of 325 ms,
  against about 800 ms before. A default search averaged 1.88 s against 2.33 s
  on the same client and the same eight queries. That comparison is 35
  searches from one sequential client with no concurrent load, and the two
  ranges overlap (`nexus_rdr/227-research-13`).
- **Insert cost.** Measured twice with different methods and not reconciled:
  through the engine's write path on the test host a single row cost 43% more
  and a 128-row batch took 1.8 to 1.9 times as long
  (`nexus_rdr/227-research-11`); statement time on the fork was within noise
  (`nexus_rdr/227-research-12`). No real indexing run was timed.

## Implementation vs plan

### As planned

- One partial index per collection at or above B, built one at a time by a
  single lock holder; a second engine reports standby.
- A missing or invalid index degrades to the shared walk, never to an error.
- The builder needs admin credentials and says so in the status object and in
  `nx doctor` when it has none.

### Diverged

1. **Gap 3, the cold first search, was left open.** The plan warmed each index
   with `pg_prewarm`. The local PostgreSQL bundle ships no `pg_prewarm`, and
   adding it meant a bundle change on every platform plus a new changeset. Sam
   deferred it on 2026-10-09 (T2 `nexus_rdr/227-prewarm-decision`, bead
   nexus-43ulx.26). The reopen trigger is a material first-search penalty after
   a restart. It has not been exercised: the only measurement followed an
   engine restart, with the database and its buffers still warm.
   *Drift class: deferred critical constraint.*

2. **The Phase 2b gate was applied in a narrower form than the RDR stated.**
   The RDR made lowering T conditional on three recall runs: one code
   collection on its own topic, prose, and a second tenant. The code run
   passed. Prose with its index read a minimum of 0.9833, under 0.99. The
   second-tenant run was 20 queries drawn from the collection's own stored
   embeddings, which its record calls weak evidence. The gate actually used was
   "every collection between B and T has a valid index and met 0.99"; prose sat
   below B and was set aside. Sam said proceed (bead nexus-43ulx.34). The RDR
   carried the original gate until the close-time fold.
   *Drift class: unvalidated assumption.*

3. **B and T shipped equal, at 20,000, with no margin.** The design assumed
   B below T, so a collection would have its index before the router stopped
   scanning it exactly. With the two equal, a collection at or above 20,000
   without a valid index is served by the shared walk at `ef_search = 1000`,
   measured at 0.975 in the worst of three collections. That happens between
   crossing the threshold and the next build pass, between boot and the first
   read of the index set, after a failed build, and permanently on an install
   whose builder cannot run. Setting `NX_SEARCH_EXACT_MAX_ROWS=30000` keeps such
   collections exact.
   *Drift class: under-specified architecture.*

## What stays open

- **Cold start** (nexus-43ulx.26, deferred): no warm-up exists and first touch
  after a database restart or failover is unmeasured.
- **Prose past the threshold** (nexus-qqoak): the prose exception holds only
  while no prose collection reaches 20,000 rows. `rdr__1-79` was at 14,300.
  Prose index recall at 20,000 rows or more was never measured, and nothing
  checks the row counts today.
- **Credential rotation**: a targeted admin rotation does not restart the
  engine, so the builder would fail authentication until the next restart. The
  deploy side added a mandatory engine recreate to its rotation procedure on
  2026-10-09. It is a procedure step that no script enforces, and no targeted
  rotation has happened since the builder went live. A skipped step is visible:
  the status object reads `auth_failed`, `nx doctor` fails, and the engine logs
  a warning on every pass.
- **An outside constraint**: the deploy side's recall gate goes red if the
  cloud's T is raised above 28,595.

## What the next person should take from this

**A gate rewritten in a bead is a gate nobody can audit.** Divergence 2 was a
defensible call made in the open, and it still read as a silent substitution
two days later, because the RDR kept the old gate and the new one lived in a
bead comment. When a gate changes, the RDR changes in the same hour.

**A threshold decided on a row count is a claim about the future.** "Prose is
below B" was true on the day and was written as if it were a property of prose.
An exception that depends on a count needs something that reads the count.

**"Gaps to close" has to say which ones closed.** Three gaps were listed and
two were closed. Until the close-time fold, three places in the RDR still
described the deferred mitigation as if it were in force.

## Critique record

Two close-time critiques, no Critical finding in either. Round 1 (T2
`nexus_rdr/227-close-critique-2026-10-12`) found five Significant issues, all
in the RDR's text and all folded in. Round 2
(`nexus_rdr/227-close-critique-2026-10-12-r2`) found one, the prose inference,
corrected the same day. The RDR was closed as Implemented on Sam's decision
with the round-2 verdict at "partial" (`nexus_rdr/227-close-override-2026-10-12`).
