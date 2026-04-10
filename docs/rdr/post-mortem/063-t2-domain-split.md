---
rdr: RDR-063
title: "T2 Domain Split — Memory / Plans / Catalog / Telemetry"
closed_date: 2026-04-10
close_reason: implemented
---

# Post-Mortem: RDR-063 — T2 Domain Split

## RDR Summary

T2 had absorbed four distinct domains under a single `T2Database` class and a single `sqlite3.Connection` + `threading.Lock`: agent memory, plan library, catalog taxonomy, and relevance telemetry. A long `find_overlapping_memories` scan could block telemetry writes; an expensive `cluster_and_persist` rebuild could block interactive memory access; the `topic_assignments.doc_id` ↔ `memory.title` cross-schema JOIN was implicit and untyped.

The RDR split the monolith into four domain stores under `src/nexus/db/t2/` in two phases:

- **Phase 1** — logical split behind a shared connection. Each store owns its own tables and methods; `T2Database` becomes a composing facade. No schema change, no behavior change.
- **Phase 2** — promote each store to its own `sqlite3.Connection` + `threading.Lock` against the shared SQLite file in WAL mode. Concurrent reads in one domain no longer block on writes in another.

Phase 3 (physical file split) is explicitly deferred and requires its own RDR if pursued.

## Implementation Status

All 13 beads closed across Phase 1 (9 beads) and Phase 2 (4 beads):

- **Phase 1**: `nexus-2oo7` (pre-work) → `nexus-mwdo` (scaffold) → `nexus-vx3c` (MemoryStore) → `nexus-kpe7` (PlanLibrary) → `nexus-u29l` (CatalogTaxonomy) → `nexus-yjww` (Telemetry) → `nexus-g5e4` (facade + guards + shim) → `nexus-rpnk` (test patches) → `nexus-jsx5` (review remediation).
- **Phase 2**: `nexus-3d3k` (per-store connections) → `nexus-9xa6` (concurrency tests + baseline) → `nexus-45sb` (docs) → `nexus-s8o5` (review remediation).

Final state on branch `feature/nexus-4y8d-rdr063-t2-split` (11 commits ahead of main): full non-integration suite passes **3456 / 14 skipped**; focused T2 suite (7 files, 181 tests) passes in 7.4s; concurrency acceptance gate passes with ratio **0.93–1.04x** across six stability runs (gate <1.5x).

## Implementation vs. Plan

| Area | Planned | Delivered | Drift |
|------|---------|-----------|-------|
| Logical split into 4 domain modules | `memory_store`, `plan_library`, `catalog_taxonomy`, `telemetry` | ✓ All four extracted, `T2Database` reduced to composing facade | None |
| Per-store `sqlite3.Connection` + `threading.Lock` | Each store opens its own connection in WAL mode with `busy_timeout=5000` | ✓ Implemented; `SharedConnection` dataclass deleted, `T2Database.conn` / `._lock` removed | None |
| Cross-domain coupling made explicit | `CatalogTaxonomy` constructor takes a `MemoryStore` reference for `get_topic_docs` JOIN | ✓ Constructor-level dependency, no hidden imports | None |
| Per-domain migration guards | Per-domain `_migrated_paths` + `_migrated_lock` (Open Question 3, Option B) | ✓ Memory, plans, taxonomy each own a guard; telemetry has no migrations and no guard | None |
| Cross-domain `expire()` composition | Facade calls memory + telemetry expire in sequence | ✓ Facade `expire()` delegates to all stores that register expiry work | None |
| Concurrency tests in `tests/test_t2_concurrency.py` | Memory reads not blocked by telemetry writes | ✓ 4 tests: cross-domain parallelism, same-store serialization, baseline, under-load acceptance gate | See Drift 2 |
| Success criterion: `t2.py < 50 LOC (facade only)` | < 50 LOC | `src/nexus/db/t2/__init__.py` = **367 LOC** | **Drift 1** |
| Success criterion: each domain module < 400 LOC | < 400 LOC | memory_store 790, catalog_taxonomy 449, plan_library 320, telemetry 225 | **Drift 1** |
| Phase 2: `cluster_and_persist` does not block memory_search | Benchmarked | Architectural guarantee verified (separate connection); not explicitly measured under load | **Drift 2** |
| Access tracking in `memory.search` | Unchanged (inherited from RDR-057) | Rewritten to best-effort fail-fast under contention | **Drift 3** |

## Drift Classification

