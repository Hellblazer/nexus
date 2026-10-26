---
title: "Aspect Extraction Queue onto the Tuple-Space Queue Template, and Why the PDF Pipeline Row Stays"
id: RDR-235
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-10-26
accepted_date:
related_issues: [nexus-4vx0a, nexus-lqaq1]
related: [RDR-089, RDR-120, RDR-163, RDR-173, RDR-184, RDR-205, RDR-206, RDR-211]
---

# RDR-235: Aspect Extraction Queue onto the Tuple-Space Queue Template, and Why the PDF Pipeline Row Stays

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

Nexus runs several work queues it built by hand, each with its own claim, lease
and retry rules. Two of them matter here:

- The **aspect extraction queue** (`nexus.aspect_extraction_queue`). After a
  document is indexed into an eligible collection, a row is enqueued. A worker
  daemon claims the row, asks a model for the document's structured "aspects"
  (RDR-089), writes them to `document_aspects` and deletes the row.
- The **PDF pipeline row** (`nexus.pdf_pipeline`). This is one row per run of
  one PDF through the streaming indexer. It carries the run's resume state, and
  it decides which process owns the run.

The tuple space (RDR-205, extended by RDR-206 and RDR-211) is a general
coordination substrate in the same Postgres. It is a Linda-style store: a
producer `out`s a small record, and a consumer `in`s (claims) a matching one,
then `ack`s, `nack`s, `renew`s or `release`s the claim. RDR-211 added a
`queue/<name>` template with leases, attempt counting and dead-lettering, and a
`lock/<resource>` template. RDR-205's own audit says its claim statement reuses
the shape of `AspectRepository.claimNext`. The question is whether these hand-built
queues should move onto the template.

RDR-205 limits its consumers: "a consumer not named here needs its own RDR"
(rdr-205 §Approach, line 538). Neither the aspect queue nor the PDF pipeline is
named there or in RDR-211. This is that RDR, for both.

### Enumerated gaps to close

#### Gap 1: The aspect queue's claim is unfenced, and its lease is shorter than a batch

The aspect queue has no claim id. `markDone`, `markFailed` and `markRetry` match
by document, with no owner or status check (Finding F2). The only lease is a
reclaim window: rows `in_progress` longer than 300 s go back to `pending`.
That window is shorter than one attempt's timeout for a batch of five (420 s,
Finding F5). So a slow batch can be reclaimed while its worker is still
extracting. A second worker then extracts the same document, and whichever
finishes second overwrites or deletes the other's row. The daemon's own
comment records this race (`aspect_worker_daemon.py:77-83`).

#### Gap 2: A row whose worker dies every time is reclaimed forever

`reclaimStale` never increments `retry_count` (Finding F3). A document that
kills its worker (an out-of-memory crash, a hang the worker is killed for) is
returned to `pending` indefinitely and never reaches the terminal `failed`
state that RDR-163 defined.

#### Gap 3: A cross-machine arrival waits for a 300 s poll

The worker learns of new work from a same-machine wake file, checked every
1 s. A row enqueued from another machine waits for the 300 s backstop poll
(Finding F6). This is the managed-service shape: a laptop enqueues, and a
worker on another host drains.

#### Gap 4: The tuple queue template cannot carry the aspect queue today

The template has none of the following, and the aspect queue needs each one
(Findings F7 to F12):

- delayed visibility for the RDR-163 backoff ladder;
- a body large enough for the MCP path, which puts whole documents in the row;
- capacity and retention for a bulk index (`max_live_rows` 10,000, retention
  48 h);
- re-enqueue that resets a finished or failed task;
- the catalog foreign-key cascade that deletes queued work with its document.

Moving without closing these gaps would lose work silently.

#### Gap 5: Whether the PDF pipeline row's claim should move is undecided

The pipeline row decides ownership by heartbeat age: a run whose `updated_at`
is older than 5 minutes can be taken over. A `lock/<resource>` tuple offers a
leased, fenced owner instead. Whether it should replace the row's ownership
decision needs an answer from the code (Findings F13 to F15).

## Relationship to Prior RDRs

I scanned docs/rdr for RDRs on aspect extraction, queues, leases, the tuple
space and the PDF pipeline.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-089 | Origin (aspect extraction) | It defined one aspect row per document and measured extraction time (median 26.5 s, p95 38.1 s). Both stand. |
| RDR-163 | Origin (backoff ladder) | Its rationale, that transient failures must survive a drain, still holds. The move has to keep the ladder, so the template needs delayed visibility (Gap 4). RDR-163 rejected a dead-letter table because the terminal `failed` state already is one; the tuple `dead` state is the same idea. |
| RDR-173 | Origin (worker hosting) | Its per-tenant worker lease stays. Its 300 s backstop poll and the wake file are what this RDR replaces with a parked `in`. |
| RDR-205 | Origin (tuple space), and the scope rule | The scope clause is why this RDR exists. Its audit row "Reuse the statement shape" (rdr-205 line 972) is the precedent that the claim fits. |
| RDR-206 | Precedent | `renew` and the compare-and-swap `ack` on `claim_id` are the fencing Gap 1 needs. |
| RDR-211 | Origin (queue and lock templates) | It sized `queue/` at 1,000 tasks a day per queue. The aspect queue needs an aspect-specific template, not that template retuned (Proposed Solution, E3). It named no production consumer. |
| RDR-120 | Precedent (rule) | Substrate first, consumers by their own RDR. This RDR follows it. |
| RDR-184 | Adjacent (closed) | The dispatch ledger is the first named tuple consumer. There is no overlap with the aspect queue. |

## Context

### Background

This RDR came out of a bulk DEVONthink index of 161 PDFs on 2026-10-25. The
runner was hard-killed mid-record, and the next attempt hit
`PipelineConflictRunning`. That was a missing signal handler, fixed in
nexus-4vx0a, not the lease design. Sam then asked whether the hand-built queues
should move onto the tuple space. The session's first reading was that the
aspect queue is a strong candidate and the pipeline might at most move its
ownership claim. This RDR tests that reading against the code.

### Technical Environment

Engine: the Java nexus-service over PostgreSQL 17, all three tables under
forced row-level security keyed on `nexus.tenant`. Client: the Python `nx`
package (`HttpAspectQueue`, `HttpTupleStore`, `HttpPipelineDB`). The worker:
`src/nexus/aspect_worker.py`, hosted by `aspect_worker_daemon.py` (RDR-173).
Extraction: `claude -p` subprocesses in `aspect_extractor.py`, except for
`rdr__*`, which uses a deterministic frontmatter parser.

