---
title: "Bound and Freshen the Tombstone-Filter Vector Read Path: a Statement Ceiling and Autovacuum Tuning"
id: RDR-213
type: Bug Fix
status: draft
priority: high
author: Daniel
reviewed-by: self
created: 2026-09-16
accepted_date:
related_issues: []
related_rdrs: [RDR-156, RDR-191, RDR-149]
---

# RDR-213: Bound and Freshen the Tombstone-Filter Vector Read Path: a Statement Ceiling and Autovacuum Tuning

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Scope note.** This is a design-only RDR (no implementation in its PR).
Implementation lands in a separate follow-up PR, per the contributor
convention that a fork's design-review PR ships the RDR alone.

**Provenance.** A live debugging session (2026-09-16/17). `nx taxonomy discover
--collection docs__1-8__bge-base-en-v15-768__v1` hung indefinitely after
printing `embeddings 5,100/6,648`. Root-cause tracing found a single Postgres
backend pinned at 100% CPU for 25+ minutes on ONE `/v1/vectors/get-embeddings`
batch; the service log carried the mirror image — `event=vector_handler_error
op=/get-embeddings … java.io.IOException: Broken pipe`. The service never
crashed. An `ANALYZE` of the manifest table collapsed the same 300-id batch from
*unbounded* to **7 ms**. This RDR designs the durable fix, not the one-off
`ANALYZE`. Every measurement below was taken live against the reporting user's
local engine during that session. The design was revised after a round-1 gate
(see Revision History): Part A's scope and Part B's mechanism both changed once
the remedy was checked against the actual call graph.

## Context

### Background

Nexus stores permanent knowledge as vector chunks (T3) served by a Java engine
(`nexus-service`) over Postgres + pgvector. `nx taxonomy discover` reads back
every stored embedding for a collection (via `/v1/vectors/get-embeddings`, paged
300 ids at a time) and clusters them into topics.

The **tombstone filter** (`liveChunksCondition`,
`PgVectorRepository.java:3624`) is a doubly-nested `NOT EXISTS` anti-join added
by RDR-156 Decision 6 and RDR-191 GATE-2. It excludes a chunk from a read when
its owning catalog document is soft-deleted (`catalog_documents.deleted_at IS
NOT NULL`) and no live document still references the chunk. Five read methods
call this helper, each inside an **unbounded** `tenantScope.withTenant`
transaction: `get()` (`:1452`), `getEmbeddings()` (`:1549`), `getWhere()`
(`:1643`), `getAllMetadata()` (`:1731`), and `list()` (`:2481`). So the
vulnerable surface is not `discover`-only.

Two nearby methods are **not** in that set and were wrongly grouped with it in
the first draft (round-1 gate):

- `search()` / `hybridSearch()` apply the tombstone predicate through a
  different, *inlined* SQL dead-set form (not the `liveChunksCondition` helper),
  and they **already carry a statement ceiling** — `PgSession
  .setSearchStatementTimeout` (see Technical Environment). They are outside
  Part A's scope.
- `count()` (`:2728`) uses a plain `ctx.fetchCount(ch.table(),
  ch.collection().eq(collection))` with **no** tombstone filter at all. It is
  not a tombstone-filtered reader and is outside Part A's scope.

### Technical Environment

- **Engine / substrate**: `nexus-service` (Java) over bundled Postgres 17 +
  pgvector in local mode. Chunk rows live in `nexus.chunks`; the
  document→chunk manifest in `nexus.catalog_document_chunks`; document metadata
  (including `deleted_at`) in `nexus.catalog_documents`. The manifest carries a
  `(tenant_id, chash)` btree index (`idx_catalog_chunks_chash`) and a
  `(tenant_id, collection)` index (`idx_catalog_chunks_collection`).
- **Row-Level Security**: `nexus.chunks` and the catalog tables enforce
  `tenant_id = current_setting('nexus.tenant', true)`; every read runs inside a
  `tenantScope.withTenant(tenant, ctx -> …)` transaction that sets that GUC.
- **Two existing statement ceilings, neither on the five helper readers.**
  (a) `SweepBounds.STATEMENT_TIMEOUT = 30s` bounds the TTL-sweep transactions,
  applied transaction-locally (`is_local=true`) via
  `SweepBounds.applyStatementTimeout(tx, timeout)`. (b)
  `PgSession.setSearchStatementTimeout(ctx)` bounds `search()`/`hybridSearch()`
  — default `DEFAULT_SEARCH_STATEMENT_TIMEOUT_MS = 30_000`, env-tunable up to
  `600_000` via `NX_SEARCH_STATEMENT_TIMEOUT_MS`, and pinned by
  `HnswServingGucParityTest`. `statement_timeout` is in `PgSession.ALLOWED_GUCS`.
  The five `liveChunksCondition` helper readers use neither.
