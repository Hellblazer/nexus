---
title: "Eliminate Global Singletons in nexus.catalog and nexus.mcp_infra"
id: RDR-118
type: Architecture
status: draft
priority: high
author: Hellblazer
reviewed-by: self
created: 2026-05-19
accepted_date:
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

`src/nexus/mcp_infra.py` declares a parallel, independent singleton plus three global hook collections:

- `_catalog_instance: Catalog | None = None` and `_catalog_mtime: float` at module load. `get_catalog()` lazy-initialises on first call; subsequent calls return the cached instance, refreshing via `_max_jsonl_mtime` checks.
- `_post_store_hooks: list = []` and `_post_store_batch_hooks: list = []` accept registrations via `register_post_store_hook()` and `register_post_store_batch_hook()`. Hooks self-register at module import.
- `_post_store_batch_hooks_with_catalog_doc_id: set = set()` classifies which hooks expect the catalog_doc_id argument; populated by id() of the registered hook function.

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

#### Gap 4: Seven autouse conftest fixtures resetting singletons

`tests/conftest.py` lines 150 to 380 define seven `autouse=True` fixtures whose sole purpose is resetting the state above on every test boundary:

| Fixture | Lines | Resets |
| --- | --- | --- |
| `_restore_structlog_config_each_test` | 130 to 147 | `structlog` global config |
| `_restore_post_store_batch_hooks_after_test` | 150 to 199 | `_post_store_hooks`, `_post_store_batch_hooks`, `_post_store_batch_hooks_with_catalog_doc_id`, `_catalog_instance`, `_cached` (via reset_cache) |
| `_isolate_dispatch_routing` | 202 to 241 | 7 env vars |
| `_pin_storage_mode_direct_for_tests` | 244 to 267 | `NX_STORAGE_MODE` |
| `_isolate_t1_sessions` | 270 to 285 | `NEXUS_SKIP_T1` |
| `_auto_migrate_t2_in_tests` | 288 to 321 | wraps `T2Database.__init__` to re-introduce migration that was severed in nexus-uqqy |
| `_isolate_config_dir` | 324 to 362 | `NEXUS_CONFIG_DIR` |
| `_isolate_catalog` | 365 to 380 | `NEXUS_CATALOG_PATH` |

Each fixture docstring cites a past incident (orphan owners, leaked sockets, dropped hooks, escaped subprocess writes) that motivated its addition. They are not paranoid: they exist because real test pollution happened. The autouse pressure adds 250 lines of conftest boilerplate that future contributors must understand, and every new test inherits the cost.

## Context

### Background

Discovery sequence (this session, nexus-w1ip):

1. User complaint about test count and singleton tangle.
2. Phase 1A parametrize collapse on `test_plugin_structure.py` (679 to 45) and `tests/commands/test_help_completeness.py` (402 to 4) saved 1032 tests with zero coverage change. Sweep landed at 8074. Shipped on PR #864.
3. Investigation of remaining bloat showed 423 files with average 19 tests each. The 67 thinnest files only hold 218 tests; bloat is broadly distributed.
4. Inspection of test_aspect_*, test_plan_*, test_t1/t2/t3*, test_catalog* file families showed each file pins specific feature surface or specific RDR-phase decisions. Naive merging loses regression protection.
5. Inspection of conftest revealed the seven autouse fixtures, each documenting a past incident. The singletons they reset are the root cause; the user's "tangled mess" complaint is symptomatic.
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
- `tests/conftest.py` (698 lines, 7 autouse fixtures) read end-to-end. Each fixture docstring cites motivating incident.
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
| `pytest` autouse fixture cost | Documented | Autouse runs per test; 7 fixtures times ~8000 tests is ~56k fixture executions per CI run. Real overhead but not the primary motivator. |

### Key Discoveries