### Drift 1 — Module size targets missed
The RDR targeted `< 50 LOC` for the facade and `< 400 LOC` per domain module. Actuals: facade 367 (7× over), memory_store 790 (~2× over), catalog_taxonomy 449 (slightly over), plan_library 320 (✓), telemetry 225 (✓).

The overruns partially reflect domain scope, but the honest read is that one alternative was not examined:

- **Facade** — the 367 LOC includes a ~60-line module docstring describing the architecture, imports, and thin delegate methods for 30+ public T2 operations. No implementation logic lives in the facade; the RDR's 50 LOC number assumed a minimal shim, not a backward-compatible surface. This overrun is genuinely explained by the compatibility requirement.
- **memory_store** — absorbs memory CRUD + FTS5 sanitization + access tracking + TTL expire + RDR-057 consolidation helpers (`find_overlapping_memories`, `merge_memories`, `flag_stale_memories`) + migration guards. A defensible further split would have extracted the consolidation helpers into their own `memory_consolidation.py` module (possibly as a separate domain store or as a helper class composed into `MemoryStore`). That would cut `memory_store.py` from 790 → ~550 LOC and put it back under the 400 target… no, still over. A two-way split of memory (core CRUD + consolidation) was not examined during the RDR design phase, and it was not examined during implementation either. The decision to keep everything in `memory_store.py` was made by default, not by analysis. Whether that was the right call remains an open question — the consolidation helpers are distinct enough in purpose (bulk deduplication / staleness / merge) that a follow-up refactor extracting them would be legitimate.
- **catalog_taxonomy** — 49 LOC over the 400 target. The cross-domain JOIN machinery and topic-assignment CRUD are inherently tangled.

**Classification**: Minor in practice (the split is functional and the tests pass), but the framing "numeric targets were aspirational" is incomplete. The honest statement is: one meaningful alternative decomposition (extracting RDR-057 consolidation helpers) was not considered during design or implementation, and the numeric miss on `memory_store.py` is a symptom of that oversight. Functional modularity, import visibility, and test-site coverage are still in place, and no refactor was skipped to hit a LOC budget — but a different decomposition might have been cleaner.

### Drift 2 — `cluster_and_persist` under load not explicitly benchmarked
RDR Success Criterion 2c says "cluster_and_persist does not block memory_search for its duration". The Phase 2 architecture guarantees this (taxonomy and memory now run on separate `sqlite3.Connection` instances), and `test_concurrent_domain_writes_no_contention` exercises memory + plans + telemetry in parallel. But no test explicitly measures memory_search latency *during* a `cluster_and_persist` rebuild. The cross-domain parallelism is covered for the common case; the specific taxonomy-clustering path is covered architecturally but not empirically.

**Classification**: Minor coverage gap. Follow-up candidate if taxonomy clustering shows up as a latency source in production.

### Drift 3 — Best-effort access tracking (behavior change discovered during Phase 2 review)
Not in the RDR plan. Phase 2 exposed a latent bug that Phase 1's global Python lock had masked: `memory.search(access="track")` and `memory.get()` do `SELECT` → `UPDATE access_count` → `commit` inside the memory store's lock. Under concurrent cross-domain writes, the `UPDATE` contended for SQLite's single writer lock and either (a) blocked the caller for the full 5-second `busy_timeout` or (b) failed with `SQLITE_BUSY`. Both outcomes violated the acceptance gate.

**Fix** (commits `4871a75` + `aa5bcf9` for `search`; a later commit extended the same treatment to `get`): access tracking is now a best-effort statistical signal. The `UPDATE` runs with a temporary `PRAGMA busy_timeout = 0` (fail-fast) inside a try/finally that restores the 5-second default. `SQLITE_BUSY` is swallowed and logged at warning (`memory.access_tracking.skipped`); other `OperationalError` subclasses (SQLITE_CORRUPT, SQLITE_IOERR, SQLITE_CANTOPEN) still propagate via the shared `_is_sqlite_busy` helper which uses `exc.sqlite_errorcode` for precision. Under the under-load test, 5–10% of updates skip; the acceptance ratio is 0.93–1.04x of the single-threaded baseline for `search` and 1.08–1.22x for `get`.

