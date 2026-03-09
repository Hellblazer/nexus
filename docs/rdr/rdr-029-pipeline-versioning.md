---
title: "Pipeline Versioning — Force Reindex and Collection Version Stamping"
id: RDR-029
type: Enhancement
status: accepted
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-014", "RDR-028"]
related_tests: []
implementation_notes: ""
---

# RDR-029: Pipeline Versioning — Force Reindex and Collection Version Stamping

## Problem Statement

When the indexing pipeline changes (new context prefixes, new chunking logic, new embedding model), `nx index repo` without `--force` silently skips unchanged files because `content_hash` and `embedding_model` match. There is no mechanism to:

1. **Detect staleness**: Know that a collection was indexed with an older pipeline version and needs updating
2. **Selectively re-index**: Re-index only collections with outdated pipeline versions

The `--force` flag (implemented via `docs/plans/2026-03-03-force-reindex-impl-plan.md`, beads nexus-dp08/jazw/mj98/5aoy/wk85, all closed) solves brute-force re-indexing. What remains is **version awareness** — knowing *when* to use `--force` and being able to target only stale collections.

## Context

- A plan exists at `docs/plans/2026-03-03-force-reindex-impl-plan.md` for the `--force` flag
- RDR-014 R5 identified the version stamping need: "Adding a version stamp to collection metadata (and checking it on open) would surface this mismatch"
- The staleness check at `indexer.py:524-533` compares `content_hash` + `embedding_model` — pipeline version is not checked
- Each pipeline change (RDR-014 context prefix, RDR-028 definition extraction) requires manual collection deletion

## Research Findings

### F1: Force Reindex Plan (Verified — existing plan)

The plan at `docs/plans/2026-03-03-force-reindex-impl-plan.md` proposes adding `--force` to all four `nx index` subcommands (`repo`, `pdf`, `md`, `rdr`). Implementation: skip the `content_hash` check when `--force` is set, re-process all files regardless.

### F2: Collection Metadata API (Verified — ChromaDB docs)

ChromaDB collections support arbitrary metadata via `collection.modify(metadata={...})`. This can store a pipeline version:
```python
col.modify(metadata={"pipeline_version": "3", "last_indexed": "2026-03-08T12:00:00"})
```

On collection open, compare stored `pipeline_version` against current `PIPELINE_VERSION` constant. If mismatched, warn user.

### F3: What Constitutes a Pipeline Version Change

Changes that invalidate existing embeddings:
- Context prefix format changes (RDR-014, RDR-028)
- Chunk size changes (Track B)
- Embedding model changes
- Definition extraction additions (RDR-028)
- Classifier changes (new SKIP extensions)

Changes that do NOT invalidate embeddings:
- Scoring weight changes
- CLI flag additions
- Formatter changes

## Proposed Solution

### Component 1: `--force` Flag (DONE)
Already implemented. `--force` is on all four `nx index` subcommands. Mutual exclusion with `--frecency-only` and `--dry-run` enforced.

### Component 2: Pipeline Version Stamp
1. Define `PIPELINE_VERSION = "4"` constant in `indexer.py` (bump on each embedding-affecting change)
2. On `nx index repo`: write `pipeline_version` to collection metadata after indexing
3. On `nx index repo` (without --force): check `pipeline_version` against current. If mismatched, emit warning: "Collection was indexed with pipeline v{old}, current is v{new}. Run with --force to re-index."
4. On `nx doctor`: check all collections for version mismatches

### Component 3: Selective Force
Add `--force-stale` flag that only re-indexes collections with outdated `pipeline_version`, skipping collections that are current. This is the "smart force" — re-index what needs it without touching everything.

## Alternatives Considered

**A. Auto-reindex on version mismatch**: Dangerous for large collections (e.g., `code__ART` at 94K chunks). User should opt in.

**B. Per-file pipeline version in metadata**: More granular but much more complex. Collection-level is sufficient because pipeline changes affect all files equally.

## Trade-offs

**Benefits**:
- Eliminates the manual "delete collection, re-index" workflow
- Version mismatch warnings prevent silent quality degradation
- `--force-stale` provides a one-command upgrade path

**Risks**:
- Bumping `PIPELINE_VERSION` requires developer discipline (forgettable)
- `--force` on large collections is expensive (e.g., `code__ART`: 94K chunks, ~$15 Voyage AI)

## Implementation Plan

### Phase 1: Pipeline Version Constant + Stamp (~30 LOC)
1. Add `PIPELINE_VERSION = "4"` constant to `indexer.py` with version history comment: v1-v3 pre-versioning, v4 = RDR-028 language registry + RDR-014 CCE prefixes
2. After successful indexing in `_run_index()`, write `pipeline_version` to collection metadata **only when `force=True`** (or `force_stale=True`), using merge form: `col.modify(metadata={**(col.metadata or {}), "pipeline_version": PIPELINE_VERSION})`. Non-force runs must NOT advance the version stamp — otherwise a partial incremental run would mark stale chunks as current.
3. Write version to code and docs collections at CLI level (`commands/index.py`) after all indexing completes (avoids touching doc_indexer.py). For standalone `nx index pdf/md/rdr --force`, stamp the target collection after indexing.

