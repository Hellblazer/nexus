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
extracting. A second worker then extracts the same document, and each
finisher writes the aspect row and deletes the queue row. The daemon's own
comment records this race (`aspect_worker_daemon.py:77-83`).

The harm today is small. Both writers upsert the aspect row
(`.onConflict(DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION,
DOCUMENT_ASPECTS.SOURCE_PATH).doUpdate()`, `AspectRepository.java:265-269`), so
the later write replaces the earlier row with another extraction of the same
document. `markDone` is `ctx.deleteFrom(ASPECT_EXTRACTION_QUEUE).where(
ASPECT_EXTRACTION_QUEUE.DOC_ID.eq(docId))` with no status or owner test
(`AspectRepository.java:1159-1161`), so the first finisher deletes the row the
second worker holds and the second finisher's delete removes nothing. The
daemon comment calls this "double-extraction + wasted quota"
(`aspect_worker_daemon.py:81-83`). What is lost is the second extraction's
model quota. A re-enqueue that lands during an extraction is a different case,
covered by E4.

The move closes the overwrite. Today the aspect write and the queue delete are
two calls (`t2/__init__.py:846-847`), and a tuple `ack` alone would fence only
the second. So the aspect write itself checks the claim, in the same
transaction as the write (E5). The `renew` lease keeps a healthy slow batch
from being reclaimed at all.

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
- a dead-letter on request, for a failure no retry will fix (`nack`
  dead-letters only when the attempt count reaches `max_attempts`, F23);
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
| RDR-206 | Precedent | `renew` and the compare-and-swap `ack` on `claim_id` are the fencing Gap 1 needs for the claim. E5 carries the same check into the aspect write. |
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
| `TupleWaitRegistry` | Yes | A 1 s re-query and a 25 s cap on a parked call. The park caps (default 16 global; the cloud measured 32, research-27; 4 per claimant) are engine-wide, not per tenant (F19). |
| `TupleHandler` routes | Yes | There is no delete or purge route for a tuple (F22). |
| `TemplateRegistry`, sweep, doctor checks | Yes | A new template can carry its own limits without changing other queues. The depth and dead-letter doctor row sees only `queue/` templates (F18). |
| `PipelineRepository` | Yes | `run_epoch` is checked under the row's `FOR UPDATE` in every write route. Ownership comes from heartbeat age. |
| `document_text(doc_id)` stored function | Yes | It returns rows, not one string, and no Java route or Python client calls it. For a note, joined in order, it equals the text enqueued today (F17). The design does not use it (E2). |
| `AspectHandler`, `AspectRepository.upsertAspect`, `liveClaimRowQuery` | Yes | The aspect write is one route whose repository method runs in a single transaction, and `ack` finds its claim through a lookup that also requires a live lease, so the write can run the same lookup (E5, research-28, research-30). The route's error ladder maps no typed tuple error today (E5). |

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
  Timeouts: 180 s for one document (aspect_extractor.py:2388-2393; the module
  docstring says the same at :52), and 180 + 60 per extra document in a batch,
  which is 420 s at the default batch of 5 (`_BATCH_TIMEOUT_PER_EXTRA_PAPER_S`
  at :1643, the 420 s in the comment at :1642, the sum at :1841; default size
  at aspect_worker.py:326). Each call gets three attempts (`_RETRY_ATTEMPTS = 3`,
  :1206).
- **F6 (Documented). Discovery.** A backstop poll every 300 s
  (`DEFAULT_POLL_INTERVAL_S`, aspect_worker.py:118), plus a same-machine wake
  file checked every 1 s (`WAKE_CHECK_INTERVAL_S`, :128; `_wait_for_wake_or_timeout`
  at :469). The comment at :121-122 says there is no LISTEN/NOTIFY.
- **F7 (Documented). Body.** On the MCP `store_put` path `fire_note_chains` calls
  `fire_document` with the whole note content and the catalog tumbler
  (note_write.py:989, :1022). `aspect_extraction_enqueue_hook` passes that
  content into `aspect_queue.enqueue` (aspect_worker.py:1558-1644, the call at
  :1640-1644), so the row's `content` column holds the whole document and
  `source_path` is the note's first chunk id (`doc_ids[0]`, note_write.py:1022;
  the catalog tumbler travels separately as `doc_id`), not a file. The
  docstring at :1586-1588 says the
  CLI paths pass empty content, and :1599-1601 says the worker then falls back
  to a file read. That docstring is stale. The code is `extract_aspects`: with
  `content == ""` it builds a `chroma://` URI and `read_source` reassembles the
  document from T3 chunks through the catalog manifest
  (aspect_extractor.py:1518-1525). The source file is read only to put an RDR's
  frontmatter back in front of the chunk text (:1526-1529). The reader joins the
  chunks with a blank line (aspect_readers.py:517).
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
  maps to 404, and writes nothing. Before that update, each of the four first
  finds its row through `liveClaimRow` (:2010, :2149, :2202, :2262), whose query
  also requires `lease_until` in the future (`liveClaimRowQuery`, :2672-2678).
  So a claim whose lease has lapsed is refused with 404 whether or not anyone
  has re-taken it (docs/tuple-space.md:181); `liveClaimCondition` alone has no
  lease term and only guards the UPDATE (research-30). A parked call wakes on a same-process signal
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
  comment), so using it would need a new engine route or reassembly through
  `store_get`; the design takes neither, because the worker already has a
  reassembly of its own (E2, A2); (2) this holds only for notes: `docs__` and
  `rdr__` file chunks go through the markdown chunker, which prepends headings
  and overlaps, so for a file source `document_text` is not the original text;
  (3) I did not read the engine round trip of `chunk_text`. The test that settles (3) is an integration test
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
  and hyphens; a segment is capped at 256 bytes. A literal name (no `<param>`
  segment) is looked up before any template (TemplateRegistry.java:204-222;
  `isLiteral`, TemplateSchema.java:141-143), which the one `aspects` queue uses
  (E3). The sweeps delete by each row's
  own `expires_at`, stamped at write from its template, and lapse leases with
  each row's own template `max_attempts`; none reads a global retention.
  `max_live_rows` counts live rows per subspace, so the one `aspects` queue
  caps a tenant's backlog and dead rows together: a dead row is unconsumed, so
  it counts as live until it expires (TupleRepository.java:2867, :2874, :665-671,
  research-28). Doctor: `_check_tuple_queue_depth` keeps only templates
  whose name starts with `queue/`, so `aspects` is invisible to the depth
  and dead-letter warning (a gap to fill, not a regression).
  `_check_tuple_unclaimed_age` applies to every take-enabled non-lock template
  with a fixed 3,600 s threshold, so a bulk backlog on `aspects` older than
  an hour turns that row red. `park_slots` and `sweep_freshness` are global.
  *Source: service/src/main/java/dev/nexus/service/tuples/TemplateRegistry.java:86-93, :202, :224-240, :395-413; TemplateSchema.java:67-89; TupleLimits.java (MAX_FIELD_VALUE_BYTES); TupleRepository.java:653-671, :2514-2532, :2596-2606, :2644-2654; src/nexus/health.py:7672, :7801-7990, :8668, :8758*
