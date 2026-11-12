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

- **Verified (source reading):** `nx backup` refuses managed mode first (`pg_backup.resolve_target`).
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
- its **reassigned id**, if its primary key is a global serial (18 tables), and the **columns that refer
  to it**, both foreign keys (`nx_answer_steps.run_id`, `topic_assignments.topic_id`,
  `topic_links.from_topic_id` and `to_topic_id`, `topics.parent_id`) and references with no foreign key
  (`nx_answer_runs.plan_id` to `plans.id`, `taxonomy_centroids.topic_id` to `topics.id`);
- the **columns a trigger maintains** (`topics.updated_at` and its doc count, restamped when assignments
  load), which verification skips.
A parity test fails when a live RLS table has no entry, when a foreign key or serial primary key is not
reflected in the entry, or when an entry names a column that no longer exists.

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

**Client (`nx backup`, cloud arm).** In managed mode `resolve_target` no longer refuses. The verb opens a
session, pulls every table page by page into `~/nexus-backups/nexus-<tenant>-<UTC stamp>.nxtenant.partial`
(mode 0600) as one framed file (the header, a frame per table made of its pages, and a trailer of
per-table row counts and SHA-256), retries a failed page from its key, checks each table's rows against
the count its first page carried, closes the session, then renames. It prints size, time, row counts
by class, `ONLY_COPY_SENTENCE`, and that the file holds sensitive documents when any are present. The
local arm is unchanged. A client older than the route's engine version refuses with the version it needs.

