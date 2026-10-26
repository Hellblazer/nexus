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

This is a source reading of develop at f87b857c4 (2026-10-26). The full fact
sheet, with every line reference, was produced in the session that drafted
this RDR. The findings below carry the facts the design depends on. Each
measured or configured value is stated once, here.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `AspectRepository` (Java) | Yes | `claimNext` claims under `FOR UPDATE SKIP LOCKED`, with no claim id. The mark calls are unfenced. `reclaimStale` doesn't count. |
| `TupleRepository`, `TupleLimits`, `queue.yaml`, `lock.yaml` | Yes | There is no visibility column. The body cap is 4,096 bytes. The queue limits are 10,000 live rows and 48 h retention, with a 900 s lease ceiling and 3 attempts. Ack, nack, renew and release are compare-and-swaps on `claim_id`. |
| `TupleWaitRegistry` | Yes | Per engine process, with a 1 s re-query and a 25 s cap on `in`. |
| `PipelineRepository` | Yes | `run_epoch` is checked under the row's `FOR UPDATE` in every write route. Ownership comes from heartbeat age. |
| `document_text(doc_id)` stored function | Yes (named in src/nexus/db/AGENTS.md) | It rebuilds a document's text from its manifest. That is the candidate content source for a pointer-only task (Assumption A2). |

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
  (:1224-1234; daemon constant aspect_worker_daemon.py:77-86).
- **F4 (Documented). Backoff ladder.** `markRetry` stamps
  `next_retry_at = now() + interval` on the database clock. The worker's
  interval is `30 × 2^retry_count` s with ±20% jitter, capped at 5 attempts
  (aspect_worker.py:97-99, :260-273), so about 30, 60, 120, 240 and 480 s.
  Re-enqueue clears it (AspectRepository.java:1065-1070).
- **F5 (Documented). Extraction time.** RDR-089's spike measured a median of
  26.5 s and a p95 of 38.1 s per document. Timeouts: 180 s for one document,
  and 180 + 60 per extra document in a batch, which is 420 s at the default
  batch of 5. Each call gets three attempts (aspect_extractor.py:52, :1199-1200,
  :1697).
- **F6 (Documented). Discovery.** A backstop poll every 300 s, plus a
  same-machine wake file checked every 1 s. There is no LISTEN/NOTIFY
  (aspect_worker.py:101-150).
- **F7 (Documented). Body.** On the MCP `store_put` path the row's `content`
  column holds the whole document, and `source_path` is a doc_id, not a file
  (aspect_worker.py:1586-1644). The indexer's batched path enqueues with empty
  content and reads from disk.
- **F8 (Documented). Re-enqueue.** `enqueue` is an upsert that resets the row to
  `pending`, `retry_count` 0 and `next_retry_at` null (AspectRepository.java:1001-1071).
  That includes a row that is `in_progress`.
- **F9 (Documented). Cascade.** A foreign key to `catalog_documents` with
  `ON DELETE CASCADE` deletes queued work with its document
  (fk-001-catalog-cross-store.xml:250-252).

**The tuple queue template**

- **F10 (Documented). No delayed visibility.** The claim predicate is
  `consumed_at IS NULL AND expires_at > now()`, the row not dead, and no live
  lease (TupleRepository.java:1883-1891). No column holds a not-before time.
  `nack` and `release` make the row claimable at once (:2379-2401).
- **F11 (Documented). Limits.** `MAX_BODY_BYTES = 4096`, a fixed constant
  (TupleLimits.java:32). `queue.yaml` sets the following.
  - `max_attempts: 3`. A lapsed lease counts an attempt (:1900-1923).
  - `max_lease_seconds: 900`. A longer lease is refused, not clamped. `renew`
    extends a live claim without counting an attempt.
  - `retention_seconds: 172800` (48 h). A refire cannot extend it (:675-680).
  - `max_live_rows: 10000`, enforced at `out`.