- **✅ Verified** (source search) — F19, A4a. The park caps default to 16 global,
  4 per claimant and 25 s per call (`DEFAULT_PARK_CAP_GLOBAL = 16`,
  TupleRepository.java:107); the cloud runs 32 global (research-27). Each has an
  env knob
  (`NX_TUPLE_PARK_CAP_GLOBAL`, `NX_TUPLE_PARK_CAP_PER_CLAIMANT`,
  `NX_TUPLE_TIMEOUT_CAP_SECONDS`). They are engine-wide, not per tenant: there
  is one global counter and one per-claimant map keyed by the bare claimant
  string. A parked `in` takes one global and one claimant slot; `inp` never
  parks (docs/tuple-space.md:98), so it takes neither; `rd`
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
  doctor warns at 75 percent (12 of the default 16), and on the default 16 slots
  shared by all tenants, an engine with 12 or more live sessions has no room for
  a parked `in` per tenant worker without raising `NX_TUPLE_PARK_CAP_GLOBAL`.
  The cloud's cap is 32, not 16 (research-27: 9 of 32 slots in use, no refusal,
  with nine live sessions on one box), so there the same arithmetic starts at 24
  sessions. A busy-hour peak and the local engine's cap are not measured. To
  settle it, read `GET /v1/tuples/park_stats` on the cloud engine at a busy hour
  (`global_in_use`, `refused_global`).
  *Source: src/nexus/mcp/channel.py:162, :1134-1187; src/nexus/mcp/core.py:7877; src/nexus/commands/tuple_cmd.py:239, :328; src/nexus/health.py:7691, :8574*
- **✅ Verified** (source search) — F21, A4c. Per-collection queues with one
  parked `in` each do not fit. N collections parked at once cost N global and N
  claimant slots under one claimant id, so more than 4 collections per worker
  claimant is refused with `ParkCapExceededException`, and the N also count
  against the global cap (default 16). Two shapes fit. (a) One worker parks one announce-mode
  `wait` over up to 34 `aspects/<collection>` subspaces (one global slot, no
  claimant slot) and claims with `inp`, which never parks, each claim under its
  own claimant string (F25); the subspace list
  comes from `subspace_list` with prefix `aspects/` and must be rebuilt when a
  collection appears. (b) One queue per tenant, `aspects/<tenant-scoped name>`,
  with the collection in the body or dims; a parked `in` per tenant then costs 1
  global plus 1 claimant slot, and `in` cannot select by collection (F24), so the
  worker takes the oldest task of any collection, which is fine for this worker.
  The design takes shape (b), as one queue named `aspects` per tenant (Sam, gate
  round 1; E3): `claimNext` already takes any collection
  (AspectRepository.java:1098-1102), and `claim_batch` workers handle a claim
  that mixes prefixes (aspect_worker.py:601-610).
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
  deterministic id this RDR wants (nonce = doc_id) is reachable
  only by nonce, which `in` cannot filter on.
  *Source: service/src/main/resources/tuples/templates/queue.yaml:49-50; TupleRepository.java:1069-1077, :1850-1853, :1978-1985*
- **✅ Verified** (source search) — F25. A repeat `in` or `inp` by the same
  claimant on the same subspace and keys pattern returns the claim that claimant
  already holds, so one claimant string cannot claim five tasks. Before the
  claim statement, `claimOnce` looks for a row matching the pattern that is
  `claimed`, held by this claimant, unconsumed, unexpired and with `lease_until`
  in the future, and returns it with no update and no log row
  (TupleRepository.java:1860-1878; docs/tuple-space.md:97-98, :179). A claim
  whose lease has lapsed is not returned this way, even to its own holder. A
  claimant string is capped at 128 bytes (TupleLimits.java:44). The aspect
  worker has no worker identity today (no `worker_id` or `claimant` in
  aspect_worker.py or aspect_worker_daemon.py), so the design gives each claim
  its own claimant string (Worker side, step 1).
  *Source: service/src/main/java/dev/nexus/service/db/TupleRepository.java:1858-1878, :1978-1985; TupleLimits.java:44; docs/tuple-space.md:97-98, :179; research-30*

### Critical Assumptions

- [ ] **A1**: Adding a nullable `available_at` column and one claim-predicate arm
  (`available_at IS NULL OR available_at <= now()`) keeps the claim's index
  scan. **Status**: Unverified. **Method**: Spike (EXPLAIN on a seeded
  tenant). The spike is still to run.
- [ ] **A2**: A task can carry the document's identity instead of its text,
  and the text the worker gets by the existing empty-content contract (the
  document reassembled from T3 chunks through the catalog manifest, F7) is an
  acceptable input for aspect extraction. **Status**: Re-scoped at gate round 1.
  The contract is read in source (aspect_extractor.py:1518-1525), and it is
  what a CLI-indexed document gets today, because the CLI paths pass empty
  content (aspect_worker.py:1586-1588). What is not settled is the MCP path.
  Why it enqueues the whole text at all is not stated in the code read. For a
  note stored in several pieces the reassembled text joins the pieces with a
  blank line (aspect_readers.py:517), while the MCP content is the pieces joined
  with nothing (F17), so the model would see a different text from today's.
  Inferred by comparing the two reads, not run. **Method**: Source Search. The
  settling check is to extract a sample of multi-piece notes both ways and
  compare the aspect rows; it has not run. `document_text(doc_id)` is not part
  of this assumption any more (F17).
- [x] **A3**: An aspect-specific template can have a larger `max_live_rows`,
  a longer retention and its own attempt cap, without changing the sweep or
  the doctor rows for other queues. **Status**: Verified (F18). The doctor's
  depth and dead-letter row does not see the `aspects` template, and its
  unclaimed-age row would go red on a backlog older than an hour. **Method**:
  Source Search (template loader, sweep, doctor).
- [ ] **A4**: A parked `in` loop (25 s calls) per tenant worker fits within the
  park-slot caps, alongside the existing parkers. **Status**: Split, three
  parts. (a) The caps are verified: default 16 global (the cloud measured 32,
  research-27), 4 per claimant, 25 s, engine-wide (F19). (b) Fit alongside the
  existing parkers is Assumed, pending `park_stats` on the cloud engine at a
  busy hour (F20); one reading at nine live sessions found 9 of 32 in use
  (research-27). The mailbox and ledger have no parked `in` consumer; the
  standing parkers are the MCP `ChannelWaiter` waits, one per live session.
  (c) Per-collection parked queues are verified not to fit past 4 collections
  per claimant (F21); the design uses one `aspects` queue per tenant, so one
  worker parks one `in`, under its slot-0 claimant string: 1 global slot and
  1 of that string's 4 (E3, F25).
  **Method**: Source Search; the busy-hour count of live parkers is still
  missing.

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

Decided at gate round 1 (Sam):

- Which queue shape (F21 (a) or (b))? One `aspects` queue per tenant with the
  collection in the task body (E3). The claim-time drop's key is the task's
  `doc_id`, carried in the body.
- How does a worker get a note's text? The task carries the identity, and the
  worker uses the existing empty-content contract (E2, A2), not a
  `document_text` route.

Still open:

- Plan-time (A1): does the `available_at` arm keep the claim's index scan on a
  seeded backlog? The EXPLAIN spike has not run.
- Runtime (A4b): `park_stats` on the cloud engine at a busy hour.
- No one has measured a bulk backlog depth (F1), so E3's `max_live_rows` is
  unset.
- A2: is the reassembled text an acceptable input for a multi-piece MCP note?

## Proposed Solution

### Approach

Two decisions.

**Decision 1: move the aspect queue onto the tuple space, behind five
extensions, four of them in the engine release (E1, E3, E4 and E5; E2 is
client-only), in phases.** The move closes Gaps 1 to 3 with mechanisms that
already exist and are tested in the tuple space, plus one fence that is new:

- a `claim_id` that the aspect write itself checks, in the same transaction as
  the write (E5), so a worker whose claim is no longer live (another worker
  took it over, or its lease lapsed) cannot overwrite the new holder's result;
  the `ack` that follows is also a compare-and-swap on `claim_id` and looks the
  claim up the same way;
- a real lease with `renew`, so a batch longer than the lease is kept alive
  by its worker, not reclaimed under it;
- attempts that count a lapsed lease, so a crash-looping document dead-letters;
- a parked wait, so a cross-machine enqueue is seen in about 1 s.

The move does not happen until the template can carry the queue (Gap 4). The
five extensions are:

- **E1. Delayed visibility.** A nullable `available_at` on `nexus.tuples`, one
  arm in the claim predicate, `out` with `delay_s`, and `nack` with
  `retry_after_s`. Both are stamped on the database clock, as RDR-163 stamps
  `next_retry_at`. The ladder's intervals stay worker-chosen.
