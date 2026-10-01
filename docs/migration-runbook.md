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
  different collection, no matching chunk, a chash divergence, more matched
  chunks than the registered count, several chunks at one position) DEFERS the
  rung; it does not fail `nx upgrade`. The rest of the upgrade runs and exits 0,
  the walk prints the collections, the per-class count of skipped documents and
  the remedy, `nx doctor` keeps the rung pending, and nothing is recorded. No
  verb heals a skipped note, and its text is hidden from `nx store get` and
  search, so the fix needs your own copy of the note. List each chunk with its
  owner document's title using `nx t3 census-manifest-less --collection <c>`,
  then re-put your copy with `nx store put - --collection <c> --title '<title>'`
  under the same title: the new chunk is manifested under the same document and
  the old one becomes the reaper's.
- Retries: a residual is not re-examined at all for 24 hours at the same
  package version (a T2 note, `upgrade_ladder_state/rdr192-manifest-backfill.residual`,
  holds its fingerprint and time), so a stuck tenant pays no census per session
  start. After 24 hours one census runs, and the backfill repeats only if the
  residual changed. A new package version retries at once, and
  `NX_RDR192_BACKFILL_RETRY=1` forces a retry now (use it right after a re-put).
  A backfill that raised is a different thing: it is never remembered, so the
  next session start retries it. A file lock keeps concurrent session starts
  from stacking backfills, and an unwritable config directory defers too.
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
  upgraded since this rung shipped stays unreaped until one does. That refusal
  is complete only for a tenant with NO earlier record: the gate ignores
  `package_version` and nothing revokes a record, so a tenant that recorded at an
  older version stays open even if a later census finds a residual. The record
  is an attestation, not proof; the reaper's own in-engine census (nexus-2x9xa)
  is what covers that case.

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
   the Chroma directory is left on disk afterward, untouched). Three
   preconditions, all about WHICH engine the pin talks to:
   - **Local engine only.** Leave `NX_SERVICE_URL`, `NX_SERVICE_TOKEN` and the
     `service_url` config key unset (or export `NX_LOCAL=1`), and never run the
     pin's `nx guided-upgrade` with `--service-url` or aim `nx upgrade` at a
     managed endpoint. That path is unsupported: the engine retired the
     `/v1/staging` routes the 6.x migration lands through (nexus-z0o2p.27), and
     even before that it very likely lost manifest and topic pointers on a
     current engine, which dropped `chash_alias` and the rekey route (engine
     v0.1.100) and now admits only 64-hex chashes where Chroma-era ids are 16
     or 32 hex. Nobody has measured it; do not rely on it
   - **Stop any current-engine local service first.** A 7.x install may have
     left its local service running (`nx daemon service status`; stop it with
     `nx daemon service stop`). The pin's own engine (v0.1.52) must be the one
     it provisions, not a newer engine that no longer has the routes
   - **A Voyage-embedded source needs a Voyage-keyed local engine.** If the
     Chroma data is Voyage-embedded (a ChromaDB Cloud store, or any
     `voyage-*` collection), run the local engine with `NX_VOYAGE_API_KEY`
     reaching the service. Without it a voyage-model collection is refused
     (the migration will not copy Voyage vectors onto a bge-only service) or
     re-embedded to bge-768, and a bge collection can never be imported into a
     Voyage cloud afterward. This is the most consequential choice in the hop
     for anyone headed to the cloud. Whether the pin's v0.1.52 engine runs the
     Voyage posture has not been verified
3. upgrade to current normally

Frozen Chroma directories left on disk after that copy are relics, not a
rollback option: nothing in this release reads them, and there is no path
back to the Chroma/SQLite era (Sam, 2026-08-29).

### Getting that data into the managed cloud

The data migration ends on a local engine. Reaching the managed cloud is a
separate hop made with the CURRENT client, after step 3. **This second hop has
not been rehearsed end to end.** The verbs below exist and their flags match
`--help`, and the gates named below were read in source, but nobody has run the
whole sequence against a cloud tenant and counted what arrived (nexus-xbqh9
will). Treat it as a plan, check counts as you go, and expect to find
something. What hop 1 does when aimed at a current managed engine is likewise
unmeasured (nexus-6g218).

