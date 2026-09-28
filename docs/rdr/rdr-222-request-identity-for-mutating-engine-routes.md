---
title: "Request Identity for Mutating Engine Routes"
id: RDR-222
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-27
related_issues: [nexus-ll31n, nexus-wvek6, nexus-mfw6c, nexus-r46u9, nexus-w94eo, nexus-vhyar, nexus-8hdg9]
related_rdrs: [RDR-181, RDR-191, RDR-192, RDR-193, RDR-108, RDR-103]
---

# RDR-222: Request Identity for Mutating Engine Routes

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Draft written read-only against develop b1d900fd6, 2026-09-27. No code changed.

## Problem Statement

The engine (the Java nexus-service that embeds text and stores vectors in
Postgres) accepts mutating requests over HTTP through a managed edge (the proxy
in front of the cloud engine) that cuts a request off at a fixed bound (about
30 s when r46u9 was sized; conexus PR #358 raises it to 55 s on the embed-write
routes). When the edge cuts a request, the engine keeps working and may commit.
The client sees a gateway error (502/503/504) or a socket timeout and learns
nothing about the outcome. Three beads are one problem seen from three places:

#### Gap 1: A caller cannot tell whether a mutating sweep committed (nexus-ll31n)

**nexus-ll31n (sweeps).** A mutating sweep such as
   `POST /v1/vectors/gc/quarantine-orphans` (an anti-join move of chunks with no
   manifest row into a quarantine collection) cannot be told apart from outside
   as committed, still in flight, or rolled back. Measured 2026-09-16: a
   completed 41,032-row move read as a failure; the auto-retry hit the RDR-191
   sweep gate (a per-collection Postgres advisory lock) the first call still
   held and got an opaque 500, read as a rollback for five minutes. The
   auto-retry half is fixed (`_NON_IDEMPOTENT_SWEEP_PATH_SUFFIXES` in
   `src/nexus/db/gateway_backoff.py`, shipped v7.58.0). The three-state question
   is still unanswerable.
#### Gap 2: A retried upsert can embed the same chunks twice (nexus-wvek6)

**nexus-wvek6 (duplicate embeds).** A retried `upsert-chunks` page that
   overlaps the still-running original is embedded twice (Voyage billing plus
   engine load), because the RDR-181 existence partition (the engine's check
   "is this chash already stored?", which skips re-embedding stored chunks)
   sees only committed rows. nexus-r46u9 floors the resend at 30 s after a 504,
   so this is a tail, not the common case.
#### Gap 3: A late write from an abandoned index run can revert a newer run's metadata (nexus-mfw6c)

**nexus-mfw6c (stale run reverts per-run keys).** An abandoned
   `upsert-chunks` attempt from indexing run N that commits after run N+1's
   write re-merges run N's per-run metadata keys (indexed_at, embedding_model,
   session_id, source_agent, ttl_days) under the engine's merging
   `ON CONFLICT` (`metadata = chunks.metadata || EXCLUDED.metadata`, nexus-w94eo).
   An idempotency key does not help here: run N+1 is a different request.

Terms used below. A **chash** is the SHA-256 of a chunk's text and is its row
identity in `nexus.chunks`. The **fence** is the per-document run record on
`nexus.catalog_documents` (catalog-020: `index_state`, `index_content_hash`,
`index_run_id`, `index_started_at`), stamped `indexing` before a run's first
chunk and `complete` after its verified last manifest write. **Tenant RLS** is
Postgres row-level security keyed on the `nexus.tenant` setting that
`TenantScope.withTenant` stamps per transaction.

## Research Findings (measurements done for this draft)

All three are code reads against develop b1d900fd6. No production data was
read; where a number needs production, the exact query is given and the number
is left unknown.

### M-a. Are duplicate-chash embeds logged, and what would count them?

- **Verified (source).** Within ONE request a duplicate chash is never embedded
  twice: `PgVectorRepository.upsertChunksInternal` de-duplicates ids first-wins
  before the partition and the embed (`PgVectorRepository.java:613-640`), and
  logs `event=upsert_dedup_collapsed collection= received= kept= collapsed=` at
  INFO when it collapses any. `CombinedWriteService` (the combined write on
  `/v1/catalog/manifest/write_many` with inline chunks) de-duplicates the same
  way and logs `event=combined_write_embed_partition` per call.
