---
title: "RDR-063: T2 Domain Split — Separating Memory, Plans, Catalog, and Telemetry"
status: draft
type: architecture
priority: P2
created: 2026-04-09
reviewed-by: self
---

# RDR-063: T2 Domain Split — Separating Memory, Plans, Catalog, and Telemetry

## Problem Statement

T2 was originally defined as a single-purpose store: **per-project persistent memory** — notes, session state, and research context for agents. After RDR-058 (plan library), RDR-061 E5 (persistent taxonomy), RDR-061 E2 (retrieval feedback), and RDR-061 E6 (memory consolidation), T2 now holds five tables across four distinct domains:

| Table | Domain | Scope | Owner |
|-------|--------|-------|-------|
| `memory` | Agent memory | Per-project | Agent (via `memory_*` tools) |
| `plans` | Query plans | Per-project | Skills (via `plan_save`/`plan_search`) |
| `topics` + `topic_assignments` | Catalog taxonomy | Per-collection | Clustering pipeline |
| `relevance_log` | Search telemetry | Session-scoped | MCP search/store hooks |

The problems this causes are concrete, not theoretical:

1. **Mixed concurrency regimes (forward-looking)**: `memory` sees interactive writes (agent actions at human pace). `relevance_log` sees automated writes (one batch per `store_put`, potentially many per second during indexing). They share a single SQLite file and a single `threading.Lock`. A long `find_overlapping_memories` scan will block `relevance_log` inserts for the duration. This is currently microseconds for small memory tables (<500 rows) and has not been observed as a bottleneck; it is a forward-looking design concern as projects accumulate memory entries past O(1000). Phase 2 resolves this preemptively.

2. **Cross-schema assumption leaks**: `topic_assignments.doc_id` joins against `memory.title` — an implicit contract between two tables that belong to different domains. The join only works when T2 memory entries are the universe of clusterable documents, which is neither stated in schema nor enforced. Documents indexed via `store_put` (T3) cannot participate.

3. **Opaque disk footprint**: Operators can't tell why `memory.db` is 500 MB. Is it agent notes? Plan library? Taxonomy vectors? Relevance log rows? The answer requires `sqlite3` CLI and domain knowledge.

4. **Incompatible retention policies**:
   - `memory`: heat-weighted TTL (RDR-057)
   - `plans`: explicit TTL at save time
   - `topics`: rebuilt on-demand, no retention
   - `relevance_log`: 90-day purge (RDR-061 E2)

   Four policies in one `expire()` method that currently handles only `memory` and (as of this branch) `relevance_log`. Adding taxonomy expiry or plan rebuild means conflating more concerns.

5. **Unclear ownership for future features**: If a new feature needs durable state, which table does it join? The answer today is "put it in T2" because T2 has migration machinery and a `T2Database` class. This is how `topics` and `relevance_log` got there — by availability, not design.

6. **Migration coupling**: All five table migrations live in `T2Database._init_schema`. A bug in `_migrate_relevance_log_if_needed` could corrupt the migration sequence for `memory`. The RDR-062 follow-up (run migrations once per process) treats all five as a single batch.

## Non-Goals

- **Not a rewrite of T2's API**: `memory_put`, `memory_search`, `plan_save`, etc. keep their signatures. Callers should not need to change.
- **Not a multi-file database rearrangement today**: The split may eventually mean separate SQLite files, but the first step is logical separation inside `nexus.db` — module boundaries, type boundaries, lock boundaries.
- **Not a redefinition of tiers**: T1/T2/T3 as storage tiers remain. This is about the *internal structure* of T2, not the tier model.

## Known Defect (must be acknowledged, not canonized by the split)

`taxonomy.get_topic_docs()` builds its JOIN as:

```sql
LEFT JOIN memory m ON m.title = ta.doc_id AND m.project = (
    SELECT collection FROM topics WHERE id = ta.topic_id
)
```

This conflates `topics.collection` (a T3 ChromaDB collection name, e.g. `knowledge__research`) with `memory.project` (a T2 project scope). The JOIN silently returns empty results for any taxonomy clustered from T3 collections — which is the primary RDR-061 E5 use case.

**This is a pre-existing defect, not introduced by the split.** RDR-063 acknowledges it here so the split does not inadvertently canonize the broken JOIN as an "explicit contract."

