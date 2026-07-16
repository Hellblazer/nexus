---
allowed-tools: Bash
description: Guided Chroma-to-service upgrade; preview then run nx migrate-to-service
---

# Upgrade to the nexus service stack

This is a thin guided surface over the nexus migration engine. It owns no
migration logic: it shows the read-only preview, then hands off to
`nx migrate-to-service`, which drives the proven detect → sequence → validate →
unlock engine (`nexus.migration.driver.run_guided_upgrade`).

## Footprint preview (read-only, touches no data)

!`nx migrate-to-service --dry-run 2>&1 || true`

## What to do with the preview

The preview above classifies the existing Chroma footprint per collection
(source leg × embedding model) and lists what would migrate, plus any
**unsupported** collections that must be re-indexed first. It moves no data and
needs no service token. A BLOCKED preview is this command's PRIMARY use case,
not an error: the dry-run deliberately exits non-zero when it finds a blocking
condition (scriptable), and the preamble above tolerates that exit so the
report always renders — field incident 2026-07-16: the bare `!`cmd`` form made
the skill CRASH with "Shell command failed" exactly when it had 18 blocked
collections to explain. The preview output names the cause per collection. The
causes have different fixes: an **unsupported model** must be re-indexed to a
supported embedder; **legacy non-32-char chunk ids** (pre-RDR-108) need the
collection re-indexed from its source content — never weaken the chash
constraints (GH #1390); a **Voyage-model collection with no
`NX_VOYAGE_API_KEY`** just needs the key set (no re-index). Resolve whichever
the preview names before the real run.

Walk the user through the preview, then proceed only on their go-ahead:

1. **Confirm the per-leg / per-model counts and the time estimate** look right.
2. **Resolve unsupported collections** if any were flagged (re-index them to a
   supported embedder), then re-run the preview.
3. **Ensure the service stack is reachable** and `NX_SERVICE_TOKEN` is set
   (the full run requires both; the dry-run does not).

## Run the migration

When the user approves, run the full guided upgrade:

```bash
nx migrate-to-service
```

It sequences the T2 catalog ETL then the T3 vectors per detected leg, validates
(taxonomy floor + per-collection counts + manifest orphans), and **unlocks** on
a clean verdict. On a validation block it leaves the `migrated-failed` sentinel
(reads stay degraded-LOUD, never a bare empty index) and exits non-zero.

Rollback is **offered, never automatic**. The copy-not-move ETL leaves Chroma
intact, so a blocked run is fully recoverable:

```bash
nx storage migrate vectors --rollback        # add --cloud for a cloud leg
```

Do not auto-invoke rollback; surface the block to the user and let them choose.
