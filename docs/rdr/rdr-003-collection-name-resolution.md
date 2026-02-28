---
title: "Collection Name Resolution by Prefix"
id: RDR-003
type: Feature
status: superseded
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-02-27
accepted_date:
close_date: 2026-02-27
close_reason: superseded
superseded_by: RDR-004
related_issues: []
---

# RDR-003: Collection Name Resolution by Prefix

> **SUPERSEDED** by [RDR-004](rdr-004-four-store-architecture.md) (2026-02-27).
> After 5 gate rounds the design was still BLOCKED. Root cause: prefix resolution
> in a single mixed-purpose store creates interlocking exception-handling and
> contract-preservation constraints that resist clean solution. RDR-004 adopts a
> three-store architecture (code / docs / rdr) that eliminates the problem class.

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
- Affected commands: `nx store list`, `nx store put`, `nx memory promote`, `nx search --corpus`, `nx collection` subcommands

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

Resolution semantics differ by operation type:

**Read-path** (`list_store`, `nx collection list/info/verify`, `nx search --corpus`):
1. Exact match → use as-is
2. Single prefix match → use it, log a note
3. Zero matches → `CollectionNotFoundError`
4. Multiple prefix matches → `AmbiguousCollectionError` listing candidates

**Write-path** (`put()`, `nx memory promote`):
1. Exact match → use as-is
2. Single prefix match → use the resolved name (write into the existing collection)
3. Zero matches → fall through; `get_or_create_collection()` creates it with the original name (preserves current creation behavior)
4. Multiple prefix matches → `AmbiguousCollectionError` (user must disambiguate before writing)

The resolution should be implemented as two functions in `corpus.py` alongside the existing `resolve_corpus()`, not duplicated across subcommands. Both accept a `names: list[str]` of collection names, where `names` is fetched by the caller **lazily** — only when an exact match via `_client.get_collection()` fails. This avoids a `list_collections()` round-trip when the user already supplies a fully-qualified name (the common case for programmatic callers), while still resolving prefixes for user-supplied partial names. Note: `self._client.list_collections()` returns objects with a `.name` attribute; callers must normalize via `[c.name for c in self._client.list_collections()]` before passing to these functions.

