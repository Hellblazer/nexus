---
title: "Rename-Cascade / Aspect-Worker Coordination: Serialize T2 Collection Rename Against In-Flight Aspect Extraction"
id: RDR-138
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-28
accepted_date:
related_issues: [nexus-u0u8a]
related_rdrs: [RDR-128, RDR-129, RDR-103]
related_tests: [tests/test_collection_rename.py]
implementation_notes: ""
---

# RDR-138: Rename-Cascade / Aspect-Worker Coordination

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

`rename_collection_cascade` (`src/nexus/db/t2/__init__.py:503`) re-homes a
collection's rows across every T2 store inside one transaction on a
dedicated connection. The `aspect_worker` independently mutates
`aspect_extraction_queue` (`claim_next` / `mark_done` / `mark_failed`) on
its own connection and its own transactions. Post-nexus-zir76 both route
through the T2 daemon against the same `memory.db`. There is no
coordination between the two: the daemon's single-writer discipline
serializes individual statements but not the multi-statement cascade
against the worker's claim-then-complete sequence.

A concurrent collection rename plus an in-flight aspect extraction can
therefore interleave such that a queued row is moved by the cascade and
then deleted by a worker `mark_done` (or vice versa), or a
`document_aspects` row is written under the OLD collection name after the
cascade already moved it to NEW. This was surfaced as a recurring
"flake" in `test_collection_rename` and confirmed a real race by the
nexus-u0u8a debugger investigation (T3:
`debug-aspect-queue-rename-cascade-worker-race-canary-2026-05-28`).

### Enumerated gaps to close

#### Gap 1: cascade and worker run as uncoordinated separate transactions

`rename_collection_cascade` and `aspect_queue.claim_next` /
`mark_done` / `mark_failed` are distinct transactions on distinct
connections. Single-writer (RDR-128/129) guarantees one writer at a time
per statement but does not make the cascade atomic with respect to the
worker's claim-then-complete pair. The fix must establish a critical
section so a rename and a queue mutation cannot interleave on the same
collection's rows.

#### Gap 2: queued-row loss / re-pend churn on rename

A worker that `mark_done`s a row the cascade just moved (or moves a row the
worker is mid-claim on) can delete an extraction that should have survived
the rename, or strand an `in_progress` row under the new name until
`reclaim_stale` re-pends it. The fix must guarantee that an in-flight
extraction for a renamed collection either completes against a consistent
collection name or is cleanly re-pended, with no silent loss.

#### Gap 3: document_aspects denorm drift vs T3

If the cascade moves `document_aspects` to NEW while a worker writes a new
`document_aspects` row under OLD (its claimed collection name), the T2
denormalized `collection` column drifts from the T3 chunks (which the
data-plane rename already moved to NEW). The fix must keep the
`document_aspects.collection` value consistent with the T3 collection a
rename produces.

## Context

### Background

Discovered while cutting conexus 5.4.1: `test_collection_rename`'s
`test_both_aspect_tables_updated_by_data_plane` failed intermittently on
CI (PR #997, PR #1006) with the signature `aq_old=0 AND aq_new=0` (the
queued row gone from both old and new). The hypothesis "canary, not
flake" was confirmed by a dedicated debugger run that reproduced the
mechanism at 74-95%.

The test-suite manifestation was closed by Layer 2 (nexus-u0u8a, commit
`4801675b` on develop): an autouse `_reset_aspect_worker_singleton`
fixture confines any spawned worker to its own test, plus a deterministic
assertion replacing an ineffective 5s poll. This RDR is Layer 1: the
product-side coordination fix.

### Technical Environment

- T2 daemon single-writer model (RDR-128, RDR-129).
- `aspect_worker` singleton (`src/nexus/aspect_worker.py`):
  `ensure_worker_started`, `claim_next`, `mark_done`, `mark_failed`,
  `stop_claiming`, `reclaim_stale`.
- Rename data plane: `src/nexus/collection_rename.py:82-95` calls
  `t2db.rename_collection_cascade` via `t2_index_write` (daemon-routed),
  then the T3 native rename.

## Research Findings

### Investigation

Full root cause and reproduction in T3:
`debug-aspect-queue-rename-cascade-worker-race-canary-2026-05-28`
(nexus-u0u8a).

### Key Discoveries

