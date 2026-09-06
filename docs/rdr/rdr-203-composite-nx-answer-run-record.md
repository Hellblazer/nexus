---
title: "One Composite Run Record: Collapse nx_answer's Three Telemetry Writes into a Single Engine Operation"
id: RDR-203
type: Architecture
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-05
accepted_date: 2026-09-06
related_issues: [nexus-m20mf, RDR-193, RDR-196, RDR-198]
---

# RDR-203: One Composite Run Record

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Recording one `nx_answer` run takes three HTTP writes from the client to the
engine, spread across two T2 domain stores and two moments in the call.
Server side it is one transaction over three tables that the client currently
composes by hand.

The three writes, read on develop on 2026-09-05, at a tip carrying
`a69a714aa` (nexus-m20mf P3):

| # | When | Client call | Route | Tables touched |
|---|---|---|---|---|
| 1 | after the plan match, before execution (`src/nexus/mcp/core.py:8674`) | `db.plans.increment_run_started(plan_id)` | `POST /v1/plans/metrics/run_start` | `nexus.plans` |
| 2 | at the terminating arm | `_nx_answer_record_run(db.telemetry, ...)` | `POST /v1/telemetry/nx_answer_runs/record` | `nexus.nx_answer_runs`, `nexus.nx_answer_steps` |
| 3 | at the same terminating arm | `db.plans.increment_run_outcome(plan_id, success=...)` | `POST /v1/plans/metrics/run_outcome` | `nexus.plans` |

Write 2 already carries the per-step rows inline, in its own `steps[]` field
(RDR-196 `.p1c`), so there is no separate steps write to collapse. The
`nx_answer_plan_choice` record is a structlog event plus a field on the
structured envelope (`core.py:8646`, `core.py:8216`) and never reaches T2 at
all. An earlier framing of this work counted those two as separate writes.
They are not, and the count of record-path writes to collapse is three.

Line numbers here and below drift. Every one was read on that same tip; treat
them as pointers to be re-resolved, not as fixed addresses.

### Enumerated gaps to close

Three gaps, in descending order of how well the evidence supports them, each
labelled with what is actually known about it. The first is structural and
visible in the code. The second is a real mechanism that fires rarely: one
orphan in 190 recorded uses, 0.5%. The third is a real mechanism with no
occurrence rate at all, because nobody has measured how often a plan clears the
promotion gate on abandoned attempts. Rare and unmeasured are different states
and the gaps say which they are in.

#### Gap 1: One operation costs three round trips

**Verified** against develop, and visible in the table above. Recording one run
issues three POSTs to the same engine, two of which carry a single counter
increment each. Each pays its own request, its own auth header build, and on
the engine its own `TenantScope.withTenant` transaction, which means its own
`set_config('nexus.tenant', ..., true)` GUC stamp and its own commit.

What the fix delivers: one POST, one transaction, one GUC stamp. What it does
not deliver is a latency win worth quoting; see the claim section below and
the Context section for the numbers.

#### Gap 2: The client composes an operation the engine cannot make atomic

**Mechanism verified, occurrence rate measured at 1 in 190.** The three writes are
three separate `withTenant` transactions, so no arrangement of client code can
make them one. Between write 1 and write 3 the client can die, the host can
restart, or the network can drop, and the plan row keeps a `use_count`
increment with neither a success nor a failure recorded against it. Nothing
reconciles that afterwards.

RDR-198's research pass spiked this and found zero orphaned records: five plans
with `use_count > 0`, 29 runs, `use_count == success_count + failure_count`
everywhere. The pre-acceptance re-measure of 2026-09-05 went wider and found
one: 30 plans, 16 with `use_count > 0`, 190 recorded uses against 189 outcomes,
the single orphan on plan 365 with `use_count` 11, 10 successes and no
failures. That is 0.5% of runs. So the gap is real and rare rather than
unobserved, and what the record cannot say is whether that one was an abandoned
call, a crashed process, or a lost outcome POST. The finding is
`nexus_rdr/203-research-1`.

What the fix delivers: the three writes land in one transaction or none of them
does, so the window closes by construction rather than by luck.

#### Gap 3: `use_count` counts attempts and cannot be reconciled

**Verified by reading, unmeasured in the field.** Write 1 fires before
execution, so `use_count` counts plan matches that began running, while
`success_count + failure_count` counts runs that finished. The two are
different populations and nothing checks them against each other.
`plans/promote.py` reads `use_count` as its first gate,
`use_count >= DEFAULT_MIN_USE_COUNT` where the default is 3
(`src/nexus/plans/promote.py:47` and `:88-92`), so a plan that starts often and
finishes rarely can clear that gate on abandoned attempts.

What the fix delivers: `use_count` becomes a count of recorded runs, which
makes `use_count == success_count + failure_count` an invariant a reader can
check. D4 takes that change deliberately and states what it costs.

### Why this is worth an RDR and what the claim is not

This is a wire-contract change across two languages on a hot path, which is
the shape the bead itself named as needing an RDR rather than a bead body.

The claim is round-trip count and where the operation lives. Three writes
become one, and one logical operation stops being client-composed. The
supporting property is that the engine can then make it atomic, which it
cannot today no matter how the client is written.

The claim is explicitly not latency. Bead `nexus-m20mf`'s own correction of
2026-08-23 retracted the "90% of steady-state cost" headline: it was measured
against a mocked-I/O harness whose whole call is 0.405s, against a real
`nx_answer` p50 of 80 seconds. The remaining client-side fan-out measured
about 60ms on that 80-second call, 0.07%. Two of the three round trips this
RDR removes are small POSTs against the same host. Anyone justifying this work
on latency is quoting a number that was withdrawn.

The claim is also not a fix for a common defect. RDR-198's research pass spiked
the orphan window and found nothing: five plans, 29 runs, no orphan. The
pre-acceptance re-measure went wider and found one in 190 uses, 0.5%. So the
atomicity gap does occur, rarely, and this document carries the measured rate
rather than an argument in place of one. One orphan in 190 is not a reason to
do this work on its own; it is a real integrity gap that closing the round-trip
count happens to close as well.

## Relationship to prior RDRs

**RDR-198 (closed) withdrew exactly this scope and named its successor.** That
RDR covered the client transport only: fourteen `httpx` clients resolving to
one host, collapsed onto one pooled transport. Its "Withdrawn scope" section
removed operation atomicity from its own remit with the ruling that the
mechanism is real, the observed rate is zero, and it "belongs in its own RDR,
argued on its own evidence, sequenced against RDR-193". This is that RDR.

**RDR-193 (draft) is the same move for a different tier.** It puts index-time
catalog reconciliation and the taxonomy discover pipeline on the engine as
transactional SQL and Java jobs. It establishes the pattern of moving a
client-composed multi-step operation into one engine transaction, and it does
not touch telemetry or the plan library. RDR-203 is the small, single-operation
member of that family.

**RDR-196 built the surface this extends.** `POST
/v1/telemetry/nx_answer_runs/record` gained its optional `steps[]` field and
its `/version` capability probe there, and its degradation contract (probe says
unsupported, write the parent row only, log the drop) is the contract this RDR
reuses verbatim for a new route rather than inventing a second shape.

**Bead nexus-m20mf phases P1 to P3 already shipped the client-side prerequisite.**
Per-call counts went from five `T2Database` facades to one and from 40 `httpx`
clients to eight (`74cb0de5b`, `23dd5fab5`, `adeb4ba57`), and the shared-client
injection landed after that (`a69a714aa` and its follow-ons). The consequence
that matters here: the telemetry store is now process-lifetime under the
refcounted singleton, so the `/version` probe fires once per process instead of
once per call. Adding a second capability probe was expensive before that
landed and is free after it. The sequencing was deliberate.

## Context: what the three writes cost and what they cannot guarantee

**Round trips.** Three POSTs per recorded run, two of them a single counter
increment each. Each carries its own request, its own auth header build, and on
the engine its own `TenantScope.withTenant` transaction, which means its own
`set_config('nexus.tenant', ..., true)` GUC stamp and its own commit.

**Atomicity.** `TenantScope.withTenant` is one PG transaction per call
(`autoCommit=false`, commit on lambda return, rollback on throw). Three calls
are three transactions. Between write 1 and write 3 the client can die, the
host can restart, or the network can drop, and the plan row keeps a `use_count`
increment with neither a success nor a failure recorded against it. Nothing
reconciles that. The measured rate is one orphan in 190 recorded uses across 16
plans (`nexus_rdr/203-research-1`, 2026-09-05), against RDR-198's earlier zero
in 29.

**Failure independence, which cuts both ways.** Today a 429 or a transport
error costs one of the three writes and the other two still land, producing a
partial record. Under one composite POST the same error costs the whole record.
All three writes are best-effort telemetry wrapped in boundary catches at every
call site, so neither shape can break a user-facing answer. Partial telemetry
is the worse of the two outcomes, which is the same reasoning
`recordNxAnswerRun` already applies to its parent and child rows.

## Research Findings

Both pre-acceptance items from the Open research section were investigated on
2026-09-05 and recorded in T2 under `nexus_rdr`. One came back with a number
that changed a claim in this document, which is what the research step is for.

### Key Discoveries