## Research Findings

### Investigation

This is a source reading of develop. The draft was written against f87b857c4
(2026-10-26). A second pass the same day re-read every finding and tested the
four critical assumptions against develop 609c8501f. Line references below are
from the second pass; several moved by a few lines, and F7 and F12 were
corrected (see the Revision History). The findings below carry the facts the
design depends on. Each measured or configured value is stated once, here.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `AspectRepository` (Java) | Yes | `claimNext` claims under `FOR UPDATE SKIP LOCKED`, with no claim id. The mark calls are unfenced. `reclaimStale` doesn't count. |
| `TupleRepository`, `TupleLimits`, `queue.yaml`, `lock.yaml` | Yes | There is no visibility column. The body cap is 4,096 bytes. The queue limits are 10,000 live rows and 48 h retention, with a 900 s lease ceiling and 3 attempts. Ack, nack, renew and release are compare-and-swaps on `claim_id`. |
| `TupleWaitRegistry` | Yes | A 1 s re-query and a 25 s cap on a parked call. The park caps (16 global, 4 per claimant) are engine-wide, not per tenant (F19). |
| `TupleHandler` routes | Yes | There is no delete or purge route for a tuple (F22). |
| `TemplateRegistry`, sweep, doctor checks | Yes | A new template can carry its own limits without changing other queues. The depth and dead-letter doctor row sees only `queue/` templates (F18). |
| `PipelineRepository` | Yes | `run_epoch` is checked under the row's `FOR UPDATE` in every write route. Ownership comes from heartbeat age. |
| `document_text(doc_id)` stored function | Yes | It returns rows, not one string, and no Java route or Python client calls it. For a note, joined in order, it equals the text enqueued today (F17). |

### Key Discoveries

**The aspect queue today**

- **F1 (Documented). Grain.** There is one row per document, never one per
  aspect: UNIQUE `(tenant_id, collection, source_path)`, aspects-001:167. A bulk
  index of N eligible documents enqueues up to N rows. The only measured
  arrival rate is about 16 items an hour (aspect_worker.py:113-116). I found no
  measured bulk backlog depth.
- **F2 (Documented). Claim and marks.** `claimNext` selects one `pending` row
  whose `next_retry_at` is null or past, `FOR UPDATE SKIP LOCKED`, and sets
  `in_progress` (AspectRepository.java:1079-1138). `markDone`, `markFailed` and
  `markRetry` match by doc_id or (collection, source_path) with no owner check
  (:1156-1219).
- **F3 (Documented). Reclaim.** `reclaimStale(300 s)` returns stale
  `in_progress` rows to `pending` without touching `retry_count`
  (:1224-1234; daemon constant `_DEFAULT_STALE_TIMEOUT_S = 300`,
  aspect_worker_daemon.py:77-86).
- **F4 (Documented). Backoff ladder.** `markRetry` stamps
  `next_retry_at = now() + interval` on the database clock. The worker's
  interval is `30 × 2^retry_count` s with ±20% jitter, capped at 5 attempts
  (aspect_worker.py:97-99, `_backoff_interval_seconds` at :261-272), so about
  30, 60, 120, 240 and 480 s. Re-enqueue clears it (AspectRepository.java:1065-1071,
  in `buildEnqueueQuery`). `markFailed` and `markRetry` both add 1 to
  `retry_count`.
- **F5 (Documented). Extraction time.** RDR-089's spike measured a median of
  26.5 s and a p95 of 38.1 s per document (aspect_worker.py:6; rdr-089:251).
  Timeouts: 180 s for one document (aspect_extractor.py:52, :2244-2249), and
  180 + 60 per extra document in a batch, which is 420 s at the default batch
  of 5 (:1505-1510, :1697; default size at aspect_worker.py:326). Each call
  gets three attempts (`_RETRY_ATTEMPTS = 3`, :1199).
- **F6 (Documented). Discovery.** A backstop poll every 300 s
  (`DEFAULT_POLL_INTERVAL_S`, aspect_worker.py:118), plus a same-machine wake
  file checked every 1 s (`WAKE_CHECK_INTERVAL_S`, :128; `_wait_for_wake_or_timeout`
  at :469). The comment at :121-122 says there is no LISTEN/NOTIFY.
- **F7 (Documented). Body.** On the MCP `store_put` path `fire_note_chains` calls
  `fire_document` with the whole note content and the catalog tumbler
  (note_write.py:989, :1022). `aspect_extraction_enqueue_hook` passes that
  content into `aspect_queue.enqueue` (aspect_worker.py:1558-1644, the call at
  :1640-1644), so the row's `content` column holds the whole document and
  `source_path` is a doc_id, not a file. The docstring at :1586-1590 says the
  CLI paths pass empty content and the worker reads from disk.
- **F8 (Documented). Re-enqueue.** `enqueue` (:929) and `enqueueMany` (:976)
  build an upsert, `INSERT ... ON CONFLICT (tenant_id, collection, source_path)
  DO UPDATE`, that resets the row to `pending`, `retry_count` 0, and
  `last_attempt_at`, `last_error` and `next_retry_at` null
  (AspectRepository.java:995-1072). It does not check the current status, so a
  row that is `in_progress` is reset too.
- **F9 (Documented). Cascade.** A foreign key `fk_aspect_queue_catalog_doc` on
  `(tenant_id, doc_id)` to `catalog_documents (tenant_id, tumbler)` with
  `ON DELETE CASCADE` deletes queued work with its document
  (fk-001-catalog-cross-store.xml:249-252).

**The tuple queue template**

- **F10 (Documented). No delayed visibility.** The claim predicate is
  `consumed_at IS NULL`, `expires_at > now()`, `claim_state` not dead, and
  `claim_state` null or `lease_until < now()`, ordered by `created_at`, under
  `FOR NO KEY UPDATE SKIP LOCKED` (TupleRepository.java:1883-1892). No column
  holds a not-before time. `nack` and `release` call `releaseOrDeadLetter`,
  which clears `claim_state`, so the row is claimable at once (:2379-2401).
