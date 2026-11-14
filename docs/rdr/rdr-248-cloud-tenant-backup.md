---
title: "Cloud-Mode nx backup: an Engine Tenant Export Saved to One File, with a Tested Restore into an Empty Tenant"
id: RDR-248
type: Feature
status: accepted
priority: high
author: Claude (linda, for Sam)
reviewed-by: self
created: 2026-11-11
accepted_date: 2026-11-11
related_issues: [nexus-f3yqd.4]
related_rdrs: [RDR-108, RDR-155, RDR-243]
---

# RDR-248: Cloud-Mode nx backup: an Engine Tenant Export Saved to One File, with a Tested Restore into an Empty Tenant

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

## Problem Statement

Sam, 2026-11-11: "We need to have nx-backup work for the cloud mode, also, too." Asked where a
cloud backup should be made, he chose an export made by the engine for one tenant over a bundle the
client assembles from the existing per-store exports.

`nx backup` (nexus-f3yqd.4) dumps the LOCAL engine's Postgres with the bundled `pg_dump`, connected as
the cluster's bootstrap superuser. A cloud install has neither: its client holds a tenant token and
talks to the managed engine over HTTP, so `nx backup` refuses with "cloud backups are on the conexus
side, not something you run here". A cloud user therefore has no copy of their own data that they hold.

Terms. A **tenant** is one account's slice of the shared cloud database; every tenant-owned table carries
row-level security (RLS), a Postgres rule that shows a session only the rows of the tenant named in the
`nexus.tenant` setting. **T2** is the notes-and-state tier (memory, plans, taxonomy and the rest), **T3**
the vector tier (`nexus.chunks`), and the **catalog** the document graph (`catalog_documents`, links,
manifests).

### Enumerated gaps to close

#### Gap 1: A cloud user cannot back up at all

`nx backup` refuses managed mode before reading anything, and nothing else writes a complete copy of a
cloud tenant to the user's disk.

#### Gap 2: The exports that do work in cloud mode are partial and not one snapshot

`nx store export --all` writes T3 collections with their embeddings; `nx catalog export` writes catalog
links and `store_put` notes, without embeddings. Nothing exports T2, document aspects, highlights, tuples
or the full catalog document rows. Run one after another they read different moments, so a write between
them leaves the copies disagreeing.

#### Gap 3: No restore path exists for a cloud tenant, so a backup would be unproven

The local runbook restores with `pg_restore` into a fresh database. A cloud user cannot reach the cloud
database, so a cloud backup is only worth something if the engine can load it, and a test proves the
round trip.

## Relationship to Prior RDRs

- **Origin: nexus-f3yqd.4 (`nx backup`, local).** Its rules carry over unchanged: no automatic backup,
  ever (Sam, 2026-11-10); `nx backup` is the only caller (`tests/test_nx_backup_pin.py`); a partial file
  is never left behind; restore never overwrites live data. This RDR adds the cloud arm and does not
  change the local one.
- **Precedent: RDR-108 (catalog/T3 split).** Chunks are content-addressed and document structure lives in
  the catalog manifest. An export must carry both sides or the restored tenant has chunks with no
  documents, or documents with no chunks.
- **Adjacent: RDR-243 (document sensitivity).** A sensitive document is the user's own data and is in the
  backup. The file is written owner-read-only and the verb says the file holds sensitive documents.
- **Adjacent: RDR-155 (engine everywhere).** One engine code path serves local and cloud, so the export
  route also works against a local engine. That is how the round trip is tested without the cloud.

## Context

### Background

The local `nx backup` shipped in 8.1.x after the 2026-11-10 incident (nexus-f3yqd) in which an upgrade
rewrote user data at first boot. Cloud users were left out because the client cannot reach the cloud
database. The tenant `nexus` exports 3.82 GB of binary COPY and 2.27M rows from the tables a backup
covers, 48,772 of them catalog documents (T2 `nexus/rdr248-cloud-spike-2026-11-11`).

### Technical Environment

- Engine: Java 25, Postgres 17 with pgvector; cloud is a self-managed PG 17.11 on arm64 EC2 behind the
  managed service. Schema by Liquibase.
- RLS: at the schema head (Liquibase `grants-nexus-diag-5`, 572 changesets) 50 parent tables carry
  `FORCE ROW LEVEL SECURITY`, keyed on `current_setting('nexus.tenant')` (`t1.scratch` on a second
  setting, `nexus.t1_tenant`), plus 40 RLS partition leaves of `nexus.chunks`, which is LIST-partitioned by
  model, then tenant. The global tables outside RLS: `embedding_models`, `install_pings`,
  `request_ledger_tenants`, `service_tokens`, `session_tokens`, `tuple_tenants`, and Liquibase's own.
  (A first count from the changesets said 60; it included tables since dropped.)
- The engine connects as `nexus_svc`: not superuser, no BYPASSRLS, not the tables' owner. It has SELECT
  and INSERT on all 50 tables and TEMP on the database, USAGE but not UPDATE on their sequences, and it
  cannot change `session_replication_role`, so it cannot switch triggers off.
- `CatalogRepository.COLLECTION_SCOPED_TABLES` plus a parity test against the live schema already keep
  one table list honest (nexus-20890); the export uses the same pattern.

## Research Findings

### Investigation

Read for this draft: `src/nexus/backup/pg_backup.py`, `src/nexus/commands/backup_cmd.py`, the Liquibase
changesets under `service/src/main/resources` (RLS and CREATE TABLE statements), `nx store export --help`,
`nx catalog export --help`, `tests/catalog/test_collection_scoped_tables_schema_parity.py`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Postgres `COPY` under RLS | No (docs only) | `COPY table TO` is refused on an RLS table; `COPY (SELECT ...) TO STDOUT` applies the policies. |
| pgvector binary I/O | No | `vector` has binary send/receive, so binary COPY should round-trip. To verify (A3). |
| Cloud ingress | Yes (conexus config, bob) | Every exchange ends at an absolute 60 s and a request body is capped at 20 MB (A1, refuted), hence the session design. |

### Key Discoveries

