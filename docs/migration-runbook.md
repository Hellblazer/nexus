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
`RUNG_ORDER` (`src/nexus/upgrade_ladder/registry.py`) holds four rungs:
`rdr192-manifest-backfill`, `aspect-queue-tuples`, `note-tags-array` and
`manifest-section-labels`, each described below the table. Everything else in `nx upgrade` is precondition work:

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
and get, and the RDR-192 reaper moves it to quarantine once it has had no owner for
30 days (quarantine is deleted 14 days later; `docs/operations/engine-reaper.md`). The rung
censuses every non-quarantine collection (`nx t3 census-manifest-less`'s
route), backfills each collection that holds a `legacy-unmanifested` chunk
(`nx t3 backfill-manifest --no-dry-run --only-gapped`'s call), and records
completion in the engine's `nexus.ladder_completions` only after a census
reads zero legacy-unmanifested and zero unclassified chunks (each collection's
reading taken in that session or kept from an earlier one at most 24 hours old). It never deletes a
chunk and leaves the `superseded`, `dead-owner` and `no-owner` buckets to the
reaper.

A tenant that holds nothing is converged without a census, and the engine's
reaper gate reads at least as strictly as that branch (it counts quarantine
chunks, which the client's census skips, and a tenant with no chunk has no
manifest row because of the manifest's foreign key to `nexus.chunks`): it
passes such a tenant with no completion record, so a break-glass or
credential-only tenant needs no `nx upgrade` of its own
(`docs/operations/engine-reaper.md`, nexus-wbfpw.73).

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
  the old one becomes the reaper's to quarantine, 30 days after it lost its owner.
  If the reaper has already moved the note, see
  [Restoring chunks the reaper quarantined](#restoring-chunks-the-reaper-quarantined).
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

### The `aspect-queue-tuples` rung

RDR-235 moves aspect extraction from the `aspect_extraction_queue` table to the
engine's `aspects` tuple queue, selected by `NX_ASPECT_QUEUE=tuples`. This rung
moves the rows the table already holds, so none wait for the table side of the
worker or strand.

- It applies only when `NX_ASPECT_QUEUE` is `tuples`, the engine has the
  `aspects` template, and the engine answers
  `GET /v1/aspects/queue/list_waiting` (`engine-service-v0.1.170`; older
  engines answer 404). Any other case is reported as not applicable, nothing is
  recorded, and the rung is detected again at each `nx upgrade`, including the
  first one after the default flips to `tuples`. With the variable unset or
  `table` it sends no request.
- Each `pending` or `in_progress` row becomes one `aspects` task (an `out` with
  `mode=replace`, nonce = `doc_id`, an identity body), then the table row is
  deleted. A crash between the two leaves the row in both places; the re-run
  replaces the same task. `pending` rows keep their wait as `delay_s` (seconds to
  `next_retry_at` on the database clock, clamped to 0 through 7,776,000);
  `in_progress` rows move with no delay.
- `failed` rows stay in the table (`nx aspects requeue-failed` re-enqueues
  them). A row with an empty `doc_id` also stays, for the table worker to drain;
  a log line counts them.
- Migrated rows restart at attempts 0: `retry_count` does not travel.
- `verify()` passes only when it read the waiting rows and none can move; an
  unreachable read fails the rung with the reason.

### The `note-tags-array` rung

RDR-236 RF-17 makes a note's tags a set. In the note's catalog entry (`meta.tags`)
they are a JSON array, so a filter can match one tag of several. Clients before
nexus-6pfgb.38 stored the comma string the CLI and MCP pass, `"arch,decision"`;
this rung rewrites those strings as arrays, once.

- A note is a catalog document with `content_type` `knowledge` and no `file_path`
  in a `knowledge__*` collection. Indexed files never carried `meta.tags`.
- `detect()` is the dry run: it reads every `knowledge__*` collection's listing
  and reports how many notes hold a string, out of how many it examined. The rung
  is not applicable only when no catalog is available.
- `converge()` re-reads each such note just before writing (a put since the
  listing may have written the array, and that put wins), then sends
  `meta={"tags": [...]}` alone: the string trimmed, split on commas, de-duplicated,
  first-seen order kept; `""` becomes `[]`. The engine merges `meta`, so no other
  key changes. An entry already holding an array, or no `tags` key, is left alone,
  so a re-run does nothing. Batches of 500 resume on the next call.
- It is exempt from the RDR-236 backfill's fill-only rule on purpose: the backfill
  never rewrites a value it finds, and this conversion rewrites the old spelling of
  the same value. It runs before the backfill, which then writes arrays.
- `verify()` passes only when every `knowledge__*` listing was read and no note
  holds a string; a missing or unreadable catalog fails the rung with the reason.
  The recorded detail names how many notes it examined and how many it converted.
- Until it has run, readers tolerate the string: `store_get`, `store_list` and the
  recovery-bundle export split it on read.

### The `manifest-section-labels` rung

catalog-046 gave every manifest row its own section label (`section_title`,
`section_type`, `page_number`) and a `labels_written` flag. A row written before it
is *legacy*: NULL labels, flag false, and readers fall back to the shared T3 chunk
row's label, which belongs to whichever document wrote that chunk last. `doc_indexer`
skips unchanged files, so a re-index never fills them. This rung fills the rows where
the chunk row's label is exact, once (nexus-1nrcu).

- **The rule.** A legacy row takes the chunk row's label only when its chash has
  exactly one live manifest row in the tenant, and that row is this one. One *owner*
  is not enough: a document that holds a chash at two positions has one owner and two
  rows, and the chunk row holds one label. Rows of any other chash stay legacy.
- **Four more guards.** The chunk row must carry at least one of the three label
  keys (a writer that predates labels omitted them; copying `""` would say "no
  heading" where the truth is "never recorded"). Its `indexed_at` must be no later
  than the document's plus five minutes (a later row was written by someone else).
  Its `title` (the last writer's) must name this document: the document's title, its
  file path or file name, with or without the `:12-30` / `:chunk-3` / `:page-2`
  suffix the indexers add; a row with no title is not contradicted, and one that
  names another file stays legacy and is counted as "chunk row not attributable".
  Just before each write the batch's manifests and ownership are read again; a row
  that became written, moved or gained a co-owner is skipped.
- **Fill-only.** A row already `labels_written` is never touched, an empty label
  included. The rung never writes a label with the flag false.
- **Known gap.** Ownership is read from live manifests, so a *tombstoned* document
  that holds the chash is not counted; no client read lists a tombstoned document's
  rows. The `indexed_at` and `title` guards are the defence: they cover a former
  owner whose chunk row was written more than five minutes after the document, or
  carries another file's title. They do not cover one whose row has no title or the
  same title (nexus-6pfgb.58 closes it with an engine read). The same gap is
  documented on `nx catalog backfill-meta`.
- **Cost.** `detect()` is the dry run and does the whole scan with no write: it
  reads every live document's manifest and the chunk rows of the legacy single-row
  chashes, 50 documents at a time. `converge()` writes through `append_manifest_many`,
  one request per collection per 50 documents and per 300 rows (a document's rows spill into the next request). The engine restamps a document's
  `indexed_at` on every manifest write; the rung writes each filled document's
  stamp back to the value it read at scan, so the attribution guard above and
  `nx catalog backfill-meta`'s are unaffected and the order of the two does not
  matter. A document whose stamp cannot be written back is named and the run is
  DEFERRED.
- **Reading the result.** The recorded detail counts, apart: rows already written,
  legacy rows, rows filled, and the legacy rows left alone with the reason (shared
  with another document, repeated inside the document, chunk without label keys,
  chunk missing, chunk row not attributable, changed meanwhile, write failed). An
  empty tenant converges having examined 0 documents, and says so. The rung is not
  applicable when no catalog is available or the engine has no
  `/manifest/chash_positions` route.

### Restoring chunks the reaper quarantined

`nx t3 quarantine restore` brings back chunks the engine reaper moved to a `quarantine-` collection, by chash, `gc_audit` id or date window, and with `--reattach` (the default) makes them visible again when their own metadata names a live document.
The whole procedure, including what to do about a chunk that comes back hidden (a file collection's chunks need the file re-indexed, not a re-put), is [Getting a chunk back](operations/engine-reaper.md#getting-a-chunk-back) in the reaper runbook.
The flags and exit codes are in the [CLI reference](cli-reference.md#nx-t3-quarantine-restore).

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
   - **Local engine only.** The pin must provision and talk to its own local
     engine, so clear every setting that names a managed one and force local
     mode. Write down your `service_url` and `service_token` values first: the
     cloud steps below need both again. In the shell:
     `unset NX_SERVICE_URL NX_SERVICE_TOKEN NX_SERVICE_HOST NX_SERVICE_PORT`
     and `export NX_LOCAL=1`. In the config file: on a
     client that has the verb (7.81.0 and later) run `nx config unset service_url`
     (and `nx config unset service_token` if one is saved); on an older client
     delete the `service_url:` line (and `service_token:`) under `credentials:`
     in `~/.config/nexus/config.yml` (`$NEXUS_CONFIG_DIR/config.yml` if you set
     that variable) by hand. Leave every other key alone. `nx config get service_url` prints
     `service_url: not set` once it is clear. A machine that ran in cloud
     mode has the key set. **Both halves are needed.** Clearing `service_url`
     alone is not enough, and `NX_LOCAL=1` alone is not enough. A configured
     `service_url` names the engine at the pin whatever `NX_LOCAL` says, so
     `NX_LOCAL=1` does not by itself override it. And the pin's provisioning
     (`provision_service_stack`) returns without starting an engine, and the
     upgrade then stops with "guided-upgrade provisioning requires a LOCAL
     service", whenever `is_local_mode()` is false: that is the case under
     `NX_LOCAL=0`, under `install.mode: managed` in `config.yml` (written by
     `nx init`'s managed path), and for a box with a ChromaDB Cloud key
     (`chroma_api_key`) and no mode record, which is the Voyage-from-ChromaDB-Cloud
     case in the table below. `NX_LOCAL=1` is the top-precedence step of
     `is_local_mode()`, so it fixes all three, and it does not change which
     Chroma store the migration reads. (This is read from the v6.18.1 source:
     `config.py` `is_local_mode`, `commands/init.py` `provision_service_stack`,
     `upgrade_ladder/provisioning.py` `provision_and_serve`. It has not been run
     at the pin on a box with `install.mode: managed`.) Never
     run the pin's `nx guided-upgrade` with `--service-url`, and never aim
     `nx upgrade` at a managed endpoint. That path is unsupported, and it
     failed in every case measured (nexus-6g218, T2
     `nexus/6-18-1-upgrade-cloud-posture-2026-10-17`: gate jars of the last
     commit with the `/v1/staging` routes and of develop, over loopback http,
     bge posture, one tenant; the managed endpoint itself was not used).
     `nx upgrade` stopped before writing anything ("upgrade ladder did not
     converge ... HTTP 422: collection ... is not registered").
     `nx guided-upgrade --service-url` refused at its readiness check ("lease
     is NOT discoverable") when no local service lease existed; with a
     synthetic lease standing in for a running 6.x service it got past that
     check (behaviour with a real local service plus a managed URL was not
     measured) and then also needed `NX_SERVICE_TOKEN` exported. Past both,
     the develop engine, which has no `/v1/staging` routes (retired in
     nexus-z0o2p.27), answered the first landing call with HTTP 404 and
     nothing was written. On the last build that still had those routes, a
     store whose collections keep their embedding model and has no document
     aspects was reported "Migration VERIFIED" (on a rerun, after the first run
     failed with a taxonomy HTTP 422) while every catalog manifest
     row, topic assignment, frecency and relevance_log row keyed by the 16- and
     32-hex ids measured was discarded; a store built by 6.18.1's own seeder,
     with aspects and model-changing collections, failed loudly instead (HTTP
     422 and 409). Use the local path above.
   - **Stop any current-engine local service first.** A 7.x install may have
     left its local service running (`nx daemon service status`; stop it with
     `nx daemon service stop`). The pin's own engine (v0.1.52) must be the one
     it provisions, not a newer engine that no longer has the routes
   - **Use a Postgres data directory no 7.x engine has booted on.** The nx-managed
     cluster (`<pg_data>`: `<config>/postgres`, which is `~/.config/nexus/postgres`
     unless `NEXUS_CONFIG_DIR` is set; the `PG_DATA` key of `<config>/pg_credentials`
     names it, and it holds `pg.log`) is shared by every engine version on the box, and an engine migrates the schema at boot. A 7.x
     engine boot on that cluster has already applied the RDR-191 unified-chunks
     changesets (the per-dimension `chunks_384/768/1024` tables folded into
     `nexus.chunks`), and a boot of an engine at or after the staging-retirement
     tag (nexus-z0o2p.27) has already dropped the `staging` schema, which the
     pin's `/v1/staging` landing writes into. The pin's v0.1.52 engine cannot undo
     either, and it is not expected to land through those routes on such a
     cluster. So: if a 7.x engine has run on this box, do not point the pin at
     that cluster. Move the cluster aside (stop the service first, rename
     `<pg_data>`; keep the old directory until the hop is done, because it also
     holds whatever the 7.x install stored) so the pin should initialise a fresh one,
     and expect the 7.x data to be absent from the new cluster. Leave
     `<config>/pg_credentials` in place: its `PG_DATA` names `<pg_data>`, which no
     longer exists after the rename. In the current `provision()` (the pin's copy
     of that function was not read) the missing `PG_VERSION` marker skips the
     already-running shortcut, the port and passwords come from that file, initdb
     runs at `<pg_data>`, and `PG_DATA` is rewritten. **All of this is read from
     the current changelog and source, not measured**: nobody has booted
     v0.1.52 against a cluster a current engine already migrated, or run the
     pin's provisioning after the rename, and counted what it did, and there is
     no measured alternative to the fresh cluster. A box
     that never ran a 7.x engine is not affected
   - **A Voyage-embedded source needs a Voyage-keyed local engine.** If the
     Chroma data is Voyage-embedded (a ChromaDB Cloud store, or any
     `voyage-*` collection), run the local engine with `NX_VOYAGE_API_KEY`
     reaching the service. Without it a voyage-model collection is refused
     (the migration will not copy Voyage vectors onto a bge-only service) or
     re-embedded to bge-768, and a bge collection is not expected to import into
     a Voyage cloud afterward. This is the most consequential choice in the hop
     for anyone headed to the cloud. Whether the pin's v0.1.52 engine runs the
     Voyage posture has not been verified, and neither has what a current local
     engine without the key does with a voyage-named collection when you export
     it later (nexus-xbqh9)
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
something. Hop 1 aimed at a current engine failed before writing anything in
every case measured, and on the last staging-era build one store shape lost its
pointers under a success message (nexus-6g218; see step 2 above for what was
measured and how).

**Pick the path by what the data is:**

| You are | Hop 1 gives you | Best path to the cloud |
|---|---|---|
| Local-ONNX (minilm-384 or bge-768 collections) | Collections embedded with a local model; T2 memory and plans; taxonomy; notes | Do not carry the vectors: a bge collection is not expected to import into a Voyage cloud (see below). For code, docs and rdr content, **re-index from source in the cloud** (`nx index repo`, `nx index pdf`, `nx index rdr`), which is cheaper than two hops and embeds with Voyage. Hop 1 matters only for T2 memory (hand-carried, step 2 below) and notes; plans and taxonomy do not reach the cloud |
| Voyage, from ChromaDB Cloud | Voyage collections kept as Voyage only if hop 1 ran Voyage-keyed (above) | `nx store export` and `nx store import` carry the vectors as they are. Re-indexing source content is still the cheaper path where you have the source |
| Notes with no source files (`store_put`-origin knowledge) | The notes, as chunks plus catalog documents | `nx catalog export` and `nx catalog import`: the bundle holds no embeddings, so import re-embeds each note and works across models |

**Before you switch to the cloud (local steps).** Do these while the
current client still talks to your local engine; once you switch, the local
engine's data is unreachable from the client.

1. Write down what you want to carry: `nx store export --all -o ./nxexp-backup/`
   (one `.nxexp` per collection, embeddings included) and
   `nx catalog export recovery.jsonl` (link graph plus `store_put`-origin
   notes; no embeddings).
2. Hand-carry the T2 memory entries you cannot lose: `nx memory list` to see
   them, `nx memory get --project NAME --title NAME` to read one, and later
   `nx memory put CONTENT --project NAME --title NAME` in the cloud. `nx memory`
   has no export or import verb, and doing this by hand does not scale past tens
   of entries. Past that, plan on treating T2 memory as left behind, or write a
   loop yourself. The hand-carry is lossy: `get` prints the content only, and
   `list` shows the id, project/title, agent and timestamp, so tags, agent, TTL
   and timestamps are not carried unless you retype them (`--tags`, `--ttl`);
   quarantined entries are hidden from `list` unless you ask for them
   (`nx memory list --quarantined`). Read one entry by id (`nx memory get ID`,
   the id `list` prints in brackets) rather than by `--title`, because a title
   is matched as a unique prefix and a prefix shared with another entry fails as
   ambiguous. On the way in, pass the body on stdin (`nx memory put - --project
   NAME --title NAME < body.txt`) rather than in argv, so a body that starts with
   a dash or is very large does not trip the shell or the argument-length limit.

**After you switch (cloud steps).**

3. Switch as in
   [Getting Started § Cloud mode](getting-started.md#cloud-mode-optional)
   (`nx config set service_url ...` plus `NX_SERVICE_TOKEN`), using the
   `service_url` and `service_token` you wrote down before hop 1. First
   `unset NX_LOCAL` (and drop it from your shell profile if you added it
   there): `NX_LOCAL=1` wins over `service_url` in current clients too, so with
   it set the client would stay local after you set the endpoint.
4. **Clear the stranded banner.** The pre-PG files from hop 1 are still on
   disk (copy-not-move), and in cloud mode the stranded-install detector
   cannot trust the engine's migration record, so every command banners and
   `nx doctor` fails until you either run `nx stranded ack` (attests that this
   machine's pre-PG data was migrated) or move the pre-PG files the banner names
   aside. They are relics (Sam, 2026-08-29).
5. **Re-index from source first, then load.** `nx index repo` / `nx index pdf`
   for the content you are re-indexing; then `nx store import FILE` for each
   `.nxexp` you are carrying; then `nx catalog import recovery.jsonl` LAST. The
   bundle's links resolve by `source_uri` against documents the target already
   holds, so a link to a document not indexed yet stays unresolved. An
   interrupted `nx store import` finishes the documents it left open when rerun.
   Idempotence of the catalog import and of a re-index over an import is
   unmeasured (nexus-xbqh9).

**What happens to a collection embedded with a local model.** Hop 1 renames a
local-model collection to a bge-named one (the pin swaps the model segment of
the name for minilm sources and for voyage-named collections whose vectors
measure 768). That name passes `nx store import`'s name check and its dimension
check (768 equals 768), so the file is sent to the cloud engine. The cloud
engine is a Voyage deployment and may refuse a collection whose name does not
carry a Voyage model. Nobody has run this, so what the cloud answers is
unverified (nexus-xbqh9). `EmbeddingDimensionMismatch` ("header
claims 'voyage-context-3' (1024-dim) but vectors are 768-dim") is a different
case: a file whose header says Voyage over vectors that measure 768.
`--assume-model` only corrects a mislabeled header like that and does not get
past a real mismatch. Either way the answer for a locally-embedded collection is
to re-index its source.

**What this path does not carry.** An `.nxexp` holds chunks, embeddings and
owner rows. The recovery bundle holds links and `store_put`-origin notes. Nothing
here carries:

- T2 memory and plans, except the entries you hand-carry in step 2 (plans have
  no hand-carry path)
- taxonomy topics, their assignments, topic links and centroids
- `document_aspects` (LLM-extracted; re-extracting is billed), the aspect queue
  and `aspect_promotion_log`
- `frecency` and `relevance_log`. A note's TTL is not in those tables: the
  `.nxexp` carries each chunk's `ttl_days` and `indexed_at` as stored (a shared
  chunk holds the last writer's), and the recovery bundle, imported last, gives
  each `store_put` note its own `ttl_days` and restarts its clock at the import
  (a note already expired but not reaped comes back with a full TTL)
- the telemetry stores (search telemetry, hook and index failure records)
- DEVONthink highlights
- curated catalog metadata beyond links and `store_put` notes: author, year,
  corpus, `meta` fields and collection supersession. Owner tumblers are
  re-minted in the cloud, so a tumbler you noted on the old box does not name
  the same owner there

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

## If the RDR-231 note-URI backfill halts startup

**Symptom.** The new engine does not come up. `nx daemon service start` exits 2,
within a few seconds when it starts the supervisor directly and after 60 seconds
when it starts it through an autostart unit (launchd, systemd, Task Scheduler),
and the upgrade's own finish pass reports a service that did not start or is not
answering (see "How the halt first shows up" below for the exact text). The
engine's native log (`~/.config/nexus/logs/storage_service_native.log` on a local
install, the deployment's engine log in the cloud) contains the reason: the
message below appears in it several times, once for each link of the exception
chain, with stack frames between the copies.

```text
RDR-231 note-URI backfill halted and nothing was changed: N live chroma:// note row(s) have a live nx-note:// twin. ...
```

**What it means.** Changeset `note-uri-001-1` rewrites the stored `source_uri` of
every note from `chroma://<collection>/<title>` to `nx-note://<collection>/<title>`
(and, inside an export identity, `nxexp://<target>/chroma://...` to
`nxexp://<target>/nx-note://...`), in `catalog_documents`, `document_aspects`
and `document_highlights`. It stops when one note is stored under both spellings
as two live catalog documents (a pair): the rewrite would give both the same
`source_uri`, which `ux_catalog_documents_live_source_uri` forbids, and RDR-231
does not let a changeset merge documents on its own. `N` counts the
`chroma://` side of each pair.

**Nothing was changed.** The changeset runs in one transaction. The halt rolled
it back whole: no row changed, and row-level security is still forced on all
three tables. Because Liquibase runs before the HTTP server binds, the halted
engine serves nothing, so it cannot run the merge itself. The merge runs on the
previous engine, which still serves the unchanged database. Starting the
previous engine here is allowed: RDR-231 Decision 1 forbids running an engine
below the release over rows the backfill has rewritten, and an aborted backfill
rewrote none. (Changesets of the same engine tag that sit before
`note-uri-001-1` did commit. The previous engine starts over that schema and is
used only to merge, then stopped.)

**1. List the pairs.** Run this as a role that bypasses row-level security: the
superuser of the nx-managed cluster locally, the operator's diagnostic role in
the cloud. It is the guard's own test, one row per pair.

Locally the cluster listens only on TCP at `127.0.0.1` (no Unix socket) and
every `pg_hba.conf` line is `scram-sha-256`. The superuser role is named for the
OS login that provisioned the cluster (`$USER`), not `postgres`. Its password is
`PG_SUPERUSER_PASS` and the port is `PG_PORT`, both in `<config>/pg_credentials`
(`~/.config/nexus` unless `NEXUS_CONFIG_DIR` says otherwise). Use the PostgreSQL
client nx extracted under `<config>/pg-bundle`; nx never falls back to a host
`psql`. The cluster keeps running when the service stops, so this works while the
engine is halted. This opens a session without printing the password or putting
it on a command line. **It is for Linux and macOS.**

```bash
CFG="${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}"
PGPASSWORD="$(sed -n 's/^PG_SUPERUSER_PASS=//p' "$CFG/pg_credentials")" \
  "$CFG/pg-bundle/bundle/bin/psql" -h 127.0.0.1 \
  -p "$(sed -n 's/^PG_PORT=//p' "$CFG/pg_credentials")" -U "$USER" -d nexus
```

Three ways this can differ on your machine:

- **Windows.** The command above does not apply. The superuser role there is
  `nx_` plus the first 16 hex digits of the SHA-256 of the service account's SID
  (`windows_superuser_name` in `src/nexus/db/pg_provision.py`), not your login
  name, and the shell syntax is POSIX. A verb that runs this for you is planned
  (nexus-bivz3.28); until then there is no copy-and-paste command for Windows.
- **`role "<you>" does not exist`.** The cluster was provisioned from a process
  with neither `USER` nor `LOGNAME` set (a service manager, `cron`), and
  `bootstrap_superuser` falls back to `postgres` for the role. Use `-U postgres`
  with the same password.
- **A password prompt nobody can answer.** A cluster that predates
  `PG_SUPERUSER_PASS` was `trust` with no password. The supervisor converts it to
  `scram-sha-256`, with a generated password written to `pg_credentials`
  (`harden_cluster_auth`), the first time it provisions the cluster, and it does
  that before it starts the engine, so by the time you see this halt the key is
  there. If `pg_credentials` still has no `PG_SUPERUSER_PASS` line, nx refuses to
  loosen `pg_hba.conf` (`PgAuthLockedError`); its message says to restore the key
  from a backup of `pg_credentials`, or to reset the role's password in
  single-user mode and record it there.

Paste the query below at the psql prompt.

```sql
WITH o AS (
  SELECT tenant_id, tumbler, source_uri,
         CASE WHEN starts_with(source_uri, 'chroma://')
              THEN 'nx-note://' || substr(source_uri, 10)
              ELSE regexp_replace(source_uri, '^(nxexp://[^/]*)/chroma://', '\1/nx-note://') END AS new_uri,
         CASE WHEN starts_with(source_uri, 'chroma://')
              THEN split_part(substr(source_uri, 10), '/', 1)
              ELSE substring(source_uri from '^nxexp://[^/]*/chroma://([^/]*)') END AS coll
    FROM nexus.catalog_documents
   WHERE deleted_at IS NULL
     AND (starts_with(source_uri, 'chroma://') OR source_uri ~ '^nxexp://[^/]*/chroma://'))
SELECT o.tenant_id, o.tumbler AS chroma_row, t.tumbler AS nx_note_row, o.source_uri
  FROM o
  JOIN nexus.catalog_documents t
    ON t.tenant_id = o.tenant_id AND t.deleted_at IS NULL AND t.source_uri = o.new_uri
 WHERE NOT (starts_with(o.coll, 'rdr__') OR starts_with(o.coll, 'docs__') OR starts_with(o.coll, 'code__'))
 ORDER BY o.tenant_id, o.tumbler;
```

**2. Merge each pair on the previous engine.** The merge moves identity, not
content. In one transaction it clears the duplicate's `source_uri`, gives that
`source_uri` to the canonical only if the canonical has none (a canonical that
already has one keeps its own), sets the duplicate's `alias_of` to the canonical,
and re-points the duplicate's links at the canonical (`CatalogRepository.mergeDocuments`).
It touches only `catalog_documents` and `catalog_links`: it does not move the
duplicate's chunks and does not change either row's collection.

Compare the two rows of a pair before you merge, because you cannot after.
`nx catalog show <tumbler>` follows `alias_of`, so once the merge has run, showing
the duplicate prints the canonical's card. Before the merge, run it on both rows
and compare `Chunks`, `Collection` and `Indexed`, and read the text each row holds
(`nx store list` lists the notes with their IDs; `nx store get <id>` prints one).
Keep, as the canonical, the row whose text you want. That is usually the
`nx_note_row` (the newer write), but a note stored by `nx store put` before the
upgrade has its chunks on the `chroma_row`, and a `nx_note_row` registered without
content shows `Chunks: 0` and an empty `Collection`. Merging the chunk-holding row
into such an empty one leaves the note's lookups resolving to a row with no chunks.

Then merge the other row (the duplicate) into the one you keep, in the tenant that
owns both. The CLI call is `nx catalog merge <duplicate> <canonical>` and the
engine route is `POST /v1/catalog/merge` with
`{"duplicate": "<tumbler>", "canonical": "<tumbler>"}`. Run the step 1 query
again: it must return no rows.

If both rows have chunks, the merge does not combine them. The duplicate stays a
live catalog row, and a chunk is live while any live document owns it (a manifest
row in the chunk's own collection; `nexus.chunk_live_owners`, which does not look
at `alias_of`). So the duplicate's chunks still come back from `nx search` and
`nx store get`, and still appear in `nx store list`, next to the canonical's. If
you would rather keep both versions, do nothing: the other version stays
searchable under its own tumbler.

To drop the other version's chunks from reads, delete the duplicate after the
merge with `nx catalog delete <duplicate>`. It tombstones only that row (the
engine does not follow the alias for a delete). Chunks that no other live document
owns then stop appearing in search and `store get`; a chunk with identical text in
the same collection that the canonical also owns stays. The chunks remain stored:
`nx catalog restore <duplicate>` brings them back until `nx catalog purge-trash`
reclaims them after its grace window. The confirmation prompt prints the
canonical's title next to the duplicate's tumbler, because the prompt resolves
through the alias; the tumbler is what is deleted.

**3. Restart on the new engine.** The backfill then passes. Three checks that it
did:

- `nx daemon service status` lists `service_release_version` as the new engine's
  version (the `REQUIRED_ENGINE_VERSION` in `src/nexus/engine_version.py`).
- In psql (step 1), `SELECT id, exectype FROM public.databasechangelog WHERE id =
  'note-uri-001-1'` returns the row with `EXECUTED`. Liquibase's own tables are in
  the `public` schema, not `nexus` (`nexus.databasechangelog` does not exist). The
  query returns no row while the backfill has not run.
- `nx catalog show <the canonical>` prints `URI:` with an `nx-note://` spelling.

### Cloud

1. Keep, or put back, the previous engine release (`engine-service-vX.Y.Z`, the
   tag the cloud ran before this deploy) serving the tenants. A deploy that
   halted at the changeset never replaced it if the old instance was kept until
   the new one reported healthy; otherwise redeploy the previous tag.
2. Run the query from step 1, then step 2 per tenant with that tenant's token
   from a cloud-mode box. The pre-tag census counts these pairs ahead of the
   deploy, so a halt in production means the census was skipped or the data
   changed since.
3. Deploy the new engine tag again and run the cloud gate.

### Local install

A local install has no operator and no census; the client's pinned engine
(`REQUIRED_ENGINE_VERSION` in `src/nexus/engine_version.py`) is the new one, so
the previous engine is the tag your install ran before the upgrade. Its tag is
in the previous client release's changelog entry (`engine-service-v0.1.165` for
the client release before RDR-231).

```bash
nx daemon service stop                                   # normally prints "already stopped": the supervisor exited with the halted engine. Harmless, and it makes sure nothing is retrying
nx daemon service install-binary engine-service-v0.1.165 --no-pg-bundle   # the PREVIOUS engine tag, binary only
nx daemon service start                                  # previous engine, same database
# step 1: list the pairs (psql over TCP as the superuser, see step 1 above)
nx catalog merge <duplicate> <canonical>                 # step 2, once per pair
nx daemon service stop
nx daemon service install-binary <the new engine tag>    # the tag in the new client release's changelog entry
nx daemon service start                                  # the backfill passes; check logs/storage_service_native.log
```

**How the halt first shows up.** On a local install you usually meet it in the
output of the upgrade itself. The first `nx` command after `uv tool upgrade` runs
the upgrade-finish pass, which installs the pinned engine and starts it; a halted
engine never answers. What `nx daemon service start` prints in that case
(the lines are a rehearsal's; the first is stderr noise from a box with no
`systemctl`):

```text
Not starting through the autostart unit /home/nexus/.config/systemd/user/nexus-service.service (no_manager: systemctl not found on PATH or at /usr/bin/systemctl, /bin/systemctl); starting the supervisor directly.
Error: Storage service supervisor exited with code 2 before the service became ready.
Schema migration failed on port 47055: 2026-10-29 13:56:09,901 ERROR [main] dev.nexus.service.Main - event=schema_migration_failed error="Liquibase migration failed". Engine log: /home/nexus/.config/nexus/logs/storage_service_native.log
The engine reported: RDR-231 note-URI backfill halted and nothing was changed: 1 live chroma:// note row(s) have a live nx-note:// twin. Start the previous engine binary, merge each pair with POST /v1/catalog/merge, then restart on the new engine. Steps for cloud and local installs: https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md#if-the-rdr-231-note-uri-backfill-halts-startup
Supervisor log: /home/nexus/.config/nexus/logs/storage_service.log
Supervisor stderr: /home/nexus/.config/nexus/logs/storage_service.crash.log
```

The finish pass carries that text, up to its first 1,000 characters, in a `NEEDS
HUMAN` line (wrapped here for the page; the line is one):

```text
NEEDS HUMAN: the storage service did not start (`nx daemon service start` exited 2); the engine
is NOT converged to v0.1.167. Its reason: Storage service supervisor exited with code 2 before
the service became ready. Schema migration failed on port 47055: 2026-10-29 13:56:09,901 ERROR
[main] dev.nexus.service.Main - event=schema_migration_failed error="Liquibase migration
failed". Engine log: /home/nexus/.config/nexus/logs/storage_service_native.log The engine
reported: RDR-231 note-URI backfill halted and nothing was changed: 1 live chroma:// note row(s)
have a live nx-note:// twin. Start the previous engine binary, merge each pair with POST
/v1/catalog/merge, then restart on the new engine. Steps for cloud and local installs:
https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md#if-the-rdr-231-note-uri-backfill-halts-startup
Supervisor log: /home/nexus/.config/nexus/logs/storage_service.log Supervisor stderr:
/home/nexus/.config/nexus/logs/storage_service.crash.log Run `nx daemon service start` to see it
again, read the engine log, and check `nx doctor`. [cycle] stop rc=0: Storage service stopped
(pid(s)=352). | start rc=2: Not starting through the autostart unit
/home/nexus/.config/systemd/user/nexus-service.service (no_manager: systemctl not found on PATH
or at /usr/bin/systemctl, /bin/systemctl); starting the supervisor directly. Error: Storage
service supervisor exited with code 2 before the service became ready. S
```

On a box whose service runs under an autostart unit (launchd on macOS, systemd
on Linux, Task Scheduler on Windows), `start` goes through the unit and waits 60
seconds for the service, so its first line is instead `The autostart unit
<unit> was started but the storage service did not become ready within 60s;
the service manager keeps restarting the unit.` (on Windows: `Task Scheduler
restarts the task when it fails.`), followed by `The supervisor reported:
Schema migration failed ...` and the same `The engine reported: RDR-231
note-URI backfill halted ...` line, then the log paths. Because that start only
timed out waiting, the finish pass cannot tell a halt from a slow migration and
says so: its line reads `NEEDS HUMAN: ... nothing answers yet; the service may
still be migrating ... UNVERIFIED`, with the same reason text. The `The engine
reported:` line is the one that decides it: if it names the RDR-231 halt, the
engine is not migrating, it has stopped, and the steps below apply.

A later `nx daemon restart-stale` installs and starts nothing: the pinned engine
is already on disk, and a stopped service is an ordinary state that command does
not restart on its own. Its engine line is neutral:

```text
engine: installed engine is v0.1.167 (the release dependency); the service is not running (no
discoverable storage-service lease). Start it with `nx daemon service start` (its output names
the reason if the engine halts at startup).
```

It says only that the service is not running. `nx daemon service start` is what
prints the halt, so run it, or read the engine log.

Client 7.88.0 printed `engine: converged` on that line, from the installed binary
alone, and its finish line said the service "restarted but its version could not
be confirmed" with the start output cut at 300 characters, so neither named the
halt. The fix is first in the next client release after 7.88.0. On 7.88.0,
`nx daemon service start` still prints the supervisor's `Schema migration failed
... Liquibase migration failed. Engine log: <path>`; read the RAISE in the log it
names, then follow the steps above.

`install-binary` also swaps the PostgreSQL bundle by default (`--pg-bundle`), and
the previous tag ships the bundle it was built with, which can be an older
PostgreSQL than the one that last opened this data directory. The first call
therefore passes `--no-pg-bundle`: it installs the previous engine binary and
leaves the PostgreSQL you have in place. The second call, for the new tag, takes
the default.

Do not run `nx daemon restart-stale` or any upgrade step between the two
`install-binary` calls: they would put the pinned engine back before the merge.

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