- **Verified (source).** ACROSS overlapping requests nothing is logged that
  identifies a duplicate embed. The engine has no request id and no access log
  (no `request_id`/`X-Request-Id` anywhere in `service/src/main`; logback is a
  plain console pattern). The only per-request INFO line is
  `event=upsert_embed_skipped skipped= embedded=`, emitted only when
  `skipped > 0`; `event=upsert_chunks_done` is DEBUG. The final multi-row
  `INSERT ... ON CONFLICT` (`:824-860`) does not report which rows inserted and
  which hit the conflict, so a row embedded because it was absent at the
  partition but present by INSERT time is indistinguishable from a first insert.
- **What counting takes (engine-only, no wire change).** Add
  `RETURNING chash, (xmax = 0) AS inserted` to that INSERT. A row with
  `inserted = false` whose index came from the partition's ORIGINAL absentee
  set (not the content-divergent reroute, not the zero-row reroute) was
  committed by another writer between this request's partition and its INSERT:
  this request paid a duplicate embed for it. Log
  `event=upsert_embed_raced collection= raced= embedded=` at INFO when
  `raced > 0`, and add a lifetime `raced_embeds_total` counter to the existing
  `EmbedActivitySnapshot` on `GET /v1/status`. Same change in
  `CombinedWriteService`'s per-doc write. Caveats: `xmax` is a system column,
  so the jOOQ form needs checking against `RawSqlGateTest`; the count cannot
  say WHICH other writer won (a retry of the same page, or another document
  with identical text) until requests carry an id (Phase 1 below).
- **Production today.** Engine logs cannot answer M-a. Two proxies exist, and
  both are upper bounds, not counts:
  - conexus, edge logs: count responses with status in (502, 503, 504) on
    `POST /v1/vectors/upsert-chunks`, `POST /v1/vectors/store-put` and
    `POST /v1/catalog/manifest/write_many`, per day, since the r46u9 client
    release and again since conexus-vtlr's fast 503 refusals. The syntax
    depends on conexus's edge log store, which this repo does not document;
    the fields needed are timestamp, method, path, status, upstream duration.
    Each 504 on those routes is at most one overlapping resend.
  - client, captured index logs (the CLI logs to stderr only; `mode == "cli"`
    in `logging_setup.py` adds no file handler, so only runs whose stderr was
    captured, like the w94eo `dtindex.log`, carry this):
    `grep -h "vector_gateway_retry_embed_write_504" <captured-log> | awk '{print $1}' | sort | uniq -c`
    (KeyValueRenderer format; each line is one floored resend after a 504).

### M-b. Does the RDR-181 existence partition see uncommitted rows?

- **Verified (source), it does not.** `resolveNeedEmbedIdx`
  (`PgVectorRepository.java:3864-3939`) runs inside its own
  `tenantScope.withTenant(...)` call. `TenantScope.stampAndRun`
  (`TenantScope.java:220-246`) sets `autoCommit=false`, runs the work, and
  calls `conn.commit()` before returning; there is no isolation override, so
  the transaction is Postgres's default READ COMMITTED. The embed runs after
  that commit and outside any transaction (`:726-741`, comment at `:722-725`).
  The INSERT runs later in a separate `DeadlockRetry`-wrapped `withTenant`
  (`:802-862`). A concurrent request's rows are therefore invisible to this
  partition until that request's INSERT transaction commits. The duplicate
  window is the first attempt's whole embed time plus its INSERT, which is
  why a resend 30 s after a 504 can still overlap under a Voyage slowdown.
  `CombinedWriteService` has the same shape (partition in its own short
  transaction, embed outside, per-doc write transactions after).

### M-c. Can a real late cross-run commit be detected from existing data?

- **Verified (source).** The fence carries `index_run_id` and
  `index_started_at` (timestamptz since catalog-031; engine clock, stamped by
  `CatalogRepository.beginIndexRun`, `:6382-6409`). Nothing compares
  `index_run_id` on any write (`grep INDEX_RUN_ID` hits only begin and the
  read projection). Chunk metadata carries no run id: the client mints the
  run id (`uuid4().hex`, `doc_indexer.py:492`) and sends it only to begin.
  `session_id` defaults to `""` (`metadata_schema.make_chunk_metadata`), so it
  is empty on the PDF path. `indexed_at` in chunk metadata is the CLIENT's
  clock, stamped per run at chunk build (`pipeline_stages._build_chunk_metadata`,
  `doc_indexer._pdf_chunks`). `nexus.chunks.created_at` is first-insert only
  and there is no updated_at.
- **Detection is possible, approximately.** After a complete run N+1, every
  chunk in the document's manifest was written by N+1 (upsert or the RDR-181
  have-vector metadata refresh), so its `indexed_at` should be at or after
  `index_started_at`, give or take client/engine clock skew. A manifest chunk
  whose `indexed_at` is well BEFORE `index_started_at` was last written by a
  request built before this run began but committed after this run's write:
  exactly the late-commit class, from this document's earlier run or from
  another document sharing the chash (sharing affects attribution, not
  detection). A second fingerprint: chunk `content_hash` different from the
  fence's `index_content_hash` (a late write from a run over DIFFERENT content
  that shares chunk text).