- **Statistics maintenance is autovacuum only.** The engine runs `VACUUM`
  (post-purge) but never `ANALYZE`; planner statistics are left to Postgres
  autovacuum. Autoanalyze fires when `n_mod_since_analyze > analyze_threshold +
  analyze_scale_factor × reltuples`; at the defaults (threshold 50, scale factor
  0.1) a 137k-row manifest needs ~13,700 modifications before it re-analyzes.

## Problem Statement

Two independent defects compound into an unbounded, CPU-pegging hang on an
ordinary read of a freshly-indexed collection.

### Enumerated gaps to close

#### Gap 1: The tombstone-filtered helper reads have no statement ceiling

The five `liveChunksCondition` readers (`get`, `getEmbeddings`, `getWhere`,
`getAllMetadata`, `list`) run inside a `withTenant` transaction with no
`statement_timeout`. A mis-planned read therefore runs to completion however long
that takes, and Postgres does **not** cancel the backend when the HTTP client
disconnects: in the incident the client hit its 120 s read timeout, closed the
socket (server logged `Broken pipe`), and the backend kept executing the same
`SELECT` for **25+ minutes** at 100% CPU. A retry stacks a second orphan on the
first. `search()`/`hybridSearch()` already bound this via
`setSearchStatementTimeout`; the five helper readers do not.

#### Gap 2: Autoanalyze is too slow to catch a freshly-indexed collection

After a bulk index or `--force` re-embed writes a new large collection, its
manifest rows land in `catalog_document_chunks`, but at the default autoanalyze
trigger (~13,700 modifications on this table) a single ~6,648-row collection does
**not** cross the threshold, so autoanalyze does not run and the column stays
unanalyzed. In the incident, `pg_stats` held **zero rows** for
`catalog_document_chunks.collection`, so the planner estimated **1 row** for
`collection = 'docs__1-8…'` when the true count was ~6,648. On that one-row
estimate it drove the chash correlation as a `Join Filter` instead of an index
probe and nested it: for each candidate chunk it scanned *every* manifest row of
the collection (m) × *every* manifest row again (m2) — ~44M iterations per id,
billions per 300-id batch.

Gap 1 bounds the blast radius; Gap 2 removes the cause. Both are needed. Gap 1
alone turns the 25-minute hang into a fast clean error but leaves
`discover`/`search` unable to *complete* until stats catch up; Gap 2 alone leaves
the read unbounded whenever statistics go cold for any other reason.

## Research Findings

### Investigation

The command hung after `embeddings 5,100/6,648`. `pg_stat_activity` showed one
backend `active`, **no wait event** (pure CPU), query age climbing past 15
minutes on a `SELECT chash, embedding_768 FROM chunks WHERE collection=$1 AND
chash IN (…)`. `track_activity_query_size` (1024 bytes) truncated the logged
text; reading `PgVectorRepository.getEmbeddings` → `liveChunksCondition` revealed
the full shape: a doubly-nested `NOT EXISTS` tombstone anti-join. Reproducing the
simple `IN` alone was ~1 ms; reproducing it **with** the anti-join reproduced the
hang. `EXPLAIN` named the cause. A round-1 gate then audited the remedy against
the full call graph and corrected its scope (below, and Revision History).

### Key Discoveries (all measured live except where noted)

1. **The plan is correct in isolation, catastrophic under cold stats.** Same
   `docs__1-8` query, same connection:

   | Variant | Result |
   |---|---|
   | Simple `chash IN (…)`, no tombstone filter (custom plan) | 1.0 ms (`chunks_pk`) |
   | Same, forced generic plan | 0.33 ms (`chunks_pk`) |
   | **Full tombstone filter, 5 ids, before ANALYZE** | **>45 s (statement timeout hit)** |
   | **Full tombstone filter, 300 ids, after `ANALYZE`** | **7.3 ms** (`idx_catalog_chunks_chash` probe) |