- **✅ Verified** (spike) — The orphan window does fire, rarely. A read of the
  live plan library found 30 plans, 16 with `use_count > 0`, 190 recorded uses
  against 189 outcomes: exactly one orphan, plan 365, `use_count` 11 with 10
  successes and no failures. That is 0.5% of runs, against RDR-198's earlier
  zero in 29. What this does not say is why: the record cannot distinguish an
  abandoned call from a crashed process from a lost outcome POST. Consequence
  for this RDR: the justification stays round-trip count and operation
  placement, with the atomicity gap attached as a measured, small base rate
  rather than as an unobserved hypothesis or an urgent defect.
  *Source: `nx plan list --json` plus `nx plan show --json` per used plan,
  read-only, 2026-09-05; recorded as `nexus_rdr/203-research-1`.*

- **✅ Verified** (source search plus an existing test) — The before half of the
  round-trip count. One `nx_answer` with a plan hit issues three run-record
  writes on three routes, `POST /v1/plans/metrics/run_start`,
  `POST /v1/telemetry/nx_answer_runs/record` and
  `POST /v1/plans/metrics/run_outcome`, plus one `GET /version` capability
  probe per process. The instrument already exists:
  `tests/test_nx_answer_t2_fanout_budget.py` wraps `httpx.Client.send` and
  records every outbound `(method, path)` for one call on the test substrate,
  and it already pins the post-nexus-m20mf transport budget at one
  `T2Database` construction, at most eight `httpx.Client` constructions, and at
  most one `/version` probe per process. What it does not do is assert any
  per-path count, so the before number is read from the code and from that
  harness rather than from an assertion.
  *Source: `tests/test_nx_answer_t2_fanout_budget.py`, and the run_start /
  record / run_outcome call sites in `src/nexus/mcp/core.py`; recorded as
  `nexus_rdr/203-research-2`.*

- **✅ Verified** (instrumented measurement) — The after half of the
  round-trip count: one `POST /v1/telemetry/nx_answer_runs/complete` per
  converting call against a supporting engine, and three POSTs for a D6
  survivor or a 404-downgrade call. Measured, not read, at P3
  (nexus-dt2tu.3), 2026-09-06:
  `tests/test_nx_answer_t2_fanout_budget.py::test_supporting_engine_issues_exactly_one_run_record_post`
  and `::test_non_supporting_engine_issues_three_run_record_posts`, both
  green against the self-provisioned engine substrate, each wrapping
  `httpx.Client.send` to count real outbound requests for one `nx_answer`
  call. Supporting engine: exactly 1 POST across all four run-record
  routes, landing on `/complete`, zero to the three routes it replaces.
  Non-supporting engine (same substrate, `HttpTelemetryStore.
  _supports_nx_answer_run_complete` monkeypatched False, no other change):
  exactly 3 POSTs, one each to `run_start`, `record`, `run_outcome`, zero
  to `/complete`. 3 POSTs down to 1 per converting call — the RDR's central
  claim, counted rather than assumed. The D6-survivor and 404-downgrade
  three-POST cases are pinned separately (unit-level) by
  `tests/test_nx_answer_run_complete.py`'s
  `TestFourOhFourDowngradeGuard`/`TestHandoffArmUseCountInvariant`.
  *Source: `tests/test_nx_answer_t2_fanout_budget.py`; recorded as the
  second half of `nexus_rdr/203-research-2`.*

## Decisions

### D1. One new route, `POST /v1/telemetry/nx_answer_runs/complete`

Body is the existing `/record` body plus one new required field:

```json
{
  "question": "...",
  "plan_id": 42,
  "matched_confidence": 0.71,
  "step_count": 4,
  "final_text": "...",
  "cost_usd": 0.0123,
  "duration_ms": 81422,
  "created_at": "2026-09-05T18:22:31.481920+00:00",
  "steps": [ ... ],
  "outcome": "success"
}
```

`created_at` is **required on `/complete`**, and missing, blank or unparsable
is a 400 naming the field. It is the dedup key
(`tenant_id, question, created_at`) that makes a retried composite idempotent,
so a payload without it defeats D3 and the retry guard in Risks: the engine
would stamp `now()` on each attempt, the dedup index would never match, and a
lost response would double-apply the plan counters. That is the failure the
Risks section calls the sharpest edge in the design, and neither of the two
tests pinning it would catch a missing field, because both assume the field is
present and ask only whether it is stable.

So the field gets the same treatment `outcome` gets on the same route, for the
same stated reason: no silent fallback for a correctness-bearing field. This is
a `/complete`-only rule. `handleNxAnswerRunComplete` is its own handler method
and enforces presence itself; `/record` keeps its lenient handling (absent
means stamp `now()`) untouched, because the ETL path, the D6 survivors and
`nx_answer_report` all rely on it. An earlier draft called the field
load-bearing and then left it defaultable, which is a contradiction rather than
a trade-off.

Named in the engine's contract rather than left to the client half, because P2
and P3 are different developers: P2 makes the route refuse a payload without
it, and P3 makes the client always send it.

`outcome` is a closed vocabulary of two values, `"success"` and `"failure"`.
A missing or unrecognized value is a 400 naming the field, never a default.
That follows the rule the same handler already applies to a step's `ok` field
and to `step_index`: no silent fallback for a correctness-bearing field. A
string rather than a boolean because the value is a closed vocabulary and reads
as one on the wire, and because `success: false` on a request body invites
being read as "the request failed".

`plan_id` stays nullable, as on `/record`. Null or zero means there is no
library row to count against, and the engine writes the run row and its steps
and touches no counters.

The route lives under `/v1/telemetry` and is served by `TelemetryHandler`
because the operation is a telemetry record whose plan-counter updates are part
of the same record. It does not get a new handler or a new context registration
in `NexusService`.

### D2. Transaction boundary: one `withTenant` lambda, all three tables

The engine executes the whole composite inside one
`tenantScope.withTenant(tenant, ctx -> { ... })`. That gives one connection,
one `set_config` GUC stamp, one transaction, commit on return, rollback on any
throw. Inside it, in order:

1. Insert the parent `nx_answer_runs` row, keeping the existing
   `onConflictDoNothing()` against the ETL dedup index
   `(tenant_id, question, created_at)`, returning the id.
2. If the returned id is non-null, insert the `nx_answer_steps` children.
3. If the returned id is non-null and `plan_id` is non-null and greater than
   zero, apply both plan-counter updates: `use_count + 1` and `last_used =
   now()`, then `success_count + 1` or `failure_count + 1`.

Any failure at any step rolls back all of it. A step row that violates the
`nx_answer_steps_source_chk` CHECK takes the run row and both counter updates
with it, which is the outcome `recordNxAnswerRun` already chose for the parent
and children, extended to the counters.

### D3. Dedup skip means the whole composite is skipped

When the parent insert conflict-skips, the returning id is null, the children
are already skipped today, and the counters are skipped too. A dedup hit means
this run is already recorded; bumping counters again would double-count it.
On the live write path `created_at` is `now()` at microsecond resolution, so a
real collision is close to impossible, and this rule exists so the behaviour is
decided rather than emergent.

### D4. `use_count` changes meaning, from attempts to recorded runs

Today write 1 fires before execution, so `use_count` counts plan matches that
began executing. Under the composite it fires at the terminating arm, so it
counts runs that recorded a terminal result. A run killed mid-flight stops
being counted.

This is a real semantic change and it is taken deliberately:

- What is gained: `use_count == success_count + failure_count` becomes an
  invariant per plan rather than a property that happens to hold, and it is
  checkable. It holds on every path, not only the composite one: the D6
  survivors and the 404 fallback issue a deferred `run_start` of their own
  (D5), so no path records an outcome without having counted its use.
- What is lost: the count of abandoned runs. That signal was never readable
  anyway, because an abandoned run writes no `nx_answer_runs` row either, so
  the only trace it left was a counter nobody could reconcile against anything.
- What tightens: `plans/promote.py` reads `use_count` as its first gate,
  `use_count >= DEFAULT_MIN_USE_COUNT` where the default is 3
  (`src/nexus/plans/promote.py:47` and `:88-92`). Under the new invariant
  `use_count` equals `success_count + failure_count`, so that gate becomes a
  restatement of the total-completions check the second gate already implies.
  The direction is a tightening, and it closes a real gap: today a plan that
  begins often and finishes rarely can pad `use_count` with abandoned attempts
  and clear the first gate on one or two real completions. After the change it
  cannot. Nothing about the success-rate gate itself changes.
- What skews: `last_used` moves later by the run duration, up to about 80
  seconds at p50. Nothing reads it at that resolution.

An earlier draft of this section asserted that `promote.py` does not read
`use_count`. That was false, and it was false in the direction that made the
change look free. The corrected reading is above.

The change is documented in `docs/cli-reference.md` wherever `use_count` and
`nx answer-runs` are explained, and in `plans/promote.py`'s own module
docstring, whose "three actual runs" gloss on the `use_count >= 3` gate goes
stale on cutover. Both belong to the phase that ships the client half.

**Rejected alternative: collapse only writes 2 and 3, leave write 1 where it
is.** That preserves attempt semantics and still merges the run row with its
outcome, but it leaves two POSTs instead of one and, more importantly, it does
not close the orphan window at all: the window is opened by write 1 firing
early, so a design that keeps write 1 early keeps the window. Half the
round-trip saving for none of the integrity property.

