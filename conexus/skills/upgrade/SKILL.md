---
name: upgrade
description: Use when a user wants to upgrade or migrate their existing Chroma knowledge store to the nexus PG16+pgvector service stack, moving permanent vectors off ChromaDB onto the served backend, or asking how to run the guided migration
effort: low
---

# Guided Chroma-to-service upgrade

A thin guided surface over the nexus migration engine. All sequencing,
validation, and rollback live in `nexus.migration` (the `nx migrate-to-service`
CLI and `nexus.migration.driver.run_guided_upgrade`); this skill only routes the
user to it and explains the two-step preview-then-run shape.

## When this applies

The user has an existing local-Chroma or cloud-Chroma knowledge store and wants
their permanent vectors served by the nexus service (PG16 + pgvector) instead.
One command replaces the ~8 manual upgrade steps.

## Step 1: preview (read-only)

```bash
nx migrate-to-service --dry-run
```

Classifies the Chroma footprint per collection (source leg × embedding model),
previews per-leg/per-model counts and a coarse time estimate, and flags
**unsupported** collections (e.g. a model the service is not wired for) that
must be re-indexed before a real run. Touches no data; needs no service token.
A non-zero exit means the preview found a blocking condition, named per
collection in its output. Two distinct causes with different fixes: an
**unsupported model** needs a re-index to a supported embedder; a
**Voyage-model collection with no `NX_VOYAGE_API_KEY`** just needs the key set
(no re-index). Read the preview to tell which.

## Step 2: run (after the user approves)

```bash
nx migrate-to-service
```

Requires a reachable service and `NX_SERVICE_TOKEN`. Sequences the T2 catalog
ETL then the T3 vectors per leg, validates (taxonomy floor + per-collection
counts + manifest orphans), and unlocks on a clean verdict.

## Invariants to honor

- The user **never sees a bare empty index** mid-migration: a blocked run
  leaves the `migrated-failed` sentinel and reads stay degraded-LOUD.
- Rollback is **offered, never auto-invoked** (`nx storage migrate vectors
  --rollback [--cloud]`). The copy-not-move ETL keeps Chroma intact, so a
  blocked run is recoverable. Surface the block to the user; let them choose.
- This surface adds **no migration logic**. If something needs orchestration,
  it belongs in `nexus.migration`, not here.

## Notes

Record any migration outcome the next session should know via `nx scratch put`
(session-local) or `nx memory put` (cross-session): a blocked verdict, the
unsupported collections found, or a clean unlock.