**Decision**: Option 3 — **accept the restriction and document it**. `get_topic_docs()` is a convenience view for T2-clustered projects. T3 collection taxonomies do not need `memory.title` resolution because chunk titles live in T3 metadata, not T2. When a future feature needs T3 chunk title resolution, it should go through the catalog (`CatalogEntry.title`), not T2 memory.

Rationale for rejecting the alternatives:
1. **memory_project column on topics**: adds schema complexity for a feature no current caller exercises. T3-origin topics are only used by `cluster_and_persist` which clusters over T2 memory entries — the JOIN already works for that path.
2. **Change JOIN to catalog lookup**: couples `catalog_taxonomy` to the catalog module, deepening cross-module coupling at the exact moment the split is trying to reduce it.

Action for Phase 1: update `get_topic_docs()` docstring to explicitly state the T2-only scope. Add a regression test (already present: `test_get_topic_docs_known_defect_project_collection_mismatch`) that documents the behavior. No schema change.

## Research Findings

Concrete grounding data captured during v3.7.0 release (2026-04-10). All numbers are file-line facts, not estimates. This section calibrates the Phase 1 scope before a bead is cut.

### RF-063-1: T2 module size breakdown

**Source**: `src/nexus/db/t2.py` (1,052 LOC total)

| Domain | LOC | Location |
|--------|-----|----------|
| Memory | ~250 | Schema 55–97 + `put`/`search`/`search_glob`/`search_by_tag`/`get_all`/`find_overlapping_memories`/`merge_memories`/`flag_stale_memories` |
| Plans | ~80 | Schema 98–130 + `save_plan`/`search_plans`/`list_plans` |
| Topics / topic_assignments | ~15 | Schema 132–146 only — **all behavior lives in `taxonomy.py`** |
| Relevance log | ~60 | Schema + `log_relevance`/`log_relevance_batch`/`get_relevance_log`/`expire_relevance_log` |
| Migration functions | ~169 | 6 functions: `_migrate_fts_if_needed` (40), `_migrate_plans_if_needed` (42), `_migrate_topics_if_needed` (27), `_migrate_relevance_log_if_needed` (32), `_migrate_access_tracking_if_needed` (18), `_migrate_plans_ttl_if_needed` (10) |
| Infrastructure | ~200 | FTS5 helpers, `_SCHEMA_SQL`, `_sanitize_fts5`, `__init__`, `_init_schema`, `close`, context manager, `expire` |

**Implication**: Memory is the largest domain and deserves its own file on merit alone. Plans is small but architecturally distinct (TTL-based, no access tracking). Relevance_log is smallest and isolated — an easy early extraction target. Migrations add ~169 LOC that must be distributed correctly to preserve the per-domain guard keys.

### RF-063-2: taxonomy.py coupling to T2 internals

**Source**: `src/nexus/taxonomy.py`

**Direct T2 internal access**: 19 references to `db._lock` and `db.conn.execute` across 6 functions:
- `get_topics()` (43–54) — 2 queries + `_lock`
- `assign_topic()` (58–65) — 2 INSERT/UPDATE + `_lock`
- `get_topic_docs()` (68–104) — 1 multi-table LEFT JOIN + `_lock`
- `get_topic_tree()` (107–145) — 4 recursive queries + `_lock`
- `clear_project_topics()` (230–239) — 2 DELETE + `_lock`
- `cluster_and_persist()` (148–244) — ~4 `db.get_all()` + INSERT sequences

**The critical JOIN** (lines 92–103):
```sql
SELECT ta.doc_id, m.title, m.project
FROM topic_assignments ta
LEFT JOIN memory m ON m.title = ta.doc_id AND m.project = (
    SELECT collection FROM topics WHERE id = ta.topic_id
)
WHERE ta.topic_id = ?
```

This JOIN assumes `memory.title == topic_assignments.doc_id` AND `memory.project == topics.collection`. It breaks for T3-origin topics (known defect, documented in RDR-063 Known Defect section).

**Implication for Phase 1 step 4**: `taxonomy.py` does not use a public T2 API — it reaches through `db._lock` and `db.conn.execute` directly. Leaving these references intact defeats Phase 2's goal of separate per-domain connection locks. The move from `taxonomy.py` → `catalog_taxonomy.py` must rewrite every call site to use the new module's own connection, not `T2Database`'s.

### RF-063-3: Test inventory for characterization baseline

**Source**: `grep -rl "T2Database\|taxonomy\." tests/` (captured 2026-04-10)

