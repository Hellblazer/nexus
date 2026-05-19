---
title: "Eliminate Global Singletons in nexus.catalog and nexus.mcp_infra"
id: RDR-118
type: Architecture
status: accepted
priority: high
author: Hellblazer
reviewed-by: self
created: 2026-05-19
accepted_date: 2026-05-19
related_issues: [nexus-o5ck, nexus-w1ip, nexus-wuerf]
---

# RDR-118: Eliminate Global Singletons in nexus.catalog and nexus.mcp_infra

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The test suite has grown to 9106 collected tests, of which the user describes 6100+ as bloat. The headline complaint is not raw count: "its impossible to keep the singletons cleared and the whole thing is tangled mess." Investigation traced the symptom to source-side global mutable state in two modules that violates the project's stated architectural ground rules.

`CLAUDE.md` is explicit: "Composition over inheritance ... Constructor injection, no global singletons, no service locators." The catalog and mcp_infra modules currently fail that contract. The test-suite mess is downstream of that failure.

Phase 1A of the test-reduction sweep (nexus-w1ip, PR #864) cut 1032 tests via parametrize collapse with zero coverage change, landing the suite at 8074. Further cuts toward the user's <3000 target require either deleting real regression coverage or removing the singleton state that forces the test isolation gymnastics. This RDR proposes the latter.

### Enumerated gaps to close

#### Gap 1: nexus.catalog process-level mutable singletons

`src/nexus/catalog/__init__.py` declares two module-level mutables that cache constructed Catalog instances and a T2Client across the whole process:

- `_cached: dict[(Path, str), Catalog]` keyed by catalog path and storage mode, guarded by `_cache_lock`. Holds Catalog instances indefinitely for the lifetime of the process.
- `_t2_client: Any = None` guarded by `_t2_client_lock`. Holds a T2Client socket pool singleton, reused across every daemon-mode Catalog opened in this process.

Public `open_cached(cat_path)` and `_get_t2_client()` read from these caches. `reset_cache()` is the only way to drop them, and the conftest invokes it on every test setup and teardown because each test wants a fresh tmp_path catalog that does not collide with the previous test's cached instance.

A reader cannot tell from `open_cached(some_path)` whether they get a brand-new Catalog or one constructed thirty tests ago. Tests that mutate the catalog through one path and observe through another silently share state. The conftest's `_isolate_catalog` fixture documents the original incident: "64 orphan int-cce-* curator owners accumulated from test_cce_query_retrieves_cce_indexed_markdown alone" before isolation was added.

#### Gap 2: nexus.mcp_infra._catalog_instance and post-store hook lists

`src/nexus/mcp_infra.py` declares a parallel, independent singleton plus three global hook collections at three different document-granularity tiers:

- `_catalog_instance: Catalog | None = None` and `_catalog_mtime: float` at module load (line 28). `get_catalog()` lazy-initialises on first call; subsequent calls return the cached instance, refreshing via `_max_jsonl_mtime` checks.
- **Chain 1 (single-doc, MCP-only)**: `_post_store_hooks: list = []` at line 469 with `register_post_store_hook` / `fire_post_store_hooks`. Signature `fn(doc_id, collection, content)`. Fires once per MCP `store_put`.
- **Chain 2 (batch, CLI ingest)**: `_post_store_batch_hooks: list = []` at line 544 + `_post_store_batch_hooks_with_catalog_doc_id: set = set()` at line 552 with `register_post_store_batch_hook` / `fire_post_store_batch_hooks`. Signature `fn(doc_ids, collection, contents, embeddings, metadatas[, *, catalog_doc_id])`. Fires once per CLI chunk batch. Three load-bearing self-registrations at module load: `chash_dual_write_batch_hook`, `taxonomy_assign_batch_hook`, `manifest_write_batch_hook`.
- **Chain 3 (document-grain, both paths)**: `_post_document_hooks: list = []` at line 1011 + `_post_document_hooks_with_doc_id: set = set()` at line 1019 with `register_post_document_hook` / `fire_post_document_hooks`. Signature `fn(source_path, collection, content)`. Fires once per document from BOTH MCP `store_put` AND every CLI ingest path. RDR-089 aspect extraction is the canonical consumer. Synchronous-only contract; async callables are silently unsupported.

The `_catalog_instance` global is independent of `nexus.catalog._cached`. A test that monkeypatches `NEXUS_CATALOG_PATH` clears `_cached` (via the `_isolate_catalog` fixture) but the `_catalog_instance` retains its reference to the prior tmp_path. The conftest's `_restore_post_store_batch_hooks_after_test` fixture documents the consequence: "the first test that initialises the singleton pins it to its own tmp_path, so subsequent tests' manifest writes target the wrong (deleted) tmp catalog and the assertion `cat.get_manifest(tumbler)` returns []".

The hook registration lists are also process-global. A test that clears a hook to assert isolation permanently removes the load-bearing registration unless the fixture snapshots and restores. The classification set has an additional Python id() reuse hazard documented in the fixture.

#### Gap 3: Env-driven config read at every call site

Configuration is communicated via process environment variables read at every accessor invocation, not injected. The conftest strips or pins seven such vars on every test:

- `NEXUS_CONFIG_DIR` (read by `nexus.config.nexus_config_dir`)
- `NEXUS_CATALOG_PATH` (read by Catalog accessors)
- `NX_STORAGE_MODE` (read by `nexus.db.is_daemon_mode`, pinned to `direct` for tests)
- `NEXUS_DISPATCH_BACKEND`, `NEXUS_DISPATCH_QWEN_OPERATORS`, `NEXUS_DISPATCH_CLAUDE_OPERATORS` (dispatch router)
- `NEXUS_ASPECT_BACKEND`, `NEXUS_SCHOLARLY_PAPER_VERSION` (aspect extractor)
- `NEXUS_TIER_B_DISPATCHER`, `QWEN_AGENT_SUPERVISOR` (qwen agent transport)
- `NEXUS_SKIP_T1` / `NX_T1_ISOLATED` (T1 discovery)

228 of 435 test files (52%) use `monkeypatch` to manage these. Per the project's TDD rule the test files are not the problem; the env-as-config pattern forces each test to manage process-global state because the source has nowhere else to put it.

#### Gap 4: Eight autouse conftest fixtures resetting singletons

`tests/conftest.py` lines 128 to 380 define eight `autouse=True` fixtures whose sole purpose is resetting the state above on every test boundary:

| Fixture | Lines | Resets | RDR-118 disposition |
| --- | --- | --- | --- |
| `_restore_structlog_after_test` | 128 to 147 | `structlog` global config | STAYS (orthogonal) |
| `_restore_post_store_batch_hooks_after_test` | 150 to 199 | `_post_store_hooks`, `_post_store_batch_hooks`, `_post_store_batch_hooks_with_catalog_doc_id`, `_catalog_instance`, `_cached` (via reset_cache) | DELETED in Phase 2 |
| `_isolate_dispatch_routing` | 202 to 241 | 7 env vars | DELETED in Phase 3 |
| `_pin_storage_mode_direct_for_tests` | 244 to 267 | `NX_STORAGE_MODE` | DELETED in Phase 3 |
| `_isolate_t1_sessions` | 270 to 285 | `NEXUS_SKIP_T1` | DELETED in Phase 3 |
| `_auto_migrate_t2_in_tests` | 288 to 321 | wraps `T2Database.__init__` to re-introduce migration severed in nexus-uqqy | DELETED in Phase 4 (after absorbing `run_if_needed` into runtime T2 construction) |
| `_isolate_config_dir` | 324 to 362 | `NEXUS_CONFIG_DIR` | DELETED in Phase 3 |
| `_isolate_catalog` | 365 to 380 | `NEXUS_CATALOG_PATH` | DELETED in Phase 3 |

Each fixture docstring cites a past incident (orphan owners, leaked sockets, dropped hooks, escaped subprocess writes) that motivated its addition. They are not paranoid: they exist because real test pollution happened. The autouse pressure adds ~250 lines of conftest boilerplate that future contributors must understand, and every new test inherits the cost. RDR-118 eliminates 7 of the 8 fixtures by removing the source-side state they exist to reset.

## Context

### Background

Discovery sequence (this session, nexus-w1ip):

1. User complaint about test count and singleton tangle.
2. Phase 1A parametrize collapse on `test_plugin_structure.py` (679 to 45) and `tests/commands/test_help_completeness.py` (402 to 4) saved 1032 tests with zero coverage change. Sweep landed at 8074. Shipped on PR #864.
3. Investigation of remaining bloat showed 423 files with average 19 tests each. The 67 thinnest files only hold 218 tests; bloat is broadly distributed.
4. Inspection of test_aspect_*, test_plan_*, test_t1/t2/t3*, test_catalog* file families showed each file pins specific feature surface or specific RDR-phase decisions. Naive merging loses regression protection.
5. Inspection of conftest revealed the eight autouse fixtures, each documenting a past incident. The singletons they reset are the root cause; the user's "tangled mess" complaint is symptomatic.
6. Source inspection via Serena confirmed two independent singletons (`nexus.catalog._cached` and `nexus.mcp_infra._catalog_instance`) and three global hook collections.

The arithmetic is straightforward. To drop from 8074 to below 3000 by deletion alone, ~63% of remaining tests must go. The architecture fix is the only path that does not destroy regression coverage; remove the singletons and the autouse fixtures disappear, which removes the structural reason tests need 250 lines of isolation ceremony.

### Technical Environment

- Python 3.12+, single-process CLI plus optional T2/T3 daemons (RDR-112).
- SQLite WAL for T2; ChromaDB EphemeralClient / PersistentClient / CloudClient for T3.
- ~140 call sites import from `nexus.catalog` or call `mcp_infra.get_catalog()` across ~25 modules. Top callers: `commands/catalog.py` (47 references), `mcp_infra.py` itself (20), `catalog/__init__.py` (11), `commands/enrich.py` (7), `commands/search_cmd.py` (6).
- Existing partial injection precedent: `_inject_catalog`, `_inject_t3`, `_reset_singletons` already exist in `mcp_server.py` and are used by `test_rdr052_verification.py`. Pattern is incomplete: it injects into the mcp_server module only, not catalog/__init__.

## Research Findings

### Investigation

Source files read:
- `src/nexus/catalog/__init__.py` (218 lines) confirmed `_cached`, `_cache_lock`, `_t2_client`, `_t2_client_lock`, `open_catalog`, `open_cached`, `reset_cache`.
- `src/nexus/mcp_infra.py` confirmed `_catalog_instance`, `_catalog_mtime`, `_post_store_hooks`, `_post_store_batch_hooks`, `_post_store_batch_hooks_with_catalog_doc_id`, `register_post_store_hook`, `register_post_store_batch_hook`, `fire_post_store_hooks`, `fire_post_store_batch_hooks`, `get_catalog`.
- `tests/conftest.py` (698 lines, 8 autouse fixtures at lines 128, 150, 202, 244, 270, 288, 324, 365) read end-to-end. Each fixture docstring cites motivating incident.
- `src/nexus/mcp_server.py` sample showed existing `_inject_catalog` / `_inject_t3` / `_reset_singletons` precedent for one module.

Call-site survey via grep across `src/nexus/`:
- `open_cached | reset_cache | _catalog_instance | get_catalog()` appears in 25 modules, top 5 listed above.
- `register_post_store_batch_hook` self-registrations happen in `nexus.mcp_infra` module-load context (RDR-108 Phase 3 nexus-bdag).

Test-side survey:
- 9106 collected pre-cut, 8074 post Phase 1A.
- 228 of 435 test files use `monkeypatch` (51%).
- 83 files use `assert_called`, `called_with`, or `call_count` (mock-coupling pattern).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| python `threading.Lock` | Yes | `Lock()` is reentrant-unsafe; current pattern uses double-checked locking correctly. No change needed. |
| `chromadb.EphemeralClient` | Project memory only | Shared-process-state warning noted (project_chromadb_ephemeral_shared_state.md). Means T3 isolation also needs container-level coordination. |
| `pytest` autouse fixture cost | Documented | Autouse runs per test; 8 fixtures times ~8000 tests is ~64k fixture executions per CI run. Real overhead but not the primary motivator. |

### Key Discoveries

- **Verified**: `_cached` and `_catalog_instance` are independent singletons; clearing one does not clear the other (conftest must clear both manually). Source-confirmed at `catalog/__init__.py:44` and `mcp_infra.py:28`.
- **Verified**: `reset_cache()` is invoked from outside `nexus.catalog` only by tests. No production caller resets the cache. Source search across `src/`.
- **Verified**: `_inject_catalog`/`_inject_t3` precedent exists in `mcp_server.py`. Used by `test_rdr052_verification.py` but the pattern was never extended beyond that one module.
- **Documented**: The conftest fixtures' docstrings name specific incidents (nexus-mrmq escaped subprocess writes, nexus-dc57/ze2a leaked chroma children, RDR-060 orphan owners). These are real failure modes, not theoretical.
- **Assumed**: A runtime-container injection pattern across ~140 call sites can ship in phases without breaking direct-mode production CLI users mid-refactor. Needs Phase 1 spike to validate the migration shape.

### Critical Assumptions

- [x] **A1: Phased migration is feasible without a flag day.** Status: Verified (revised scope). Method: Source Search (codebase-deep-analyzer dispatch, 2026-05-19). FINDING: Phase 1 CANNOT ship catalog-only. Bidirectional call-time coupling at `catalog/__init__.py:112` (lazy `from nexus.mcp_infra import t2_ctx`) and `mcp_infra.py:914` (`manifest_write_batch_hook` calls `get_catalog()`) forces a minimum mcp_infra subset into Phase 1. See § Revised Phase 1 Boundary below. T2 record: `nexus_rdr/118-research-1-a1-phasing`.
- [ ] **A2: Removing autouse fixtures after the migration surfaces no new failures.** Status: Partially Verified. Method: Source inspection of the 8 conftest autouse fixtures (recount during accept gate, 2026-05-19). FINDING: 7 of 8 fixtures are deletable across the four phases. Phase 2 deletes 1 (`_restore_post_store_batch_hooks_after_test`). Phase 3 deletes 5 env-config fixtures (`_isolate_config_dir`, `_isolate_catalog`, `_isolate_dispatch_routing`, `_pin_storage_mode_direct_for_tests`, `_isolate_t1_sessions`). Phase 4 deletes 1 (`_auto_migrate_t2_in_tests` — by absorbing `run_if_needed` into the runtime's T2 construction path). Only `_restore_structlog_after_test` stays permanently (structlog is its own global, orthogonal to RDR-118). Forward-looking residual: post-Phase-4 full-suite run is the final verification.
- [ ] **A3: Test count drops materially.** Status: Likely Verified (with revised optimism). Method: MVV target pivot from `test_aspect_extractor.py` (env-config, Phase 3 territory) to `tests/test_post_store_hook.py` (16 tests, 28 singleton-coupling instances, redundant local `_reset_hooks` autouse). API sketch confirmed feasibility (T2 record: `nexus_rdr/118-research-3-a3-mvv`). REVISED LANDING: singleton fix alone takes 8074 → ~5500; reaching <3000 requires additional sweeps (closed-RDR pin deletion, slow-tier marker for integration tests, hotspot parametrize consolidation).
- [x] **A4: The dual-singleton is historical accident.** Status: Verified. Method: Source Search (git history via deep-research-synthesizer dispatch, 2026-05-19). FINDING: `_catalog_instance` introduced 2026-04-08 commit 14c300d7 (mcp_server.py extraction, no bead/RDR). `_cached` introduced 2026-05-02 commit 5c0e303b PR #487 bead nexus-6xqk (SQLite write-lock storm fix). 24 days apart, separate motivations, no cross-reference. Safe to collapse. TWO SEMANTIC PRESERVATIONS REQUIRED: (a) `get_catalog()`'s mtime-refresh logic (mcp_infra.py:385-394, cross-process consistency for direct mode) must be absorbed by `runtime.get_catalog()` or explicitly retired with documented rationale; (b) `(cat_path, mode)` keying must be preserved (test isolation requirement). T2 record: `nexus_rdr/118-research-2-a4-singleton-origin`.

## Proposed Solution

### Approach

Introduce a `NexusRuntime` container that owns the previously-global state, then thread it through the call paths via explicit injection. The container is constructed once per CLI invocation (or once per test class) and torn down deterministically. Module-level globals are removed.

Concretely:

- `NexusRuntime` holds: the catalog instance(s) keyed by path, the optional T2Client socket pool, the post-store hook lists, the dispatch-mode setting, the config-dir path, the T1 isolation flag.
- Public accessors (`open_cached`, `get_catalog`, `register_post_store_hook`) become methods on the runtime, not module functions.
- The CLI entry point constructs the runtime from environment plus flags, then passes it to command implementations. Tests construct it directly with explicit kwargs.
- Module-level functions stay as thin redirectors during the migration, accepting an explicit `runtime` argument that defaults to a context-local lookup. The default lookup uses `contextvars.ContextVar` so concurrent tests in the same process do not alias, and the lookup raises with a clear error in a context that has no runtime set.

The post-store hooks system migrates from `register_post_store_hook(fn)` global registration to `runtime.hooks.register(fn)` or constructor-time injection. The module-load self-registrations (chash dual-write, taxonomy assign, manifest write) move to a `default_hooks(runtime)` factory that the CLI entry point calls.

Env-driven config consolidates into the runtime's constructor. `NEXUS_CONFIG_DIR`, `NEXUS_CATALOG_PATH`, `NX_STORAGE_MODE`, dispatch routing, aspect backend, and T1 isolation are read once at runtime construction. Code that previously called `nexus.config.nexus_config_dir()` calls `runtime.config_dir`. Tests that need a specific config-dir pass it to the constructor; they do not monkeypatch the env.

### Technical Design

**`NexusRuntime` shape** (illustrative, signatures only):

```text
class NexusRuntime:
    # Constructed once per CLI invocation or per test class
    def __init__(
        self,
        *,
        config_dir: Path,
        catalog_path: Path | None = None,
        storage_mode: Literal["direct", "daemon"] = "direct",
        dispatch_backend: str | None = None,
        # ... explicit kwargs for every config that was previously env-driven
    ) -> None: ...

    # Catalog accessors (replace nexus.catalog.open_cached / open_catalog)
    def get_catalog(self, cat_path: Path | None = None) -> Catalog: ...
    def fresh_catalog(self, cat_path: Path) -> Catalog: ...

    # Hook registry (replace nexus.mcp_infra.register_post_store_*)
    @property
    def hooks(self) -> HookRegistry: ...

    # Lifecycle
    def close(self) -> None: ...
    def __enter__(self) -> NexusRuntime: ...
    def __exit__(self, *exc) -> None: ...
```

**Default-lookup mechanism**:

```text
_runtime_var: ContextVar[NexusRuntime | None] = ContextVar("nexus_runtime", default=None)

def current_runtime() -> NexusRuntime:
    rt = _runtime_var.get()
    if rt is None:
        raise RuntimeError(
            "No NexusRuntime in current context. "
            "CLI entry points must construct one; tests must use the runtime fixture."
        )
    return rt
```

Existing module functions keep their public signature for one cycle but read `current_runtime()` internally. New call sites prefer explicit `runtime` argument passing.

**HookRegistry** (covers all three chains, not just two):

```text
class HookRegistry:
    # Chain 1: single-doc (MCP only)
    def register_single(self, fn: SingleHook) -> None: ...
    def fire_single(self, doc_id: str, collection: str, content: str) -> None: ...

    # Chain 2: batch (CLI ingest)
    def register_batch(
        self, fn: BatchHook, *, with_catalog_doc_id: bool = False,
    ) -> None: ...
    def fire_batch(self, records: list[...]) -> None: ...

    # Chain 3: document-grain (both paths) — synchronous-only contract
    def register_document(
        self, fn: DocumentHook, *, with_doc_id: bool = False,
    ) -> None: ...
    def fire_document(
        self, source_path: str, collection: str, content: str,
    ) -> None: ...
```

Replaces the six module-level mutables (three lists + three id()-keyed classification sets) across `_post_store_hooks`, `_post_store_batch_hooks`, `_post_store_batch_hooks_with_catalog_doc_id`, `_post_document_hooks`, `_post_document_hooks_with_doc_id`. Per-chain failure isolation + T2 `hook_failures` persistence semantics preserved verbatim from the existing dispatchers.

**Migration shape**: each phase touches one subsystem at a time and leaves the others operating against the legacy module-global accessors.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `NexusRuntime` | `nexus.mcp_server._inject_*` | Extend: generalize the inject-per-test precedent from `mcp_server.py` into the full runtime container. |
| `HookRegistry` | `nexus.mcp_infra._post_store_hooks*` | Replace: move hook collections into the runtime; delete the module-global lists. |
| Catalog accessors | `nexus.catalog._cached`, `_t2_client`, `open_cached`, `open_catalog` | Replace: `open_cached` becomes `runtime.get_catalog`; `_cached` becomes a runtime instance attribute. `reset_cache` becomes `runtime.close()`. |
| Config-dir resolution | `nexus.config.nexus_config_dir` | Extend: resolution stays but the return value is cached on the runtime, not read at every call. |
| Storage-mode resolution | `nexus.db.is_daemon_mode` | Extend: function stays; reads `runtime.storage_mode` instead of `os.environ`. |

### Decision Rationale

A runtime container provides three things the current pattern does not:

1. **Explicit lifecycle**: construction and `close()` are called by the entry points. Tests construct per class; CLI constructs per invocation. No more implicit module-load initialisation.
2. **Per-test isolation by construction**: each test gets its own runtime; no shared mutable state between tests; 7 of 8 autouse fixtures become unnecessary across the four phases (1 in Phase 2, 5 in Phase 3, 1 in Phase 4). Only `_restore_structlog_after_test` stays — structlog is its own global, orthogonal to RDR-118.
3. **CLAUDE.md compliance**: "Constructor injection, no global singletons, no service locators" is the project's stated rule. This brings the source into compliance.

The migration cost is real (~140 call sites, ~25 modules) but bounded. The phased approach keeps each phase independently shippable and revertable.

## Alternatives Considered

### Alternative 1: Thread-local state (`threading.local()` or `contextvars` only)

**Description**: Keep the module-level accessors but back them with `threading.local()` or `ContextVar` so each thread or async context gets its own state.

**Pros**:
- Smaller change footprint; no call-site rewrites required.
- Tests can `_runtime_var.set(new_runtime)` to isolate.

**Cons**:
- Does not move toward constructor injection; the singleton is still implicit, just per-context.
- Existing fixtures must still exist to call `.set()` and `.reset()` correctly; the conftest gymnastics remain.
- Does not address the dual-singleton problem (`_cached` plus `_catalog_instance` would still need separate reset paths).
- Concurrent tests in async code paths still collide on shared mutable state if a single ContextVar is used carelessly.

**Reason for rejection**: Solves none of the user's complaints. The visible mess (conftest fixtures, monkeypatch usage, mysterious inter-test pollution) stays.

### Alternative 2: Pass the Catalog explicitly to every call site, no runtime container

**Description**: Change every function signature that currently calls `open_cached` to take an explicit `Catalog` argument. No container; just functional dependency injection.

**Pros**:
- Most explicit possible API.
- Type checker enforces correctness.

**Cons**:
- Noisy: 140+ call sites each grow a parameter. Adds significant churn to function signatures across the codebase.
- Does not solve the post-store hooks problem (those need a registry, not a Catalog).
- Does not solve the env-driven config problem.

**Reason for rejection**: Right idea, wrong scope. A single runtime parameter that carries Catalog plus hooks plus config is the same change with one parameter instead of three or four.

### Alternative 3: Status quo plus better test fixtures

**Description**: Keep the singletons; improve the conftest fixtures so isolation is more reliable and tests can opt out of autouse where they need to.

**Pros**:
- Zero source change.
- Existing tests keep working.

**Cons**:
- Does not solve the architectural complaint.
- Does not enable the test count reduction (each new test still inherits the autouse cost).
- User's verbatim complaint ("impossible to keep singletons cleared") goes unaddressed.

**Reason for rejection**: User explicitly called out the singleton tangle as the problem.

### Briefly Rejected

- **Module-level metaclass tricks** (e.g., make the singleton path-keyed via `__class_getitem__`): too magical, harder to read than the current code.
- **Async context manager only** (no plain class): forces callers to use `async with`, breaks the synchronous CLI surface.

## Trade-offs

### Consequences

- **Positive**: 7 of 8 autouse fixtures deletable across the four phases (1 in Phase 2 + 5 in Phase 3 + 1 in Phase 4); ~230 lines of conftest gone; per-test isolation becomes constructor-level not fixture-level. Only `_restore_structlog_after_test` stays — orthogonal concern.
- **Positive**: Future tests do not need to learn the monkeypatch dance; they construct a runtime and pass it.
- **Positive**: CLAUDE.md architectural rule honored; new contributors find injection patterns, not service locators.
- **Positive**: Enables the user's <3000 test target by removing structural reasons for fixture-heavy test code.
- **Negative**: ~140 call sites must change. Multi-phase work, not a single PR.
- **Negative**: Intermediate phases have a mix of runtime-container and legacy accessors. Code reviewers must hold both patterns in their head until Phase 4 lands.
- **Negative**: ContextVar default-lookup is a hidden control-flow path. A test that forgets to set the runtime gets a clear error but the failure is at access time, not at test collection time.

### Risks and Mitigations

- **Risk** (HIGH, from A1 research): `src/nexus/mcp/core.py:29` `get_catalog as _get_catalog` is a TOP-LEVEL alias frozen at import time. Phase 1 shim only resolves correctly if `mcp/core.py` is imported AFTER the shim is installed.
  **Mitigation**: Daemon restart required after Phase 1 deploys; document explicitly in the Phase 1 PR description. Alternatively, change the import to a function reference that resolves at call time.

- **Risk** (HIGH, from A1 research): `mcp_infra.py:983-985` self-registers 3 batch hooks at module load. If Phase 1 moves the registry but `register_post_store_batch_hook` still appends to the legacy list, `manifest_write` + `chash_dual_write` + `taxonomy_assign` silently drop on all CLI ingest until the process is restarted with the correct wiring.
  **Mitigation**: Move the 3 self-registrations into `install_default_hooks(runtime)` factory called by the CLI entry point AFTER runtime construction. Tests that need the default hooks call the factory explicitly; tests that don't (most of them) get a clean registry by default.

- **Risk** (MEDIUM, active blocker, from A1 research): bead `nexus-qw21` unfixes `t2_ctx()` under `NX_STORAGE_MODE=daemon`. `_record_hook_failure` (mcp_infra.py:521) and `_record_batch_hook_failure` (line 676) both call `t2_ctx()`, both raise `RuntimeError`, masking the original hook exception.
  **Mitigation**: Close `nexus-qw21` before Phase 1 ships, OR fold its fix into Phase 1 Step 5.

- **Risk**: A subsystem outside the audited modules (worker threads, asyncio handlers, background daemons) holds a stale runtime reference after the test that constructed it has exited.
  **Mitigation**: `NexusRuntime.close()` actively closes its T2Client and clears its catalog cache; long-lived references see closed handles and raise. Spike during Phase 1 to confirm no daemon code path holds a runtime reference past its construction context.

- **Risk**: ContextVar defaults do not propagate across `loop.run_in_executor` calls when using non-default executors; an executor thread may see no runtime set. (Default executor inherits context as of Python 3.7+.)
  **Mitigation**: Audit executor usage during Phase 1. Anywhere a runtime is needed in a background thread, capture it at thread-spawn and pass explicitly. Document the pattern. `src/nexus/daemon/t2_daemon.py` and `src/nexus/commands/taxonomy_cmd.py` are known `run_in_executor` call sites.

- **Risk**: A reviewer or future contributor reinstates a module-global singleton "for convenience."
  **Mitigation**: Add a lint check (ruff custom rule, or a grep test in `tests/test_singleton_freeze.py`) that fails CI on new module-level mutables in `nexus.catalog` and `nexus.mcp_infra`. Pin the rule in the RDR-118 close.

### Failure Modes

- **Visible**: A test forgets to install a runtime in its context; the first accessor call raises `RuntimeError("No NexusRuntime in current context")`. Easy to diagnose.
- **Silent**: A long-lived background worker captures a stale runtime; subsequent operations through that worker target the previous test's tmp_path. Mitigation: `runtime.close()` invalidates handles; capture audit during Phase 1.
- **Recovery**: Roll back to the legacy module-global accessor by reverting the phase commit; the module-level functions still exist as thin redirectors during Phase 1 to 3.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (Phase 1 spike covers them).
- [ ] PR #864 (nexus-w1ip Phase 1A) merged so the test-count baseline is 8074.
- [ ] `nexus-qw21` closed, OR its `t2_ctx()` daemon-mode fix is folded into Phase 1 Step 5. Without one of these, `_record_hook_failure` / `_record_batch_hook_failure` raise `RuntimeError` under daemon mode and mask hook exceptions — implementers would misattribute the failure to the migration itself.

### Minimum Viable Validation

After Phase 1 ships, `tests/test_post_store_hook.py` (16 tests, 28 singleton-coupling instances, contains a redundant local `_reset_hooks` autouse fixture) is rewritten to use the runtime fixture. The rewritten file must:

1. Pass with the legacy autouse fixtures still in place.
2. Pass with the legacy autouse fixtures locally disabled via a marker.
3. Show a clear LOC reduction (the file's own `_reset_hooks` autouse, ~18 lines, deletes entirely).

**MVV target pivot (research finding 2026-05-19):** Original draft named `test_aspect_extractor.py` (73 tests). Research found that file's monkeypatch usage is for `NEXUS_ASPECT_BACKEND` and `NEXUS_SCHOLARLY_PAPER_VERSION` env vars — Phase 3 (env-config) MVV territory, not Phase 1 (catalog/hooks). The pivoted target directly exercises `register_post_store_hook` / `fire_post_store_hooks` / `register_post_store_batch_hook` against the module globals, which is exactly the Phase 1/2 surface.

If MVV passes, Phases 2 to 4 are mechanical extensions. If MVV fails, the runtime API needs revision before scaling out.

### Revised Phase 1 Boundary (research finding 2026-05-19)

A1 research surfaced that Phase 1 cannot ship catalog-only. The bidirectional call-time coupling at `catalog/__init__.py:112` (lazy `from nexus.mcp_infra import t2_ctx`) plus `mcp_infra.py:914` (`manifest_write_batch_hook` calls `get_catalog()`) forces a minimum mcp_infra subset into Phase 1, otherwise the manifest hook writes against a different catalog instance than the indexer reads (silent data divergence).

**Phase 1 MUST include:**

- `nexus.runtime.NexusRuntime` + `HookRegistry` + `ContextVar` machinery (new module).
- `nexus.catalog` accessors: `open_cached`, `open_catalog`, `_get_t2_client`, `reset_cache` move to `NexusRuntime`.
- `nexus.mcp_infra.get_catalog()` becomes a 1-line shim delegating to `runtime.catalog`.
- `_post_store_batch_hooks` list + `_post_store_batch_hooks_with_catalog_doc_id` set move to `HookRegistry` (because the 3 self-registering hooks at `mcp_infra.py:983-985` run at module load and must populate the same registry that `fire_post_store_batch_hooks` reads).
- `install_default_hooks(runtime)` factory function: extracts the 3 module-load self-registrations into an explicit factory called by the CLI entry point AFTER runtime construction.

**Phase 1 MAY DEFER to Phase 2:**

- `mcp_infra._post_store_hooks` single-doc chain (MCP-only, lower frequency, no correctness dependency vs catalog).
- `mcp_infra._catalog_instance` / `_catalog_mtime` mtime-refresh logic — remains as a compatibility path through the shim. **A4 preservation S1:** the mtime-refresh logic (mcp_infra.py:385-394) must be absorbed by `runtime.get_catalog()` or explicitly retired with documented rationale before Phase 2 closes; silent removal breaks direct-mode clients that rely on cross-process refresh.

**A4 preservation S2:** Runtime's catalog cache MUST adopt `(cat_path, mode)` keying (not global single-instance). Test isolation relies on per-path keying.

### Phase 1: Code Implementation

#### Step 1: Introduce `nexus.runtime.NexusRuntime` and `HookRegistry`

New module `src/nexus/runtime.py`. Defines the runtime container, the ContextVar machinery, and the public `current_runtime()` lookup. Adds `pytest` fixture `runtime` in `tests/conftest.py` that constructs a fresh runtime per test class (yield) and closes it on teardown.

#### Step 2: Migrate `nexus.catalog` accessors

`open_cached`, `open_catalog`, `_get_t2_client`, `reset_cache` become methods on `NexusRuntime`. Module-level `_cached`, `_t2_client`, `_cache_lock`, `_t2_client_lock` deleted. Module-level functions kept as thin redirectors that call `current_runtime().get_catalog(...)` for back-compat through Phase 4.

#### Step 3: Migrate `mcp_infra.get_catalog` + batch-hook registry

`get_catalog` becomes a shim: `return current_runtime().catalog`. `_post_store_batch_hooks` and `_post_store_batch_hooks_with_catalog_doc_id` move to `HookRegistry`. `register_post_store_batch_hook` becomes shim. Self-registering hooks (chash dual-write, taxonomy assign, manifest write) move from module-load to `install_default_hooks(runtime)` factory called by CLI entry.

#### Step 4: Run MVV (rewrite `tests/test_post_store_hook.py`)

Rewrite the file against the new runtime fixture. Confirm pass with and without the legacy autouse fixtures. Measure LOC delta (target: -18 LOC for the local `_reset_hooks` autouse deletion).

#### Step 5: Resolve nexus-qw21 blocker

Active bead `nexus-qw21` (`t2_ctx()` unconditionally passes `_path_resolver` under daemon mode → raises `RuntimeError`) breaks `_record_hook_failure` and `_record_batch_hook_failure` in daemon mode. Either close `nexus-qw21` before Phase 1 ships, or fold its fix into Phase 1.

### Phase 2: Single-doc hooks + document-chain + mtime-refresh resolution

#### Step 0 (gate condition): Decide and document the mtime-refresh disposition

Before deleting `_catalog_instance`, the A4 preservation S1 must be resolved: either absorb the mtime-refresh logic (mcp_infra.py:385-394, cross-process JSONL re-`_ensure_consistent` on advance) into `runtime.get_catalog()`, or explicitly retire it under daemon-mode with documented rationale. Silent removal breaks direct-mode clients that rely on cross-process refresh. This decision gates Phase 2 closure.

#### Step 1: Move `_post_store_hooks` (single-doc chain) to `HookRegistry`

`register_post_store_hook`, `fire_post_store_hooks` migrate to `registry.register_single` / `registry.fire_single`. Deferred from Phase 1 because the single-doc chain has no correctness coupling with catalog.

#### Step 1b: Move `_post_document_hooks` (document-grain chain) to `HookRegistry`

`register_post_document_hook`, `fire_post_document_hooks`, `_post_document_hooks_with_doc_id` migrate to `registry.register_document` / `registry.fire_document`. RDR-089 aspect-extraction self-registration moves to `install_default_hooks` factory alongside the batch-chain registrations from Phase 1.

#### Step 2: Delete `_restore_post_store_batch_hooks_after_test` autouse fixture

After Phase 2 Step 1 lands, the fixture has nothing to restore. Delete it. Run full suite. Any test that fails reveals an inadvertent global reach into the legacy hook list; fix at the test site.

### Phase 3: Env-driven config migration

#### Step 1: Consolidate env reading into `NexusRuntime.__init__`

Currently scattered `os.environ.get("NEXUS_CONFIG_DIR")` calls and similar move into a single resolution at runtime construction. The runtime exposes `config_dir`, `catalog_path`, `storage_mode`, `dispatch_backend`, `aspect_backend`, etc. as attributes.

#### Step 2: Delete `_isolate_config_dir`, `_isolate_catalog`, `_isolate_dispatch_routing`, `_pin_storage_mode_direct_for_tests`, `_isolate_t1_sessions`

These five fixtures all monkeypatch env vars. After Phase 3 Step 1, tests pass kwargs to `NexusRuntime(...)` instead of setting env. Delete the fixtures. Audit remaining tests; rewrite the few that genuinely need env (the `nx doctor` shakeout tests) to set env in the test body.

### Phase 4: T2 migration auto-init revisit

`_auto_migrate_t2_in_tests` exists because RDR-112 P0.4 severed migration from `T2Database.__init__`. The runtime can call `run_if_needed(path)` during T2 construction, so the fixture is no longer needed. Delete the fixture; verify the daemon's startup migration path is independent.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `NexusRuntime` instances | N/A (process-scoped) | `runtime.repr()` shows config | `runtime.close()` | `runtime.is_closed` | N/A (state lives in T2/T3) |
| `HookRegistry` | `runtime.hooks.list()` | Per-hook signature inspect | `runtime.hooks.clear()` | N/A | N/A |

### New Dependencies

None. ContextVar is in the stdlib.

## Test Plan

- **Scenario**: A single test class constructs `NexusRuntime(config_dir=tmp_path/".config")` and runs ten tests inside it. **Verify**: Each test sees its own runtime; no autouse fixture is required for catalog isolation.
- **Scenario**: After Phase 2, delete `_restore_post_store_batch_hooks_after_test` and run the full suite. **Verify**: No failures attributable to leaked hook state.
- **Scenario**: After Phase 3, delete the five env-isolation fixtures and run the full suite. **Verify**: No failures attributable to env leak.
- **Scenario**: Lint test that no new module-level mutables appear in `nexus.catalog` or `nexus.mcp_infra`. **Verify**: AST scan returns empty.
- **Scenario**: After Phase 4 completion, count collected tests. **Verify**: Suite drops materially. Honest target (A3 research finding): 8074 → ~7700 from RDR-118 phases alone; ~5500 combined with `nexus-wuerf` sweeps (closed-RDR pin deletion, slow-tier integration marker, hotspot parametrize consolidation). Reaching the user's <3000 ceiling requires both efforts.

## Validation

### Testing Strategy

1. **Scenario**: Phase 1 MVV. Rewrite `tests/test_post_store_hook.py` against the runtime fixture (the research-pivoted target — directly exercises the hook registry surface).
   **Expected**: All 16 tests pass; local `_reset_hooks` autouse (~18 LOC) deletes entirely; pass with and without legacy autouse `_restore_post_store_batch_hooks_after_test`.
2. **Scenario**: Phase 2 single-doc + document-chain hook migration. Move `_post_store_hooks` and `_post_document_hooks` to `HookRegistry`.
   **Expected**: Existing hook firing paths unchanged; `nexus-bdag` (chash dual-write) and RDR-089 aspect extraction (document chain) self-registrations moved to `install_default_hooks` factory.
3. **Scenario**: Phase 3 env consolidation. CLI dispatch reads from runtime, not env.
   **Expected**: `NEXUS_CONFIG_DIR=/tmp/x nx search ...` still routes correctly via CLI entry point resolution; tests construct runtime directly.
4. **Scenario**: Phase 4 full-suite run with five fixtures deleted (catalog/hook/env-config). Two fixtures (`_restore_structlog_config_each_test`, `_auto_migrate_t2_in_tests`) intentionally remain.
   **Expected**: Suite passes; collected count drops to ~7700 from RDR-118 phases alone. Reaching <3000 requires nexus-wuerf sweeps in addition.

(A `test_aspect_extractor.py` rewrite remains a Phase 3 validation candidate because its monkeypatch usage is for `NEXUS_ASPECT_BACKEND` env vars, but it is not the Phase 1 MVV.)

### Performance Expectations

ContextVar lookups are O(1) and cheap. The current code does dict lookups against `_cached` on every `open_cached` call; the new code does the same lookup against `runtime._cached`. No performance change expected. If the post-store hook firing path becomes a hot spot (it iterates a list either way), measure during Phase 2 review.

## Finalization Gate

> Complete each item with a written response before
> marking this RDR as **Accepted**.

### Contradiction Check

No contradictions found between research findings, design principles, and proposed solution after gate-round-1 in-place fixes. Specifically:

- A4's mtime-refresh preservation (mcp_infra.py:385-394) is now a gate condition on Phase 2 closure (Phase 2 Step 0), not an implicit hope.
- The MVV target (`test_post_store_hook.py`) is consistent between §Minimum Viable Validation and §Validation Testing Strategy Scenario 1 after fix.
- The HookRegistry design covers all three hook chains (`_post_store_hooks` + `_post_store_batch_hooks` + `_post_document_hooks`) — the original draft only covered two; gate-round-1 added the document chain.
- RDR-112 storage-mode semantics are preserved via the A4 S2 `(cat_path, mode)` keying requirement on the runtime cache.
- RDR-062's `_catalog_instance` design intent is respected: the collapse is explicitly justified by A4 evidence that the two singletons accreted independently 24 days apart with no documented unified design.

### Assumption Verification

To be completed during `/nx:rdr-research` and `/nx:rdr-gate`. The four Critical Assumptions above are each addressed by the Phase 1 spike (MVV) before any of Phases 2 to 4 ship.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `contextvars.ContextVar.set`, `.get`, `.reset` | python stdlib | Source Search (stdlib reference) |
| `threading.Lock` (existing pattern) | python stdlib | Source Search (already in use) |

### Scope Verification

MVV is in scope and not deferred: Phase 1 Step 3 rewrites `test_aspect_extractor.py` and gates further phases on that single file's success.

### Cross-Cutting Concerns

- **Versioning**: No public API breakage. Module-level functions retain signatures through Phase 4; new runtime API ships alongside.
- **Build tool compatibility**: N/A (stdlib only).
- **Licensing**: N/A.
- **Deployment model**: CLI entry points construct one runtime per invocation. The daemon process constructs its own. No new deployment surface.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Phased migration is the entire approach.
- **Secret/credential lifecycle**: Voyage / Chroma keys move into `NexusRuntime.credentials`; current resolution from `os.environ` continues to work at runtime construction time.
- **Memory management**: One `Catalog` per `(cat_path, mode)` per runtime, same as today. `runtime.close()` releases. No new retention.

### Proportionality

The RDR is large for the change but the change touches ~140 call sites and is the architectural keystone for the user's <3000 test target. The size is justified. Sections that may trim before locking: Alternatives 3 (status quo) can compress to one paragraph if reviewer agrees the rejection is obvious.

## References

- `CLAUDE.md` (project): "Composition over inheritance ... Constructor injection, no global singletons, no service locators."
- `src/nexus/catalog/__init__.py` lines 44 to 192 (singleton declarations and lifecycle).
- `src/nexus/mcp_infra.py` lines 28 to 395 (catalog instance, post-store hooks).
- `tests/conftest.py` (eight autouse fixtures at lines 128, 150, 202, 244, 270, 288, 324, 365; each docstring cites motivating incident).
- `src/nexus/mcp_server.py` (existing `_inject_catalog`/`_inject_t3` precedent).
- PR #864 (nexus-w1ip Phase 1A): test count 9106 to 8074 via parametrize collapse.
- Bead nexus-o5ck (this RDR's tracking bead).
- Bead nexus-wuerf (Phase 2/3 follow-up cuts, blocked on this RDR).

## Revision History

### 2026-05-19 — Research round 1 (A1, A3, A4 verified; A2 partial)

Three findings recorded in T2 (`nexus_rdr/118-research-{1,2,3}-*`):

1. **A1 (phased migration feasibility)** verified with revised scope. Phase 1 cannot ship catalog-only; minimum mcp_infra subset must move together. Source: codebase-deep-analyzer dispatch enumerated bidirectional call-time coupling at `catalog/__init__.py:112` and `mcp_infra.py:914`. Three new HIGH/MEDIUM risks surfaced (top-level alias freezing, self-registration silent drop, nexus-qw21 blocker). RDR body updated.

2. **A4 (dual-singleton origin)** verified. Source: deep-research-synthesizer git-history dispatch. `_catalog_instance` introduced 2026-04-08 (commit 14c300d7, no bead/RDR — mcp_server.py extraction). `_cached` introduced 2026-05-02 (commit 5c0e303b, PR #487, bead nexus-6xqk — SQLite write-lock storm fix). 24 days apart, independent motivations. Safe to collapse with TWO preservations: mtime-refresh logic (mcp_infra.py:385-394) and `(cat_path, mode)` keying. Both encoded into the Revised Phase 1 Boundary section.

3. **A3 (test count drops materially)** partially verified. MVV target pivoted from `test_aspect_extractor.py` (env-config, Phase 3 territory) to `tests/test_post_store_hook.py` (16 tests, redundant local `_reset_hooks` autouse, direct singleton coupling). API sketch confirms feasibility. REVISED LANDING: singleton fix alone takes 8074 → ~5500. The user's <3000 target needs `nexus-wuerf` sweeps in addition. RDR body's "<3000" claim corrected throughout.

4. **A2 (autouse fixture deletion)** partially verified. Of the 8 conftest autouse fixtures (recount during accept gate), 7 are deletable across the four phases: Phase 2 deletes 1 (post-store-batch), Phase 3 deletes 5 (env-config), Phase 4 deletes 1 (T2 auto-migrate, absorbed into runtime). Only `_restore_structlog_after_test` stays. RDR body's earlier "7 fixtures deletable" and "5 of 7" framings were both off-by-one; corrected to "7 of 8 deletable" throughout.

Forward-looking residual: A2 final verification requires post-Phase-4 full-suite run; A3 final verification requires the actual MVV spike.

### 2026-05-19 — Gate round 1 (PASSED with in-place fixes)

`/nx:rdr-gate RDR-118` ran all three layers:

- **Layer 1 (structural)**: PASS. Four `#### Gap N:` headings present, all required sections present, 3 research findings recorded in T2.
- **Layer 2 (assumption audit)**: PASS. All four Critical Assumptions have explicit status, method, and evidence citations.
- **Layer 3 (substantive critique)**: PASS with 0 Critical, 2 Significant, 3 Observations. Critic verdict: "the problem is real, the research findings are credible and correctly narrow the Phase 1 scope, the ContextVar approach is appropriate, and the phased migration plan is honest about its coupling constraints."

Significant issues fixed in-place during gate round 1:

1. **Third hook chain (`_post_document_hooks`) was missing from the HookRegistry scope.** mcp_infra.py:1011 declares a third independent chain (RDR-089 aspect-extraction consumer, signature `fn(source_path, collection, content)`, fires from both MCP and CLI ingest paths). Original Gap 2 enumerated only two chains. Fixed: Gap 2 now enumerates all three chains by tier; HookRegistry design block now exposes `register_single` / `register_batch` / `register_document` methods covering all six module-level mutables.

2. **MVV target contradiction between §Minimum Viable Validation and §Validation Scenario 1.** §MVV named `test_post_store_hook.py` (the research-pivoted target); §Validation Scenario 1 still named `test_aspect_extractor.py` (the pre-research draft target). Implementers reading §Validation as their acceptance checklist would have rewritten the wrong file. Fixed: §Validation Scenario 1 now names `test_post_store_hook.py`; `test_aspect_extractor.py` moved to a Phase 3 validation note.

Observations addressed:

- **Test count delta**: tightened from "8074 → ~5500 from singleton fix alone" (overstated by ~400) to "8074 → ~7700 from RDR-118 phases alone; ~5500 combined with nexus-wuerf sweeps."
- **nexus-qw21 as formal prerequisite**: added to §Prerequisites as a third checkbox (close OR fold-into-Phase-1).
- **mtime-refresh forcing function**: added Phase 2 Step 0 gate condition requiring the disposition (absorb or retire) to be decided and documented before Phase 2 closes.
- **Stale "7 autouse fixtures" wording**: corrected to "7 of 8 deletable, 1 stays" after the post-accept audit recount caught the off-by-one in the actual conftest fixture count.

Gate outcome: **PASSED**. RDR-118 is ready for `/nx:rdr-accept` once user reviews the gate-round-1 in-place fixes (per project policy: pause between gate and accept).