- **E2. Identity-only task body.** The task body carries the queue row's own
  identity columns, `{collection, source_path, doc_id, content_hash}`, never
  document text. These are four of the nine fields `claimNext` returns (`id`,
  `collection`, `source_path`, `content_hash`, `content`, `status`,
  `retry_count`, `doc_id`, `last_attempt_at`; AspectRepository.java:1126-1134).
  The task carries the identity, and the
  worker uses the contract it already has for a row with empty content: it
  reassembles the document from T3 chunks through the catalog manifest, and for
  an RDR it puts the source file's frontmatter back in front (A2, F7). That
  needs no new engine route and no disk read for the text, so a worker on
  another host reads what a laptop wrote. One consequence: for a note stored in
  several pieces, the text the model sees is the reassembly, not the original
  content (A2). The four fields fit under the 4,096-byte cap for any
  `source_path` shorter than about 3.5 KB (inferred from the cap, not
  measured); `source_path` is unbounded TEXT (aspects-001-baseline.xml:157), so
  a longer one is refused by `out` and fails loud at the producer. The cap
  itself stays as it is.
- **E3. An aspect queue template.** A new literal template named `aspects`,
  not a retune of `queue/`, so the board, ledger and ad-hoc queues keep
  RDR-211's sizing. It is one queue per tenant (tenancy is the row-level
  security of F12b), and the collection is a field of the task body, not part
  of the queue name. A worker then parks one `in`, the template is sized once,
  and a collection rename is a body rewrite (Technical Design). The registry
  looks up a literal name before any template (TemplateRegistry.java:208-222).
  None of the seven shipped templates is literal, and the two existing tests of
  a literal name use a two-segment one (`ledger/fixed`, TemplateSchemaParserTest
  :60, TemplateRegistryTest :465). The parser has no minimum segment count
  (TemplateSchemaParser.java:216-231), so a one-segment name parses and
  resolves by reading, and no test has run one; the first E3 test settles it,
  and the fallback is `aspects/<name>` with one fixed name, which changes
  nothing else here. The file, in full (limits are explained below it):

  ```yaml
  name: aspects
  keys:
    queue:
      values: [aspects]
  id_from: keys+nonce
  take:
    enabled: true
    max_attempts: 6
    max_lease_seconds: 900
  retention_seconds: 7776000
  max_live_rows: <from the measured backlog>
  claim_log_ttl_seconds: 8640000
  ```

  - The one key is `queue`, as in `queue.yaml` (`keys: [queue]`), with the
    mapping form that lists the one allowed value, `aspects`. `out` refuses
    any other value (TupleRepository.java:821-822), and `in` and `inp` refuse a
    call that does not pin every template key (:1849-1853), so the producer and
    the worker both pin `queue = aspects`.
  - `id_from: keys+nonce`, with the nonce set to the `doc_id` (a tumbler fits
    the 128-byte nonce ceiling, TupleLimits.java:41). There are no dimensions
    and no `id_dims`: `queue.yaml` needs `from` in its id because its nonce is
    minted, and here the id comes from the `doc_id` alone.
  - `take.enabled: true`, as in `queue.yaml`, whose header says take is what
    lets any free worker claim the oldest available task with `in()`
    (queue.yaml:1-7). `max_lease_seconds: 900`
    is at least the worker's 600 s lease; a longer lease request throws
    `LeaseTooLongException` (:1845-1848).

  Its own limits:
  - `max_live_rows` large enough for a bulk index. It counts the tenant's
    backlog and its dead tasks together, because a dead task is unconsumed
    (F18);
  - `retention_seconds: 7776000` (90 days). Retention is the lifetime of a
    backlog task, of a dead task and of an acked row alike, so it is long
    enough that no backlog expires before a worker reaches it, and a dead task
    stays for 90 days. Today a failed row never ages out, so this is a
    behaviour change (Dead tasks, below). The 90 days is chosen, not measured;
  - `max_attempts: 6`, the ladder cap plus one (`_RETRY_MAX_ATTEMPTS = 5`,
    aspect_worker.py:97; the worker fails a row terminally once its retry count
    reaches the cap, so six claims are allowed). One count serves both a
    `nack` (a ladder retry) and a lapsed lease (a crash or a stalled worker),
    because each adds 1 to it (F11). So a crash spends one of the retries the
    ladder would have had;
  - `claim_log_ttl_seconds: 8640000` (100 days). The boot check requires the
    effective TTL to exceed retention plus the 21,600 s sweep interval, which
    is 7,797,600 s for 90 days, and a template may only shorten the engine
    default of 15,552,000 s, or the engine refuses to boot (F18). Because the
    template TTL may not exceed the engine's, the engine's
    `NX_TUPLE_CLAIM_LOG_TTL_DAYS` must be at least 100, which is 8,640,000 s:
    unset is 180 days and passes, and a host that lowered it below 100 makes
    the engine refuse to boot with this template, naming the variable
    (TemplateRegistry.java:70-71, :395-402). The operator step: before the
    engine release that carries `aspects.yaml` reaches a host, read that host's
    `NX_TUPLE_CLAIM_LOG_TTL_DAYS`, and raise it to at least 100 or leave it
    unset. How an operator reads the live value on a given host was not read.

  `max_live_rows` is the one number that comes from a measured bulk backlog,
  which F1 says nobody has yet. Measuring it is a prerequisite, and a template
  that omits the field is unbounded (TemplateSchemaParser.java:122-128), so
  the file does not ship with the placeholder. The template
  file must also be added to the loader's explicit classpath list, and the
  doctor's depth and dead-letter row, which sees only `queue/` templates, must
  be widened to see it (F18).
- **E4. Re-enqueue, and dead-letter on request.** Two engine changes. The
  first is an `out` mode `replace` for the document's deterministic id (nonce =
  `doc_id`; the collection is not part of the id, so a rename or a move never
  changes it):
  - a consumed or dead row is reset to available, with the new body, attempts
    at 0 and its retention clock restarted (`created_at` and `expires_at`),
    because a refire is clamped to `created_at` plus retention
    (TupleRepository.java:675-680), and an old dead row would otherwise expire
    soon after its revival;
  - an available row gets the new body;
  - a claimed row is marked `dirty`, so its `ack` returns it to available
    instead of consuming it. This is new engine work. Today `ack` always
    consumes the row (F23). The new path should take the shape of `release`
    (attempts unchanged) with its own claim-log transition name, not `nack`,
    which counts an attempt.

  This keeps today's "the newest content wins" behaviour (F8) without the
  double extraction the current upsert causes on an `in_progress` row.

  The second is a `nack` that takes `dead: true`, which dead-letters the claim
  at once, with the attempt counted and the claim-log rows `nack` then `dead`,
  as `nack` writes when the count reaches `max_attempts`. It goes through
  `releaseOrDeadLetter` (TupleRepository.java:2379-2395) with the test
  `attempts >= maxAttempts` joined by the flag, so a task becomes dead at the
  same two code sites as today (research-29). The worker needs it for a failure
  no retry will fix: a non-retryable exception and the refusal of an
  unattributable row (`_is_unattributable`, aspect_worker.py:1349), which are
  terminal `mark_failed` calls today. There is no `ack` with a failure record
  as an alternative: `ack` clears the body (TupleRepository.java:2048), so
  there is nowhere to keep it.
