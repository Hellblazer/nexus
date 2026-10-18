---
title: "Request Identity for Mutating Engine Routes"
id: RDR-222
type: Architecture
status: accepted
accepted_date: 2026-10-18
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-27
related_issues: [nexus-ll31n, nexus-wvek6, nexus-mfw6c, nexus-r46u9, nexus-w94eo, nexus-vhyar, nexus-8hdg9]
related_rdrs: [RDR-181, RDR-191, RDR-192, RDR-193, RDR-108, RDR-103, RDR-225, RDR-229]
---

# RDR-222: Request Identity for Mutating Engine Routes

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Draft written read-only against develop b1d900fd6, 2026-09-27. Phase 0 has
since shipped (`engine-service-v0.1.137`); no other phase has code.

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
   `src/nexus/db/gateway_backoff.py`, shipped v7.58.0). The incident ran the
   unbounded sweep. Since then the indexer and `nx t3` call the bounded sweep
   loop (`row_limit`, nexus-a6mon), each batch under a 25 s statement bound
   and a 2 s lock bound (222-research-8, -9). They fall back to the unbounded
   form only when the client object lacks the bounded method; an older engine
   that ignores `row_limit` runs the unbounded sweep inside the bounded call.
   What is left of Gap 1: a bounded batch still runs close to the edge bound
   (25 s, which already includes the lock wait, plus pool wait, against about
   30 s), the expire route runs with no statement bound, and none of them
   reports committed, in flight or rolled back. Phase 1 closes this for the
   bounded quarantine and restore sweeps; expire and `quarantine-restore`
   stay outside it (Technical Design 2). The
   three-state question is still unanswerable.
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

Terms used below. A **chash** is the SHA-256 of a chunk's text; since
RDR-225 a row of `nexus.chunks` is identified by `(tenant_id, collection,
chash, embedding_model)` (vectors-030 :677, 222-research-12). The **fence** is the per-document run record on
`nexus.catalog_documents` (catalog-020: `index_state`, `index_content_hash`,
`index_run_id`, `index_started_at`), stamped `indexing` before a run's first
chunk and `complete` after its verified last manifest write. **Tenant RLS** is
Postgres row-level security keyed on the `nexus.tenant` setting that
`TenantScope.withTenant` stamps per transaction.

## Context

### Background

The problem surfaced on 2026-09-16 during the owner-1.1 cleanup (nexus-ll31n,
Gap 1): a sweep that committed was reported as failed, and its auto-retry
then failed against the sweep gate the first call still held. The auto-retry
half shipped in v7.58.0. What remains is that no response, and no read
afterwards, tells a caller whether a mutating request committed, is still
running, or rolled back. Gaps 2 and 3 are the same blindness on the write
routes. Phase 0 of this RDR (the raced-embed counter) shipped in
`engine-service-v0.1.137` and read small (Decisions after Phase 0), which
deferred Gap 2 and closed Gap 3. Phase 1, the ledger for sweeps, is what this
record now carries to implementation.

### Technical Environment

- The engine: Java, `com.sun.net.httpserver`, jOOQ over Postgres with
  Liquibase changesets, tenant isolation by RLS through `TenantScope`.