- **The query conexus would run** (read-only, as the diag role with RLS
  bypass, or once per tenant with `SET nexus.tenant`):

```sql
WITH fence AS (
  SELECT d.tenant_id, d.tumbler AS doc_id, d.index_started_at AS run_start,
         d.index_content_hash
  FROM nexus.catalog_documents d
  WHERE d.deleted_at IS NULL
    AND d.index_state = 'complete'
    AND d.index_started_at IS NOT NULL
), c AS (
  SELECT f.tenant_id, f.doc_id, m.collection, f.run_start, f.index_content_hash,
         CASE WHEN ch.metadata->>'indexed_at' ~ '^\d{4}-\d{2}-\d{2}T'
              THEN (ch.metadata->>'indexed_at')::timestamptz END AS chunk_indexed_at,
         ch.metadata->>'content_hash'   AS chunk_content_hash,
         ch.metadata->>'embedding_model' AS chunk_model,
         (SELECT count(*) FROM nexus.catalog_document_chunks m2
           JOIN nexus.catalog_documents d2
             ON d2.tenant_id = m2.tenant_id AND d2.tumbler = m2.doc_id AND d2.deleted_at IS NULL
          WHERE m2.tenant_id = m.tenant_id AND m2.collection = m.collection
            AND m2.chash = m.chash) AS owners
  FROM fence f
  JOIN nexus.catalog_document_chunks m
    ON m.tenant_id = f.tenant_id AND m.doc_id = f.doc_id AND m.collection IS NOT NULL
  JOIN nexus.chunks ch
    ON ch.tenant_id = m.tenant_id AND ch.collection = m.collection AND ch.chash = m.chash
)
SELECT tenant_id, collection, doc_id,
       count(*) AS manifest_chunks,
       count(*) FILTER (WHERE chunk_indexed_at < run_start - interval '5 minutes') AS stale_indexed_at,
       count(*) FILTER (WHERE chunk_indexed_at < run_start - interval '5 minutes' AND owners = 1) AS stale_sole_owner,
       count(*) FILTER (WHERE chunk_content_hash IS DISTINCT FROM index_content_hash AND owners = 1) AS foreign_content_hash,
       count(DISTINCT chunk_model) AS distinct_models,
       max(run_start - chunk_indexed_at) AS max_gap
FROM c
GROUP BY 1, 2, 3
HAVING count(*) FILTER (WHERE chunk_indexed_at < run_start - interval '5 minutes') > 0
    OR count(DISTINCT chunk_model) > 1
ORDER BY stale_indexed_at DESC
LIMIT 200;
```

  Reading it: the 5-minute tolerance absorbs clock skew and a `now_iso`
  computed just before begin; a true late commit shows a gap of the interval
  between runs (minutes to days). `stale_sole_owner` excludes chashes shared
  with another live document, so a hit there is this document's own earlier
  run. Negative control: w94eo's 1.12.153 is a WITHIN-run revert (stub and
  post-pass from the same run), so it must NOT appear. There is no known
  positive; seed one in a test tenant (index, abandon a delayed page, re-index)
  to prove the query can fire before trusting a zero. Legacy documents with a
  NULL `index_started_at` are out of reach. The result is unknown until
  conexus runs it.

### M-d. Reassessing mfw6c's harm (found while doing M-c)

- **Verified (source).** For a conformant collection name
  (`<content_type>__<owner>__<model>__v<n>`, RDR-103) the collection's model
  segment is the embedder authority: `EmbedderRouter` refuses to embed a
  conformant collection with any other model (javadoc, `EmbedderRouter.java`
  `modelEmbedders`). The client resolves the write model INTO the collection
  name (`corpus.resolve_write_embedding_model`), and in service mode the
  stamped `embedding_model` is that target model (`pipeline_stages.py:298`).
  A model change between runs therefore changes the target collection, and a
  late run-N write lands in the OLD collection.
- **Consequence.** The case mfw6c calls harmful (a stored label that disagrees
  with the vector) needs a same-collection label change between runs, which
  the routing rules out for conformant names. **Assumed, not verified:** no
  caller stamps an `embedding_model` different from the collection token (an
  explicit `--collection` with a non-conformant name is the case to check).
  What a stale write can still revert: `ttl_days` together with `indexed_at`
  (expiry is derived from both, so a ttl change between runs could expire a
  chunk early); `content_hash` on chunk text shared across content versions
  (the freshness check reads one chunk's `content_hash` and `embedding_model`,
  `doc_indexer.py:1815-1830`, so a revert costs a spurious re-index, which is
  over-work, not loss); and provenance keys (`session_id`, `source_agent`).
  mfw6c is real but lower-severity than filed.

