---
allowed-tools: Bash
description: Guided Chroma-to-service upgrade — preview then run nx migrate-to-service
---

# Upgrade to the nexus service stack

This is a thin guided surface over the nexus migration engine. It owns no
migration logic: it shows the read-only preview, then hands off to
`nx migrate-to-service`, which drives the proven detect → sequence → validate →
unlock engine (`nexus.migration.driver.run_guided_upgrade`).

## Footprint preview (read-only, touches no data)

!`nx migrate-to-service --dry-run`

## What to do with the preview

The preview above classifies the existing Chroma footprint per collection
(source leg × embedding model) and lists what would migrate, plus any
**unsupported** collections that must be re-indexed first. It moves no data and
needs no service token. A non-zero exit means unsupported collections were
found — resolve those (re-index to a supported model) before the real run.

Walk the user through the preview, then proceed only on their go-ahead:

1. **Confirm the per-leg / per-model counts and the time estimate** look right.
2. **Resolve unsupported collections** if any were flagged (re-index them to a
   supported embedder), then re-run the preview.
3. **Ensure the service stack is reachable** and `NX_SERVICE_TOKEN` is set —
   the full run requires both (the dry-run does not).

## Run the migration

When the user approves, run the full guided upgrade:

```bash
nx migrate-to-service
```

It sequences the T2 catalog ETL then the T3 vectors per detected leg, validates
(taxonomy floor + per-collection counts + manifest orphans), and **unlocks** on
a clean verdict. On a validation block it leaves the `migrated-failed` sentinel
(reads stay degrade-LOUD, never a bare empty index) and exits non-zero.

Rollback is **offered, never automatic** — the copy-not-move ETL leaves Chroma
intact, so a blocked run is fully recoverable:

```bash
nx storage migrate vectors --rollback        # add --cloud for a cloud leg
```

Do not auto-invoke rollback; surface the block to the user and let them choose.