**Files instantiating `T2Database`**: 18 of 130 total test files (13.8%)

**Key test counts** (union of T2 + consolidation + taxonomy + relevance_log coverage):
- `tests/test_t2.py` — 58 tests (primary T2 suite)
- `tests/test_memory.py` — 28 tests
- `tests/test_memory_consolidation.py` — 28 tests (incl. MCP integration)
- `tests/test_relevance_log.py` — 24 tests (incl. contract tests)
- `tests/test_taxonomy.py` — 14 tests
- `tests/test_schema.py` — 1 test

**Total**: ~153 tests across 6 files exercise T2 directly. The Phase 1 characterization test (`tests/test_t2_facade.py`) must preserve the behavioral contract these 153 tests already verify.

**Files patching T2 internals** (will need updating during refactor):
- `_t2_ctx`: 24 monkeypatch references (mostly `test_memory_consolidation.py`)
- `_migrate_*`: 7 references (migration guard counting tests)
- `_migrated_paths`: 4 references (per-process guard tests)
- `_init_schema`: 1 reference

**No `:memory:` databases used** — all tests use `tmp_path` with file-based SQLite. Phase 2's multi-connection model is safe from the `:memory:` incompatibility footgun.

### RF-063-4: Cross-domain coupling points

**Source**: grep across `src/nexus/` for cross-table SQL

Only ONE true cross-domain JOIN exists: the `topic_assignments ↔ memory ↔ topics` query in `taxonomy.get_topic_docs()` (RF-063-2).

**Other "cross-domain" references** that turn out to be single-table:
- `plans` table has 1 external reference in `commands/catalog.py:94` (reads for builtin-template filtering) — not a JOIN, just a SELECT
- Relevance_log: zero external references; pure write-aggregation

**FTS5 triggers**: intra-domain only. `memory_fts` triggers fire only on `memory` INSERT/UPDATE/DELETE. `plans_fts` triggers are isolated. No cross-domain trigger chains.

**Implication**: Phase 1's module split is structurally safe. Only one JOIN crosses domain boundaries, and it's already identified as a Known Defect. All other SQL is single-table and can be confined to its owning module.

### RF-063-5: Caller distribution — who calls what?

**Source**: grep for `db.<method>` across `src/` and `tests/`

| Method | Production call sites | Test call sites | Callers |
|--------|----------------------|-----------------|---------|
| `db.put` | 11 | 293 | hooks.py, mcp/core.py, commands/{memory,store,scratch}.py |
| `db.search` | 14 | 76 | catalog/catalog.py, mcp/core.py, commands/memory.py, search_engine.py, doc_indexer.py |
| `db.save_plan` | 2 | 26 | mcp/core.py, commands/catalog.py |
| `db.log_relevance` / `log_relevance_batch` | 2 | 23 | mcp/core.py, mcp/catalog.py |
| `db.find_overlapping_memories` | 1 | 4 | mcp/core.py |
| `taxonomy.get_topic_docs` | 3 | 6 | commands/taxonomy_cmd.py, taxonomy.py (internal), tests |
| `taxonomy.cluster_and_persist` | 2 | 3 | taxonomy.py (internal), search_engine.py |

**Implication**: Memory has the largest call-site footprint (11 prod + 293 test). Phase 1's facade must preserve `T2Database.put`, `T2Database.search`, `T2Database.get`, `T2Database.delete` as public methods delegating to `MemoryStore`. Plans has only 2 production callers — cheaper to migrate. Relevance_log has only 2 production callers (both in MCP hooks) — easiest.

### RF-063-6: Migration guard current state

**Source**: `src/nexus/db/t2.py` lines 181–233

```python
_migrated_paths: set[str] = set()
_migrated_lock = threading.Lock()
```

**Thread safety**: Guarded by `_migrated_lock` held across the full check-run-add sequence (round 2 fix). Two concurrent `T2Database` constructors on the same path cannot both enter the migration functions.

**Path canonicalization**: `path.resolve()` at `__init__` time resolves symlinks and `.` segments (round 3 fix). Prevents two T2Database instances on different path strings pointing at the same file from bypassing each other's guard.

**Runtime cost**: MCP server opens a fresh `T2Database` per tool call (via `_t2_ctx()` in `mcp_infra.py`). Without the guard, each call would re-probe all 6 migrations. With the guard, only the first call in a process pays that cost.