- **E5. A claim-fenced aspect write.** The `POST /v1/aspects/upsert` route takes
  an optional `claim_id` and `claimant`, which come together or not at all
  (one without the other answers 400). When they are present, the route looks
  the claim up, in the same transaction as the `document_aspects` upsert, with
  the lookup `ack`, `nack`, `renew` and `release` use: `liveClaimRowQuery`
  (TupleRepository.java:2672-2678), which requires the `claim_id`, claim state
  `claimed`, not consumed, and `lease_until` in the future. The query is
  package-private and `AspectRepository` is in the same package, so E5 calls
  it and adds the row lock (`FOR NO KEY UPDATE`, the level the claim path takes
  and skips with `SKIP LOCKED`; F10); that the lock clause can be added to that
  query, or that its WHERE is hoisted into one shared condition, is inferred,
  not read, and the E5 test settles it. A claim that is not live answers 404.
  That covers a claim another worker re-took, one the sweep released, one
  already acked, and one whose lease has lapsed, whether or not anyone has
  re-taken it. A live claim held by another claimant answers 403. These are the
  answers `ack` gives (`consumeClaim`, TupleRepository.java:2009-2016), so the
  write and the `ack` that follows agree in every case. Nothing is written when
  the lookup fails. A write to a retired collection name already answers 409
  with nothing written (`ensureCollectionRegistered`,
  AspectRepository.java:169-174); the worker handles it in Worker side, step
  3. The route must render these codes: `AspectHandler`'s
  error ladder has only `IllegalArgumentException` (400) and a typed database
  error or 500 (AspectHandler.java:146-148), so a `ClaimNotFoundException`
  would answer 500 today. E5 adds a `TupleException` branch that sends the
  exception's own HTTP code, as `TupleHandler` does (TupleHandler.java:195).
  The upsert already runs in one transaction (`tenantScope.withTenant` in
  `upsertAspect`, AspectRepository.java:225-226), so the lookup joins it, and a
  re-take skips the locked row until the write commits (inferred from the lock
  rules, not read). Callers with no claim omit the fields and behave as today:
  `commands/enrich.py:2202` (through `complete_aspect`) and `:3222` (a direct
  `document_aspects.upsert`) are the two (census in research-30); `nx aspects`
  calls neither. A crash between the write and the `ack` lapses the claim, the
  task is re-taken with an attempt counted, and the re-extraction upserts
  again, which is idempotent (`t2/__init__.py:829-830`). The same holds if the
  lease lapses in the milliseconds between a successful write and the `ack`:
  the `ack` answers 404, the aspect row stands, and the task is re-extracted
  into the same row, a wasted extraction and not a wrong result.

**Decision 2: the PDF pipeline row does not move, not even its ownership.**
Design Rationale argues this from F13 to F16.

### Technical Design

**Producer side.** `HttpAspectQueue.enqueue` and `enqueue_many` become `out`s
to the `aspects` queue with the identity body and `replace` (nonce = `doc_id`).
The hook sites (F1) do not change. The FK cascade (F9) has no tuple equivalent.
A claim of a document the catalog no longer has is `ack`ed as dropped, with a
log event (`aspect_task_dropped_missing_doc`). For one deleted document that
claim-time check is the only way its task goes away: there is no client tuple
delete route (F22), so a document delete cannot remove the task, and a task
stays queued until a worker claims it or retention ends it.

**Collection rename, move and purge.** The collection is a field of the task
body, so a rename is a body rewrite, not a queue rename. Ten engine sites name
the queue table, or loop over the registry that lists it, and each gets a tuple
form in the same transaction as its table form, while the table drains. The
census is by grep of `service/src/main/java` for `ASPECT_EXTRACTION_QUEUE` and
for `COLLECTION_SCOPED_TABLES` (research-30): five sites that name the table,
and five loops over the registry. Cross-model re-embed and the ghost sweep run
through the registry loops, so a census that stopped at the table-naming sites
would miss them.

The tuple form is one hand-written helper in the `db` package (called
`AspectTaskRefs` here; the name is a proposal), with three statements over the
tenant's unconsumed `aspects` tasks, which means available, claimed and dead
ones: count the tasks naming a collection, rewrite `collection` in their body,
and delete them. A task body is TEXT (tuples-001-baseline.xml:79-81), so the
statements read it through a cast to JSON; no index serves that match, so each
statement scans the tenant's unconsumed `aspects` rows, which `max_live_rows`
bounds (both inferred, not read). The helper is called next to each site, in
that site's transaction.

| Site | Today | Tuple form |
| --- | --- | --- |
| `AspectRepository.renameQueueCollection` (:1334-1350), refused under a re-embed job | deletes the new-side rows that collide, then sets `collection` | rewrite `collection` in the body of every unconsumed `aspects` task naming the old collection, under the same re-embed refusal. There is no collision step: the id comes from the `doc_id`, and one document has one tumbler. |
| `CatalogRepository.completeMove` (:6418-6433), one document's re-embed move | deletes the source-side row that collides with a target-side row, then sets `collection` on the document's row | rewrite `collection` in the body of that document's unconsumed task |
| `CatalogRepository` collection purge (:8942) | deletes the collection's queue rows | delete the unconsumed `aspects` tasks whose body names the collection, inside the purge transaction. This is the one `deleteFrom` on `nexus.tuples` outside the sweep. It is an engine statement, not a client route, so F22 (no client-reachable delete) stands. |
| `ReembedSourceReader` residue count (:141, :151-152) | counts queue rows naming the source collection | add the unconsumed `aspects` tasks whose body names the source |
| `ReembedSourceReader` plan count (:377-379) | counts queue rows naming the source and not yet moved | add the unconsumed `aspects` tasks naming the source whose `doc_id` has not moved |
| `blockingTable` (:9593), loop at :9594; behind `collectionIsEmpty` (:9630, public wrappers :9502 and :9581) and the rename's merge refusal (:9748) | the first registry table holding a row for the name; the queue table counts, because only the four audit tables are excused (`AUDIT_ONLY_TABLES`, :9526) | an unconsumed `aspects` task naming the collection counts as a row, under its own key, `aspects_tasks`, and is not audit-only |
| `collectionHoldsContent` (:9645), loop at :9646; called by a collection delete's sibling cleanup (:8884), the retired-name prune (:8992) and the ghost sweep (:10626) | true when any non-audit registry table holds a row for the name | an unconsumed task naming the collection counts as content, so a registry row a queued task names is not swept as a ghost |
| `renameCollectionTxn` step 2 (loop at :9931) | updates every registry table from the old name to the new | rewrite the body of every unconsumed task naming the old name, and report it in the counts map under `aspects_tasks` |
| `rehomeCollection` (:10148, loop at :10195, through `moveScopedTable` :10337) | moves each registry table's rows from the source to the target, except the cascade-carried two | rewrite the body of every unconsumed task naming the source to name the target; no collision predicate is needed, for the reason in the first row |
| `rehomeStatus` (:10234, loop at :10238) | counts what still names the source, and how much of it can move | add the tasks naming the source to `total`, `movable` and the per-table map, every one of them movable |

The queue table's own registry entry and disposition line (`COLLECTION_SCOPED_TABLES`
:9350, `CROSS_MODEL_DISPOSITIONS` :9400, REPOINT) stay while the table exists.
The tasks cannot be a registry entry, and need no REPOINT line there. A
`CollectionScopedTable` is a jOOQ table plus a `collection` column
(CatalogRepository.java:9370), and `nexus.tuples` has no such column. The
registry's other list, `CROSS_MODEL_DISPOSITIONS_OUTSIDE_REGISTRY`, is tested to
hold only tables that exist in the schema and carry a collection-named column
(`CrossModelDispositionParityTest`, :74-100). And that test pins the REPOINT set
to exactly five tables, the queue table among them (:127-139), so a sixth line
would fail it. So the helper carries its own disposition, REPOINT, as a constant
beside the registry, and its own parity test: a Java source scan in the style
of `ManifestWriteRetryGateTest`, which already scans `CatalogRepository` for
loops over `COLLECTION_SCOPED_TABLES`. It fails when a loop over the registry or
a direct statement on the queue table has no helper call in the same method.
It also asserts that it examined at least ten sites, so a scan that finds
nothing cannot pass, and it has a planted-site case that must be reported. When
the table retires, that changeset removes its registry entry and its
disposition line and changes the five-table pin; the helper then stands as the
fifth REPOINT line.