- **F11 (Documented). Limits.** `MAX_BODY_BYTES = 4096`, a fixed constant
  (TupleLimits.java:32). `queue.yaml` sets the following (:65-74).
  - `max_attempts: 3`. A lapsed lease counts an attempt in `claimOnce`
    (TupleRepository.java:1900-1923).
  - `max_lease_seconds: 900`. A longer lease throws `LeaseTooLongException`,
    not clamped (:1847-1850). `renew` extends a live claim without counting an
    attempt (:2253).
  - `retention_seconds: 172800` (48 h). A refire cannot extend it past
    `created_at` plus retention (:676-681).
  - `max_live_rows: 10000`, enforced at `out` (`writeOut`, :653-671), per
    subspace and over live rows only.
  - `claim_log_ttl_seconds: 2592000` (30 days).
- **F12 (Documented). Identity and refire.** Queue tuple ids are the keys plus
  the nonce plus the `id_dims` the template names (queue.yaml:61-63). A refire
  with the same id only moves `expires_at`, clamped to the retention ceiling. A
  same-id write with a different body or dims on a row that is not consumed
  throws `NonceReuseConflictException` (TupleRepository.java:736-752). A refire
  never revives a consumed row, because the claim predicate excludes consumed
  rows (:1883-1892). So `out` with the same id never replaces a live body; it
  either only moves the expiry or is refused.
- **F12a (Documented). Fencing and wake.** `ack`, `nack`, `renew` and `release`
  are compare-and-swaps on `claim_id` through `liveClaimCondition`
  (TupleRepository.java:2455-2460; `ack` :2060, `nack` :2146, `release` :2199,
  `renew` :2253). A stale one throws `ClaimNotFoundException`, which the route
  maps to 404, and writes nothing. A parked call wakes on a same-process signal
  or a 1 s re-query (TupleWaitRegistry.java header), so a write on another
  engine instance is seen within about 1 s. The park caps are in F19.
- **F12b (Documented). Tenancy.** `nexus.tuples` and `nexus.tuple_claim_log` are
  `ENABLE` and `FORCE ROW LEVEL SECURITY` (tuples-001-baseline.xml:101-102,
  :143-144); `document_aspects` is forced the same way
  (aspects-001-baseline.xml:95). Tenancy is not a blocker.

**The PDF pipeline row**

- **F13 (Documented). The row carries the run.** It holds the resume counters,
  `extraction_meta`, status and the heartbeat. The page and chunk WAL tables hang
  off it by `pipeline_id`. Here WAL means the write-ahead buffer of extracted
  pages and chunks (pipeline-001, pipeline-002, pipeline-003-run-epoch;
  PipelineRepository.java:292, :326).
- **F14 (Documented). The write fence.** Every write route first runs `lockRun`:
  `SELECT ... FOR UPDATE` on the pipeline row and a compare of the caller's
  `run_epoch`, throwing `PipelineStaleRunException` on mismatch. `requireRun`
  wraps it for WAL writes, in the same transaction as the write
  (PipelineRepository.java:458-497). A takeover bumps `run_epoch` in the same
  UPDATE that claims the row (:318-330).