**Import session.** The same limits apply, so an import is a session too: `POST /v1/tenant/import`
opens it with the file's header and gets an id, after the refusal checks below, which need only the
header and the target, so a refusal never costs an upload; `PUT /v1/tenant/import/{id}/rows?table=T&part=k` sends
pages under 20 MB each, staged on the engine's dedicated connection inside one open transaction; `POST
/v1/tenant/import/{id}/commit` starts the load and returns 202 at once, because loading a large tenant
(2.27M rows for `nexus`, 542,778 chunks into an HNSW index) cannot finish inside one 60-second exchange.
The engine runs the steps below on the session's dedicated connection, and `GET /v1/tenant/import/{id}`
reports `staging`, `loading <table>`, `verifying`, `committed` or `failed: <reason>`; the outcome is kept
in the engine's memory for 24 hours after the session ends. A client that loses its connection reads the
outcome later: dropping the client neither stops nor rolls back a running load. An engine restart loses
the outcome, so "no such session" can follow a load that committed or one that rolled back; the restore
verb tells them apart by opening a new import session with the same header, which the empty-tenant
check refuses after a committed load and accepts after a rolled-back one. Every route of a session (rows,
commit, status) answers only the token of the tenant that opened it; any other tenant's token gets "no
such session". The idle bound applies until commit (a status poll is not activity, because nothing waits
on the client during the load); the total bound applies to the upload phase; the load itself runs under
a statement timeout set from the Step 6 scale measurement. It loads into the caller's own tenant, which must exist
through tenant create (that is what makes its `chunks` partition leaves; rows are never how a tenant
comes to exist). It refuses unless every `data` and `derived` table is empty for that tenant, ignoring the rows tenant create
itself writes; unless the engine can read the file; and unless every `embedding_models` row in the
header exists. The engine can read a file when its format version is one the engine supports, its last
changeset count is not above the engine's (a file written by a newer schema is refused, naming both
counts), and, always, for every table in the file, the table is in the engine's registry and every file
column exists there with the same type (a missing table or column is refused by name); a target column
the file lacks must be nullable or have a default, and takes it. There is no shortcut that skips the
column comparison. A file from an older engine therefore loads into a newer one, which is the cloud
case: the operator, not the user, decides the engine version, and the schema moves with each release
that adds a changeset. Then, in that one transaction, table by table in load order:
1. stage the pages: a temp table whose columns are the FILE's columns for that table, from the header
   (an older file has fewer columns than the target; not `CREATE TABLE ... AS` from the target, which
   would hold a lock on every `chunks` partition until commit and block tenant create the same way),
   `COPY ... FROM STDIN (FORMAT binary)` into it as they arrive (COPY into the RLS table itself is
   refused);
2. for a table with a reassigned id, build a temp map of old id to a new id from the table's own sequence;
3. `INSERT ... SELECT` naming the file's columns, from the stage, with the caller's tenant in the tenant
   column, new ids from the map, and referring columns, foreign key or not, rewritten through their
   parent's map; target columns the file lacks take their defaults.
While the load runs, its inserts hold locks on the `chunks` tree until commit, so tenant create, which
needs an exclusive lock on a model-level `chunks` partition, fails with `tenant_creation_busy` for the
load's duration (248-research-13). The user starts a restore, so no operator can schedule around it.
Sam's choice (2026-11-11) is to bound it, not accept it: the engine admits one restore load at a time,
and only when no tenant create is pending; a tenant create that arrives during a load waits a short
window for it to end, then answers with a retry-after (the restore's own target tenant exists before the
session opens).
It then checks each table's row count against the trailer, and a checksum over the columns that neither
the reassignment nor a trigger touches. Any failure rolls the whole load back. Reassigning ids always,
not only on a collision, also leaves the sequences correct: the engine role cannot `setval`, and a load
that kept the old ids made the engine's next insert collide.

**Restore verb.** `nx backup restore FILE` (cloud and local) starts an import session, uploads the
pages, commits and polls the status until it ends. A cloud file restores into a local engine under the
same rule, when the local engine's schema is the file's or newer. The local `pg_restore` runbook stays
for whole-cluster restores. An empty tenant comes from tenant create: the operator on the cloud
(conexus), `nx init` on a fresh local install. The runbook's order is: the operator runs the current
engine, creates the empty tenant and issues its token; the user restores. Nobody pins an engine
version.

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
tested registry) and consistent (one snapshot), and it gives the cloud a restore path that the user can
run. RLS confines it to the caller's tenant without any new authorization code.

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
  **Mitigation** (Sam, 2026-11-11: bound it): one restore load at a time per engine, admitted only when no
  tenant create is pending; a tenant create during a load waits a short window, then gets a retry-after;
  the Step 6 scale run records how long the window is.

### Failure Modes

- A page request fails: the client asks again from the same key; after a bounded number of tries it
  closes the session, removes `.partial`, and says to run again.
- The engine restarts mid-session: the dedicated transaction ends, the snapshot is gone; the next page
  request gets a clear "session closed" and the client starts over.
- Import fails mid-load: one transaction, so the tenant stays empty; the status route reports the table.
- The client drops during a load: the load continues; the outcome is read later from the status route.
- The engine restarts during or after a load: the outcome is lost; the verb reopens to learn whether the
  load committed (the empty-tenant check refuses) or rolled back (it accepts).
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

#### Step 6: The round-trip test (local engine, two tenants), the commit at scale, the committed fixture file, and the runbook

### Phase 3: Operational Activation

#### Activation Step 1: Engine release carrying the export routes (Phase 1), then the cloud deploy (Sam's go); the restore routes ride a later release

#### Activation Step 2: A cloud export of a test tenant in an announced window (bob), time and size recorded; after Phase 2, a cloud round trip

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
- **Scenario**: a file written by a newer engine, or with a column the target lacks — **Verify**: refused,
  naming the changeset or the column.
- **Scenario**: the client drops during a commit — **Verify**: the load continues, and a later status call
  reports its outcome.
- **Scenario**: the engine restarts after a commit, then the verb reopens — **Verify**: the reopen is refused
  as not empty, and the verb reports the restore as done; after a restart during the load, the reopen is
  accepted.
- **Scenario**: another tenant's token on a session's rows, commit or status route — **Verify**: "no such
  session".
- **Scenario**: tenant create during an import load (the test holds the load open between its `chunks`
  insert and commit) — **Verify**: it waits, then gets a retry-after, and succeeds after commit; a second
  restore load on the same engine is not admitted until the first ends.
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
  keys and the two unenforced references resolve to the new rows; the next engine insert after the load
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
would run longer fails, naming the limit. The import session's bounds are settled in the Phase 2 design
(nexus-csayy.5).

## Finalization Gate

### Contradiction Check

The first draft's single streamed response contradicted A1 once it was refuted (the cloud edge ends
every exchange at 60 s); the design was changed to sessions of short requests, and no part of
the text still assumes one long stream. The first draft's "60 tables" came from a changeset count; the
live catalog says 50 parent tables plus 40 chunk leaves, and the registry is built from the live catalog.
Gate round 1 found the import commit and the changeset rule contradicting the cloud limits; both were
redesigned (asynchronous commit; a column-compatibility rule keyed on the changeset count). No other
contradictions found between research findings, design principles, and proposed solution.

### Assumption Verification

A2 to A7 are verified (research records 248-research-1 to -14): A2, A3 and A6 by local spikes, A4 at toy
scale by a local spike, A5 and A7 by a read-only cloud window, A1 refuted and designed around. Left for
implementation, not assumed in the design: the column types the spike did not exercise (bigint[],
smallint, uuid, the identity column), which the Step 4 and 6 tests seed; the commit's time at scale, measured
in Step 6 and used for the load's statement timeout; and the cloud edge's transfer rate, which sets an
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
  table's columns; import loads a file from an older or equal schema whose columns all exist in the
  target, and refuses a newer schema or a missing table or column, naming it. The routes are an additive wire change, recorded in the wire-contract ledger.
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