A task held by a worker when a rename or a move lands is rewritten too, as the
table rename updates by collection whatever the row status
(AspectRepository.java:1345-1348). Its worker's write then names the old
collection. After a rename the old name is retired, so the write is refused
with 409 (`ensureCollectionRegistered`, AspectRepository.java:169-174; Worker
side, step 3). After a one-document move the source collection is still live,
so the write is not refused and lands under it: the same exposure an
`in_progress` row has today (inferred, not run).

**Dead tasks (a behaviour change).** Today a `failed` queue row stays until it
is re-enqueued or its document or collection goes. The engine's deletes of queue
rows are `markDone`, the rename and move collision defences and the collection
purge, and none looks at age (research-28). A dead tuple expires with its row's
retention, and until then it counts against `max_live_rows`. This RDR gives dead
tasks no lifetime of their own: they get the template's 90-day retention (E3).
After 90 days a dead task leaves the listing and `nx aspects requeue-failed`
cannot revive it; its document has to be indexed again. `replace` on a dead task
restarts its retention clock (E4).

**Readers of the old table.** Four engine routes read the queue table only:
`/queue/pending_count`, `/queue/is_drained`, `/queue/list_pending` and
`/queue/list_failed` (AspectHandler.java:133-136, handlers :620-640). They stay
as they are until the table retires, so every reader below adds a tuple form
beside its table call, or it goes blind to new work the day the producer flips.
The census is a grep over `src/nexus` for the four client methods (research-30):

| Reader | Calls | Tuple form while both exist |
| --- | --- | --- |
| the store hook's after-enqueue depth, `_best_effort_queue_depth` (aspect_worker.py:1707, caller :1780) | `pending_count` | table `pending_count` plus `subspace_stats` on `aspects`, field `available` |
| `drain_worker` (aspect_worker.py:1139; polls at :1254 and :1261, timeout detail at :1281), behind `nx aspects drain` (aspects.py:34-70, the gate the upgrade tells an operator to run) | `is_drained`, `pending_count` | drained means the table is drained and `aspects` shows `available` plus `claimed` equal to 0; dead tasks do not block it, as failed rows do not block it today. The timeout detail reads `claimed` and `lapsed` for tasks, where it reads `pending_count` for rows |
| `nx doctor` aspect-queue report, `_report_aspect_queue_service` (doctor.py:1084-1126) and `_run_check_aspect_queue` (:1177) | `pending_count`, `list_failed` | pending adds `available`; the failed count adds the dead tasks, and a nonzero count still fails the check |
| the console health collector, `_collect_aspect_queue_data_service` (console/routes/health.py:130-160) | `pending_count`, `list_failed` | the same two additions |
| `nx aspects requeue-failed` (aspects.py:304) | `list_failed` | the table's failed rows plus the dead tasks, read with `rd` on `aspects`, filtered on `claim_state` `dead` and paged with `since`, because `n` is clamped to the engine's read cap (TupleRepository.java:1040-1046); reading dead rows through `rd` is documented (docs/tuple-space.md:177), and filtering them on the client is inferred, not read |
| `list_pending` | no caller in `src/nexus` | table only |

The tuple forms use the existing `subspace_stats` and `rd` routes, so they need
no engine change. After the table retires, its half is removed from each form
in that changeset.

**Worker side.** The RDR-173 per-tenant daemon keeps its lease. Its loop
becomes:

1. Claim a batch of up to 5, each claim under its own claimant string. A repeat
   `in` or `inp` by one claimant on the queue returns the claim that claimant
   already holds (F25), so five claims under one string would be one task five
   times. The worker has a worker id `W`; none exists today, so the
   implementation chooses one that is unique per live worker process (tenant,
   pid and a start nonce) and short enough that `W:<slot>` stays under the
   128-byte claimant ceiling. The claimant for the claim in slot `n`, `n` from 0
   to 4, is `W:n`. Slot 0 is one parked `in` on the `aspects` queue (25 s a
   call), with a lease of 600 s. Slots 1 to 4 are `inp`, each under its own
   string, which never park. The park cost is 1 global slot and 1 of the 4 that
   belong to the string `W:0`, however many collections the tenant has (F19,
   F21); the `inp` calls take neither. `in` cannot select by collection (F24),
   so the worker takes the oldest task of any collection, as `claimNext` does
   today (AspectRepository.java:1098-1102), and reads the collection from the
   body. A claim left live by a failed `ack` comes back to its slot's next `in`
   or `inp` as the same claim (F25), and the worker finishes it as its own.
   While the old table has pending rows, the tuple turn is `inp` and not a
   parked `in` (Migration).
2. Extract, with a renewer thread that calls `renew` every 120 s on every claim
   in the batch, each by its own `claim_id` and its own claimant string, and
   only while the extraction call is inside its own deadline.
   The worker computes that deadline from the extractor's limits. Each call gets
   3 attempts, each timed out at 420 s for a batch of 5 (180 s for one
   document), with sleeps of about 2 s and 5 s between attempts
   (aspect_extractor.py:1206-1208, :1841), so one sub-batch is at worst about
   1,270 s; a call over several sub-batches sums them. Past that deadline plus a
   margin the renewer stops, the leases lapse within 600 s, and the tasks are
   retried with an attempt counted. A wedged extraction therefore releases its
   tasks; a renewer that ran on until retention would hold them for days.
3. On success, one call per task persists it (the persist call, below): write
   `document_aspects` with that task's `claim_id` and claimant string (E5), then
   `ack` with the same pair. A refused write means the claim is not the
   worker's any more. On 404 (the claim was re-taken, released, or its lease
   lapsed) or 403, the worker drops that result, writes nothing and does not
   `ack`. On 409 (the collection name was retired by a rename), the worker drops
   the result and writes nothing as well, and because the claim is still live it
   `release`s it, which counts no attempt (F23); the rename already rewrote the
   task body to the successor (Technical Design), so the next claim extracts
   under the new name (inferred, not run). A claim whose source is unreadable or
   whose collection has no extractor is `ack`ed as dropped, with a log event, as
   `mark_done` clears it today (aspect_worker.py:680, :696, :932, :950).
4. On a retryable failure, `nack` with `retry_after_s` from the RDR-163 ladder.
5. On a non-retryable failure, and on a row refused as unattributable
   (aspect_worker.py:714, and the single-row path through `_mark_failed_routed`, :760), `nack` with `dead: true`, which is part of E4:
   `nack` dead-letters only at `max_attempts` today (F23).
6. On a self-fault stand-down (the worker is shutting down), `release`, which
   counts no attempt.

A batch of 5 is five claims under five claimant strings (step 1). There is
no batch `in`, and RDR-205 says one lands only when a consumer asks. This RDR
does not ask.

**The persist call.** `complete_aspect` (t2/__init__.py:811) is the one call
that persists a result today. It upserts `document_aspects` and then calls the
table's `mark_done`, both under `RENAME_LOCK` (:844-847). It has three callers
(census in research-30): `aspect_worker.py:720` and `:974`, which hold a claim,
and `commands/enrich.py:2202`, which holds none. In the tuple era it takes an
optional `claim_id` and `claimant`. With them, it upserts through E5 and then
sends the tuple `ack` with the same pair, in place of `mark_done`, and keeps
`RENAME_LOCK` around both. Without them, it upserts and calls the table's
`mark_done` as today: that is the claim-less caller, and a row from the old
table during the migration. One consequence: a claim-less caller that extracts a
document leaves that document's tuple task queued, because no client can delete
a task by document (F22), and the worker extracts it again. That second
extraction upserts the same row and wastes quota. The other claim-less caller
of the aspect route, `enrich.py:3222`, is a direct upsert with no queue call at
all.

**Signatures (illustrative; verify during implementation).**