- **F15 (Documented). Ownership and identity.** `create` takes over a `failed`
  row or one whose heartbeat is older than `STALE_THRESHOLD` (5 min, :86;
  takeover :313-330), and refuses a fresh `running` row by throwing
  `PipelineConflictException` (:336-338; I did not read the 409 mapping). The
  row's identity is the document `(tenant_id, content_hash, collection,
  pdf_path)`, UNIQUE (pipeline-002-per-row-identity.xml:98), not the content
  hash alone.
- **F16 (Verified, measured 2026-10-25 for nexus-lqaq1).** In `extractor=auto`,
  Docling converts the whole document before the first page reaches the buffer.
  A 300-page PDF was silent for 83.7 s on the laptop (0.28 s a page), so a run of
  about 1,070 pages or more is silent past the 5-minute window. The heartbeat
  only moves on progress writes. The second pass confirmed the second half by
  source: `updated_at` is written only at takeover (PipelineRepository.java:326)
  and at :365, :409, :451, :656 and :727, which are create, resume and progress
  writes, so a run with no progress writes is silent for the whole Docling
  pass. It did not re-run the 83.7 s measurement; that figure is carried from
  the nexus-lqaq1 record.

**Verification of the critical assumptions (second pass, develop 609c8501f)**

These entries test A2 to A4 below and answer three questions the first draft
left open (Q1 to Q3). A "park" is a call that holds its connection open until a
matching row arrives or the call's timeout passes; a "park slot" is one unit of
the engine's budget for such calls.

- **✅ Verified** (source search) — F17, A2. For a note, the text `store_put`
  enqueues equals `document_text` joined in position order, with three caveats.
  `note_pieces` guarantees `join(pieces) == content`, and the splitters drop
  and add nothing; the pieces are written verbatim as chunk text with manifest
  positions in order, and `fire_document` gets the same content. The stored
  function returns `TABLE(position, chunk_text)` ordered by position, scoped to
  the tenant and aware of tombstones, so `string_agg(chunk_text, '' ORDER BY
  position)` equals the content. Caveats: (1) it returns rows, not one string,
  and no Java route or Python client calls it (the only match is a doctor
  comment), so the worker needs a new engine route or must reassemble through
  `store_get`; (2) this holds only for notes: `docs__` and `rdr__` file chunks
  go through the markdown chunker, which prepends headings and overlaps, so for
  a file source the worker must read from disk; (3) I did not read the engine
  round trip of `chunk_text`. The test that settles (3) is an integration test
  that `store_put`s a multi-piece note with leading and trailing whitespace,
  CRLF and a repeated paragraph, then compares the aggregate to the original.
  *Source: src/nexus/catalog/store_hook.py:163-201; src/nexus/chunker.py:226-266; src/nexus/catalog/note_write.py:247-290, :1022; tests/test_store_put_split.py:102,142; service/src/main/resources/db/changelog/vectors-005-repoint-functions-views.xml:722-750*
- **✅ Verified** (source search) — F18, A3. An aspect template can carry its
  own limits without changing the sweep or other queues' doctor rows. Templates
  load from an explicit classpath list, so `aspects.yaml` must be added there.
  `max_live_rows`, `retention_seconds` and `take.max_attempts` are per-template
  fields. A boot check requires a template's `claim_log_ttl_seconds` to exceed
  its own retention plus the 21,600 s sweep interval, and a template may only
  shorten the engine default of 180 days, so a long retention needs a matching
  `claim_log_ttl_seconds`, up to 15,552,000 s. The name `aspects/<collection>`
  resolves, because the address-segment pattern admits double underscores, dots
  and hyphens; a segment is capped at 256 bytes. The sweeps delete by each row's
  own `expires_at`, stamped at write from its template, and lapse leases with
  each row's own template `max_attempts`; none reads a global retention.
  `max_live_rows` counts live rows per subspace, so per-collection templates
  cap per collection. Doctor: `_check_tuple_queue_depth` keeps only templates
  whose name starts with `queue/`, so `aspects/<c>` is invisible to the depth
  and dead-letter warning (a gap to fill, not a regression).
  `_check_tuple_unclaimed_age` applies to every take-enabled non-lock template
  with a fixed 3,600 s threshold, so a bulk backlog on `aspects/<c>` older than
  an hour turns that row red. `park_slots` and `sweep_freshness` are global.
  *Source: service/src/main/java/dev/nexus/service/tuples/TemplateRegistry.java:86-93, :202, :224-240, :395-413; TemplateSchema.java:67-89; TupleLimits.java (MAX_FIELD_VALUE_BYTES); TupleRepository.java:653-671, :2514-2532, :2596-2606, :2644-2654; src/nexus/health.py:7672, :7801-7990, :8668, :8758*
- **✅ Verified** (source search) — F19, A4a. The park caps are 16 global, 4 per
  claimant and 25 s per call, as defaults with env knobs
  (`NX_TUPLE_PARK_CAP_GLOBAL`, `NX_TUPLE_PARK_CAP_PER_CLAIMANT`,
  `NX_TUPLE_TIMEOUT_CAP_SECONDS`). They are engine-wide, not per tenant: there
  is one global counter and one per-claimant map keyed by the bare claimant
  string. A parked `in` or `inp` takes one global and one claimant slot; `rd`
  and `wait` take one global slot and no claimant slot. One `wait` call
  multiplexes up to 34 subspaces. Plain `wait` and `rd` match any unconsumed,
  unexpired row, including rows another worker holds; only announce mode
  narrows to claimable rows. So a plain `wait` over a backlog held by other
  workers returns at once, and a worker loop must not spin on it.
  *Source: service/src/main/java/dev/nexus/service/db/TupleRepository.java:100-107, :128, :1069-1077, :1169-1172, :1574; TupleWaitRegistry.java:140-141, :740-772; src/nexus/db/t2/http_tuple_store.py:145, :1204*
- **❓ Assumed** (source search, runtime figure missing) — F20, A4b. Whether a
  parked loop per tenant worker fits beside the existing parkers depends on a
  number the source cannot give: how many Claude sessions are live on the cloud
  engine. By code, the standing parkers are one MCP `ChannelWaiter` per session
  (a single multi-subspace `wait`, 25 s per call, so one global slot per live
  session, shared by every tenant), plus on-demand `tuple_in` and `tuple_rd`
  MCP calls and `nx tuple rd`/`in` with `--timeout-s`. No daemon or library
  loop calls `in` with a park, so there is no mailbox or ledger `in` consumer;
  mailbox delivery is the `ChannelWaiter` wait. What the source does settle: the
  doctor warns at 75 percent (12 of 16), and with 16 global slots shared by all
  tenants, an engine with 12 or more live sessions has no room for a parked `in`
  per tenant worker without raising `NX_TUPLE_PARK_CAP_GLOBAL`. To settle it,
  read `GET /v1/tuples/park_stats` on the cloud engine at a busy hour
  (`global_in_use`, `refused_global`).
  *Source: src/nexus/mcp/channel.py:162, :1134-1187; src/nexus/mcp/core.py:7877; src/nexus/commands/tuple_cmd.py:239, :328; src/nexus/health.py:7691, :8574*
- **✅ Verified** (source search) — F21, A4c. Per-collection queues with one
  parked `in` each do not fit. N collections parked at once cost N global and N
  claimant slots under one claimant id, so more than 4 collections per worker
  claimant is refused with `ParkCapExceededException`, and the N also count
  against the 16 global. Two shapes fit. (a) One worker parks one announce-mode
  `wait` over up to 34 `aspects/<collection>` subspaces (one global slot, no
  claimant slot) and claims with `inp`, which never parks; the subspace list
  comes from `subspace_list` with prefix `aspects/` and must be rebuilt when a
  collection appears. (b) One queue per tenant, `aspects/<tenant-scoped name>`,
  with the collection in the body or dims; a parked `in` per tenant then costs 1
  global plus 1 claimant slot, and `in` cannot select by collection (F24), so the
  worker takes the oldest task of any collection, which is fine for this worker.
  *Source: service/src/main/java/dev/nexus/service/db/TupleWaitRegistry.java:757-772*
- **✅ Verified** (source search) — F22, Q1. There is no tuple delete or
  purge-by-id route. The `/v1/tuples` routes are `out`, `rd`, `rdp`, `wait`,
  `in`, `inp`, `ack`, `nack`, `renew`, `release`, `registry`, `subspace_list`,
  `subspace_stats` and `park_stats`. The only `deleteFrom` on `nexus.tuples` is
  the sweep's purge of expired rows, and on the claim log likewise. The nearest
  client-reachable removal is claim then `ack`, and it cannot target a task:
  `inp` and `in` pick the oldest claimable row matching a keys pattern, not an
  id, nonce or dims. `ack` then clears the body and sets `consumed_at`; the row
  stays until retention, and a task another worker holds cannot be reached at
  all. So a catalog purge cannot remove one document's task. Dropping it needs
  the claim-time check (the worker acks as dropped when the catalog document is
  missing, already in this RDR) or a new engine route. `out` with the same id
  does not replace a live body either.
  *Source: service/src/main/java/dev/nexus/service/http/TupleHandler.java:173-186; TupleRepository.java:1883-1892, :1998-2012, :2603, :2651, :736-752*
- **✅ Verified** (source search) — F23, Q2. There is no dirty ack. A grep of the
  engine and `src/nexus/db` finds no dirty column or claim usage. `ack`
  consumes: it sets `consumed_at`, clears the body, logs `ack` and never returns
  a row to available. The ways back to available that exist are `nack` (adds 1
  to attempts, dead-letters at `max_attempts`, logs `nack` then `dead`),
  `release` (attempts unchanged, never dead-letters, logs `release`), and a
  lapsed lease at claim time or in the sweep (adds 1 to attempts, logs
  `expire`, dead-letters at the cap). The claim-log transition names are
  `claim`, `ack`, `nack`, `renew`, `expire`, `dead` and `release`; the column is
  plain TEXT with no CHECK, so a new name needs no DDL. A dirty ack that returns
  the row is a new path. To keep attempts untouched it should take the shape of
  `release` with a new transition name (for example `ack_dirty`), not `nack`.
  There is likewise no `nack` that dead-letters on request; `nack` dead-letters
  only when the count reaches `max_attempts`.
  *Source: service/src/main/java/dev/nexus/service/db/TupleRepository.java:132-142, :1900-1923, :1998-2060, :2146-2175, :2199-2230, :2379-2401, :2514-2532*
- **✅ Verified** (source search) — F24, Q3. A queue's keys are `[queue]` only,
  and `in` matches keys only. `queue.yaml` declares `keys: [queue]`; the dims
  `from`, `kind` and `correlation_id` are not matchable. `claimOnce` requires
  every template key to be pinned in the keys pattern, and `matchCondition`
  builds equality on the keys JSON only, never dims; `rd` and `rdp` match keys
  only too. A template that wants `in` to select by collection must put the
  collection in `keys`. Because the id comes from keys, nonce and `id_dims`, the
  deterministic id this RDR wants (nonce = collection + doc_id) is reachable
  only by nonce, which `in` cannot filter on.
  *Source: service/src/main/resources/tuples/templates/queue.yaml:49-50; TupleRepository.java:1069-1077, :1850-1853, :1978-1985*

### Critical Assumptions

- [ ] **A1**: Adding a nullable `available_at` column and one claim-predicate arm
  (`available_at IS NULL OR available_at <= now()`) keeps the claim's index
  scan. **Status**: Unverified. **Method**: Spike (EXPLAIN on a seeded
  tenant). The spike is still to run.
- [x] **A2**: `document_text(doc_id)` returns the same text the MCP
  `store_put` path enqueues today. Then a task can carry a pointer instead of
  the document. **Status**: Verified for notes, with caveats (F17): it returns
  rows with no route to call it, and it does not hold for file sources.
  **Method**: Source Search. An integration test over a stored note remains the
  settling check for the engine round trip.
- [x] **A3**: An aspect-specific template can have a larger `max_live_rows`,
  a longer retention and its own attempt cap, without changing the sweep or
  the doctor rows for other queues. **Status**: Verified (F18). The doctor's
  depth and dead-letter row does not see `aspects/` templates, and its
  unclaimed-age row would go red on a backlog older than an hour. **Method**:
  Source Search (template loader, sweep, doctor).
- [ ] **A4**: A parked `in` loop (25 s calls) per tenant worker fits within the
  park-slot caps, alongside the existing parkers. **Status**: Split, three
  parts. (a) The caps are verified: 16 global, 4 per claimant, 25 s, engine-wide
  (F19). (b) Fit alongside the existing parkers is Assumed, pending
  `park_stats` on the cloud engine at a busy hour (F20). The mailbox and ledger
  have no parked `in` consumer; the standing parkers are the MCP `ChannelWaiter`
  waits, one per live session. (c) Per-collection parked queues are verified not
  to fit past 4 collections per claimant (F21); the design must use one
  multi-subspace `wait` or one queue per tenant. **Method**: Source Search; the
  runtime count of live parkers is still missing.

### Open Questions

Answered in the second pass:

- Can a catalog purge delete a document's task by id? **No.** There is no tuple
  delete route, and `in` cannot target an id (F22). The design uses the
  claim-time drop, or adds an engine route.
- Does `ack` have a dirty variant that returns a re-enqueued task to available?
  **No.** `ack` always consumes (F23). It is new engine work, shaped like
  `release`.
- Can `in` select a task by collection or other dims? **No.** It matches keys
  only (F24). Collection must be in `keys`, or the worker takes the oldest task
  of any collection.

Still open:

- Plan-time (A1): does the `available_at` arm keep the claim's index scan on a
  seeded backlog? The EXPLAIN spike has not run.
- Runtime (A4b): `park_stats` on the cloud engine at a busy hour.
- No one has measured a bulk backlog depth (F1), so E3's limits are unset.
- Which queue shape (F21 (a) or (b)) the design takes. This also decides the
  claim-time drop's key, since `in` cannot filter by id.
- How a worker gets a note's text: a new `document_text` route, or `store_get`
  (F17).

## Proposed Solution

### Approach

Two decisions.

**Decision 1: move the aspect queue onto the tuple space, behind four engine
extensions, in phases.** The move closes Gaps 1 to 3 with mechanisms that
already exist and are tested in the tuple space:

- a `claim_id` with a compare-and-swap `ack`, so a reclaimed worker's late
  result is refused instead of overwriting;
- a real lease with `renew`, so a batch longer than the lease is kept alive
  by its worker, not reclaimed under it;
- attempts that count a lapsed lease, so a crash-looping document dead-letters;
- a parked wait, so a cross-machine enqueue is seen in about 1 s.

The move does not happen until the template can carry the queue (Gap 4). The
four extensions are:

- **E1. Delayed visibility.** A nullable `available_at` on `nexus.tuples`, one
  arm in the claim predicate, `out` with `delay_s`, and `nack` with
  `retry_after_s`. Both are stamped on the database clock, as RDR-163 stamps
  `next_retry_at`. The ladder's intervals stay worker-chosen.
- **E2. Pointer-only task body.** The task body carries
  `{collection, doc_id, content_hash, source}`, never document text. The
  worker reads the text from disk when `source` is a file, and for a note
  rebuilds it from `document_text(doc_id)` (A2, F17). That function returns
  rows and has no route today, so the worker needs a new engine route or must
  reassemble the text through `store_get`. The 4,096-byte cap then never binds,
  and the cap itself stays as it is.
- **E3. An aspect queue template.** A new `aspects/<collection>` template,
  rather than retuning `queue/`, so the board, ledger and ad-hoc queues keep
  RDR-211's sizing. Its own limits are sized from F1 and F5:
  - `max_live_rows` large enough for a bulk index;
  - retention long enough that no backlog expires before a worker reaches it;
  - `max_attempts` equal to the ladder cap plus one;
  - a `claim_log_ttl_seconds` longer than the retention plus the 21,600 s sweep
    interval, up to 15,552,000 s, or the engine refuses to boot (F18).
  The exact numbers come from a measured bulk backlog, which F1 says nobody
  has yet. Measuring it is a prerequisite. The template file must also be added
  to the loader's explicit classpath list, and the doctor's depth and
  dead-letter row, which sees only `queue/` templates, must be widened to see
  it (F18).
- **E4. Re-enqueue.** An `out` mode `replace` for the document's deterministic
  id (nonce = `collection + doc_id`):
  - a consumed or dead row is reset to available, with the new body and
    attempts at 0;
  - an available row gets the new body;
  - a claimed row is marked `dirty`, so its `ack` returns it to available
    instead of consuming it. This is new engine work. Today `ack` always
    consumes the row (F23). The new path should take the shape of `release`
    (attempts unchanged) with its own claim-log transition name, not `nack`,
    which counts an attempt.

  This keeps today's "the newest content wins" behaviour (F8) without the
  double extraction the current upsert causes on an `in_progress` row.

**Decision 2: the PDF pipeline row does not move, not even its ownership.**
Design Rationale argues this from F13 to F16.

### Technical Design

**Producer side.** `HttpAspectQueue.enqueue` and `enqueue_many` become `out`s
to `aspects/<collection>` with the pointer body and `replace`. The hook sites
(F1) do not change. The FK cascade (F9) has no tuple equivalent. A claim of a
document the catalog no longer has is `ack`ed as dropped, with a log event
(`aspect_task_dropped_missing_doc`). That claim-time check is the only way a
purged document's task goes away: there is no tuple delete route (F22), so a
catalog purge cannot remove the task, and a task stays queued until a worker
claims it or retention ends it. A purge route would be a new engine extension.

**Worker side.** The RDR-173 per-tenant daemon keeps its lease. Its loop
becomes:

1. Wait for work, then claim with a lease of 600 s. A parked `in` per
   collection does not fit the park caps past 4 collections per worker (F21),
   so the loop uses one of two shapes, not yet chosen. (a) One announce-mode
   `wait` over up to 34 `aspects/<collection>` subspaces, 25 s per call, then
   `inp` on the subspace that woke it. Announce mode matters: a plain `wait`
   returns at once on rows another worker holds (F19). (b) One queue per tenant
   and a parked `in` on it; `in` cannot select by collection (F24), so the
   worker takes the oldest task of any collection.
2. Extract, with a renewer thread that calls `renew` every 120 s.
3. On success, write `document_aspects` and `ack`.
4. On a retryable failure, `nack` with `retry_after_s` from the RDR-163 ladder.
5. On a non-retryable failure, `nack` to dead. No such `nack` exists today:
   `nack` dead-letters only when the attempt count reaches `max_attempts`
   (F23), so a dead-on-request `nack` is more engine work, or the worker acks
   with a failure record.
6. On a self-fault stand-down (the worker is shutting down), `release`, which
   counts no attempt.

A batch of 5 is five claims in sequence. There is no batch `in`, and RDR-205 says
one lands only when a consumer asks. This RDR does not ask.

**Signatures (illustrative; verify during implementation).**

```text
out(template="aspects/<collection>", body={collection, doc_id, content_hash, source},
    nonce=f"{collection}:{doc_id}", mode="replace", delay_s=0) -> {id, created|replaced|dirtied}