- **Verified** — cascade-alone produces only `(0,1)` for the queue; the
  collision-defense DELETE (`t2/__init__.py:594-602`) matches 0 rows in
  the seeded state. A row gone from BOTH old and new requires an external
  DELETE, which only the worker's `mark_done` supplies.
- **Verified** — a concurrent worker `claim_next`+`mark_done` racing the
  cascade reproduced `(0,0)` 148/200 (74%); the end-to-end leaked-singleton
  path 57/60 (95%) at fast poll, rare at the production 2s poll.
- **Documented** — `document_aspects` never races because nothing else
  writes it; only `aspect_extraction_queue` has a concurrent mutator. This
  asymmetry is the fingerprint.

### Critical Assumptions

- [ ] The cascade and worker mutations are the ONLY two writers that can
  touch a collection's `aspect_extraction_queue` rows concurrently —
  **Status**: Unverified — **Method**: Source Search
- [ ] A coarse per-`memory.db` queue-maintenance lock does not measurably
  regress steady-state worker throughput (claims are short) —
  **Status**: Unverified — **Method**: Spike

## Proposed Solution

### Approach

Establish a single critical section, held inside the T2 daemon, that
serializes `rename_collection_cascade` against `aspect_queue` claim /
mark_done / mark_failed for the duration of a rename. Renames are rare and
short; the worker's claims are short; a coarse lock is acceptable.

Three candidate mechanisms to evaluate during research (lead: A):

- **(A) Daemon-held queue-maintenance lock.** A single mutex inside the
  daemon process guards both the cascade and the queue claim/complete
  paths. The cascade acquires it for its whole transaction; the worker
  acquires it around claim-then-complete. Keeps the coordination where the
  single writer already lives (RDR-129).
- **(B) `stop_claiming()` around the cascade.** `collection_rename.py`
  calls `aspect_worker.stop_claiming()` before the cascade and resumes
  after. Simpler, but only coordinates an in-process worker, not a worker
  in a different daemon-client process, so it is insufficient alone
  post-zir76.
- **(C) Fold queue-maintenance into the cascade RPC.** Make the cascade
  and any pending queue completion for the affected collection a single
  daemon RPC under one transaction. Strongest atomicity; largest change.

### Decision Rationale

(A) respects the RDR-128/129 single-writer daemon model and is the
narrowest change that actually closes the cross-process window. (B) is
attractive but does not cover the daemon-routed multi-process case. (C) is
the most correct but disproportionate to a low-severity, self-healing bug.
Research should confirm (A) is sufficient and bound its throughput cost.

## Trade-offs

### Consequences

- Renames briefly block aspect-queue claims (acceptable: renames are rare,
  claims are short).
- Closes the queued-row-loss and denorm-drift windows.

### Risks and Mitigations

- **Risk**: a coarse lock serializes more than necessary and slows the
  worker under heavy ingest.
  **Mitigation**: scope the lock to the rename critical section only;
  measure claim latency under load (Critical Assumption 2).
- **Risk**: lock not held across the cross-process boundary (in-process
  lock only).
  **Mitigation**: the lock must live in the daemon process that owns the
  single writer, not in client processes.

### Failure Modes

Visible: a rename blocks longer than expected. Silent (today, pre-fix): a
queued extraction is lost or a `document_aspects` row drifts from T3.
Recovery today is `reclaim_stale` re-pending plus a re-index; the fix
removes the need.

## Test Plan

- **Scenario**: concurrent rename + worker claim/mark_done on the same
  collection, looped — **Verify**: the queued row is never lost
  (`aq_new==1` deterministically; never `(0,0)`).
- **Scenario**: rename while a `document_aspects` write is in flight —
  **Verify**: the persisted `collection` matches the post-rename T3
  collection (no OLD/NEW drift).
- **Scenario**: throughput probe — worker claim latency with and without
  an active rename — **Verify**: no material regression in steady state.

## References

- nexus-u0u8a; T3 `debug-aspect-queue-rename-cascade-worker-race-canary-2026-05-28`
- RDR-128 (single-writer enforcement), RDR-129 (daemon write-path hardening)
- `src/nexus/db/t2/__init__.py:503` (rename_collection_cascade)
- `src/nexus/collection_rename.py:82-95`; `src/nexus/aspect_worker.py`

## Revision History

(Gate findings appended here.)