2. **Root of the misestimate: missing column statistics from a too-high
   autoanalyze trigger.** `pg_stats` had **zero** rows for
   `catalog_document_chunks.collection` (`has_collection_stats = 0`) before the
   fix; the table was otherwise healthy (136,975 live rows, 0.7% dead, 951k index
   scans vs 534 seq scans). A ~6,648-row collection is well under the default
   ~13,700-modification autoanalyze trigger, so the fresh collection's stats were
   never gathered. An explicit `ANALYZE` created them and flipped the plan from a
   collection-scan `Join Filter` to a per-candidate `idx_catalog_chunks_chash`
   probe.
3. **The catastrophe needs the single-large-collection shape.**
   `liveChunksCondition`'s design comment records a measured 0.9 ms for 200 ids
   on a 76k-chunk fixture — but that fixture spread manifest rows across many
   collections. `docs__1-8` holds all ~6,648 chunks under one `collection` value;
   that is the shape that starves the cold-stats plan, and no gate exercises it.
4. **The five vulnerable readers, and the two already-covered ones.** `get`,
   `getEmbeddings`, `getWhere`, `getAllMetadata`, `list` call
   `liveChunksCondition` inside an unbounded transaction. `search`/`hybridSearch`
   use an inlined dead-set predicate and already carry
   `setSearchStatementTimeout` (30 s default, env-tunable, pinned by
   `HnswServingGucParityTest`). `count()` has no tombstone filter. (This
   enumeration was corrected at the round-1 gate; the first draft mis-scoped it.)
5. **Autovacuum, not an application `ANALYZE`, is the right lever.** The catalog
   completion paths (`completeIndexRun` and the hot-path `stampCompleteIfVerified`
   inside `writeManifestMany`) are both **per-document**; there is no shared
   once-per-run hook. Hooking `ANALYZE` there would run it N times per bulk index
   and split across two paths. Tuning the table's autoanalyze trigger instead is
   uniform across every write path, requires no application code on the hot path,
   and runs `ANALYZE` under autovacuum's own privileges (so no service-role
   `MAINTAIN` dependency) and its own lock management (so no request-path lock
   contention). (This finding supersedes the first draft's completion-hook
   design; see Revision History.)

## Decision

Two engine-side (`service/`) changes, PR'd to `develop` and gated by the engine
suite. Neither rewrites `liveChunksCondition` itself.

### Part A — statement ceiling on the five unbounded helper readers (closes Gap 1)

Apply a transaction-local `statement_timeout` at the head of the `withTenant`
transactions in `get()`, `getEmbeddings()`, `getWhere()`, `getAllMetadata()`,
and `list()`, reusing `SweepBounds.applyStatementTimeout(ctx,
READ_STATEMENT_TIMEOUT)`.

- **Value: 60 s** (`READ_STATEMENT_TIMEOUT`), a new constant beside
  `SweepBounds.STATEMENT_TIMEOUT`. Above any legitimate read (healthy is
  single-digit ms; a large honest read is seconds) and below the Python client's
  120 s `_post` default, so the **server** aborts first with `canceling statement
  due to statement timeout` (a 5xx the client reports) rather than the client
  timing out and orphaning the backend.
- **`search()`/`hybridSearch()` are excluded** — they already have
  `setSearchStatementTimeout`; layering a second `set_config` on the same
  transaction would either silently loosen the pinned 30 s to 60 s (last
  `SET LOCAL` wins) or be inert. Their existing bound is left untouched. `count()`
  is excluded (no tombstone filter).

### Part B — tune autoanalyze on the manifest table (closes Gap 2)

A Liquibase changeset (DDL-only, per the substrate boundary — no data backfill)
lowers the autoanalyze trigger on `catalog_document_chunks` so a fresh
single-collection bulk write promptly re-analyzes the table and gathers its
`collection` stats:

```sql
ALTER TABLE nexus.catalog_document_chunks
  SET (autovacuum_analyze_scale_factor = 0.02,
       autovacuum_analyze_threshold    = 500);
```

At current scale this drops the trigger from ~13,700 to ~3,200 modifications, so
a single ~6,648-row collection crosses it and autoanalyze runs at the next
autovacuum cycle. `nexus.chunks` may receive the same treatment for symmetry,
but `catalog_document_chunks` is the table with the demonstrated cold-stats
defect and the essential target. Exact values are a tuning knob to confirm at
implementation.

The changeset itself is pure storage-parameter DDL: `ALTER TABLE … SET
(autovacuum_*)` takes only a `ShareUpdateExclusiveLock`, which does not block
concurrent reads or writes, so applying it to this hot table (951k index scans
in the incident snapshot) is non-disruptive — confirm the lock level at
implementation.