```text
// Illustrative — verify signatures during implementation
def resolve_collection_name(names: list[str], name: str) -> str:
    """Read-path resolution: exact → prefix → error on zero matches.
    Caller fetches names via self._client.list_collections() and passes the list.
    Raises CollectionNotFoundError | AmbiguousCollectionError."""
    # 1. Exact match: name in names — return as-is
    # 2. Prefix scan: [n for n in names if n.startswith(name)]
    # 3. Zero matches → CollectionNotFoundError
    # 4. Multiple matches → AmbiguousCollectionError(matches)

def resolve_collection_name_for_write(names: list[str], name: str) -> str:
    """Write-path resolution: exact → prefix → original name on zero matches.
    Caller fetches names via self._client.list_collections() and passes the list.
    Raises AmbiguousCollectionError only. Never raises CollectionNotFoundError."""
    # 1. Exact match: name in names — return as-is
    # 2. Single prefix match — return resolved name
    # 3. Zero matches — return original name (let get_or_create_collection handle it)
    # 4. Multiple matches → AmbiguousCollectionError(matches)
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `resolve_collection_name()` | `src/nexus/corpus.py` — `resolve_corpus()` handles type-prefix (`code` → all `code__*`), not basename+suffix | Add companion function in `corpus.py`; takes `names: list[str]` (not `ChromaClient`) — no ChromaDB import in `corpus.py`; callers in `T3Database` fetch names lazily on miss |
| `t3_collection_name()` | `src/nexus/corpus.py` — adds `knowledge__` prefix for bare names; no suffix resolution | Call `resolve_collection_name()` after `t3_collection_name()` in `T3Database.list_store()` and `T3Database.put()` |
| `AmbiguousCollectionError` | `src/nexus/errors.py` — `CollectionNotFoundError` exists; `AmbiguousCollectionError` does not | Add `AmbiguousCollectionError` to `errors.py` |
| `nx store list --collection` | `commands/store.py` → `t3_collection_name()` → `T3Database.list_store()` | Fix at `T3Database.list_store()` call site; not in `commands/store.py` directly |
| `nx store put --collection` | `commands/store.py` → `t3_collection_name()` → `T3Database.put()` → `get_or_create_collection()` | Fix at `T3Database.put()` using write-path semantics; **resolution must be the first operation**, before `doc_id` computation |
| `nx memory promote --collection` | `commands/memory.py` `promote_cmd` → `T3Database.put()` directly (no `t3_collection_name()`) | Same write-path fix as `T3Database.put()`; covered automatically by Step 3 |
| `nx search --corpus` | `commands/search_cmd.py` — already uses `resolve_corpus()` for type-prefix; `resolve_corpus()` takes `all_collections: list[str]` | Use already-available `all_collections` param in `resolve_corpus()` exact-match branch — no T3Database call inside `corpus.py`, no changes to `search_cmd.py` |
| `nx collection list/verify` | `commands/collection.py` | Add read-path resolution at command layer; these are read-only — safe to error on zero matches |
| `nx collection info` | `commands/collection.py:38` → `T3Database.collection_info()` | Fix at `T3Database.collection_info()` level (consistent with T3Database-layer strategy). **Also**: `info_cmd` currently calls `get_or_create_collection()` — must be changed to `get_collection()` to avoid write side-effect on a read-only command |
| `nx collection delete` | `commands/collection.py` | **Exact names only** — do not apply prefix resolution; require user to supply full name |
| `upsert_chunks(collection, ...)` | `db/t3.py` — called from `pm.py:389`, indexer pipeline | Always receives fully-qualified names from internal code — no user input reaches this method. **No resolution needed.** |
| `upsert_chunks_with_embeddings(collection_name, ...)` | `db/t3.py` — called from `indexer.py:299,444,522`, `doc_indexer.py:204` | Always receives fully-qualified names generated by `_safe_collection()` / `_repo_identity()`. **No resolution needed.** |
| `update_chunks(collection, ...)` | `db/t3.py` — frecency reindex path | Always receives fully-qualified names from internal pipeline. **No resolution needed.** |
| `delete_by_source(collection_name, ...)` | `db/t3.py` — indexer incremental update | Always receives fully-qualified names from indexer. **No resolution needed.** |
| `collection_info(name)` | `db/t3.py` — called from `collection.py:38` | User-facing (via `nx collection info`); add read-path resolution inside `T3Database.collection_info()` — consistent with T3Database-layer strategy |
| `collection_exists(name)` | `db/t3.py` — called from `pm.py:457` | Called with fully-qualified names from PM search path — not user-facing. **No resolution needed.** |
| `nx collection expire` | `commands/collection.py` | **Out of scope** for this RDR |

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
- `client.list_collections()` is called only when exact match fails (lazy evaluation) — zero extra HTTP overhead for fully-qualified names; one extra round-trip for prefix resolution
- Ambiguous prefix errors give users actionable information
- Cross-machine collection portability remains unsupported (hash is path-based by design)
- `rdr__` collections (created by `_rdr_collection_name()` in `registry.py`) have the same hash-suffix scheme and the same UX problem — excluded from this RDR's scope as a future gap

### Risks and Mitigations

- **Risk**: `client.list_collections()` is slow on large ChromaDB instances with many collections
  **Mitigation**: Lazy evaluation ensures the call is only made when `get_collection()` fails (i.e., the name is not fully qualified). Programmatic callers that always supply fully-qualified names incur no overhead. Uses name-only listing (no count calls) to minimize the payload.

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

#### Step 2: Add `resolve_collection_name()` and `resolve_collection_name_for_write()` to `src/nexus/corpus.py`

Implement both functions as companions to the existing `resolve_corpus()`. Both accept a `names: list[str]` parameter (pre-fetched collection names); no ChromaDB import is added to `corpus.py`. This is consistent with `resolve_corpus()`'s calling convention — `corpus.py` stays free of direct ChromaDB client dependencies. Read-path raises `CollectionNotFoundError` on zero matches; write-path returns the original name on zero matches (preserving `get_or_create_collection()` semantics). Exact-match check is `name in names` — no extra round-trip.

#### Step 3: Wire into `T3Database.list_store()`, `T3Database.put()`, and `T3Database.collection_info()`

These are the actual call sites in `src/nexus/db/t3.py`. All three use the same **lazy evaluation pattern** to avoid an unconditional `list_collections()` round-trip on every invocation:

```text
# Lazy resolution pattern (read-path example):
try:
    col = self._client.get_collection(col_name)  # fast path — no extra HTTP
