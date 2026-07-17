---
name: upgrade
description: Use when a user wants to upgrade nexus, migrate an existing store onto the service stack, asks what upgrade steps their install needs, is blocked on a migration, or is holding an old/dormant install that may carry era debt
effort: low
---

# Upgrade

Upgrading nexus is: update the code, then run `nx upgrade`. There is no
sequence to hold and no era to identify.

```bash
uv tool upgrade conexus    # 1. update the code (PRESERVES extras like [local])
nx upgrade                 # 2. converge the data
```

`nx upgrade` brings the package, engine, process, and provisioning
preconditions current, then walks one ordered ladder that auto-applies whichever
data migrations the install actually needs — T2 schema, the ChromaDB →
Postgres+pgvector substrate move, pre-RDR-108 chunk identity, embedder era. Each
rung detects, converges, and verifies before completion is recorded; the walk is
idempotent and resumable, and the source store is left byte-untouched as a
rollback target. An install dormant since 5.x converges the same way a current
one no-ops.

## Reporting before running

- `nx doctor` — reports pending rungs read-only, plus era debt (legacy chunk
  ids) on installs that have not migrated yet.
- `nx upgrade --dry-run` — reports what would run, from each rung's read-only
  detect. Changes nothing and never opens the completion store.

Both are safe to run unprompted. Neither needs a service token.

## The three genuine decisions

The walk asks only what it cannot derive. Surface these to the user and let them
choose; never answer on their behalf:

1. **Billed re-embedding** — collections changing embedding model re-embed
   through a billed Voyage key. The rung shows an estimate-and-confirm prompt
   first. A walk with nothing billable never prompts.
2. **A source collection that vanished** — re-acquire or drop. The walk DEFERS
   rather than guessing; a deferred rung records nothing and retries on a later
   run.
3. **Rollback** — never automatic, never derivable. On a validation block the
   copy stays in place, reads stay degraded-LOUD (never a bare empty index), and
   the remedy is printed: `nx storage migrate vectors --rollback [--cloud]`.
   Surface the block; let the user decide.

Everything else is automatic.

## When a user is blocked on legacy chunk ids

Pre-RDR-108 stores hold 16/18-char chunk ids. `nx upgrade` converges these on
the wire — the correct id is `sha256(chunk_text)[:32]`, a pure function of text
the ETL is already carrying, so **no re-index and no source files are needed**,
including for `store_put`-only notes that have neither.

If you meet an older diagnostic (or the demoted `nx migrate-to-service` path)
printing "re-index from source" as the remedy for legacy ids: that remedy was
impossible for source-less notes and is what RDR-185 retired. The answer is
`nx upgrade`.

**Never drop or weaken chash length CHECK constraints to force upserts
through** (GH #1390). Wire re-id computes the CORRECT address for existing
content; it does not force a wrong id through. If you are blocked on
chash-length errors, run `nx upgrade` or STOP and report — never "unblock" the
constraint.

## Managed service

Pointing at a managed endpoint is configuration, not upgrade. Once configured,
the upgrade is the same one verb:

```bash
nx config set service_url https://api.conexus-nexus.com
export NX_SERVICE_TOKEN=<tenant-token>
nx upgrade
```

## Invariants to honor

- **This surface adds no upgrade logic.** It routes to `nx upgrade`. Anything
  needing orchestration belongs in `nexus.upgrade_ladder`, not here.
- **Do not reach for the demoted primitives.** `nx guided-upgrade`,
  `nx migrate-to-service`, `nx migration`, `nx migration-audit`, and
  `nx collection backfill-hash` are internal primitives — callable, but out of
  the user story because the ladder does their job. If one seems necessary,
  that is a finding worth reporting, not a step to take.
- **A new upgrade verb is never the answer.** New data axes become rungs.

## Notes

Record any upgrade outcome the next session should know via `nx scratch put`
(session-local) or `nx memory put` (cross-session): a blocked verdict, a
deferred decision awaiting the user, or a clean converge.
