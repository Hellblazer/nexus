---
title: "Collection Name Resolution by Prefix"
id: RDR-003
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-02-27
accepted_date:
related_issues: []
---

# RDR-003: Collection Name Resolution by Prefix

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Users cannot easily reference their indexed collections by the repo name alone. ChromaDB collection names include a hash suffix (`code__ART-8c2e74c0`), but CLI commands like `nx store list --collection code__ART` silently return empty results instead of resolving the intended collection. The hash is an internal implementation detail that should be transparent to users.

## Context

### Background

When `nx index repo` runs, it computes a repo identity hash (8-char hex) from the **filesystem path** to the main repo root and appends it to the collection name. Specifically, `_repo_identity()` in `registry.py` runs `git rev-parse --git-common-dir` to resolve worktrees to their main repo path, then takes the first 8 hex digits of the SHA-256 of that path string. This makes the hash stable across worktrees of the same repo on one machine, but **not portable across machines** — two different machines will produce different hashes for the same repo. The hash is logged during indexing but not persisted anywhere user-visible.

After indexing completes, the user has no way to know `code__ART-8c2e74c0` without either:
1. Re-running `nx index repo -v` and reading the log
2. Calling `nx store list` (no `--collection` filter) to enumerate all collections
3. Reading the internal registry or ChromaDB directly

This was discovered when running `nx store list --collection code__ART` after indexing the ART repo; the command returned empty results. The actual collection was `code__ART-8c2e74c0`.

### Technical Environment

- Nexus CLI (`nx`), Python, Click
- ChromaDB Cloud for T3 storage
- Collection naming: `{type}__{basename}-{hash8}` (e.g., `code__ART-8c2e74c0`)
- Repo identity computed by `_repo_identity()` in `src/nexus/registry.py`
- Affected commands: `nx store list`, `nx store put`, `nx store get`, `nx search --corpus`, `nx collection` subcommands

## Research Findings

### Investigation

Traced the collection naming through `indexer.py` and `registry.py`. The `_repo_identity()` function returns `(basename, hash8)` where `hash8` is the first 8 hex digits of SHA-256 of the resolved filesystem path. Collections are named `{type}__{basename}-{hash8}`. The `nx store list --collection NAME` command routes through `t3_collection_name(NAME)` (in `corpus.py`) then `T3Database.list_store(col_name)`, which calls `get_collection()` requiring an exact match. There is no bare `get_collection()` call in `commands/store.py` itself. `nx store put --collection NAME` follows the same path and silently creates a new misnamed collection if the name is unresolved.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| ChromaDB `get_collection` | Yes | Requires exact name — no prefix/glob support |
| ChromaDB `list_collections` | Yes | Returns all collections; can filter client-side |

### Key Discoveries

- **Verified** — `get_collection(name)` in ChromaDB requires exact name match (no fuzzy/prefix)
- **Verified** — `list_collections()` returns all collections without filter; prefix filtering must be done client-side
- **Verified** — `_repo_identity()` in `registry.py` returns `(basename, hash8)` where `hash8` is SHA-256 of the filesystem path to the main repo root (not the remote URL)
- **Verified** — The hash suffix is never written to any user-visible config or status file during indexing
- **Verified** — Hash is stable across worktrees of the same repo on one machine (via `git rev-parse --git-common-dir`); definitively not portable across machines (different filesystem paths produce different hashes)

### Critical Assumptions

- [x] `list_collections()` can enumerate all collections to find prefix matches — **Status**: Verified — **Method**: Source Search
- [x] Hash stability — **Status**: Verified — hash is path-based; stable within one machine, not portable across machines. Cross-machine collection sharing is not a supported use case.

## Proposed Solution

### Approach

Add a **prefix resolution layer** to the CLI's collection name handling. When a user provides a collection name that does not exactly match any collection, attempt prefix resolution: list all collections and return the unique match whose name starts with the provided prefix. If zero or multiple matches exist, surface a helpful error.

### Technical Design

Resolution priority for a given `name`:
1. Exact match → use as-is
2. Single prefix match (one collection starts with `name`) → use it, log a note
3. Zero matches → error: "no collection matching `{name}` or `{name}-*`"
4. Multiple prefix matches → error listing all matches (disambiguation required)

The resolution should be a shared utility function in `corpus.py` alongside the existing `resolve_corpus()`, not duplicated across subcommands. For prefix scanning, it must call `_client.list_collections()` directly (returns names only) rather than `T3Database.list_collections()` which makes N+1 HTTP calls (one `count()` per collection).