**Implication for Phase 1**: Each domain module will need its own guard set (per Open Question 3, already resolved: `memory_store._migrated_paths`, `plan_library._migrated_paths`, etc.). The current single-set design must be split, not shared, or a future migration added to one domain will trigger re-probing of all domains.

### RF-063-7: Per-domain SQL touch distribution

**Source**: `grep -rn "FROM <table>" src/nexus/`

| Table | References in `src/nexus/` | Distribution |
|-------|---------------------------|--------------|
| `memory` | 15 | All in `db/t2.py` |
| `plans` | 6 | 5 in `db/t2.py`, 1 in `commands/catalog.py:94` |
| `topics` | 9 | All in `taxonomy.py` |
| `topic_assignments` | 4 | All in `taxonomy.py` |
| `relevance_log` | 4 | All in `db/t2.py` |

**Implication**: All domain SQL is already confined to 2 files (`db/t2.py` + `taxonomy.py`). The only external references are the single `commands/catalog.py:94` read for plan template filtering, which is trivial to redirect through the facade. This is strong evidence that Phase 1 is a low-risk mechanical refactor: the domain boundaries already exist implicitly; the refactor just makes them explicit in the module structure.

## Proposed Solution

### Phase 1: Internal module split (no file change)

Restructure `src/nexus/db/` into domain-owned modules, each backed by its own `*Database` class, still storing data in the same SQLite file:

```
src/nexus/db/
├── __init__.py
├── t1.py            # T1Database (unchanged)
├── t3.py            # T3Database (unchanged)
├── t2/
│   ├── __init__.py          # T2Database facade (backward-compat wrapper)
│   ├── _connection.py       # Shared SQLite connection + migration guard
│   ├── memory_store.py      # MemoryStore: memory table, FTS, expire, consolidation
│   ├── plan_library.py      # PlanLibrary: plans table, FTS, TTL
│   ├── catalog_taxonomy.py  # CatalogTaxonomy: topics, topic_assignments
│   └── telemetry.py         # Telemetry: relevance_log
└── local_ef.py
```

- Each `*Store` class owns its table schema, migrations, and queries.
- Each class holds a reference to a shared `_Connection` object — still one SQLite file.
- `T2Database` becomes a thin facade that exposes the existing public API by delegating to the underlying stores (`self.memory.put(...)`, `self.plans.save_plan(...)`).
- Public method signatures are preserved: `memory_put`, `memory_search`, etc. keep working.

**Connection model**: `t2/_connection.py` in Phase 1 exports a minimal `SharedConnection` dataclass:
```python
@dataclass(slots=True)
class SharedConnection:
    conn: sqlite3.Connection
    lock: threading.Lock
```
All four stores receive a reference to the same `SharedConnection` instance in their constructors. Phase 1 = one connection, four stores sharing it. Phase 2 promotes each store to open its own `sqlite3.Connection` (and its own `threading.Lock`) and discard the shared object — the `SharedConnection` dataclass becomes a Phase 1-only artifact. Specifying this upfront prevents the Phase 1 bead from inventing a singleton that Phase 2 would then have to unwind.

**Benefits of Phase 1**:
- Each domain's schema, migrations, and queries live in one file (~150-300 LOC per module instead of 900+ in `t2.py`).
- Tests can instantiate a single store in isolation (e.g., `MemoryStore(conn)` without also setting up plans/topics/telemetry).
- Cross-domain joins become explicit (a file importing two modules signals the coupling).

**Cost of Phase 1**: ~8-9 hours total. Steps 1-3 and 5-10 are ~5 hours of mechanical refactoring (no behavior change, no migration). Step 4 (taxonomy.py rewrite) is ~3-4 hours because it is NOT mechanical: each of taxonomy.py's 6 functions accesses `db._lock`/`db.conn.execute` directly (RF-063-2), and moving them onto a per-store connection requires reviewing each function's transaction scope — some functions use `with db._lock` at entry, others nest lock acquisition inside loops (e.g., `get_topic_tree`). Do not rush step 4.

### Phase 2: Separate connections per domain

Each store gets its own `sqlite3.Connection` to the same file, with its own lock:

- `memory_store` and `plan_library` share a read-heavy lock profile (agent-paced writes).
- `telemetry` gets its own connection — high-frequency writes from MCP hooks don't block agent queries.
- `catalog_taxonomy` gets its own connection — expensive cluster rebuilds don't block memory reads.