except ChromaNotFoundError:
    names = [c.name for c in self._client.list_collections()]  # only on miss
    col_name = resolve_collection_name(names, col_name)        # raises on 0 or >1 match
    col = self._client.get_collection(col_name)
```

- **`list_store()`**: Apply lazy read-path resolution as above before the `_client.get_collection()` call.
- **`put()`**: **Resolution must be the first operation**, before `doc_id` derivation (`sha256(f"{collection}:{title}")[:16]`). If resolution happens after ID computation, a write via prefix `code__ART` and a prior write via full name `code__ART-8c2e74c0` will produce different IDs and create silent duplicates. Apply lazy write-path resolution using `resolve_collection_name_for_write()`: zero matches return the original name (falls through to `get_or_create_collection()`); multiple matches raise `AmbiguousCollectionError`.
- **`collection_info()`**: Apply lazy read-path resolution before the ChromaDB call. This covers `nx collection info` at the T3Database layer — consistent with the overall strategy that `T3Database` callers get resolution automatically.

This covers `nx store list`, `nx store put`, `nx memory promote`, `nx collection info`, and any other command routing through these three `T3Database` methods.

#### Step 4: Wire into `nx search --corpus` and `nx collection list/verify`

**`nx search --corpus`**: Modify the exact-match branch in `resolve_corpus()` in `corpus.py`. `resolve_corpus()` already receives `all_collections: list[str]` as a parameter from its caller in `search_cmd.py` — no new ChromaDB or T3Database call inside `corpus.py`. When the corpus name contains `__` (indicating a type+basename pattern such as `code__ART`), call `resolve_collection_name(all_collections, corpus)` using that already-available list to resolve the hash suffix. No changes to `search_cmd.py`.

**`nx collection list/verify`**: Apply read-path resolution in `collection.py`'s `list_cmd` and `verify_cmd` (lazy pattern: try exact match first, fetch names on miss, call `resolve_collection_name()`).

**`nx collection info`**: Covered by `T3Database.collection_info()` resolution in Step 3. Additionally, `info_cmd` in `collection.py` currently calls `get_or_create_collection()` — change this to `get_collection()` (or route through `collection_info()`) to eliminate the write side-effect on a read-only command.

**`nx collection delete` — exact names only**: Do not apply prefix resolution to `delete_cmd`. Require the user to supply the full resolved name. This prevents accidental deletion via ambiguous prefix match.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| ChromaDB collections | `nx store list` | N/A | `nx collection delete` (exact name required) | `nx search` | N/A |

## Test Plan

- **Scenario**: `nx store list --collection code__ART` after indexing → **Verify**: Returns collection contents (resolves to `code__ART-{hash}`)
- **Scenario**: `nx store put - --collection code__ART` after indexing → **Verify**: Stores into `code__ART-{hash}`, not a new `code__ART` collection
- **Scenario**: `nx store list --collection code__NONEXISTENT` (no matching collection) → **Verify**: Error message is `CollectionNotFoundError` (distinct from "No entries in {name}" for an existing empty collection)
- **Scenario**: `nx store list --collection code__ART` against an existing but empty collection → **Verify**: "No entries" message (not an error — collection exists)
- **Scenario**: Two repos with same basename both indexed → `nx store list --collection code__ART` → **Verify**: `AmbiguousCollectionError` listing both candidates
- **Scenario**: `nx store put file.txt --collection knowledge__new-topic` where no matching collection exists → **Verify**: New collection `knowledge__new-topic` created (write-path zero-match falls through to `get_or_create_collection`)
- **Scenario**: `nx memory promote --collection knowledge__new-topic` where no matching collection exists → **Verify**: New collection created (same write-path semantics)
- **Scenario**: `nx collection delete code__ART` (without full hash) → **Verify**: Rejected with "exact name required for delete" — no prefix resolution applied
- **Scenario**: `nx store put file.txt --collection code__ART` where two repos with same basename are indexed → **Verify**: `AmbiguousCollectionError` raised on write-path (same as read-path — write-path ambiguity must not silently pick one)

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

### Re-gate 2 (2026-02-27) — PASSED

All C1–C3 and S1–S4 fixes verified correct against codebase. No new critical issues.

#### Significant — Should Fix Before Implementation

**S-NEW-1. Write-path resolution breaks new collection creation.** `T3Database.put()` calls `get_or_create_collection()`, not `get_collection()`. If `resolve_collection_name()` with zero-match → error is applied uniformly to `put()`, then `nx store put --collection knowledge__new-topic` will fail with `CollectionNotFoundError` when the collection does not yet exist — breaking all first writes to new collections. Fix: differentiate read vs. write resolution: for `list_store()`, zero-match is an error; for `put()`, zero-match falls through to `get_or_create_collection()` with the original name. Add test scenario: "first write to nonexistent collection → new collection created."

**S-NEW-2. `nx memory promote` missing from infrastructure audit.** `commands/memory.py` `promote_cmd` accepts `--collection` and calls `T3Database.put()` directly, bypassing `t3_collection_name()`. It is not listed in Affected Commands or the audit table. Must be accounted for.

#### Minor — Fix Before Implementation

**M1. Stale command references.** `nx store get` in Affected Commands does not exist (valid commands: `put`, `list`, `expire`). `nx store delete` in Day 2 Operations table does not exist — correct command is `nx collection delete`.

**M2. Implementation Plan Step 3 says `get_collection()` in `put()`.** `T3Database.put()` calls `get_or_create_collection()`, not `get_collection()`. Step 3 should distinguish: resolution before `_client.get_collection()` in `list_store()`, and resolution with write-path semantics before `get_or_create_collection()` in `put()`.

### Re-gate 2 — Significant/Minor Resolved (2026-02-27)

- S-NEW-1 RESOLVED: Technical Design now defines separate read-path and write-path resolution semantics. `resolve_collection_name_for_write()` returns original name on zero matches (preserving `get_or_create_collection()` behavior). Infrastructure Audit and Implementation Plan Step 3 updated accordingly. Test scenarios added for first-write-to-nonexistent-collection.
- S-NEW-2 RESOLVED: `nx memory promote --collection` added to Affected Commands and Infrastructure Audit.
- M1 RESOLVED: Removed nonexistent `nx store get` from Affected Commands. Replaced `nx store delete` with `nx collection delete` in Day 2 Operations table.
- M2 RESOLVED: Step 3 now explicitly distinguishes `_client.get_collection()` (in `list_store()`) from `get_or_create_collection()` (in `put()`).

### Re-gate 3 (2026-02-27) — PASSED

All S-NEW-1, S-NEW-2, M1, M2 fixes verified correct. No new critical issues.

#### Significant — Should Fix Before Implementation

**Sig-1. `resolve_collection_name()` calling convention is unspecified — `corpus.py` has no ChromaDB imports.** The Technical Design defines `resolve_collection_name(client: ChromaClient, name: str) -> str` as a free function. However, `corpus.py` currently imports no ChromaDB types — `resolve_corpus()` takes `(corpus: str)` with no client parameter, obtaining collections via `T3Database` internally. Adding a `ChromaClient` parameter to a free function in `corpus.py` would introduce a new import dependency and break symmetry with `resolve_corpus()`. Fix: redesign the signature to accept `list[str]` (pre-fetched collection names) instead of `ChromaClient`, keeping `_client.list_collections()` calls inside `T3Database.list_store()` and `T3Database.put()`. This preserves the existing corpus.py calling convention and avoids leaking ChromaDB types into the free-function layer.

**Sig-2. `nx search --corpus` Step 4 has an unresolved "or" — approach is not specified.** The implementation plan says "extend `resolve_corpus()` or add a post-step" without resolving which. The current `resolve_corpus()` exact-match branch uses `t3_collection_name()` directly; a corpus name containing `__` (e.g., `code__ART`) reaches the exact-match branch and bypasses the prefix logic entirely. Fix: specify the approach explicitly — either (a) modify the exact-match branch in `resolve_corpus()` to call `resolve_collection_name_for_write()` after `t3_collection_name()`, or (b) add a post-resolution step in `nx search --corpus` before passing to ChromaDB. Choose one and update Step 4 accordingly.

#### Observations

- O-new-1: The exact-match check in the resolution algorithm adds a `get_collection()` round-trip before the prefix scan. If the caller has already fetched the collection list (as Step 3 proposes with `_client.list_collections()`), the exact-match check is redundant — membership in the list suffices. Simplifying to a single list-scan would eliminate the extra round-trip.
- O-new-2: `T3Database` methods `expire_cmd` and collection `info` are not addressed by the implementation plan. Acceptable if explicitly noted as out of scope.

### Re-gate 3 — Significant/Observations Resolved (2026-02-27)

- Sig-1 RESOLVED: Function signatures changed to `names: list[str]` (not `ChromaClient`). Pseudocode updated — exact match is `name in names` (no extra round-trip). Description clarifies callers in `T3Database` fetch names via `self._client.list_collections()` before calling the functions. Infrastructure Audit decision column updated. Steps 2 and 3 updated with precise caller pattern.
- Sig-2 RESOLVED: Step 4 now specifies a single approach — modify the exact-match branch in `resolve_corpus()` when corpus name contains `__`; no changes to `search_cmd.py`. Infrastructure Audit `nx search --corpus` decision updated.
- O-new-1 APPLIED: Pseudocode now uses `name in names` for exact-match check; no separate `get_collection()` call. Single list-scan eliminates the extra round-trip.
- O-new-2 APPLIED: `nx collection expire` and `nx collection info` explicitly noted as out of scope in Infrastructure Audit table.

### Re-gate 4 (2026-02-27) — BLOCKED

Source files verified against RDR claims: `corpus.py`, `db/t3.py`, `commands/store.py`, `commands/collection.py`, `commands/memory.py`, `commands/search_cmd.py`, `registry.py`.

#### Critical — Must Fix Before Re-gate

**C1. `doc_id` derivation in `put()` uses the user-supplied name — silent duplicate documents on prefix write.** `T3Database.put()` derives `doc_id` as `sha256(f"{collection}:{title}")[:16]`. If a document was previously stored using the full name `code__ART-8c2e74c0` and is now written again using the prefix `code__ART` (which resolves to the same collection), the IDs differ — the second write does not upsert over the first. Two documents with identical content and different IDs coexist silently. No error is raised. The RDR's primary write scenario (`nx store put --collection code__ART`) will produce silent duplicates unless resolution happens at the top of `put()` before the `doc_id` computation, not after. Implementation plan must explicitly document that resolution is the first step in `put()`, preceding all ID derivation.

**C2. Six T3Database write methods omitted from the implementation plan — silent failures after fix.** The Infrastructure Audit covers only `list_store()` and `put()`. Verified in source: six additional methods accept unresolved collection names:

| Method | Call Sites |
|--------|-----------|
| `upsert_chunks(collection, ...)` | `pm.py:389`, indexer pipeline |
| `upsert_chunks_with_embeddings(collection_name, ...)` | `indexer.py:299,444,522`, `doc_indexer.py:204` |
| `update_chunks(collection, ...)` | frecency reindex path |
| `delete_by_source(collection_name, ...)` | indexer incremental update |
| `collection_info(name)` | `collection.py:38` — raises `KeyError` on miss |
| `collection_exists(name)` | `pm.py:457` — exact match only |

The plan must address each: either add resolution, document that call sites always supply fully-qualified names, or explicitly mark as out of scope with rationale.

**C3. Step 4 `nx search --corpus` fix contradicts the "no T3Database import in corpus.py" design principle.** Step 4 says to "fetch collection names via `T3Database`" inside `resolve_corpus()`. But `corpus.py` must not import `T3Database` (the RDR's own constraint in Step 2; a `corpus → t3 → corpus` circular import risk exists). Verified: `resolve_corpus()` already receives `all_collections: list[str]` as a parameter from its caller — no additional fetch is needed. The `__`-containing exact-match branch in `resolve_corpus()` should call `resolve_collection_name(all_collections, corpus)` using the already-available list. The instruction to fetch from `T3Database` is both redundant and a violation of the module invariant.

#### Significant — Should Fix Before Re-gate

**S1. `nx collection info` calls `get_or_create_collection()` — write side-effect on a read-only command.** Verified: `collection.py:40` calls `db.get_or_create_collection(name)`. After prefix resolution is applied, a misresolved or ambiguous name will silently create a new empty collection in ChromaDB. The RDR does not acknowledge this pre-existing issue. The implementation must not worsen it — either note it explicitly, or change `info_cmd` to use `get_collection()` (read-only, raises on miss).

**S2. `list_store()` resolution placement makes the `list_collections()` call unconditional for all invocations.** Step 3 says `list_store()` fetches `names = [c.name for c in self._client.list_collections()]` and then calls `resolve_collection_name(names, col_name)`. If `col_name` is already fully qualified (exact match), the `name in names` check short-circuits — but the fetch happens unconditionally. The RDR claims the round-trip only happens "when exact match fails," which is inconsistent with the proposed implementation. Either lazy-evaluate (try exact match via `get_collection()` first, only fetch names list on miss), or acknowledge that one `list_collections()` call now occurs on every `list_store()` and `put()` invocation, and update the performance analysis in Trade-offs accordingly.

**S3. Resolution for `collection_info()` placed at command layer — inconsistent with the T3Database-layer architecture decision.** The RDR places resolution for `list_store()` and `put()` inside `T3Database` so all callers benefit automatically. But Step 4 places resolution for `collection list/info/verify` at the command layer in `collection.py`. This creates two resolution code paths. A direct caller of `T3Database.collection_info()` (e.g., a future command or PM integration) bypasses command-layer resolution and sees unresolved names again. Either move resolution for `collection_info()` into `T3Database`, or document explicitly why the command-layer approach is preferred for these methods.

#### Observations

- O1: `rdr__` collections (via `_rdr_collection_name()` in `registry.py`) use the same hash-suffix scheme and would have the same UX problem. Scope exclusion is reasonable but should be explicitly documented.
- O2: `list_collections()` in `T3Database` returns objects with a `.name` attribute; normalization to `list[str]` must happen at the caller before passing to `resolve_collection_name()`. The existing `list_collections()` method handles this polymorphism — implementers must match the pattern.
- O3: Alternative 2 (RepoRegistry) rejection understates the registry's value for the primary indexed-repo use case. A hybrid approach (registry first for O(1) lookup on indexed repos, prefix-scan fallback for unregistered collections) would eliminate the HTTP round-trip for the common case. Not a blocker but worth revisiting if performance becomes a concern.
- O4: Test plan has no negative case for `nx store put` with an ambiguous prefix. Write-path ambiguity should be verified explicitly.

### Re-gate 4 — Critical/Significant/Observations Resolved (2026-02-27)

- C1 RESOLVED: Step 3 now explicitly states resolution is the first operation in `put()`, before `doc_id` derivation. Lazy evaluation pseudocode added. Infrastructure Audit decision column for `nx store put` updated.
- C2 RESOLVED: Infrastructure Audit extended with all 6 omitted T3Database methods. `upsert_chunks()`, `upsert_chunks_with_embeddings()`, `update_chunks()`, `delete_by_source()`, `collection_exists()` — all receive fully-qualified names from internal pipelines, no resolution needed (documented). `collection_info()` — user-facing, added read-path resolution at T3Database layer.
- C3 RESOLVED: Step 4 now uses `all_collections: list[str]` already passed to `resolve_corpus()` — no T3Database fetch inside `corpus.py`. Infrastructure Audit `nx search --corpus` decision updated.
- S1 RESOLVED: Step 4 now explicitly requires `info_cmd` to use `get_collection()` (not `get_or_create_collection()`) to eliminate the write side-effect on a read-only command.
- S2 RESOLVED: Technical Design description and Step 3 now specify lazy evaluation — `list_collections()` only called after exact match via `get_collection()` fails. Trade-offs updated to accurately reflect the performance characteristic.
- S3 RESOLVED: `collection_info()` resolution moved into `T3Database.collection_info()` (Step 3), consistent with the T3Database-layer strategy. Command-layer resolution for `info` removed from Step 4.
- O1 APPLIED: `rdr__` collections explicitly noted as out of scope in Trade-offs/Consequences.
- O2 APPLIED: `.name` attribute normalization noted in Technical Design description.
- O3 NOTED: RepoRegistry hybrid noted as optimization opportunity in Trade-offs; not a blocker.
- O4 APPLIED: Test plan now includes `nx store put` with ambiguous prefix scenario.

### Re-gate 5 (2026-02-27) — BLOCKED

Source files re-verified: `corpus.py`, `db/t3.py`, `commands/collection.py`, `commands/store.py`, `commands/memory.py`, `commands/search_cmd.py`, `registry.py`.

#### Critical — Must Fix Before Re-gate

**C1. `list_store()` swallows `_ChromaNotFoundError` before lazy resolution fires.** Verified: the current `list_store()` catches `_ChromaNotFoundError` and returns `[]`. The RDR's lazy resolution pseudocode wraps `get_collection()` in a try/except, but that inner catch intercepts the exception first — the resolution attempt never executes. The fix is not a simple wrap: the existing `return []` path must be replaced so that resolution is attempted first; only after resolution fails (zero matches) should `CollectionNotFoundError` be raised. The RDR must also explicitly specify the post-resolution contract: after a real not-found, does `list_store()` raise `CollectionNotFoundError` or return `[]`? The call site at `store.py:116-118` currently handles both differently.

**C2. `resolve_collection_name()` raises inside `resolve_corpus()` breaks `search_cmd`'s graceful degradation.** Verified: `resolve_corpus()` returns `[]` with a warning on zero matches; `search_cmd.py:151-154` handles the empty-list case and continues to other corpora. If `resolve_collection_name()` raises `CollectionNotFoundError` inside `resolve_corpus()`'s `__`-branch, the exception propagates unhandled. The RDR constraint "No changes to `search_cmd.py`" is incompatible with `resolve_collection_name()` raising. Fix: catch `CollectionNotFoundError` inside the `__`-branch of `resolve_corpus()` and return `[]` to preserve the existing zero-match contract — OR relax the no-`search_cmd.py`-changes constraint and handle the exception there. Pick one.

#### Significant — Should Fix Before Implementation

**S1. `info_cmd` has an exact-match guard at line 31 that bypasses the T3Database fix.** Verified: `info_cmd` calls `list_collections()` at line 30 and does an exact-match check at line 31. A prefix input fails the guard before ever reaching `T3Database.collection_info()`. The T3Database-layer fix is correct but invisible to `info_cmd`. Both the guard (line 31) and the `get_or_create_collection()` call (line 40) must be changed; the RDR currently only mentions the latter.

**S2. `list_cmd` and `verify_cmd` already call `list_collections()` unconditionally — lazy pattern is inapplicable.** Verified: both commands call `db.list_collections()` at their top level and do exact-match filtering. There is no `get_collection()` fast path to fall back from. The lazy pattern (try `get_collection()` first) does not apply here. The correct fix is to call `resolve_collection_name(names, name)` against the already-fetched names list. The RDR's lazy-pattern instruction for these two commands will produce incorrect code.

**S3. `promote_cmd` passes raw `--collection` string to `T3Database.put()` with no `t3_collection_name()` wrapping.** Verified: `commands/memory.py:140` passes the user argument directly to `t3.put()`. Bare names like `--collection myproject` are not canonicalized to `knowledge__myproject`, unlike `nx store put --collection myproject` which routes through `t3_collection_name()`. Either add `t3_collection_name()` canonicalization in `promote_cmd`, or document that `--collection` requires a fully-qualified name.

#### Observations

- O1: `T3Database.collection_metadata()` not in audit — not user-facing today, acceptable omission, but worth a note in the plan so the gap is not silently inherited by future commands.
- O2: `info_cmd` calls `collection_info()` at line 38 but never uses the result — dead call, pre-existing. Clean up during the same pass.
- O3: Test plan covers write-path ambiguity (`AmbiguousCollectionError`) but not write-path success (`nx store put` resolves and writes into the correct existing collection). Add the success scenario.
