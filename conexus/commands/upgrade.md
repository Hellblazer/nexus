---
allowed-tools: Bash
description: Show what nx upgrade would converge, then run it
---

# Upgrade

Upgrading nexus is: update the code, then run `nx upgrade`. This command owns no
upgrade logic — it shows the read-only report, then hands off to the single
trigger, which walks the RDR-185 ladder (preconditions, then every pending data
rung, each detect → converge → verify).

## What is pending (read-only, changes nothing)

!`nx upgrade --dry-run 2>&1 || true`

!`nx doctor 2>&1 | grep -iE 'upgrade ladder|chunk-id era' || true`

## What to do with the report

The report above comes from each rung's read-only `detect()` — it opens no
completion store and writes nothing. It names every pending rung and why. An
empty report means the install is current: say so and stop.

Walk the user through it, then proceed only on their go-ahead:

1. **Confirm the pending rungs** look like their install's history.
2. **Surface any cost**: collections changing embedding model re-embed through a
   billed Voyage key. The rung prompts with an estimate before charging; nothing
   billable means no prompt.
3. **Surface any deferred decision**: a source collection that has vanished is a
   re-acquire-or-drop choice the product will not guess. The walk defers it.

## Run the upgrade

When the user approves:

```bash
nx upgrade
```

It converges the preconditions (including provisioning and verifying the service
stack if a legacy footprint needs one to migrate into), then walks each pending
rung to completion. Idempotent and resumable — re-running converges, never
duplicates. The source store is left byte-untouched throughout, so it stays a
valid rollback target.

On a validation block the migrated copy stays in place, reads stay
degraded-LOUD (never a bare empty index), and the run exits non-zero.

Rollback is **offered, never automatic** — undo is not something the product can
derive:

```bash
nx storage migrate vectors --rollback        # add --cloud for a cloud leg
```

Surface the block to the user and let them choose. Do not auto-invoke rollback.

## Do not

- Do not reach for `nx guided-upgrade`, `nx migrate-to-service`, `nx migration`,
  `nx migration-audit`, or `nx collection backfill-hash`. They are demoted
  internal primitives: still callable, but the ladder does their job. Needing one
  is a finding to report, not a step to take.
- Do not answer "re-index from source" for legacy (pre-RDR-108) chunk ids. The
  rung recomputes the correct id from the stored chunk text on the wire — no
  re-index, no source files. That impossible remedy is what RDR-185 retired.
- Do not weaken chash length CHECK constraints to force upserts through
  (GH #1390). Run `nx upgrade` or STOP and report.