### M-e. Existing pieces this design reuses

- **Verified (source).** The combined write (`write_many` with `chunks`,
  nexus-kl2z6) already commits chunk rows, manifest rows and the completion
  stamp for one document in ONE short transaction, with embedding outside it
  (`CombinedWriteService` javadoc; `CatalogHandler.handleManifestWriteMany`).
  That is most of the brittleness proposal's P3 "document-commit route" for
  the repo indexer path. The streaming PDF path (the w94eo exposure) does not
  use it: it streams `upsert-chunks` pages, then an `update-metadata` post-pass,
  then `index-run/complete`.
- **Verified (source).** The sweep SQL functions write a `nexus.gc_audit` row in
  the same transaction as the move (catalog-033, catalog-037/hygiene-008),
  under the sweep gate (`pg_advisory_xact_lock(hashtext('sweepgate:...'))`
  with `lock_timeout 2000`, `statement_timeout 25000`).
- **Verified (source).** A request deadline already exists end to end
  (nexus-8hdg9): `AuthFilter` mints it into `RequestContext` from
  `X-Nexus-Request-Deadline-Ms` or `NX_EMBED_DEADLINE_MS` (default 300 s), and
  the client sends 540 s on `upsert-chunks`. The ledger lease below derives
  from it.
- **Verified (source).** Custom request headers already cross the edge
  (`X-Nexus-Request-Deadline-Ms`, `X-Nexus-Tenant`).

## Proposed Solution

### Approach

Four pieces, independent enough to ship separately:

1. **A client-minted request id** on mutating routes, sent as a header and
   reused unchanged across every retry of the same logical request.
2. **A durable request ledger** in the engine: one row per (tenant, request
   id), claimed at entry and marked committed in the SAME transaction as the
   mutation. A resend of a committed request replays the original outcome; a
   resend of an in-flight request gets a 409 naming it.
3. **A status route** keyed by request id, answering committed / in_flight /
   rolled_back / unknown.
4. **A run fence on document-scoped writes** (mfw6c): writes that carry
   (doc_id, run_id) are compared against the fence's `index_run_id`; a stale
   run's write stores content but does not merge metadata.

Pieces 1-3 answer ll31n and wvek6. Piece 4 answers mfw6c and does not use the
ledger at all.

### Technical Design

#### 1. Request id

- Header `Idempotency-Key: <uuid>` (name is an open question; the IETF httpapi
  draft `draft-ietf-httpapi-idempotency-key-header` uses this name with 409 for
  a concurrent duplicate and 422 for a key reused with a different body, which
  matches this design. Documented, recalled, to re-read at research).
- The client mints a UUIDv7 (time-ordered, so retention and log reading sort
  naturally) ONCE per logical request, at the call site that owns it: each page
  in `HttpVectorClient.upsert_chunks`'s page loop; each `gc_*` call; each
  `write_manifest_many` page that carries chunks; each `put`. It must NOT be
  minted in `_post`/`_request`: `_vector_with_retry` and
  `write_with_registration_retry` re-invoke `_post` for each attempt, so a key
  minted there changes per attempt and defeats the design. `_post` and
  `_request_once` gain a `headers` pass-through; the T2/catalog httpx client
  (`_refreshable_client.py`) gets the same.
- The header is optional. No header means today's behavior on every route.

#### 2. Request ledger (engine)

Table `nexus.request_ledger` (new Liquibase changeset, next free
`vectors-`/`catalog-` id at implementation time):

| column | type | meaning |
|---|---|---|
| tenant_id | text not null | RLS key |
| request_id | uuid not null | client key |
| route | text not null | e.g. `/v1/vectors/gc/quarantine-orphans` |
| body_sha256 | bytea not null (32) | fingerprint of the request body |
| state | text not null, CHECK in ('in_flight','committed','failed') | |
| holder | uuid | per-attempt nonce; NULL once terminal |
| lease_until | timestamptz | in_flight only |
| started_at, finished_at | timestamptz | |
| response_status | int | |
| response | jsonb | bounded summary (see below) |
| doc_id | text | optional, for Phase 3 revocation and diagnosis |