SQLite with WAL mode supports multi-writer coordination reasonably well; separate connections in the same process are safe. The existing single-lock pattern serializes unnecessarily.

**Benefits of Phase 2**:
- `relevance_log` writes no longer block `memory_search`.
- `cluster_and_persist` can run without freezing interactive memory access.
- Latency attribution becomes possible — each connection can be profiled independently.

**Cost of Phase 2**: ~4 hours. Requires testing under concurrent load — `tests/test_t2_concurrency.py` should exercise memory+telemetry simultaneously.

### Phase 3: Physical file split (optional, future)

If operational pain justifies it, each store moves to its own file:
- `memory.db` — agent memory + plans (small, backed up with project)
- `taxonomy.db` — topics + assignments (rebuilt on demand, disposable)
- `telemetry.db` — relevance_log (high-volume, aggressive retention)

This is a migration with backward-compat cost. Only do it after Phase 1/2 show that logical separation is insufficient.

**This RDR does not commit to Phase 3.** It is listed only to clarify that Phase 1 is forward-compatible with eventual file separation.

## Cross-Domain Contracts (must survive the split)

The three known coupling points that cannot be eliminated by the split:

1. **Taxonomy → Memory**: `topic_assignments.doc_id = memory.title` JOIN in `get_topic_docs()`. This is an explicit design contract today (RDR-061 E5). After the split: `CatalogTaxonomy.get_topic_docs()` imports `MemoryStore` and joins across the two modules. The contract stays, but the coupling is visible in imports rather than implicit in one big class.

2. **Telemetry → T3 chunks**: `relevance_log.chunk_id` references T3 chunk IDs. No foreign key (T3 is not in SQLite), but downstream re-ranking will need to resolve these. No change needed — the reference is text.

3. **Plans → Memory**: Query plans reference memory entries by title via the plan content. No schema-level join, just content reference. No change needed.

## Impact on Existing Callers

All `T2Database` public methods remain. Callers using:

- `from nexus.db.t2 import T2Database` → still works via facade
- `db.put(...)`, `db.search(...)`, `db.save_plan(...)` → all delegate to underlying stores
- Test fixtures using `T2Database(path)` → unchanged

The facade class is intentionally thin — ~50 lines of method delegation — to preserve backward compatibility during the transition. After 2-3 release cycles, callers can be migrated to the domain-specific stores directly.

## Migration Strategy

Because Phase 1 changes no schema and no behavior:
- No SQLite migration
- No data movement
- No version bump for operators
- Tests may need path updates where they patch internal details (`_init_schema`, private methods)

Phase 2 also changes no schema — just lock topology. Any test that held assumptions about single-lock serialization may need updates, but those assumptions were implementation details, not contracts.

## Alternatives Considered

### Alternative A: Do nothing, keep T2 monolithic

**Rejected**: The problems are growing, not shrinking. Each new feature adds more coupling. Memory consolidation already depends on access tracking from a different RDR. Taxonomy joins memory by title. Fix it now at ~10 hours of work, or fix it later at 30+ hours when the implicit contracts have calcified.

### Alternative B: Move taxonomy to T3

**Rejected**: T3 is ChromaDB — it stores embeddings, not relational data. Taxonomy needs `topic_assignments` (a many-to-many join table) and hierarchical parent_id references. SQL is the right tool. Moving taxonomy to T3 would require storing it as metadata on chunks, which loses the hierarchical structure.

### Alternative C: Move telemetry to its own SQLite file immediately (Phase 3 first)

**Rejected**: Too much upfront cost for uncertain benefit. Phase 1 (logical split) is reversible and tells us whether lock contention is the real bottleneck. Phase 3 can happen later if metrics show T2 writes are the pain point.

### Alternative D: Make the existing `T2Database` class an interface with multiple implementations

**Rejected**: `T2Database` is the concrete class with state. An interface approach would require every caller to import from a Protocol, and the test code would need dependency injection everywhere. The facade approach keeps call sites unchanged and confines the refactor to one module.

## Implementation Plan

### Phase 1 — Logical split (~8-9 hours)

