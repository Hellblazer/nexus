# Post-Mortem: RDR-203 One Composite Run Record

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

Recording one `nx_answer` run took three HTTP writes from the client to the
engine (the Java service that owns the T2 tables): a plan-library `run_start`,
a telemetry `record`, and a plan-library `run_outcome`, spread across two
stores and two moments in the call. The engine already wrote three tables in
one transaction for the import path; the client was composing the same
operation by hand. RDR-203 proposed one new route,
`POST /v1/telemetry/nx_answer_runs/complete`, one engine transaction over
`nx_answer_runs`, `nx_answer_steps` and `plans`, a `/version` capability flag
with byte-identical degradation to the old three calls, and a change in what
the plan library's `use_count` means: recorded runs, not attempts.

## Implementation Status

**Implemented.** Engine half shipped in `engine-service-v0.1.105` (commit
`2b2555170`), deployed to the cloud 2026-09-06 19:54Z. Client half shipped in
conexus 7.33.0 (published 20:36Z the same day). The `/version` flag is live
through the public edge. The post-cutover reconciliation read found 15 of 16
used plans balanced exactly; the one gap is the pre-cutover orphan row the RDR
had already measured (plan 365), left in place by decision.

---

## What we expected

Four phases, one developer each, each with named tests and falsifiers. P1
would move ten recording arms behind one client function with no wire change.
P2 would add the route and transaction with no schema change. P3 would put the
composite behind a probe. P4 would pair the engine tag with a client release
and read the counters back. The RDR carried twenty-one residuals from four
plan-audit rounds so the implementers would meet them on the page.

## What actually happened

The four phases landed in one day, in order, each passing its stacked review.
The central claim held when measured: one POST per converting call against a
supporting engine, three against a non-supporting one, read from an
instrumented harness rather than from the code. Five things diverged.