PK `(tenant_id, request_id)`; index `(tenant_id, state, lease_until)` for the
retention sweep; ENABLE + FORCE RLS with the standard `tenant_isolation`
policy (the `gc_audit` shape, catalog-018-2); grants in the runAlways
`grants-nexus-svc.xml` and `grants-nexus-diag.xml` families (the v0.1.78
zero-grants lesson).

Protocol for a request carrying a key:

1. **Claim** (short transaction, or the first statement of an existing one):
   `INSERT ... VALUES (..., state='in_flight', holder=:nonce,
   lease_until = clock_timestamp() + deadline + 60 s) ON CONFLICT DO NOTHING`.
   On conflict, read the row:
   - `body_sha256` differs: **422** `{"error":"idempotency_key_reused"}`.
   - `committed`: **replay** the stored status and response, header
     `Idempotency-Replayed: true`. No work runs.
   - `in_flight` and `lease_until > clock_timestamp()`: **409**
     `{"error":"request_in_flight","request_id","route","started_at","lease_until"}`
     with `Retry-After`. No work runs.
   - `in_flight` with an expired lease, or `failed`: **take over**:
     `UPDATE ... SET holder=:nonce, lease_until=..., state='in_flight'`
     guarded on the old holder, then run.
2. **Execute.** Embedding stays outside every transaction, as today.
3. **Commit marker, inside the mutation's own transaction:**
   `UPDATE request_ledger SET state='committed', holder=NULL, finished_at=now(),
   response_status=200, response=:summary WHERE tenant_id=? AND request_id=?
   AND holder=:nonce AND lease_until > clock_timestamp()`. Zero rows means the
   attempt was taken over or outlived its lease: throw, so the mutation rolls
   back with it. This is what makes the states exact: the effect and
   "committed" commit together or not at all, and an attempt whose lease has
   expired can never commit later, so "rolled back" is stable once reported.
   (`clock_timestamp()`, not `now()`: `now()` is the transaction START time.)
4. **Failure.** On an exception, a best-effort separate transaction sets
   `state='failed'`, `holder=NULL`, `response_status`. If the engine dies, the
   lease expiry covers it.

Where the marker goes per route:

- **Sweeps** (`quarantineOrphans`, `quarantineOrphansBounded`,
  `restoreRereferenced`, `expireQuarantine`): each is one `withTenant`
  transaction around one SQL function call, so the marker UPDATE goes in the
  same `ctx` after the call. Exact three-state semantics.
- **upsert-chunks**: two committed transactions exist today (the RDR-181
  have-vector metadata refresh in `resolveNeedEmbedIdx`, then the INSERT). The
  marker goes in the INSERT transaction (or, when every chash was settled by
  the metadata refresh and no INSERT runs, in the refresh transaction). The
  claim folds into `resolveNeedEmbedIdx`'s existing short transaction as its
  first statement, so no extra pool checkout is added (the open RDR-181 pool
  risk, T2 `nexus-f0r8p-pool-exhaustion-known-risk`). Caution: that method
  swallows exceptions into "embed everything"; a claim conflict must raise a
  typed exception outside that swallow. For this route "rolled_back" means
  "the final write did not commit; the earlier metadata refresh may have, and
  it is an idempotent merge, so resending the same key converges". The route's
  docs must say so.
- **store-put, write_many with chunks**: same pattern, Phase 2.

Stored response: the route's JSON body, capped at 16 KiB. The sweep `sample`
(up to 5,000 chashes under the `gc_audit` clamp) is truncated to 20 entries in
the stored copy with `sample_truncated: true` and the `gc_audit` row id; the
full list is in `gc_audit`.

Retention: a scheduled engine arm (the tuple-sweep scheduling pattern in
`NexusService.runScheduledTupleSweep`) deletes terminal rows older than
`NX_REQUEST_LEDGER_RETENTION_DAYS` (proposed 7) and in_flight rows whose lease
expired more than that long ago. Never an in_flight row within its lease.

Size: one row per keyed mutating request. Indexing runs are the volume
(one row per `upsert-chunks` page, 16 chunks per page local, 64 CCE, 300 code),
so 7 days of rows is the order of pages indexed in a week. Measure after
Phase 2 before settling the retention default.

#### 3. Status route

`GET /v1/requests/<request_id>` (new `RequestHandler`; `AuthFilter`'s scope
table must admit it for tenant-scope tokens), tenant-scoped by RLS:

| ledger row | reported state |
|---|---|
| committed | `committed`, with `response_status` and the stored response |
| in_flight, lease live | `in_flight`, with `started_at`, `lease_until` |
| in_flight, lease expired; or failed | `rolled_back` (sweeps: nothing applied; content-addressed routes: see above) |
| no row | `unknown`: never reached the engine, purged by retention, or sent to an engine without the ledger |