- **Verified (source reading):** `nx backup` refuses managed mode first (`pg_backup.resolve_target`; since nexus-csayy.3 the verb takes the cloud arm before it calls that resolver, which stays the local arm's and still refuses managed mode).
- **Verified (source reading):** the tenant is selected by the `nexus.tenant` setting in every RLS policy.
- **Verified (source reading):** credentials live only in global tables, so a tenant export cannot carry a
  token by construction.
- **Verified (local spike, 2026-11-11, T2 `nexus/rdr248-local-spike-2026-11-11`):** on chas, with two
  seeded tenants (catalog, 57 chunks with bge-768 vectors, T2, aspects, highlights, tuples, a sensitive
  document), as `nexus_svc` in one REPEATABLE READ READ ONLY transaction:
  - the tenant's visible rows equal its true rows on every populated table, and the other tenant's rows
    are invisible on all 50 tables (and absent byte-for-byte from the frames); a write committed during
    the export is not in it;
  - binary `COPY (SELECT ...) TO STDOUT` round-trips bigint, boolean, bytea, double precision, integer,
    integer[], jsonb, `vector(768)`, numeric, text, timestamptz and generated tsvector;
  - `COPY ... FROM` into any RLS table is refused ("COPY FROM not supported with row-level security"), so
    a load stages each frame in a temp table and inserts from it;
  - 21 tables key on a global serial or hashed id without the tenant, so a restore into a second tenant
    in the same database collides unless ids are reassigned; reassigning through per-table maps, with
    foreign-key children rewritten and tables loaded in foreign-key order, loaded A into an empty C with
    counts and checksums equal (outside the reassigned columns and one trigger-maintained column);
  - tuple ids hash the tenant, so tuples cannot move to another tenant by copying;
  - the seeded tenant exported 312 KB in 0.04 s, 285 KB of it chunks: size follows chunk count, about
    5 KB per bge-768 chunk.
- **Not exercised by the spike:** 29 tables that got no rows, and the types bigint[], smallint, uuid and
  the one identity column (`pdf_pipeline.pipeline_id`); all but two of those tables are classed
  `excluded` below.

### Critical Assumptions

| # | Assumption | Status | Method |
| --- | --- | --- | --- |
| A1 | The cloud front end lets one authenticated response stream for tens of minutes and several GB | **Refuted** (bob, from conexus config, 2026-11-11): the edge proxy closes every exchange at an absolute 60 s, the load balancer idle timeout and CloudFront's origin read timeout are 60 s, and the sidecar caps a request body at 20 MB | Config reading; the design below works within those limits |
| A2 | One `REPEATABLE READ READ ONLY` transaction with `set_config('nexus.tenant', T, true)` returns exactly that tenant's rows from every RLS table | Verified locally (spike); the cloud has no pooler (the engine reaches PG directly through its own Hikari pool, bob 2026-11-11), so the local result carries | Local spike |
| A3 | Binary `COPY` of the column types in these tables round-trips | Verified for every type that held data; staged load required | Local spike; the remaining types (bigint[], smallint, uuid, identity) by the Steps 4 and 6 test's seed |
| A4 | Import loads in one transaction | Verified at toy scale (57 chunks, 0.12 s) with id reassignment, foreign-key order and tuples excluded; at real scale the load cannot fit one 60-second exchange (248-research-11, inferred), so the commit is asynchronous; its time at scale is measured in Step 6 | Local spike |
| A5 | A read-only snapshot held for the export does not hurt the shared cloud database | Verified (bob, read-only cloud window, Sam's go, 2026-11-11, T2 `nexus/rdr248-cloud-spike-2026-11-11`): tenant `nexus` is 2,272,023 rows and 3.82 GB in binary COPY over the 26 data and derived tables, read from one snapshot in about 83 s (`chunks` 542,778 rows, 3.39 GB, 78 s, about 43 MB/s; the rest about 5 s). A snapshot held 60 minutes (by accident) left the xmin age near 1000 and dead tuples barely grown (`topic_assignments` 45.7k to 46.8k); no autovacuum ran in the window | Cloud measurement |
| A6 | A snapshot exported by one transaction serves many later page transactions, confined by RLS, consistent across writes | Verified locally (spike round 2, T2 `nexus/rdr248-local-spike-r2-2026-11-11`): 43 pages, writes and deletes committed between pages, concatenated pages equal the single-transaction export row for row on all 26 tables | Local spike |
| A7 | The cloud engine role has no `idle_in_transaction_session_timeout` that would kill the held transaction between page requests | Verified (bob, same window): idle-in-transaction, idle-session, statement and transaction timeouts are 0 at server level, with no role or database override for `nexus_svc`; the engine sets `statement_timeout` locally only on its search and taxonomy paths, so the export's dedicated connection sets its own bounds | Cloud settings read; engine source |

## Proposed Solution

### Approach

The engine writes the backup, because only the engine can read a tenant completely and consistently. One
read-only snapshot, scoped to the caller's tenant by RLS, is read in many short requests, each well
inside the cloud's 60-second exchange limit, and the client joins the pieces into one framed file. `nx backup` in cloud mode checks that file and
says how to keep it. A second route loads such a file into an EMPTY tenant, so a restore can never overwrite data. The
same routes run against a local engine, which is where the round trip is tested.

### Technical Design

**The table registry** (`TenantExport.TABLES`, engine). One entry per RLS parent table, built and checked
against the live catalog (`relrowsecurity AND NOT relispartition`), never a hand count. Each entry has:
- a **class**: `data` (user data, always exported), `derived` (rebuildable, exported because it is cheap
  and saves a rebuild) or `excluded` (operational state, with a reason);
- a **load order**: the topological order of the foreign keys AND of the references with no foreign key
  (the spike's first staged load failed on `nx_answer_runs.plan_id`, which refers to `plans` with no
  foreign key); there are no cycles today, and only one foreign key is deferrable, so deferring
  constraints does not replace ordering;
- its **reassigned id**, if its primary key holds a serial (20 tables: 18 key on the serial alone, and
  `catalog_links` and `pdf_pipeline` key it with the tenant), and the **columns that refer to it**, both
  foreign keys (`nx_answer_steps.run_id`, `topic_assignments.topic_id`, `topic_links.from_topic_id` and
  `to_topic_id`, `topics.parent_id`) and references with no foreign key (`nx_answer_runs.plan_id` to
  `plans.id`, `taxonomy_centroids.topic_id` to `topics.id`, the array `memory_summaries.source_ids` to
  `memory.id`, and `plans.id` inside the jsonb `nx_answer_runs.run_summary` at `run.plan_id`);
- each **trigger and when it fires**, and the **columns a trigger maintains** (`topics.updated_at` and its
  doc count, restamped when assignments load; `catalog_collections.owner_id`, recomputed from the
  collection's documents as they load), which verification skips; a table whose trigger reads another
  table during the load loads after it (`catalog_documents` after `catalog_owners` and
  `catalog_collections`).
A parity test fails when a live RLS table has no entry, when a foreign key or serial primary key is not
reflected in the entry, when an entry names a column that no longer exists, when a trigger is added or
changes when it fires, or when a bigint, bigint[] or jsonb column of an exported table is not classified
as a reference or as not one (bead nexus-csayy.1).

The classes, from the spike (one reason each in the spike record):
- `data`: catalog_owners, catalog_collections, catalog_documents, catalog_document_chunks, catalog_links,
  catalog_meta, catalog_sensitivity_changes, chunks, embedding_profile, memory, memory_summaries, plans,
  document_aspects, document_highlights, topics, topic_assignments, topic_links, taxonomy_meta,
  claude_assisted_remediation_consents.
- `derived`: frecency, taxonomy_centroids, search_telemetry, relevance_log, nx_answer_runs,
  nx_answer_steps, chunk_orphaned_at.
- `excluded`: in-flight work (pdf_pipeline, pdf_pages, pdf_chunks, reembed_jobs, reembed_job_moves),
  coordination state whose ids hash the tenant and whose claims would be stale (tuples, tuple_claim_log,
  tuple_deliveries), session scratch (t1.scratch), and operational logs (agent_dispatches,
  aspect_promotion_log, canary_query_ring, capability_census, catalog_reconcile_runs and _rows,
  chash_remap, gc_audit, hook_failures, index_failures, ladder_completions, request_ledger,
  retention_markers, routing_events, tier_writes).

**Export session.** The cloud edge ends every HTTP exchange at 60 seconds and caps a request body at
20 MB (A1), so no single request carries a backup. An export is a session made of short requests, all
reading one snapshot:
1. `POST /v1/tenant/export` (the caller's own tenant token; no admin token, no other tenant). The engine
   opens a dedicated connection, begins a `REPEATABLE READ READ ONLY` transaction as the engine role, and
   calls `pg_export_snapshot()`, and NOTHING ELSE: that transaction stays open for the session's life and
   must read no table. A read there holds AccessShareLocks on every table it touched until the session
   ends, and tenant create, which needs an AccessExclusiveLock on a model-level `chunks` partition, then
   fails for every tenant (spike round 2: 503 `tenant_creation_busy` after 3.45 s with 467 locks held;
   0.04 s with the snapshot alone). The header is read in a separate short transaction that imports the
   snapshot. The response is the session id and the header: format
   version, engine version, the schema's changeset count (distinct applied changeset identities, the
   value the engine already reports as `schema_changeset_count`; the last applied id is no marker, because
   the changelog always ends with a `runAlways` grants changeset, 248-research-12), tenant id, UTC time,
   each `data` and `derived`
   table's columns and types, and the `embedding_models` rows the tenant's rows refer to (a global table
   that two exported tables reference by foreign key). It carries no row counts: counting every table in
   one request would be a long request again, so each table's first page carries that table's count in
   the snapshot.
2. `GET /v1/tenant/export/{id}/rows?table=T&after=<key>`. Each call is its own short `REPEATABLE READ
   READ ONLY` transaction whose FIRST statement is `SET TRANSACTION SNAPSHOT '<id>'` (Postgres refuses it
   later, and refuses it under READ COMMITTED), followed by the tenant setting. The tenant comes from the
   session's owner, never from the request: the snapshot does not carry the tenant, and the same snapshot
   with another tenant's setting reads that tenant's rows. It returns `COPY (SELECT <non-generated
   columns> FROM T WHERE <key> > after ORDER BY <key> LIMIT n) TO STDOUT (FORMAT binary)`, the key being
   the table's primary key, with the page cut by bytes at about 16 MB (under the 20 MB body cap; about
   3,200 bge-768 chunks or 2,700 voyage-1024 chunks; the database serves 4,000 chunk rows in about 0.1 s,
   so the network sets a page's time). The response carries the page's last key and a SHA-256 of the
   page; a table's first page also carries the table's row count in the snapshot.
3. `DELETE /v1/tenant/export/{id}`, or expiry: the session closes, the transaction ends and the snapshot is
   released. A session idle past 10 minutes, or older than 60 minutes, is closed by the engine, so an
   abandoned export cannot hold the snapshot (Sam, 2026-11-11: "60 minutes"; an export that would need
   longer fails loudly and names the limit, instead of holding the snapshot past what A5 observed), which pins the vacuum horizon for the whole database while
   it lives. The bounds must sit inside any `idle_in_transaction_session_timeout` the cloud sets for the
   engine role (A7).
Because each page names where it ended, a client that loses a request asks again from the same key: a
dropped connection costs one page, not the export. One export session per tenant at a time.

**Not a client walk (RDR-241).** RDR-241 (draft) bars client code from paging through every document of a
tenant to answer a question or change rows, leaves a client walk standing only where a person typed a
command whose job is to read every row (export), and lists "the export and recovery bundle as streamed
routes" in its Step 5. This export is that route in the only shape the cloud edge allows (A1: 60 seconds an
exchange, 20 MB a body). What RDR-241 forbids is a client walking live tables whose size and state it does
not control: each page then costs more as the tenant grows, and the walk reads a table that is changing
under it. Here the engine holds one snapshot and the client pages that, through routes that exist only for
the export. A page is cut by bytes. On a table whose key leads with the tenant (chunks, manifests, topic
assignments) the page reads in key order, so its cost does not depend on the tenant's size (gate N1 found the
planner sorting instead, and the page transaction now forbids the sort). On a table keyed by `id` alone (memory,
plans, aspects) a page costs the cheaper of a key-order scan of the shared table and a sort of the tenant's
remaining rows, so it can grow with the table and the tenant's share, and the 20 s page budget, not the tenant,
bounds it (see Risks). Every page reads the same state; the table whitelist is the registry's; and the engine, not the client, bounds the session (idle
10 minutes, 60 minutes in all). RDR-241's ratchet of walk shapes lists no call of these routes, and
RDR-241 Step 5 should read "streamed" as "a session of short requests over an engine-held snapshot" when
it lands.