**Pick the path by what the data is:**

| You are | Hop 1 gives you | Best path to the cloud |
|---|---|---|
| Local-ONNX (minilm-384 or bge-768 collections) | Collections embedded with a local model; T2 memory and plans; taxonomy; notes | Do not carry the vectors: a bge collection cannot be imported into a Voyage cloud collection. For code, docs and rdr content, **re-index from source in the cloud** (`nx index repo`, `nx index pdf`, `nx index rdr`), which is cheaper than two hops and embeds with Voyage. Hop 1 matters only for T2 memory and plans, notes and taxonomy |
| Voyage, from ChromaDB Cloud | Voyage collections kept as Voyage only if hop 1 ran Voyage-keyed (above) | `nx store export` and `nx store import` carry the vectors as they are. Re-indexing source content is still the cheaper path where you have the source |
| Notes with no source files (`store_put`-origin knowledge) | The notes, as chunks plus catalog documents | `nx catalog export` and `nx catalog import`: the bundle holds no embeddings, so import re-embeds each note and works across models |

**Steps**, while the box is still local:

1. Write down what you want to carry: `nx store export --all -o ./nxexp-backup/`
   (one `.nxexp` per collection, embeddings included) and
   `nx catalog export recovery.jsonl` (link graph plus `store_put`-origin
   notes; no embeddings).
2. Switch to the cloud as in
   [Getting Started § Cloud mode](getting-started.md#cloud-mode-optional)
   (`nx config set service_url ...` plus `NX_SERVICE_TOKEN`).
3. **Clear the stranded banner.** The pre-PG files from hop 1 are still on
   disk (copy-not-move), and in cloud mode the stranded-install detector
   cannot trust the engine's migration record, so every command banners and
   `nx doctor` fails until you either run `nx stranded ack` (attests that this
   machine's pre-PG data was migrated) or move the pre-PG files the banner names
   aside. They are relics (Sam, 2026-08-29).
4. **Re-index from source first, then load.** `nx index repo` / `nx index pdf`
   for the content you are re-indexing; then `nx store import FILE` for each
   `.nxexp` you are carrying; then `nx catalog import recovery.jsonl` LAST. The
   bundle's links resolve by `source_uri` against documents the target already
   holds, so a link to a document not indexed yet stays unresolved. All three
   are idempotent, and an interrupted `nx store import` finishes the documents it
   left open when rerun.

**What `nx store import` refuses, and why.** A collection embedded with a local
model fails against a Voyage collection. In the default case (a collection
named with a Voyage token, whose vectors are bge-768) the file's header agrees
with the name, so the model check passes and the dimension check stops it:
`EmbeddingDimensionMismatch` ("header claims 'voyage-context-3' (1024-dim) but
vectors are 768-dim"). `--assume-model` only corrects a mislabeled header and
does not get past that. A collection named with a bge token passes both checks
(768 equals 768) and is sent to the cloud engine; what the engine does with it
has not been verified, so do not rely on it. Either way the answer for such a
collection is to re-index its source.

**What this path does not carry.** An `.nxexp` holds chunks, embeddings and
owner rows. The recovery bundle holds links and `store_put`-origin notes. Nothing
here carries:

- T2 memory and plans (`nx memory` has no export or import verb; carry entries
  by hand with `nx memory get` and `nx memory put`)
- taxonomy topics and their assignments
- `document_aspects` (LLM-extracted; re-extracting is billed) and the aspect queue
- `frecency` and `relevance_log`
- DEVONthink highlights
- tuples

The old direct path landed the pointer stores, `document_aspects` and the aspect
queue through the staging routes and T2 through ordinary ones, so the two-hop
route delivers less than that path did when it worked. That is the cost of
retiring the staging routes.

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