### D5. Capability probe and the degradation contract

`GET /version` gains `"nx_answer_run_complete_supported": true`, a compile-time
constant beside the existing `"nx_answer_steps_supported": true` in
`VersionHandler`.

Client side, `HttpTelemetryStore` reads both flags from one cached `/version`
body per store instance. Today `_supports_nx_answer_steps` issues its own GET
and caches a bare bool; that becomes a read from a single cached capabilities
dict, so adding a second flag adds no second round trip and the existing budget
assertion of at most one `/version` probe per process still holds.

Degradation, when the probe reports no support or fails for any reason:

- The run-start site issues `increment_run_started` exactly as today.
- The terminating arm issues `_nx_answer_record_run` and then
  `increment_run_outcome`, in that order, with today's independent best-effort
  catches.

That is byte-for-byte today's behaviour, which is what makes the degradation
path cheap to test: it is the current code path, reached through one branch.

A probe that fails to reach `/version` reads as unsupported. The direction is
safe: a new client against an old engine records everything it records today.

**The capability decision is made once per call, and carried.** The run-start
site reads the probe exactly once and records two booleans in the call's own
state. The read and both assignments sit immediately **before** the
`if best.plan_id:` guard at `core.py:8669`; only the `increment_run_started`
call stays inside it. Putting the read inside the guard, which an earlier draft
of this section implied by naming the site without naming the guard, would
leave `composite_supported_at_start` at its entry default of `False` on every
plan-miss call, so a `plan_id == 0` run would never take the composite no
matter what the engine supports. That contradicts D1, which accepts a null or
zero plan id on `/complete`, and P2's `zeroPlanIdWritesRunRowAndNoCounters`,
which exists because that payload is expected to arrive. The two booleans are:

- `composite_supported_at_start`, the probe's answer at that moment.
- `early_bump_fired`, true when the early site actually issued
  `increment_run_started`, which happens when the probe said no support and the
  plan id is usable. A plan-miss call leaves it `False` for the second reason
  even on a non-supporting engine, which is correct: there is no plan row to
  count against, so nothing downstream should bump one either.

**Both booleans are initialised to `False` at `nx_answer`'s entry**, not at the
run-start site, and the run-start site assigns rather than introduces them.
This is not style. The planner-failure arm at `core.py:8570` executes upstream
of the run-start site, so on that path the run-start site never runs, and a
name first bound there would be unbound when the arm reads it. That arm's write
sits inside a `try: with _t2_ctx() as db: ... except Exception: pass`
(`core.py:8568-8577`), which would swallow the resulting `UnboundLocalError`
and silently stop recording planner-failure runs. Nothing would raise, nothing
would log, and the rows would simply stop appearing. Entry initialisation is
what keeps that arm reading a defined record.

Every terminating arm, and `_nx_answer_ensure_run_started` itself, keys on that
record. Never on `plan_id` alone, and never on a fresh read of the shared
cached flag. Two rules follow:

1. If `early_bump_fired`, no arm bumps again.
2. If not `early_bump_fired`, exactly one of the composite route or the
   deferred helper bumps, and the arm decides which from the per-call record.

**`run_start` is deferred, never dropped, and never doubled.**
`_nx_answer_ensure_run_started(db, plan_id, early_bump_fired)` issues
`increment_run_started` immediately before any direct `_nx_answer_record_run`
write, and no-ops when the plan id is null or zero **or** when
`early_bump_fired` is already true. Both halves of that condition are load
bearing. A `plan_id`-only no-op double-counts on the case round 2 found:
non-supporting engine, D6 survivor, where the early site already bumped and the
survivor's helper would bump again for one outcome.

**Rule 1 is enforced by routing, not by a wire field.** The composite is taken
only when `composite_supported_at_start` is true, and `early_bump_fired` can
only be true when it is false, so the pair (early bump fired, composite route)
is unreachable by construction. The alternative, a `count_use: false` field on
the composite telling the engine to skip the increment, was rejected: it puts
one client's private call history into the wire contract, makes the engine's
behaviour depend on a claim it cannot verify, and doubles the engine's tested
surface for a combination that cannot occur. The engine route stays stateless,
always incrementing for a usable plan id, and the client's job is to route
correctly rather than to instruct.

**Exactly one bump per call, over four cases plus the mid-call flip.** A call
reaches exactly one terminating arm.

| engine | arm | early bump | who bumps | total |
|---|---|---|---|---|
| supporting | converting | not fired | the composite, server side | 1 |
| supporting | D6 survivor | not fired | the deferred helper | 1 |
| non-supporting | converting | fired at the early site | nobody again; the helper no-ops on `early_bump_fired` | 1 |
| non-supporting | D6 survivor | fired at the early site | nobody again; same no-op | 1 |

The mid-call flip is the fifth case and it is why the record exists. A sibling
call's 404 flips the shared cached flag between this call's run-start read and
its terminating arm. This call routes from its own record, so it still takes
the composite, that POST 404s against the same old engine, and the fallback
below completes it: `early_bump_fired` is false, so the deferred helper bumps
once. Round 2 found the version of this that reads the flag live instead: the
arm goes down the plain path after the early site already skipped, and the bump
is lost. A flip during a call changes future calls, never this one.

**Downgrade guard.** The probe caches for the life of a process, and after
nexus-m20mf P2 and P3 that is the life of the MCP host. If a supporting engine
is replaced by a non-supporting one under a running client, the cached `true`
would post to a route that 404s and the entire run record would be lost,
including the outcome, which is worse than today. The client therefore treats a
404 from `/complete` as a probe correction: it flips the cached flag to false
**for future calls**, logs it once, and then completes the tripping call from
that call's own record, issuing `_nx_answer_ensure_run_started` (which bumps,
because `early_bump_fired` is false on any call that reached the composite),
then the record, then the outcome, in that order. Three POSTs for that call,
one composite POST for every call after it in the process. "Falls back to the
three-call path for that call" is then literally true and testable, which an
earlier draft asserted without making it so: it flipped the flag and left the
tripping call's own record on the floor. A 404 is the only status treated this
way; a 429 or a 5xx is a transport failure and keeps today's drop-and-warn
handling.

### D6. What stays on the existing route

Three writers keep `POST /v1/telemetry/nx_answer_runs/record` and are out of
scope:

- The planner-failure arm (`core.py:8570`), which records a run with
  `plan_id=None` and has no outcome to report. It calls
  `_nx_answer_ensure_run_started` like every other direct writer, where the
  call is a structural no-op because there is no plan id. It is written that
  way so the census rule below has no exception to carve out.
- The RDR-200 continuation handoff arm (`core.py:9378` for the row,
  `core.py:9387` for its outcome). This one is a genuine
  `(record, outcome)` pair and could convert on shape alone. It is excluded on
  contract: RDR-200 R2 fixes the ordering (the handoff row is written before
  the envelope is ever returned), and the handoff row plus the later
  `nx_answer_report` row are one paired construct that `nx answer-runs` joins
  at read time on the `continuation_id` embedded in both markers. RDR-203 does
  not reopen that contract. The arm keeps all three of today's writes in
  today's order: `_nx_answer_ensure_run_started`, the record, the outcome.
  Under a supporting engine the first of those is the deferred `run_start` from
  D5, since the early site skipped it.
- `nx_answer_report`, which appends a report event, not a run.

This exclusion is the rule the rest of the document is counted against. The
converting set is the **ten** remaining `(record, outcome)` pairs, not eleven.
An earlier draft listed the handoff pair among the converting arms while also
listing it here; the exclusion wins, and the counts below reflect it.

The deferral is what keeps the exclusion cheap. A survivor that skipped
`run_start` and then recorded an outcome would drive its plan's `use_count`
below `success_count + failure_count`, breaking D4's invariant in the
direction that matters: `promote.py`'s `use_count >= 3` gate would become
unclearable for a plan whose runs mostly end in a continuation handoff. With
the deferred call the invariant holds on every path, and a handoff-heavy plan
promotes on the same evidence as any other.

The old routes are not deprecated and not removed. They serve those three
writers, they serve the ETL import path, and they are the degradation target.

## Technical design

### Engine half

**`VersionHandler`**: one line beside the existing flag, same constant shape.

**`TelemetryHandler`**: one `case "/nx_answer_runs/complete"` in the existing
switch, and one `handleNxAnswerRunComplete` that reuses the existing body
parsing and `parseNxAnswerSteps`, adds the closed-set `outcome` read, and calls
the repository.

**`TelemetryRepository`**: today's `recordNxAnswerRun` lambda body is lifted
into a private `insertRunAndSteps(DSLContext ctx, ...)` that returns the run
id, so the existing method and the new composite share one copy of the insert
DSL. The new `recordNxAnswerRunComplete(...)` opens one `withTenant`, calls
`insertRunAndSteps`, and when it gets an id and a usable plan id, calls the two
plan helpers below.

**`PlanRepository`**: the bodies of `incrementRunStarted` and
`incrementRunOutcome` are lifted into package-private **static**
`incrementRunStartedIn(DSLContext ctx, long id)` and
`incrementRunOutcomeIn(DSLContext ctx, long id, boolean success)`. The two
public methods become `withTenant` wrappers over them and keep their exact
current behaviour. `TelemetryRepository` is in the same package
(`dev.nexus.service.db`) and calls the helpers directly, so the counter DSL
exists once and both entry points execute the same statements.