```text
out(template="aspects", body={collection, source_path, doc_id, content_hash},
    keys={queue: "aspects"}, nonce=doc_id, mode="replace", delay_s=0)
    -> {id, created|replaced|dirtied}
in(subspace="aspects", keys_pattern={queue: "aspects"}, claimant="<W>:0",
   lease_s=600, timeout_s=25) -> (row, claim_id) | None
inp(subspace="aspects", keys_pattern={queue: "aspects"}, claimant="<W>:<1..4>")
nack(claim_id, claimant, retry_after_s: int | None, dead: bool = False) -> {state}
POST /v1/aspects/upsert {<aspect fields>, claim_id?, claimant?}
    -> {written} | 400 (one of the pair) | 403 | 404 | 409
```

**Migration.** Engine first (E1, E3, E4 and E5, one Liquibase changeset and
template file per the engine-release rules; E2 is a client change), then a
client release.

- A flag `NX_ASPECT_QUEUE=tuples|table` selects the producer.
- In `tuples` mode the worker alternates, one claim batch from the tuple queue
  and then one `claim_batch` from the old table, for as long as the table has
  pending rows, so neither side starves. While the table has pending rows the
  tuple turn claims with `inp`, never a parked `in`, because a parked call would
  hold the table's turn for up to 25 s; once the table has none, slot 0 is a
  parked `in` again (Worker side, step 1). The readers of both sides are in
  Readers of the old table. An old-version client can keep
  enqueuing to the table during a rolling upgrade (the table's enqueue route
  stays registered; inferred, not read), and a rule of draining the table first
  would hold the tuples behind it. The table side ends when the table is
  drained.
- An upgrade-ladder rung moves rows one at a time: `out` first, then delete the
  table row, so a crash leaves a row in both places and never in neither. A
  repeat is harmless because the id is the `doc_id` and `replace` takes a
  repeat. `pending` rows move with `next_retry_at` as the `out` delay (E1).
  `in_progress` rows move as `pending`: their claim is a table status with no
  tuple claim behind it, so a live old-version worker that finishes one later
  deletes nothing (`markDone` deletes by `doc_id`) and upserts the aspect
  again, the double extraction of today.
- A row that holds `content` (the MCP path) moves as an identity body. The rung
  drops `content`, and the worker reassembles the text through the manifest
  (E2, A2). A source that cannot be read drops the task, as `ExtractFail` does
  today (aspect_worker.py:689-698).
- `failed` rows do not move. `out` cannot create a dead task: a task becomes
  dead at two code sites only, the lapsed-lease branch of the claim and
  `releaseOrDeadLetter` (which `nack` and the sweep's lapse release go through;
  research-28), and a third path would be new engine surface for a state that is
  meant to be rare. They stay in the table.
  `nx aspects requeue-failed` is extended to read the table's failed rows and
  the dead tasks (reading dead tasks through `rd` is inferred, not read), and
  re-enqueues each through `out` `replace`.

Retiring the table is a separate, later changeset, after a release with no
reader and with no failed rows left.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Fenced claim, lease, renew, attempts, dead state | `TupleRepository` (RDR-205/206/211) | Reuse. |
| Fenced aspect write | `AspectRepository.upsertAspect`, `TupleRepository.liveClaimRowQuery` | Extend: the upsert looks the claim up, lease included, in its own transaction (E5). |
| Delayed visibility | `AspectRepository` `next_retry_at` arm | Extend: port the arm into the tuple claim predicate (E1). |
| Aspect template | `queue.yaml` | Extend: a new literal template file beside it, not a retune (E3). |
| Content source for identity bodies | the worker's empty-content contract (`extract_aspects`, `read_source`) | Reuse as it is: reassembly from T3 chunks through the manifest (E2, A2, F7). `document_text` is not used. |
| Wake | `aspect_wake` file, 300 s poll | Replace with one parked `in` (F21). Keep the wake file only while the old table drains. |
| Collection rename, move, purge | `renameQueueCollection`, `completeMove`, collection purge, the re-embed counts, and the five loops over `COLLECTION_SCOPED_TABLES` | Extend each of the ten sites with a call to one hand-written helper beside the registry, in the same transaction, with its own parity test. The registry gets no entry for tasks (Technical Design). |
| Readers of the old table | `pending_count`, `is_drained`, `list_failed` callers in the worker, `nx aspects`, doctor and console | Extend each with a tuple form from `subspace_stats` and `rd` (Technical Design). |
| Per-tenant worker lease | `aspect_worker_daemon.py` (RDR-173) | Reuse. |
| Pipeline ownership | `PipelineRepository.create` heartbeat takeover | Keep (Decision 2). |

### Decision Rationale

**Why the aspect queue moves.** Gaps 1 to 3 are each a mechanism the tuple space
already has and tests: fencing (RDR-206), counting attempts (RDR-211) and
cross-instance wake (RDR-205). Hardening the table in place (Alternative 1)
would rebuild those mechanisms a second time, in a second repository, with a
second set of tests and doctor rows. The cost of moving is concentrated in E1
to E5. E1 and E4 are generally useful to any future queue consumer: delay,
replace and dead-letter on request. E2 needs no engine change, and E3 is a
template file. E5 is specific to the aspect write route.

**Why the PDF pipeline row stays.** A `lock/<resource>` tuple could replace one
thing, the decision about who owns a run. It could replace nothing else, and
keeping both would be worse than today:

1. *The write fence would have to be built twice.* `run_epoch` is checked under
   the pipeline row's `FOR UPDATE`, in the same transaction as each page and
   chunk write (F14). A tuple `claim_id` fences another table's write only when
   that write's route checks the claim in its own transaction, as E5 does for
   the one aspect route. Moving ownership to a lock would still keep
   `run_epoch`, because a takeover bumps it in the same UPDATE that claims the
   row (F14), so the run would have two ownership records. A crash between
   taking the lock and bumping the epoch, or the reverse, leaves them
   disagreeing: the lock says A and the epoch says B. Every write route would
   then need to check both.
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

**Reason for rejection**: This is the fallback. If Sam rejects the five
extensions (E1 to E5; E2 is client-only), it closes Gaps 1 to 3 at a lower immediate cost, and this
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

- Positive: a worker whose claim is no longer live (another worker took it
  over, the sweep released it, or its lease lapsed) cannot write its result,
  because the aspect write looks the claim up as `ack` does (E5), and its `ack`
  is refused too. That closes the overwrite in Gap 1. The `renew` lease keeps a
  healthy slow batch from being reclaimed at all, which is what limits the
  wasted second extraction.
- Positive: a crash-looping document reaches a dead state (Gap 2).
- Positive: a cross-machine enqueue is seen in about 1 s, instead of up to 300 s
  (Gap 3).
- Positive: one substrate, and the tuple doctor rows cover the aspect queue
  once the depth row is widened to see the `aspects` template (F18). The
  unclaimed-age row would already go red on a backlog older than an hour.
- Negative: five extensions, four of them in the engine release (E1, E3, E4 and
  E5), an engine release, and a migration rung.
- Negative: the declarative FK cascade becomes a consumer-side check (E2,
  producer side), and a collection rename, move or purge needs a tuple form at
  each of ten engine sites, beside a hand-written helper and its own parity
  test (Technical Design). Every reader of the old table needs a tuple form too.
- Negative: a claim-less caller of `complete_aspect` (`nx enrich`) no longer
  clears the document's queued task, so the worker extracts that document
  again, which wastes quota (The persist call).
- Negative: dead tasks now expire after 90 days, where a failed row today never
  ages out (Dead tasks).
- Negative: for a note stored in several pieces, the text the model sees
  changes from the original content to the reassembly (A2).
- Negative: `rdr__*` documents, which are parsed with no model call, now pay a
  tuple round trip. That cost is negligible next to an engine write.

### Risks and Mitigations

- **Risk**: The tuple claim scan slows with a large aspect backlog in one
  tenant. **Mitigation**: A1's EXPLAIN spike on a seeded backlog of the
  measured size, before E1 lands.
