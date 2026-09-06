---
title: "One Composite Run Record: Collapse nx_answer's Three Telemetry Writes into a Single Engine Operation"
id: RDR-203
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-05
accepted_date:
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

The claim is also not a fix for an observed defect. RDR-198's research pass
spiked the orphan window directly and found zero orphaned records: across all
five plans with `use_count > 0`, 29 runs, `use_count == success_count +
failure_count` held everywhere. The atomicity gap is latent. It is recorded
below with that base rate attached, and re-measuring it is a gate item before
acceptance rather than a claim this document makes on its own authority.

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
reconciles that. The measured rate of it happening is zero out of 29 runs.

**Failure independence, which cuts both ways.** Today a 429 or a transport
error costs one of the three writes and the other two still land, producing a
partial record. Under one composite POST the same error costs the whole record.
All three writes are best-effort telemetry wrapped in boundary catches at every
call site, so neither shape can break a user-facing answer. Partial telemetry
is the worse of the two outcomes, which is the same reasoning
`recordNxAnswerRun` already applies to its parent and child rows.

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
  "steps": [ ... ],
  "outcome": "success"
}
```

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
  checkable.
- What is lost: the count of abandoned runs. That signal was never readable
  anyway, because an abandoned run writes no `nx_answer_runs` row either, so
  the only trace it left was a counter nobody could reconcile against anything.
- What is unaffected: `plans/promote.py` gates on
  `success / (success + failure)`, which does not read `use_count`.
- What skews: `last_used` moves later by the run duration, up to about 80
  seconds at p50. Nothing reads it at that resolution.

The change is documented in `docs/cli-reference.md` wherever `use_count` and
`nx answer-runs` are explained, as part of the phase that ships the client half.

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

**Downgrade guard.** The probe caches for the life of a process, and after
nexus-m20mf P2 and P3 that is the life of the MCP host. If a supporting engine
is replaced by a non-supporting one under a running client, the cached `true`
would post to a route that 404s and the entire run record would be lost,
including the outcome, which is worse than today. The client therefore treats a
404 from `/complete` as a probe correction: it flips the cached flag to false,
logs it once, and falls back to the three-call path for that call and every
call after it in the process. A 404 is the only status treated this way; a 429
or a 5xx is a transport failure and keeps today's drop-and-warn handling.

### D6. What stays on the existing route

Three writers keep `POST /v1/telemetry/nx_answer_runs/record` and are out of
scope:

- The planner-failure arm (`core.py:8570`), which records a run with
  `plan_id=None` and has no outcome to report.
- The RDR-200 continuation handoff row.
- `nx_answer_report`, which appends a report event, not a run.

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
`incrementRunOutcome` are lifted into package-private
`incrementRunStartedIn(DSLContext ctx, long id)` and
`incrementRunOutcomeIn(DSLContext ctx, long id, boolean success)`. The two
public methods become `withTenant` wrappers over them and keep their exact
current behaviour. `TelemetryRepository` is in the same package
(`dev.nexus.service.db`) and calls the helpers directly, so the counter DSL
exists once and both entry points execute the same statements.

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
  step_count, final_text, cost_usd, duration_ms, steps, success)`: one POST to
  `/complete`. It is the wire call only. It does not branch on the probe,
  because the fallback needs the plans store as well and that decision belongs
  at the call site.

**`src/nexus/mcp/core.py`**

One new choke point, `_nx_answer_record_complete(db, *, question, plan_id,
matched_confidence, step_count, final_text, step_records, duration_ms, trace,
success)`, replacing each `(_nx_answer_record_run, _nx_answer_record_outcome)`
pair. There are eleven such pairs today, one per terminating arm, listed as
`(record site, outcome site)`: `(8362, 8373)`, `(8890, 8898)`, `(8930, 8938)`,
`(9015, 9027)`, `(9041, 9049)`, `(9110, 9120)`, `(9217, 9226)`, `(9378, 9387)`,
`(9438, 9447)`, `(9572, 9551)` where the outcome is recorded first, and
`(9709, 9591)` where the success outcome is recorded before Step 6 writes the
row. The twelfth `_nx_answer_record_run` call, at `8570`, is the planner-failure
arm and has no outcome; it stays on the existing route per D6. Every arm calls
the choke point once, inside one `_t2_index_write` closure, matching the
closure-purity rule P2 established.

The run-start site at `core.py:8674` becomes conditional: issue
`increment_run_started` only when the probe reports no support.

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
> the lifted `insertRunAndSteps` and the new package-private
> `PlanRepository.incrementRunStartedIn` / `incrementRunOutcomeIn`),
> `TelemetryHandler.handleNxAnswerRunComplete`, and the `VersionHandler`
> flag. No existing route, request field or response field changed shape.
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

`service/src/test/java/dev/nexus/service/db/NxAnswerRunCompleteTransactionTest.java`

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

`service/src/test/java/dev/nexus/service/http/TelemetryHandlerNxAnswerCompleteTest.java`

- `missingOutcomeIs400`, `unknownOutcomeValueIs400`: the no-silent-default rule.
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
- `test_404_on_complete_falls_back_and_flips_the_cached_flag`: first call falls
  back to three writes, second call in the same process goes straight to three
  writes with no further attempt on `/complete`.