- The managed edge in front of the cloud engine cuts a request at a fixed
  bound (about 30 s; 55 s on the embed-write routes since conexus PR #358).
- The client: Python, `HttpVectorClient` and the catalog HTTP client, with
  gateway retry in `src/nexus/db/gateway_backoff.py` and the r46u9 30 s
  resend floor after a 504.
- The quarantine and restore sweeps run one SQL function inside one
  `withTenant` transaction under the RDR-191 sweep gate (an advisory lock)
  and write a `nexus.gc_audit` row in that same transaction. Expire takes no
  gate, and `quarantine-restore` commits once per quarantine sibling
  (222-research-10, -14).
- One managed engine instance today (open question 7, answered 2026-10-17).

## Research Findings (measurements done for this draft)

### Investigation

All five (M-a to M-e) are code reads against develop b1d900fd6. No production data was
read; where a number needs production, the exact query is given and the number
is left unknown. On 2026-10-18 the load-bearing claims were re-read at
develop ecd51d2c3 and filed as T2 research records `nexus_rdr/222-research-1`
to `-7`; they still hold, and only some line numbers moved (the records give
both). Records -8 to -14 carry the Phase 1 facts the gate asked for. Phase 0's production readings are in the Decisions after Phase 0.

### Key Discoveries

- **✅ Verified** (source search): one request never embeds a duplicate
  chash twice (M-a). *Source: 222-research-1.*
- **✅ Verified** (source search): the engine carries no request identity
  today; Phase 0's raced-embed signal is live. *Source: 222-research-2.*
- **✅ Verified** (source search): the existence partition sees only
  committed rows, so the duplicate window is a whole embed (M-b).
  *Source: 222-research-3.*
- **✅ Verified** (source search): no write compares `index_run_id`; chunk
  metadata carries no run id (M-c). *Source: 222-research-4.*
- **❓ Assumed** (source search): the M-c detection query would fire on a
  real late commit. It has no known positive. *Source: 222-research-5.*
- **❓ Assumed** (source search): the M-d harm downgrade holds. It rests on
  the conformant collection name naming the model, which RDR-229 retires for
  new collections (Sam, 2026-10-18), so it must be restated against the
  registry row before Phase 3 is ever built. *Source: 222-research-6.*
- **✅ Verified** (source search): the sweep gate, `gc_audit` in the move
  transaction, and the request deadline exist (M-e).
  *Source: 222-research-7.*
- **✅ Verified** (source search): there are four sweep routes, with plain,
  bounded and floored repository forms; production calls the bounded and
  floored forms. *Source: 222-research-8.*
- **✅ Verified** (source search): only the bounded forms carry an effective
  statement bound (25 s); the unbounded quarantine function carries only its
  in-body 2 s lock bound, and the unbounded restore and the expire route
  carry no bound. *Source: 222-research-9.*
- **✅ Verified** (source search): the sweep handler builds its response
  body after the repository transaction, and `quarantine-restore` commits
  once per quarantine sibling. *Source: 222-research-10.*
- **✅ Verified** (source search): AuthFilter path-restricts only the mint
  and board-ci scopes; the client carries per-request values in
  thread-locals, not new parameters. *Source: 222-research-11.*
- **❓ Assumed** (docs only): `Idempotency-Key` passes through the managed
  edge, and so do the response headers the design adds. *Source:
  222-research-13.*

The measurements behind these entries follow.

#### M-a. Are duplicate-chash embeds logged, and what would count them?

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
  `EmbedActivitySnapshot` on `GET /v1/status` (as built: a top-level
  `raced_embeds_total` field instead, since the count has no embedder
  dimension; see Phase 0). Same change in
  `CombinedWriteService`'s per-doc write. Caveats: `xmax` is a system column,
  so the jOOQ form needs checking against `RawSqlGateTest`; the count cannot
  say WHICH other writer won (a retry of the same page, or another document
  with identical text) until upserts carry an id (Phase 2 below, deferred).
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

#### M-b. Does the RDR-181 existence partition see uncommitted rows?

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

#### M-c. Can a real late cross-run commit be detected from existing data?

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

#### M-d. Reassessing mfw6c's harm (found while doing M-c)

(Caveat, 2026-10-18: this argument reads the model from the conformant
name. Sam ruled on RDR-229 that new collections stop carrying that shape (recorded as
comments on bead nexus-j4gl6 and in 222-research-6; the RDR-229 file is
still a draft), so it must be restated
against the registry row before Phase 3 is ever reopened.)

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

#### M-e. Existing pieces this design reuses

- **Verified (source).** The combined write (`write_many` with `chunks`,
  nexus-kl2z6) already commits chunk rows, manifest rows and the completion
  stamp for one document in ONE short transaction, with embedding outside it
  (`CombinedWriteService` javadoc; `CatalogHandler.handleManifestWriteMany`).
  That is most of the "document-commit route" proposed as P3 of the
  indexing brittleness proposal (T2 `nexus/indexing-brittleness-proposal-2026-09-25`,
  which proposes reopening RDR-193 with it; RDR-193 has no such route) for
  the repo indexer path. The streaming PDF path (the w94eo exposure) does not
  use it: it streams `upsert-chunks` pages, then an `update-metadata` post-pass,
  then `index-run/complete`.
- **Verified (source).** The sweep SQL functions write a `nexus.gc_audit` row in
  the same transaction as the move (catalog-033, catalog-037/hygiene-008),
  under the sweep gate (`pg_advisory_xact_lock(hashtext('sweepgate:...'))`).
  The bounded forms run under `lock_timeout 2000` and `statement_timeout
  25000`, set before the call; the unbounded forms have no effective
  statement bound (222-research-9).
- **Verified (source).** A request deadline already exists end to end
  (nexus-8hdg9): `AuthFilter` mints it into `RequestContext` from
  `X-Nexus-Request-Deadline-Ms` or `NX_EMBED_DEADLINE_MS` (default 300 s), and
  the client sends 540 s on `upsert-chunks`. The ledger lease for
  `upsert-chunks` derives from it; a sweep lease derives from the sweep's own
  bounds (Technical Design 2).
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

Pieces 1-3 answer ll31n (Phase 1) and wvek6 (Phase 2, deferred). Piece 4
answers mfw6c (Phase 3, closed) and does not use the ledger at all.

### Technical Design

#### 1. Request id

- Header `Idempotency-Key: <uuid>` (Sam, 2026-10-17, with the IETF httpapi
  draft's semantics: `draft-ietf-httpapi-idempotency-key-header` uses this
  name with 409 for a concurrent duplicate and 422 for a key reused with a
  different body. Documented, recalled, not re-read).
- The client mints a UUIDv7 (time-ordered, so retention and log reading sort
  naturally) ONCE per logical request, at the call site that owns it: each page
  in `HttpVectorClient.upsert_chunks`'s page loop (Phase 2); each call of a
  keyed sweep method (Phase 1, Technical Design 2); each
  `write_manifest_many` page that carries chunks and each `put` (Phase 2). It must NOT be
  minted in `_post`/`_request`: `_vector_with_retry` and
  `write_with_registration_retry` re-invoke `_post` for each attempt, so a key
  minted there changes per attempt and defeats the design. The key reaches
  the request the way the client already carries per-request values: a
  thread-local, set at the minting call site and cleared in a `finally` around
  the call, read by `_request_once` when it builds headers. That reuses the
  reason the client keeps `_response_header_capture` and
  `_sent_bearer_capture` in thread-locals (new parameters on `_request_once`
  and `_post` are deliberately avoided, 222-research-11), in the request
  direction; the nearest request-side precedent is the per-path deadline
  header. Status-poll GETs run outside that set-and-clear and carry no key. The catalog httpx
  client (`_refreshable_client.py`) needs the same only in Phase 2.
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

1. **Claim**, in a transaction that commits before the work starts: its
   own short transaction for the sweeps (one extra pool checkout per keyed
   sweep), the first statement of the existing partition transaction for
   `upsert-chunks` (Phase 2). A claim folded into the sweep's own transaction
   would be invisible until commit, so the status route could never say
   `in_flight`. `INSERT ... VALUES (..., state='in_flight', holder=:nonce,
   lease_until = clock_timestamp() + :lease) ON CONFLICT DO NOTHING`. The
   lease is the attempt's own bound plus a margin that covers the pool wait
   before the work starts: the service pool's connection timeout (30 s,
   222-research-14) plus 10 s. For a keyed sweep that is its statement bound,
   which already includes its lock wait, plus the margin: 25 s + 40 s = 65 s.
   For `upsert-chunks` (Phase 2) it is the deadline minted for this request
   (the client header, else `NX_EMBED_DEADLINE_MS`, capped by
   `NX_EMBED_DEADLINE_MAX_MS`) plus the same margin. An attempt that still
   outlives its lease (a pool wait past the timeout plus a slow lookup) is
   rolled back by the marker guard below, never committed late.
   On conflict, read the row:
   - `route` or `body_sha256` differs: **422**
     `{"error":"idempotency_key_reused"}`.
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
4. **Failure.** On an exception before the mutation's transaction commits,
   a best-effort separate transaction sets `state='failed'`, `holder=NULL`,
   `response_status`, guarded `WHERE holder=:nonce AND state='in_flight'`. The
   catch wraps only the work up to that commit. An exception after the commit
   (writing the response to a client the edge has already cut, say) never
   touches the row, so a committed request can never read `rolled_back`. If
   the engine dies, the lease expiry covers it.

