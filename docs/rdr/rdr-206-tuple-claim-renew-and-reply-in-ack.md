---
title: "Tuple Space Claim Renewal and Reply-in-Ack: Close the Two Limits RDR-205 Accepted for v1"
id: RDR-206
type: Feature
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-11
accepted_date: 2026-09-11
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
- Engine release cadence: `engine-service-v0.1.115` is the newest tag and
  `REQUIRED_ENGINE_VERSION` is `(0, 1, 115)`; the tag this RDR pairs with is the
  next one cut after Phase 1 closes.

## Research Findings

### Investigation

Three research passes on 2026-09-11, recorded in T2 under
`nexus_rdr/206-research-1` (engine), `-2` (client, MCP, CLI, skills, docs,
release plumbing), and `-3` (prior art: JavaSpaces, Que, Solid Queue, Oban,
pgmq, Graphile Worker, SQS). Every finding below cites its record. Before
the passes, the draft was written from a direct reading of
`TupleRepository.ack`, `nack`, `releaseOrDeadLetter`, the claim statement's
lease clamp, `liveClaimRow`, `TupleHandler`, `tuples-001-baseline.xml`, the
mailbox template, `HttpTupleStore`, the mailbox skill, and RDR-205 §Prior art.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `TupleRepository` (engine) | Yes | `ack` resolves the live claim row by `claim_id`, checks `claimant`, sets `consumed_at`/`consumed_by`, logs `ack`; one `withTenant` transaction. `out` validates against the template and calls `waitRegistry.signalAll` after commit. Both are reusable inside one transaction. |
| `tuples-001-baseline.xml` | Yes | `tuple_claim_log.transition` is `TEXT NOT NULL` with no CHECK; a new transition value needs no changeset. |
| `mailbox.yaml` | Yes | `max_lease_seconds: 900`, `max_attempts: 3`, `retention_seconds: 604800`. A renew must respect the first and the row's `expires_at`. |
| `HttpTupleStore` | Yes | `ack(claim_id, claimant)` posts `/ack`; adding optional fields is a body extension, not a new route. All four errors renew raises already exist as client classes; `_raise_typed` re-raises an unknown route's 404 as a bare `httpx.HTTPStatusError` (research-2 §1). |
| `TenantScope.withTenant` | Yes | Opens its own connection and commits per call; no overload takes a caller's `DSLContext`, so `out`'s body must be factored into a `DSLContext`-parameterised helper before `ack` can compose it (research-1 §1). |
| `TupleHandler` | Yes | Body helpers take any `Map<String,Object>`, so a nested `reply` object parses with the same code `handleOut` uses; `/renew` is one switch case; all nine typed errors and statuses already cover both operations (research-1 §3). |
| Engine and client tests | Yes | All engine tuple tests run on Testcontainers Postgres; client tests use `t2_service_env`; the MCP tool names are pinned in `tests/test_mcp_package.py` (two lists) and `tests/test_mcp_tuple_tools.py` (research-1 §4, research-2 §2, §6). |
| JavaSpaces, SQS, pgmq, Que, Solid Queue, Oban, Graphile | Yes (specs and source) | Renew is relative-duration and fails on an expired lease in JavaSpaces (`UnknownLeaseException`) and SQS (`MessageNotInflight`); pgmq's `set_vt` is unconditional and resurrects; every system with renewal caps it at an absolute deadline (research-3). |

### Key Discoveries

- **Documented**: the lease clamp at claim time (`lease_until` never after
  `expires_at`) is the rule a renew must repeat. A renew that extends past
  the tuple's expiry would let a claim outlive its tuple, which RDR-205 forbids.
- **Documented**: `liveClaimRow` requires `claim_state = 'claimed'` and
  `lease_until > now()`. A renew on a lapsed claim must fail with
  `ClaimNotFound`, exactly as a late `ack` does today, so a holder that missed
  its window learns it lost the claim instead of extending a claim it no longer
  holds.