nack(id, claim_id, retry_after_s: int | None, dead: bool = False) -> {state}
```

**Migration.** Engine first (E1 to E4, one Liquibase changeset and template
file per the engine-release rules), then a client release.

- A flag `NX_ASPECT_QUEUE=tuples|table` selects the producer.
- In `tuples` mode the worker still drains the old table first, until it is
  empty, then only tuples.
- An upgrade-ladder rung moves `pending` rows into tuples, keeping
  `next_retry_at` as `available_at`.
- `failed` rows become dead tuples, so `nx enrich` requeue-failed keeps working
  through `replace`.

Retiring the table is a separate, later changeset, after a release with no
reader.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Fenced claim, lease, renew, attempts, dead state | `TupleRepository` (RDR-205/206/211) | Reuse. |
| Delayed visibility | `AspectRepository` `next_retry_at` arm | Extend: port the arm into the tuple claim predicate (E1). |
| Aspect template | `queue.yaml` | Extend: a new template file beside it, not a retune (E3). |
| Content source for pointer bodies | `document_text(doc_id)` | Reuse for notes, behind a new route or `store_get`; files are read from disk (A2, F17). |
| Wake | `aspect_wake` file, 300 s poll | Replace with a parked wait (F21). Keep the wake file only while the old table drains. |
| Per-tenant worker lease | `aspect_worker_daemon.py` (RDR-173) | Reuse. |
| Pipeline ownership | `PipelineRepository.create` heartbeat takeover | Keep (Decision 2). |

### Decision Rationale

**Why the aspect queue moves.** Gaps 1 to 3 are each a mechanism the tuple space
already has and tests: fencing (RDR-206), counting attempts (RDR-211) and
cross-instance wake (RDR-205). Hardening the table in place (Alternative 1)
would rebuild those mechanisms a second time, in a second repository, with a
second set of tests and doctor rows. The cost of moving is concentrated in E1
to E4. E1, E2 and E4 are generally useful to any future queue consumer: delay,
pointer bodies and replace. E3 is a template file.

**Why the PDF pipeline row stays.** A `lock/<resource>` tuple could replace one
thing, the decision about who owns a run. It could replace nothing else, and
keeping both would be worse than today:

1. *The write fence cannot move.* `run_epoch` is checked under the pipeline
   row's `FOR UPDATE`, in the same transaction as each page and chunk write
   (F14). A tuple `claim_id` lives in another table and fences only tuple
   operations. Moving ownership to a lock would keep `run_epoch` anyway, so the
   run would have two ownership records. A crash between taking the lock and
   bumping the epoch, or the reverse, leaves them disagreeing: the lock says A
   and the epoch says B. Every write route would then need to check both.
2. *The resume state cannot move.* The counters, `extraction_meta` and the
   WAL are the row (F13), and a tuple body is capped at 4,096 bytes (F11).
3. *The key reintroduces a fixed defect.* `lock/<content_hash>` would
   serialise two documents that share bytes, which pipeline-002 removed (F15).
   Encoding the whole document identity in the resource is possible (a hash
   of it), but then the lock key and the row key must be kept in step.
4. *The lease is no better than a heartbeat.* The lock's 900 s lease ceiling
   means a long extraction must `renew`, and a missed renew loses the lock for
   good. That is the same obligation as a heartbeat, with a harsher failure.
5. *The defects that started this question have other homes.* The stranded
   `running` row after a kill is fixed by releasing the row on SIGTERM and
   Ctrl-C (nexus-4vx0a). The remaining risk, a live run going silent past
   5 minutes and being taken over (F16), is a missing heartbeat, tracked as
   nexus-lqaq1: an engine route that refreshes `updated_at` under the
   `run_epoch` fence. A lock would need the same renew loop to fix it.

## Alternatives Considered

### Alternative 1: Harden the aspect table in place

**Description**: Add a `claim_id` and a `lease_until` column to
`aspect_extraction_queue`, fence the mark calls on `claim_id`, count a reclaim
as an attempt, and add LISTEN/NOTIFY or a long-poll route for wake-up.

**Pros**:

- No body cap, capacity or retention questions, and the FK cascade stays.
- Smaller engine change, and no producer or worker migration.

**Cons**:

- It rebuilds fencing, leases, attempts and wake a second time, in a second
  repository. Their tests, doctor rows and failure modes would drift from the
  tuple space's.
- Each later hand-built queue would face the same choice again.

**Reason for rejection**: This is the fallback. If Sam rejects the template
extensions E1 to E4, it closes Gaps 1 to 3 at a lower immediate cost, and this
RDR's Gap list carries over unchanged.

### Alternative 2: Move onto `queue/aspects` as it is

**Description**: Use the existing `queue/` template with no engine change.

**Reason for rejection**: It loses work. It has no backoff, so a retryable
failure would retry at once and burn its 3 attempts. The 4,096-byte body
cannot hold MCP documents, the 10,000-row cap refuses a bulk index, and the
48 h retention expires an undrained backlog (F10, F11).

### Alternative 3: Move the pipeline's ownership onto `lock/`

**Reason for rejection**: Decision Rationale, items 1 to 5.

### Briefly Rejected

- **A separate queue table per consumer, but shared code**: shared code over
  separate tables is what the tuple space already is.
- **Batch `in`**: RDR-205 defers it until a consumer needs it. Five sequential
  claims are enough at the measured arrival rate (F1).

## Trade-offs

### Consequences

- Positive: a reclaimed worker's late result is refused, which closes the
  documented double extraction (Gap 1).
- Positive: a crash-looping document reaches a dead state (Gap 2).
- Positive: a cross-machine enqueue is seen in about 1 s, instead of up to 300 s
  (Gap 3).
- Positive: one substrate, and the tuple doctor rows cover the aspect queue
  once the depth row is widened to see `aspects/` templates (F18). The
  unclaimed-age row would already go red on a backlog older than an hour.
- Negative: four engine extensions, an engine release, and a migration rung.
- Negative: the declarative FK cascade becomes a consumer-side check (E2,
  producer side).
- Negative: `rdr__*` documents, which are parsed with no model call, now pay a
  tuple round trip. That cost is negligible next to an engine write.

### Risks and Mitigations

- **Risk**: The tuple claim scan slows with a large aspect backlog in one
  tenant. **Mitigation**: A1's EXPLAIN spike on a seeded backlog of the
  measured size, before E1 lands.
- **Risk**: A worker's renewer thread dies, and the lease lapses mid-extraction.
  **Mitigation**: The late `ack` is refused (404), and the task is retried with
  an attempt counted. That is the fenced form of today's silent overwrite.
- **Risk**: Park slots are exhausted (A4). The 16 global slots are shared by
  every tenant on the engine and the standing parkers are one per live Claude
  session (F19, F20); an engine with 12 or more live sessions has no room for a
  parked call per tenant worker. **Mitigation**: Read `park_stats` on the cloud
  engine at a busy hour, and use one multi-subspace `wait` per worker (F21).
  Fall back to unparked polling every 5 s, which is still 60× faster than the
  backstop.

### Failure Modes

- An enqueue refused by the template (`MaxLiveRowsExceeded`) is loud at the
  producer and logged with the collection. It never drops silently.
- A task whose document vanished is acked as dropped, with
  `aspect_task_dropped_missing_doc`.
- A dead task shows on `nx enrich` failed listings and, once the depth row is
  widened to see `aspects/` templates (F18), on the tuple doctor row. The
  requeue path is `replace`.
- Diagnosis: `tuple_list` / `tuple_stats` on `aspects/<collection>` show
  available, claimed and dead counts. The claim log shows each attempt.

## Implementation Plan

### Prerequisites

- [ ] Settle what is still open: run the A1 spike, read `park_stats` for A4b,
  and choose the queue shape (F21). A2, A3 and A4a and c are verified by source.
- [ ] Measure a real bulk backlog. Index one large collection and record the
  peak queue depth and the drain time. E3's limits are set from that, not
  guessed.
- [ ] The engine freeze is lifted. E1 to E4 follow the engine-release rules.

### Minimum Viable Validation

One tenant, flag set to `tuples`. Index 50 documents into a `knowledge__`
collection, with the extractor faked to fail document 7 twice (retryable)
and to crash the worker on document 13.

- Every other document gets its `document_aspects` row exactly once.
- Document 7 succeeds on its third claim, each claim at least the ladder
  interval apart.
- Document 13 dead-letters after the attempt cap.
- A second worker started mid-run claims nothing already claimed.
- A late `ack` from a killed worker is refused.

### Phase 1: Code Implementation

#### Step 1: Engine extensions E1 to E4

- One changeset for `available_at`.
- The claim predicate arm.
- `out` `delay_s` and `mode=replace`.
- `nack` `retry_after_s`.
- The `dirty` flag and the ack path that honours it (new, `release`-shaped; F23).
- The `aspects/<collection>` template file, its entry in the loader's
  classpath list, and the widened doctor depth row (F18).
- A route or a client path for a note's text (F17).

Each comes with a Java test, plus a spike for A1.

#### Step 2: Client producer and worker

`HttpAspectQueue` is behind the flag. The worker loop and renewer are as in
Technical Design. The old table drains first.

#### Step 3: Migration rung

An upgrade-ladder rung moves `pending` and `failed` rows to tuples.

### Phase 2: Operational Activation

#### Activation Step 1: Default flip

Flip `NX_ASPECT_QUEUE` to `tuples` after the MVV passes on all CI shards and in
one managed-service tenant. Retiring the table is a later changeset.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `aspects/<collection>` tuples | `tuple_list` (in scope) | `tuple_stats` (in scope) | `replace`, and the claim-time drop for a purged document (no purge route exists, F22) | Doctor row (in scope) | Postgres backup (existing) |

### New Dependencies

None.

## Test Plan

- **Scenario**: A task is nacked with `retry_after_s=60`. **Verify**: it is not
  claimable at 59 s and is claimable at 60 s (database clock).
- **Scenario**: A worker's lease lapses mid-extraction, and a second worker
  claims the task. **Verify**: the first worker's `ack` answers 404 and writes
  no `document_aspects` row.
- **Scenario**: A task's worker dies on every claim. **Verify**: it reaches
  `dead` after the cap, and `nx enrich` requeue-failed revives it.
- **Scenario**: Re-enqueue during extraction (`replace` on a claimed row).
  **Verify**: the `ack` returns it to available, and the next claim sees the
  new `content_hash`.
- **Scenario**: An MCP-stored note is enqueued. **Verify**: the body is a
  pointer under 4,096 bytes, and the worker's text equals today's `content`
  (A2). Use a multi-piece note with leading and trailing whitespace, CRLF and
  a repeated paragraph, and compare the rebuilt text byte for byte (F17).
- **Scenario**: More collections than 4 are active for one worker. **Verify**:
  no park is refused with `ParkCapExceededException` (F21).
- **Scenario**: A catalog document is purged with a task queued. **Verify**: the
  task is acked as dropped when a worker claims it (no purge route exists, F22).
- **Scenario**: Cross-instance enqueue to a parked worker. **Verify**: claimed
  within 2 s.
- **Scenario**: A bulk index at the measured backlog size. **Verify**: no `out`
  is refused, and no task expires before it is drained.

## Validation

### Testing Strategy

Java tests for E1 to E4 against the real Postgres container. Python tests for
the producer and worker against the engine substrate the unit suite already
boots, never against a fake queue. The MVV above is a scenario-journey test.

### Performance Expectations

No speculative targets. The comparison that matters is wake latency: up to
300 s today (F6) against about 1 s (F12a). The claim-scan cost under a backlog
is measured in the A1 spike.

## Finalization Gate

### Contradiction Check

The second pass found several places where the first draft's text disagreed with
the source; each is fixed in the body and listed in the Revision History. No
other contradictions are known. One tension is stated rather than hidden: RDR-211
sized the queue template small on purpose. This RDR keeps that sizing by
adding a separate template, not by changing it.

### Assumption Verification

A2, A3, A4a and A4c are verified by source reading. A1 (the EXPLAIN spike) and
A4b (`park_stats` at a busy hour) are not, and remain prerequisites, listed
above.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `TupleRepository` claim, ack, nack, renew, release | nexus-service | Source Search |
| `AspectRepository.claimNext`, `markRetry` | nexus-service | Source Search |
| `document_text(doc_id)` | nexus-service (SQL) | Source Search (F17): returns rows, no route; true for notes only |
| `/v1/tuples` routes, `TupleWaitRegistry` caps | nexus-service | Source Search (F19, F22) |

### Scope Verification

The MVV is in scope for Phase 1 and runs before the default flip.

### Cross-Cutting Concerns

- **Versioning**: Engine first, then a client gated on the engine version. The
  wire contract change is declared in the wire-contract ledger.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: Local and managed alike, since both run the same engine.
  The per-tenant worker lease is unchanged.
- **IDE compatibility**: N/A
- **Incremental adoption**: The flag, plus draining the old table first.
- **Secret/credential lifecycle**: N/A
- **Memory management**: Pointer bodies shrink the queue's storage. Today's
  rows can hold whole documents.

### Proportionality

The pipeline half is deliberately short. It is a recorded "no", with reasons,
so the question is not reopened without new evidence.

## References

- docs/rdr/rdr-089, rdr-163, rdr-173, rdr-205 (§Approach scope clause, line 538;
  §Existing Infrastructure Audit, line 972), rdr-206, rdr-211 (§Scale and
  Limits).
- service/src/main/java/dev/nexus/service/db/AspectRepository.java,
  TupleRepository.java, TupleLimits.java, TupleWaitRegistry.java,
  PipelineRepository.java.
- service/src/main/resources/tuples/templates/queue.yaml, lock.yaml.
- service/src/main/resources/db/changelog/aspects-001, aspects-002, tuples-001,
  pipeline-001 to 003, fk-001.
- src/nexus/aspect_worker.py, aspect_extractor.py,
  daemon/aspect_worker_daemon.py, db/http_pipeline_client.py.
- Beads nexus-4vx0a (the SIGTERM release) and nexus-lqaq1 (the heartbeat gap).

## Revision History

- 2026-10-26: drafted.
- 2026-10-26: second pass against develop 609c8501f (bead nexus-58gzh). Line
  references in F2 to F16 re-checked; moved: F4, F5, F6, F7, F8, F9, F10, F11,
  F12, F12a, F14 and F15. New findings F17 to F24 test A2 to A4 and Q1 to Q3.
  Claims in the draft that the records contradict, now fixed:
  - The risk and A4 text assumed mailbox and ledger consumers park an `in`.
    None does. The only standing parker is one MCP `ChannelWaiter` `wait` per
    live session (F20).
  - The producer and Day 2 text relied on a catalog purge deleting a task by id.
    No such route exists, so only the claim-time drop remains (F22).
  - E4 relied on a dirty `ack` as if it were available. `ack` always consumes;
    the dirty path is new, `release`-shaped engine work (F23). The same holds
    for the `nack`-to-dead step.
  - The worker loop parked an `in` per collection. Past 4 collections per
    claimant that is refused, so it uses one multi-subspace `wait` or one queue
    per tenant (F21), and `in` matches keys only, never dims (F24).
  - The doctor claim: the depth and dead-letter row sees only `queue/`
    templates, so `aspects/` needs it widened (F18).
  - F12 now says the tuple id includes `id_dims`, and F7 to F11 line ranges
    were corrected. E2 and E3 now state the missing `document_text` route, the
    15,552,000 s claim-log ceiling and the template classpath list.
  - A2 held only for notes, and `document_text` has no caller today (F17).