**Classification**: This is a **behavior change**, not a pure bug fix. The pre-split behavior guaranteed that every in-scope `search` or `get` call would increment `access_count` and update `last_accessed` on success. The post-split behavior does not — under sustained write load, some fraction of calls silently skip the counter update. The RDR plan said Phase 2 would change *coordination topology* only, not observable per-call semantics. This change was not anticipated, it was not discussed in the RDR, and it was discovered because the acceptance gate empirically surfaced it. Calling it "a feature of the Phase 2 transition" understates the honesty cost: it is a deliberately-accepted behavior change that improves tail latency at the expense of counter precision, and future readers should know that.

**Interaction with RDR-057**: RDR-057's heat-weighted TTL uses `access_count` as a survivability multiplier (`effective_ttl = base_ttl * (1 + log(access_count + 1))`). Best-effort tracking means heavily-accessed entries can have their `access_count` undercounted specifically during heavy cross-domain write load — which is often exactly when they are most active. The resulting TTL compression is small (log curve; a 10% undercount on 10 accesses is ~3% TTL shortening) but it is not uniformly distributed across the memory set. This interaction was not analyzed during the RDR-063 design or implementation phases. It is now documented in `docs/storage-tiers.md` under the Heat-Weighted Expiry section. Follow-up consideration: if heat-weighted TTL correctness ever becomes load-bearing, the fix is to move access tracking onto its own connection or a background queue so it doesn't contend with the agent's interactive reads. That would be a Phase 2 revision, not a Phase 3 question.

## Carry-Forward Items (not addressed in this RDR)

1. **CatalogTaxonomy.get_topic_docs Known Defect** (RDR-063 §Known Defect Option 3). T3-origin topics return `title=doc_id` as a fallback because the memory table doesn't hold those rows. Pinned by regression test `test_get_topic_docs_known_defect_project_collection_mismatch`. Documented, not fixed — the fix requires RDR-061 catalog integration work.

2. **Phase 3 fragility in `get_topic_docs`.** The cross-table JOIN (`topic_assignments ⋈ memory`) runs on the taxonomy connection and works only because Phase 2 keeps all four domains in a single SQLite file. If Phase 3 (physical file split) ever proceeds, the taxonomy connection loses sight of the `memory` table and the JOIN silently returns empty — masquerading as the Known Defect above. Flagged in a method docstring comment in `catalog_taxonomy.py::get_topic_docs`. Must be redesigned into a two-step fetch (taxonomy rows → `self._memory.get(...)` resolution) as part of any Phase 3 RDR.

3. **Benchmark gap for `cluster_and_persist` under memory search load.** RDR Success Criterion 2c ("cluster_and_persist does not block memory_search for its duration") is architecturally guaranteed by the per-store connection model but not explicitly benchmarked. The cross-domain tests cover the common path (telemetry + plans under memory reads) but not the specific taxonomy-clustering path. Follow-up candidate if taxonomy clustering shows up as a latency source in production, or as a proactive rigor improvement.

4. **Phase 3** (physical file split). Explicitly deferred. Revisit only if Phase 2 metrics show lock contention persists at the file level. Requires its own RDR.

### Addressed during close (previously listed as carry-forward)

- **`memory.get()` contention.** Initially listed here as a carry-forward after `nexus-s8o5`. The substantive-critic audit correctly pushed back on the "out of scope" framing: `get()` is called on interactive user-facing paths (`mcp/core.py` `memory_get` tool, `commands/memory.py`, `merge_memories` verification), and the same SELECT + UPDATE pattern meant Phase 2 had introduced the same 5-second-freeze regression as `search()` but with smaller blast radius per call. Fixed in the same session: `memory.get()` now uses the same fast-fail `busy_timeout = 0` + `_is_sqlite_busy` treatment as `memory.search()`. New concurrency test `test_memory_get_under_concurrent_write_load` establishes the same ratio gate (baseline ~ 0.15ms, under-load ~ 0.18ms, ratio 1.08–1.22x across stability runs).

## What Went Well

- **Phase 1 mechanical extraction held up exactly as planned.** Nine beads, no schema change, no behavior change, ~70 test sites rewritten from `db.conn` / `db._lock` to per-store attributes. Full non-integration suite stayed green at every step.
- **The concurrency acceptance gate methodology.** Measuring baseline and under-load in the same test process with a ratio assertion (not an absolute bound) eliminated hardware and CI variance. Six stability runs, ratio spread 0.93–1.04x, no flakiness.
- **Code-review-expert caught four real issues pre-merge.** Two of them were non-trivial: (a) transposed positional args in code examples (silent FTS5 misbehavior if copy-pasted), and (b) an overclaim about write-vs-write parallelism in `docs/architecture.md` that would have misled anyone reading the docs about the WAL single-writer constraint. The two hardening recommendations (narrow `OperationalError` catch to `SQLITE_BUSY`; move `PRAGMA busy_timeout = 0` inside the try/finally) closed real fault modes even though they were unlikely to fire.
- **Per-domain migration guards (Option B) work as specified.** `test_migration_guard_concurrent_threads` proves 10 concurrent `T2Database` constructors run the migration exactly once.

