---
title: "Catalog Store Behind the Existing T2 Daemon: Close the catalog.db Starvation by Routing Catalog Writes Through the Daemon That Already Serves It"
id: RDR-146
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-03
accepted_date: 2026-06-03
related_issues: [nexus-41otf]
related_rdrs: [RDR-120, RDR-128, RDR-129, RDR-140]
---

# RDR-146: Catalog Store Behind the Existing T2 Daemon — Close the catalog.db Starvation by Routing Catalog Writes Through the Daemon That Already Serves It

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

`catalog.db` (`~/.config/nexus/catalog/.catalog.db`) is the **one** persistent
shared-state store still *not* fronted by a daemon. Every other store reached the
single-writer / serialized-access discipline through the RDR-120 → 128 → 129 →
140 arc; the catalog was left on the old direct-`sqlite3`-handle model.

GitHub issue **#1046** is the symptom. An interactive `nx dt index` run was
**starved indefinitely** (~30 min, ~0 CPU, process in `S`/sleeping) by scheduled,
hook-spawned `nx index repo` jobs contending on the shared catalog writer. `lsof`
confirmed both the foreground `dt index` and the background `nx index repo` held
`.catalog.db` + `-wal` + `-shm` open read/write; the foreground job gained
**+0.08s CPU in 10s** while the background indexer was pegged at `R`. Killing the
background jobs flipped the foreground job to `R`/running instantly. Diagnosis:
**starvation, not slowness.**

The starvation decomposes into the gaps below.

#### Gap 1: The catalog writer is not serialized by an owner

`catalog.db` is the one persistent shared-state store with no daemon owning its
write handle. N processes (interactive `nx dt index`, hook-spawned `nx index
repo`, the MCP singleton) open their own read/write `sqlite3` handles and race
for the WAL writer lock with no coordinator. Every other store crossed this line
in RDR-120 → 128 → 129 → 140; the catalog did not.

#### Gap 2: `--on-locked=skip` guards the wrong lock

Repo indexers take a *per-repo* advisory lock (`~/.config/nexus/locks/<hash>.lock`)
and correctly skip when that is held, but they treat the shared `.catalog.db`
writer as uncontended. The guard never fires on the resource that actually
contends, so background indexers never yield the catalog writer to an interactive
caller.

#### Gap 3: No interactive-vs-batch fairness on the shared writer

A foreground human-driven write has no priority over background hook-spawned batch
reindexing. The background `nx index repo` reacquires the WAL writer lock per file
(RF-3) faster than the interactive waiter can win under `busy_timeout`; the
foreground job gained **+0.08s CPU in 10s** while the indexer was pegged. The
failure is silent (no error, no surfaced timeout), recurs on **every** hook fire,
and a killed background indexer respawns within ~1 min (reparented to `launchd`)
and re-starves.

#### Gap 4: The catalog graph traverse is unbounded (nexus-41otf)

The `traverse` `link_types` path hangs ~4 min where the `purpose` path returns
instantly — an unbounded Python BFS over the link graph (RF-5). Folded here
because the fix lives in the same catalog write/query surface and benefits
directly from the daemon's single-owner connection.

A fifth, lower-severity defect observed in the same incident — `nx dt index`
holding **20+ duplicate open fds to `pipeline.db`** (connection-per-unit-of-work,
RF-6) — is an orthogonal subsystem and is **split out** (bead `nexus-c341x`), not
a gap of this RDR.

## Why Not the Postgres Rewrite

A ground-up Java/Quarkus/Postgres storage-service rewrite (plan v2, T3 tumbler
1.11.145) was pressure-tested on 2026-06-03 and returned a **high-confidence
NO-GO** (substantive-critic; T2 `nexus/postgres-storage-service-plan-v2-NO-GO-2026-06-03`).
Summary of the rejected alternative:

- The "cloud multitenant service seed" justification is aspirational — no
  committed consumer, no timeline. Multitenancy-from-day-one is speculative scope
  on a solo local tool.
- #1046 — the **one** genuinely-unfixed structural class — is a **days-not-months
  in-arch fix** using the proven daemon pattern. Using it to motivate a months-long
  rewrite is an order-of-magnitude disproportionate.
- A Phase-2 pgvector benchmark miss would leave a permanently polyglot system
  (Java service + Chroma Python daemon) more complex than today's all-Python
  baseline.