Static, not instance, and that is a constraint rather than a preference:
`TelemetryRepository`'s constructor takes only a `TenantScope`
(`TelemetryRepository.java:57`), and `NexusService` builds the two repositories
independently (`NexusService.java:334-335`). An instance helper would mean
handing `TelemetryRepository` a `PlanRepository`, which is a wiring change in
`NexusService` for no gain: the helpers take their `DSLContext` as an argument
and hold no state of their own.

**No SQL strings anywhere.** Every statement is generated jOOQ DSL over the
generated `NX_ANSWER_RUNS`, `NX_ANSWER_STEPS` and `PLANS` tables, which is what
lifting the existing bodies rather than rewriting them guarantees.
`RawSqlGateTest` gains no sanctioned region. If an implementation finds itself
needing one, that is the signal the design is wrong, not a reason to add an
allowlist entry.

**No Liquibase changeset, because there is no DDL.** The composite writes three
existing tables and adds no column, index, constraint or table. If a later
revision needs schema, it goes through Liquibase like everything else.

**RLS.** One `withTenant` stamps `nexus.tenant` once for all three tables'
policies, where today three calls take three stamps. The composite reduces GUC
work; it does not change the tenant contract.

### Client half

**`HttpTelemetryStore`**

- `_version_capabilities()`: one cached `/version` body per store instance,
  never raising, an unreachable engine reading as an empty capability set.
- `_supports_nx_answer_steps()`: unchanged contract, now a read of that dict.
- `_supports_nx_answer_run_complete()`: the new flag, same shape.
- `record_nx_answer_run_complete(*, question, plan_id, matched_confidence,
  step_count, final_text, cost_usd, duration_ms, created_at, steps, success)`:
  one POST to `/complete`. It is the wire call only. It does not branch on the
  probe, because the fallback needs the plans store as well and that decision
  belongs at the call site. A 404 propagates to the caller rather than being
  swallowed here, since the caller is what turns it into a fallback.

**`src/nexus/mcp/core.py`**

One new choke point, `_nx_answer_record_complete(db, *, question, plan_id,
matched_confidence, step_count, final_text, step_records, duration_ms, trace,
success)`, replacing each `(_nx_answer_record_run, _nx_answer_record_outcome)`
pair. Ten arms convert, listed as `(record site, outcome site)`:
`(8362, 8373)`, `(8890, 8898)`, `(8930, 8938)`, `(9015, 9027)`,
`(9041, 9049)`, `(9110, 9120)`, `(9217, 9226)`, `(9438, 9447)`,
`(9572, 9551)` where the outcome is recorded first, and `(9709, 9591)` where
the success outcome is recorded before Step 6 writes the row. Each calls the
choke point once, inside one `_t2_index_write` closure, matching the
closure-purity rule P2 established.

Two of the twelve `_nx_answer_record_run` call sites do not convert, per D6:
`8570`, the planner-failure arm, which records `plan_id=None` and has no
outcome; and `9378` with its outcome at `9387`, the RDR-200 continuation
handoff, which keeps both of today's writes. Ten converting pairs, two
surviving direct writers, and that is what the census test below asserts.

A second new helper,
`_nx_answer_ensure_run_started(db, plan_id, early_bump_fired)`, issues
`increment_run_started` and no-ops on a null or zero plan id or when the early
bump already fired. It is called immediately before each of the two surviving
direct `_nx_answer_record_run` writes, by `_nx_answer_record_complete`'s
degradation branch, and by its 404 fallback, in each case before the record
write.

At the survivors the call goes **inside** the arm's existing
`with _t2_ctx() as db:` block, before the record write, because the helper
needs the same `db`. That placement puts it under the arm's outer
`except Exception: pass`, so the helper carries its own boundary catch logging
`nx_answer_plan_use_increment_failed` and never re-raises: a `run_start`
failure keeps the named warning the early site produces today
(`core.py:8677-8681`) instead of vanishing into the arm's silent swallow, and
the record write still proceeds. Opening a second context outside the arm's
`with` would keep the logging too, but at the cost of a second T2 context on a
path this RDR exists to keep to one.

The run-start site at `core.py:8669-8681` becomes the one place the capability
question is asked, with the probe read and both assignments placed immediately
**before** the `if best.plan_id:` guard, not inside it (D5). Only the
`increment_run_started` call stays inside the guard. Both booleans are
initialised to `False` at `nx_answer`'s entry so an arm upstream of this site
still reads a defined record (D5). Every
arm and the helper read that record. Nothing downstream re-reads the shared cached flag, which is what
makes a sibling's mid-call downgrade unable to change this call's arithmetic.
Read together, the rule is that the bump moves rather than disappearing or
doubling, and the census test below is what holds it in place.

**Precondition this depends on, verified and pinned.** Every arm that carries a
non-zero `plan_id` executes downstream of the run-start site, so the composite
can bump `use_count` unconditionally for a usable plan id without double
counting. `_budget_exhausted_response` is defined at `core.py:8239`, upstream of
the run-start site in the file, but every call to it (`8725`, `8755`, `9290`,
`9465`) is downstream of it in execution. The one recording arm genuinely
upstream of run-start is the planner-failure arm at `8570`, which records
`plan_id=None` and takes no counters. A test pins this rather than leaving it
as a reading of the file.

## Wire-ledger entry

Written into `docs/wire-contract-pending.md` under `## Unshipped` by the phase
that lands the engine half. The token must lead the note, and the both-direction
prose is required by `tests/test_wire_contract_pairing_lint.py`.

> `<engine-half-sha>` -- bead `<P2 bead id>` -- engine tag `TBD (next
> engine-service cut, not yet tagged)` -- [additive] one NEW route, `POST
> /v1/telemetry/nx_answer_runs/complete`, plus one NEW `/version` field,
> `nx_answer_run_complete_supported`. Engine half in this commit:
> `TelemetryRepository.recordNxAnswerRunComplete` (one `withTenant`
> transaction over `nx_answer_runs`, `nx_answer_steps` and `plans`, reusing
> the lifted `insertRunAndSteps` and the new package-private static
> `PlanRepository.incrementRunStartedIn` / `incrementRunOutcomeIn`),
> `TelemetryHandler.handleNxAnswerRunComplete`, and the `VersionHandler`
> flag. The new route REQUIRES `outcome` and `created_at`, 400 on either
> missing or unparsable; `created_at` is the dedup key that makes a retried
> composite idempotent. That requirement is scoped to `/complete` alone:
> `POST /v1/telemetry/nx_answer_runs/record` keeps its lenient handling
> (absent `created_at` stamps `now()`) unchanged, which the ETL path and
> three surviving client writers still rely on. No existing route, request
> field or response field changed shape.
> No DDL: the three tables already exist. Direction safety, both
> directions: OLD client + NEW engine -- the route exists and the flag is
> present in `/version`, but no released client posts to `/complete` or
> reads the flag, so every old client keeps issuing `run_start`,
> `nx_answer_runs/record` and `run_outcome` exactly as before and observes
> no change; NEW client + OLD engine -- the capability probe reads a
> `/version` body with no `nx_answer_run_complete_supported` key, degrades
> to the same three calls, and a 404 from `/complete` (a downgrade under a
> running process) flips the cached flag false and falls back for the rest
> of the process, so the record is never lost. Ack condition: the client
> release whose `REQUIRED_ENGINE_VERSION` bumps to the engine tag carrying
> this route AND whose client half posts to `/complete` behind the probe.

## Tests

Named here because the phases below are scoped by them. Each carries its
falsifier, per the house rule that a gate which cannot go red proves nothing.

### Java

`service/src/test/java/dev/nexus/service/NxAnswerRunCompleteTransactionTest.java`

- `runRowStepsAndPlanCountersLandInOneTransaction`: post a composite with steps
  and a real plan id; assert the run row, its step children, `use_count + 1`,
  `last_used` moved, and the correct outcome counter, all visible after one
  call.
- `stepConstraintViolationRollsBackRunRowAndPlanCounters`: the falsifier for the
  one-transaction property. Send a step whose `source` violates
  `nx_answer_steps_source_chk`; assert no run row, no step rows, and
  `use_count` / `success_count` / `failure_count` all unchanged. Reverting the
  composite to three separate `withTenant` calls turns this red.
- `dedupSkipLeavesPlanCountersUntouched`: post the same
  `(tenant, question, created_at)` twice; assert one run row and exactly one
  set of counter increments.
- `nullPlanIdWritesRunRowAndNoCounters`.
- `zeroPlanIdWritesRunRowAndNoCounters`: distinct from the null case and not
  redundant with it. `plan_id` arrives as a boxed `Long`, so `null` and `0L`
  are different values, and D1 gives them the same meaning: no library row to
  count against. An implementation that guards only on `planId != null` would
  bump counters against the synthetic inline-planner id 0, which is a row that
  does not exist. Falsifier: relax the guard to a null check alone and this
  reds while `nullPlanIdWritesRunRowAndNoCounters` stays green, which is the
  reason both exist.

`service/src/test/java/dev/nexus/service/http/TelemetryHandlerNxAnswerCompleteTest.java`