**Client (`nx backup`, cloud arm).** In managed mode the verb takes the cloud arm before it calls `resolve_target`, which stays the local arm's resolver and still refuses managed mode. The verb opens a
session, pulls every table page by page into `~/nexus-backups/.nexus-<tenant>-<UTC stamp>.nxtenant.partial-<pid>`
(mode 0600) as one framed file (the header, a frame per table made of its pages, and a trailer of
per-table row counts and SHA-256), retries a failed page from its key, checks each table's rows against
the count its first page carried, closes the session, then renames. It prints size, time, row counts
by class, `ONLY_COPY_SENTENCE`, and that the file holds sensitive documents when any are present. The
local arm is unchanged. A client older than the route's engine version refuses with the version it needs.

**Import session (Phase 2 design, settled 2026-11-13, nexus-csayy.5).** The same edge limits apply, so an
import is a session of short requests too. Evidence for each rule is in T2 `nexus/rdr248-phase2-evidence-2026-11-13`;
file and line references below are to `service/src/main/java/dev/nexus/service` unless a path says otherwise.

*Who and where.* The caller's own tenant token, as for export; every route of a session answers only that tenant
and gives any other "no such session". A session belongs to the tenant, not to the token: a request with an expired
token gets the usual 401, and the same session goes on under a new token of that tenant. It loads into the caller's own tenant, which exists because the operator
ran tenant create (`POST /v1/tenants/create`), the only way a tenant's `chunks` leaves come to exist; rows never
make a tenant. Sessions live in the engine's memory, which is sound because one engine runs per database: a
second engine is refused at boot (`EngineSingletonLock`, `Main.java:237-240`; one replica, `docs/runbooks/rdr-225-cloud-deploy.md:48-49`).
That lock has an opt-out (`NX_ENGINE_SINGLETON_LOCK=off`) and is skipped behind pgbouncer
(`EngineSingletonLock.java:96-97, 145-148`); the import routes refuse to open a session unless the engine reports the
lock held (a flag `Main` sets; an in-process test service declares it), so a second engine is a refusal, not two
session registries.
One import session per tenant, and import sessions share the export sessions' per-engine cap
(`TenantExportSessions.java:180-199`), each holding one dedicated connection from the pool.

*1. Open: `POST /v1/tenant/import`* with the file's header and the file's total row bytes. Every refusal check
runs in its own short READ COMMITTED transaction on a pooled connection, never on the session's held one, so no
check holds a lock through the upload. Refusals, each naming what failed, and none costing an upload:
- **Not empty.** The tenant is empty when every `data` table holds no row for it other than its **seeded rows**.
  A seeded row is one the engine itself writes for a tenant before any user write, listed per table in the
  registry (`ExportTable.seededKeys`). Today there are two kinds: `catalog_meta` key `rdr204_ghost_sweep_v1`, written
  on the tenant's first authenticated request (`AuthFilter.java:287-291`, `CatalogRepository.java:11905`), and,
  on a local engine only, the `embedding_profile` rows every boot writes for the default tenant (`Main.java:524`).
  Tenant create itself writes no registry row (`TokenAdminHandler.java:140-149`; the token insert only makes
  partition leaves). `catalog_owners` is not seeded: only document register and reconcile write it, both user
  writes (`CatalogRepository.java:1056-1074`, `catalog-048-reconcile.xml:272-288`). `derived` tables are not part
  of emptiness (see *Derived tables* below). A test boots a fresh tenant, makes one authenticated request and one
  search, and fails if any `data` table then holds a row that `seededKeys` does not cover, so a new lazily
  written row is a reviewed change, not a refused restore.
- **Unreadable file.** Its format version is not one the engine supports; or a file table or column is neither in
  the engine's registry with the same type nor listed as retired (see *Cross-release*); or a target column the
  file lacks is NOT NULL with no default.
- **Missing model.** An `embedding_models` row the header names does not exist.
- **Too large.** The declared row bytes exceed `NX_IMPORT_MAX_BYTES` (default 8 GiB, about twice tenant `nexus`).
- **Busy.** The tenant already has an import or export session (409, naming the open session's id, so a client
  that lost its earlier session can `DELETE` it), or the engine is at its session cap (503 with a retry-after).
A newer file (its `schema_changeset_count` above the engine's) is not refused on the count alone: the column
comparison decides, and the count difference is logged and shown in the status. The count reads the database's
changelog journal, which can only read high, so the count was always the weaker test (N2).
The response is the session id and the client part size the engine accepts.

*2. Upload: `PUT /v1/tenant/import/{id}/rows?table=T&part=k`*, a body of at most 8 MiB (a part fits 60 s at
about 1.1 Mbps; the 20 MB edge cap would need 2.7 Mbps). The client re-cuts each exported page at row boundaries
into parts, each a complete binary COPY stream (header, rows, trailer), and sends its SHA-256 in a header.
Before any upload the client verifies the whole file (every page's SHA-256 and the trailer's counts, the same check
as `nx backup --check`), so a damaged file is refused before it costs the cloud anything. On the engine a part is read whole into memory (at most 8 MiB) and its SHA-256 checked before anything touches
the session's transaction, so a part damaged in transit is refused and sent again without cost. A part that
passes is copied with `COPY ... FROM STDIN (FORMAT binary)` into the table's stage (a temp table with the FILE's
columns, from the header; never `CREATE TABLE ... AS` from the target, which would lock every `chunks` partition
until commit), with no savepoint: a savepoint per part would hold one subtransaction id per part until the
transaction ends, and past 64 of them every other backend's snapshot on the shared database reads `pg_subtrans`
(a nexus-size file is about 480 parts). A COPY of a part whose hash passed fails on a malformed stream, a full disk or a timeout, and any of these
fails the session (`failed: part T/k: <error>`) and the client starts over. One part is in flight per session: a
request that arrives while another runs (a retry after the edge cut the first at 60 s) gets 409 `part_in_flight`
and the client asks again. Parts of a table arrive in order: a repeat of the last staged part with the same
SHA-256 answers 200 and stages nothing; a different SHA-256 for a staged part, or a gap, is refused. The engine
counts staged bytes and refuses the part that would pass `NX_IMPORT_MAX_BYTES`, so the cap holds whatever the
open declared. Tables arrive in load order, and a table is closed by `POST /v1/tenant/import/{id}/close?table=T&rows=N`,
N being the table's row count from the trailer; the engine answers 409 `row_count_mismatch` (with both counts)
unless the staged count equals N, and a part for a closed table is refused. That staged-count check against the
trailer, plus each part's own SHA-256, is the integrity check of the transfer (S3): the page hashes were already
checked by the client against the file, and the engine never sees the original pages. The held transaction is
READ COMMITTED, so the load's re-check below sees writes committed during the upload.