- **F12 (Documented). Identity and refire.** Queue tuple ids are `keys+nonce`.
  A refire with the same id only moves `expires_at`. It never resets claim
  state, never replaces the body and never revives a consumed row
  (TupleRepository.java:740-756). A changed body under a live nonce is
  refused (`NonceReuseConflictException`).
- **F12a (Documented). Fencing and wake.** `ack`, `nack`, `renew` and `release`
  are compare-and-swaps on `claim_id`. A stale one answers 404 and writes
  nothing (:2009-2057, :2455-2460). A parked `in` wakes on a same-process
  signal or a 1 s re-query, so a write on another engine instance is seen
  within about 1 s. Each `in` call is capped at 25 s, with 16 park slots globally
  and 4 per claimant (TupleWaitRegistry.java; TupleRepository.java:100-107).
- **F12b (Documented). Tenancy.** `nexus.tuples` is under forced RLS on
  `nexus.tenant`, like the aspect queue. Tenancy is not a blocker.

**The PDF pipeline row**

- **F13 (Documented). The row carries the run.** It holds the resume counters,
  `extraction_meta`, status and the heartbeat. The page and chunk WAL tables hang
  off it by `pipeline_id`. Here WAL means the write-ahead buffer of extracted
  pages and chunks (pipeline-001, pipeline-002).
- **F14 (Documented). The write fence.** Every write route first runs `lockRun`:
  `SELECT ... FOR UPDATE` on the pipeline row and a compare of the caller's
  `run_epoch`, in the same transaction as the WAL write
  (PipelineRepository.java:460-492). A takeover bumps `run_epoch` in the same
  UPDATE that claims the row.
- **F15 (Documented). Ownership and identity.** `create` takes over a `failed`
  row or one whose heartbeat is older than `STALE_THRESHOLD` (5 min), and
  refuses a fresh `running` row with 409. The row's identity is the document
  `(content_hash, collection, pdf_path)` (pipeline-002), not the content hash.
- **F16 (Verified, measured 2026-10-25 for nexus-lqaq1).** In `extractor=auto`,
  Docling converts the whole document before the first page reaches the buffer.
  A 300-page PDF was silent for 83.7 s on the laptop (0.28 s a page), so a run of
  about 1,070 pages or more is silent past the 5-minute window. The heartbeat
  only moves on progress writes.

### Critical Assumptions

- [ ] **A1**: Adding a nullable `available_at` column and one claim-predicate arm
  (`available_at IS NULL OR available_at <= now()`) keeps the claim's index
  scan. **Status**: Unverified. **Method**: Spike (EXPLAIN on a seeded
  tenant).
- [ ] **A2**: `document_text(doc_id)` returns the same text the MCP
  `store_put` path enqueues today. Then a task can carry a pointer instead of
  the document. **Status**: Unverified. **Method**: Source Search plus a test
  over a stored note.
- [ ] **A3**: An aspect-specific template can have a larger `max_live_rows`,
  a longer retention and its own attempt cap, without changing the sweep or
  the doctor rows for other queues. **Status**: Unverified. **Method**:
  Source Search (template loader, sweep, doctor).
- [ ] **A4**: A parked `in` loop (25 s calls) per tenant worker fits within the
  park-slot caps, alongside the mailbox and ledger consumers. **Status**:
  Unverified. **Method**: Source Search plus a count of live parkers.

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
- a parked `in`, so a cross-machine enqueue is seen in about 1 s.

The move does not happen until the template can carry the queue (Gap 4). The
four extensions are:

- **E1. Delayed visibility.** A nullable `available_at` on `nexus.tuples`, one
  arm in the claim predicate, `out` with `delay_s`, and `nack` with
  `retry_after_s`. Both are stamped on the database clock, as RDR-163 stamps
  `next_retry_at`. The ladder's intervals stay worker-chosen.
- **E2. Pointer-only task body.** The task body carries
  `{collection, doc_id, content_hash, source}`, never document text. The
  worker reads the text from disk when `source` is a file, and from
  `document_text(doc_id)` otherwise (A2). The 4,096-byte cap then never binds,
  and the cap itself stays as it is.