- The rewrite re-enters the exact supervisor / single-writer / kill-9-recovery
  domain that four Python RDRs just stabilized, in a new language at zero test
  coverage.

This RDR is the recommended path from that decision: close #1046 **in-arch**.

## Approach

**No NEW daemon, but the existing daemon must be taught to serve the rich catalog
write API (RF-7 CORRECTED → RF-8).** The catalog *is* the 8th T2 domain store, but
the daemon serves `T2Database.catalog` = the low-level **`CatalogStore`**
(execute/commit/transaction/search/…), **not** the rich `nexus.catalog.catalog.Catalog`
write API (`register`/`update`/`link`/`register_owner`) that the 49 consumer sites
actually use. RF-7's "register/update/create_link already served" was wrong (see
RF-8). The fix therefore has two parts: (a) host a rich `Catalog` *inside* the
existing T2 daemon and expose a curated 16-op JSON-serializable write subset over
RPC; (b) cut the 49 consumer write sites over to that RPC surface. It reuses the
existing T2 daemon (no second daemon, no second election — PC-1 still dissolves),
but it is **not** the mechanical "point sites at an already-served op" that RF-7
implied. Reads stay direct WAL (RF-1/RF-2/RF-8 Q5). Numbered items are the
close-time cross-walk surface.

1. **Host the rich `Catalog` in the T2 daemon + expose a write-only op whitelist [MEDIUM].**
   Construct one `nexus.catalog.catalog.Catalog` inside the
   daemon (owns the single `.catalog.db` write handle *and* the JSONL append path)
   and register a **whitelist** of write ops on the dispatch table — NOT the
   default "all public methods minus a denylist." The default auto-exposes
   dataclass-returning reads (`links_from`/`links_to`/`resolve` →
   `CatalogLink`/`CatalogEntry`, which do not round-trip JSON) and is unsafe here
   (re-gate audit finding). The whitelist is exactly: `register_owner`,
   `ensure_owner_for_repo`, `register`, `update`, `link`, `link_if_absent`,
   `unlink`, `delete_document`, `register_collection`, `delete_collection_projection`,
   `supersede_collection`, `set_owner_head_hash`, `write_manifest`,
   `append_manifest_chunks`, `atomic_manifest_replace`, `resync_chunk_count_cache`
   (RF-8 Q6). Each needs only a `Tumbler↔str` shim (~3 lines); no rich objects
   cross the boundary. Write dispatch is **single-threaded / serially dispatched**
   in the daemon (the per-instance `_owner_register_lock` at `catalog.py:533`
   stays correct; if a future phase adds threaded write dispatch the lock becomes
   load-bearing — document the contract in the bead, re-gate Significant). No new
   heavy deps; daemon construction is mtime-gated (RF-8 Q4).
2. **Atomic cutover via a typed reader/writer split [MEDIUM].**
   The core, NOT incremental. Route the 49 `Catalog(...)` write sites (RF-4) through the
   daemon. **The cutover must be ATOMIC** (RF-8 Q3): the rich `Catalog` bumps the
   `owners.jsonl` `next_seq` high-water mark, so any in-process writer coexisting
   with the daemon double-allocates → duplicate tumblers + flock contention. The
   read-vs-write distinction is the enforcement crux (re-gate Critical): the lint
   `CATALOG_BANNED_CONSTRUCTORS=("Catalog",)` cannot tell a read-only instance
   from a write-capable one. Resolve with **two typed factories**:
   - `make_catalog_writer()` → a daemon-routed write proxy exposing *only* the 16
     whitelisted ops (no read methods, so a consumer cannot accidentally read-
     after-write through a stale local handle).
   - `make_catalog_reader()` → read-only access. Reads stay local (RF-8 Q5: WAL
     read-committed sees daemon-committed writes). **Confirm at implementation
     whether `_ensure_consistent` (`catalog.py:622-624`) issues a SQLite rebuild
     (a write); if so the reader MUST avoid triggering it** (read via the
     `CatalogStore` subset, or a reader that skips `_ensure_consistent`) so a
     read-local construction never re-acquires the WAL writer lock against a
     daemon that is actively writing (re-gate Significant).

   Bare `Catalog(...)` in consumer code is then banned by the lint and every site
   goes through one factory or the other — making the read/write split *tooling-
   enforced*, not convention. Flip the `CATALOG_BANNED_CONSTRUCTORS` lint (baseline
   49 from P0.1) to enforce once the floor is reached; reuse the RDR-128
   epsilon-allow protocol for the documented-irreducible substrate sites (PC-3).