- `missingOutcomeIs400`, `unknownOutcomeValueIs400`: the no-silent-default rule.
- `missingCreatedAtOnCompleteIs400`, `malformedCreatedAtOnCompleteIs400`: the
  same rule for the dedup key (D1). Falsifier: fall back to `/record`'s lenient
  parsing on this route and both go green while the idempotency guarantee is
  gone, which is exactly the state an earlier draft of D1 described.
- `missingCreatedAtOnRecordStillStampsNow`: the other half of that pair, and
  the one that keeps the fix from leaking. `/record` must keep its lenient
  handling, because the ETL path, the D6 survivors and `nx_answer_report` all
  depend on it. Falsifier: make the requirement global and this reds.
- `stepsAbsentWritesParentOnly`: the existing `/record` degradation contract
  holds on the new route too.

`VersionHandler`'s existing test class gains
`versionAdvertisesRunCompleteSupport`.

### Python

`tests/test_nx_answer_run_complete.py`

- `test_unsupported_engine_degrades_to_three_calls`: a stub `/version` with no
  `nx_answer_run_complete_supported` key; assert exactly
  `run_start`, `nx_answer_runs/record`, `run_outcome`, in that order, and no
  request to `/complete`. Falsifier: force the flag true against the same stub
  and the assertion reds.
- `test_probe_failure_reads_as_unsupported`: `/version` raises; same three
  calls.
- `test_supported_engine_skips_run_start`: no request to
  `/v1/plans/metrics/run_start` at all.
- `test_404_downgrade_issues_deferred_run_start_then_record_then_outcome`: the
  tripping call issues exactly three POSTs, in that order,
  `/v1/plans/metrics/run_start`, `/v1/telemetry/nx_answer_runs/record`,
  `/v1/plans/metrics/run_outcome`, after the 404 from `/complete`; the next
  call in the same process goes straight to those three with no further attempt
  on `/complete`. Falsifier: drop the deferred `run_start` from the fallback
  and the first assertion sees two POSTs, which is the shape the earlier draft
  would have shipped.
- `test_every_converting_arm_routes_through_the_choke_point`: an AST census
  over `src/nexus/mcp/core.py` asserting that exactly two direct
  `_nx_answer_record_run` calls survive outside the choke point, that they are
  the two D6 exclusions (the planner-failure arm and the RDR-200 continuation
  handoff), that exactly one direct `_nx_answer_record_outcome` call survives
  and it is the handoff arm's, and that each surviving direct
  `_nx_answer_record_run` is immediately preceded by an
  `_nx_answer_ensure_run_started` call. Naming the survivors rather than
  asserting zero is what makes the census both true and useful: an eleventh
  converting arm added later reds here, and so does an implementer who quietly
  folds a D6 exclusion in. The preceded-by clause is what catches the other
  direction, a survivor that keeps its record write and loses its deferred
  bump. Falsifier: restore one converted arm's direct pair and the survivor
  count goes to three; delete one survivor's `_nx_answer_ensure_run_started`
  and the ordering clause reds.
- `test_recording_arms_are_downstream_of_run_start`: pins the D-section
  precondition, so a future refactor that hoists an arm above the run-start
  site fails here instead of double-counting `use_count` in production.
- `test_gateway_retry_reuses_one_created_at_stamp`: the falsifier for the
  design's own sharpest edge (see Risks). Drive one composite POST against a
  stub that answers 503 then 200, and assert the two attempts carry a
  byte-identical `created_at`, so the engine's dedup index can recognise the
  replay. Falsifier: recompute `created_at` inside the retry loop instead of
  once at payload construction and the assertion reds. Paired with the Java
  half, `dedupSkipLeavesPlanCountersUntouched`, which proves the engine
  actually declines to double-bump on that replay. Neither half is sufficient
  alone: the Python test proves the key is stable, the Java test proves a
  stable key is honoured.
- `test_composite_payload_always_carries_created_at`: the client half of D1's
  required-field rule, and the one that catches what stability cannot. The
  retry test above assumes the field is present and asks only whether it stays
  the same; this one asserts it is on the wire at all, stamped at the choke
  point before the first attempt, on every composite POST including the 404
  fallback's retry into the degradation path. Falsifier: drop the stamp and
  this reds while the retry test stays green, which is the gap that made the
  field worth requiring server side.
- `test_handoff_arm_against_supporting_engine_keeps_use_count_equal_to_outcomes`:
  the invariant test for the D6 survivors, and the one test here that needs a
  real store rather than a stub. Against the self-provisioned engine substrate
  (`ensure_engine` / `mint_test_tenant`) with `/complete` supported, drive an
  `nx_answer` call that terminates on the RDR-200 continuation handoff arm,
  then read the plan row back and assert
  `use_count == success_count + failure_count`. Falsifier: remove the
  survivor's `_nx_answer_ensure_run_started` and the read comes back with
  `use_count` one short, which is the exact shape that would make
  `promote.py`'s `use_count >= 3` gate unclearable for a handoff-heavy plan.
  Never against the operator's live install.
- `test_non_supporting_engine_handoff_arm_bumps_use_count_exactly_once`: the
  other half of that pair, and the case round 2 found. Against a stub whose
  `/version` reports no support, drive a call that terminates on the handoff
  arm and assert exactly one POST to `/v1/plans/metrics/run_start`: the early
  site fires it, and the survivor's helper no-ops on `early_bump_fired`.
  Falsifier: make the helper's no-op condition `plan_id`-only again and the
  assertion sees two, which is the double count this test exists for.
- `test_mid_call_flag_flip_does_not_change_this_calls_bump_count`: flip the
  shared store's cached capability flag from true to false between the
  run-start site's read and the terminating arm, and assert exactly one bump
  for that call whichever arm it reaches. Falsifier: have the arm consult the
  shared flag instead of the per-call record, together with a plain path that
  carries no deferred bump, and the assertion sees zero. This is the sibling
  interference case: the flip belongs to future calls, and this test is what
  says so in code rather than in prose.
- `test_plan_miss_against_supporting_engine_takes_composite`: the falsifier for
  D5's placement rule. Drive a plan-miss call (`plan_id == 0`, the synthetic
  inline-planner match) against a supporting stub and assert it posts to
  `/complete`, once, with `plan_id` null or zero and no counter routes touched.
  Falsifier: move the probe read and the two assignments inside the
  `if best.plan_id:` guard and this reds, because
  `composite_supported_at_start` stays at its entry default and every plan-miss
  call silently takes the degradation path. Nothing else would notice: the
  three writes still land, the counters are correctly untouched, and the only
  symptom is a route that quietly stops being used for a whole class of calls.
- `test_planner_failure_arm_upstream_of_run_start_still_records`: the falsifier
  for the entry-initialisation rule in D5. Force the inline planner to raise so
  the call terminates on the arm at `core.py:8570`, upstream of the run-start
  site, and assert the planner-failure run row is still written. Falsifier:
  bind the two booleans at the run-start site instead of at entry and the row
  stops appearing, silently, because that arm's `except Exception: pass`
  swallows the `UnboundLocalError`. Nothing else in the suite would notice,
  which is the whole reason this test is named.

`tests/test_nx_answer_t2_fanout_budget.py`

- `test_supporting_engine_issues_exactly_one_run_record_post`: against a
  supporting stub engine, exactly one POST across all **four** run-record
  routes for one `nx_answer` call. Four, not three: the count has to include
  `/v1/telemetry/nx_answer_runs/complete`, which is where that single POST
  actually lands, alongside the three routes it replaces. A three-route
  counter would read zero and pass while proving nothing. Falsifier: point the
  client at a non-supporting stub and the count is three, spread across the
  old routes.

## Phases

One developer per phase. P1 and P2 are independent and may run in parallel;
P3 depends on both; P4 depends on P3 and on an engine tag carrying P2.

**P1. Client choke point, no wire change.** Introduce
`_nx_answer_record_complete` and route the ten converting arms through it,
leaving the two D6 exclusions alone. It issues today's two calls in today's
order. The run-start site is untouched. Ships the AST census test and the
downstream-of-run-start test. Entirely client-side, no engine dependency, and
it reduces the P3 edit to one branch in one function. P1 ships only the
census test's survivor-count and survivor-naming clauses; the preceded-by
clause names `_nx_answer_ensure_run_started`, which P3 introduces, so it ships
with P3 (residual 13). Exit: the census test is
green, names both D6 survivors, and reds when one converted arm is reverted;
no route and no payload changes, and the only wire-visible difference is the
POST order on the two reversed pairs `(9572, 9551)` and `(9709, 9591)`, where
the outcome goes out before the record today (residual 8). P1 also carries
residuals 9 and 11: the choke point reproduces `_nx_answer_record_run`'s
redaction and `cost_usd` derivation and `_nx_answer_record_outcome`'s
`plan_id` guard and boundary catch, with a redaction test on the choke-point
path.

**P2. Engine half.** The route, the handler, the repository composite, the two
lifted `PlanRepository` helpers, the `/version` flag, the Java test classes
above, and the wire-ledger entry. Also an edit to an existing test:
`VersionHandlerReleaseVersionTest.java:121` asserts the capability fragment by
exact equality, so a second flag on the same append path reds there and the fix
belongs in this phase rather than being discovered by the next person to run
the suite (residual 1). P2 also owns D1's required-`created_at` rule: the
handler refuses a `/complete` payload without a parsable `created_at` with a
400 naming the field, and leaves `/record` lenient. No Liquibase changeset.
Exit: the full engine suite green via `scripts/mvnw-leased.sh`, the rollback
falsifier red when the composite is split back into three transactions,
`missingCreatedAtOnCompleteIs400` and `malformedCreatedAtOnCompleteIs400` green
with `missingCreatedAtOnRecordStillStampsNow` green beside them (so the
requirement did not leak onto the old route), and `RawSqlGateTest` green with
no new sanctioned region.

