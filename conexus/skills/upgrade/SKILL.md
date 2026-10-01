---
name: upgrade
description: Use when a user wants to upgrade nexus, migrate an existing store onto the service stack, asks what upgrade steps their install needs, is blocked on a migration, or is holding an old/dormant install that may carry era debt
effort: low
---

# Upgrade

Upgrading nexus is: update the code, then run `nx upgrade`. There is no
sequence to hold and no era to identify.

```bash
nx self install            # 1. update the code (PRESERVES extras like [local])
nx upgrade                 # 2. converge the data
```

`nx self install` builds a new side-by-side generation and flips the `current`
symlink the `nx` shims resolve through, so it is safe with live sessions
attached — holders keep running from their own generation and converge at their
next spawn. `uv tool upgrade conexus` does not touch a generation install; it is
the code-update step only on a box that has not migrated to generations yet,
where `nx self install` refuses rather than doing the wrong thing.

`nx upgrade` brings the package, engine, process, and provisioning
preconditions current, then walks one ordered ladder that auto-applies whichever
data migrations the install actually needs — T2 schema, pre-RDR-108 chunk
identity, embedder era. (The ChromaDB → Postgres+pgvector move is NOT among
them on a current release: see "Installs that predate Postgres" below.) Each
rung detects, converges, and verifies before completion is recorded; the walk is
idempotent and resumable, and the source store is left byte-untouched as a
rollback target. An install dormant since 5.x converges the same way a current
one no-ops.

## Reporting before running

- `nx doctor` — reports pending rungs read-only, plus era debt (legacy chunk
  ids) on installs that have not migrated yet.
- `nx upgrade --dry-run` — reports what would run, from each rung's read-only
  detect. Writes nothing. A rung's detect may read the completion ledger, so
  these need a reachable engine and its token to be exact; without one they
  report the rung as pending and say why.

Both are safe to run unprompted.

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

## The RDR-192 backfill rung and its failure mode

`rdr192-manifest-backfill` censuses every collection for legacy notes that
have a catalog document but no manifest row (the engine hides them from search
since RDR-192) and backfills them. It records completion, per tenant, only after
a fresh census reads zero; the engine's reaper refuses to run on a tenant
without that record; that refusal is complete only for a tenant with no earlier
record (an older record still stands, and the reaper's own in-engine census is
what covers that case). When some note cannot be backfilled the rung DEFERS
instead of failing: `nx upgrade` finishes its other steps, exits 0, prints which
collection and what to do, and `nx doctor` keeps showing the rung as pending.
No verb heals a skipped note, and its text is hidden from `nx store get`, so the
remedy needs the user's own copy: re-put it with `nx store put` under the same
title into the collection named in the message (`nx t3 census-manifest-less
--collection <c>` lists the owner titles). A residual is not re-examined for 24
hours or until the package version changes (`NX_RDR192_BACKFILL_RETRY=1` retries
now); a backfill that errors is retried at the next session start. Do not "fix"
a deferred rung by recording the completion by hand; the record is what tells
the reaper the census read zero.

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

## Installs that predate Postgres

A box carrying unmigrated pre-PG data (`chroma.sqlite3`, `t2.db`, `memory.db`,
`catalog/.catalog.db`) is refused by a current release with a two-hop
redirect, and `nx upgrade` here cannot migrate it. The path is: install the
pinned `conexus==6.18.1` (the refusal names the exact command), run `nx upgrade`
there, then upgrade back to current. Three preconditions for the hop at the pin:

- **A LOCAL engine only.** `NX_SERVICE_URL`, `NX_SERVICE_TOKEN` and the
  `service_url` config key unset (or `NX_LOCAL=1`). Never run the pin's
  `nx guided-upgrade --service-url ...` or aim it at a managed endpoint: that
  path is unsupported (the engine retired the `/v1/staging` routes it lands
  through, and it very likely already lost manifest and topic pointers on a
  current engine; unmeasured, nexus-6g218). Do not set `service_url` first "to
  save a step".
- **Stop any current-engine local service** a 7.x install left running
  (`nx daemon service stop`) so the pin provisions its own engine.
- **Voyage-embedded data needs a Voyage-keyed local engine**
  (`NX_VOYAGE_API_KEY` reaching the service), or those collections are refused
  or re-embedded to bge-768 and can never be imported into a Voyage cloud.

If the user wants the managed cloud, that is a second hop with the CURRENT
client after the upgrade back. It has NOT been rehearsed end to end (nexus-xbqh9)
and carries less than the old direct path: no T2 memory or plans (`nx memory`
has no export verb), taxonomy, `document_aspects`, aspect queue, `frecency`,
`relevance_log`, DEVONthink highlights or tuples. Pick by user type:

- Local-ONNX collections (bge-768) cannot be imported into a Voyage cloud
  collection (`nx store import` stops with `EmbeddingDimensionMismatch`, 768 vs
  1024). For code, docs and rdr content, re-index from source in the cloud
  (`nx index repo`, `nx index pdf`); that is cheaper than two hops.
- Voyage collections from a Voyage-keyed hop 1: `nx store export --all`
  locally, then `nx store import FILE` in the cloud.
- Source-less `store_put` notes: `nx catalog export recovery.jsonl` locally,
  `nx catalog import` in the cloud (re-embeds; carries links and notes only).

Order in the cloud: switch with `nx config set service_url ...`, clear the
stranded banner (`nx stranded ack`, or move the pre-PG files it names aside),
re-index from source, `nx store import`, and `nx catalog import` LAST (links
resolve by `source_uri` against documents already there). The full procedure is
`docs/migration-runbook.md` § Getting that data into the managed cloud; surface
the choices to the user rather than making them.

## Managed service

Pointing at a managed endpoint is configuration, not upgrade, and it is for an
install whose data is already on the Postgres substrate (a pre-PG box migrates
locally first; see above). Once configured, the upgrade is the same one verb:

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
