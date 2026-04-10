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

1. **Mixed concurrency regimes**: `memory` sees interactive writes (agent actions at human pace). `relevance_log` sees automated writes (one batch per `store_put`, potentially many per second during indexing). They share a single SQLite file and a single `threading.Lock`. A long `find_overlapping_memories` scan blocks `relevance_log` inserts for the duration.

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

**Benefits of Phase 1**:
- Each domain's schema, migrations, and queries live in one file (~150-300 LOC per module instead of 900+ in `t2.py`).
- Tests can instantiate a single store in isolation (e.g., `MemoryStore(conn)` without also setting up plans/topics/telemetry).
- Cross-domain joins become explicit (a file importing two modules signals the coupling).

**Cost of Phase 1**: ~6 hours of mechanical refactoring, no behavior change, no migration.

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

### Phase 1 — Logical split (~6 hours)

1. Create `src/nexus/db/t2/` package with `__init__.py` exposing `T2Database`
2. Move `memory` table schema + methods (`put`, `get`, `search`, `list_entries`, `delete`, `expire`, `find_overlapping_memories`, `merge_memories`, `flag_stale_memories`) to `memory_store.py`
3. Move `plans` table schema + methods (`save_plan`, `search_plans`, `list_plans`) to `plan_library.py`
4. Move `topics` + `topic_assignments` schema + all helper methods to `catalog_taxonomy.py` (note: the topics helpers live in `taxonomy.py` today, not `t2.py` — this phase leaves them there and only moves the schema ownership)
5. Move `relevance_log` schema + methods (`log_relevance`, `log_relevance_batch`, `get_relevance_log`, `expire_relevance_log`) to `telemetry.py`
6. Move `_migrate_*` functions to their respective domain modules
7. Keep `T2Database` as a facade in `t2/__init__.py` delegating to composed stores
8. Shim: keep `from nexus.db.t2 import T2Database` working
9. Update tests where they patch internal `_init_schema` or `_migrate_*`

### Phase 2 — Separate connections (~4 hours)

1. Each store opens its own `sqlite3.Connection` to the shared DB file
2. Each store has its own `threading.Lock`
3. Concurrency tests in `tests/test_t2_concurrency.py` verify memory reads aren't blocked by telemetry writes under load
4. Document the WAL + multi-connection pattern in `docs/contributing.md`

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

## Open Questions

1. **Should `plan_library` live under `memory_store`?** Both are per-project, both are agent-facing. The domains are distinct (plans vs. memory) but the owner is similar. For Phase 1, keep them separate to clarify boundaries. Revisit if it proves artificial.

2. **Should the facade expose stores as attributes (`db.memory.put(...)`) or methods (`db.put(...)`)?** For backward compat, keep methods. New callers can reach through via `db.memory` if they want to hold a reference.

3. **Migration guard per domain or per file?** The RDR-062 follow-up guard (`_migrated_paths`) treats the whole file as one. After Phase 1, each domain should have its own guard key so adding a new migration doesn't re-probe unrelated tables.

## Related RDRs

- **RDR-058** (Pipeline orchestration + plan library): Created the `plans` table — the first non-memory tenant of T2
- **RDR-057** (Progressive formalization): Added access tracking, heat-weighted expiry, consolidation helpers — expanded `memory`'s surface area
- **RDR-061** (Literature-grounded enhancement): Added `topics`, `topic_assignments`, `relevance_log` — the moment T2 became multi-domain
- **RDR-062** (MCP interface tiering): Split the MCP server into focused surfaces; this RDR applies the same principle to the T2 layer underneath