**P3. Client half behind the probe.** The capabilities-dict refactor of the
existing probe, including its cache-a-failed-probe behaviour (residual 10),
`record_nx_answer_run_complete`, the branch inside
`_nx_answer_record_complete`, the per-call capability record set at the
run-start site, the new `_nx_answer_ensure_run_started` helper with its
`early_bump_fired` guard and its calls at the two D6 survivors and on both
degradation branches, and the 404 downgrade guard including its deferred
`run_start`. P3 also owns idempotency: the choke point stamps `created_at` once,
at construction, before the first attempt, on every composite payload. The
engine now refuses a `/complete` without it (D1, P2), so this is not a
convention the two halves each hope the other keeps.
Ships the Python tests above including the budget test,
`test_gateway_retry_reuses_one_created_at_stamp`,
`test_composite_payload_always_carries_created_at`, the 404 downgrade ordering
test, the handoff invariant test, the non-supporting-engine handoff test, the
mid-call flip test, the planner-failure-arm test, the plan-miss composite test,
and the census test's preceded-by clause (residual 13). It also edits
`tests/test_nx_answer_t2_fanout_budget.py`: the five-context enumeration in its
construction assertion becomes three under a supporting engine, and its
non-empty-`step_records` comment stops being true once the probe fires at the
run-start site on every call (residual 16). Exit: the degradation test green against a
non-supporting stub, the budget test green against a supporting stub, both red
when the probe is forced the other way, the retry-stamp test green and red when
`created_at` is recomputed inside the retry loop, the stamp-present test green
and red when the stamp is dropped, the 404 test showing three
POSTs in order then one composite, the handoff invariant test green and red
when the survivor's deferred bump is removed, both bump-count tests green
and red under their own falsifiers (a `plan_id`-only no-op condition, and an
arm that re-reads the shared flag), and the plan-miss composite test green and
red when the probe read is moved inside the `if best.plan_id:` guard.

**P4. Pairing, cutover and documentation.** Bump
`REQUIRED_ENGINE_VERSION` to the engine tag carrying P2 in the client release
that carries P3, move the ledger entry from `## Unshipped` to `## Shipped`,
and update both `docs/cli-reference.md` and `src/nexus/plans/promote.py`'s
module docstring for the `use_count` semantic change. After
cutover, run the reconciliation read the research item below defines and record
the result. Exit: `scripts/check_engine_release_floor.py` green without a
paired-deploy exception, and the reconciliation recorded.

## Residuals carried into implementation

Twenty-one findings from four plan-audit rounds, all classified
DISCOVER-AT-IMPLEMENTATION. They are recorded here so the implementer meets
them on the page rather than in the first test run. None of them re-opens a
decision, and none is re-planned. Items 1 to 7 came from round 1, 8 to 11 from
round 2, 12 to 16 from round 3, and 17 to 21 from the post-acceptance round.

1. **`VersionHandlerReleaseVersionTest.java:121` asserts by exact equality.**
   `service/src/test/java/dev/nexus/service/http/VersionHandlerReleaseVersionTest.java`'s
   `appendNxAnswerStepsCapabilityFieldAlwaysEmitsTrue` compares the emitted
   fragment to the literal `,"nx_answer_steps_supported":true`. Adding a second
   capability field on the same append path reds it. Listed as a P2 edit above.
2. **D1's route body must carry `created_at`.** P2 and P3 are different
   developers, so the field's role as the dedup key is stated in the engine's
   contract (D1) rather than left as a client-side implementation detail. An
   engine developer reading only D1 would otherwise treat it as optional
   decoration and a client developer might omit it.
3. **`TelemetryRepository`'s constructor takes only `TenantScope`.** The lifted
   `PlanRepository` helpers are therefore static, or `NexusService`'s wiring
   changes to hand one repository to the other. This RDR takes the static form;
   the note is here so the choice is not re-litigated at the keyboard.
4. **The `(9709, 9591)` pair straddles the RDR-084 plan-grow block.** The
   success outcome is recorded at `9591` and Step 6's record write is at
   `9709`, with the plan-grow save between them. P1 chooses the merge site when
   it collapses that pair into one choke-point call, and the choice is a
   judgement about where the grow block should sit relative to the record, not
   a mechanical move.
5. **Nine of the ten converting arms still use `with _t2_ctx() as db:`, not
   `_t2_index_write`.** Only the Step 6 site was converted by nexus-m20mf P2.
   Moving the other nine changes which failures reach
   `_service_t2_write_locked`'s eviction classifier, and `core.py:9695-9708`
   already documents the one-way version of this for the Step 6 site: an
   internal `_warn_telemetry_drop` swallow means a connectivity error there
   never reaches the classifier. Widening the population of callers inside the
   singleton is a behaviour change worth watching, not a refactor.
6. **The capability probe fires at the run-start site on every call after P3.**
   That is a cached-dict read, not a round trip, so the budget of one
   `/version` probe per process still holds. Recorded because a reader of the
   budget test could otherwise mistake the new early call for a regression.
7. **`NxAnswerRunCompleteTransactionTest` sits beside `TelemetryRepositoryTest`.**
   That is `service/src/test/java/dev/nexus/service/`, not a `db/`
   subpackage. An earlier draft named the `db/` path, which does not exist.

Four more from the round-2 audit, same classification:

8. **P1's "no behaviour change observable on the wire" is false for the two
   reversed pairs.** At `(9572, 9551)` and `(9709, 9591)` the outcome POST goes
   out before the record POST today. Collapsing each into one choke-point call
   necessarily picks an order, so those two arms change the order of two POSTs
   even though P1 changes no route and no payload. P1's exit criterion names
   the exception rather than claiming a property it does not have.
9. **The choke point must reproduce what `_nx_answer_record_run` does before
   the wire call.** Two behaviours live in that function rather than in the
   store: `trace=False` replaces both `question` and `final_text` with
   `[redacted]`, and `cost_usd` is the sum of the step records' known costs, or
   `None` when none is known, never a fabricated `0.0` (`core.py:7078-7083`).
   The composite path has to carry both. A test must cover redaction on the
   composite path specifically, since a redaction that silently stops applying
   on a new route is not visible in any output the caller sees.
10. **`_version_capabilities()` must cache a failed or empty probe.** Today
    `_supports_nx_answer_steps` writes `False` into its cache on any exception
    before returning (`http_telemetry_store.py:437-440`), so an unreachable
    `/version` costs one attempt per process rather than one per call. The
    capabilities dict has to keep that property, or the budget test's
    one-probe-per-process assertion holds only on the happy path.
11. **Inlining the outcome increment must reproduce
    `_nx_answer_record_outcome`'s two guards.** It returns early when
    `plan_id` is falsy, covering the synthetic inline-planner id 0, and it
    wraps the write in a boundary catch that logs
    `nx_answer_plan_outcome_increment_failed` rather than raising
    (`core.py:7256-7276`). Both have to survive the move into the choke point;
    losing the catch turns a best-effort telemetry failure into a crashed
    answer.

Five more from the round-3 audit, which was residuals-only:

12. **The two booleans are initialised at `nx_answer`'s entry, not at the
    run-start site** (high). The planner-failure arm at `core.py:8570` runs
    upstream of the run-start site at `core.py:8674`, so a name first bound at
    that site is unbound on that path, and the arm's
    `try: with _t2_ctx() as db: ... except Exception: pass`
    (`core.py:8568-8577`) would swallow the `UnboundLocalError` and silently
    stop writing planner-failure run rows. Folded into D5's text rather than
    left as a note, and pinned by
    `test_planner_failure_arm_upstream_of_run_start_still_records`.
13. **P1 ships the census test, but its preceded-by clause names a P3
    symbol** (medium). `_nx_answer_ensure_run_started` does not exist until P3,
    so P1 cannot assert that each surviving record write is preceded by a call
    to it. Split the test: P1 ships the survivor-count and survivor-naming
    clauses, P3 adds the preceded-by clause when it adds the helper. Named in
    both phases below.
14. **The survivors' helper call needs a `db`, and its placement decides its
    failure visibility** (medium). Inside the arm's existing
    `with _t2_ctx() as db:`, before the record write, is the rule. That puts it
    under the arm's `except Exception: pass`, so the helper owns a boundary
    catch logging `nx_answer_plan_use_increment_failed` and never re-raises.
    Written into the client-half design above.
15. **The budget test must count four routes** (low).
    `test_supporting_engine_issues_exactly_one_run_record_post` counts POSTs
    across the three routes the composite replaces plus `/complete` itself,
    which is where the one POST lands. Corrected in the test description above.