- **Risk**: A worker's renewer thread dies, and the lease lapses mid-extraction.
  **Mitigation**: The late write and the late `ack` are both refused (404),
  whether or not another worker has re-taken the task, because both look the
  claim up with `lease_until` in the future. The task is retried with an
  attempt counted. That is the fenced form of today's silent overwrite.
- **Risk**: A wedged extraction keeps its claims alive. Today such a row is
  reclaimed 300 s after the claim (F3). **Mitigation**: The renewer renews every
  claim in the batch only while the extraction call is inside its own deadline,
  and stops after it (Worker side, step 2). The leases then lapse within 600 s.
- **Risk**: Park slots are exhausted (A4). The global slots (default 16; the
  cloud measured 32, research-27) are shared by every tenant on the engine and
  the standing parkers are one per live Claude session (F19, F20); on the default
  16 an engine with 12 or more live sessions has no room for a parked call per
  tenant worker. **Mitigation**: One `aspects` queue per tenant means one parked
  `in` per worker, 1 slot of the global budget (F21). Read `park_stats` on the
  cloud engine at a busy hour. Fall back to unparked polling every 5 s, which is
  still 60× faster than the backstop.

### Failure Modes

- An enqueue refused by the template (`MaxLiveRowsExceeded`) is loud at the
  producer and logged with the collection. It never drops silently.
- A task whose document vanished is acked as dropped, with
  `aspect_task_dropped_missing_doc`.
- A write refused for a lost claim (404 or 403) is dropped by its worker, with
  no retry from that worker. The task's new holder, or the lease clock, decides
  what happens next. A write refused 409 for a retired collection name is also
  dropped, and its worker releases the claim (Worker side, step 3).
- A dead task shows in `nx aspects requeue-failed --dry-run`, extended to read
  dead tasks, and, once the depth row is widened to see the `aspects` template
  (F18), on the tuple doctor row. It stays for the template's 90-day retention
  and is then removed by the sweep, which failed table rows never were. The
  requeue path is `replace`.
- Diagnosis: `tuple_list` / `tuple_stats` on `aspects` show available, claimed
  and dead counts. The claim log shows each attempt.

## Implementation Plan

### Prerequisites

- [ ] Settle what is still open: run the A1 spike, read `park_stats` at a busy
  hour for A4b (research-27 has one reading), and extract a sample of
  multi-piece notes both ways for A2. A3, A4a and A4c are verified by source.
  The queue shape is decided: one `aspects` queue per tenant (E3).
- [ ] Measure a real bulk backlog. Index one large collection and record the
  peak queue depth and the drain time. E3's `max_live_rows` is set from that,
  not guessed.
- [ ] E1, E3, E4 and E5 ship as one engine release by the engine-release
  procedure (E2 is client-only). Before it reaches a host, check that host's
  `NX_TUPLE_CLAIM_LOG_TTL_DAYS` is unset or at least 100, or the engine refuses
  to boot with `aspects.yaml` (E3).

### Minimum Viable Validation

One tenant, flag set to `tuples`. Index 50 documents into a `knowledge__`
collection, with the extractor faked to fail document 7 twice (retryable)
and to crash the worker on document 13.

- Every other document gets its `document_aspects` row exactly once.
- Document 7 succeeds on its third claim, each claim at least the ladder
  interval apart.
- Document 13 dead-letters after the attempt cap.
- A second worker started mid-run claims nothing already claimed.
- A worker whose claim is no longer live (a second worker took it over, or its
  lease lapsed) writes nothing: its `document_aspects` write answers 404 and
  changes no row, its `ack` answers 404, and the row holds the second worker's
  extraction.
- A worker that claims a batch of 5 holds five distinct tasks, one claimant
  string each.
- With the old table holding pending rows and the tuple queue holding tasks, both
  drain, and neither is held back by the other.

### Phase 1: Code Implementation

#### Step 1: Engine extensions E1, E3, E4 and E5 (E2 is client-only)

- One changeset for `available_at`.
- The claim predicate arm.
- `out` `delay_s` and `mode=replace`, which restarts the retention clock of a
  consumed or dead row.
- `nack` `retry_after_s`.
- `nack` `dead: true`, the dead-letter on request, through
  `releaseOrDeadLetter` (E4).
- The `dirty` flag and the ack path that honours it (new, `release`-shaped; F23).
- The `aspects` template file as E3 lists it, its entry in the loader's
  classpath list, and the widened doctor depth row (F18).
- `claim_id` and `claimant` on `POST /v1/aspects/upsert`, both or neither (else
  400), looked up with `liveClaimRowQuery` and the row lock in the upsert's
  transaction (E5), and a `TupleException` branch in `AspectHandler`'s error
  ladder so the route answers 404 and 403.
- The tuple forms of the ten sites in Technical Design (rename, move, purge, the
  two re-embed counts and the five registry loops): the hand-written helper, a
  call at each site, and its parity test. The registry gets no entry for tasks.

Each comes with a Java test, plus a spike for A1. The E5 test includes a
re-take that races the check and the write, and a lapsed lease that nobody
re-took.

#### Step 2: Client producer, worker and readers

`HttpAspectQueue` is behind the flag. The worker loop (one claimant string per
claim) and renewer are as in Technical Design. `complete_aspect` takes the
optional claim pair (The persist call), and each reader of the old table gets
its tuple form (Readers of the old table). The worker alternates between the
tuple queue and the old table while the table has pending rows.

#### Step 3: Migration rung

An upgrade-ladder rung moves `pending` and `in_progress` rows to tuples, as
Technical Design describes. `failed` rows stay in the table until they are
re-enqueued.

### Phase 2: Operational Activation

#### Activation Step 1: Default flip

Flip `NX_ASPECT_QUEUE` to `tuples` after the MVV passes on all CI shards and in
one managed-service tenant. Retiring the table is a later changeset.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `aspects` tasks | `tuple_list` (in scope) | `tuple_stats` (in scope) | `replace`, the claim-time drop for a deleted document (no client delete route exists, F22), and the engine's own purge and rename statements for a collection | Doctor row (in scope) | Postgres backup (existing) |
| Dead `aspects` tasks | `nx aspects requeue-failed --dry-run`, extended to read them | the claim log | removed by the sweep at 90 days | Doctor row (in scope) | Postgres backup (existing) |

### New Dependencies

None.

## Test Plan

- **Scenario**: A task is nacked with `retry_after_s=60`. **Verify**: it is not
  claimable at 59 s and is claimable at 60 s (database clock).
- **Scenario**: A worker's lease lapses mid-extraction, and a second worker
  claims the task. **Verify**: the first worker's `document_aspects` write
  answers 404 and changes no row, its `ack` answers 404, and the row holds the
  second worker's result.
- **Scenario**: A worker's lease lapses and nobody takes the task. **Verify**:
  its `document_aspects` write answers 404 and changes no row, its `ack`
  answers 404 (a lapsed claim is refused whether or not anyone re-took it), and
  the task is re-taken after the lapse with an attempt counted.
- **Scenario**: The lease lapses between a successful write and the `ack`.
  **Verify**: the aspect row stands, the `ack` answers 404, and the task is
  re-taken with an attempt counted and re-extracted into the same row.
- **Scenario**: A worker's write names a claim with no claimant, or a claimant
  with no claim. **Verify**: the route answers 400 and writes nothing. A write
  with a claim held by another claimant answers 403, and a claim that is not
  live answers 404, each as an HTTP code and not a 500.
- **Scenario**: One worker claims a batch of 5 with a parked `in` as `W:0` and
  `inp` as `W:1` to `W:4`. **Verify**: the five claims return five distinct
  tasks. A control run with all five calls under one claimant string returns the
  first task five times (F25).