"unknown" is not ambiguous in practice: resending with the SAME key is safe in
every state (committed replays, in_flight 409s, rolled_back and unknown
execute once).

#### 4. Client behavior

- **Sweeps (ll31n).** On a gateway code or a socket timeout, do not resend
  and do not report failure. Poll `GET /v1/requests/<id>` (every 5 s, up to the
  lease): `committed` returns the stored outcome as if the call had
  succeeded; `rolled_back` resends with the same key; `in_flight` keeps
  polling; a 404 from the status route (older engine) falls back to today's
  behavior (surface the gateway error). This replaces "never retry" with
  "retry safely by key". `gateway_backoff.is_non_idempotent_sweep_path`
  stays, and applies only when no key was sent.
- **upsert-chunks (wvek6).** Keep the r46u9 30 s floor on a 504, then resend
  with the same key. A 409 `request_in_flight` means poll status, not
  re-POST. A replay counts as the ack (the `upserted` echo check at
  `http_vector_client.py:2733-2742` runs against the replayed body).
- **Capability.** The engine echoes `Idempotency-Key` on any response where
  it recorded the request; the client caches "this engine keeps a ledger"
  per endpoint and skips status polling otherwise.
- The existing 409s (`HttpUtil.sendTypedDbError` class-23 identity conflicts,
  pipeline conflicts, index-run refusals) keep their own `error` codes; the
  client dispatches on `error`, never on the status code alone.

#### 5. Run fence on document-scoped writes (mfw6c)

- Doc-scoped writers (the streaming PDF uploader, the metadata post-pass,
  the batch PDF path) send optional `"doc_id"` and `"run_id"` fields on
  `upsert-chunks` and `update-metadata`; `run_id` is the value the client
  already sends to `index-run/begin`.
- In each write transaction that merges metadata (the have-vector refresh,
  the INSERT, `update-metadata`), the engine takes
  `pg_advisory_xact_lock_shared(hashtext('indexrun:' || tenant || ':' || doc_id))`
  and reads `index_run_id`. `beginIndexRun` takes the same key EXCLUSIVE
  (`acquireIndexRunLock`, today used only by complete/stamp). Begin of run N+1
  therefore waits for any in-flight write transaction of run N (short: the
  embed is outside it), and any run-N write after that sees the new run id.
- On a mismatch the write is **stale**: absent chashes still insert (content
  addressed; if run N+1's manifest does not name them they are manifest-less,
  which RDR-192 `live(c)` makes invisible and reapable), but conflicting rows
  are `DO NOTHING` and the metadata refresh is skipped. The response gains
  `"stale_run": true, "current_run_id": ...` and the client abandons run N.
- Why not a generation column on chunks: chunks are shared across documents
  (identical text in one collection is one row, RDR-108), so a per-chunk
  generation from different documents is not comparable, and client run ids
  are unordered UUIDs. Equality against the fence needs no ordering.
- Lock order must be checked against the combined write (which writes chunk
  rows and then stamps the document row) for 40P01; `DeadlockRetry` covers a
  residual deadlock, as it does today.

#### 6. Relation to RDR-193's document-commit route (brittleness P3)

The document-commit route narrows mfw6c's window from "a whole embed" to
"one short transaction" but does not close it: a delayed commit request from
run N can still land after run N+1's. It closes it only with the same fence
check inside the commit. So piece 5 is the mechanism either way, and it is
small enough to ship without reopening RDR-193. If RDR-193 reopens, its commit
carries the check. Also, the combined write (M-e) already is a per-document
single-transaction commit for the repo path; the streaming PDF path is the one
that would need moving.

### Wire contract and paired release

Every piece is `[additive]` in `docs/wire-contract-pending.md`'s sense.

- **Request id + ledger + status route.** OLD client + NEW engine: no header,
  so the ledger never engages and every route behaves byte-identically; the
  status route is unreachable surface. NEW client + OLD engine: the header is
  an unknown request header, ignored by `com.sun.net.httpserver`; the status
  route answers 404, which the client maps to "no ledger" and falls back to
  today's behavior (sweeps surface the gateway error, upserts resend on the
  r46u9 floor). No refusal window in either order, so choreography (a)
  applies: deploy the engine before the client tag.
- **Run fence.** OLD client + NEW engine: no `doc_id`/`run_id`, no check, today's
  merge. NEW client + OLD engine: the fields are ignored by the schemaless body
  map; `stale_run` is absent and the client treats absence as "not stale".
- Each both-halves commit gets its `## Unshipped` entry with the leading
  `[additive]` token and both-directions prose. The client release that
  carries each client half bumps `REQUIRED_ENGINE_VERSION` to the engine tag
  carrying its engine half (unconditional project rule).