- `test_every_terminating_arm_routes_through_the_choke_point`: an AST census
  over `src/nexus/mcp/core.py` asserting no direct
  `_nx_answer_record_outcome` call survives outside the choke point, and that
  `increment_run_started` appears at exactly one site. This is what keeps a
  twelfth arm added later from silently going off-contract. Falsifier: restore
  one arm's direct pair and it reds.
- `test_recording_arms_are_downstream_of_run_start`: pins the D-section
  precondition, so a future refactor that hoists an arm above the run-start
  site fails here instead of double-counting `use_count` in production.

`tests/test_nx_answer_t2_fanout_budget.py`

- `test_supporting_engine_issues_exactly_one_run_record_post`: against a
  supporting stub engine, exactly one POST across all three run-record routes
  for one `nx_answer` call. Falsifier: point the client at a non-supporting
  stub and the count is three.

## Phases

One developer per phase. P1 and P2 are independent and may run in parallel;
P3 depends on both; P4 depends on P3 and on an engine tag carrying P2.

**P1. Client choke point, no wire change.** Introduce
`_nx_answer_record_complete` and route all eleven terminating arms through it.
It issues today's two calls in today's order. The run-start site is untouched.
Ships the AST census test and the downstream-of-run-start test. Entirely
client-side, no engine dependency, and it reduces the P3 edit to one branch in
one function. Exit: the census test is green and reds when one arm is reverted;
no behaviour change observable on the wire.

**P2. Engine half.** The route, the handler, the repository composite, the two
lifted `PlanRepository` helpers, the `/version` flag, the four Java test
classes above, and the wire-ledger entry. No Liquibase changeset. Exit: the
full engine suite green via `scripts/mvnw-leased.sh`, the rollback falsifier
red when the composite is split back into three transactions, and
`RawSqlGateTest` green with no new sanctioned region.

**P3. Client half behind the probe.** The capabilities-dict refactor of the
existing probe, `record_nx_answer_run_complete`, the branch inside
`_nx_answer_record_complete`, the conditional run-start, and the 404 downgrade
guard. Ships the Python tests above including the budget test. Exit: the
degradation test green against a non-supporting stub, the budget test green
against a supporting stub, and both red when the probe is forced the other way.

**P4. Pairing, cutover and documentation.** Bump
`REQUIRED_ENGINE_VERSION` to the engine tag carrying P2 in the client release
that carries P3, move the ledger entry from `## Unshipped` to `## Shipped`,
and update `docs/cli-reference.md` for the `use_count` semantic change. After
cutover, run the reconciliation read the research item below defines and record
the result. Exit: `scripts/check_engine_release_floor.py` green without a
paired-deploy exception, and the reconciliation recorded.

## Risks

**The atomicity case rests on a latent gap with a measured rate of zero.**
RDR-198's spike found no orphans in 29 runs across five plans. If the re-measure
below also finds zero, the honest justification narrows to round-trip count and
operation placement, and "do nothing" gets stronger. This RDR should not be
accepted on an atomicity argument that its own evidence does not carry.

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
counters double. This is the sharpest edge in the design; pin it with a test.

**Probe staleness across an engine change under a running process.** The store
is process-lifetime after nexus-m20mf P3, so an upgrade is adopted at the next
MCP spawn (safe, just delayed) and a downgrade is handled by the 404 guard in
D5. A downgrade to an engine that answers something other than 404 on an
unknown route would defeat the guard; no engine in the tree does that.

**Scope drift toward a read-side bundle.** A plan-match plus price-table read
bundle is the obvious next composite and is deliberately excluded. The price
table is process-cached with a TTL and the plan cache is a 90-second process
singleton, so a read bundle would buy close to nothing today and its contract
question is larger. It is not in this RDR and should not ride along.

**Eleven arms is a lot of edit surface for a telemetry change.** P1 exists
specifically to take that risk on its own, with no wire change in flight, and
the AST census exists to keep the twelfth arm honest.

## Open research, to close before acceptance

1. **Re-measure the orphan base rate.** Re-run RDR-198's spike against the
   current store: for every plan with `use_count > 0`, compare `use_count` with
   `success_count + failure_count`. Record the counts and the date. On a live
   install this is a read through `nx plan list` and the plan library, never a
   write, and never from a dev session against the operator's install.
2. **Confirm the round-trip claim end to end.** On the test substrate, count
   HTTP requests by path for one fixed `nx_answer` question before and after
   P3, and record the numbers. Expected direction, stated in advance so a miss
   is visible: three run-record POSTs become one, `/version` probes stay at one
   per process, and wall clock moves by an amount too small to measure against
   an 80-second call. The counts are the claim.
3. **Decide whether P4's reconciliation becomes a standing check.** If the
   re-measure finds a non-zero orphan population from before cutover, those
   rows need either a one-time reconciliation or an explicit decision to leave
   them.

## Alternatives considered

**Do nothing.** Serious, and the strongest competitor. The measured cost is two
small POSTs per run against the same host on a call whose p50 is 80 seconds,
and the integrity gap has a measured occurrence rate of zero. What do-nothing
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

**Client-side retry reconciliation instead of atomicity.** A background sweep
that finds `use_count > success + failure` and reconciles it. That adds a
mechanism to compensate for a gap the engine can simply not open, and it needs
its own schedule, its own failure modes and its own tests.

## Revision History

- 2026-09-05: created as draft. Picks up the scope RDR-198 withdrew and named
  as belonging in its own RDR. Scoped to the run-record operation only; the
  read-side bundle is deliberately excluded.