**Implementation outcome — Phase 1 (beads `.20` host, `.5` parity, `.21` atomic
cutover; shipped 2026-06-03/04, full suite green, lint floor 0 enforced).** The
locked design above shipped with four recorded deviations, each forced by a
discovery during implementation (not silent scope drift; all cross-walked at the
phase gate):

- **Whitelist 16 → 22 ops (item 1).** An AST inventory of all 49 cutover sites at
  P1.2 start found six write ops outside the original 16, on the `nx catalog`
  admin surface + `collection_rename`: `rename_collection`, `bulk_unlink`,
  `update_documents_collection_batch`, `sync`, `pull`, `compact`. Per the
  "expand + route all, no surviving direct writer" decision, all six were added to
  the whitelist so the daemon (the single writer) runs them. Tumbler coercion is
  **per-op** (`TUMBLER_PARAMS_BY_OP`) because `bulk_unlink`'s `from_t`/`to_t` are
  plain-string filters that must NOT be `Tumbler.parse`d.
- **Write dispatch serialized by an `asyncio.Lock` (item 1).** `_dispatch` holds a
  catalog-write lock across the threaded invocation, because the dispatch thread
  pool would otherwise let the hosted Catalog's multi-step JSONL+SQLite mutations
  interleave (the directory flock does not serialise sibling threads). The
  `_owner_register_lock` contract is documented but is not, by itself, sufficient.
- **`read_only` Catalog mode (item 2).** `_ensure_consistent` (and
  `_emit_backfilled_collection_events`) DO write at construction — confirmed. The
  reader factory constructs `Catalog(..., read_only=True)`, which opens `mode=ro`
  and skips both construction-time writes, so a read-local handle never re-acquires
  the WAL writer lock against the daemon.
- **Strict reader/writer split + `make_catalog_admin` escape hatch (item 2).** The
  writer stays write-only and the reader read-only; mixed-use helpers
  (`generate_*_links`, `auto_link`, `_CatalogBackedRegistry`, the `catalog.py`
  backfills, etc.) take a `writer=` parameter (defaulting to the read `cat` for
  single-object callers). Two deep-maintenance commands — `dedupe-owners` and
  `undelete` — operate through low-level catalog internals (raw `_db` transactions,
  `_append_jsonl`, the event log) that are not expressible as the 22 RPC ops; they
  use `make_catalog_admin()`, a full local rich Catalog gated by a live-daemon
  probe that refuses to open a second writer (run with the daemon quiesced).

  A stacked code-review + substantive-critic pass caught two Criticals the green
  suite did not prove: `generate_citation_links` was a missed mixed read+write
  helper, and `catalog/store_hook.py` was a direct `.catalog.db` writer **invisible
  to the lint** (it lives under the allowlisted `catalog/` prefix) firing on every
  `store_put`. Both fixed; the lint's `catalog/`-allowlist blind spot is logged as
  a follow-up (tighten the lint to catch write-method calls in consumer-hot-path
  files even inside `catalog/`, or relocate `store_hook`).
3. **Interactive-vs-batch fairness [SMALL-MEDIUM].** A foreground `nx dt index` /
   `nx dt capture` / catalog register/link must not be starved by background
   `nx index repo`. With writes serialized through the single T2 daemon (item 1),
   this is a **queue-ordering concern inside the existing daemon**, not a new
   subsystem: a priority bit on the write RPC — `interactive|batch` — plus
   background indexers skipping when an interactive catalog write is pending
   (item 4). Specify the cross-client signaling protocol at planning (PC-2);
   mirror RDR-129 B2 bounded-retry in the serving dispatch.
4. **Fix the `--on-locked=skip` target [SMALL].** The skip guard must guard the
   real contention point — the catalog **writer** availability — not the per-repo
   advisory lock. One-line change at the `commands/index.py` + `indexer.py` probe
   sites.
5. **Bounded catalog graph traversal (nexus-41otf) [SMALL — FOLD].** Replace the
   Python BFS in `_LinkOps.graph()` (`catalog/catalog_links.py:871`, RF-5) with a
   single `WITH RECURSIVE` query (cycle detection, depth cap, direction /
   link-type filters as params; SQLite ≥3.35). ~50 LOC in one function, does not
   touch the daemon boundary. Folded into this RDR.