- The ledger changeset is DDL only (no row DML), so it does not enter the
  rehearsal seed set; it does enter the PITR-fork Liquibase walk conexus runs
  before any tag carrying a changeset.

## Alternatives Considered

- **A `request_id` column on `gc_audit`.** Sweeps already write `gc_audit` in
  the move transaction, so a row with the id means committed. Cheap, sweep
  only, and it cannot tell in-flight from rolled back (no row either way).
  A reasonable Phase-1-lite if Sam wants the sweep half without a new table.
- **Async jobs for sweeps** (202 + poll, the `RekeyJobs`/RDR-193 `EngineJobs`
  shape). Solves the edge timeout but the registry is in memory per instance
  (state lost on restart reads as unknown), and it changes the response
  contract, so it is not additive.
- **In-flight coalescing** (nexus-8hdg9 Option D: a process-wide map from
  chash set to a future). Bounds duplicate embeds on one instance, keeps no
  durable status, and does not answer ll31n.
- **Client-side census before and after** (the operator practice during the
  ll31n incident). Racy: a census taken before the open transaction ends
  reports nothing moved.
- **Making the key required on sweep routes.** Would make ll31n protection
  universal but breaks every old client; not additive. Open question below.

## What this does NOT close

- **Cancellation.** The engine still cannot detect a disconnected client
  (nexus-8hdg9 §2 Option A). A sole abandoned attempt runs to commit or to its
  deadline; the ledger prevents a SECOND concurrent attempt, not the first.
  Voyage calls already dispatched are billed either way.
- **Late commit of a sole attempt after the client died** (the mfw6c shape
  without piece 5): bounded by the lease, not closed.
- **Shared-chunk metadata across documents** is last-writer-wins by design;
  the run fence is per document and cannot adjudicate between two documents
  that share chunk text.
- **Unkeyed callers**: old clients, scripts calling `_post` directly, any
  route not yet keyed.
- **Whether the edge forwarded a request**: "unknown" makes a resend safe; it
  does not say whether the edge ever passed the first one on.
- **Multi-request operations**: a whole index run, or a `row_limit` sweep
  loop, is many requests; the key covers one request each.
- **The other brittleness items**: fail-open follow-ups (P0.1), stale vectors
  and vector provenance (P4, tysei), the server-side reconcile (RDR-193).
- **Status after retention**: a purged request reads `unknown`.

## Implementation Plan

### Phase 0: instrument (engine only, no wire change) and query (conexus)

- Raced-embed counter: `RETURNING chash, (xmax = 0)` on the `upsert-chunks`
  INSERT and in `CombinedWriteService`; `event=upsert_embed_raced` log line;
  `raced_embeds_total` on `GET /v1/status`.
