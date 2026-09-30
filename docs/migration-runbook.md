# Upgrading nexus

`nx upgrade` is the upgrade. There is no order of operations to hold, no
window to schedule, and nothing to drive by hand.

This file used to be the operator's manual for the one-time SQLite/Chroma to
Postgres migration (RDR-152/153/155). That migration cannot be performed by
this release at all — the Chroma read path, the `nx storage migrate` verb
group, `nx guided-upgrade`, and `nx migrate-to-service` were deleted by
RDR-155 P4b, and the ladder has been rung-less since nexus-lgdel.l1. The
narrative, the failure playbooks, and the rollback procedures for that window
are in git history and in the 6.18.1 release, which is where they still apply.
What is left here is what a normal upgrade needs.

## The normal path

```bash
nx upgrade
```

It converges a set of PRECONDITIONS and then walks the data ladder.
`RUNG_ORDER` (`src/nexus/upgrade_ladder/registry.py`) holds one rung:
`rdr192-manifest-backfill`, described below the table. Everything else in
`nx upgrade` is precondition work:

| Precondition | Converges |
|---|---|
| `package` | the installed `conexus` distribution |
| `engine` | the engine binary to `REQUIRED_ENGINE_VERSION` |
| `provisioning` | the PG roles, schema, and grants the service needs |
| `process` | the running service, restarted onto the new binary |
| `plugin-lockstep` | the Claude Code plugin against the installed package |
| `plan-library` | the builtin plan templates, reconciled against disk |

Each is idempotent and safe to re-run. An upgrade that reports nothing to do
has nothing to do.

### The `rdr192-manifest-backfill` rung

A note stored before nexus-b6enc has a catalog document but never got a
manifest row. Since RDR-192 Phase 2 the engine hides such a chunk from search
and get, and the RDR-192 reaper will delete it once it ages out. The rung
censuses every non-quarantine collection (`nx t3 census-manifest-less`'s
route), backfills each collection that holds a `legacy-unmanifested` chunk
(`nx t3 backfill-manifest --no-dry-run --only-gapped`'s call), and records
completion in the engine's `nexus.ladder_completions` only after a fresh census
reads zero legacy-unmanifested and zero unclassified chunks. It never deletes a
chunk and leaves the `superseded`, `dead-owner` and `no-owner` buckets to the
reaper.

- A tenant with no collections records at once, provided the catalog agrees it
  is empty. An empty listing over a catalog that holds manifest rows is a
  listing failure and defers.
- An unreachable engine, one older than the census route, or a census answer
  missing its bucket totals defers: nothing is recorded and the next
  `nx upgrade` retries. An answer without the totals is never read as clean.
- A legacy chunk the backfill cannot heal (its owner is registered under a
  different collection, no matching chunk, a chash divergence, a chunk-count
  mismatch, several chunks at one position) DEFERS the rung; it does not fail
  `nx upgrade`. The rest of the upgrade runs and exits 0, the walk prints the
  collections and the remedy, `nx doctor` keeps the rung pending, and nothing is
  recorded. No verb heals a skipped note. List each chunk and its owner with
  `nx t3 census-manifest-less --collection <c>`, then re-put the note with
  `nx store put` under the same title: the new chunk is manifested and the old
  one becomes the reaper's. An unchanged residual is not retried at each session
  start (a T2 note, `upgrade_ladder_state/rdr192-manifest-backfill.residual`,
  holds its fingerprint); it is retried when the residual or the package version
  changes. A file lock keeps concurrent session starts from stacking backfills.
- `detect()` never takes a census. A completion recorded at the installed
  package version is converged; otherwise the rung is pending and `converge`
  does the census. So a new package version re-derives the record once, and
  `nx doctor` and `nx upgrade --dry-run` cost one ledger read. Re-derive on
  demand with `nx t3 census-manifest-less --all --require-zero legacy-unmanifested`.
- The record's `detail` carries the census summary (collections scanned,
  quarantine skipped, per-bucket totals).
- The record is per tenant and written by whichever client runs `nx upgrade`
  against it. The reaper refuses to run on a tenant without it
  (`Rdr192BackfillGate` in the engine), so a cloud tenant no client has
  upgraded since this rung shipped stays unreaped until one does. The record is
  an attestation, not proof: the reaper must also re-check the census in the
  engine (nexus-2x9xa).

## Verifying

```bash
nx doctor
```

Zero `✗` is the bar. Warnings are worth reading rather than clearing
reflexively — each one names its own remedy.

## Installs that predate Postgres

