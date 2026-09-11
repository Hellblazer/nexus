---
title: "Tuple Space Claim Renewal and Reply-in-Ack: Close the Two Limits RDR-205 Accepted for v1"
id: RDR-206
type: Feature
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-11
accepted_date:
related_issues: []
related_rdrs: [RDR-205, RDR-184]
---

# RDR-206: Tuple Space Claim Renewal and Reply-in-Ack

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

RDR-205 shipped the tuple space (a shared collection of small typed records
where a take is exclusive and a read can wait) and accepted two limits for its
first version, naming both as later candidates and designing neither. Both are
now live in every install (engine `engine-service-v0.1.114`, client 7.41.1),
and both are safe today only because no consumer exercises them. This RDR
closes them with two additions to the same primitive: a `renew` operation on a
live claim, and an `ack` that can carry a reply in its own transaction.

### Enumerated gaps to close

#### Gap 1: A claim cannot be extended, so long work loses its message silently

A take (`in`) holds a claim under a lease, and the mailbox template caps the
lease at 900 seconds (`max_lease_seconds` in
`service/src/main/resources/tuples/templates/mailbox.yaml`). RDR-205 §Prior art
records the safety of that cap as "an assumption the record does not argue:
that no v1 consumer holds a mailbox claim across work longer than 900 seconds,
so the absence of a renew operation is safe by scope rather than by
construction." A reader that is still working when its lease lapses does not
learn that it lost the claim. The sweep, or the next reader, retakes the
message, `attempts` increments, and two readers act on one message. After three
lapses the message is dead-lettered while its first reader may still be
working on it. Nothing in the client, the skill, or the doctor rows reports
this. The fix is a `renew` operation: the holder extends its own live claim
before the lease lapses, never past the tuple's expiry, with a claim-log row
per renewal.

#### Gap 2: The reply and the ack are two calls, so a crash between them duplicates work or replies