- **Documented** (research-4): a retried `ack` with reply after a lost
  response fails at the ack step, before the reply write runs, because
  `liveClaimRow` returns nothing once `consumed_at` is set. The ack-first
  ordering is the mechanism that yields exactly one reply row. A rolled-back
  transaction cannot show the order by itself, so Phase 1 Step 2 pins the
  order by a unit test on the composed method's call sequence, and pins the
  atomicity separately. The reply's identity is
  made stable as well, so a future reordering could not break it: the engine
  sets the reply's nonce to the request's tuple id in hex. `computeId` digests the template
  keys, the `id_dims` (`from` for the mailbox), and the nonce, so one request
  can produce one reply row per responder however many times the write runs,
  and two replies to two requests never collide.
- **Documented**: the waiter signal fires after the transaction commits. A
  reply written inside the ack transaction must signal the requester's mailbox
  waiters after that commit, from the same place `out` does today.
- **Documented** (research-1 §1): `withTenant` cannot nest and takes no
  caller context. `out`'s body touches only `ctx`, so a private
  `writeOut(DSLContext, ...)` helper called by both `out` and `ackWithReply`
  is a mechanical refactor. The reply's waiters are signalled after the
  composed `withTenant` returns, as `out` does at line 280, and only when a
  reply was written. RLS is a plain tenant equality, there are no triggers,
  and the only unique index is the primary key, so nothing objects to the
  request update and the reply insert sharing a transaction.
- **Documented** (research-1 §2): the claim-time clamp is a pure function of
  `(now, lease_s, expires_at)` and factors into a shared static helper. The
  sweep's release arm and the claim statement's lapsed branch both compare
  the live `lease_until` column against `now()`, so a renewed row stops
  matching them with no other change.
- **Documented** (research-1 §3, research-2 §1, research-4): no new typed error
  is needed on either side. `renew` raises `ClaimNotFound`, `ClaimOwnership`,
  `LeaseTooLong`, `SchemaViolation`. A reply inside `ack` runs the same
  validation as `out` and can raise everything `out` can: `UnknownSubspace`
  when the reply subspace does not resolve, `TtlTooLong` when its
  `ttl_seconds` exceeds the reply template's retention, and `SchemaViolation`
  for a bad key, dimension, nonce, or non-positive TTL.
  `tests/test_tuple_error_table_pin.py` is a floor of nine and stays green.
- **Documented** (research-2 §2, §3): three test files pin the MCP tool names
  and must change together; the comment block above the tools says "Eight
  MCP tools". The CLI convention is repeatable `KEY=VALUE` flags parsed by
  `_parse_kv_pairs`, and no JSON-blob input exists, which decides the
  `nx tuple ack --reply-*` shape.
- **Documented** (research-2 §1): the RDR's reply object omitted
  `ttl_seconds`, which `out` accepts. The reply forwards it as optional, so a
  reply can carry a shorter life than its template's retention.
- **Documented** (research-3): JavaSpaces renews by relative duration and
  throws `UnknownLeaseException` on an expired lease; SQS returns
  `MessageNotInflight`; pgmq's `set_vt` has no ownership or visibility guard
  and is the one surveyed renew that resurrects a lapsed claim. Every
  surveyed system with renewal bounds it at an absolute deadline. This RDR's
  design already matches the two systems that fail loud and avoids pgmq's
  gap by construction, through `liveClaimRow`.
- **Assumed**: no v1 consumer needs a lease longer than the template cap even
  with renewal; renewal changes who decides when work is long, not the cap.
- **Assumed** (research-2 §5): no renew-specific first-engine-version
  constant is needed beside `_TUPLE_ROUTE_FIRST_ENGINE_VERSION`, because
  renew extends a shipped route family and an old engine's 404 already
  fails loud. Revisit only if a doctor row targets renew.

### Critical Assumptions