Where the marker goes per route:

- **Sweeps.** Phase 1 keys these, and only these (222-research-8, -9):

  | route | repository form keyed | client method |
  |---|---|---|
  | `/gc/quarantine-orphans` with `row_limit`, no floor | `quarantineOrphansBounded` | `gc_quarantine_orphans_bounded` |
  | `/gc/quarantine-orphans` with `row_limit` and `floor_fraction` | `quarantineOrphansFloored` with a row limit (bounded only then, 222-research-14) | `gc_quarantine_orphans_bounded` with a floor |
  | `/gc/restore-rereferenced` with `row_limit` | `restoreRereferencedBounded` | `gc_restore_rereferenced_bounded` |

  Not keyed in Phase 1, each for a stated reason:
  - the unbounded `quarantineOrphans`, `restoreRereferenced` and the
    floor-only `quarantineOrphansFloored` (no `row_limit`): no effective
    statement bound, so no lease can cover them;
  - `/gc/expire-quarantine`: it has no bounded form to loop on, so a
    statement bound would roll back every large expiry, and it takes no
    sweep gate (222-research-14). A bounded expire form is follow-up work;
  - `/gc/quarantine-restore`: it commits once per quarantine sibling and so
    cannot carry one marker; it keeps its typed 503 busy body with the audit
    ids it wrote.

  Unkeyed routes keep `is_non_idempotent_sweep_path` exactly as today.
  Each keyed form is one `withTenant` transaction around one SQL function
  call. The marker UPDATE goes in that transaction after the call. The
  response body is built by the handler after the repository returns
  (222-research-10), so the three keyed repository methods take a
  `LedgerMarker` callback, run inside the transaction with the outcome
  record; it builds the body with that route's body builder (each keyed
  shape's inline builder moved to a static function of the outcome and the
  request echoes) and writes the marker. Replay returns that stored body: the
  body the first caller saw, except that a sample over the cap is truncated
  (stored response, below).
  A keyed sweep whose gate wait times out (`lock_not_available` with the
  RDR-191 sweep gate marker in its message, which the engine already tells
  apart from a row-lock timeout, 222-research-14) returns a typed 503
  `{"error":"sweep_gate_busy"}` with `Retry-After`, never the opaque 500, and
  marks only its own row failed. A row-lock timeout keeps today's error.
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

Stored response: the route's full JSON body, capped at 64 KB (Sam,
2026-10-17). A sweep `sample` (up to 5,000 chashes under the `gc_audit`
clamp) can exceed the cap, and for a large sweep it usually does: the
indexer asks for 5,000 (222-research-12), and a bounded batch moves up to
its `row_limit`. The stored copy then truncates the sample to fit, with
`sample_truncated: true`, and the full list stays in `gc_audit` (the
outcome records carry no audit id, so the stored copy names none). (How a too-large sample is cut is this draft's reading
of the cap, not a separate ruling.)

Retention: a scheduled engine arm (the tuple-sweep scheduling pattern in
`NexusService.runScheduledTupleSweep`) deletes terminal rows older than
`NX_REQUEST_LEDGER_RETENTION_DAYS` (default 7, Sam 2026-10-17) and in_flight rows whose lease
expired more than that long ago. Never an in_flight row within its lease.
The ledger is FORCE RLS, so the arm runs one `withTenant` pass per tenant
(how it lists tenants is decided at implementation).