A responder to a request does `in` (claim the request), does the work, `out`
(write the reply to the requester's mailbox), then `ack` (consume the request).
RDR-205 accepted the window between `in` and the reply `out`: a crash there
redelivers the request at lease lapse, the work repeats, and no reply is lost
because none was sent. The window it did not discuss is between the reply
`out` and the `ack`. A crash there leaves the reply delivered and the request
unconsumed, so the request is redelivered, the work repeats, and the requester
receives a second reply carrying the same correlation id. The fix is an `ack`
that carries an optional reply `out` in the ack's own transaction, so the reply
and the consumption of the request commit together or not at all.

## Relationship to Prior RDRs

Searched the index for: tuple, Linda, claim, lease, renew, ack, reply,
mailbox, coordination, orchestration.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-205 (closed 2026-09-11) | Origin | It built the primitive and named both of this RDR's operations as "candidates for a later version, not scheduled and not designed here" (§Prior art). Its rationale for deferring them was scope: "no v1 consumer holds a mailbox claim across work longer than 900 seconds" and "the window is bounded by the lease and visible in the claim log." Both rationales still hold today and are the reason this RDR is small: the operations are additions, not repairs. Its scope clause ("a consumer not named here needs its own RDR") is why this is an RDR and not a follow-on bead: both changes alter the primitive's contract. |
| RDR-184 (closed) | Precedent | Diagnosed the report-never-arrives and directive-lands-late failures that RDR-205's two consumers fix. The reply-duplication window in Gap 2 is the same class, a delivery that the sender cannot tell apart from a lost one, one level up. |
| RDR-110 (abandoned) | Origin of the vocabulary | Its design kernel (registered schemas, lease plus ack/nack, append-only claim log) is what RDR-205 carried forward and what this RDR extends. It had no renew either. |

## Context

### Background

Both gaps were recorded at RDR-205's gate, accepted as v1 scope, and restated
on the public tuple-space page and in `docs/tuple-space.md` §Prior art, where
JavaSpaces is the comparison: JavaSpaces has lease renewal and cancellation by
the holder, and its take-and-reply runs under one transaction. RDR-205 marked
both rows "accepted for v1; a later candidate." This RDR is that later
candidate, opened on Sam's instruction on 2026-09-11 after the page rebuild
made the two limits the only open items in the tuple space.

### Technical Environment

- Engine: `dev.nexus.service.db.TupleRepository` (claim statement, `ack`,
  `nack`, `releaseOrDeadLetter`, sweep arms), `dev.nexus.service.http.TupleHandler`
  (ten routes under `/v1/tuples`, typed `TupleException` subclasses rendered
  as `{error, detail}` at their own HTTP status), `TupleWaitRegistry`
  (per-subspace park and `signalAll` after commit). jOOQ 3.20, Liquibase-only
  DDL, raw SQL in Java banned by `RawSqlGateTest`.
- Tables: `nexus.tuples` (`claim_state`, `claimant`, `claim_id`,
  `lease_until`, `attempts`, `expires_at`, `consumed_at`, `consumed_by`),
  `nexus.tuple_claim_log` (`transition TEXT NOT NULL`, no CHECK constraint on
  the value: verified by reading `tuples-001-baseline.xml`), `nexus.tuple_tenants`.
- Claim-log transitions today: `claim`, `ack`, `nack`, `expire`, `dead`
  (constants in `TupleRepository`).
- Client: `src/nexus/db/t2/http_tuple_store.py` (`out`, `rd`, `in_`, `ack`,
  `nack`, registry, census), eight `tuple_*` MCP tools in `src/nexus/mcp/core.py`,
  `nx tuple` with eight verbs, the mailbox and orchestration skills.
- Wire ledger: `docs/wire-contract-pending.md`; every both-halves change is an
  `## Unshipped` entry naming the engine tag, with direction-safety prose.
- Engine release cadence: `engine-service-v0.1.115` is the newest tag;
  `REQUIRED_ENGINE_VERSION` is `(0, 1, 114)`.

## Research Findings

### Investigation

Read in full: `TupleRepository.ack`, `nack`, `releaseOrDeadLetter`, the claim
statement's lease clamp, `liveClaimRow`; `TupleHandler.handleAck`/`handleNack`;
`tuples-001-baseline.xml` for the claim-log column definitions; the mailbox
template; `HttpTupleStore.ack`/`nack`; the mailbox skill's drain and
dead-letter rules; RDR-205 §Prior art and §Alternatives; `docs/tuple-space.md`
§Claims, leases, nack and dead letter.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `TupleRepository` (engine) | Yes | `ack` resolves the live claim row by `claim_id`, checks `claimant`, sets `consumed_at`/`consumed_by`, logs `ack`; one `withTenant` transaction. `out` validates against the template and calls `waitRegistry.signalAll` after commit. Both are reusable inside one transaction. |
| `tuples-001-baseline.xml` | Yes | `tuple_claim_log.transition` is `TEXT NOT NULL` with no CHECK; a new transition value needs no changeset. |
| `mailbox.yaml` | Yes | `max_lease_seconds: 900`, `max_attempts: 3`, `retention_seconds: 604800`. A renew must respect the first and the row's `expires_at`. |
| `HttpTupleStore` | Yes | `ack(claim_id, claimant)` posts `/ack`; adding optional fields is a body extension, not a new route. |

### Key Discoveries

- **Documented**: the lease clamp at claim time (`lease_until` never after
  `expires_at`) is the rule a renew must repeat. A renew that extends past
  the tuple's expiry would let a claim outlive its tuple, which RDR-205 forbids.
- **Documented**: `liveClaimRow` requires `claim_state = 'claimed'` and
  `lease_until > now()`. A renew on a lapsed claim must fail with
  `ClaimNotFound`, exactly as a late `ack` does today, so a holder that missed
  its window learns it lost the claim instead of extending a claim it no longer
  holds.
- **Documented**: `out` is idempotent by construction (id from caller fields).
  A reply carried inside `ack` therefore has a stable identity, and a retried
  `ack` after a lost response lands on the same reply tuple; the second `ack`
  itself fails `ClaimNotFound` because the first consumed the request, which
  is the existing contract for a repeated `ack`.
- **Documented**: the waiter signal fires after the transaction commits. A
  reply written inside the ack transaction must signal the requester's mailbox
  waiters after that commit, from the same place `out` does today.
- **Assumed**: no v1 consumer needs a lease longer than the template cap even
  with renewal; renewal changes who decides when work is long, not the cap.

### Critical Assumptions

- [ ] A renew inside the holder's live lease is the only renew that succeeds;
  a lapsed claim is not resurrected — **Status**: Verified by reading
  `liveClaimRow` — **Method**: Source Search
- [ ] Writing a reply tuple and consuming the request in one
  `withTenant` transaction is expressible with the existing `out` and `ack`
  bodies — **Status**: Unverified until the spike in Phase 1 Step 1 —
  **Method**: Spike
- [ ] Adding optional fields to the `/ack` body and one new `/renew` route
  is `[additive]` in both directions (old client ignores them, new client
  against an old engine gets 404 on `/renew` and a plain ack on `/ack`) —
  **Status**: Verified by reading `TupleHandler`'s route switch and
  `_raise_typed` — **Method**: Source Search

## Proposed Solution

### Approach

Add two operations to the primitive and nothing else.

1. `renew(claim_id, claimant, lease_s)`: extend a live claim held by this
   claimant. New `lease_until` is `now + lease_s`, capped at the template's
   `max_lease_seconds` from now and clamped to the row's `expires_at`. Writes
   one claim-log row with transition `renew`. Errors: `ClaimNotFound` (no live
   claim with that id, including a lapsed one), `ClaimOwnership` (held by
   another claimant), `LeaseTooLong` (above the template cap), `SchemaViolation`
   (`lease_s` at or below zero).
2. `ack(claim_id, claimant, reply=None)`: as today, plus an optional reply
   object `{subspace, keys, dims, body, nonce}` that the engine writes with the
   same validation as `out`, in the same transaction that consumes the claim.
   On any validation failure of the reply, nothing is written and the request
   stays claimed, so the responder can correct and retry. Waiters on the reply's
   subspace are signalled after commit.

Both consumers stay as they are. The mailbox skill gains two rules: renew
before the lease lapses when work is long, and reply through `ack` when a
request needs an answer. The orchestration skill is unchanged.

### Technical Design

Engine (`TupleRepository`):

```text
// Illustrative — verify API signatures during implementation
public OffsetDateTime renew(String tenant, String claimId, String claimant, long leaseSeconds)
    // withTenant: liveClaimRow -> ownership check -> LeaseTooLong check against
    // template.take().maxLeaseSeconds() -> leaseUntil = min(now + leaseSeconds,
    // expires_at), truncated to micros -> update LEASE_UNTIL -> insertClaimLog(renew)
    // -> return leaseUntil

public byte[] ackWithReply(String tenant, String claimId, String claimant, ReplySpec replyOrNull)
    // withTenant: the existing ack body, then, if replyOrNull != null, the existing
    // out body against the reply's subspace/template in the SAME ctx; the reply's
    // waiters are signalled after commit exactly as out does. Returns the reply id
    // or null.
```

Routes (`TupleHandler`): `POST /v1/tuples/renew` with body
`{claim_id, claimant, lease_s}` returning `{"lease_until": ...}`;
`POST /v1/tuples/ack` accepts an optional `reply` object and returns
`{"acked": true, "reply_id": <hex or null>}`. Method and body validation
follow the existing handlers. The new transition string `renew` joins the
constants; no DDL.

Client (`HttpTupleStore`): `renew(claim_id, claimant, lease_s) -> datetime`;
`ack(claim_id, claimant, reply: ReplySpec | None = None) -> str | None`.
MCP: `tuple_renew`; `tuple_ack` gains an optional `reply` argument. CLI:
`nx tuple renew`; `nx tuple ack --reply-subspace/--reply-key/...` or a JSON
`--reply` argument, whichever the existing verb conventions favour.

Skills: `conexus/skills/mailbox/SKILL.md` gains the renew rule (renew at half
the lease when a task is still running) and the reply-in-ack rule (a request
that needs an answer is answered through `ack`, never by a separate `out`
followed by `ack`). The page and `docs/tuple-space.md` §Prior art drop the two
"accepted for v1" rows.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `renew` | `TupleRepository.ack`/`nack` (claim resolution, ownership check) | Extend: same `liveClaimRow` path, new update and log row |
| reply-in-ack | `TupleRepository.out` and `.ack` | Extend: compose the two bodies in one transaction; no new validation code |
| `/renew` route | `TupleHandler` route switch | Extend: one case |
| client/MCP/CLI | `HttpTupleStore`, `tuple_*` tools, `nx tuple` | Extend: one method, one tool, one verb, one optional argument |

### Decision Rationale

Two operations, both composed from code that already exists in one
transaction each, with no schema change. The alternative of leaving both
limits in place holds only while no consumer runs long or needs a reply, and
the failure when that stops being true is silent in both cases (a lost claim,
a duplicate reply), which is the class of failure RDR-184 and RDR-205 exist to
end.

## Alternatives Considered

### Alternative 1: Raise `max_lease_seconds` instead of adding renew

**Description**: Set the mailbox cap high enough that no task outlives it.

**Pros**: No new operation.

**Cons**: A crashed reader holds its message for the whole cap, so redelivery
after a crash slows to the cap; a long cap and fast crash recovery are in
direct conflict. Renew keeps the cap short and lets a live holder prove it is
live.

**Reason for rejection**: It trades a silent failure for a slow one.

### Alternative 2: Automatic renewal by the client while a call is in flight

**Description**: The MCP tool or CLI renews on the holder's behalf on a timer.

**Pros**: No skill rule to remember.

**Cons**: The holder is Claude, mid-turn, with no background thread the MCP
server can safely drive; a timer in the MCP process renews claims the agent
may have abandoned.

**Reason for rejection**: Renewal must be a deliberate act by the holder.

### Briefly Rejected

- **Reply as a field on the request tuple**: turns a mailbox into a mutable
  record and breaks "a tuple is a signal, not data".
- **Two-phase commit across `out` and `ack` from the client**: RDR-205's
  JavaSpaces table already rejected 2PC; one engine transaction is the whole
  point.

## Trade-offs

### Consequences

- One new route, one extended route, one new transition string, no DDL.
- Two new skill rules; the mailbox convention becomes slightly longer.
- The claim log gains `renew` rows, which the census and any audit must treat
  as non-terminal.

### Risks and Mitigations

- **Risk**: a holder renews forever and a message never reaches anyone else.
  **Mitigation**: renew never extends past the tuple's `expires_at`; the
  message still expires at retention, and the doctor's oldest-unclaimed row is
  unaffected because a renewed claim is claimed, not unclaimed. A renew
  count per claim is visible in the log.
- **Risk**: the reply's validation failure leaves the request claimed and the
  responder confused.
  **Mitigation**: the error names the reply field, nothing is written, and the
  claim is still live to retry; documented in the skill.
- **Risk**: a retried `ack` with reply after a lost response.
  **Mitigation**: the reply is idempotent by id; the retried `ack` fails
  `ClaimNotFound` as any repeated `ack` does, and the responder reads that as
  "already consumed".

### Failure Modes

- Visible: `renew` on a lapsed claim fails `ClaimNotFound`; the holder learns
  it lost the claim.
- Visible: `LeaseTooLong` on a renew above the cap.
- Visible: reply validation failure on `ack` returns `SchemaViolation` naming
  the field, request still claimed.
- Silent, resolved: a crash after `ack` with reply commits leaves nothing
  pending on either side.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (the transaction-composition spike is
  Phase 1 Step 1).

### Minimum Viable Validation

Engine-direct, on a dev jar: a claimant takes a mailbox message with a 10 s
lease, renews at 5 s to 10 s more, and acks at 12 s; the claim log shows
`claim`, `renew`, `ack` and no `expire`. Then: a request in mailbox A is taken
by B, B acks with a reply into A, a reader parked on A wakes with the reply
before B's ack call returns to B, and A's request row is consumed. Both
sequences are one test each.

### Phase 1: Engine

#### Step 1: Spike the composed transaction

Write `ackWithReply` as the existing `ack` body followed by the existing `out`
body in one `withTenant`, with the signal after commit. Pin: reply visible and
request consumed in the same read, or neither.

#### Step 2: `renew`

Repository method, handler route, typed errors, `renew` transition, tests for
clamp to `expires_at`, cap by template, lapsed-claim refusal, ownership.

#### Step 3: Sweep and census unaffected

Pin that the release arm ignores renewed live claims and that
`subspace_stats` counts a renewed claim under `claimed`.

### Phase 2: Client

`HttpTupleStore.renew`, `ack(reply=)`, `tuple_renew`, the `tuple_ack`
argument, the two `nx tuple` additions, doctor unchanged, mailbox skill rules,
page and reference updates. Wire-ledger `## Unshipped` entry, `[additive]`,
naming the engine tag from Phase 3.

### Phase 3: Engine release and pairing

Cut on the engine's own cadence (the tag is Sam's decision). The client
release that bumps `REQUIRED_ENGINE_VERSION` moves the ledger entry to
`## Shipped`. All-additive, so the engine deploys before the client tag.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `renew` claim-log rows | In scope (existing log read) | In scope | Existing log purge | Existing census | Existing PG backup |