- conexus runs the M-c query once, and the edge-504 count from M-a.
- Exit: two numbers exist. Phase 2 proceeds only if `raced_embeds_total`
  over a representative window is material (threshold Sam's call); Phase 3
  proceeds only if M-c finds real cases.
- Engine write-path change: needs the standing throughput A/B (one extra
  RETURNING on the hot INSERT).

### Phase 1: ledger + sweeps + status route (closes ll31n)

- Changeset: table, RLS, grants. Update the three hand-kept lists for a new
  tenant table: the Liquibase schema test for the family,
  `JooqRecordReflectionFeatureTest.EXPECTED_RECORD_TYPES`, and
  `_RLS_TENANT_TABLES` in `src/nexus/health.py` with the fixture in
  `tests/test_health_service_checks.py`.
- `RequestLedger` component (constructor-injected into `VectorHandler` and
  `RequestHandler`), claim/marker/fail/status, retention arm.
- Keyed handling on the three GC routes; `GET /v1/requests/<id>`.
- Client: key minting in `gc_quarantine_orphans`, `gc_restore_rereferenced`,
  `gc_expire_quarantine`; status polling on ambiguity; 409/422 handling.
- Engine cut, deploy, then the client release (choreography (a)).

### Phase 2: content-addressed writes (closes wvek6)

- Keyed handling on `upsert-chunks`, then `store-put` and `write_many` with
  chunks; claim folded into the existing partition transaction.
- Client: per-page key in `upsert_chunks`, per-page in `write_manifest_many`,
  per-call in `put`; 409 means poll.
- Throughput A/B required (standing directive): the claim adds one INSERT to
  an existing transaction and one UPDATE to another.
- Exit: `raced_embeds_total` from Phase 0 reads near zero on the same
  workload after this ships.

### Phase 3: run fence on document-scoped writes (mfw6c), conditional

- Engine: shared advisory lock + `index_run_id` compare in the three
  metadata-merging transactions; exclusive in `beginIndexRun`; `stale_run`.
- Client: thread `run_id` from `_fence_begin` into the streaming uploader,
  the post-pass and the batch PDF path; abandon the run on `stale_run`.

### Independence

Phase 0 and the M-c query need nothing else and can ship first. Phase 1
depends only on the changeset. Phase 2 depends on Phase 1's ledger. Phase 3
depends on nothing in Phases 0-2.

## Test Plan

Java (engine, jOOQ only, real Postgres substrate):

- `RequestLedgerTest`: claim; replay of committed (no work runs: assert the
  embedder/SQL-function invocation count stays flat); 409 on in-flight; 422 on
  a changed body; takeover after lease expiry; an attempt whose lease expired
  mid-work cannot commit and its mutation rolls back; failed then resend
  executes once.
- RLS: tenant B cannot read tenant A's ledger rows; the status route returns
  404/unknown across tenants.
- Sweep route: two concurrent same-key calls produce one 200 and one 409; a
  status read during the held sweep (test hook holding the transaction) says
  `in_flight`, after commit `committed` with the true `moved`.
- Upsert: two concurrent same-key pages invoke the embedder once
  (`onnxInvocationCount` or a counting fake embedder).
- Phase 0 counter: two different keys over overlapping chashes, forced to
  interleave with the existing `afterExistencePartitionHookForTests` seam;
  `raced` equals the overlap.
- Retention: deletes only terminal rows past the cutoff and expired in-flight
  rows past the cutoff; never a live lease.
- Phase 3: `beginIndexRun` for N+1 blocks while run N's write transaction
  holds the shared lock; a run-N write after begin merges no metadata and
  reports `stale_run`.

Python (client):

- The key is identical across `_request`'s gateway retries,
  `_vector_with_retry` attempts, and `write_with_registration_retry` (fake
  server recording headers).
- 409 `request_in_flight` leads to status polling, never a re-POST.
- Old engine (header ignored, status 404) reproduces today's behavior exactly.
- Scenario on the engine substrate, the ll31n replay: a sweep held past a
  client socket timeout reports committed with the true moved count and no
  500.
- Scenario, the w94eo case-1 shape: three 504s on one page, embedder called
  once for that page, and nothing in flight when the post-pass runs.
- Wire-contract lint entries; the local-service gate, because shared HTTP
  client plumbing (`_post`/`_request_once` headers) changes.

Minimum viable validation: the two scenarios above on a real substrate, plus
Phase 0's counter reading a non-zero value under a forced overlap and zero
under the keyed Phase 2 path.

## Decisions (Sam, 2026-09-27)

1. **Own RDR.** This is a separate record, not an amendment to RDR-193.
2. **nexus-mfw6c waits for measurement.** Phase 3 (the run fence) is built
   only if the M-c query finds real late cross-run commits.
3. **Start small.** Phase 0 goes first: the engine-only raced-embed counter,
   plus conexus running the M-c query and the edge-504 count. Phases 1 and 2
   wait on those numbers. Phase 0 rides `engine-service-v0.1.137`.

Record: T2 `nexus/request-identity-decisions-2026-09-27`.

## Open Questions (for Sam)

Questions 3, 4 and 6 are answered above. Still open: 1, 2, 5 and 7.

1. Header name: `Idempotency-Key` (IETF draft semantics) or a house
   `X-Nexus-Request-Id`.
2. Retention default (proposed 7 days) and whether to store full responses or
   summaries only.
3. Own RDR, or an amendment inside RDR-193 (deferred)? The brittleness
   proposal places P1/P3 there; this draft argues for a separate record
   because the ledger is route-generic and RDR-193 is about reconcile and
   taxonomy.
4. mfw6c: M-d weakens the harm argument (the embedding_model revert rewrites
   an equal value in a conformant collection). Build Phase 3 now, wait for the
   M-c query, or fold it into a reopened RDR-193?
5. Should a keyless sweep ever be refused (not additive), or stay optional
   forever?
6. Phase 1-lite: accept the `gc_audit.request_id` column (committed-or-not for
   sweeps only, no new table) as the first step?
7. Is the managed service one engine instance or several? The lease design
   uses the database clock and holds either way, but the capability echo and
   retention arm should run once per database, not once per instance.

## Revision History

- 2026-09-27: created as RDR-222 from the T2 draft; Sam's decisions recorded; Problem Statement gaps given `#### Gap N:` headings.

- 2026-09-27: draft body written by architect-planner, read-only, held in T2.