## Takeaways

### 1. Numeric LOC targets in RDRs should reflect actual domain scope

The RDR set `< 50 LOC` for the facade and `< 400 LOC` per domain module without enumerating what each module would own. memory_store was always going to be the outlier — it's the most-methoded table in T2. Setting numeric targets before auditing the scope creates meaningless drift at close time.

**Process note**: for future "split monolith X" RDRs, either enumerate the methods each new module will own (and let the LOC fall where it falls) or skip the LOC target entirely. The meaningful success signal is "each module has a single responsibility and no cross-module state leakage", not a line count.

### 2. Phase 2 exposed latent bugs that Phase 1's global lock had hidden

The access-tracking contention bug was not new in Phase 2 — it was latent in Phase 1 under the global `threading.Lock`, which happened to serialize every memory, plans, taxonomy, and telemetry write behind a single mutex. When Phase 2 removed that mutex, the SQLite layer's own write-lock serialization became the only coordinator, and the race surfaced immediately under the under-load benchmark.

**Lesson**: any refactor that removes a coarse Python-level lock in favor of a finer-grained storage-level coordinator will expose races that the coarse lock was silently masking. The Phase 2 design was right to add an explicit benchmark that exercises concurrent cross-domain writes — without it, this bug would have shipped unnoticed.

### 3. Best-effort side-effects on read paths need explicit contention handling

`memory.search` was originally written as a pure read. RDR-057 added access tracking as an `UPDATE` inside the same transaction. This conflates two different failure modes — a read that fails because the data is gone (real failure) and a read-with-side-effect that fails because someone else is writing (recoverable). The fix treats access tracking as a best-effort statistical signal and decouples its fate from the search result.

**Pattern to codify**: when a read path has a write side-effect (access tracking, last-used-at, frecency updates, etc.), it should:
1. Complete the read first and commit any read transaction.
2. Run the side-effect under a short busy_timeout (fail-fast).
3. Log and swallow `SQLITE_BUSY` specifically.
4. Restore the normal busy_timeout in a `finally`.

This pattern applies to `memory.get()` too (carry-forward item 1) and should be considered whenever a future read gains a side-effect.

### 4. Two-agent review catches things a single agent misses

`nx:code-review-expert` caught R1 (the transposed args bug) and R2 (the write-concurrency overclaim) which I had read past during the docs writeup. An inline self-review found the test-validation issues (rewrite correctness, access_count test safety, migration-guard concurrency). Neither review alone would have surfaced all of these — the static-analysis agent caught what I couldn't see; the inline validation caught what a static-only agent couldn't.

**Note**: background agents should not run tests concurrently with the main thread (per session rules). The test-validator agent was stopped and its work done inline. In future reviews, either serialize the agents or restrict background reviewers to static-only work.

## Artifacts

- **Branch**: `feature/nexus-4y8d-rdr063-t2-split` (11 commits ahead of main)
- **Phase 1 commits**: `501e62b` (scaffold) → `a1ac728` → `bc53b56` → `695eab7` → `414c928` → `c874c98` (review remediation)
- **Phase 2 commits**: `6faf4a7` (per-store connections) → `339e9c6` (concurrency tests) → `4598444` (docs) → `4871a75` (access-tracking fix) → `aa5bcf9` (review remediation)
- **Test files**: `tests/test_t2_concurrency.py` (new, 4 tests), `tests/test_t2.py::test_migration_guard_concurrent_threads` (per-domain guard concurrency proof)
- **nx memory**: `rdr-063-concurrency-baseline` (id=662, single-threaded p95 baseline), `rdr-063-phase2-complete` (id=667, full Phase 2 summary)
- **Implementation**: `src/nexus/db/t2/__init__.py` (facade), `memory_store.py`, `plan_library.py`, `catalog_taxonomy.py`, `telemetry.py`
- **Docs**: `docs/architecture.md` § T2 Domain Stores, `docs/contributing.md` § Adding a T2 Domain Feature