- **Verified**: `_cached` and `_catalog_instance` are independent singletons; clearing one does not clear the other (conftest must clear both manually). Source-confirmed at `catalog/__init__.py:44` and `mcp_infra.py:28`.
- **Verified**: `reset_cache()` is invoked from outside `nexus.catalog` only by tests. No production caller resets the cache. Source search across `src/`.
- **Verified**: `_inject_catalog`/`_inject_t3` precedent exists in `mcp_server.py`. Used by `test_rdr052_verification.py` but the pattern was never extended beyond that one module.
- **Documented**: The conftest fixtures' docstrings name specific incidents (nexus-mrmq escaped subprocess writes, nexus-dc57/ze2a leaked chroma children, RDR-060 orphan owners). These are real failure modes, not theoretical.
- **Assumed**: A runtime-container injection pattern across ~140 call sites can ship in phases without breaking direct-mode production CLI users mid-refactor. Needs Phase 1 spike to validate the migration shape.

### Critical Assumptions

- [ ] Phased migration is feasible without a flag day, that is, intermediate states with mixed runtime-container and module-global accessors must compile and pass tests. Status: Unverified. Method: Phase 1 spike.
- [ ] Removing the autouse fixtures after singleton elimination does not surface new test failures from other shared state (T3 EphemeralClient process sharing, structlog config, etc.). Status: Unverified. Method: Run full suite after Phase 4 with fixtures removed.
- [ ] Test count drops materially (target: 8074 to roughly 3000 or below) because tests can construct one runtime per class instead of monkeypatching per test. Status: Unverified. Method: Sample ten high-fixture test files; rewrite against the new container; measure test count delta.
- [ ] The dual-singleton (`_cached` plus `_catalog_instance`) is historical accident, not a designed split with hidden semantics. Status: Unverified. Method: Source-search the git history for the introduction of each; confirm no documented rationale for keeping them separate.

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

**HookRegistry**:

```text
class HookRegistry:
    def register(self, fn: PostStoreHook, *, batch: bool = False) -> None: ...
    def fire(self, doc_id: str, collection: str, content: str) -> None: ...
    def fire_batch(self, records: list[...]) -> None: ...
```

Replaces the three module-level lists plus the id()-keyed classification set.

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
2. **Per-test isolation by construction**: each test gets its own runtime; no shared mutable state between tests; the 7 autouse fixtures become unnecessary.
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

- **Positive**: 7 autouse fixtures deletable; 250 lines of conftest gone; per-test isolation becomes constructor-level not fixture-level.
- **Positive**: Future tests do not need to learn the monkeypatch dance; they construct a runtime and pass it.
- **Positive**: CLAUDE.md architectural rule honored; new contributors find injection patterns, not service locators.
- **Positive**: Enables the user's <3000 test target by removing structural reasons for fixture-heavy test code.
- **Negative**: ~140 call sites must change. Multi-phase work, not a single PR.
- **Negative**: Intermediate phases have a mix of runtime-container and legacy accessors. Code reviewers must hold both patterns in their head until Phase 4 lands.
- **Negative**: ContextVar default-lookup is a hidden control-flow path. A test that forgets to set the runtime gets a clear error but the failure is at access time, not at test collection time.

### Risks and Mitigations

- **Risk**: A subsystem outside the audited modules (worker threads, asyncio handlers, background daemons) holds a stale runtime reference after the test that constructed it has exited.
  **Mitigation**: `NexusRuntime.close()` actively closes its T2Client and clears its catalog cache; long-lived references see closed handles and raise. Spike during Phase 1 to confirm no daemon code path holds a runtime reference past its construction context.

- **Risk**: ContextVar defaults do not propagate across `loop.run_in_executor` calls; an executor thread may see no runtime set.
  **Mitigation**: Audit executor usage during Phase 1. Anywhere a runtime is needed in a background thread, capture it at thread-spawn and pass explicitly. Document the pattern.

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

### Minimum Viable Validation

After Phase 1 ships, a single high-fixture test file (sample: `test_aspect_extractor.py`, 73 tests, monkeypatch-heavy) is rewritten to use the runtime fixture instead of monkeypatch. The rewritten file must:

1. Pass with the seven autouse fixtures still in place.
2. Pass with the seven autouse fixtures locally disabled via a marker.
3. Show a clear LOC reduction in the rewritten test file (no monkeypatch setenv calls).

If MVV passes, the pattern works; Phases 2 to 4 are mechanical extensions. If MVV fails, the runtime API needs revision before scaling out.

### Phase 1: Code Implementation

#### Step 1: Introduce `nexus.runtime.NexusRuntime` and `HookRegistry`

New module `src/nexus/runtime.py`. Defines the runtime container, the ContextVar machinery, and the public `current_runtime()` lookup. Adds `pytest` fixture `nexus_runtime` in `tests/conftest.py` that constructs a fresh runtime per test class (yield) and closes it on teardown.

#### Step 2: Migrate `nexus.catalog` accessors

`open_cached`, `open_catalog`, `_get_t2_client`, `reset_cache` become methods on `NexusRuntime`. Module-level `_cached`, `_t2_client`, `_cache_lock`, `_t2_client_lock` deleted. Module-level functions kept as thin redirectors that call `current_runtime().get_catalog(...)` for back-compat through Phase 4.

#### Step 3: Run MVV (rewrite `test_aspect_extractor.py`)

Rewrite one test file against the new runtime fixture. Confirm pass with and without the legacy autouse `_isolate_catalog` fixture. Measure LOC delta and test count delta.

### Phase 2: Post-store hooks migration

#### Step 1: Move `_post_store_hooks`, `_post_store_batch_hooks`, `_post_store_batch_hooks_with_catalog_doc_id` to `HookRegistry`

`register_post_store_hook`, `register_post_store_batch_hook`, `fire_post_store_hooks`, `fire_post_store_batch_hooks` become methods on the registry. Self-registering hooks (chash dual-write, taxonomy assign, manifest write) move to `default_hooks(runtime)` factory.

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
- **Scenario**: After Phase 4 completion, count collected tests. **Verify**: Suite below 3000 collected tests, with no skipped or xfail bloat.

## Validation

### Testing Strategy

1. **Scenario**: Phase 1 MVV. Rewrite `test_aspect_extractor.py` against the runtime fixture.
   **Expected**: All 73 tests pass; file LOC reduces; no `monkeypatch.setenv` calls remain.
2. **Scenario**: Phase 2 hook migration. Replace `register_post_store_batch_hook` global with `runtime.hooks.register`.
   **Expected**: Existing hook firing paths unchanged; `nexus-bdag` self-registration moved to factory.
3. **Scenario**: Phase 3 env consolidation. CLI dispatch reads from runtime, not env.
   **Expected**: `NEXUS_CONFIG_DIR=/tmp/x nx search ...` still routes correctly via CLI entry point resolution; tests construct runtime directly.
4. **Scenario**: Phase 4 full-suite run with seven fixtures deleted.
   **Expected**: Suite passes; collected count below 3000.

### Performance Expectations

ContextVar lookups are O(1) and cheap. The current code does dict lookups against `_cached` on every `open_cached` call; the new code does the same lookup against `runtime._cached`. No performance change expected. If the post-store hook firing path becomes a hot spot (it iterates a list either way), measure during Phase 2 review.

## Finalization Gate

> Complete each item with a written response before
> marking this RDR as **Accepted**.

### Contradiction Check

To be completed during `/nx:rdr-gate`.

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
- `tests/conftest.py` lines 130 to 380 (seven autouse fixtures, each docstring cites motivating incident).
- `src/nexus/mcp_server.py` (existing `_inject_catalog`/`_inject_t3` precedent).
- PR #864 (nexus-w1ip Phase 1A): test count 9106 to 8074 via parametrize collapse.
- Bead nexus-o5ck (this RDR's tracking bead).
- Bead nexus-wuerf (Phase 2/3 follow-up cuts, blocked on this RDR).

## Revision History

(Gate findings will be appended here.)