6. **pipeline.db fd-leak [SPLIT].** The 20+-duplicate-fd connection-per-unit
   pattern (`pipeline_buffer.py:84-106`, `pipeline_stages.py:617`, RF-6) is an
   orthogonal subsystem (resource lifecycle, not lock contention). Filed as a
   separate bead (see References); listed here only so it is not lost.

## Research Findings

**RF-1 (RESOLVED): the daemon surface is WRITE-ONLY.** Two load-bearing facts,
both verified against the codebase (2026-06-03 deep-analyzer pass):

1. **catalog.db is in WAL mode.** `PRAGMA journal_mode=WAL` at
   `src/nexus/db/t2/catalog.py:347` (and the schema literal at `:88`);
   `busy_timeout` set at `:346`. Under WAL, N readers and one writer run
   concurrently with no blocking. Read starvation is structurally impossible.
2. **The #1046 contention was writer-vs-writer.** Both contenders were *writers*:
   - Foreground victim `nx dt index` writes via `_register_or_lookup_doc_id →
     Catalog.register()`, then `_stamp_dt_uri_on_entry` (`commands/dt.py:123`,
     `cat._writes.update()`), then optional `create_link()`. It was blocked
     holding/awaiting the WAL **writer** lock, not as a reader.
   - Background starver `nx index repo` (`_catalog_hook`, `indexer.py:649`) is a
     sustained writer: a per-file loop (`indexer.py:717`) of
     `register()`/`update()` each ending in `self._db.commit()`, then link
     generation, then housekeeping.

   WAL's reader concession is therefore irrelevant to this bug class; serializing
   **writes** through the daemon closes it. Direct WAL reads (MCP
   `get_catalog()`, `search_engine` prefilter at `search_engine.py:334`,
   read-only resolves) stay lock-free and do not route through the daemon.

**RF-2 (precedent confirms write-only): T2 is already a write-only gateway.**
`t2_index_write()` (`src/nexus/mcp_infra.py:294`) routes writes through
`T2Client` with a direct-`T2Database` epsilon-allow fallback; `t2_ctx()`
(`mcp_infra.py:199-211`) returns a direct `T2Database` for **all reads**
(`memory_search`, `memory_get`, `plans.search`, …). The catalog daemon mirrors
this split exactly: reads direct, writes through `CatalogClient`.

**RF-3 (root cause confirmed): per-record commits, no coarse transaction.** The
`_catalog_hook` per-file loop does N individual WAL-writer-lock acquisitions + N
`commit()`s (`catalog.py:1378` event-sourced path, `:1396` legacy path); no
`transaction()`/`bulk_load_documents()` wraps it (`bulk_load_documents` at
`catalog.py:1014` is rebuild-only). Each commit briefly yields then re-acquires —
never long enough for the interactive waiter to win under `busy_timeout`. So the
fix is fairness/priority on the write path, not transaction restructuring.

**RF-4 (boundary surface): 56 `Catalog(...)` constructor sites across 28 files.**
All are constructor calls, not raw `sqlite3.connect` — mechanical to lint. Heaviest:
`indexer.py` (6), `commands/dt.py` (5), `doc_indexer.py` (3),
`pipeline_stages.py` (2), plus the MCP singleton `mcp_infra.py:495` and the
post-store hook `catalog/store_hook.py:51`. Writes route through the client; read
sites keep epsilon-allow annotations.