1. Create `src/nexus/db/t2/` package with `__init__.py` exposing `T2Database`
2. Move `memory` table schema + methods (`put`, `get`, `search`, `list_entries`, `delete`, `expire`, `find_overlapping_memories`, `merge_memories`, `flag_stale_memories`) to `memory_store.py`
3. Move `plans` table schema + methods (`save_plan`, `search_plans`, `list_plans`) to `plan_library.py`
4. **Migrate `taxonomy.py` into `catalog_taxonomy.py`**. The existing `taxonomy.py` functions (`get_topics`, `get_topic_tree`, `get_topic_docs`, `assign_topic`, `cluster_and_persist`, `rebuild_taxonomy`) directly access `db._lock` and `db.conn.execute()`. Leaving them as a separate module defeats the split's purpose — Phase 2's separate-connection benefit cannot apply while taxonomy reaches through into the monolithic lock. Move both schema ownership AND the query functions into `catalog_taxonomy.py`. Add explicit deprecation shim: `taxonomy.py` re-exports from `catalog_taxonomy` for backward compat. Remove the shim in the first PR after Phase 2 is merged (concrete, not calendar-based).
5. Move `topics` + `topic_assignments` schema to `catalog_taxonomy.py` (combined with step 4)
6. Move `relevance_log` schema + methods (`log_relevance`, `log_relevance_batch`, `get_relevance_log`, `expire_relevance_log`) to `telemetry.py`
7. Move `_migrate_*` functions to their respective domain modules
8. Keep `T2Database` as a facade in `t2/__init__.py` delegating to composed stores
9. Shim: keep `from nexus.db.t2 import T2Database` working
10. Update tests where they patch internal `_init_schema` or `_migrate_*`

**Phase 1 prerequisites (must resolve before starting)**:
- Resolve the `get_topic_docs()` JOIN defect (see "Known Defect" section). Either decide the JOIN semantics upfront or explicitly track it as a follow-up bead. Doing the refactor without deciding risks the split enshrining the broken JOIN.

**Phase 1 test strategy**:

The characterization test (`tests/test_t2_facade.py`) is a **smoke-test**, not the behavioral contract. The real contract is the existing 153 tests across 8 files — they must all pass unchanged (except those patching internals). `test_t2_facade.py` exists only to catch obvious facade wiring bugs (e.g., a method not delegated) before the full suite runs. Do not promote it to "the contract" — that would provide false confidence.

First bead of Phase 1: create `tests/test_t2_facade.py` smoke-test. Exercise one method from each domain through the facade and assert return values are non-error. The assertion is "did the delegation happen?" not "did behavior match pre-split exactly."

Baseline test inventory (captured 2026-04-10 via `grep -rl "T2Database\|taxonomy\." tests/`):

Primary T2Database exercises:
- `tests/test_t2.py` — memory CRUD, expire, FTS, access tracking, relevance log, migration guard
- `tests/test_memory_consolidation.py` — find_overlapping, merge_memories, flag_stale, MCP tool
- `tests/test_taxonomy.py` — topics schema, cluster_and_persist, get_topic_docs, rebuild, CLI
- `tests/test_mcp_server.py` — T2 patched as mcp.core._t2_ctx
- `tests/test_relevance_log.py` — log_relevance + batch + expire
- `tests/test_scratch.py` — promote() calls T2.put
- `tests/test_catalog_e2e.py` — catalog store_put hook writes to T2 catalog
- `tests/test_rdr052_verification.py` — catalog routing via T2

Taxonomy-specific:
- `tests/test_taxonomy.py` — all taxonomy.py functions
- `tests/test_memory_consolidation.py` — shares T2 with taxonomy via find_overlapping
- `tests/test_t2.py::test_expire_*` — T2.expire() interacts with taxonomy tables only via the shared schema

Process:
1. Create `tests/test_t2_facade.py` with the characterization tests before touching any source
2. Run the full suite: `uv run pytest -m 'not integration' -q` — record pass count baseline
3. Execute Phase 1 refactor
4. Re-run the same command — diff must be zero failures
5. Any test failure during the refactor is a regression, not a "patching internals" update

Key structlog events to preserve during refactor (tests/monitoring may key off them):
- `expire_complete` (memory_deleted, relevance_log_deleted, relevance_log_error)
- `embedding_fetch_failed` / `embedding_fetch_shape_mismatch` (collection, requested)
- `contradiction_check` (collections, results, pairs_checked, flagged)
- `expire_relevance_log_failed`
- `relevance_log_store_failed`
- `t1_access_count_update_failed` (id)
- `catalog_prefilter_applied` (paths)