*3. Commit: `POST /v1/tenant/import/{id}/commit`* returns 202 at once and runs the load on the session's held
connection. Admission (Sam, 2026-11-11, bound it): the load's first statement takes the restore-load advisory lock with
`pg_try_advisory_xact_lock` (one key for the database), so it is released by the transaction's end on every
path; if another load holds it, the commit answers 503 `restore_busy` with a 60 s retry-after. The try-lock runs on the
held connection before the 202, so `restore_busy` is the commit's own answer. A refused commit counts as activity
for the idle bound, and the upload's total bound stops at the first commit attempt; a **restore-wait bound** of
`NX_IMPORT_LOAD_MAX` from that first attempt then applies, past which the engine closes the session, so a user who
meets `restore_busy` after a long upload keeps it staged while another load runs, and no longer. The verb retries
the commit every 60 s until that bound. Tenant create
checks the same key: a new changeset redefines `create_tenant_partitions` (today only in `vectors-030`:349-403) with
a parameter `p_check_restore boolean DEFAULT true`, which the token-insert trigger leaves at its default and the
RDR-225 walk (`vectors-030`:724-725) passes as false. With it true, the slow path, the one that runs DDL (the fast
path at :378-386, every leaf present, takes no lock, so minting or rotating a token for an existing tenant is never
delayed), first calls
`pg_try_advisory_xact_lock_shared` on the restore key (a different key from its per-tenant lock at :387-389) and
raises `lock_not_available` when a load holds it. TokenStore retries that as it retries a lock timeout
(`TokenStore.java:83-84`, 3 attempts with 200-400 ms backoff), so the answer comes within about 1 s: 503
`tenant_creation_busy`, whose Retry-After the handler sets to 30 s when `pg_locks` shows the restore key held,
and keeps at 2 s otherwise (`HttpUtil.java:630-636, 843` today returns a constant 2). A load that finds a tenant
create holding the key shared retries the lock for up to 5 s, then answers `restore_busy`. When a session ends,
its dedicated connection is closed and evicted from the pool, never returned: temp tables and session settings
would otherwise outlive it. The load, in that one transaction, in load order:
1. **Re-check emptiness** inside the load transaction, with the same seeded-row rule: writes that landed during
   the upload refuse the load (`failed: tenant not empty: <table>`) and nothing is written.
2. For a table with a reassigned id, build a temp map of old id to new id from the table's own sequence
   (`nextval`; the engine role cannot `setval`, `grants-nexus-svc.xml:140-143`, so ids are always reassigned and
   the sequences stay right).
3. `INSERT ... SELECT` naming the file's columns, from the stage, in ONE statement per table (the
   non-deferrable self foreign key `topics.parent_id` is checked at the end of each statement, so one statement
   satisfies it; a table split over statements loads parents first, `TenantExport.java:131-141`), with the
   caller's tenant, new ids from the map, and every referring column rewritten through its parent's map: foreign
   keys, and the soft references by the rule in *S1* below. Seeded
   rows load with `ON CONFLICT DO NOTHING` on their key; for `embedding_profile` every non-tenant column of the file's row must equal
   the seeded one, or the load fails naming the key and the column (a different profile means the restored vectors do not match
   the engine's embedder).
4. **Verify** (S3): for each table, inserted rows plus seeded rows skipped by `ON CONFLICT` plus rows dropped by
   the soft-reference rule equal the staged count (skipped `derived` tables are not loaded and not counted); every
   foreign key holds by construction; for each soft reference, every positive value is a new id from the map
   (a positive value outside the map fails the load), and the counts of remapped, passed-through and negated
   values are reported. Column checksums are a round-trip test check only, because trigger-maintained
   columns and reassigned ids change by design.
5. Upsert `catalog_meta(tenant, 'rdr248_restore', <JSON: the file's header SHA-256, session id, UTC time, row
   counts>)` as the transaction's last write (an upsert, because a file taken after an earlier restore carries
   that key as ordinary data), then commit.
`GET /v1/tenant/import/{id}` reports `staging`, `loading <table>`, `verifying`, `committed` or `failed:
<reason>`; the engine keeps the outcome in memory for 24 hours. A dropped client neither stops nor rolls back a
load. After an engine restart the in-memory outcome is gone, so the verb does not infer it from a refused reopen
(a refusal also follows any other write): it reads `GET /v1/tenant/import-marker`, which returns that tenant's
`rdr248_restore` row. The load committed exactly when that row names this file's header SHA-256; otherwise it
rolled back.

*Routes.* Open `POST /v1/tenant/import`; part `PUT /v1/tenant/import/{id}/rows?table=T&part=k`; close `POST
/v1/tenant/import/{id}/close?table=T&rows=N`; commit `POST /v1/tenant/import/{id}/commit` (a repeat while the
load runs or after it ended answers 200 with the current status and never starts a second load, so a client that
lost the 202 just commits again); status `GET /v1/tenant/import/{id}`; abort `DELETE /v1/tenant/import/{id}`; the
restore marker `GET /v1/tenant/import-marker` (its own path, so no session id can shadow it). Session ids are UUIDs.

*Bounds* (S6 and the fix check). Before commit: idle 10 minutes without a part, 60 minutes in all, as for
export; a status poll is not activity. After a commit refused `restore_busy`, the restore-wait bound above
(`NX_IMPORT_LOAD_MAX` from the first attempt). In all a session can hold its slot and its transaction for up to
three hours. After commit the load has a total bound, `NX_IMPORT_LOAD_MAX` (default 60
minutes, set from the Step 6 scale run with a margin of at least 2x), enforced by the engine cancelling the load's
backend; each statement also runs under a statement timeout of the same value. `DELETE /v1/tenant/import/{id}`
aborts: before commit it rolls back the staged transaction; during the load it cancels the backend, and the
status reads `failed: aborted`. The export keeps its 60-minute cap (Sam, 2026-11-11); A5 is stated as observed
benign for 60 minutes at that window's write rate, not as a basis for a longer cap, and no xmin-age alarm is
added (the cap is the bound).

*S1, soft references.* A soft reference holds a reassigned id with no foreign key, so the load decides what a
value with no map entry becomes. The registry carries the rule per column, and the census test fails on a soft
reference without one:

| Column | Sentinels passed through | A value with no map entry (its parent was gone before the export) |
| --- | --- | --- |
| `memory_summaries.source_ids` (bigint[], NOT NULL, non-empty, provenance) | any negative value (a source already gone in an earlier restore) | negated: `-old_id`, so the array keeps its length and the CHECK `cardinality > 0` (`memory-004`:128) holds; a negative id names no memory, which is what a reaped source already is, and because negatives pass through, a second backup and restore leaves it negative |
| `nx_answer_runs.plan_id` (nullable) | NULL, 0 (plan miss, `TelemetryRepository.java:1379`), any negative value | negated: `-old_id`, which keeps the run counted as a plan hit (`TelemetryRepository.java:1500-1503` counts NULL and 0 as misses) and names no plan |
| `nx_answer_runs.run_summary` `run.plan_id` (jsonb) | null, 0, any negative value | negated |
| `taxonomy_centroids.topic_id` (NOT NULL, part of the key; `derived`) | none | the row is dropped and counted |

The array holds the `memory.id` values a rollup summarised, as provenance (`memory-004-quarantine-and-rollup.xml:33-40,
122`). A rollup keeps its sources (`MemoryRepository.java:809`); a later `nx memory reap` deletes them
(`MemoryRepository.java:722`), so a summary whose sources were all reaped is the normal state, and the export does
not filter quarantined rows, so unreaped sources are in the file and remap. Remapping, not restoring old ids, is the
choice because an old positive id in a new tenant names some other memory or none. The parity census
(`everyIdShapedColumnOfAnExportedTableIsClassified`) covers bigint, bigint[] and jsonb, the only types that can
hold a BIGSERIAL value, and every reassigned key is one; it is not widened. An id kept in text or uuid is not
caught by any test; that is a stated limit, not a check. A `source_ids` value of 0 never comes from a BIGSERIAL; it
passes through like a negative one, and verify fails only on a positive value outside the map.

