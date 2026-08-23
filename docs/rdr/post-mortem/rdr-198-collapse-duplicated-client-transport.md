# Post-Mortem: RDR-198 Collapse the Duplicated Client Transport

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

RDR-198 began as a proposal to collapse fifteen HTTP client classes onto one
pooled transport, add engine endpoints so multi-store operations became one
transaction, and decide a schema boundary — justified by a bead figure
attributing ~90% of `nx_answer`'s cost to per-call client construction. It
closed having shipped a 40-line test guard and nothing else.

## Implementation Status

**Not Implemented — declined on its own evidence.** Every substantive claim was
refuted by research or by the RDR's own acceptance threshold. What shipped is a
guard (`tests/test_constructor_baked_auth_clients_not_shared.py`, commit
`dd074aef6`) pinning the one finding that survived.

---

## What we expected

That fourteen `httpx.Client`s pointed at one host was costing real time, that a
non-atomic multi-store write was producing partial records, that Liquibase was
being used tactically, and that the codebase needed a generated-jOOQ-DSL
discipline built for it.

## What actually happened

Four claims, four refutations, each from a different check.

**The 90% was 0.07%.** The origin bead's figure was real for the harness it came
from — its own text says *"99.4% of that call's **mocked-I/O** wall time"* — and
meaningless for production, where `nx_answer`'s p50 is 80 seconds. Measured
after two interim fixes, the entire remaining fan-out was ~60 ms.

**The atomicity gap was latent, not observed.** The mechanism is real:
`increment_run_started` and `increment_run_outcome` sit in separate `_t2_ctx`
blocks with nothing atomic between them, which makes partial state
arithmetically visible as `use_count > success + failure`. Across all five plans
with `use_count > 0` — 29 runs — the counts balanced exactly. Zero orphans.

**The Liquibase claim was simply false.** The draft said three of 104 changelogs
opt out of transactional DDL via `runInTransaction="false"`. There are **zero**
attribute usages and no `CONCURRENTLY` anywhere; both apparent matches were
prose inside XML comments explaining why those files deliberately avoided the
opt-out. Every changeset already runs in a transaction.

**The jOOQ discipline already existed, and was better than the proposal.**
`RawSqlGateTest` is statement-granular with a sanctioned-exception registry,
assembly sentinels, wrapper call-site tracking, and meta-tests proving the gate
catches what it claims.

**Then the RDR's own threshold killed what was left.** Acceptance gated Layer 1
on `C` = store-construction cost as a percentage of per-file indexing wall time,
with `C < 2%` meaning drop. The measurement found the indexer constructs *no*
stores per file: catalog goes through a refcounted shared handle, T2 has one
call site using the `t2_index_write` singleton with a batched `enqueue_many`, T3
is a module singleton, and `doc_indexer` has zero T2 access. `C ≈ 0`.

## What to check first next time

**Read the factory, not the call site.** The estimate that survived longest —
~20% of per-file indexing cost — came from observing that per-file *hooks* fire
per file and inferring per-file *construction*. Those hooks call factories that
return proxies over shared singletons. **"A per-file call site exists" is not
evidence of "a per-file construction."**

**Never quote a percentage without checking what it was measured against.** The
90% figure survived into a bead title, a draft RDR, and most of a day's work.
One sentence in the bead's own body disclosed the harness.

**Mine the prior-art corpus you are handed.** `/conexus:rdr-create` pre-loads
every existing RDR with title and status. This RDR was drafted claiming a novel
diagnosis while RDR-164 sat in that same table — closed, same diagnosis, fix
shipped, two resulting bugs named by id. Nothing was missing; it was skimmed.
That failure produced a fix to the command itself and a `## Relationship to
Prior RDRs` section in the template.

**Quote sources you are leaning on, then re-read them.** The first gate blocked
partly because this RDR claimed RDR-063's rationale was "void in every clause."
RDR-063 lists six problems; only the first is substrate-specific, and the quote
dropped RDR-063's own *"(forward-looking)… has not been observed as a
bottleneck"* hedge. Selective quotation in the author's own favour.

**Write the falsifiable threshold before you want the answer.** The gate flagged
that Layer 1's go/no-go had no numeric criterion. Adding one — with an explicit
"drop it" branch — is what let the measurement end the work cleanly instead of
becoming a negotiation.

## What shipped instead

Two tests and four comments. The tests pin that `HttpTokenStore` and
`HttpScratchStore` keep their own clients: both bake `Authorization` into the
constructor, so sharing one would send a domain's credential on another domain's
requests. Both were proved red against a simulated regression before being
trusted. One deliberately pins the *premise* — if a store is ever converted to
per-call auth it fails loudly and says to delete the guard for that store, so a
stale guard cannot sit there asserting a shape nobody maintains.

Layer 0 (converting those two to per-call auth) was re-argued after Layer 1 died
and did not clear the bar either: the rebuild it would remove costs 220 ms/day,
and the concurrency hazard it would fix was already found, guarded, and
documented as latent at `nexus-g5hzk` review H1. Rewriting a live auth path that
fires ~25×/day, where a mistake is an outage, for no measured gain.

## Deferred, not abandoned

Recorded so the withdrawn scope stays recoverable:

- **Operation atomicity via engine endpoints.** Mechanism real, no observed
  instance. Needs its own evidence and must sequence against RDR-193, whose
  Part A already designs `catalog_reconcile_commit` as one transaction.
  RDR-164 is its precedent.
- **Schema boundary** (`nexus` / `staging` / `t1`). A real question with no
  evidence gathered. Note RDR-193 Part A grows the `staging` schema, so its
  outcome is an input.

## Was the RDR worth writing?

Yes, and this is the part worth internalising. It cost a day and produced no
feature. It also prevented an engine-side migration on a separate release
lifecycle, justified by a number that was wrong by a factor of about 1,200. The
process worked in the direction it is supposed to work but rarely does — the
author's own proposal was the thing it killed, using a threshold the author
wrote.

## Related

- Research: T2 `nexus_rdr/198-research-1` (atomicity refuted),
  `198-research-2` (per-file construction refuted), `198-research-3`
  (Layer 0 re-argument), `198-research-jooq-liquibase`,
  `198-research-transport-isolation`
- Gate: `198-gate-latest` (PASSED after BLOCKED),
  `198-critique-layer3-gate-2026-08-23`, `198-critique-layer3-regate-2026-08-23`
- Beads: `nexus-m20mf` (origin), `nexus-752bo` (the measurement, closed)
- Interim fixes that removed most of the measurable cost before this RDR
  existed: `888bdee8f` (shared SSL context), `602940b35` (config-parse cache)