### New Dependencies

None.

## Test Plan

- **Scenario**: renew within lease — **Verify**: `lease_until` moves forward,
  one `renew` log row, claim still held.
- **Scenario**: renew after lapse — **Verify**: `ClaimNotFound`, row available
  or retaken, no log row.
- **Scenario**: renew by another claimant — **Verify**: `ClaimOwnership`.
- **Scenario**: renew past `expires_at` — **Verify**: clamped, never after
  expiry.
- **Scenario**: renew above template cap — **Verify**: `LeaseTooLong`.
- **Scenario**: ack with valid reply — **Verify**: request consumed and reply
  present in one read; waiter on the reply subspace wakes.
- **Scenario**: ack with invalid reply — **Verify**: `SchemaViolation`, request
  still claimed, no reply row.
- **Scenario**: ack with reply retried after lost response — **Verify**: second
  call `ClaimNotFound`, exactly one reply row.
- **Scenario**: old client against new engine and new client against old engine
  — **Verify**: plain ack unchanged; `/renew` 404 surfaces as a loud typed error.

## Validation

### Testing Strategy

1. **Scenario**: the MVV's two sequences on a dev jar.
   **Expected**: both pass engine-direct and through `HttpTupleStore`.
2. **Scenario**: the mailbox skill's cross-instance walkthrough, re-run with
   reply-in-ack.
   **Expected**: one reply per request under a forced crash between the old
   `out` and `ack` positions.

