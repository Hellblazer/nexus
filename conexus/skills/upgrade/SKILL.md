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
idempotent and resumable. An install dormant since 5.x converges the same way a current
one no-ops.

## Reporting before running

- `nx doctor` — reports pending rungs read-only, plus era debt (legacy chunk
  ids) on installs that have not migrated yet.
- `nx upgrade --dry-run` — reports what would run, from each rung's read-only
  detect. Writes nothing. A rung's detect may read the completion ledger, so
  these need a reachable engine and its token to be exact; without one they
  report the rung as pending and say why.

Both are safe to run unprompted.

## What the walk asks

Nothing, today. The ladder's rungs (`manifest_section_labels`, `manifest_span_fill`, `note_tags_array`,
`rdr192_manifest_backfill`, `retire_keys_sweep`) either finish or DEFER: a deferred rung stays pending and holds back the rungs that depend on it, the walk
goes on to the rungs that do not, and it retries on a later run (`nx doctor`'s `Upgrade ladder` row says "REPEATEDLY DEFERRED:" once one keeps deferring).
No rung rolls back.

`manifest_span_fill` has no engine count to ask, so unattended (`--auto`) against a managed tenant it reads nothing and
reports not applicable. A managed tenant needs one interactive `nx upgrade` (three catalog walks) before
`nx t3 retire-keys --apply`, run with the tenant quiet: a concurrent re-index can be reverted by the write.

`retire_keys_sweep` runs last, and on a local engine `--auto` runs it (a real write). It never accepts a residue: a standing one DEFERS it. Give the user the counts (`nx t3 retire-keys --tenant T`) and what accepting each loses; the decision is theirs, never a pasted command.

If a rung ever asks a question (a billed re-embed, a choice the walk cannot derive), surface it to the user and let
them choose; never answer on their behalf.

## The RDR-229 stamp moves and the one restart

An engine at 0.1.202 or later does collection-id work in the background after it serves, and
`nx upgrade` does not drive it. The engine registers one data move per collection-bearing
table (`rdr229-stamp-<table>`); each fills the collection id on existing rows in resumable batches. They need the `pg_background` extension
([configuration, background data moves](https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md)), and they show under `boot_work` on `/v1/status`
and in `nx doctor` (the `RDR-229 completion` row, `Collection ids`, and a `data move needs an
operator` row if one keeps failing). The chunks move is the long one: about 38 hours on a
production-size store. Leave `NX_DATA_MOVE_SLEEP_MS` unset.

When every stamp move is complete the engine lists one more `boot_work` entry, `rdr229-restart`
("restart the engine once"); until it runs, a collection rename is refused (409). That restart is
the one manual step and nothing is lost by waiting: the user restarts the engine, never you. A rollback starts with the down-step
([runbook](https://github.com/Hellblazer/nexus/blob/main/docs/runbooks/rdr-229-p31-rollback.md)).

## The RDR-192 backfill rung and its failure mode

`rdr192-manifest-backfill` censuses every collection for legacy notes that
have a catalog document but no manifest row (the engine hides them from search
since RDR-192) and backfills them. It records completion, per tenant, only after
a census reads zero (each collection read in that session or kept from one at
most 24 hours earlier); the engine's reaper refuses to run on a tenant
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

Nothing converges legacy chunk ids any more: the wire re-id rung and
`/v1/remap/rekey` are retired. A current engine deletes them instead. Its boot
changeset `hygiene-001-5` (first in engine 0.1.91) deletes every chunk row whose
chash is not 32 bytes, and the manifest rows that reference those chunks, and
`hygiene-001-8` deletes the remaining legacy-width manifest rows. So an install
that has booted engine 0.1.91 or later holds no legacy rows.

An install still older than engine 0.1.91 loses those rows, `store_put`-only
notes included, the first time a current engine boots on it. Before upgrading
one, take a backup with `nx backup`, which keeps a copy of every row.

If the VALIDATE job (`POST /v1/admin/validate-constraints`) or `nx doctor`
reports legacy-width rows on a current install, the width CHECK was bypassed or
dropped and re-added. These rows are not expected, and no command repairs them.
Report them, with the documents the report names, to the operator or
maintainer.

**Never drop or weaken chash length CHECK constraints to force upserts
through** (GH #1390). If you are blocked on chash-length errors, STOP and
report; never "unblock" the constraint.

## Installs that predate Postgres

A box carrying unmigrated pre-PG data (`chroma.sqlite3`, `t2.db`, `memory.db`,
`catalog/.catalog.db`) is refused by a current release with a two-hop
redirect, and `nx upgrade` here cannot migrate it. The path is: install the
pinned `conexus==6.18.1` (the refusal names the exact command), run `nx upgrade`
there, then upgrade back to current. Three preconditions for the hop at the pin:

- **A LOCAL engine only.** Have the user write down `service_url` and
  `service_token` first; the cloud steps need both again. Then
  `unset NX_SERVICE_URL NX_SERVICE_TOKEN NX_SERVICE_HOST NX_SERVICE_PORT` in the
  shell, clear the saved `service_url` (and `service_token`) with
  `nx config unset service_url` on a client that has the verb (7.81.0 and later);
  on an older client delete those lines under `credentials:` in
  `~/.config/nexus/config.yml` by hand. Then `export NX_LOCAL=1` for
  the hop. `nx config get service_url` prints `service_url: not set` when it is clear.
  Both halves are needed. `NX_LOCAL=1` does not by itself override a configured
  `service_url` (the endpoint reads it first), and the pin's provisioning
  refuses when `is_local_mode()` is false (`NX_LOCAL=0`, `install.mode:
  managed`, or a ChromaDB Cloud key with no mode record), which `NX_LOCAL=1`
  overrides. Read from the 6.18.1 source, not run at the pin. Never run
  the pin's `nx guided-upgrade --service-url ...` or aim it at a managed
  endpoint: that path is unsupported and failed in every case measured
  (nexus-6g218, local gate jars, not the managed endpoint): against a current
  engine it stops before writing anything, and on the last build that still had
  the `/v1/staging` routes a same-model store with no document aspects was
  reported "Migration VERIFIED" while its 16- and 32-hex pointers were
  discarded.
  Do not set `service_url` first "to save a step".
- **Stop any current-engine local service** a 7.x install left running
  (`nx daemon service stop`) so the pin provisions its own engine.
- **Voyage-embedded data needs a Voyage-keyed local engine**
  (`NX_VOYAGE_API_KEY` reaching the service), or those collections are refused
  or re-embedded to bge-768 and are not expected to import into a Voyage cloud.

If the user wants the managed cloud, that is a second hop with the CURRENT
client after the upgrade back. It has NOT been rehearsed end to end (nexus-xbqh9)
and carries less than the old direct path: T2 plans, taxonomy, `document_aspects`,
the aspect queue, `frecency` and `relevance_log`, the telemetry stores, DEVONthink
highlights, curated catalog metadata and owner tumblers are re-minted, and T2
memory moves only by hand (`nx memory list`, `nx memory get`, then `nx memory put`
in the cloud, BEFORE the switch). The migration runbook
(https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md), § Getting
that data into the managed cloud, has the full list. Pick by user type:

- Local-ONNX collections (bge-768): not expected to import into a Voyage cloud, and
  what the cloud engine answers is unverified (nexus-xbqh9). For code, docs and rdr
  content, re-index from source in the cloud (`nx index repo`, `nx index pdf`).
- Voyage collections from a Voyage-keyed hop 1: `nx store export --all` locally,
  then `nx store import FILE` in the cloud.
- Source-less `store_put` notes: `nx catalog export recovery.jsonl` locally,
  `nx catalog import` in the cloud (re-embeds; carries links and notes only).

Order: hand-carry T2 memory and run the exports while the box is still local;
then `unset NX_LOCAL` and switch with `nx config set service_url ...` (and the
`service_token` you noted), clear the stranded banner (`nx stranded ack`, or move
the pre-PG files it names aside), re-index from source, `nx store import`, and
`nx catalog import` LAST (links resolve by `source_uri` against documents already
there). Surface the choices to the user rather than making them.

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
- **Do not reach for the old migration primitives.** `nx migration` is an
  internal primitive, callable but out of the user story because the ladder
  does its job; `guided-upgrade`, `migrate-to-service` and `migration-audit`
  no longer exist. If one seems necessary, that is a
  finding worth reporting, not a step to take.
- **A new upgrade verb is never the answer.** New data axes become rungs.

## Notes

Record any upgrade outcome the next session should know via `nx scratch put`
(session-local) or `nx memory put` (cross-session): a blocked verdict, a
deferred decision awaiting the user, or a clean converge.