Size: one row per keyed mutating request. Indexing runs are the volume
(one row per `upsert-chunks` page, 16 chunks per page local, 64 CCE, 300 code),
so 7 days of rows is the order of pages indexed in a week. Measure the table
after Phase 2 keys the write routes; the 7-day default (Sam, 2026-10-17)
is revisited only if that measurement argues for it.

#### 3. Status route

`GET /v1/requests/<request_id>` (new `RequestHandler`, registered with
`AuthFilter` as the authenticated `/v1` contexts are; AuthFilter
path-restricts only the mint and board-ci scopes, so tenant-scope tokens
reach it, 222-research-11), tenant-scoped by RLS. It answers **200** with a
`state` body for every row below, `unknown` included, and **400** for a
malformed id. **404** comes only from an engine without the route (no
context registered for it), never from this route:

| ledger row | reported state |
|---|---|
| committed | `committed`, with `response_status` and the stored response |
| in_flight, lease live | `in_flight`, with `started_at`, `lease_until` |
| in_flight, lease expired; or failed | `rolled_back` (sweeps: nothing applied; content-addressed routes: see above) |
| no row | `unknown`: never reached the engine, or purged by retention |

"unknown" is not ambiguous in practice: resending with the SAME key is safe in
every state (committed replays, in_flight 409s, rolled_back and unknown
execute once).

#### 4. Client behavior

- **Sweeps (ll31n).** `gateway_backoff.is_non_idempotent_sweep_path` stays
  in force for every sweep, keyed or not, so `_request` never gateway-retries
  a sweep. The keyed sweep method itself, above `_request`, handles an
  ambiguous outcome, and only once this engine has echoed `Idempotency-Key`
  on an earlier response (the capability rule below); before that it
  behaves exactly as today. On a gateway code or a socket timeout it does
  not report failure: it polls `GET /v1/requests/<id>` (every 5 s, up to the
  lease). `committed` returns the stored outcome as if the call had
  succeeded; `rolled_back` and `unknown` resend with the same key;
  `in_flight` keeps polling; a 404 from the status route falls back to
  today's behavior (surface the gateway error). A 503 `sweep_gate_busy`
  means nothing was applied: wait `Retry-After`, then resend with the same
  key. Resends are capped at 3 per logical request; past the cap the
  method surfaces the last outcome. This replaces "never retry" with "retry
  safely by key".
- **upsert-chunks (wvek6).** Keep the r46u9 30 s floor on a 504, then resend
  with the same key. A 409 `request_in_flight` means poll status, not
  re-POST. A replay counts as the ack (the `upserted` echo check at
  `http_vector_client.py:3037-3038` runs against the replayed body).
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

#### 6. Relation to the proposed document-commit route (brittleness proposal P3)

The document-commit route narrows mfw6c's window from "a whole embed" to
"one short transaction" but does not close it: a delayed commit request from
run N can still land after run N+1's. It closes it only with the same fence
check inside the commit. So the run fence (piece 4, Technical Design 5) is the mechanism either way, and it is
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

### Alternative 1: A `request_id` column on `gc_audit` (Phase 1-lite)

**Description**: sweeps already write `gc_audit` in the move transaction, so
a row carrying the caller's id means committed.

**Pros**:

- Cheap: one column, no new table, sweeps only.

**Cons**:

- No row reads the same for in flight and rolled back, the ambiguity Gap 1
  exists to remove.

**Reason for rejection**: Sam, 2026-10-18 (open question 6): Phase 1 builds
the full ledger.

### Alternative 2: Async jobs for sweeps

**Description**: 202 plus polling, the `RekeyJobs` / RDR-193 `EngineJobs`
shape.

**Pros**:

- Removes the edge timeout from the sweep path.

**Cons**:

- The job registry is in memory per instance, so a restart turns every
  running job into unknown.
- Changes the response contract, so it is not additive.

**Reason for rejection**: loses state on restart and breaks old clients.

### Alternative 3: In-flight coalescing

**Description**: nexus-8hdg9 Option D, a process-wide map from a chash set
to a future.

**Pros**:

- Bounds duplicate embeds on one instance.

**Cons**:

- Keeps no durable status, so it does not answer Gap 1.

**Reason for rejection**: answers Gap 2 only, and only per instance.

### Alternative 4: Client-side census before and after

**Description**: the operator practice during the ll31n incident.

**Pros**:

- Needs no engine change.

**Cons**:

- Racy: a census taken before the open transaction ends reports nothing
  moved.

**Reason for rejection**: it produced the wrong answer in the incident.

### Alternative 5: Make the key required on sweep routes

**Description**: refuse a keyless sweep.

**Pros**:

- Gap 1 protection would be universal.

**Cons**:

- Breaks every old client and every hand call; not additive.

**Reason for rejection**: Sam, 2026-10-17 (open question 5): a keyless sweep
is never refused.

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

## Trade-offs

### Consequences

- Positive: a keyed sweep reports committed, in flight or rolled back
  exactly, and a resend with the same key is safe in every state.
- Positive: every piece is additive; old clients and old engines behave as
  today (Wire contract and paired release).
- Negative: one new tenant table, with its RLS policy, grants and the
  hand-kept lists a new tenant table needs (Phase 1).