- **Why not an explicit `ANALYZE`:** see Research Finding 5 and Alternatives —
  the completion paths are per-document and split, so an application `ANALYZE`
  there is N-per-run, hot/fallback-split, and adds request-path lock contention.
  Autovacuum tuning avoids all three.
- **Residual window:** `n_mod_since_analyze` is table-wide, not per-collection.
  For a single write at or above the tuned trigger (~3,200 rows — the incident's
  ~6,648-row shape qualifies), the counter crosses immediately and the cold
  window is bounded by the next autovacuum cycle (naptime, default ~60 s). For a
  smaller or incremental write the counter crosses only once accumulated
  table-wide churn reaches the trigger, so its cold window is bounded by that
  churn, not by naptime. Either way Part A caps the *per-request cost* to a fast
  (~60 s) clean error plus a retry that succeeds once autoanalyze has run — not a
  25-minute hang. This window is the accepted cost of the declarative approach.

## Alternatives considered

- **Explicit synchronous `ANALYZE` at index-run completion** (the first draft's
  Part B). *Pros:* freshens stats immediately after the write. *Cons:* the
  completion paths (`completeIndexRun`, and the hot-path `stampCompleteIfVerified`
  in `writeManifestMany`) are both per-document with no shared once-per-run hook,
  so it runs N times per bulk index, must be wired into two paths, adds
  request-path latency, depends on the service role holding `MAINTAIN`, and
  serializes concurrent runs on `ANALYZE`'s `ShareUpdateExclusiveLock`.
  *Rejected* at the round-1 gate in favour of autovacuum tuning, which has none
  of these properties. A coalesced/debounced async variant was considered and
  judged more machinery than the tuning approach warrants.
- **Rewrite `liveChunksCondition` to be plan-stable regardless of statistics.**
  *Pros:* removes the stats dependency entirely. *Cons:* touches a
  carefully-tuned, documented hot query on the `search` path; highest regression
  risk. *Rejected:* Part A already caps the downside of any future misestimate.
  Left as a possible future RDR.
- **Client-side only** (make `get_embeddings` resumable, retry `TimeoutError`, or
  lower its timeout). *Cons:* the client cannot influence engine-side statistics,
  and retrying/resuming a 25-minute query just stacks more orphaned backends.
  *Rejected:* Part A already makes the client fail fast and cleanly.

## Trade-offs

### Consequences

- A genuinely slow-but-valid `liveChunksCondition` read is now capped at 60 s. At
  current scale healthy reads are single-digit ms and the largest honest reads
  are seconds, so 60 s is generous headroom; a read that exceeds it is
  overwhelmingly a mis-plan, which is what should abort.
- `catalog_document_chunks` re-analyzes more often (after ~3,200 vs ~13,700
  modifications). `ANALYZE` on a table this size is <1 s and runs in the
  background under autovacuum; the extra frequency is negligible cost for a table
  whose plan quality is this sensitive.
- The plan remains stats-dependent; Part B makes the dependency reliably
  satisfied soon after a write rather than removing it.

### Risks