- [ ] A renew inside the holder's live lease is the only renew that succeeds;
  a lapsed claim is not resurrected — **Status**: Verified by reading
  `liveClaimRow` — **Method**: Source Search
- [ ] Writing a reply tuple and consuming the request in one
  `withTenant` transaction is expressible with the existing `out` and `ack`
  bodies — **Status**: Documented by reading (research-1 §1: the bodies
  compose once `out`'s is factored onto a caller `DSLContext`); execution
  not yet run, which is Phase 1 Step 2 — **Method**: Source Search, then Spike
- [ ] The compare-and-swap conditions on `ack`, `nack`, and `renew` change
  no successful path, only the stale-update race — **Status**: Documented by
  reading the update statements and the sweep's locked select (research-4);
  the race tests in Phase 1 Steps 1 and 3 execute it — **Method**: Source Search,
  then Spike
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
   object `{subspace, keys, dims, body, ttl_seconds}` (the fields `out`
   accepts, minus `nonce`) that the engine writes with the same validation as
   `out`, in the same transaction that consumes the claim. The engine sets the
   reply's `nonce` itself to the request's tuple id in hex; no client, tool,
   or CLI flag carries a reply nonce, and a `nonce` key in the reply object is
   a `SchemaViolation`.
   On any validation failure of the reply (`UnknownSubspace`, `TtlTooLong`,
   or `SchemaViolation`, the same three `out` raises), nothing is written and
   the request stays claimed, so the responder can correct and retry. Waiters on the reply's
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
    // expires_at), truncated to micros -> update LEASE_UNTIL WHERE id = ? AND
    // claim_state = 'claimed' AND claim_id = ? AND consumed_at IS NULL -> if the update
    // touched zero rows, throw ClaimNotFound (the sweep or a concurrent release won)
    // -> insertClaimLog(renew) -> return leaseUntil

private byte[] writeOut(DSLContext ctx, String tenant, String subspace, ..., String nonce, Long ttlSeconds)
    // out's existing body (validateOut, computeId, upsert, maintainTenant) moved onto a
    // caller-supplied ctx; out itself becomes withTenant(tenant, ctx -> writeOut(ctx, ...))
    // followed by signalAll, unchanged in behaviour (research-1 §1).

private TuplesRecord consumeClaim(DSLContext ctx, String tenant, String claimId, String claimant)
    // ack's existing body (liveClaimRow, ownership check, the compare-and-swap update of
    // Step 1, the ack log row) moved onto a caller-supplied ctx and returning the consumed
    // row; ack itself becomes withTenant(tenant, ctx -> consumeClaim(ctx, ...)). One body,
    // so ackWithReply cannot ship without the compare-and-swap.

public byte[] ackWithReply(String tenant, String claimId, String claimant, ReplySpec replyOrNull)
    // withTenant: consumeClaim FIRST (a consumed or foreign claim fails here and nothing
    // else runs), then, if replyOrNull != null, writeOut(ctx, ...) against the reply's
    // subspace/template in the SAME ctx, with nonce = hex(consumed row's id), set here and
    // never taken from the caller. After withTenant returns, signalAll(tenant, replySubspace)
    // only if a reply was written. Returns the reply id or null.
```

Routes (`TupleHandler`): `POST /v1/tuples/renew` with body
`{claim_id, claimant, lease_s}` returning `{"lease_until": ...}`;
`POST /v1/tuples/ack` accepts an optional `reply` object and returns
`{"acked": true, "reply_id": <hex or null>}`. Method and body validation
follow the existing handlers. The new transition string `renew` joins the
constants; no DDL.

Client (`HttpTupleStore`): `renew(claim_id, claimant, lease_s) -> datetime`;
`ack(claim_id, claimant, reply: ReplySpec | None = None) -> str | None`.
MCP: `tuple_renew`; `tuple_ack` gains an optional `reply` argument; the
three name pins and the "Eight MCP tools" comment change together. CLI:
`nx tuple renew --claim-id --claimant --lease-s`; `nx tuple ack` gains
`--reply-subspace`, repeatable `--reply-key KEY=VALUE` and
`--reply-dim KEY=VALUE`, `--reply-body`, and `--reply-ttl-seconds`, parsed
by the existing `_parse_kv_pairs`; there is no reply nonce flag, because the
engine sets it. `ReplySpec` on the client carries the same five fields. No JSON
blob: the CLI has no JSON input today and this keeps parity with `out`
(research-2 §3). `docs/cli-reference.md` § `nx tuple` gains the verb and the
flags.

Skills: `conexus/skills/mailbox/SKILL.md` gains the renew rule (renew at half
the lease when a task is still running) and the reply-in-ack rule (a request
that needs an answer is answered through `ack`, never by a separate `out`
followed by `ack`), and its cross-instance line that acks "by `tuple_out`
back to your address" migrates to `tuple_ack(reply=...)`. The mailbox skill
is plugin surface and needs a `conexus/PENDING_RELEASE.md` entry; the
orchestration skill is unchanged (research-2 §4). Docs: `docs/tuple-space.md`
gains the `renew` signature and row, the `ack` reply field, and the error
notes, and its §Prior art drops the two "accepted for v1" rows; the
walkthroughs' cross-instance sequence and the site page's appendix
paragraph on the two limits change with it.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `renew` | `TupleRepository.ack`/`nack` (claim resolution, ownership check) | Extend: same `liveClaimRow` path, new update and log row; the update is a compare-and-swap on `claim_state` and `claim_id` with the row count checked |
| compare-and-swap on `ack` and `nack` | `TupleRepository.ack`, `releaseOrDeadLetter` (update by id only, research-4) | Extend: add the same `claim_state`/`claim_id`/`consumed_at` conditions and row-count check, so a stale ack or nack fails `ClaimNotFound` instead of writing over a row the sweep released or another claimant now holds |
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
- `ack` and `nack` gain a compare-and-swap condition; a stale ack or nack that
  today silently writes over a released row now fails `ClaimNotFound`.
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
  **Mitigation**: the ack step runs first and fails `ClaimNotFound` on a
  consumed claim, so the reply write is never reached on a retry; the
  responder reads that as "already consumed". The reply's nonce is the
  request's tuple id, so even a write that did run would land on the same
  row.

### Failure Modes

- Visible: `renew` on a lapsed claim fails `ClaimNotFound`; the holder learns
  it lost the claim.
- Visible: `LeaseTooLong` on a renew above the cap.
- Visible: reply validation failure on `ack` returns `UnknownSubspace`,
  `TtlTooLong`, or `SchemaViolation` naming the field, request still claimed.
- Silent, resolved: a crash after `ack` with reply commits leaves nothing
  pending on either side.
- Visible: a `renew`, `ack`, or `nack` that loses the race with the sweep's
  release fails `ClaimNotFound` and writes nothing; before this RDR, `ack` and
  `nack` wrote over the released row.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (the transaction-composition spike is
  Phase 1 Step 2).

### Minimum Viable Validation

Engine-direct, on a dev jar: a claimant takes a mailbox message with a 10 s
lease, renews at 5 s to 10 s more, and acks at 12 s; the claim log shows
`claim`, `renew`, `ack` and no `expire`. Then: a request in mailbox A is taken
by B, B acks with a reply into A, a reader parked on A wakes with the reply
before B's ack call returns to B, and A's request row is consumed. Both
sequences are one test each.

### Phase 1: Engine

The steps run in dependency order. The compare-and-swap comes first because
`consumeClaim` (Step 2) is defined as containing it and `renew` (Step 3) uses
it; the earlier ordering, with the compare-and-swap third, was cyclic.

#### Step 1: Compare-and-swap on the shipped claim updates

`liveClaimRow` reads without a lock and `ack` and `nack` update by id alone,
while the sweep's release arm selects the same rows under
`FOR NO KEY UPDATE SKIP LOCKED`. Change both updates to
`WHERE id = ? AND claim_state = 'claimed' AND claim_id = ? AND consumed_at IS NULL`,
check the affected-row count, raise `ClaimNotFound` on zero rows on the caller
paths, and write the claim-log row only after a one-row update.
`releaseOrDeadLetter` is shared by `nack` and the sweep's release arm
(research-4), so the sweep's call gains the same condition; under its row lock
the condition is always true, and on zero rows the sweep records the row in
its run log and continues with no claim-log row, never raises, because one
exception would abort its batch transaction. Pins:
a test that releases the row between the read and the update and asserts
`ClaimNotFound` and no log row, through a new ack-side test seam shaped like
the existing `claimOnce` one; and the sweep's counts unchanged. This changes
shipped `ack`/`nack` behaviour in exactly that race.

#### Step 2: Factor `out` and `ack` onto a caller context, then compose

Move `out`'s body into `writeOut(DSLContext, ...)` and `ack`'s body, with
Step 1's compare-and-swap, into `consumeClaim(DSLContext, ...)`, with `out`
and `ack` unchanged in behaviour (their existing tests pin that). Then write
`ackWithReply` as `consumeClaim` followed by `writeOut` in one `withTenant`,
the reply nonce set by the engine to the consumed row's id, signalling the
reply subspace after the call returns. Pins: reply visible and request
consumed in the same read, or neither; a reader parked on the reply subspace
wakes after the commit; a reply whose validation fails leaves the request
claimed with no reply row (atomicity); and the call order, `consumeClaim`
before `writeOut`, by a unit test on the composed method, since a rolled-back
transaction cannot show the order by itself. This executes Critical
Assumption 2.

#### Step 3: `renew`

Repository method with the compare-and-swap of Step 1, handler route, typed
errors, `renew` transition, the lease clamp extracted from `claimOnce` into
one shared helper, `attempts` untouched. Tests for clamp to `expires_at`, cap
by template, lapsed-claim refusal, ownership, and the race with the sweep's
release between the read and the update.

#### Step 4: Sweep and census unaffected

Pin that the release arm ignores renewed live claims, that `subspace_stats`
counts a renewed claim under `claimed`, and that a renew does not change
`attempts`.

#### Step 5: Minimum Viable Validation, engine-direct

Run both MVV sequences on a dev jar before Phase 1 closes: the renew sequence
(claim, renew, ack, no `expire` in the log) and the ack-with-reply sequence (a
parked reader wakes with the reply and the request is consumed). The client
half of the MVV runs again in Phase 2 through `HttpTupleStore`.

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
- **Scenario**: renew, ack, or nack whose claim the sweep released between the
  read and the update — **Verify**: zero rows updated, `ClaimNotFound`, no
  log row, the released row untouched.
- **Scenario**: renew by another claimant — **Verify**: `ClaimOwnership`.
- **Scenario**: renew past `expires_at` — **Verify**: clamped, never after
  expiry.
- **Scenario**: renew above template cap — **Verify**: `LeaseTooLong`.
- **Scenario**: ack with valid reply — **Verify**: request consumed and reply
  present in one read; waiter on the reply subspace wakes.
- **Scenario**: ack with invalid reply — **Verify**: `SchemaViolation`, request
  still claimed, no reply row.
- **Scenario**: ack with a reply whose `ttl_seconds` exceeds the reply
  template's retention — **Verify**: `TtlTooLong`, request still claimed, no
  reply row.
- **Scenario**: ack with a reply to an unregistered subspace — **Verify**:
  `UnknownSubspace`, request still claimed, no reply row.
- **Scenario**: ack with reply retried after lost response — **Verify**: second
  call `ClaimNotFound`, exactly one reply row.
- **Scenario**: ack with a reply that fails validation — **Verify**: the request
  is still claimed and no reply row exists, which pins that the claim and the
  reply roll back together (atomicity, not order).
- **Scenario**: the composed method's call sequence — **Verify**: `consumeClaim`
  runs before `writeOut`, by a unit test on `ackWithReply` in the repository's
  own package, since a rolled-back transaction cannot show the order.
- **Scenario**: ack with a reply object that carries a `nonce` key — **Verify**:
  `SchemaViolation`, request still claimed.
- **Scenario**: old client against new engine and new client against old engine
  — **Verify**: plain ack unchanged; `/renew` 404 surfaces as a bare
  `httpx.HTTPStatusError`, never a silent no-op.
- **Scenario**: `out` after the refactor — **Verify**: every existing
  `TupleRepositoryTest` case for `out` passes unchanged.
- **Scenario**: sweep with a renewed live claim — **Verify**: the release arm
  does not touch it; `subspace_stats` counts it under `claimed`.
- **Scenario**: MCP tool census — **Verify**: the two name lists in
  `tests/test_mcp_package.py` and `tests/test_mcp_tuple_tools.py` include
  `tuple_renew` and the module comment names nine tools.

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
| `TupleRepository.ack`, `.out` composed | engine | Spike (Phase 1 Step 2) |
| `TupleHandler` route addition | engine | Source Search |
| `HttpTupleStore._post` | client | Source Search |

### Scope Verification

The MVV is Phase 1 Step 5, engine-direct, before the phase closes, and its client half repeats in Phase 2; not deferred.

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

- T2 `nexus_rdr/206-research-1`, `-2`, `-3` (2026-09-11), the three research
  records this section cites.
- Jini Lease Specification §LE.2.2, §LE.2.3 and the JavaSpaces Specification
  (river.apache.org/release-doc/current/specs/html/lease-spec.html, js-spec.html).
- Amazon SQS API reference, ChangeMessageVisibility (MessageNotInflight, 12 h cap).
- pgmq `set_vt` source (github.com/pgmq/pgmq); Que README; Solid Queue README
  and process-failure notes; Oban job lifecycle docs; Graphile Worker admin functions.
- Gray and Cheriton, "Leases: an efficient fault-tolerant mechanism for
  distributed file cache consistency", SOSP 1989.
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

### 2026-09-11 — Research pass (three parallel records, T2 `nexus_rdr/206-research-1` to `-3`)

Engine, client, and prior-art passes. Design changes from the findings:
`out`'s body is factored onto a caller `DSLContext` before composition
(`withTenant` cannot nest); the reply object carries `ttl_seconds`; the CLI
reply shape is repeatable `--reply-*` flags, not a JSON blob; the mailbox
skill's cross-instance ack line migrates to `tuple_ack(reply=...)`; the MCP
name pins in three test files are named in the plan. Prior art confirms
relative-duration renew that fails on a lapsed claim (JavaSpaces, SQS) and
records pgmq's unconditional `set_vt` as the resurrection anti-pattern this
design avoids. Critical Assumption 2 moves from Unverified to Documented,
with execution still owed to Phase 1 Step 2.

- 2026-09-11: Gate round 1 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); commit `c10889d15`; critique `nexus_rdr/206-gate-critique-2026-09-11`.
- 2026-09-11: Gate round 2 — PASSED (0 Critical, 4 Significant, 0 ship-blocker(s)); commit `325e6cced`; critique `nexus_rdr/206-gate-critique-2026-09-11b`.
- 2026-09-11: Post-accept amendment — Phase 1 re-derived in dependency order (compare-and-swap first, then factor and compose, then renew, then pins, then the engine-direct MVV); the earlier order was cyclic. Step references, Scope Verification, and the engine pin updated. Fix check on this change recorded in T2 as `nexus_rdr/206-fix-check-<tip>`, where `<tip>` is the RDR file's commit after this amendment.