```text
// Illustrative — verify signatures during implementation
def resolve_collection_name(client: ChromaClient, name: str) -> str:
    """Return exact collection name, resolving by prefix if needed.
    Raises CollectionNotFoundError | AmbiguousCollectionError.
    Uses client.list_collections() directly to avoid N+1 count calls."""
    # 1. Exact match via client.get_collection(name) — if found, return
    # 2. Prefix scan: [c.name for c in client.list_collections() if c.name.startswith(name)]
    # 3. Zero matches → CollectionNotFoundError
    # 4. Multiple matches → AmbiguousCollectionError(matches)
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `resolve_collection_name()` | `src/nexus/corpus.py` — `resolve_corpus()` handles type-prefix (`code` → all `code__*`), not basename+suffix | Add companion function in `corpus.py`; distinct semantic from `resolve_corpus()` |
| `t3_collection_name()` | `src/nexus/corpus.py` — adds `knowledge__` prefix for bare names; no suffix resolution | Call `resolve_collection_name()` after `t3_collection_name()` in `T3Database.list_store()` and `T3Database.put()` |
| `AmbiguousCollectionError` | `src/nexus/errors.py` — `CollectionNotFoundError` exists; `AmbiguousCollectionError` does not | Add `AmbiguousCollectionError` to `errors.py` |
| `nx store list --collection` | `commands/store.py` → `t3_collection_name()` → `T3Database.list_store()` | Fix at `T3Database.list_store()` call site; not in `commands/store.py` directly |
| `nx store put --collection` | `commands/store.py` → `t3_collection_name()` → `T3Database.put()` | Fix at `T3Database.put()` call site; silently creates misnamed collection without fix |
| `nx search --corpus` | `commands/search_cmd.py` — already uses `resolve_corpus()` for type-prefix | Extend `resolve_corpus()` or add post-step for basename+suffix resolution |
| `nx collection list/info/verify` | `commands/collection.py` | Add resolution; these are read-only — safe to resolve by prefix |
| `nx collection delete` | `commands/collection.py` | **Exact names only** — do not apply prefix resolution; require user to supply full name |

### Decision Rationale

Prefix resolution is the minimal change that fixes the UX without breaking any existing behavior (exact matches continue to work unchanged). Since the hash is path-based and not portable across machines, cross-machine collection sharing is not a supported use case regardless of the resolution strategy — this constraint is accepted, not a differentiator between alternatives.

## Alternatives Considered

### Alternative 1: New registry file (`~/.config/nexus/repos.json`)

**Description**: Store `basename → collection_name` mapping in a new local JSON file written at index time.

**Pros**:
- O(1) lookup (no `list_collections()` call)
- Works offline

**Cons**:
- Per-machine state — since the hash is already path-based and not cross-machine portable, this adds no new portability limitation, but it does add a new file to maintain and a new cleanup obligation when repos are deleted or re-indexed
- Extra file to maintain separate from the existing registry

**Reason for rejection**: `RepoRegistry` (see Alternative 2) already stores collection names — adding a second file duplicates existing state. Prefix scan against ChromaDB is cheap for the common case.

### Alternative 2: Query the existing `RepoRegistry`

**Description**: `RepoRegistry` (in `registry.py`) already stores `code_collection` and `docs_collection` per registered repo path in `repos.json`. A lookup by `basename` over the registry is O(n) over registered repos (typically very few) with no ChromaDB call.

**Pros**:
- O(1) for registered repos; no extra ChromaDB round-trip
- Works offline
- No new state to maintain

**Cons**:
- Only covers repos registered via `nx index repo` — collections created by other means (e.g., `nx store put`) are not in the registry
- Does not handle `knowledge__` collections (T3 store entries not tied to indexed repos)
- Adds coupling between the resolution utility and the registry

**Reason for rejection**: The registry covers the primary use case (indexed repos) but leaves gaps for manually-created collections. Prefix scan against ChromaDB is universal and avoids the coverage gap.

### Briefly Rejected

- **Strip hash from collection names entirely**: Would break disambiguation between two repos with the same basename cloned in different locations on the same machine.
- **Expose hash in `nx index repo` output**: Helps discoverability but does not fix the lookup problem at query time.

## Trade-offs

### Consequences

- `nx store list --collection code__ART` and `nx store put --collection code__ART` will work as expected (no hash needed)
- `client.list_collections()` is called on any unresolved name — one HTTP request returning collection names (no count calls); negligible overhead
- Ambiguous prefix errors give users actionable information
- Cross-machine collection portability remains unsupported (hash is path-based by design)

### Risks and Mitigations

- **Risk**: `client.list_collections()` is slow on large ChromaDB instances with many collections
  **Mitigation**: Only invoked when exact match fails; not in the hot path. Uses name-only listing (no count calls) to minimize overhead.

### Failure Modes

- Two repos with the same basename (e.g., `ART`) both indexed → ambiguous prefix → user sees error listing both; must use full name with hash to disambiguate

## Implementation Plan

### Prerequisites

- [x] Hash stability confirmed: path-based, stable within one machine, not cross-machine
- [x] All CLI entry points that accept collection names identified (see Infrastructure Audit)

### Minimum Viable Validation

`nx store list --collection code__ART` and `nx store put --collection code__ART` work correctly after indexing the ART repo, without requiring the hash suffix.

### Phase 1: Core Resolution Utility

#### Step 1: Add `AmbiguousCollectionError` to `src/nexus/errors.py`

Add the new exception class alongside the existing `CollectionNotFoundError`.

#### Step 2: Add `resolve_collection_name()` to `src/nexus/corpus.py`

Implement the 4-case resolution logic (exact → prefix → zero → ambiguous) as a companion to the existing `resolve_corpus()`. Use `client.list_collections()` directly (not `T3Database.list_collections()`) to avoid N+1 count calls.

#### Step 3: Wire into `T3Database.list_store()` and `T3Database.put()`

These are the actual call sites in `src/nexus/db/t3.py` where `get_collection(name)` is called. Apply `resolve_collection_name()` before the `get_collection()` call in both methods. This covers `nx store list`, `nx store put`, and any other command that routes through `T3Database`.

#### Step 4: Wire into `nx search --corpus` and `nx collection list/info/verify`

Extend `resolve_corpus()` in `corpus.py` or add a post-step in `search_cmd.py` to handle basename+suffix resolution for `--corpus` arguments containing `__`. Apply same resolution to `collection.py`'s `list_cmd`, `info_cmd`, and `verify_cmd`.

**`nx collection delete` — exact names only**: Do not apply prefix resolution to `delete_cmd`. Require the user to supply the full resolved name. This prevents accidental deletion via ambiguous prefix match.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| ChromaDB collections | `nx store list` | N/A | `nx store delete` | `nx search` | N/A |

## Test Plan

- **Scenario**: `nx store list --collection code__ART` after indexing → **Verify**: Returns collection contents (resolves to `code__ART-{hash}`)
- **Scenario**: `nx store put - --collection code__ART` after indexing → **Verify**: Stores into `code__ART-{hash}`, not a new `code__ART` collection
- **Scenario**: `nx store list --collection code__NONEXISTENT` (no matching collection) → **Verify**: Error message is `CollectionNotFoundError` (distinct from "No entries in {name}" for an existing empty collection)
- **Scenario**: `nx store list --collection code__ART` against an existing but empty collection → **Verify**: "No entries" message (not an error — collection exists)
- **Scenario**: Two repos with same basename both indexed → `nx store list --collection code__ART` → **Verify**: `AmbiguousCollectionError` listing both candidates
- **Scenario**: `nx collection delete code__ART` (without full hash) → **Verify**: Rejected with "exact name required for delete" — no prefix resolution applied

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and proposed solution.

### Assumption Verification

- Hash stability: Verified. Hash is SHA-256 of the filesystem path (via `git rev-parse --git-common-dir`), stable across worktrees on the same machine, definitively not portable across different machines. Cross-machine collection sharing is not a supported use case — accepted known limitation.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `client.list_collections()` | chromadb | Source Search |
| `client.get_collection(name)` | chromadb | Source Search |

### Scope Verification

Minimum Viable Validation is in scope: `nx store list --collection code__ART` tested against indexed ART repo.

### Cross-Cutting Concerns

- **Versioning**: N/A — no schema change
- **Build tool compatibility**: N/A
- **Licensing**: N/A — internal change
- **Deployment model**: N/A
- **IDE compatibility**: N/A
- **Incremental adoption**: Backward-compatible — exact names continue to work
- **Secret/credential lifecycle**: N/A
- **Memory management**: N/A — one extra API call, no significant memory impact

### Proportionality

Document is appropriately sized for a UX/API convenience feature.

## References

- `src/nexus/registry.py` — `_repo_identity()` (path-based hash, worktree resolution)
- `src/nexus/indexer.py` — collection naming at index time
- `src/nexus/corpus.py` — `resolve_corpus()`, `t3_collection_name()` (existing resolution infrastructure)
- `src/nexus/db/t3.py` — `T3Database.list_store()`, `T3Database.put()` (actual insertion points)
- `src/nexus/errors.py` — `CollectionNotFoundError` (existing); `AmbiguousCollectionError` (to be added)
- `src/nexus/commands/store.py` — `nx store list`, `nx store put`
- `src/nexus/commands/collection.py` — `nx collection` subcommands
- ChromaDB Python client: `client.list_collections()` (names only, no count calls), `client.get_collection()`

## Revision History

### Gate Review (2026-02-27) — BLOCKED

#### Critical — Must Fix Before Re-gate

**C1. Hash is path-based, not remote-URL-based — root cause is wrong.** The RDR states `_repo_identity()` hashes the git remote URL. Confirmed by reading `registry.py`: the hash is SHA-256 of the **filesystem path** to the main repo root (resolved via `git rev-parse --git-common-dir`). Consequences: (1) hash is not stable across machines — definitively false, not merely unverified; (2) the Alternative 1 rejection ("breaks for repos accessed from a new machine") applies equally to the proposed solution; (3) cross-machine collection sharing is not solved by prefix resolution.

**C2. `resolve_corpus()` already exists — infrastructure audit is materially incomplete.** `corpus.py` already contains `resolve_corpus()` (lines 68–82) used by `nx search --corpus`. It handles type-prefix resolution (`code` → all `code__*` collections) but not basename+suffix resolution (`code__ART` → `code__ART-{hash}`). The audit table lists the new utility as "None" without mentioning this existing function. The implementation must extend or companion `resolve_corpus()`, not create a parallel pathway.

**C3. Wrong insertion point — `nx store list` routes through `t3_collection_name()` then `T3Database.list_store()`.** There is no bare `get_collection()` call in `commands/store.py` to replace. The actual fix must go inside `T3Database.list_store()` or at the `t3_collection_name()` call site. Also: `nx store put --collection` has the same resolution failure (silently creates a new misnamed collection) but is not in scope — this must be addressed.

#### Significant — Should Fix Before Re-gate

**S1. Test plan does not distinguish empty-collection from not-found-collection.** Both currently print "No entries in {name}" — the test plan should verify the zero-match case produces a distinct error message.

**S2. Prefix resolution scope for `nx collection delete` is unspecified and dangerous.** Applying resolution to `delete` means a typo could produce an ambiguous-match error rather than a safe "not found." The implementation plan must enumerate which subcommands get resolution (list, info, verify) vs. which require exact names (delete).

**S3. `list_collections()` makes N+1 HTTP calls (count per collection) — not one round-trip.** `T3Database.list_collections()` calls `col.count()` for every collection in a thread pool. For prefix matching, use `self._client.list_collections()` directly (names only, no count calls).

**S4. Hash stability is definitively false for cross-machine use, not "low risk".** Restate as: cross-machine collection portability is not supported by the current path-based hashing scheme. Prefix resolution works correctly within a single machine.

#### Observations — Applied

- O1: `rdr__` collections use the same naming scheme but are out of scope here — noted as a future gap.
- O2: `AmbiguousCollectionError` added to implementation plan (Step 1).
- O3: `RepoRegistry` alternative evaluated and rejected (Alternative 2) — coverage gap for non-indexed collections.

### Re-gate (2026-02-27)

All prior findings (C1, C2, C3, S1–S4, O1–O3) addressed:

- C1 RESOLVED: Background corrected — hash is SHA-256 of filesystem path via `git rev-parse --git-common-dir`; cross-machine portability documented as unsupported
- C2 RESOLVED: Infrastructure audit updated — `resolve_corpus()` and `t3_collection_name()` added; implementation plan targets `corpus.py`
- C3 RESOLVED: Insertion points corrected to `T3Database.list_store()` and `T3Database.put()`; `nx store put` added to scope
- S1 RESOLVED: Test plan now distinguishes `CollectionNotFoundError` from empty-collection "No entries" message
- S2 RESOLVED: `nx collection delete` explicitly excluded from prefix resolution; requires exact name
- S3 RESOLVED: Technical design updated to use `client.list_collections()` directly (name-only, no count calls)
- S4 RESOLVED: Hash stability reframed as verified known limitation, not "low risk unverified assumption"
