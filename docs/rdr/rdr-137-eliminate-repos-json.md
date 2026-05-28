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

### Investigation (2026-05-28 codebase-deep-analyzer dispatch)

**Consumer inventory** — 11 modules grep'd from `src/nexus/`; each access pattern classified with `file:line` precision (T2: `nexus_rdr/137-research-consumer-inventory-2026-05-28`).

| Module | Access pattern | Fields touched |
|---|---|---|
| `registry.py` | source being killed | — |
| `health.py:498-534` | read-only path enumeration | none (only `reg.all()` keys) |
| `context.py:46-86` | read-only | `collection`, `docs_collection` — the nexus-9iw41 source |
| `indexer.py:885-965, 1939-1949` | read-modify-write | reads `code_collection`/`docs_collection`; writes `status` lifecycle + `head_hash`. Primary writer. |
| `catalog/catalog.py:1012` | uses `_repo_identity()` utility only, NOT the registry class | — |
| `catalog/catalog_docs.py:446-484` | fallback-only enumeration (fires only when `owners.repo_root` empty) | repo path strings |
| `mcp/catalog.py:277-285` | read-only path enumeration for absolute-path relativization | repo path strings |
| `commands/catalog.py:1003 / 2989 / 3196` | read-only at 3 sites | repo paths + hash8 suffix |
| `commands/collection.py:544-554` | read-only collection→repo lookup | `collection`/`docs_collection` |
| `commands/doctor.py:1343-1373` | fallback-only (identical to catalog_docs.py) | repo path strings |
| `commands/index.py:15-27, 244-284, 437-439, 462-503` | read-modify-write | reads collection names; **uniquely** mutates `docs_collection` prefix via `--corpus knowledge` rewrite (lines 277-282) |

**Catalog parity probe** — every collection-name field and the repo path resolve to direct catalog equivalents (`collections.name` joined to `owners.repo_root` by `owner_id`). Live catalog has 10 repo owners with non-empty `repo_root`. One narrow gap: `head_hash` is per-repo in registry but per-document in catalog (`documents.head_hash`); see A1 verdict.

**`status` read-site sweep** — exhaustive search finds **zero readers** of `repos.json::status`. Every consumer reads collection names or repo paths. `status` is write-only from the consumer perspective. Live `repos.json` has four repos crash-stranded at `status="indexing"`, confirming the field is already unreliable as a live signal.

**External-consumer sweep** — no readers outside `src/nexus/` and the test suite. 20+ test files use `RepoRegistry(tmp_path / "repos.json")` fixtures; two doc files (`docs/configuration.md`, `docs/rdr/rdr-030-*.md`) are descriptive references.

### Critical Assumptions

- [x] **A1**: catalog carries every fact a consumer reads from `repos.json` except `status`. — **Status**: **Verified-with-gaps** — **Method**: Source Search.
  - Verdict: every collection-name + path field has a direct catalog equivalent. Single gap: `head_hash` (per-repo, used by indexer staleness skip) has no per-repo catalog home. Mitigations: (a) add `owners.head_hash` column (small schema add); (b) switch staleness check to `documents.source_mtime` (already populated).
- [x] **A2**: `status` can be replaced by a derived signal without schema addition. — **Status**: **Verified, stronger than expected** — **Method**: Source Search.
  - Verdict: `status` is **write-only**. No consumer reads it. The "replacement signal" question dissolves: just drop the writes. In-flight detection (when something genuinely needs it) is already in `~/.config/nexus/locks/<hash8>.lock` via `_clear_stale_lock`. Caveat: the `--corpus knowledge` mutation in `commands/index.py:277-282` rewrites `docs_collection` prefix at registry-write time; that routing preference has no catalog home and needs a Phase 4 design decision (separate from `status`).
- [x] **A3**: no external tool reads `repos.json` directly. — **Status**: **Verified-with-noise** — **Method**: Filesystem grep.
  - Verdict: no production readers outside `src/nexus/`. 20+ test fixtures use `RepoRegistry(tmp_path / "repos.json")` (need updating during migration but not external consumers). Two doc references are descriptive only.