### Performance Expectations

Renew is one indexed update; ack-with-reply is the existing ack plus the
existing out. No new measurement is needed beyond the existing sweep budget
check.

## Finalization Gate

### Contradiction Check

To be completed at the gate.

### Assumption Verification

To be completed at the gate.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `TupleRepository.ack`, `.out` composed | engine | Spike (Phase 1 Step 1) |
| `TupleHandler` route addition | engine | Source Search |
| `HttpTupleStore._post` | client | Source Search |

### Scope Verification

The MVV is Phase 1 Step 1 and Step 2's tests; not deferred.

### Cross-Cutting Concerns

- **Versioning**: additive wire change, paired through the ledger.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: engine tag then client release, all-additive.
- **IDE compatibility**: N/A
- **Incremental adoption**: old consumers unchanged.
- **Secret/credential lifecycle**: N/A
- **Memory management**: N/A

### Proportionality

Two operations, one RDR. Trim at the gate if any section restates RDR-205.

## References

- RDR-205 §Prior art (JavaSpaces comparison) and §Alternatives.
- `docs/tuple-space.md` §Claims, leases, nack and dead letter.
- `service/src/main/java/dev/nexus/service/db/TupleRepository.java`,
  `service/src/main/java/dev/nexus/service/http/TupleHandler.java`,
  `service/src/main/resources/db/changelog/tuples-001-baseline.xml`,
  `service/src/main/resources/tuples/templates/mailbox.yaml`.
- `src/nexus/db/t2/http_tuple_store.py`, `conexus/skills/mailbox/SKILL.md`.

## Revision History

### 2026-09-11 — Created

Drafted on Sam's instruction after the tuple-space page rebuild left these
two limits as the only open items. Not yet researched beyond source reading;
the transaction-composition spike is the first implementation step.