**RF-5 (nexus-41otf, FOLD): the traverse hang is a Python BFS, fixable in ~50 LOC
of SQL.** `_LinkOps.graph()` at `catalog/catalog_links.py:871` is a Python
while-queue BFS issuing `links_from()` + `links_to()` (2 SQL queries) per node —
O(N) round-trips; the `link_types=None` path defaults to a broad
"all-except-implements-heuristic" filter (`:902`) that fans out far wider than the
narrow `purpose` path. Replace with one `WITH RECURSIVE` query (direction,
link-type filter, depth cap, cycle detection as params; SQLite ≥3.35). Entirely
within `catalog_links.py`, does not touch the daemon boundary — **fold in** (it
also makes the daemon's `graph()` one query instead of N round-trips).

**RF-6 (pipeline.db fd-leak, SPLIT): orthogonal subsystem.** `PipelineDB`
(`pipeline_buffer.py:84-106`, `threading.local()` connections) is constructed
per-call at `pipeline_stages.py:617` with no `close()`; thread-local conns
accumulate across a batch. Lives in `pipeline_buffer.py`/`pipeline_stages.py`,
not the catalog module; different root cause (resource lifecycle, not lock
contention). **Split** to its own bead (filed: see References).

**RF-7 (PARTIALLY CORRECT — its load-bearing claim is WRONG, superseded by
RF-8).** Correct parts: `T2Database` composes 8 domain stores
(`db/t2/__init__.py:326`); the 8th, `catalog`, opens its own `.catalog.db` under
`catalog_path()` (`config.py:445`); the daemon lists `catalog` in
`_T2_STORE_ATTRS` (`t2_daemon.py:463-472`); there is no fourth daemon and PC-1
dissolves. **WRONG part:** RF-7 claimed `catalog.register`/`update`/`create_link`
are "already served over RPC today." They are NOT — see RF-8. This error (shared
by the `nx_plan_audit` "framing verified") came from assuming the dispatch table's
enumerated catalog methods *included* register/link without checking the served
class. They are not on it.

**RF-8 (CORRECTION + bounded re-plan, 2026-06-03): the daemon serves
`CatalogStore`, not the rich `Catalog`; hosting the rich `Catalog` in the daemon
is BOUNDED.** `T2Database.catalog` returns a **`CatalogStore`**
(`db/t2/__init__.py:457-475`) whose entire public surface is
`rebuild / next_document_number / search / descendants / execute / commit /
transaction / bulk_load_documents / close` (Serena symbol overview of
`db/t2/catalog.py`). It has **no** `register`/`update`/`link`/`register_owner`/
`resolve`. Those live only on `nexus.catalog.catalog.Catalog`
(`catalog/catalog.py:495`), a rich wrapper that holds a `CatalogStore` as
`self._db` plus `_Projector`/`_WriteOps` and runs entirely in consumer processes.
So `_build_dispatch_table` serves only `CatalogStore`'s methods; a client calling
`catalog.register` hits an unknown-op error. Sub-findings from the bounded-ness
investigation:
- **Q1/Q6:** a 16-op served write subset covers all 49 sites (`register_owner`,
  `ensure_owner_for_repo`, `register`, `update`, `link`, `link_if_absent`,
  `unlink`, `delete_document`, `register_collection`, `delete_collection_projection`,
  `supersede_collection`, `set_owner_head_hash`, `write_manifest`,
  `append_manifest_chunks`, `atomic_manifest_replace`, `resync_chunk_count_cache`).
  Link *generators* are module functions calling `link`/`link_if_absent` — serve
  the primitives.
- **Q2:** the ONLY serialization boundary is `Tumbler↔str` (`str(t)` / `Tumbler.parse`);
  no `CatalogEntry`/`CatalogLink`/`Owner` appear as write args/returns. ~3 lines
  per op.
- **Q3 (load-bearing constraint):** every write touches `.catalog.db` **and** the
  JSONL append path (`owners.jsonl`/`documents.jsonl`/`links.jsonl`/`events.jsonl`)
  including the `owners.jsonl` `next_seq` high-water mark. Mixed-mode (some sites
  via RPC, some in-process) double-allocates `next_seq` → duplicate tumblers +
  flock contention. **Cutover must be ATOMIC**, not the originally-planned two
  incremental waves. No cross-store writes (only `.catalog.db` + JSONL).
- **Q4:** the rich `Catalog` adds NO heavy deps (sklearn/numpy live in
  `catalog_taxonomy`, already imported by the daemon); `_ensure_consistent` at
  construction is mtime-gated, sub-ms in steady state.
- **Q5:** reads stay local — SQLite WAL read-committed makes a local reader see
  daemon-committed writes on the next statement; the only staleness risk is the
  JSONL `read_owners()` inside `register()`, eliminated by full cutover.
- **New denies:** `resolve_span`, `resolve_chash`, `link_audit` take a live T3
  `ClientAPI` → add to `_RPC_DENY_OPS`.
- **Verdict: BOUNDED.** Reuses the T2 daemon; mechanical shim surface; the real
  discipline is the atomic-cutover constraint (Q3).

## Open Questions

- **Fairness mechanism** (item 3): yield-between-units vs daemon-side priority
  queue vs preemptible background lease. RF-3 says the write path is already
  per-record commits, so a daemon-side priority bit (`CatalogClient` sets
  `priority=interactive|batch`) plus "background `nx index repo` skips the
  *catalog writer* when an interactive write is in flight" is the leading option
  against the respawn-within-1-min observation. Lock at planning.
- **Migration / coexistence during cutover** — shadow-read against the live direct
  path before flipping, mirroring the RDR-129 cutover discipline. (nexus-41otf
  scope is resolved: folded, RF-5; the fd-leak is split to `nexus-c341x`, RF-6.)

### Planning-time concerns (from the 2026-06-03 gate critique — 0 Critical, 3 Significant)

The gate PASSED (no Critical; the fourth-daemon election risk is additive, not
multiplicative — independent lock/discovery/db files). These three must be locked
at planning before implementation, and become first-class planning beads:

- **PC-1 (share vs copy the RDR-140 machinery).** "Reuse RDR-140 machinery" is
  under-specified. Decide (a) extract a shared `BaseDaemon`/utility module that both
  T2 and Catalog daemons use, vs (b) a parallel copy-adapted `catalog_daemon.py`.
  Option (a) is strongly preferred — a copy diverges on the next T2 fix and
  recreates the 5.0.2–5.0.4 patch-per-incident debt. RDR-140 documents ≥6 distinct
  election failure modes (release-before-exit, no-attach path, unconditional reap,
  no election lock, migrate-on-every-start, no discovery-file re-assert) that any
  correct catalog daemon must reproduce. If (a), add a "generalize T2 election
  machinery into BaseDaemon" planning bead.
- **PC-2 (fairness cross-client signaling protocol).** Serializing writes through
  one daemon queue removes WAL contention, but starvation re-emerges if background
  fills the queue faster than the daemon drains it. Specify: (a) how interactive
  clients register priority (a flag on the write RPC is simplest); (b) how
  background indexers learn an interactive write is pending (daemon probe RPC vs
  push); (c) what "skip" means operationally (skip-until-retry-window vs buffer
  locally). Resolve whether items 3 and 4 **compose or collapse into one
  mechanism** (see PC-4 below). Without this, FIFO-with-priority-bit only reorders
  already-queued items and relocates the race rather than killing it. Mirror
  RDR-129 B2 (bounded lock-retry in serving dispatch).
- **PC-3 (56-site cutover discipline).** Reuse the RDR-128 epsilon-allow protocol
  directly: baseline the `Catalog(...)` construction-site count, wire it into
  `nx doctor`, define a monotonically-decreasing acceptance criterion and the
  documented-irreducible exemption set. Without it the migration stalls partway as
  the T2 one did (20 raw-connect + 53 construction exemptions accrued before
  RDR-128). Add a P0-analog baseline bead.

**PC-4 (RESOLVED 2026-06-03 — see RF-7).** The catalog IS already the 8th T2
domain store, served over the existing T2 daemon; `register`/`update`/`create_link`
are already RPC ops. There is **no fourth daemon** — item 1 is the "route catalog
writes through the *existing* T2 daemon" best case. This dissolves PC-1 and the
gate's headline election-risk concern. The one carried-forward unknown: whether
the indexer's denylisted `transaction()` / `bulk_load_documents()` batch paths
need a new JSON-shaped coarse RPC op or stay process-local (RF-3 says the hot path
is per-record, so likely no new op needed — confirm at planning).

**PC-5 (item 3 / item 4 contract under a daemon, gate Observation 2).** Once the
daemon owns the writer and queues writes, "catalog writer available" is always
true to a background client — the client-visible lock disappears. Item 4 therefore
becomes "background indexer asks the daemon whether an interactive write is in
flight," which is the same probe as PC-2(b). Plan items 3 and 4 as one mechanism
unless a reason to keep them separate emerges.

## Out of Scope

- The Postgres rewrite (shelved, NO-GO) and any multitenancy seams.
- Chroma / T3 changes. The Chroma duplicate-on-rewrite fix (content-hash keyed
  upsert) is a **sibling** effort tracked separately, not part of this RDR.

## References

- GH #1046 (catalog.db starvation, the empirical driver).
- T2 `nexus/concurrency-catalog-db-starvation-2026-05-31` (live root-cause notes).
- T2 `nexus/postgres-storage-service-plan-v2-NO-GO-2026-06-03` (the rejected alternative + decision).
- RDR-120 (storage substrate split), RDR-128 (single-writer enforcement),
  RDR-129 (write-path hardening), RDR-140 (daemon supervisor ownership) — the
  proven pattern this RDR extends to a fourth store.
- nexus-41otf (traverse link_types hang).