- **Residual cold window.** For a write at/above the tuned trigger the window is
  ~autovacuum naptime; for a smaller write it is however long table-wide churn
  takes to cross the trigger (see Part B's Residual-window note). Mitigation:
  Part A bounds the per-request cost to a fast-fail + retry, not a hang,
  regardless of collection size.
- **60 s too low for a future very large legitimate read.** Mitigation: the
  constant is a single named value, trivially raised; and Part B removes the
  cold-stats cliff that produces the pathological plan.
- **Deferring the query rewrite leaves residual fragility** (fast plan ↔ bad plan
  on a misestimate). Mitigation: Part A caps the cost of any future misestimate
  to ~60 s; the rewrite is a scoped follow-up, not a silent gap.

## Implementation Plan

Design-only RDR; the phases below scope the follow-up implementation PR.

1. **Phase 1 — Part A (statement ceiling).** Add `READ_STATEMENT_TIMEOUT` (60 s)
   beside `SweepBounds.STATEMENT_TIMEOUT`; apply it via `applyStatementTimeout`
   at the head of the `withTenant` transactions in `get`, `getEmbeddings`,
   `getWhere`, `getAllMetadata`, `list`. Test (extending
   `PgVectorTombstoneFilterTest`): a deliberately slow read on each of the five
   aborts with the timeout; and an explicit assertion that `search`/`hybridSearch`
   still carry exactly their `setSearchStatementTimeout` value (no double-set
   regression), guarding the `HnswServingGucParityTest` contract.
2. **Phase 2 — Part B (autoanalyze tuning).** A Liquibase changeset setting the
   two storage parameters on `catalog_document_chunks` (and, if confirmed,
   `chunks`). Test: after a fresh single-large-collection bulk write, the manifest
   is autoanalyzed within an autovacuum cycle and the tombstone-filtered read runs
   in the fast regime (plan uses `idx_catalog_chunks_chash`); confirm the tuned
   trigger value against the table's row count.
3. **Phase 3 — engine gate.** Green `scripts/build-gate-jar.sh` +
   `scripts/mvnw-leased.sh test` + the engine-substrate suite before PR.
4. **Out of scope (separate lifecycle).** Cutting an `engine-service-v*` tag and
   bumping `REQUIRED_ENGINE_VERSION` to ship the fix to local installs is the
   engine-release process (AGENTS.md § Engine-service release), not this RDR or
   its implementation PR.

## Test Plan

- **Part A** — engine test on `PgVectorTombstoneFilterTest`: inject a tiny
  `statement_timeout`, assert a slow read aborts on each of the five helper
  paths, and assert `search`/`hybridSearch` retain their existing bound
  unchanged.
- **Part B** — integration test: a fresh single-large-collection bulk write
  triggers autoanalyze on `catalog_document_chunks` (stats present /
  `last_analyze` advances) and the subsequent tombstone-filtered read runs in the
  fast regime.

## Finalization Gate

- **Contradiction check.** Part A (bound the read) and Part B (freshen the stats)
  are complementary; the doc states why each alone is insufficient and why the
  residual window is bounded by Part A.
- **Assumptions verified.** The load-bearing claims are live-measured on the
  incident substrate (the timing table; `has_collection_stats = 0`) or read from
  the working tree (the five reader call sites; `setSearchStatementTimeout`;
  `count()`'s body; the per-document completion paths; `liveChunksCondition` at
  `:3624`). The round-1 gate corrected the claims that were not.
- **Scope.** Two named engine changes plus their tests; the query rewrite is
  explicitly deferred. `search`/`hybridSearch` and `count()` are explicitly
  excluded with reasons. No client change.
- **Proportionality.** Reuses an existing timeout primitive (Part A) and Postgres's
  own autovacuum (Part B); adds no new subsystem, endpoint, or request-path work.
  The fix is smaller than the failure it prevents (a 25-minute CPU-pegging orphan).

## Residuals / accepted gaps

- The tombstone query remains stats-fragile by construction. Part A caps the cost
  of a future misestimate to ~60 s; a plan-stable rewrite is deferred, not
  scheduled.
- The ~autovacuum-naptime residual window (Part B) is accepted: bounded by Part A
  to a fast-fail + retry.
- No pre-production rehearsal exercises the single-large-collection shape at
  scale; Part B's test adds first coverage but the general RDR-191 at-scale
  rehearsal gap is inherited, not closed here.

## Delivery

Engine-service change: **RDR only in this PR** (design of record); the
implementation lands in a separate follow-up PR, gated by
`scripts/build-gate-jar.sh` + `scripts/mvnw-leased.sh test` + the
engine-substrate suite. It reaches local installs only after a subsequent
`engine-service-v*` tag and a `REQUIRED_ENGINE_VERSION` bump (the separate engine
lifecycle, AGENTS.md § Engine-service release) — out of scope for this RDR.

## Revision History

- 2026-09-16 — Draft created (design of record for the tombstone-filter
  cold-stats blowup found while debugging a hung `nx taxonomy discover`).
- 2026-09-17 — Gate round 1: **BLOCKED** (4 critical, 4 significant, 4
  ship-blockers). The diagnosis held; the remedy's scoping against the real
  call graph did not. Findings: `nexus_rdr/213-gate-critique-2026-09-17`.
- 2026-09-17 — Revised for round 2: Part A rescoped to the five `liveChunksCondition`
  helper readers (excluding the already-bounded `search`/`hybridSearch` and the
  filter-free `count()`); Part B changed from an explicit completion-time `ANALYZE`
  to autoanalyze tuning on `catalog_document_chunks`; citations and the
  reader enumeration corrected.
- 2026-09-17 — Gate round 2: **PASSED** (0 critical, 2 significant, 0
  ship-blockers). All eight round-1 findings verified closed against the working
  tree. The two non-blocking significants (residual-window scoping; migration
  lock level) were folded in as polish. Critique:
  `nexus_rdr/213-gate-critique-round2-2026-09-17`. Status remains **draft** (the
  maintainer accepts).