- **Scenario**: A batch of 5 is extracted for longer than the 600 s lease but
  inside the call's deadline. **Verify**: each of the 5 claims is renewed, under
  its own claimant string and by its own `claim_id`, and none is lost. Then hang
  the extraction past the deadline. **Verify**: renewing stops, the leases
  lapse, and the tasks are retried with an attempt counted.
- **Scenario**: A worker `nack`s a claim with `dead: true`. **Verify**: the task
  is dead at once with its attempt counted, the claim log shows `nack` then
  `dead`, it counts under `dead` in `subspace_stats`, and `nx aspects
  requeue-failed` revives it.
- **Scenario**: The worker persists a result with a claim, and a claim-less
  caller persists one. **Verify**: with a claim, the write and the tuple `ack`
  happen and the table is untouched. Without one, the write and the table
  `mark_done` happen and a queued task for that document stays queued.
- **Scenario**: A worker's write names a collection that a rename retired.
  **Verify**: the route answers 409 and writes nothing, the worker releases the
  claim with no attempt counted, and the next claim carries the new name.
- **Scenario**: A task's worker dies on every claim. **Verify**: it reaches
  `dead` after the cap, and `nx aspects requeue-failed` revives it with a
  restarted retention clock.
- **Scenario**: A dead task passes its retention. **Verify**: the sweep removes
  it, and it no longer counts against `max_live_rows`.
- **Scenario**: Re-enqueue during extraction (`replace` on a claimed row).
  **Verify**: the `ack` returns it to available, and the next claim sees the
  new `content_hash`.
- **Scenario**: An MCP-stored note is enqueued. **Verify**: the body is an
  identity body under 4,096 bytes with no document text, and the worker's text
  is the manifest reassembly (A2). Use a multi-piece note with leading and
  trailing whitespace, CRLF and a repeated paragraph, and record how the
  reassembled text differs from the original (F17); a one-piece note matches
  byte for byte.
- **Scenario**: More than 4 collections are active for one worker. **Verify**:
  no park is refused with `ParkCapExceededException` (F21), and the worker holds
  one parked `in`, under its slot-0 claimant string.
- **Scenario**: A catalog document is deleted with a task queued. **Verify**: the
  task is acked as dropped when a worker claims it (no client delete route
  exists, F22).
- **Scenario**: A collection is renamed with one available, one claimed and one
  dead task queued, once through the catalog route (`renameCollectionTxn`) and
  once through the aspect-queue route (`renameQueueCollection`), and again with
  a re-embed move of one document and a whole-collection re-home. **Verify**:
  the next claim of each carries the new collection, a re-enqueue after the
  rename replaces the same task, a rename under a re-embed job is refused as
  today, and the re-home status counts the tasks as remaining and movable.
- **Scenario**: A collection has only a queued task naming it. **Verify**: the
  rename's merge refusal names `aspects_tasks` as the blocking table, and the
  ghost sweep does not delete its registry row.
- **Scenario**: The parity test of the hand-written helper runs on the real
  source, and again with a planted registry loop that has no helper call.
  **Verify**: the real run examines at least ten sites and passes, and the
  planted run reports the planted site.
- **Scenario**: The engine boots with `aspects.yaml` and
  `NX_TUPLE_CLAIM_LOG_TTL_DAYS` at 100, and again at 99. **Verify**: it boots at
  100 and refuses at 99, with a message naming the variable.
- **Scenario**: The old table holds pending and failed rows and `aspects` holds
  available, claimed and dead tasks. **Verify**: `pending_count`, drained,
  `nx doctor`, the console collector and `nx aspects drain` count both sides as the
  tuple-form column of Readers of the old table says, and `nx aspects drain`
  exits 0 only when both are drained.
- **Scenario**: A collection is purged with tasks queued. **Verify**: its
  unconsumed tasks are gone in the purge's own transaction and another
  collection's tasks are untouched.
- **Scenario**: The rung runs over pending, in-progress, failed and
  content-carrying rows. **Verify**: pending and in-progress rows become tasks
  once each, with `next_retry_at` as the delay; content-carrying rows become
  identity bodies; failed rows stay in the table and `nx aspects requeue-failed`
  re-enqueues them; a crash between `out` and the table delete leaves one task.
- **Scenario**: Cross-instance enqueue to a parked worker. **Verify**: claimed
  within 2 s.
- **Scenario**: A bulk index at the measured backlog size. **Verify**: no `out`
  is refused, and no task expires before it is drained.

## Validation

### Testing Strategy

Java tests for E1, E3, E4 and E5 against the real Postgres container (E2 is
client code, tested in Python). Python tests for
the producer and worker against the engine substrate the unit suite already
boots, never against a fake queue. The MVV above is a scenario-journey test.

### Performance Expectations

No speculative targets. The comparison that matters is wake latency: up to
300 s today (F6) against about 1 s (F12a). The claim-scan cost under a backlog
is measured in the A1 spike.

## Finalization Gate

### Contradiction Check

The second pass found several places where the first draft's text disagreed with
the source; each is fixed in the body and listed in the Revision History. The
fix check of `97baf5a3e` found seven more, each fixed in the body: the batch
claim (F25), the lease premise of E5, the engine census, the readers of the old
table, the aspect route's error mapping, the dead-on-request path, and the
template's literal shape (research-30). No other contradiction is recorded in
those entries. One tension is stated rather than hidden: RDR-211
sized the queue template small on purpose. This RDR keeps that sizing by
adding a separate template, not by changing it.

### Assumption Verification

A3, A4a and A4c are verified by source reading. A2 is re-scoped: its contract
is read, and whether the reassembled text suits a multi-piece MCP note is not
settled. A1 (the EXPLAIN spike) and A4b (`park_stats` at a busy hour) are not,
and remain prerequisites, listed above.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `TupleRepository` claim, ack, nack, renew, release | nexus-service | Source Search |
| `AspectRepository.claimNext`, `markRetry` | nexus-service | Source Search |
| `document_text(doc_id)` | nexus-service (SQL) | Source Search (F17): returns rows, no route; true for notes only; not used by the design |
| `POST /v1/aspects/upsert` with `claim_id`, `claimant`, `liveClaimRowQuery` | nexus-service | Source Search (research-28, research-30): one transaction; the lookup has a lease-time term |
| `AspectHandler` error ladder, `TupleHandler` typed-error branch | nexus-service | Source Search (research-30): no typed tuple branch in `AspectHandler` today |
| `claimOnce` same-claimant retake | nexus-service | Source Search (F25) |
| `extract_aspects` empty-content contract, `read_source` | nx | Source Search (F7, research-28) |
| `/v1/tuples` routes, `TupleWaitRegistry` caps | nexus-service | Source Search (F19, F22) |

### Scope Verification

The MVV is in scope for Phase 1 and runs before the default flip.

### Cross-Cutting Concerns

- **Versioning**: Engine first, then a client gated on the engine version. The
  wire contract change is declared in the wire-contract ledger.
- **Collection rename, move and purge**: each of the ten engine sites that name
  the queue, or loop over the registry that lists it, gets a tuple form in its
  own transaction (Technical Design).
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: Local and managed alike, since both run the same engine.
  The per-tenant worker lease is unchanged.
- **IDE compatibility**: N/A
- **Incremental adoption**: The flag, plus a worker that alternates between the
  tuple queue and the old table while the table drains (Migration).
- **Secret/credential lifecycle**: N/A
- **Memory management**: Identity bodies shrink the queue's storage. Today's
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
- 2026-10-26: Gate round 1 — BLOCKED (1 Critical, 6 Significant, 1 ship-blocker(s)); commit `609c8501f` (the develop twin of the gated branch commit, byte-identical RDR); critique `nexus_rdr/235-gate-critique-2026-10-26-r1`.
- 2026-10-27: Gate round 2 — PASSED (0 Critical, 5 Significant, 0 ship-blocker(s)); commit `798dd7b75`; critique `nexus_rdr/235-gate-critique-2026-10-27-r2`.