A pre-PG install is DETECTED and refused with a two-hop redirect, because the
machinery that performed that migration no longer ships:

1. install the pinned last migration-capable release, `conexus==6.18.1`
   (`nexus.stranded_install.LAST_MIGRATION_CAPABLE`). The command depends on
   the layout this box has: `nx self install --version 6.18.1` on a
   generation install (one where `~/.local/share/nexus/tools/current`
   resolves), `uv tool install conexus==6.18.1` on a box still on the legacy
   uv tree — which is the usual state for one carrying unmigrated pre-PG
   data. `nexus.install_advice.pinned_install_command` is what picks between
   them, so the refusal banner that sent you here already names the right
   one. The pin has to be IN the command: a bare `nx self install` installs
   the newest release, which is the hop this procedure exists to avoid
2. `nx upgrade` there, which performs the Chroma to PG copy (copy-not-move;
   the Chroma directory is left on disk afterward, untouched)
3. upgrade to current normally

Frozen Chroma directories left on disk after that copy are relics, not a
rollback option: nothing in this release reads them, and there is no path
back to the Chroma/SQLite era (Sam, 2026-08-29).

## A stranded migration banner

`~/.config/nexus/migration.state` is a sentinel that long-lived readers poll,
banner-wrapping every read surface while a migration is in flight. Nothing in
this release writes it — its writers were deleted — so a sentinel you find
today is stranded from an older install and will otherwise banner forever.

```bash
nx migration                  # print the sentinel, read-only
nx migration --clear-state    # clear it
```

Clearing a `migrated-failed` sentinel is unambiguous: its writer is dead.
Clearing a `migrating` sentinel needs `--force`, since it may belong to a live
process; only do that once you know the process actually crashed.

## If the service will not start

The stack never dies silently. The evidence is in the persistent logs
documented in
[cli-reference § nx daemon service, "Observability"](cli-reference.md#nx-daemon-service-start--stop--status),
under `~/.config/nexus/` unless noted:

- `logs/storage_service.log` — supervisor lifecycle: start/exit breadcrumbs,
  service exit codes, restart attempts, PG recoveries
- `logs/storage_service_native.log` — the native service's stdout/stderr
  (banners, fatal errors)
- `logs/storage_service.crash.log` — pre-startup failures of the detached
  supervisor
- `<pg_data>/pg.log` — the nx-managed Postgres cluster

**The absence convention**: a supervisor death WITHOUT a
`storage_service_supervisor_exit` breadcrumb in `storage_service.log` means it
was killed, not that it chose to exit. Check the service log tail and `pg.log`
next. Once `nx daemon service status` is green, re-run whatever was
interrupted — the ETL paths are idempotent and re-converge on
`(tenant, collection, chash)`.

## Restoring a `pg_dump` into a scratch cluster

For a ROUTINE local reinstall you should not need any of this: run
`nx catalog export recovery.jsonl` before the reinstall and
`nx catalog import` after (see `docs/catalog.md` § Recovery bundle) — it
carries the link graph and store_put-only knowledge content, which are
exactly what the ad-hoc SQL below was invented to rescue. The `pg_dump`
path remains the answer for a full forensic restore.

Two things bite on the way to a read-only forensic restore. Both were hit
during a real recovery (GH #1419) and neither is obvious from the error text.

**Always restore with `--no-privileges`.** The dump carries `GRANT` statements
naming the nx service roles (`nexus_svc`, `nexus_diag`), which do not exist in
a scratch cluster you just `initdb`'d. Without the flag `pg_restore` emits one
`role "nexus_svc" does not exist` error per grant — 176 of them in the
reported case — none of which matter and all of which bury the errors that do:

```bash
pg_restore --no-privileges --no-owner -d nexus_scratch dump.pgdump
```

`--no-owner` is the companion flag for the same reason: object ownership also
references roles that are absent. You are reading data, not reproducing an
access-control model, so dropping both is correct rather than merely
convenient.

**Keep the scratch socket directory short.** macOS caps `AF_UNIX` paths at
**103 bytes**, and Postgres puts its socket in the data directory by default.
A cluster created under a long project-scoped path — a checkout nested a few
levels down, or anything under a sandboxed `TMPDIR` — fails to start with a
socket-path error that does not name the length limit as the cause. Point the
socket somewhere short:

```bash
pg_ctl -D "$SCRATCH_PGDATA" -o "-k /tmp/nxr" start
psql -h /tmp/nxr -d nexus_scratch          # clients need the same -h
```

Any short directory works; `/tmp/nxr` is arbitrary. The data directory itself
can stay wherever it is — only the socket path is length-bound.