*Derived tables and cross-release (N2).* A `derived` table is rebuildable, so it never refuses a restore: when
the target already holds rows for it (a first search writes `search_telemetry`), or a file column of it does not
match, it is skipped and named in the status, together with every table that refers to it (skipping
`nx_answer_runs` skips `nx_answer_steps`). The registry gains a retired list: a table or column removed by a
later migration is entered there with the changeset that removed it, and a file's data in a retired table or
column is skipped and reported, not refused. The parity test cannot see a column that is gone; the committed
fixture's test is what catches a drop that was not entered in the retired list. A `data` column that is missing and not retired still refuses, by
name. Supported cloud-to-local cases are Sam's decision (ask `rdr248-cloud-to-local`).
The committed fixture (Step 6) is not silently regenerable: its test pins the fixture's SHA-256 and its header's
`engine_version`, so a regenerated fixture fails until a reviewed change edits both, and the change must say why.

*Capacity* (S2). The engine has no disk or WAL check today (no `pg_stat_wal`, `statvfs` or database-size check
on any engine path; the only ones are the local `LocalDiskPreflight.java:94` and the client's
`pg_backup.py:195-219`). A load stages about the file's size in temp tables, then writes it again with WAL and
HNSW maintenance, on the database every tenant shares. What bounds that is Sam's decision (ask
`rdr248-import-capacity`); the byte cap above is the floor whatever he decides.

**Restore verb.** `nx backup restore FILE` (cloud and local) starts an import session, uploads the
pages, commits and polls the status until it ends. A cloud file restores into a local engine under the
same column-compatibility rule; which cloud-to-local cases are supported is the ask `rdr248-cloud-to-local`. The local `pg_restore` runbook stays
for whole-cluster restores. An empty tenant comes from tenant create: the operator on the cloud
(conexus), `nx init` on a fresh local install. The runbook's order is: the operator runs the current
engine, creates the empty tenant and issues its token; the user restores, and does not use the tenant until
the verb reports the outcome (a write during the upload refuses the load at its re-check). Nobody pins an engine
version. The verb first verifies the whole file, then opens, uploads, commits, and polls; after a lost
connection it polls the status, and after "no such session" it reads `/v1/tenant/import-marker`. It prints row
counts by class, the derived tables and retired columns skipped, the soft-reference values negated, and the
centroid rows dropped.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Table registry + parity test | `COLLECTION_SCOPED_TABLES` and its parity test | Reuse the pattern, new list |
| Partial-file publish, verify, interrupt handling | `pg_backup._publish`, `_interrupts_held` | Reuse |
| Destination, size warning, step report | `default_destination`, `free_space_warning`, `StepReporter` | Reuse |
| Tenant-scoped session | the engine's RLS session setup | Reuse |
| `nx store export`, `nx catalog export` | per-store exports | Keep; not replaced |

### Decision Rationale

Sam chose the engine export (2026-11-11). It is the only design that is complete (every RLS table, by a
tested registry) and consistent (one snapshot), and it gives the cloud a restore path that the user runs,
into an empty tenant the operator created. RLS confines it to the caller's tenant without any new authorization code.

## Alternatives Considered

### Alternative 1: Client-side bundle of the existing exports

**Description**: `nx backup` runs `nx store export --all`, `nx catalog export` and a new T2 export, and
packs them into one archive.

**Pros**: client-only; fastest to ship.

**Cons**: not one snapshot; misses aspects, highlights, tuples and full catalog rows until each gets an
exporter; every new table needs a new exporter.

**Reason for rejection**: Sam chose the engine export (2026-11-11).

### Briefly Rejected

- **Operator-run `pg_dump` per tenant on the conexus side**: the user cannot run it or hold the result,
  and the superuser connection bypasses RLS, so separating one tenant is hand work.
- **Logical replication to the user**: far more machinery than a backup needs.

## Trade-offs

### Consequences

- A new engine route pair and a file format that newer engines must keep reading (the header's format
  version and per-table column lists decide; see the import's compatibility rule).
- The cloud holds one read-only transaction per running export.

### Risks and Mitigations

- **Risk**: a new reference to a reassigned id with no foreign key (like `nx_answer_runs.plan_id`) is
  missed and silently points at the wrong row after a restore.
  **Mitigation**: the registry lists those references; a reviewer adding such a column must add it, and
  the round-trip test seeds every reassigned table with children so a broken join shows as a count
  mismatch.
- **Risk**: a trigger added later changes rows during import, and verification reports a false mismatch.
  **Mitigation**: the registry names trigger-maintained columns, and the parity test lists the tables'
  triggers so a new one is a reviewed change.

- **Risk**: an export session holds a snapshot, and so holds back vacuum on the shared database, for its
  whole life (A5); an import session holds an open write transaction.
  **Mitigation**: idle and total bounds per session, the engine closing a session that passes either; one
  session per tenant at a time; the bounds set from the cloud measurement of A5.
- **Risk**: a session's dedicated connection is one of the engine's pool for minutes.
  **Mitigation**: a separate, small cap on concurrent sessions per engine, refused with a retry-after when
  full.
- **Risk**: a schema change between export and import makes a file unloadable.
  **Mitigation**: the compatibility rule loads a file from an older engine whose columns still exist in
  the target, filling new columns from their defaults. A committed fixture enforces it: Step 6 writes a
  small backup file with the engine of this RDR's implementation and keeps it in the repository; a test
  loads that fixture on every later engine, so a migration that drops or retypes an exported column goes
  red there and must say what it does to old backups.
- **Risk**: a restore's load blocks tenant create on the shared database until it commits.
  **Mitigation** (Sam, 2026-11-11: bound it): one restore load at a time per database (an advisory lock);
  tenant create's DDL path checks the same lock and answers `tenant_creation_busy` with a 30 s retry-after
  within about 1 s; the fast path for a tenant whose leaves exist is unaffected; the load
  has a total bound and an abort route; the Step 6 scale run records the load's time.
- **Risk**: a load stages and then writes a whole tenant (3.8 GB for `nexus`) with WAL and HNSW maintenance on the
  database every tenant shares, and the engine has no disk or WAL check.
  **Mitigation**: the per-file byte cap `NX_IMPORT_MAX_BYTES`, one load at a time, and the decision in ask
  `rdr248-import-capacity` (Sam).
- **Risk**: an import holds a write transaction with an assigned transaction id for up to three hours (60 minutes of
  upload, up to `NX_IMPORT_LOAD_MAX` waiting on another load, then the load), which holds back vacuum on the shared database longer than the export's read snapshot,
  and is a hold A5 did not observe.
  **Mitigation**: one load per database, the bounds, and the Step 6 scale run and Activation Step 2 record the
  hold's time and the database's oldest-xmin age during it.
- **Risk**: a write to the tenant during the load itself (after the re-check) lands beside the restored rows; no
  table lock prevents it.
  **Mitigation**: none in code; the runbook's rule that the tenant is not used until the verb reports. Residual,
  stated.
- **Risk**: a memory summary's `source_ids` names memories reaped before the export.
  **Mitigation**: those ids are negated and counted in the outcome; they were provenance only.

- **Risk**: a page of a table keyed by `id` alone costs the cheaper of a key-order scan over the shared
  table and a sort of the tenant's remaining rows (no `(tenant_id, id)` index exists), so a tenant holding a
  middling share of a very large shared table can pass the 20 s page budget and never back up.
  **Mitigation**: the engine answers such a page `export_page_over_budget` and logs the tenant, table and
  time, and the client fails it as a page failure (not a busy wait), naming the budget. Measured shares are in
  Performance Expectations. **Owner and trigger**: a `(tenant_id, id)` Liquibase index on the id-keyed tables is
  Sam's decision, taken when any id-keyed page starts its response in more than 5 s (Activation Step 2 or any
  later export) or the over-budget log line fires.

### Failure Modes

- A page request fails: the client asks again from the same key; after a bounded number of tries it
  closes the session, removes the partial file (`.<name>.partial-<pid>`), and says to run again.
- The engine restarts mid-session: the dedicated transaction ends, the snapshot is gone; the next page
  request gets a clear "session closed" and the client starts over.
- An upload part fails in transit: its SHA-256 check refuses it before it touches the transaction, and the client
  sends it again; a part whose COPY fails fails the session, and the client starts over; after a
  bounded number of tries the client aborts the session (`DELETE`), which rolls the staging back.
- Import fails mid-load, including the in-load emptiness re-check or the total bound: one transaction, so the
  tenant is as it was; the status reports the table and reason.