16. **`tests/test_nx_answer_t2_fanout_budget.py` is a P3 edit** (low). Its
    assertion enumerates the happy path's "five T2 contexts (plan_match,
    price_table, run_start, record_run, run_outcome)", which becomes three
    under a supporting engine. Its harness also reaches the `/version` probe
    only because `step_records` is non-empty, a gate its own comment explains
    ("a non-empty step_records is required to exercise the GET /version
    capability probe at all"), and that gate disappears once the probe moves to
    the run-start site and fires on every call. Both the enumeration and the
    comment need updating with the behaviour, not after it.

Five more from the post-acceptance audit round:

17. **The new `/version` flag needs an edge-allowlist relay, owned by P4.** The
    public edge trims `GET /version` to an allowlist of keys, which is how
    nexus-bwulw silently disabled three client features that were green on
    every engine-direct gate. `nx_answer_run_complete_supported` is a new key,
    so a cloud client reads it as absent, the probe degrades, and the composite
    route is never taken in cloud mode even against an engine that serves it.
    Nothing in this repo can fix that: it is a conexus-side relay, surfaced
    explicitly at deploy time, and it belongs with P4's pairing work rather
    than with the engine half that adds the field.
18. **The wire-ledger entry goes stale between P2 and P4.** P2 writes it under
    `## Unshipped` naming a `TBD` engine tag; the entry's own lint fails a
    stale `Unshipped` row once its commit is an ancestor of the newest
    published client tag. Between the two phases the entry has to be updated
    with the real tag when the engine is cut, then moved at P4. Neither phase
    owns the middle step today, so P4 checks the entry names a real tag before
    it moves it.
19. **`tests/test_engine_version.py:495` is a P4 edit.** It pins
    `REQUIRED_ENGINE_VERSION` by exact equality, currently `(0, 1, 104)`, with
    a running comment log above it explaining each bump. P4 bumps the floor to
    the tag carrying P2, so it edits both the assertion and the log, and the
    log entry is the place to say what the tag carries and that it was gated
    before the bump.
20. **P3 flips this document's own Assumed research bullet.** The after half of
    the round-trip count stays Assumed until P3's per-path assertion measures
    it. When it does, the Research Findings entry moves from `❓ Assumed` to
    `✅ Verified` with the measured numbers, and P3 is the phase that knows
    them. Leaving it Assumed after the measurement exists would be its own
    small dishonesty.
21. **P1's downstream-of-run-start test has to be checkable.**
    `test_recording_arms_are_downstream_of_run_start` pins a control-flow
    property, and a naive AST reading of source order gets it wrong: the budget
    arm is defined at `core.py:8239`, textually above the run-start site, and
    is only ever called from below it. The test has to reason about call sites
    rather than definition order, or assert the property dynamically by driving
    each arm. Whichever way P1 takes, the test must be able to fail; a version
    that cannot distinguish the two orders proves nothing.

## Risks

**The atomicity case rests on a rare gap: one orphan in 190 uses.** RDR-198's
spike found none in 29 runs; the wider re-measure found one in 190, 0.5%
(`nexus_rdr/203-research-1`). That is enough to say the mechanism fires in
practice and not enough to carry the work on its own. The justification stays
round-trip count and operation placement, with the integrity gap attached as a
measured, small base rate. This RDR should not be accepted on an atomicity
argument its own evidence does not carry.

**One 429 now refuses the whole record.** The engine's request-scoped 429
budget is not in `_send`'s retryable set, and a 429 raises. Today that costs
one of three writes; under the composite it costs the record. Blast radius is
the same, because all three are best-effort telemetry inside boundary catches,
but the refusal must be logged with the same `_warn_telemetry_drop` visibility
rather than swallowed. Direction of pressure is favourable: three requests
become one, so budget pressure falls.

**A retried composite can double-apply the counters.** `_send` retries
502/503/504. If the first attempt committed and the response was lost, the
retry re-inserts (dedup-skipped, because `created_at` is carried on the
payload) and, under D3, skips the counters with it. That is the guard, and it
depends on the client sending a stable `created_at` on retry rather than
regenerating it. The implementation must stamp `created_at` once, client side,
before the first attempt. Without that, the dedup index does not match and the
counters double.

The mechanism cooperates. `_post` and `_send` take the payload dict once from
the caller and pass the same object through every gateway retry attempt, so a
`created_at` computed at payload construction is naturally stable. Nothing
structural enforces it, which is exactly why this is the sharpest edge, and it
is pinned from both sides: `test_gateway_retry_reuses_one_created_at_stamp`
proves the key is stable across attempts and
`dedupSkipLeavesPlanCountersUntouched` proves a stable key stops the second
apply. P3 owns the stamp and carries the Python half in its exit criteria.

**Probe staleness across an engine change under a running process.** The store
is process-lifetime after nexus-m20mf P3, so an upgrade is adopted at the next
MCP spawn (safe, just delayed) and a downgrade is handled by the 404 guard in
D5. A downgrade to an engine that answers something other than 404 on an
unknown route would defeat the guard; no engine in the tree does that.

**The 404-downgrade tail is a second route into the accepted orphan class.**
P3's critique (T2 `nexus/critique-nexus-dt2tu-3-p3`, finding (b)) verified from
source that a mid-call 404 does not close the atomicity gap the composite was
built to close: `_nx_answer_ensure_run_started` and the fallback's
`record_nx_answer_run` share the same `db` handle, threaded through
unchanged, but each carries its own independent boundary catch and there is no
rollback between them. `use_count` can therefore bump while the record write
independently fails, leaving `use_count` one higher than
`success_count + failure_count` for that run — the same failure shape the
Risks section above already accepts at a measured 0.5% base rate
(`nexus_rdr/203-research-1`), reached by a second path rather than a new one.
This is not a new invariant violation and not a ship-blocker: the generic P4
reconciliation read (see Open research item 3) already catches it, because it
compares `use_count` against `success_count + failure_count` without caring
which path produced the gap.

**Scope drift toward a read-side bundle.** A plan-match plus price-table read
bundle is the obvious next composite and is deliberately excluded. The price
table is process-cached with a TTL and the plan cache is a 90-second process
singleton, so a read bundle would buy close to nothing today and its contract
question is larger. It is not in this RDR and should not ride along.

**Ten arms is a lot of edit surface for a telemetry change, and two more arms
deliberately do not move.** P1 exists specifically to take that risk on its
own, with no wire change in flight. The AST census keeps both halves honest: an
eleventh converting arm added later, and a D6 exclusion quietly folded in.

## Open research, to close before acceptance

1. **Re-measure the orphan base rate. DONE, 2026-09-05.** One orphan in 190
   recorded uses across 16 plans with `use_count > 0`. Recorded as
   `nexus_rdr/203-research-1` and carried in Research Findings below.
2. **Confirm the round-trip claim end to end. Before half DONE, after half
   pending P3.** The before number is three run-record POSTs on three routes
   plus one `/version` probe per process, read from the code and from
   `tests/test_nx_answer_t2_fanout_budget.py`. The after number is measured by
   the per-path assertion P3 adds to that same harness. Recorded as
   `nexus_rdr/203-research-2`, with its risk stated below.
3. **Decide whether P4's reconciliation becomes a standing check. Recommended
   disposition surfaced 2026-09-06, decision still Sam's.** There is one
   orphan row in the live store from before cutover, the plan 365 row
   (`use_count` 11, 10 successes, 0 failures, `nexus_rdr/203-research-1`). It
   needs either a one-time hand reconciliation or an explicit decision to
   leave it undone and documented. `nx plan --help` carries no verb that
   corrects a single counter on a live row — `list` / `show` / `delete` /
   `disable` / `enable` / `set-scope` / `reseed` / `hygiene` are the whole
   surface, and none of them touches `use_count`, `success_count` or
   `failure_count` — so a hand reconciliation today means either a direct
   write against the live plan-library store (outside any supported `nx`
   path, and not something a dev checkout's prod-write guard permits) or a
   new verb built solely to fix one row once. Recommendation:
   **leave-and-document**, one row is small enough that leaving it is
   defensible, and the read-only reconciliation this phase already runs
   (whether it becomes a standing check or a one-time read) is the record
   that the decision was made rather than inherited. Whether the
   reconciliation read itself becomes a standing check is a separate
   question this recommendation does not answer. Neither this recommendation
   nor the standing-check question has been put to Sam yet; both remain
   open until they are.

## Alternatives considered

**Do nothing.** Serious, and the strongest competitor. The measured cost is two
small POSTs per run against the same host on a call whose p50 is 80 seconds,
and the integrity gap fires about once in 190 runs. What do-nothing
does not answer is Sam's direction on the bead, which was about where the
operation lives rather than what it costs: the client composes an operation the
engine could own, and every future reader of that code has to reconstruct the
sequence from three call sites in two stores.

**Collapse writes 2 and 3 only.** Covered in D4. Half the round trips, none of
the integrity property, and it leaves the orphan window exactly where it is.

**A generic multi-write batch route.** One `/v1/telemetry/batch` taking a list
of writes and executing them in one transaction would serve this case and
others. It is rejected: it re-expresses client-side composition as a payload
instead of removing it, it makes the engine's contract a list of statements
rather than an operation, and it gives the engine no place to enforce that a
run record and its outcome belong together.

**`idempotent=False` on the composite POST instead of a stable `created_at`.**
The codebase already has a mechanism for exactly this risk shape.
`_refreshable_client.py`'s `idempotent=False` (nexus-tjvgf) issues a request
exactly once per credential, with no gateway 502/503/504 backoff loop and no
transport-error re-resolve, and its own docstring names "a retry-budget counter
double-incremented" among the hazards it exists to prevent.
`record_capability_census` and `record_routing_event` already use it. Not
using the established pattern needs a reason, and here is the reason: the
opt-out buys the no-double-apply property by giving up the 502/503/504
resilience that a real gateway incident motivated, and it gives it up on a
best-effort telemetry write whose entire failure mode is being silently lost.
The `created_at` stamp buys the same property and keeps the retry, because it
makes the operation genuinely idempotent at the engine rather than merely
un-retried at the client. Under the opt-out a single gateway blip drops the
whole run record; under the stamp it is retried and deduplicated. The stamp is
preferred for that reason and for one more: it composes, where the opt-out does
not. If the stamp ever turns out not to hold (an engine that ignores a
client-supplied `created_at`, a dedup index that changes shape), `idempotent=
False` is the correct fallback and should be taken then, deliberately, with the
resilience loss stated. It is not the first choice.

**Client-side retry reconciliation instead of atomicity.** A background sweep
that finds `use_count > success + failure` and reconciles it. That adds a
mechanism to compensate for a gap the engine can simply not open, and it needs
its own schedule, its own failure modes and its own tests.

## Revision History

- 2026-09-05: created as draft. Picks up the scope RDR-198 withdrew and named
  as belonging in its own RDR. Scoped to the run-record operation only; the
  read-side bundle is deliberately excluded.
- 2026-09-06: amendment to the accepted text, recorded as a revision rather
  than a re-plan. The post-acceptance audit round found that D5 named the
  run-start site as the one place the capability question is asked without
  naming the `if best.plan_id:` guard the site sits inside (`core.py:8669`).
  Read literally, that puts the probe read inside the guard, so every plan-miss
  call would leave `composite_supported_at_start` at its entry default and
  never take the composite, contradicting D1's acceptance of a null or zero
  plan id and P2's `zeroPlanIdWritesRunRowAndNoCounters`. D5 now places the
  read and both assignments immediately before the guard, with only
  `increment_run_started` inside it, and
  `test_plan_miss_against_supporting_engine_takes_composite` is its falsifier,
  in P3's exit criteria. Five residuals appended as items 17 to 21: the
  edge-allowlist relay for the new `/version` flag (P4), wire-ledger staleness
  between P2 and P4, `tests/test_engine_version.py:495` as a P4 edit, P3
  flipping this document's own Assumed research bullet once it measures the
  after half, and P1's downstream-of-run-start test needing to reason about
  call sites rather than definition order so it can actually fail. No decision
  changed.
- 2026-09-06: P4 (nexus-dt2tu.4) documentation pass, tag-independent parts.
  The Risks section gained a bullet naming the 404-downgrade tail as a second
  route into the accepted orphan class (P3's critique, T2
  `nexus/critique-nexus-dt2tu-3-p3` finding (b)): `use_count` can bump while
  the fallback record write independently fails, which is the same measured
  0.5% failure shape reached by a second path rather than a new one, and the
  generic P4 reconciliation read already covers it. `docs/cli-reference.md`
  (`nx plan show`) and `src/nexus/plans/promote.py`'s module docstring were
  updated for the D4 `use_count` semantic change; the "three actual runs"
  gloss in `promote.py` is stale until cutover and becomes literally true
  once it lands, and both documents say so. No decision changed; this entry
  and the P4 bump/ledger-move/reconciliation work that still depends on an
  engine tag are tracked separately on the bead.
- 2026-09-05: Layer 3 gate critique folded in (T2
  `nexus/critique-rdr-203-gate-64c4802bc` [24700]), two Criticals, both
  text-level. The gaps intro still said gaps 2 and 3 had an occurrence rate of
  zero, contradicting Gap 2's own 1-in-190 a paragraph below and inventing a
  measurement for Gap 3, which has none; it now says rare for one and
  unmeasured for the other, and a grep confirms the only surviving "zero"
  occurrence claims are the historical references to RDR-198's spike. Second,
  D1 called `created_at` the dedup key and then left it defaultable through
  `/record`'s lenient parsing, which is a contradiction rather than a
  trade-off: `/complete` now REQUIRES it, 400 on missing or unparsable, exactly
  as `outcome` is treated on the same route and scoped to that handler so
  `/record` stays lenient for the ETL path and the surviving writers. Three
  Java tests and one Python test are named for it, and both P2's and P3's exit
  criteria carry their halves. The wire-ledger entry says the field is
  required.
- 2026-09-05: both pre-acceptance research items recorded, and a
  `## Research Findings` section added carrying them in the rdr-research
  entry format. The orphan re-measure changed a claim: one orphan in 190
  recorded uses across 16 plans, 0.5%, against RDR-198's zero in 29. Every
  "measured at zero" statement in the document is corrected to that number,
  in Gap 2, the claim section, Context, Risks and Alternatives. The gap is now
  rare rather than unobserved, which strengthens it slightly and changes
  nothing about the justification, which stays round-trip count and operation
  placement. The round-trip item's before half is Verified and its after half
  is Assumed until P3 lands, with the risk (P3 ships without the per-path
  assertion and the claim is never measured) and its mitigation stated.
  Open research item 3 is sharpened rather than closed: there is one real
  orphan row in the live store to reconcile or deliberately leave.
- 2026-09-05: Problem Statement restructured into the `#### Gap N:` blocks the
  formal gate's Layer 1 requires, which had it BLOCKED before any other layer
  ran. Three gaps: the three round trips (verified), the client-composed
  operation the engine cannot make atomic (mechanism verified, and at that
  point carrying only RDR-198's zero-in-29; corrected to 1 in 190 by the
  research entry above), and `use_count` counting attempts with no way to reconcile
  it against outcomes (verified by reading, unmeasured in the field). No
  decision changed; the gaps state what the Decisions section already answers.
  The Residuals opener, which still said seven findings from round 1, now says
  sixteen across three rounds.
- 2026-09-05: round-3 plan audit folded in. Residuals-only: the round-2 blocker
  is closed and the gate is cleared, so items 12 to 16 are carried into
  implementation rather than re-planned. One of them changed live text, because
  it was a defect in the design and not only a note for the implementer: the
  two capability booleans are initialised at `nx_answer`'s entry, since the
  planner-failure arm runs upstream of the run-start site and its own
  `except Exception: pass` would swallow the unbound-name error and silently
  stop recording that arm's runs. D5 says so, and
  `test_planner_failure_arm_upstream_of_run_start_still_records` is its
  falsifier. Two smaller corrections came with the residuals: the budget test
  counts four routes rather than three, since `/complete` is where the single
  POST lands, and the survivors' helper call is placed inside the arm's
  existing T2 context with its own boundary catch, so a `run_start` failure
  keeps today's named warning.
- 2026-09-05: round-2 plan audit folded in, the mirror of round 1. Deferring
  the bump fixed the under-count and opened two over/under-count cases the
  round-1 text could not see: a `plan_id`-only no-op double-bumps a D6 survivor
  against a non-supporting engine, and an arm that re-reads the shared cached
  flag can be sent down the plain path by a sibling's mid-call 404 after its own
  early site already skipped. Both are answered by the auditor's amendment,
  adopted here: the capability question is asked once, at the run-start site,
  and its answer plus whether the early bump fired are carried in the call's
  own state. Every arm and the helper key on that record. Rule 1 (early bump
  fired means no arm bumps again) is enforced by routing rather than by a
  `count_use` field, so the engine route stays stateless; the reason is stated
  in D5. The exactly-one-bump argument is restated over four cases plus the
  mid-call flip, two bump-count tests are named with falsifiers, and four more
  DISCOVER residuals are appended.
- 2026-09-05: round-1 plan audit folded in. Both BLOCKS-PLANNING findings are
  answered by one mechanism, `_nx_answer_ensure_run_started`: the early
  `run_start` is deferred rather than dropped when the probe reports support,
  and every path that does not terminate through `/complete` issues it before
  its own record write. That keeps `use_count == success_count +
  failure_count` on the D6 survivors, so `promote.py`'s gate stays clearable
  for a handoff-heavy plan, and it makes the 404 downgrade complete its own
  tripping call as three writes in order rather than dropping that call's
  record. D5, D6, D4 and the affected tests are rewritten accordingly, and a
  Residuals section records the seven DISCOVER-AT-IMPLEMENTATION findings
  verbatim for the implementer to carry.
- 2026-09-05: critique folded in (T2 `nexus/critique-nexus-m20mf-p5-rdr-203`
  [24680]). Five changes. The scope self-contradiction is resolved in favour of
  D6: the RDR-200 continuation handoff arm at `9378`/`9387` does not convert,
  the converting set is ten pairs rather than eleven, and the census test now
  names its two survivors instead of asserting zero. D4's claim that
  `promote.py` does not read `use_count` was false and is replaced with the
  actual effect, a tightening of the `use_count >= 3` gate, with
  `promote.py`'s own docstring added to P4's doc-update list. The retried-
  composite double-apply now has a named falsifying test on both sides and sits
  in P3's exit criteria. `idempotent=False` (nexus-tjvgf) is named and rejected
  in Alternatives, with the fallback condition stated. A Java test for
  `plan_id == 0` is named separately from the null case.