- Negative: a keyed sweep takes one extra pool checkout for its claim (and
  one for a failure marker); a keyed write route (Phase 2) gains a claim
  INSERT and a marker UPDATE in existing transactions (Phase 2's throughput
  A/B measures it).
- Negative: expire and `quarantine-restore` stay unkeyed in Phase 1
  (Technical Design 2).
- Negative: unkeyed callers, sole abandoned attempts and multi-request
  operations stay outside the guarantee (What this does NOT close).

### Risks and Mitigations

- **Risk**: an attempt outlives its lease and commits after another attempt
  took over. **Mitigation**: the marker UPDATE is guarded on the holder and
  on `lease_until > clock_timestamp()` inside the mutation's own
  transaction; zero rows throws, and the mutation rolls back with it.
- **Risk**: the claim adds a pool checkout on the upsert path (the open
  RDR-181 pool risk). **Mitigation**: the claim folds into the existing
  partition transaction as its first statement.
- **Risk**: `resolveNeedEmbedIdx` swallows exceptions into "embed
  everything", which would hide a claim conflict. **Mitigation**: the claim
  conflict raises a typed exception outside that swallow (Technical Design
  2); a test pins it.
- **Risk**: the ledger table lands with no grants (the v0.1.78 zero-grants
  lesson). **Mitigation**: grants go in the runAlways `grants-nexus-svc.xml`
  and `grants-nexus-diag.xml` families, and the PITR-fork Liquibase walk runs
  before the tag.
- **Risk**: the managed service scales past one instance and the retention
  arm or capability echo runs per instance. **Mitigation**: none built yet,
  by decision (open question 7); revisit at scale-out.

### Failure Modes

- Visible: a 409 `request_in_flight` or a 422 `idempotency_key_reused`, each
  with its own `error` code; the client dispatches on `error`.
- Visible: a status read of `unknown` for a request older than retention or
  one that never reached the engine; an engine without the ledger answers
  404 instead. A resend with the same key is safe.
- Silent if wrong: the client mints the key per attempt instead of per
  logical request (inside `_post`), which defeats the design without an
  error. The client test that the key is identical across every retry layer
  guards it (Test Plan).
- Recovery: an engine crash leaves an `in_flight` row; its lease expires and
  the next same-key attempt takes over. Diagnosis: read the ledger row
  through `GET /v1/requests/<id>` or as the diag role.

## Implementation Plan

### Phase 0 (SHIPPED, engine-service-v0.1.137): instrument (engine only, no wire change) and query (conexus)

- Raced-embed counter: `RETURNING chash, (xmax = 0)` on the `upsert-chunks`
  INSERT and in `CombinedWriteService`; `event=upsert_embed_raced` log line;
  `raced_embeds_total` on `GET /v1/status`.