### Phase 2: Staleness Detection + Warning (~25 LOC)
4. At start of `_run_index()`, read collection metadata `pipeline_version`
5. If stored version is **not None** and != current `PIPELINE_VERSION`, emit `structlog.warning()` with: "Collection {name} indexed with pipeline v{old}, current is v{new}. Run with --force to re-index." Gate on `is not None` to avoid spurious warning on first-time index of new collections.
6. Continue indexing normally (warning only, not blocking)

### Phase 3: `--force-stale` Flag (~30 LOC)
7. Add `--force-stale` flag to `nx index repo` CLI command. Scoped to `repo` only — standalone `pdf`/`md`/`rdr` commands target individual files where `--force` is sufficient.
8. When set, check each collection's `pipeline_version`; if **any** collection is stale, set `force=True` for the entire `_run_index()` pass. Coarse-grained (if code_col or docs_col is stale, re-index all) — acceptable because pipeline changes affect all file types equally.
9. Mutual exclusion: `--force-stale` and `--force` are mutually exclusive (force already re-indexes everything; force-stale is selective)

### Phase 4: `nx doctor` Version Check (~50 LOC)
10. Add pipeline version check to `nx doctor`: iterate registered repos → map to collection names → read `col.metadata` for each → compare against `PIPELINE_VERSION` → flag mismatches

### Phase 5: Tests (~40 LOC)
11. Test: `PIPELINE_VERSION` written to collection metadata after force indexing (NOT after non-force)
12. Test: version mismatch warning emitted when stored != current; NOT emitted for new collections (None)
13. Test: `--force-stale` only re-indexes stale collections (mock two collections, one current one stale)
14. Test: `--force-stale` and `--force` mutual exclusion
15. Test: `nx doctor` reports stale collections

## Test Plan

- Unit: pipeline version written to collection metadata
- Unit: version mismatch warning emitted
- Unit: `--force-stale` only re-indexes outdated collections
- Unit: `--force-stale` and `--force` mutual exclusion
- Unit: `nx doctor` detects stale collections
- Integration: index, bump version, verify warning on next index

## Finalization Gate

### Contradiction Check
No contradictions. Component 1 (--force) is already implemented and verified. Components 2-4 are additive.

### Assumption Verification
- [x] ChromaDB `collection.modify(metadata={...})` supports arbitrary metadata — **Verified**: API docs and existing usage in codebase
- [x] `--force` is already implemented — **Verified**: source search confirms --force on all 4 subcommands
- [x] `collection.metadata` is readable — **Verified**: ChromaDB API returns metadata dict on collection object

### Scope Verification
Scope: one constant, metadata write after indexing, staleness check on index start, one new CLI flag, one doctor check. No architectural changes. ~145 LOC total.

### Cross-Cutting Concerns
- **doc_indexer.py**: Version stamping done at CLI level (`commands/index.py`) after all indexing completes, not inside doc_indexer.py. This keeps doc_indexer.py unaware of pipeline versioning.
- **Collection metadata merge**: `col.modify(metadata=...)` replaces all metadata. Must use `{**(col.metadata or {}), "pipeline_version": PIPELINE_VERSION}` to merge.
- **Stamp only on force**: Non-force runs must NOT advance the version stamp. Otherwise a partial incremental run marks stale chunks as current, breaking the invariant.
- **New collection guard**: First-time index has `pipeline_version=None` — skip warning, stamp after force-index.
- **--force-stale granularity**: Coarse-grained (any stale → force all). Per-collection-type force (`force_code`/`force_docs`) is over-engineering since pipeline changes affect all file types equally.

### Proportionality
~175 LOC for version-aware indexing across all collections. Proportionate — this eliminates the "silent quality degradation" anti-pattern that caused multiple P0 issues.

## References

- Existing plan: `docs/plans/2026-03-03-force-reindex-impl-plan.md`
- RDR-014 R5: version stamping need identified
- ChromaDB collection metadata API: `collection.modify(metadata={...})`

## Revision History

### Gate Review 1 (2026-03-09)

**Layer 3 — AI Critique (substantive-critic)**: 1 FAIL, 5 WARNs.

Corrections applied:
- **FAIL (Contradiction)**: Two issues fixed: (1) metadata clobber — Step 2 now uses merge form `{**(col.metadata or {}), ...}`. (2) Unconditional stamp write — now gated on `force=True` only. Non-force runs do NOT advance version stamp.
- **WARN (Assumption)**: New collection warning gated on `stored_version is not None`. Starting version "4" now has documented history comment.
- **WARN (Scope)**: doc_indexer stamping resolved — CLI-level stamp chosen. `--force-stale` explicitly scoped to `nx index repo`. Doctor LOC estimate corrected to ~50 LOC.
- **WARN (Cross-cutting)**: `--force-stale` granularity explicitly documented as coarse-grained (acceptable).
- LOC estimate updated to ~175.
