---
title: "Pipeline Versioning — Force Reindex and Collection Version Stamping"
id: RDR-029
type: Enhancement
status: draft
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

When the indexing pipeline changes (new context prefixes, new chunking logic, new embedding model), `nx index repo` without `--force` silently skips unchanged files because `content_hash` and `embedding_model` match. There is no way to:

1. **Force re-indexing**: Override the hash-based dedup to re-process all files with the new pipeline
2. **Detect staleness**: Know that a collection was indexed with an older pipeline version and needs updating

This creates a silent quality degradation window after every pipeline improvement. Users must manually delete entire collections and re-index from scratch.

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

### Component 1: `--force` Flag
Add `--force` to all `nx index` subcommands. When set, skip the content_hash/embedding_model staleness check and re-process all files.

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

1. Add `PIPELINE_VERSION` constant to `indexer.py`
2. Add `--force` flag to all `nx index` subcommands
3. Write `pipeline_version` to collection metadata after indexing
4. Check `pipeline_version` on index start, warn if stale
5. Add `--force-stale` flag for selective re-indexing
6. Add version check to `nx doctor`
7. Add tests for force reindex and version mismatch detection

## Test Plan

- Unit: `--force` bypasses content_hash check
- Unit: pipeline version written to collection metadata
- Unit: version mismatch warning emitted
- Unit: `--force-stale` only re-indexes outdated collections
- Integration: index, bump version, verify warning on next index

## References

- Existing plan: `docs/plans/2026-03-03-force-reindex-impl-plan.md`
- RDR-014 R5: version stamping need identified
- ChromaDB collection metadata API: `collection.modify(metadata={...})`