- [x] **A4**: Worktree-vs-main-repo dual registration is unintentional; collapse is acceptable. — **Status**: **Refuted-productively** — **Method**: Source Search (wave 2, 2026-05-28).
  - Verdict: dual-registration is **accidental**, not architectural — and the current code already deduplicates worktrees via `_repo_identity()` + `git rev-parse --git-common-dir` (registry.py: `_resolve_repo_collection` → `_repo_identity`; catalog.py: `ensure_owner_for_repo` lines 995-1024). Both paths derive the same `(basename, hash8)` tuple for any worktree of the same logical repo. Live owner `1.22` (qmrr-zm2n) is a **pre-RDR-103 historical artifact** from when callers used raw worktree-path hashes for `repo_hash`; a new indexing run from either path now resolves to owner `1.1` (nexus). **Phase 4 implication**: "collapse" is the right policy AND already the code's intent; the migration needs only a one-time cleanup to alias orphan owners to their `_repo_identity`-derived primary, plus a canonical `repo_root` policy (use the main repo path, not a worktree path, since `resolve_path` uses it as the relative-file-path base).
- [x] **A5**: A dual-read shim is a safe transition mechanism. — **Status**: **Verified-with-gaps** — **Method**: Code review (wave 2, 2026-05-28).
  - Verdict: shim is safe; the **RDR-101 citation is the wrong precedent**. RDR-101 did write-path dual-WRITE (`_emit_shadow_event` at `catalog.py:854`, gated by `NEXUS_EVENT_LOG_SHADOW`); RDR-137 needs read-path dual-READ. The **correct in-repo template** is `catalog/catalog_docs.py:429-484` (`resolve_path`), which already implements primary-catalog-(`owners.repo_root`)-then-fallback-`RepoRegistry` for path fields. `commands/doctor.py:1343-1373` is a parallel exemplar. Phase 2 design refinement: disagreement logger should be **DEBUG-then-WARN** (the Phase 1.5 `owner_id` backfill incompleteness causes the fallback branch to fire on legitimate-empty-catalog reads during early transition; promote to WARN once Phase 1.5 is confirmed). Graduation signal: fallback-branch fire count in `mcp.log` trending to zero (read-path-shaped analogue of RDR-101's "transitional verbs deleted" signal).

### Surfaced surprises (added to Approach + Open Questions)

1. **`collections.owner_id` is empty in live catalog data.** Column exists per RDR-103 but values are unpopulated. **Prerequisite** for any catalog-backed reader: Phase 2's parity audit will hit empty JOIN results until owner_id is backfilled. Add Phase 1.5 work item: backfill `collections.owner_id` from existing data (collection prefix → owner tumbler).
2. **`--corpus knowledge` routing mutation has no catalog home** (`commands/index.py:277-282`). The only registry write that is NOT a lifecycle signal. Phase 4 design question.
3. **Worktree dual-registration is catalog-wide, not registry-only** (see A4 above). Affects scope of the migration.
4. **`head_hash` mitigation path** (see A1 above). Choose between schema add and using `source_mtime` during Phase 1.
5. **`status="error"` semantics narrower than they look**: only credential failures + unhandled exceptions trigger it; per-file failures are silently absorbed via `skipped_files`. No consumer reads `status` anyway, so this is informational.
6. **`_repo_identity()` (from `registry.py`) is NOT registry-coupled** — uses `git rev-parse --git-common-dir` for worktree stability. Extract as standalone helper before deleting the module.

## Approach

Phased migration: build the replacement, prove parity, swap consumers one-at-a-time, then delete.

**Phase 1 — Consumer inventory + catalog parity audit** ✓ DONE (2026-05-28)

Complete the consumer inventory (Investigation step 1). For each consumer, write a parity assertion: "for every repo in `repos.json`, the catalog-backed lookup returns the same fact." Run the assertions against the current local DB; record disagreements (the phantom-collection class) as drift evidence. Output: enumerated consumer list with cutover priority, and a parity-failure ledger that shows the cost of the status quo. Inventory + verified A1/A2/A3 above; A4/A5 still pending.

**Phase 1.5 — Prerequisite catalog backfill** (added 2026-05-28 from research)

Backfill `collections.owner_id` for all rows where the column is empty. The RDR-103 migration introduced the column but did not populate existing rows; without this, any JOIN-based catalog reader returns no results. Inferring `owner_id` from collection-prefix → tumbler mapping is straightforward (tumbler `1.N` maps to a single owner). Add a `nx catalog backfill-owner-id` subcommand or a one-shot migration step; idempotent. Also resolve the `head_hash` mitigation path here: either add `owners.head_hash` column (small schema change) or switch indexer staleness to `documents.source_mtime`. Decision lives in Phase 1.5 implementation.

**Phase 2 — Catalog-backed reader**

Build a single `RepoRegistry`-shaped read API backed by the catalog (`nexus.repos.from_catalog` or similar). Same interface as `RepoRegistry.get(repo_path)` so consumers swap by import change. Include a `--shim` flag that runs both readers and logs disagreements. The in-repo template is `catalog/catalog_docs.py:429-484` (`resolve_path`) which already implements primary-catalog-then-fallback-registry for path fields; the Phase 2 shim generalises that pattern to collection-name fields and adds an explicit disagreement counter. Log at **DEBUG during Phase 2-3** (fallback branch fires legitimately while Phase 1.5's `owner_id` backfill is incomplete) and **promote to WARN once Phase 1.5 is confirmed clean**. Graduation signal: fallback-branch fire count in `mcp.log` trending to zero (the read-path-shaped analogue of RDR-101's "delete the transitional verbs" graduation).

**Phase 3 — Per-consumer cutover**

Swap consumers from `RepoRegistry` to the catalog-backed reader, one PR per consumer (or grouped by access pattern). Each cutover keeps the shim enabled; production telemetry (log count of disagreement events) gates the next swap. Order: read-only consumers first (low risk), then read-modify-write, then the writer path itself.

**Phase 4 — Writer replacement**

Replace `RepoRegistry.put(...)` with a catalog write. New writers register repo↔collection via `nexus.catalog.register(...)` (already exists). Worktree contract per A4: **collapse to canonical-repo registration**. The deduplication mechanism is already in `_repo_identity()` (uses `git rev-parse --git-common-dir`); both `registry.py::_resolve_repo_collection` and `catalog.py::ensure_owner_for_repo` use it. The migration step is a one-time cleanup that detects orphan owners (owners whose `repo_root` resolves via `_repo_identity` to a different primary owner's `repo_hash`) and aliases them to the primary. Canonical `repo_root` for the merged owner must be the main repo path, not a worktree path — `resolve_path` uses it as the relative-file-path base.

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

1. ~~Is the `status` field used as a UI signal that the catalog cannot derive?~~ **Resolved 2026-05-28**: `status` is write-only; no consumer reads it. Just drop the writes; in-flight detection (when needed) is already in `~/.config/nexus/locks/<hash8>.lock`.
2. Should worktrees register as distinct repos (current behaviour, sometimes) or always collapse to the canonical repo's collections?
3. Should the migration be one-shot at upgrade time, or per-consumer over a release cycle? One-shot is simpler if Phase 2's parity audit holds; per-release is safer if parity reveals surprises.
4. Where does the `head_hash` field live post-migration? Catalog or per-repo-derived? **2026-05-28 update**: choose during Phase 1.5 between (a) new `owners.head_hash` column, (b) switching staleness to `documents.source_mtime`.
5. (**New 2026-05-28**) Where does the `--corpus knowledge` routing preference live post-migration? Today `commands/index.py:277-282` mutates `docs_collection` prefix from `docs__` to `knowledge__` in the registry. Options: (a) per-owner config column in the catalog, (b) inferred from existing collection-name prefix at read time, (c) move the rewrite to the caller and never persist it.
6. (**New 2026-05-28**) Should `collections.owner_id` backfill (Phase 1.5) be a one-shot migration in `nx upgrade`, an explicit `nx catalog backfill-owner-id` subcommand, or both?

## Related

- nexus-tts0d — bead tracking this RDR's implementation arc.
- nexus-9iw41 — the user-facing bug this RDR's elimination prevents recurring (closed in PR #993; the data-source bug remains until this RDR ships).
- nexus-9z1qy — superseded patch-direction bead.
- RDR-049 — Git-backed catalog (the system this RDR finishes consolidating around).
- RDR-101 — Event-sourced catalog (provides the consumer-cutover precedent).
- RDR-103 — Catalog as collection-name authority (this RDR extends to repo-name authority).
