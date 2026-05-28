---
title: "Eliminate repos.json: Catalog as the Canonical Repo→Collection Source of Truth"
id: RDR-137
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-28
accepted_date:
related_issues: [nexus-tts0d, nexus-9iw41, nexus-9z1qy]
related_rdrs: [RDR-049, RDR-101, RDR-103, RDR-108, RDR-120]
supersedes: []
related_tests: []
---

# RDR-137: Eliminate repos.json: Catalog as the Canonical Repo→Collection Source of Truth

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

`~/.config/nexus/repos.json` (accessed through `nexus.registry.RepoRegistry`) maintains a per-repo mapping of `repo_path → {code_collection, docs_collection, rdr_collection, status, head_hash, …}`. The catalog (`~/.config/nexus/catalog/.catalog.db`) already maintains an authoritative repo→collection mapping via `owners`, `collections`, and `documents` tables. Two sources of truth for the same fact drift apart, and the drift is what produced nexus-9iw41 (the Knowledge Map duplicate-labels bug): the main `/Users/hal.hildebrand/git/nexus` entry in `repos.json` pointed at phantom collections `code__1-2188 / docs__1-2188 / rdr__1-2188` that do not exist in chroma, while the *worktree* entries (e.g. `nexus/.claude/worktrees/qmrr-zm2n`) correctly pointed at `1-1`. Eleven modules read or write `repos.json` today; replacing them with a catalog-backed lookup eliminates the entire class of drift bugs.

### Enumerated gaps to close

#### Gap 1: Two sources of truth for the same fact

`repos.json` and the catalog both answer "which T3 collections does this repo own?" — and they answer differently. nexus-9iw41 demonstrated the consequence: `_repo_collections('/Users/hal.hildebrand/git/nexus')` returned `{docs__1-2188, rdr__1-2188}` (from `repos.json`) while the actual nexus chunks lived in `docs__1-1 / rdr__1-1` (per catalog + chroma). The injected Knowledge Map silently surfaced phantom-collection garbage and missed the real content. Single source of truth eliminates this class.

#### Gap 2: No validation of registration against reality

The `RepoRegistry.put(...)` writer assigns a tumbler and writes the resulting collection name to `repos.json` without checking that the named chroma collection exists or that the catalog agrees. A misassignment (the 1-2188 case) persists forever until a human notices the Knowledge Map looks wrong. Catalog-backed lookup makes the invariant structural rather than advisory.

#### Gap 3: Eleven consumers, no migration plan

`grep -l 'repos\.json\|RepoRegistry' src/nexus/` returns 11 modules: `registry.py`, `health.py`, `context.py`, `indexer.py`, `catalog/catalog.py`, `catalog/catalog_docs.py`, `mcp/catalog.py`, `commands/{catalog,collection,doctor,index}.py`. Each consumer needs a catalog-backed replacement, but their access patterns differ (some read, some write, some merge status fields not present in the catalog). A flag-day swap is risky; a per-consumer cutover needs a stable dual-read shim.

#### Gap 4: Worktree handling has no documented contract

Worktrees and main repo paths get independently-assigned tumblers — same logical project, multiple identities in `repos.json`. Whether that's intentional (different working copies, different indexing state) or accidental (writer race assigning fresh tumblers per path) is not documented. The catalog representation must either preserve this multiplicity intentionally or collapse it; either requires explicit design.

#### Gap 5: `status` field has no obvious catalog home

`repos.json` carries `status: ready | indexing | error` per repo. The catalog tracks documents and collections; it does not currently carry a per-repo indexing-lifecycle state. Either (a) move `status` into the catalog (new column or table), (b) keep a small in-memory or per-process state for indexing lifecycle and let the catalog be the source for stable repo→collection mapping only, or (c) replace `status` with a derived signal (e.g. count of pending docs in catalog). The choice affects the migration scope.

## Context

### Background

Surfaced during the 2026-05-28 SessionStart injection audit. nexus-9iw41 fixed the user-facing duplicate-labels bug by adding defensive dedup in `generate_context_l1` and deleting 815 phantom topic rows. The follow-up investigation (nexus-9z1qy) found the underlying data source was `repos.json`, not the catalog. User directive: do not patch `repos.json`; eliminate it. nexus-9z1qy closed as superseded; nexus-tts0d filed as the elimination tracker; this RDR is the design vehicle.

### Technical Environment

- `nexus.registry.RepoRegistry` — file-backed JSON dict at `~/.config/nexus/repos.json`. Schema per repo: `name, collection, code_collection, docs_collection, rdr_collection, head_hash, status`.
- Catalog: SQLite at `~/.config/nexus/catalog/.catalog.db`. Tables include `owners(tumbler_prefix, name, owner_type, …)`, `collections(name, content_type, owner_id, …)`, `documents(…, owner_id, physical_collection)`. Catalog ownership is documented in RDR-049 (the git-backed catalog) and RDR-103 (catalog as collection-name authority).
- Worktrees: each git worktree under `.claude/worktrees/` is currently registered as a separate `repos.json` entry, sometimes with the same tumbler as the parent repo (e.g. `1-1`) and sometimes with a different one.
- T3 chroma: actual collection existence is what `nx collection list` returns; `repos.json` does not consult it.

## Research Findings

### Investigation

To be completed during `/conexus:rdr-research`. Outline of the work:

1. **Consumer inventory** — for each of the 11 modules: classify as read-only, write-only, or read-modify-write; capture the exact fields touched; note whether the consumer needs `status` or only the collection mapping. Output: a table that scopes the cutover work.
2. **Catalog query catalog** — for each access pattern in (1), draft the equivalent catalog SQL (or T2Database/T2Client call). Confirm the catalog actually carries the needed information; surface any gaps that need a schema addition.
3. **Worktree-handling probe** — read `nexus.indexer`'s code path that calls `RepoRegistry.put(...)` for a new repo. Determine why main and worktree get different tumblers in some cases and the same in others. Document the intended semantics; choose whether to preserve, collapse, or formalise.
4. **Failure-mode spike** — reproduce the 1-2188 misassignment in a sandbox. Confirm the writer's path, then verify the catalog-backed replacement rejects an analogous bad input.

### Critical Assumptions

- [ ] **A1**: The catalog already carries every fact a consumer reads from `repos.json` except `status`. — **Status**: Unverified — **Method**: Source Search.
- [ ] **A2**: `status` (indexing/ready/error) can be replaced by a derived signal from the catalog (e.g. "ready" iff doc_count > 0 and no in-flight indexing claim), without a schema addition. — **Status**: Unverified — **Method**: Source Search.
- [ ] **A3**: No external tool reads `repos.json` directly (no `~/.config/nexus/repos.json` references in scripts or other packages on disk). — **Status**: Unverified — **Method**: Filesystem grep.
- [ ] **A4**: Worktree-vs-main-repo dual registration is unintentional; collapsing to a single registration per logical repo is acceptable. — **Status**: Unverified — **Method**: Spike + user confirmation.
- [ ] **A5**: A dual-read shim (read from catalog first, fall back to `repos.json`, log when they disagree) is a safe transition mechanism — consumers see consistent reads during cutover, drift is observable as it happens. — **Status**: Unverified — **Method**: Code review.

## Approach

Phased migration: build the replacement, prove parity, swap consumers one-at-a-time, then delete.

**Phase 1 — Consumer inventory + catalog parity audit**

Complete the consumer inventory (Investigation step 1). For each consumer, write a parity assertion: "for every repo in `repos.json`, the catalog-backed lookup returns the same fact." Run the assertions against the current local DB; record disagreements (the phantom-collection class) as drift evidence. Output: enumerated consumer list with cutover priority, and a parity-failure ledger that shows the cost of the status quo.

**Phase 2 — Catalog-backed reader**

Build a single `RepoRegistry`-shaped read API backed by the catalog (`nexus.repos.from_catalog` or similar). Same interface as `RepoRegistry.get(repo_path)` so consumers swap by import change. Include a `--shim` flag that runs both readers and logs disagreements (RDR-101 used the same pattern during the catalog cutover).

**Phase 3 — Per-consumer cutover**

Swap consumers from `RepoRegistry` to the catalog-backed reader, one PR per consumer (or grouped by access pattern). Each cutover keeps the shim enabled; production telemetry (log count of disagreement events) gates the next swap. Order: read-only consumers first (low risk), then read-modify-write, then the writer path itself.

**Phase 4 — Writer replacement**

Replace `RepoRegistry.put(...)` with a catalog write. New writers register repo↔collection via `nexus.catalog.register(...)` (already exists). Worktree contract is decided per A4: either preserve dual registration with explicit per-worktree owner_id, or collapse to canonical-repo registration only.

**Phase 5 — Migration + delete**

One-shot migration reads the final `repos.json` state, asserts catalog parity, and on success deletes the file. Doctor check warns if `repos.json` still exists. The `nexus.registry` module is removed; CI lint guards against re-introduction.

## Success Criteria

- `~/.config/nexus/repos.json` is not present on the install surface after the user's first session start following the migration.
- All 11 historical consumers route through the catalog-backed reader; `grep -r 'repos\.json\|RepoRegistry' src/nexus/` returns zero matches outside of the deleted-module tombstone.
- A regression test seeds a phantom collection name (one not present in chroma) into the catalog write path and asserts the write is rejected with an actionable error.
- The Knowledge Map docs side for the nexus repo, post-migration, surfaces the real `docs__1-1` top-5 root topics (not empty, not phantom).
- `nx doctor` reports no drift between catalog-backed repo mapping and `nx collection list` output.

## Out of Scope

- Schema changes to T3 chroma. The catalog is the surface that owns repo registration.
- A general overhaul of `nx repos` UX. The CLI is a downstream consumer; it picks up the new reader.
- Migrating other JSON-backed state outside `repos.json` (e.g. `t2_addr.501`, `context/*.txt`). Each is an independent file-vs-catalog question; this RDR is bounded to `repos.json`.
- Cross-host federation (multiple machines syncing repo registrations). Single-host scope only.

## Open Questions

1. Is the `status` field used as a UI signal that the catalog cannot derive (e.g. "this repo is mid-index"), or is it strictly bookkeeping that can be replaced by a derived signal?
2. Should worktrees register as distinct repos (current behaviour, sometimes) or always collapse to the canonical repo's collections?
3. Should the migration be one-shot at upgrade time, or per-consumer over a release cycle? One-shot is simpler if Phase 2's parity audit holds; per-release is safer if parity reveals surprises.
4. Where does the `head_hash` field live post-migration? Catalog or per-repo-derived?

## Related

- nexus-tts0d — bead tracking this RDR's implementation arc.
- nexus-9iw41 — the user-facing bug this RDR's elimination prevents recurring (closed in PR #993; the data-source bug remains until this RDR ships).
- nexus-9z1qy — superseded patch-direction bead.
- RDR-049 — Git-backed catalog (the system this RDR finishes consolidating around).
- RDR-101 — Event-sourced catalog (provides the consumer-cutover precedent).
- RDR-103 — Catalog as collection-name authority (this RDR extends to repo-name authority).