- conexus runs the M-c query once, and the edge-504 count from M-a.
- Exit: two numbers exist. Phase 2 proceeds only if `raced_embeds_total`
  over a representative window is material (threshold Sam's call); Phase 3
  proceeds only if M-c finds real cases.
- Reading the number. A raced embed is either a retry of the same page or two
  different documents embedding the same new text at once; the counter
  cannot tell which (M-a's caveat), and Phase 2 fixes only the first. Each
  `event=upsert_embed_raced` line therefore carries up to 8 raced chashes, so
  an operator can join them against `catalog_document_chunks` and split
  single-owner chashes (likely retries) from multi-owner ones (shared text)
  before applying the threshold. Within one combined write, a chash shared by
  two of its documents counts once, on its first per-doc write.
- The counter is per engine process and resets on restart. `GET /v1/status`
  carries `process_start_time` beside it (the value `/version` serves), so a
  window whose start time changes between polls is discarded, not summed.
  One instance per poll: Open Question 7 bounds any fleet-wide reading.
- Definition change, 2026-10-02 (nexus-wbfpw.66). From the engine tag carrying that
  bead, `raced_embeds_total` counts committed writes only, from both feeders. The
  `upsert-chunks` feeder always did; the combined-write feeder counted inside its
  transaction, so a write that was rolled back (a deadlock victim, a failed
  document) still added to it. A reading that drops across that cut is a change
  of definition, not necessarily a change of rate: compare windows on one side of
  it.
- Engine write-path change: the standing throughput A/B (one extra
  RETURNING on the hot INSERT) runs at the `engine-service-v0.1.137` cut,
  in `--shakeout`'s index-throughput gate; no local harness runs it earlier
  (T2 `nexus/ulrjq-throughput-ab`).

### Phase 1: ledger + sweeps + status route (closes ll31n)

- Changeset: table, RLS, grants. Update the three hand-kept lists for a new
  tenant table: the Liquibase schema test for the family,
  `JooqRecordReflectionFeatureTest.EXPECTED_RECORD_TYPES`, and
  `_RLS_TENANT_TABLES` in `src/nexus/health.py` with the fixture in
  `tests/test_health_service_checks.py`.
- `RequestLedger` component (constructor-injected into `VectorHandler` and
  `RequestHandler`), claim/marker/fail/status, retention arm.
- Keyed handling on the three sweep forms in the Technical Design 2 table
  (`LedgerMarker` callback, typed `sweep_gate_busy` 503);
  `GET /v1/requests/<id>`.
- Client: key minting, through the thread-local, in
  `gc_quarantine_orphans_bounded` (with and without a floor) and
  `gc_restore_rereferenced_bounded`; status polling on ambiguity once the
  engine has echoed the key, `unknown` and `sweep_gate_busy` resending with
  the same key under the cap of 3; 409/422 handling.
- Logs: one INFO line per replay, 409, 422 and takeover
  (`event=request_ledger_*`), and in-process counters of the same events
  since boot in `GET /v1/status` (the `raced_embeds_total` shape; that route
  is unauthenticated and the ledger is FORCE RLS, so it carries no row
  count). Wire-contract entry for the new status fields.
- Changeset: the next free id at implementation. The ledger is DDL only
  and depends on no quarantine function; the marker is Java.
- Before the client half ships: confirm with conexus that the managed edge
  passes the `Idempotency-Key` request header and the `Idempotency-Key` and
  `Idempotency-Replayed` response headers (222-research-13).
- Engine cut, deploy, then the client release (choreography (a)).

### Phase 2 (DEFERRED, Sam 2026-09-28): content-addressed writes (closes wvek6)

- Keyed handling on `upsert-chunks`, then `store-put` and `write_many` with
  chunks; claim folded into the existing partition transaction.
- Client: per-page key in `upsert_chunks`, per-page in `write_manifest_many`,
  per-call in `put`; 409 means poll.
- Throughput A/B required (standing directive): the claim adds one INSERT to
  an existing transaction and one UPDATE to another.
- Exit: `raced_embeds_total` from Phase 0 reads near zero on the same
  workload after this ships.

### Phase 3 (CLOSED, Sam 2026-09-28; design of record only): run fence on document-scoped writes (mfw6c)

- Engine: shared advisory lock + `index_run_id` compare in the three
  metadata-merging transactions; exclusive in `beginIndexRun`; `stale_run`.
- Client: thread `run_id` from `_fence_begin` into the streaming uploader,
  the post-pass and the batch PDF path; abandon the run on `stale_run`.

### Independence

Phase 0 shipped. Phase 1 depends only on the changeset. Phase 2 depends on Phase 1's ledger. Phase 3
depends on nothing in Phases 0-2.

## Test Plan

### Phase 1

Java (engine, jOOQ only, real Postgres substrate):

- `RequestLedgerTest`: claim; replay of committed (no work runs: assert the
  SQL-function invocation count stays flat); 409 on in-flight; 422 on a
  changed body, and 422 on the same key and body sent to a different route;
  takeover after lease expiry; an attempt whose lease expired mid-work cannot
  commit and its mutation rolls back; failed then resend executes once; a
  committed request followed by an exception (the response write fails)
  still reads `committed`.
- RLS: tenant B cannot read tenant A's ledger rows; the status route answers
  200 `unknown` for another tenant's id.
- Sweep route: two concurrent same-key calls produce one 200 and one 409; a
  status read during the held sweep (test hook holding the transaction) says
  `in_flight`, after commit `committed` with the true `moved`; a same-key
  takeover that meets a gate still held answers 503 `sweep_gate_busy`.
- Replay body: a replayed keyed sweep whose sample fits the cap returns the
  same body the first call returned (`floor`, `row_limit`, `remaining`
  included); one over the cap returns it with the sample truncated and
  `sample_truncated: true`.
- Gate busy: a keyed sweep whose gate wait times out answers 503
  `sweep_gate_busy`; a row-lock timeout does not.
- Retention: deletes only terminal rows past the cutoff and expired in-flight
  rows past the cutoff; never a live lease.

Python (client):

- The key is identical across every resend of one logical request, and the
  thread-local is clear after the call (fake server recording headers).
- `_request` never gateway-retries a sweep, keyed or not.
- 409 `request_in_flight` leads to status polling, never a re-POST;
  `unknown` and `sweep_gate_busy` resend with the same key; resends stop at
  the cap of 3.
- Old engine (header ignored, no echo, status 404) reproduces today's
  behavior exactly, the key sent or not.
- Scenario on the engine substrate, the ll31n replay: a keyed bounded sweep
  held past a client socket timeout reports committed with the true moved
  count and no 500.
- Wire-contract lint entries; the local-service gate, because shared HTTP
  client plumbing (`_request_once` headers) changes.

Minimum viable validation for Phase 1: the ll31n replay scenario on a real
substrate, plus the `RequestLedgerTest` post-commit-exception case.

### Phase 0 (shipped)

- Phase 0 counter: two different keys over overlapping chashes, forced to
  interleave with the existing `afterExistencePartitionHookForTests` seam;
  `raced` equals the overlap.

### Kept with the deferred and closed design (Phases 2 and 3)

- Upsert: two concurrent same-key pages invoke the embedder once
  (`onnxInvocationCount` or a counting fake embedder).
- The key is identical across `_vector_with_retry` attempts.
- Phase 3: `beginIndexRun` for N+1 blocks while run N's write transaction
  holds the shared lock; a run-N write after begin merges no metadata and
  reports `stale_run`.
- The key is identical across `write_with_registration_retry` attempts.
- Scenario, the w94eo case-1 shape: three 504s on one page, embedder called
  once for that page, and nothing in flight when the post-pass runs.
- Phase 0's counter reads non-zero under a forced overlap and zero under the
  keyed Phase 2 path.

## Decisions (Sam, 2026-09-27)

1. **Own RDR.** This is a separate record, not an amendment to RDR-193.
2. **nexus-mfw6c waits for measurement.** Phase 3 (the run fence) is built
   only if the M-c query finds real late cross-run commits.
3. **Start small.** Phase 0 goes first: the engine-only raced-embed counter,
   plus conexus running the M-c query and the edge-504 count. Phases 1 and 2
   wait on those numbers. Phase 0 rides `engine-service-v0.1.137`. The
   threshold is applied to the retry share of the count, not the raw total
   (see Phase 0, "Reading the number").

Record: T2 `nexus/request-identity-decisions-2026-09-27`.

## Decisions after Phase 0 (Sam, 2026-09-28)

Phase 0 read small (T2 `nexus/rdr222-phase0-interpretation`): at most 47
duplicate-embed requests over 09-17..09-26 and none since the 09-27 fast
503s; no real late cross-run commits in M-c, whose signal was a query artifact
plus two since-fixed causes (be444581c, and the quarantine-restore overwrite
fixed by catalog-043, nexus-brxnp).

1. **nexus-wvek6 (Phase 2) deferred** at P4: revisit after two weeks of
   `raced_embeds_total` readings from `engine-service-v0.1.137` (live
   2026-09-28); build only if the retry share is material. (From the engine tag
   carrying nexus-wbfpw.66 the counter counts committed writes only; see Phase 0.)
2. **nexus-mfw6c (Phase 3) closed**, no evidence. This section stays the
   design of record if the class ever appears.
3. **nexus-ll31n (Phase 1)** stays open; nothing measured argues for or
   against it.

## Decisions on the open questions (Sam, 2026-10-17)

1. **Header: `Idempotency-Key`**, with the IETF draft's semantics: 409 while
   a request with the key is in flight, 422 when the same key arrives with a
   different body.
2. **Retention: 7 days, full responses.** The ledger stores the original
   response body, capped at 64 KB, so a replay returns what the first call
   returned. A sweep's response is a small JSON object of counts.
3. **A keyless sweep is never refused.** The key stays optional, so old
   clients and hand calls keep working.
4. **The managed service is one engine instance for now.** No
   once-per-database guard is built for the retention arm or the capability
   echo yet; revisit if the service runs more than one instance.

Record: T2 `nexus/decision-rdr222-open-questions-2026-10-17`.

## Decision on open question 6 (Sam, 2026-10-18)

1. **No Phase 1-lite.** Phase 1 builds the full `request_ledger` and the
   status route. A `gc_audit.request_id` column alone cannot tell in flight
   from rolled back, which is the Gap 1 incident.

Record: T2 `nexus/decision-rdr222-q6-2026-10-18`. (This draft earlier said
open question 6 was answered on 2026-09-27; that record answers 3 and 4 only.)

## Open Questions (for Sam)

All seven are answered: 3 and 4 on 2026-09-27, 1, 2, 5 and 7 on 2026-10-17,
and 6 on 2026-10-18. The list stays as asked.

1. Header name: `Idempotency-Key` (IETF draft semantics) or a house
   `X-Nexus-Request-Id`.
2. Retention default (proposed 7 days) and whether to store full responses or
   summaries only.
3. Own RDR, or an amendment inside RDR-193 (deferred)? The brittleness
   proposal (T2 `nexus/indexing-brittleness-proposal-2026-09-25`) proposes
   reopening RDR-193 for its P3; this draft argues for a separate record
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

## Finalization Gate

### Contradiction Check

No contradictions found between the research findings, the decisions and the
proposed solution after this revision. Two the gate restructure found and
fixed: the stored-response cap (16 KiB in the Technical Design against Sam's
64 KB) and open question 6 (marked answered when no ruling existed). Gate
round 1 found the rest: the status route's 404 for two meanings, the sweep
inventory, the Test Plan's two acceptance bars and the phase labels, all
fixed in this revision. Its fix check found more, also fixed: the floored
sweep keyed without a bound, keyed expire, the replay test against the
sample cap, a row count on the unauthenticated status route, keyed sweeps
re-exposed to the gateway retry ladder, and `sweep_gate_busy` with no
client rule. One stated decision reads differently from the design it
governs: Decision 2 of 2026-10-17 calls a sweep response "a small JSON
object of counts", while a sample-bearing sweep response often exceeds the
64 KB cap; the design truncates the stored sample (Technical Design 2) and
the decision's cap stands.

### Assumption Verification

Phase 1 rests on 222-research-2 (no request identity today), -7 (sweep
gate, `gc_audit`, deadline), -8 (sweep route census), -9 (sweep bounds),
-10 (handler and transaction shape) and -11 (route admission, client
plumbing) and -14 (fix-check facts), verified by source search except
-14's pool timeout (read from a comment, not a setter) and its Postgres
statement-timeout semantics (not tested here). Two Phase 1 items are not
verified: `Idempotency-Key` and the response headers passing the managed
edge (222-research-13, assumed; checked with conexus before the client half
ships, Phase 1) and
the IETF draft's 409/422 semantics (recalled, not re-read; the design states
its own 409/422 rules, so it does not depend on the draft). The other two
assumed findings, 222-research-5 and -6, concern only Phase 3, which is
closed.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| sweep routes, repository forms, client methods | engine, client | Source Search (222-research-8) |
| bounded sweep statement and lock bounds | engine `PgSession`, changelogs | Source Search (222-research-9) |
| sweep body built after the transaction; restore per sibling | engine `VectorHandler` | Source Search (222-research-10) |
| AuthFilter scope gates; client thread-local plumbing | engine `AuthFilter`, client | Source Search (222-research-11) |
| `Idempotency-Key` across the managed edge | conexus edge | Assumed (222-research-13) |
| sweep SQL functions write `gc_audit` under the sweep gate | engine changelogs | Source Search (222-research-7) |
| `X-Nexus-Request-Deadline-Ms` / `NX_EMBED_DEADLINE_MS` | engine `RequestDeadline`, `AuthFilter` | Source Search (222-research-7) |
| `Idempotency-Key` 409/422 semantics | IETF httpapi draft | Docs Only, recalled, not re-read |

### Scope Verification

The Minimum Viable Validation is in scope for Phase 1 and is stated once,
in the Test Plan: the ll31n replay scenario on a real substrate (a keyed
bounded sweep held past a client socket timeout reports committed with the
true moved count and no 500), plus the post-commit-exception case in
`RequestLedgerTest`.

### Cross-Cutting Concerns

- **Versioning**: additive on the wire both ways; the client release bumps
  `REQUIRED_ENGINE_VERSION` to the engine tag carrying the engine half.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: engine first, then the client tag (choreography (a));
  one managed instance assumed.
- **IDE compatibility**: N/A.
- **Incremental adoption**: the key is optional; each route is keyed in its
  own phase.
- **Secret/credential lifecycle**: N/A; the key is a client-minted UUIDv7,
  not a secret.
- **Observability**: INFO lines for replays, 409s, 422s and takeovers, and
  in-process counters of them in `GET /v1/status` (Phase 1).
- **Memory management**: stored responses are capped at 64 KB per row;
  retention bounds the table to about a week of keyed requests.

### Proportionality

Phases 2 and 3 are deferred and closed respectively, and stay as the design
of record; nothing is trimmed. The record is larger than Phase 1 needs
because it keeps that design.

## Revision History

- 2026-10-18: accept residual dispositions: residual 1 [BLOCKS-PLANNING] carried by bead nexus-zxgld (Sam's choice); residual 2 [DISCOVER-AT-IMPLEMENTATION] carried by bead nexus-zxgld (Phase 1). Round 2 Significants: bead nexus-icdmw.

- 2026-10-18: Gate round 2 — PASSED (0 Critical, 4 Significant, 0 ship-blocker(s)); commit `0b2ec059b`; critique `nexus_rdr/222-gate-critique-2026-10-18-r2`.

- 2026-10-18: fix check of 00e637a13 (three runs, consensus PASS) findings fixed: floored sweep keyed only with `row_limit`; expire and floor-only quarantine left unkeyed with reasons; lease margin from the pool timeout; upsert lease from the per-request deadline; replay test and stored copy against the sample cap; in-process status counters instead of a row count; sweeps never gateway-retried, polling only after the key echo, resend cap of 3; `sweep_gate_busy` tied to the gate marker with a client rule; stale phase references. Sources in 222-research-14.

- 2026-10-18: Gate round 1 — PASSED (0 Critical, 11 Significant, 0 ship-blocker(s)); commit `674dcfe43`; critique `nexus_rdr/222-gate-critique-2026-10-18-r1`.

- 2026-10-18: gate round 1 findings S1-S11 fixed (status route answers 200 for every state and 404 only from an old engine; sweep lease from the sweep's own bounds; guarded failure marker; keyed sweep table; claim committed before the work; route compared on replay; thread-local key plumbing; phase status labels and a Phase 1 Test Plan; Phase 1 research records 222-research-8 to -13; stale citations), with observations 1, 2, 4, 5 and 7. Sources in 222-research-8 to -13.

- 2026-10-18: restructured for the gate (Context, Investigation and Key
  Discoveries, the Alternatives layout, Trade-offs, Finalization Gate); the
  stored-response cap corrected to Sam's 64 KB; Sam's ruling on open question
  6 (no Phase 1-lite) recorded; research records 222-research-1 to -7 filed.

- 2026-10-17: Sam's answers to open questions 1, 2, 5 and 7 recorded (Idempotency-Key, 7 days with full responses up to 64 KB, keyless sweeps stay allowed, one managed instance).

- 2026-10-02: `raced_embeds_total` now counts committed writes only, from both feeders (nexus-wbfpw.66, engine tag carrying it); text note under Phase 0.

- 2026-09-28: post-Phase-0 decisions recorded (wvek6 deferred, mfw6c closed).

- 2026-09-28: Phase 0 as built (nexus-ulrjq). `raced_embeds_total` is a top-level `/v1/status` field, not part of `EmbedActivitySnapshot`; `process_start_time` added beside it for restart detection; raced chashes sampled into the log line so retries can be split from shared text; same-request cross-doc repeats excluded; the throughput A/B moved to the v0.1.137 cut. From the round-1 code review and critique (T2 `nexus/review-nexus-ulrjq-raced-embed-counter-round1`, `nexus/critique-nexus-ulrjq-rdr222-phase0-raced-counter`).

- 2026-09-27: created as RDR-222 from the T2 draft; Sam's decisions recorded; Problem Statement gaps given `#### Gap N:` headings.

- 2026-09-27: draft body written by architect-planner, read-only, held in T2.