**The probe read was specified inside a guard that would have made it dead
for every plan miss.** D5 named the run-start site as the one place the
capability question is asked. That site sits inside `if best.plan_id:`, and a
plan miss (the inline planner's synthetic match) has `plan_id == 0`. Built as
written, every plan miss would have taken the three-POST path forever against
a supporting engine, contradicting D1 and P2's own zero-plan-id test. The
post-acceptance plan audit caught it; D5 was amended to place the read before
the guard, and the falsifier
`test_plan_miss_against_supporting_engine_takes_composite` was added to P3.
This was found before any code was written, which is what the audit rounds are
for.

**P1's choke point silently took the outcome bump off the self-healing
path.** Before P1, the `run_outcome` write went through `_t2_index_write`, so
a connectivity failure reached the classifier that evicts and rebuilds the
shared T2 client. The first P1 commit called `increment_run_outcome` on the
caller's own `db` instead, for all ten arms. No route or payload changed, so
P1's wire-scoped exit criteria stayed green while a transport property was
lost. Both reviewers found it independently. Round 2 restored the independent
`_t2_index_write` call and added a test that pre-warms the singleton, arms a
connection error, and asserts eviction. The round-1 tests could not have
caught this: they passed one shared mock as the database.

**The `/version` flag was dead for cloud clients until a second project
acted.** The public conexus edge trims `/version` to a reviewed allowlist. The
RDR draft did not mention this, although the identical thing had already
happened once with `nx_answer_steps_supported` in 7.14.0. Plan-audit round 2
recorded it as residual A7 and assigned the relay to P4. The relay went out
early at Sam's request, was acknowledged as conexus-3wde, and landed in the
same window as the engine deploy. Without it the client half would have
shipped inert across the whole cloud estate with every local gate green.

**The RDR contradicted itself on where the `use_count` documentation
lived.** D4 said the client-half phase; the Phases section said P4. The plan
followed P4 and named the discrepancy on both beads. Harmless here, but a
reader of D4 alone would have looked for work P3 never did.

**Six pre-existing tests silently took the new branch.** An unconfigured
`MagicMock` method call is truthy, so once the probe existed, tests that had
built `db.telemetry` as a bare mock read "composite supported" and left the
degradation path they were written to pin. Five failed outright; one stayed
green by accident. Each now forces the probe false explicitly.

Two smaller items. P4 split into a tag-independent half (docs, the Risks
bullet for the 404-downgrade tail, the orphan-row recommendation) and a
tag-dependent half, because the engine tag carrying P2 did not exist when P3
closed; residual A8 had predicted that an intervening client release would
move the ledger entry, and 7.33.0 did. And the P2 test data source set
`search_path` on its connection, which Sam's 2026-09-05 directive forbids; the
fold-in removed it and the test still passed, since every reference was
already schema-qualified through generated jOOQ.

## What to check first next time

**Read the guard around the site you are naming.** D5 pointed at a line and
did not say what block it sat inside. When a decision names a code location
as "the one place X happens", quote the enclosing condition in the decision.

**A wire-scoped exit criterion cannot see a transport change.** P1 promised
"no route and no payload changes" and kept that promise while removing a
failure-classification path. When a refactor moves a call from one client
mechanism to another, name the mechanism in the exit criteria, and write the
test against the real mechanism rather than a shared mock.

**Any new `/version` field is invisible to cloud clients until the edge
allowlists it.** This has now happened twice. The relay to conexus belongs in
the RDR at draft time, as a phase deliverable, not as an audit residual.

**Force every capability probe false in tests that predate it.** A bare mock
answers "yes" to any question. When a new probe gates a new branch, grep the
test tree for mocks of the store that carries it before running the suite,
not after.

**Measure the claim in the harness that already exists.** The round-trip
claim was recorded as Assumed until P3's per-path assertion ran; the
before-half instrument was already in `tests/test_nx_answer_t2_fanout_budget.py`.
Look for the existing instrument before designing a measurement.

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 0 | | |
| **Framework API detail** | 1 | `MagicMock` truthiness flipped six tests onto the composite branch | Yes: grep for mocks of the store before running |
| **Missing failure mode** | 0 | | |
| **Missing Day 2 operation** | 0 | | |
| **Deferred critical constraint** | 0 | | |
| **Over-specified code** | 0 | | |
| **Under-specified architecture** | 1 | D5 named the probe site without its enclosing `if best.plan_id:` guard | Yes: source search at draft time |
| **Scope underestimation** | 0 | | |
| **Internal contradiction** | 1 | D4 versus the Phases section on where the `use_count` docs live | Yes: a section cross-read at gate |
| **Missing cross-cutting concern** | 2 | Outcome bump removed from the eviction classifier by P1; edge allowlist for the new `/version` field | Yes: source search (P1); yes, the 7.14.0 precedent was on record (A7) |

### Pattern References

Missing cross-cutting concern, two instances. Both were properties held by
machinery outside the changed code: the T2 write singleton's classifier and
the conexus edge. Neither appears in a diff of the files the RDR names.

## Deferred, not abandoned

- **Standing reconciliation check.** Open research item 3's second half.
  The one-time read is recorded at `nexus_rdr/203-reconciliation-post-cutover`.
  Whether it becomes a scheduled check is Sam's call and is not in any bead.
- **Plan 365's orphan row** (`use_count` 11, 10 successes, 0 failures) stays
  as measured. No `nx plan` verb corrects a single counter and a one-off store
  write is outside the supported path.

## Related

- Research: T2 `nexus_rdr/203-research-1` (orphan rate, 1 in 190),
  `203-research-2` (round trips before and after, measured),
  `203-reconciliation-post-cutover`
- Gate: `nexus_rdr/203-gate-latest`, critiques `nexus/critique-rdr-203-gate-64c4802bc`,
  `nexus/critique-rdr-203-gate-pass-2`
- Plan: `nexus/plan-rdr-203-composite-run-record.md`
- Phase records: `nexus/dev-nexus-dt2tu-1-p1-choke-point`,
  `dev-nexus-dt2tu-2-p2-engine-composite`, `dev-nexus-dt2tu-3-p3-client-half`,
  `dev-nexus-dt2tu-4-p4-tag-independent`, with paired code reviews and
  critiques [24711] [24713] [24714] [24715] [24723] [24725]
- Beads: `nexus-dt2tu` (epic), `.1` to `.4`; origin `nexus-m20mf`; withdrawn
  from RDR-198
- Ship records: `nexus/release-7.33.0-ship-2026-09-06`, engine
  `engine-service-v0.1.105`; edge relay conexus-3wde
