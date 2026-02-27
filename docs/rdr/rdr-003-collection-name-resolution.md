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

When `nx index repo` runs, it computes a repo identity hash (8-char hex) from the repo's git remote URL and appends it to the collection name. This handles the case where two repos share the same basename (e.g., `my-project` cloned in two locations). The hash is logged during indexing but not persisted anywhere user-visible.

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
- Affected commands: `nx store list`, `nx store get`, `nx search --corpus`, `nx collection` subcommands

## Research Findings

### Investigation

Traced the collection naming through `indexer.py` and `registry.py`. The `_repo_identity()` function returns `(basename, hash8)`. Collections are named `{type}__{basename}-{hash8}`. The `nx store list --collection NAME` command passes `NAME` directly to ChromaDB's `get_collection()` which requires an exact match.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| ChromaDB `get_collection` | Yes | Requires exact name — no prefix/glob support |
| ChromaDB `list_collections` | Yes | Returns all collections; can filter client-side |

### Key Discoveries

- **Verified** — `get_collection(name)` in ChromaDB requires exact name match (no fuzzy/prefix)
- **Verified** — `list_collections()` returns all collections without filter; prefix filtering must be done client-side
- **Documented** — `_repo_identity()` in `registry.py` returns `(basename, hash8)` from git remote URL hash
- **Verified** — The hash suffix is never written to any user-visible config or status file during indexing
- **Assumed** — Hash is stable for a given remote URL (needs verification for repos with multiple remotes or no remote)

### Critical Assumptions

- [x] `list_collections()` can enumerate all collections to find prefix matches — **Status**: Verified — **Method**: Source Search
- [ ] Hash is stable across machines for same remote URL — **Status**: Unverified — **Method**: Docs Only

## Proposed Solution

### Approach

Add a **prefix resolution layer** to the CLI's collection name handling. When a user provides a collection name that does not exactly match any collection, attempt prefix resolution: list all collections and return the unique match whose name starts with the provided prefix. If zero or multiple matches exist, surface a helpful error.

### Technical Design

Resolution priority for a given `name`:
1. Exact match → use as-is
2. Single prefix match (one collection starts with `name`) → use it, log a note
3. Zero matches → error: "no collection matching `{name}` or `{name}-*`"
4. Multiple prefix matches → error listing all matches (disambiguation required)

The resolution should be a shared utility function, not duplicated across subcommands.

```text
// Illustrative — verify signatures during implementation
def resolve_collection_name(db: ChromaClient, name: str) -> str:
    """Return exact collection name, resolving by prefix if needed."""
    # 1. Exact match
    # 2. Prefix scan via list_collections()
    # raises CollectionNotFoundError | AmbiguousCollectionError
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `resolve_collection_name()` | None | Create new in `src/nexus/db.py` or `collections.py` |
| `nx store list --collection` | `src/nexus/commands/store.py` | Extend: add prefix resolution before exact lookup |
| `nx search --corpus` | `src/nexus/commands/search.py` | Extend: same resolution utility |
| `nx collection` subcommands | `src/nexus/commands/collection.py` | Extend: same resolution utility |

### Decision Rationale

Prefix resolution is the minimal change that fixes the UX without breaking any existing behavior (exact matches continue to work unchanged). Storing the hash in a local registry file was considered but rejected as it adds state management complexity and breaks for repos accessed from multiple machines.

## Alternatives Considered

### Alternative 1: Registry file (`~/.config/nexus/repos.json`)

**Description**: Store `basename → collection_name` mapping in a local JSON file written at index time.

**Pros**:
- O(1) lookup (no list_collections() call)
- Works offline

**Cons**:
- Per-machine state — breaks when accessing the same repo from a new machine
- Requires cleanup when repos are deleted/re-indexed
- Extra file to maintain

**Reason for rejection**: Prefix scan against ChromaDB is cheap; avoiding per-machine state is worth the small overhead.

### Briefly Rejected

- **Strip hash from collection names entirely**: Would break disambiguation between two repos with the same basename cloned in different locations.
- **Expose hash in `nx index repo` output**: Helps discoverability but does not fix the lookup problem at query time.

## Trade-offs

### Consequences

- `nx store list --collection code__ART` will work as expected (no hash needed)
- `list_collections()` is called on any unresolved name — one extra round-trip to ChromaDB (negligible)
- Ambiguous prefix errors give users actionable information

### Risks and Mitigations

- **Risk**: `list_collections()` is slow on large ChromaDB instances with many collections
  **Mitigation**: Only invoked when exact match fails; not in the hot path

### Failure Modes

- Two repos with the same basename (e.g., `ART`) both indexed → ambiguous prefix → user sees error listing both; must use full name with hash to disambiguate

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (hash stability across machines)
- [ ] Identify all CLI entry points that accept collection names

### Minimum Viable Validation

`nx store list --collection code__ART` returns results after indexing the ART repo, without requiring the hash suffix.

### Phase 1: Core Resolution Utility

#### Step 1: Add `resolve_collection_name()` to shared module

Implement the 4-case resolution logic (exact → prefix → zero → ambiguous) in `src/nexus/db.py` or a new `src/nexus/collections.py`.

#### Step 2: Wire into `nx store list --collection`

Replace direct `get_collection(name)` call with `resolve_collection_name(db, name)` in `commands/store.py`.

#### Step 3: Wire into `nx search --corpus` and `nx collection` subcommands

Apply the same resolution utility to all other commands accepting collection names.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| ChromaDB collections | `nx store list` | N/A | `nx store delete` | `nx search` | N/A |

## Test Plan

- **Scenario**: `nx store list --collection code__ART` after indexing → **Verify**: Returns collection contents (resolves to `code__ART-{hash}`)
- **Scenario**: `nx store list --collection code__ART` with no matching collection → **Verify**: Clear "not found" error
- **Scenario**: Two repos with same basename both indexed → `nx store list --collection code__ART` → **Verify**: Ambiguity error listing both candidates

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and proposed solution.

### Assumption Verification

- Hash stability across machines: Unverified. Needs spike (clone same repo on two machines, index, compare collection names). Low risk for Phase 1 — the resolution utility works regardless of hash stability.

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

- `src/nexus/registry.py` — `_repo_identity()` function
- `src/nexus/indexer.py` — collection naming at index time
- `src/nexus/commands/store.py` — `nx store list` implementation
- ChromaDB Python client: `list_collections()`, `get_collection()`

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

#### Observations

- `rdr__` collections (produced by `nx index`) use the same `{type}__{basename}-{hash}` scheme but are not covered by this RDR's scope — will become a user-facing gap soon.
- `AmbiguousCollectionError` does not exist in `errors.py` — must be added; not mentioned in implementation plan.
- `RepoRegistry` already stores `code_collection` and `docs_collection` per registered repo path. An O(1) local registry lookup by basename is a viable alternative to ChromaDB prefix scanning for `nx index`-registered repos — worth brief evaluation even if rejected for the same cross-machine reason.