Add new events but preserve field names on existing ones.

### Phase 2 — Separate connections (~4 hours)

1. Each store opens its own `sqlite3.Connection` to the shared DB file
2. Each store has its own `threading.Lock`
3. Concurrency tests in `tests/test_t2_concurrency.py` verify memory reads aren't blocked by telemetry writes under load
4. Document the WAL + multi-connection pattern in `docs/contributing.md`

**Phase 2 incompatibility: `:memory:` databases.** SQLite `:memory:` databases are not shared across connections — each `sqlite3.connect(":memory:")` creates a distinct in-memory database. Phase 2's multi-connection model breaks any test or caller using `T2Database(path=":memory:")`. Mitigation: either (a) disallow `:memory:` in Phase 2 and migrate test fixtures to `tmp_path / "t2.db"`, or (b) use `file::memory:?cache=shared` URI mode. Decide before starting Phase 2. Current tests use tmp_path, so impact is low.

**Phase 2 prerequisite**: Phase 1 step 4 (migrate `taxonomy.py` into `catalog_taxonomy.py`) must be complete. Without it, taxonomy queries still reach through `db._lock` and Phase 2's separate-connection benefit for taxonomy cannot be realized.

### Phase 3 — Physical file split (deferred)

Not committed in this RDR. Revisit if Phase 2 load testing shows lock contention persists at the file level.

## Success Criteria

- [ ] Phase 1: `t2.py` < 50 LOC (facade only); each domain module < 400 LOC
- [ ] Phase 1: All existing tests pass unchanged except those patching internals
- [ ] Phase 1: New coupling between domains is visible as explicit `import`
- [ ] Phase 2: Concurrent memory reads + telemetry writes benchmark shows no serialization penalty
- [ ] Phase 2: `cluster_and_persist` does not block `memory_search` for its duration
- [ ] Documentation: `docs/architecture.md` updated to show the new `nexus.db.t2` structure

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Facade adds indirection overhead | Measured: < 1µs per call for Python attribute access. Noise vs. actual query cost. |
| Test fixtures break | Phase 1 preserves `T2Database(path)` constructor. Tests using private methods will need updates, but public API is stable. |
| Multi-connection SQLite locks become a new bug source | Phase 2 includes concurrency tests. WAL mode is production-tested for multi-writer. |
| Scope creep into Phase 3 | Explicitly deferred. Phase 3 requires its own RDR if pursued. |
| Topics/taxonomy coupling to memory.title fragile | Already fragile pre-split — Phase 1 makes it explicit via `from .memory_store import MemoryStore` in `catalog_taxonomy.py`. Visibility is the fix. |
| Concurrent RDR-061 E6 (memory consolidation) work conflicts with step 2 | `find_overlapping_memories` and `merge_memories` are both moving to `memory_store.py`. If RDR-061 E6 follow-up work is in flight during the refactor window, coordinate branch strategy before cutting the Phase 1 bead to avoid merge conflicts on those methods. |

## Open Questions

1. **Should `plan_library` live under `memory_store`?** Both are per-project, both are agent-facing. The domains are distinct (plans vs. memory) but the owner is similar. For Phase 1, keep them separate to clarify boundaries. Revisit if it proves artificial.

2. **Should the facade expose stores as attributes (`db.memory.put(...)`) or methods (`db.put(...)`)?** For backward compat, keep methods. New callers can reach through via `db.memory` if they want to hold a reference.

3. **Migration guard per domain or per file?** **Resolved**: use per-domain guard keys of the form `(path, domain_name)`. Each domain module owns its own guard set (`memory_store._migrated_paths`, `plan_library._migrated_paths`, etc.) so adding a new migration to one domain doesn't trigger re-probing of unrelated domains. The Phase 1 implementation must preserve the current race-free single-lock semantics per domain.

## Related RDRs

- **RDR-058** (Pipeline orchestration + plan library): Created the `plans` table — the first non-memory tenant of T2
- **RDR-057** (Progressive formalization): Added access tracking, heat-weighted expiry, consolidation helpers — expanded `memory`'s surface area
- **RDR-061** (Literature-grounded enhancement): Added `topics`, `topic_assignments`, `relevance_log` — the moment T2 became multi-domain
- **RDR-062** (MCP interface tiering): Split the MCP server into focused surfaces; this RDR applies the same principle to the T2 layer underneath