- The client drops during a load: the load continues; the outcome is read later from the status route.
- The engine restarts during or after a load: the in-memory outcome is lost; the verb reads
  `/v1/tenant/import-marker`, which names the file's header SHA-256 only when the load committed.
- A tenant create arrives during a load: `tenant_creation_busy` with a 30 s retry-after.
- A session passes its idle, total or restore-wait bound: the engine rolls it back and closes it; the next request
  gets "no such session", and the verb says which bound and to run again.
- A commit meets `restore_busy` until the restore-wait bound: as above, the session closes with that reason.
- An open while the engine's singleton lock is not held: refused naming the lock; no session is opened.
- The database runs out of disk or WAL space during the load: the transaction fails and rolls back, and the status
  reports the database error; what prevents it is ask `rdr248-import-capacity`.
- Registry out of date: the parity test fails in CI before release.

## Implementation Plan

### Prerequisites

- [x] A1, A5 and A7 settled from the cloud (bob, read-only window on Sam's go, and conexus config)
- [x] A2, A3, A4 and A6 verified by local spikes (A3 for the types that held data, the rest seeded in
  Steps 4 and 6; A4 at toy scale, its scale run in Steps 4 and 6)

### Minimum Viable Validation

Done locally for the method (the spike). The MVV is Step 4's export tests (Phase 1) and Step 6's round-trip test (Phase 2) doing the same against the shipped
routes, in-process with the test fixtures (a subprocess CLI from a checkout is refused by the
production-write guard even against a loopback engine), plus one commit at scale (at least 500,000
synthetic chunks) whose time is recorded, seeding every `data` and `derived` table,
including the types the spike did not exercise; then one export and import against a cloud test tenant
in a window, with time and size recorded.

Sam's choice (2026-11-11): export ships first, restore next. Phase 1 puts a verified backup in cloud
users' hands; Phase 2 adds the restore once the import's open questions (the gate's S1 to S5 and N2,
T2 `nexus_rdr/248-gate-critique-2026-11-11-r2`) are settled. Until Phase 2 ships, a cloud restore is an
operator task from the file.

### Phase 1: Export (code)

#### Step 1: Table registry and parity test (engine): class, load order, reassigned ids and their references, trigger-maintained columns

#### Step 2: Export session: snapshot, page route, header and trailer (engine)

#### Step 3: `nx backup` cloud arm: pull pages, verify, publish, and `nx backup --check FILE` (client)

#### Step 4: Export tests: two-tenant isolation, snapshot consistency across pages, every column type, and export paging at scale (at least 500,000 synthetic chunks, per-page time recorded)

### Phase 2: Restore (code), after the import's open questions are settled

#### Step 5: Import session (staged load, id reassignment, foreign-key order, verification, asynchronous commit with a status route, the one-load-at-a-time bound) and `nx backup restore` (engine + client)

The design is settled in the Import session section (nexus-csayy.5). Its two open asks to Sam,
`rdr248-cloud-to-local` and `rdr248-import-capacity`, do not block the routes; they decide one refusal and one
pre-check, each added when answered. The tenant-create check is the `p_check_restore` parameter of
`create_tenant_partitions`, on its slow path only, which the RDR-225 walk turns off; the busy answer
carries the restore-held fact on `TenantCreationBusyException`, sets the header and the body's
`retry_after_seconds` alike, and the `TokenStore.java:151-154` javadoc is corrected.

#### Step 6: The round-trip test (local engine, two tenants), the commit at scale, the committed fixture file, and the runbook

### Phase 3: Operational Activation

#### Activation Step 1: Engine release carrying the export routes (Phase 1), then the cloud deploy (Sam's go); the restore routes ride a later release

#### Activation Step 2: A cloud export of a test tenant in an announced window (bob), time and size recorded; after Phase 2, a cloud round trip

The export records, beside time and size: the per-page response start for every page (chunks and the
id-keyed tables, against the 5 s trigger in Risks), NX_POOL_SIZE and PG max_connections against the export
cap, and any over-budget or busy answers. A tenant near nexus's size (about 540,000 chunks) is the target.

### Day 2 Operations

| Resource | List | Info | Verify | Delete | Rename |
| --- | --- | --- | --- | --- | --- |
| Backup file | `ls ~/nexus-backups` | header print in `nx backup --check FILE` | trailer checksums | user deletes | n/a |

### New Dependencies

None.

## Test Plan

- **Scenario**: export then import into an empty tenant — **Verify**: per-table counts and checksums equal.
- **Scenario**: two tenants — **Verify**: the export holds none of the other tenant's rows.
- **Scenario**: import into a non-empty tenant — **Verify**: refused, nothing written.
- **Scenario**: a file written by an older engine — **Verify**: it loads, and columns the file lacks take
  their defaults.
- **Scenario**: a file with a data column the target lacks and has not retired — **Verify**: refused, naming
  the column; a newer file whose columns all match loads, and the count difference is shown.
- **Scenario**: the client drops during a commit — **Verify**: the load continues, and a later status call
  reports its outcome.
- **Scenario**: the engine restarts after a commit — **Verify**: `/v1/tenant/import-marker` names the file's
  header SHA-256 and the verb reports the restore done; after a restart during the load it does not, and the
  verb reports it rolled back.
- **Scenario**: a part sent twice, a part with a wrong SHA-256 (a transfer cut short), a gap, and a malformed
  part whose hash matches — **Verify**: the repeat stages nothing, the wrong hash and the gap are refused and the
  resend stages once, the malformed part fails the session; staged counts equal the trailer.
- **Scenario**: restore, export that tenant, restore the new file into a third tenant, with a summary holding a
  reaped source and a run whose plan was deleted — **Verify**: the negated values stay negative in the third
  tenant.
- **Scenario**: a commit answered `restore_busy` while another load runs — **Verify**: the upload stays staged,
  a later commit succeeds, and a session still waiting past the restore-wait bound is closed.
- **Scenario**: a request with an expired token mid-session, then a new token of the same tenant — **Verify**:
  401, then the session continues.
- **Scenario**: an open on an engine whose singleton lock is not held (an in-process service that does not
  declare it) — **Verify**: refused naming the lock.
- **Scenario**: a repeated commit after a lost 202, and an open while the tenant's earlier session is still
  open — **Verify**: the repeat answers the current status and starts no second load; the open answers 409 with
  the earlier session's id, which `DELETE` then closes.
- **Scenario**: a fresh tenant after one authenticated request and one search — **Verify**: every `data` row it
  holds is covered by `seededKeys` (a new lazily written row fails this test), and a restore into it succeeds.
- **Scenario**: a write to the tenant during the upload — **Verify**: the load's re-check refuses it and
  nothing is written.
- **Scenario**: a summary whose sources were partly and one whose sources were all reaped before the export, a run
  whose plan was deleted, a run with plan_id 0, and a centroid whose topic is gone — **Verify**: the load commits;
  remapped ids are new ids, reaped ones negated, the deleted plan negated (still counted a hit), the 0 kept, the centroid dropped, and each
  count reported.
- **Scenario**: restore into the local default tenant, whose `embedding_profile` rows the boot wrote — **Verify**:
  equal rows load; a file whose row differs in one column is refused naming it.
- **Scenario**: a part sent while the same part is still in flight — **Verify**: 409 `part_in_flight`; parts past
  `NX_IMPORT_MAX_BYTES` are refused whatever the open declared.
- **Scenario**: a file with a retired column and a derived table whose target already has rows — **Verify**:
  both are skipped and reported, and the rest loads.
- **Scenario**: abort before commit and during the load, and a load past `NX_IMPORT_LOAD_MAX` — **Verify**:
  the tenant is unchanged and the status says why.
- **Scenario**: another tenant's token on a session's rows, commit or status route — **Verify**: "no such
  session".
- **Scenario**: tenant create during an import load (the test holds the load open between its `chunks`
  insert and commit) — **Verify**: it gets `tenant_creation_busy` with a 30 s retry-after within 1 s, and
  succeeds after commit; a tenant create for a tenant whose leaves exist is not delayed; a second restore load
  in the same database gets `restore_busy` until the first ends.
- **Scenario**: the committed fixture file from the implementing engine — **Verify**: it loads on the
  current engine.
- **Scenario**: a commit at scale (a synthetic tenant of at least 500,000 chunks, local) — **Verify**: it
  completes, and its time is recorded for Performance Expectations.
- **Scenario**: a page request cut mid-transfer — **Verify**: the client resumes from the page's key and
  the file still verifies.
- **Scenario**: a write between two page requests — **Verify**: the export does not contain it (one
  snapshot across requests).
- **Scenario**: a session idle past its bound — **Verify**: the engine closes it and releases the snapshot.
- **Scenario**: tenant create while an export session is open — **Verify**: it succeeds promptly (the
  session's held transaction reads no table).
- **Scenario**: a page request for a session another tenant owns, or with another tenant's token —
  **Verify**: refused; the page route never takes the tenant from the request.
- **Scenario**: an RLS table added without a class — **Verify**: the parity test fails.
- **Scenario**: import into a second tenant in the same database — **Verify**: no key collision; foreign
  keys and the four soft references resolve to the new rows; the next engine insert after the load
  succeeds.
- **Scenario**: the target lacks an `embedding_models` row the file needs — **Verify**: refused before
  any write, naming the model.

## Validation

### Testing Strategy

Engine-substrate integration tests drive both routes against a local engine (no cloud). The cloud is
exercised once per release in an announced window.

### Performance Expectations

For tenant `nexus` (the largest): a 3.82 GB file. The database reads it in about 83 s, so an export's time
is the cloud edge's transfer rate: about 6 minutes at 10 MB/s, about 21 minutes at 3 MB/s. The first
cloud round trip (Activation Step 2) records the real rate. Export session bounds: idle 10 minutes,
total 60 minutes (Sam, 2026-11-11), refused-with-retry when an engine already has its cap of concurrent
sessions. 60 minutes is the hold A5 observed as benign at that window's write rate; an export that
would run longer fails, naming the limit. Import bounds (Phase 2 design): upload idle 10 minutes and 60 minutes
in all, so a nexus-size file (3.82 GB) needs about 1.1 MB/s sustained, and a slower link fails at the bound,
naming it; the load `NX_IMPORT_LOAD_MAX`, default 60 minutes until the Step 6 scale run sets it with a 2x margin;
parts of at most 8 MiB. An import holds one of the engine's export/import session slots for up to three hours (upload, restore wait, load).

**Gate N1 scale run (nexus-csayy.4, 2026-11-12, local engine on chas, `tests/e2e/export_scale_run.py`).** One tenant
of 500,000 bge-768 chunks (distinct vectors, about 1 KB of text each), 500,000 manifest rows over 50,000 documents,
and 400,000 `memory` rows interleaved by id with nine other tenants (4,000,000 rows in the table), exported through
the routes by the shipped client with a minted tenant token. Engine: the gate jar, `NX_POOL_SIZE` 10, every
`NX_EXPORT_*` setting at its default (16 MiB pages, 2 sessions, 20 s page budget); PostgreSQL 17.5 with
`max_connections` 100 and `shared_buffers` 2 GB on the substrate's tmpfs root, restarted before the export so shared
buffers began empty (the OS cache stayed warm); `lab-m`. The run was made twice with the same data: before and after a
fix to the page plan, described next.

**Before the fix: the keyset page was planned as a sort.** After the first page, a chunks page was planned
`Limit -> Sort -> Append -> Index Scan` on the tenant leaf's primary key, because the planner estimates one row for the
row-value keyset predicate on an ANALYZEd table. The sort cannot emit before it has read every remaining row of the
tenant, so each page cost time in proportion to the rows left, not the rows it returned (the 16 MB cut cancels the COPY
only after the sort has finished). The first page, which has no `after`, was planned `Merge Append` and was not
affected. Measured (before): file 2,352,237,445 bytes, 1,450,002 rows, 164 pages (138 cut), 142.8 s end to end; chunks,
129 pages of 3,906 rows, response start p50 0.86 s, p95 1.61 s, max 2.56 s (page 1), first decile mean 1.59 s, last
decile 0.30 s: time FELL with depth because the rows left fell. The page time was linear in the tenant's chunk count,
so the 20 s page budget would have broken between 3.2 and 3.9 million chunks and the 30 s response-start bound between
5 and 6 million. (An earlier run with the cluster on disk: 171.0 s, chunks p50 1.01 s, p95 2.15 s, max 3.13 s.)
EXPLAIN (ANALYZE) of a mid-table page, 100,000 rows uncancelled: Sort (external merge, 303 MB on disk) 918 ms; with
`enable_sort` off, `Merge Append` 233 ms. The existing plan test
(`aDeepChunksPageIsAPerLeafIndexScanUnderMergeAppendWithNoSort`) missed this because it turns `enable_sort`,
`enable_seqscan` and `enable_bitmapscan` off itself.

**The fix.** `TenantExportReads.pinOrderedScan` sets `enable_sort = off` for the page's transaction
(`set_config(.., true)`: legal in the read-only snapshot transaction, rolled back before the connection returns to the
pool, so it cannot leak) when the table's primary key leads with `tenant_id`. For such a table an ordered index scan
reads only the tenant's rows and no sort can beat it. It is a cost penalty, not a ban: a table with no index in key
order would still sort. Tables keyed by `id` alone (`memory`, `plans`, `topics`, ...) are NOT pinned: for a small tenant
in a large table the planner's other plan, the tenant's index and a sort of the tenant's few rows, is far cheaper than
the key-order scan, and the pin would remove it (measured below). It is the same transaction as the COPY, so it governs
the real cut path. Test:
`TenantExportSessionsTest.aDeepChunksPageOnAnalyzedTablesRunsNoSortUnderThePlannerSettingsThePageSetsItself` analyzes the
tables, sets no planner setting itself, reads the plan from inside the page's own transaction (the page's seam), and
asserts `Merge Append` over per-leaf `Index Scan` with an `Index Cond` on the key and no `Sort`, that `memory` is not
pinned, and that the setting does not outlive the transaction. It failed on the code before the fix (`Limit -> Sort ->
Append -> Seq Scan`).

**After the fix** (same data, same host and class, `export_scale_run.py`): file 2,352,237,445 bytes, 1,450,002 rows, 164
pages (138 cut), 52.3 s end to end through loopback (about 45 MB/s; was 142.8 s): the local engine and database are not
the limit.
- Chunks: 129 pages, 3,906 rows and about 16.65 MB a page. Response start (headers in; the page is buffered whole, so
  this is the database and the hash): p50 0.21 s, p95 0.26 s, max 0.28 s (page 0, which carries the table's `count(*)`);
  was p50 0.86 s, p95 1.61 s, max 2.56 s. The deepest page (128, 32 rows) 0.14 s. First decile mean 0.19 s, last decile
  0.24 s, a slope of +0.6 ms a page: flat, and independent of the rows left in the tenant, so a page no longer costs time
  in proportion to the tenant's size. Every page is far under the 30 s response-start bound and the 20 s page budget.
- The plan of a mid-table and the deepest page, replayed from the statements the engine sent (`explain-chunks-mid-page.txt`):
  `Limit -> Merge Append -> Index Scan using chunks_..._pkey`, `Index Cond` on the key, no Sort; 100,000 rows in 390 ms
  (was 918 to 1,020 ms); the deepest page 112 ms. The same statement without the pin (`...-pin-flipped`) is the old
  `Limit -> Sort -> Append`, 1,020 ms.
- Other tenant-keyed tables: `catalog_document_chunks` 6 pages, response start p50 0.16 s, max 0.17 s.
- `memory` (primary key `id` alone, the tenant owns a tenth of the table), unchanged by the fix: `Index Scan using
  memory_pk`, `Index Cond: (id > X)`, `Filter: tenant_id = ...`, `Rows Removed by Filter: 900,000` for 100,000 rows returned
  (490 ms), and 6 pages with response start p50 0.36 s, max 0.87 s (page 0, with the `count(*)`). Page cost is the rows
  returned divided by the tenant's share of the table. The first-page count uses `idx_memory_tenant_timestamp`
  (index-only, 26 ms). Chunks, which are partitioned by tenant, are not exposed to this.
- `memory`, a small tenant: 1,000 rows spread evenly over the same 4,000,000-row table. Its page started its response in
  0.010 s (whole export 0.13 s). The planner uses `Bitmap Index Scan on idx_memory_tenant_ttl_timestamp`, `Bitmap Heap
  Scan`, then sorts the tenant's 1,000 rows in memory: 8 ms. Forcing the key-order scan (what a pin on `memory` would
  do) is `Parallel Index Scan using memory_pk` with `Rows Removed by Filter: 1,333,000` a worker, 917 ms. No existing
  `memory` index delivers `(tenant_id, id)` order (the tenant indexes are `(tenant_id, project)`, `(tenant_id, agent)`,
  `(tenant_id, timestamp DESC)` and `(tenant_id, ttl, timestamp)`), so for an id-keyed table a page costs the cheaper
  of two plans, the key-order scan (rows returned over the tenant's share, so growing with the whole table) and a sort of
  the tenant's remaining rows (growing with the tenant). The planner takes the cheaper per page, and the key-order scan
  reads page rows divided by the share, so at a fixed share it does not grow with the table.
- `memory`, middle shares of the same 4,000,000-row table (0.5%, 1%, 2%, 5%: 20,000 to 199,000 rows): worst page
  response start 0.55 s (5%, page 0 with its count), whole exports 0.37 to 2.24 s; the plan flips between the sort
  (0.5%, 1%, the 2% first page) and the filtered key-order scan (the 2% deep page, 5%). Extrapolated, not measured, to
  40,000,000 rows: the worst share moves to about 0.5 to 1% at about 1.4 to 1.9 s a page with parallel workers, about
  6 s without. A cold or cloud volume ten times slower would put that near the 20 s budget, which is why Risks names
  an owner and a trigger for a `(tenant_id, id)` index.
- Connections: the pool holds 10; PostgreSQL saw at most 13 `nexus_svc` backends (an evicted connection closing while its
  replacement opens), of 100 `max_connections`. A cut page discards its connection (`TenantExportReads.release`), so the
  engine opened one new connection per cut page: 139 sessions by the engine for 138 cut pages.
Not measured here: the cloud's edge rate, its pool size and `max_connections`, and a cold volume. The cloud round
trip (Activation Step 2) records those; its 500,000-chunk page times should be read against 0.28 s above, not 0.1 s.

## Finalization Gate

### Contradiction Check

The first draft's single streamed response contradicted A1 once it was refuted (the cloud edge ends
every exchange at 60 s); the design was changed to sessions of short requests, and no part of
the text still assumes one long stream. The first draft's "60 tables" came from a changeset count; the
live catalog says 50 parent tables plus 40 chunk leaves, and the registry is built from the live catalog.
Gate round 1 found the import commit and the changeset rule contradicting the cloud limits; both were
redesigned (asynchronous commit; a column-compatibility rule, with the changeset count advisory since the
Phase 2 design). No other
contradictions found between research findings, design principles, and proposed solution.

### Assumption Verification

A2 to A7 are verified (research records 248-research-1 to -14): A2, A3 and A6 by local spikes, A4 at toy
scale by a local spike, A5 and A7 by a read-only cloud window, A1 refuted and designed around. Left for
implementation, not assumed in the design: the column types the spike did not exercise (bigint[],
smallint, uuid, the identity column), which the Step 4 and 6 tests seed; the commit's time at scale, measured
in Step 6 and used to set the load's total bound `NX_IMPORT_LOAD_MAX`; and the cloud edge's transfer rate, which sets an
export's time but no design choice and is recorded at Activation Step 2.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `pg_export_snapshot()`, `SET TRANSACTION SNAPSHOT` | PostgreSQL 17 | Spike (round 2) |
| `COPY (SELECT ...) TO STDOUT (FORMAT binary)` under RLS | PostgreSQL 17 | Spike |
| `COPY ... FROM STDIN (FORMAT binary)` into a temp table | PostgreSQL 17 | Spike |
| `vector` binary send/receive | pgvector | Spike |

### Scope Verification

The Minimum Viable Validation is Step 4's export tests at scale (Phase 1) and Step 6's round-trip test (Phase 2): export a seeded tenant through the shipped
routes, import into an empty tenant in the same database, and assert equal counts, checksums and joins,
in-process with the test fixtures, plus one commit at scale (at least 500,000 synthetic chunks) whose time
is recorded. Step 4 gates the export release (Activation Step 1); Step 6 gates the later restore
release (Activation Step 1); the cloud export is Activation Step 2.

### Cross-Cutting Concerns

- **Versioning**: the file header carries a format version, the schema's changeset count and every
  table's columns; import loads a file whose `data` columns all exist in the target (or are retired),
  skipping retired and mismatched `derived` ones, and refuses a missing `data` table or column, naming it; a
  newer changeset count is shown, not refused. The routes are an additive wire change, recorded in the wire-contract ledger.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A (no new dependency).
- **Deployment model**: an engine release and a cloud deploy, both on Sam's go; the client refuses an
  engine older than the routes' version.
- **IDE compatibility**: N/A.
- **Incremental adoption**: the local `nx backup` (pg_dump) is unchanged; the cloud arm and
  `nx backup restore` are additions.
- **Secret/credential lifecycle**: tokens live only in global tables, which the export cannot read; the
  routes take the caller's own tenant token and never a tenant from the request; the file is mode 0600
  and says when it holds sensitive documents.
- **Memory management**: the engine pages by bytes (about 16 MB) and never holds a whole table; the client
  writes pages to disk as they arrive.

### Proportionality

Right-sized for a new engine route pair, a file format and a restore path that must be correct across
tenants. Nothing to trim.

## Revision History

- 2026-11-11: draft; local spikes (two rounds) and a read-only cloud window settled A1-A7.
- 2026-11-11: Gate round 1 — BLOCKED (2 Critical, 6 Significant, 2 ship-blocker(s)); critique `nexus_rdr/248-gate-critique-2026-11-11-r1` (gated commit in T2 `nexus_rdr/248-gate-latest`'s prior chain).
- 2026-11-11: Gate round 2 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); critique `nexus_rdr/248-gate-critique-2026-11-11-r2`, fix check `nexus_rdr/248-fix-check-6c291f8ba` (gated commit in T2 `nexus_rdr/248-gate-latest`).
- 2026-11-11: Accepted by Sam, with export first and restore next, and the restore load's block on tenant create bounded (folded into the Implementation Plan and Risks).
- 2026-11-11: Export session total bound set to 60 minutes (Sam, by dialog via halcyon: "60 minutes"; a larger export fails loudly and names the limit); amended under standing grant 3.
- 2026-11-12: Performance Expectations amended with the gate N1 scale run (nexus-csayy.4), and again with the page-plan fix and its before/after numbers.
- 2026-11-12: Phase 1 review gate (nexus-csayy.4): the not-a-client-walk paragraph corrected for id-keyed tables; Risks gains the id-keyed page cost with its owner and trigger; Activation Step 2 names what it records. Amended under standing grant 3.
- 2026-11-13: Phase 2 design (nexus-csayy.5): the Import session, Restore verb, Risks, Failure Modes, Test Plan and import bounds revised to settle gate S1-S3, S5, S6, N2 and the fix-check items (seeded rows, part protocol, in-load re-check, restore marker, load bound and abort, advisory-lock admission against tenant create, source_ids remap, derived and retired skips). Evidence in T2 `nexus/rdr248-phase2-evidence-2026-11-13`. Two decisions left with Sam as asks: supported cloud-to-local cases and import capacity.
- 2026-11-13: Phase 2 design check (substantive-critic, T2 `nexus_rdr/248-phase2-design-check`): B1 settled with a per-column soft-reference rule (sentinels, dangling values, `source_ids` negated); D1-D11 folded in (no per-part savepoints, transaction-scoped admission lock, a 1 s tenant-create answer, the verify count identity, a refused commit keeps its upload, staged-byte cap, one part in flight, derived dependents skipped together, the embedding_profile comparison, stale lines, token expiry); O1-O7 stated as risks or notes.
- 2026-11-13: Phase 2 design check round 2 (T2 `nexus_rdr/248-phase2-design-check-r2`): negative soft-reference values pass through, so negation survives a second backup and restore; a deleted plan's id is negated, keeping the run a plan hit; a restore-wait bound on a commit refused `restore_busy`; the session hold restated as up to three hours; stale savepoint, 3.8 s and two-reference lines swept; Step 5 names the asks and the trigger-path placement.
- 2026-11-13: Phase 2 design check round 3 (T2 `nexus_rdr/248-phase2-design-check-r3`, 0 BLOCKS-PLANNING): the route list (table close, idempotent commit, `import-marker`, busy names the open session), the tenant-create check as a `create_tenant_partitions` parameter on its slow path only, the cloud-to-local line pointed at its ask, the census left at the types a BIGSERIAL fits, and the remaining Test Plan and Failure Modes lines.