- **E3. An aspect queue template.** A new `aspects/<collection>` template,
  rather than retuning `queue/`, so the board, ledger and ad-hoc queues keep
  RDR-211's sizing. Its own limits are sized from F1 and F5:
  - `max_live_rows` large enough for a bulk index;
  - retention long enough that no backlog expires before a worker reaches it;
  - `max_attempts` equal to the ladder cap plus one.
  The exact numbers come from a measured bulk backlog, which F1 says nobody
  has yet. Measuring it is a prerequisite.
- **E4. Re-enqueue.** An `out` mode `replace` for the document's deterministic
  id (nonce = `collection + doc_id`):
  - a consumed or dead row is reset to available, with the new body and
    attempts at 0;
  - an available row gets the new body;
  - a claimed row is marked `dirty`, so its `ack` returns it to available
    instead of consuming it.

  This keeps today's "the newest content wins" behaviour (F8) without the
  double extraction the current upsert causes on an `in_progress` row.

**Decision 2: the PDF pipeline row does not move, not even its ownership.**
Design Rationale argues this from F13 to F16.

### Technical Design

**Producer side.** `HttpAspectQueue.enqueue` and `enqueue_many` become `out`s
to `aspects/<collection>` with the pointer body and `replace`. The hook sites
(F1) do not change. The FK cascade (F9) has no tuple equivalent. A claim of a
document the catalog no longer has is `ack`ed as dropped, with a log event
(`aspect_task_dropped_missing_doc`), and a catalog purge also issues a best-effort
delete of that document's task.

**Worker side.** The RDR-173 per-tenant daemon keeps its lease. Its loop
becomes:

1. `in` on `aspects/<collection>`, parked for 25 s per call, with a lease of
   600 s.
2. Extract, with a renewer thread that calls `renew` every 120 s.
3. On success, write `document_aspects` and `ack`.
4. On a retryable failure, `nack` with `retry_after_s` from the RDR-163 ladder.
5. On a non-retryable failure, `nack` to dead.
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
| Content source for pointer bodies | `document_text(doc_id)` | Reuse (A2). |
| Wake | `aspect_wake` file, 300 s poll | Replace with a parked `in`. Keep the wake file only while the old table drains. |
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
- Positive: one substrate, and the tuple doctor rows, cover the aspect queue.
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
- **Risk**: Park slots are exhausted by many tenants' workers (A4).
  **Mitigation**: Count them in the spike. Fall back to unparked polling every
  5 s, which is still 60× faster than the backstop.

### Failure Modes

- An enqueue refused by the template (`MaxLiveRowsExceeded`) is loud at the
  producer and logged with the collection. It never drops silently.
- A task whose document vanished is acked as dropped, with
  `aspect_task_dropped_missing_doc`.
- A dead task shows on the tuple doctor row and on `nx enrich` failed listings.
  The requeue path is `replace`.
- Diagnosis: `tuple_list` / `tuple_stats` on `aspects/<collection>` show
  available, claimed and dead counts. The claim log shows each attempt.

## Implementation Plan

### Prerequisites

- [ ] Verify A1 to A4.
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
- The `dirty` flag.
- The `aspects/<collection>` template file.

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
| `aspects/<collection>` tuples | `tuple_list` (in scope) | `tuple_stats` (in scope) | `replace` / purge on catalog delete (in scope) | Doctor row (in scope) | Postgres backup (existing) |

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
  (A2).
- **Scenario**: A catalog document is purged with a task queued. **Verify**: the
  task is gone, or acked as dropped on claim.
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

No contradictions found between the research findings, the design principles
and the proposed solution. One tension is stated rather than hidden: RDR-211
sized the queue template small on purpose. This RDR keeps that sizing by
adding a separate template, not by changing it.

### Assumption Verification

A1 to A4 are unverified. All four are prerequisites, listed above.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `TupleRepository` claim, ack, nack, renew, release | nexus-service | Source Search |
| `AspectRepository.claimNext`, `markRetry` | nexus-service | Source Search |
| `document_text(doc_id)` | nexus-service (SQL) | Source Search pending (A2) |

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
