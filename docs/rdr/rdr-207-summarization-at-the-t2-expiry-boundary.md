---
title: "Summarization at the T2 Expiry Boundary — the Manage Phase RDR-057 Cut"
id: RDR-207
type: Feature
status: draft
priority: high
author: conexus (relayed and filed by nexus)
reviewed-by: self
created: 2026-09-12
accepted_date:
related_issues: [conexus-61pz, conexus-j2jf]
related_rdrs: [RDR-057, RDR-131, RDR-132, RDR-194, RDR-128]
---

# RDR-207: Summarization at the T2 Expiry Boundary — the Manage Phase RDR-057 Cut

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** The problem statement, measurements, and failure-mode analysis
below were written by the conexus instance (`conexus-9xjk` / `conexus-61pz`)
and are filed here verbatim at Sam's direction, because the manage-phase code
and RDR-057 both live in this repo. Text is theirs; the prior-art scan, the
house-format sections, and the one correction flagged in Research Findings are
this repo's. T2 is a store shared by both instances, so the measurements are of
a substrate neither owns alone.

## Problem Statement

RDR-057 opened by naming the defect (§Problem Statement, item 1): *"**No
heat-based promotion**: No tracking of which T1 entries are retrieved
frequently. At session close, all unpromoted T1 entries are silently lost
regardless of usage."* Item 2: *"**No consolidation**."*

What shipped inverted it. §RF-3 records the inversion in the RDR's own words:
the cited paper (arxiv 2604.01707) uses `effective_ttl = base_ttl / (1 +
log(access_count + 1))` — relevance-decay, where access *shortens* a tier's TTL
because hot entries graduate upward — and *"Our implementation inverts this for
heat-based survival: `effective_ttl = base_ttl * (1 + log(access_count + 1))` —
highly accessed entries survive longer."* Heat became lifespan-in-place. It
never became graduation. Phase 2 still carries the name "Relevance-Decay
Expiry" over what is a plain `DELETE`.

§RF-8 specified the missing half: the six operations are *"consolidation,
updating, indexing, forgetting, retrieval, compression"*, and *"The manage
phase (between write and read) is where value is created — 'summarize,
deduplicate, score priority, resolve contradictions, and delete when
appropriate.' RDR-057 should specify which operations apply at each tier
boundary rather than treating promotion as a single action."* One of the six
shipped: delete. §RF-12's boundary table states the T2→T3 row as *"store_put |
raw upsert | No consolidation check, no contradiction check"*.

§Cut and Deferred names this RDR's own precondition: **"LLM summarization on
promote() — Cut — No failure mode analysis. If worth building, deserves its own
RDR."** No such RDR was ever written. The deferred siblings were gated on
§Instrumentation — *"If these measurements are near zero, the deferred items
stay deferred"* — which names no bead, phase, or owner, so they stayed deferred
by default rather than by decision. There is no post-mortem for RDR-057, so
nothing revisited the cuts at close.

**This RDR exists to supply the failure-mode analysis RDR-057 named as the
price of admission, and to decide the boundary question §RF-8 asked and the
final design did not answer.**

**One correction to the inherited framing, kept deliberately un-smoothed.** The
cut item was scoped to `promote()`. The boundary where data is actually being
destroyed is EXPIRY, which is not a promote at all. An RDR that inherits the
`promote()` framing will miss the thing that is costing rows.

## Relationship to Prior RDRs

| RDR | Status | Relationship |
| --- | --- | --- |
| RDR-057 Progressive Formalization Across Memory Tiers | closed | **Origin.** It created the expiry this RDR questions, and it named this RDR as the precondition for revisiting the cut. Its stated rationale for cutting — "No failure mode analysis" — has expired: the analysis is below. That is the strongest evidence for reopening, and it is the reason this is a new RDR rather than an amendment. |
| RDR-131 T2 Session Rollup Summaries (MemTree-Lite) | draft | **Adjacent draft.** It specifies the `memory_summaries` shape that design (a) below would consume. Scope boundary: RDR-131 owns WHAT a rollup is and how it is produced for context injection; RDR-207 owns WHETHER expiry may delete a row that no rollup covers. RDR-131 can ship without RDR-207 (summaries with no gate); RDR-207's design (a) cannot ship without RDR-131's shape, so it sequences after. |
| RDR-132 Scope-Routed T1 to T2 Promotion | draft | **Adjacent draft.** Concerns the T1→T2 boundary and namespace scoping; RDR-207 concerns the T2-expiry boundary. They meet only in §RF-8's table, which both should fill in for their own row. No sequencing dependency. |
| RDR-194 Post-RDR-187 FK Census (§A14, "One TTL Semantics") | closed | **Precedent.** It already ruled that unit-less TTL naming is half the reason `0` reads as "no TTL", and unified the semantics rather than accepting a half-measure. RDR-207's out-of-scope item (the `ttl=30`-by-omission default) is the same family of defect at the API surface rather than the schema, so that fix has precedent here rather than needing to argue from first principles. |
| RDR-128 T2 Single-Writer Enforcement | closed | **Context, not overlap.** Its P3 routes the session-end flush and TTL sweep through the T2 daemon because the detached SessionEnd grandchild can outlive the MCP lifespan. That routing is why failure mode 1 below is about execution context rather than about correctness. |

Searched the 208-RDR corpus for: memory, summar, tier, expiry/expire,
consolidat, forget, promot, ttl, relevance. The five above are every hit with
real overlap.

## Context

The manage phase is the work done to memory between writing it and reading it
— summarizing, deduplicating, scoring, resolving contradictions, and deleting.
T2 is this project's middle memory tier: persistent notes that outlive a
session, stored in Postgres behind the engine. A row's TTL (time to live) is
how long it survives without being touched.

Today the only manage-phase operation T2 performs is deletion.

## Research Findings

### Investigation

Measurements taken by conexus against the live hosted T2 (tenant `nexus`) on
2026-09-12. Code claims re-verified in this repo at commit `798bb70e7` before
filing.

### Key Discoveries

**Verified — the sweep is continuous, not a future cliff.**
`MemoryRepository.expire()` has no scheduler; its only trigger is
`POST /v1/memory/expire`, and `src/nexus/hooks.py:440` calls it at **every
session end** — measured at ~3/hour, 20 firings in the 6.3 hours to 22:43Z, one
of which (20:29:59Z) returned a non-empty `deleted_ids`.

**Verified — the population on a clock never opted in.** 5,854 memory rows;
2,374 carried a TTL before today's intervention; 3,480 permanent. **1,265 of
the 2,374 carried `ttl=30` — the MCP `memory_put` default, i.e. what you get by
*omitting* the argument.** Of the 105 rows within days of deletion, 82 carried
it.

**Verified — historical loss is visible in the surviving population.** Rows by
creation month, with the permanent share: 2026-03 → 99%, 04 → 99%, 05 → 89%,
06 → 64%, 07 → 55%, 08 → 32%, 09 → 68%. March is not 99% permanent because
anyone was careful; it is 99% permanent because everything from March that
carried a TTL is already deleted. One March TTL'd row survives, and three from
April. The ratio is immune to the creation-rate growth that also occurred.

**Verified — storage is not the constraint** and should not be argued as one:
the whole table is 71 MB for 5,854 rows.

**Documented — the tourniquet already applied.** 105 rows past ratio 0.90 were
swept to `ttl_days=NULL` on 2026-09-12 under Sam's explicit authorization
(preservation, reversible, nothing deleted). 62 were `project=nexus` and 9
`nexus_rdr`, including `nexus-gmiaf.30-phase2-fixes-2026-06-09.md` at ratio
0.999, which would have gone at the next session end. 2,269 TTL-bearing rows
remain, 175 of them past ratio 0.75; one crossed into that band during the
fifteen minutes the sweep took. This is a tourniquet on a shared store, not a
fix.

**Correction to the relayed draft, verified in this repo.** The relayed
failure-mode 5 said the `relevance_log` purge beside expiry "swallows its
exception and records only the class name". Half right, and the half that is
wrong matters to anyone deciding how bad the precedent is. `T2Database.expire`
(`src/nexus/db/t2/__init__.py:735`) catches the purge's exception, records
`type(exc).__name__` in the `expire_complete` structured event — and **also**
emits `_log.warning("expire_relevance_log_failed", exc_info=exc)`, which
carries the full traceback. So the class-name-only limitation is a property of
the one structured field, not of the logging. The directional point stands
unchanged: the exception is caught, the expiry proceeds regardless, and a
summarization step written in that idiom would fail while the delete still
happened.

## Proposed Solution

### Failure-mode analysis (RDR-057's stated gate)

1. **Session end is the worst available execution context for an LLM call.**
   Expiry fires today from a detached SessionEnd grandchild — RDR-128 P3
   already routes it through the T2 daemon specifically because that grandchild
   can outlive the MCP lifespan. An LLM call there is network-dependent,
   latency-unbounded, runs dozens of times a day, and has no user present to
   see it fail.
2. **Summarize-then-delete is a one-way door.** A bad index can be rebuilt from
   its source. A bad summary whose source row has been deleted cannot. Any
   design must either not delete, or make the summary verifiable *before* the
   delete commits.
3. **A hallucinated summary is worse than no summary.** It is a durable false
   record in the store of record, which later sessions will read and trust. The
   correct failure is losing the row, not silently replacing it with fiction.
4. **Unbounded recurring cost on an unwatched path.** At ~3 sweeps/hour, a
   per-sweep summarization pass is a standing LLM spend on a background job
   nobody is looking at.
5. **Silent failure is the default posture of the code it would live in.** The
   `relevance_log` purge beside expiry already catches its exception and lets
   the expiry proceed (see the correction above for what it does and does not
   record). A summarization step written in that idiom fails quietly and the
   delete still happens — strictly worse than today, because today nothing
   claims to have preserved anything.
6. **Partial-batch semantics are unspecified.** If 40 rows expire and 30
   summarize, deleting the other 10 is data loss with extra steps; keeping them
   makes the sweep non-idempotent and grows a backlog.

### What the analysis implies

**Expiry must not call an LLM.** Every failure mode above is a consequence of
coupling a destructive, frequently-fired, unattended path to a slow, fallible,
costly one.

The shape that survives the analysis inverts the default: **the manage phase
runs on its own cadence and marks rows as rolled-up; expiry may only delete
rows that carry that mark.** Absence of a summary then blocks deletion instead
of permitting it — the same principle this project already applied at the
release-arming gate, where a missing attestation is `NOT-ARMED` and refuses
rather than silently meaning not-required. Absence of a label is never itself a
label.

## Alternatives Considered

Three candidate designs for the decision. This RDR does not recommend one
blind; the choice is the decision it exists to record.

- **(a) Mark-and-gate.** A separate rollup job (RDR-131's `memory_summaries`
  shape is the existing draft) writes summaries and marks source rows.
  `expire()` gains a predicate: delete only what is marked. Rows nobody
  summarized accumulate rather than vanish — visible, and fixable.
- **(b) Quarantine instead of delete.** Expiry moves rows to a cold state
  rather than deleting them; a separate, explicitly-invoked reaper removes
  quarantined rows after a long horizon. Cheapest to build, makes the loss
  recoverable, defers the summarization question without pretending to answer
  it.
- **(c) Promotion on heat, as originally described.** Restore `/` semantics
  from the source paper at a boundary where graduation actually exists. Largest
  change, closest to RDR-057's opening paragraph, and the only one that makes
  "progressive formalization" true.

**Do nothing** remains legitimate: if the answer is "deletion is correct and
summarization is not worth it", that is a valid outcome — but it should be
*decided* with the failure-mode analysis in hand, not inherited from a cut
whose gate nobody owned.

## Trade-offs

Designs (a) and (b) both imply a wire change; (c) may. Flagged explicitly:

| Surface | Change |
| --- | --- |
| Engine | `MemoryRepository.expire()`, `MemoryHandler POST /v1/memory/expire` |
| Client | `src/nexus/hooks.py:440` (session-end trigger), `T2Database.expire` (`db/t2/__init__.py`), `http_memory_store.py`, `commands/memory.py` (`expire_cmd`, `promote_cmd`) |
| Wire contract | **Yes.** Any change to what `POST /v1/memory/expire` does or returns, and any new rollup/mark endpoint, is a wire change. Two of the three candidates imply one. |
| Plugin / marketplace | Not touched. |
| Paired-release choreography | Not touched. |

## Explicitly out of scope

The `ttl=30`-by-omission defect is a separate, much cheaper fix (default to
permanent, or require an explicit ttl) and should not be bundled here — but it
is why this is urgent rather than theoretical, since it is what put 1,265 rows
on a clock nobody set. conexus tracks its half as `conexus-j2jf`. RDR-194 §A14
is the precedent for treating TTL-semantics ambiguity as a defect rather than a
preference.

## Implementation Plan

Not written. This RDR is filed to record the problem, the measurements, and the
failure-mode analysis that RDR-057 required. The plan follows the design
choice, which is not yet made.

## Test Plan

Not written; follows the design choice. One constraint applies to all three
candidates: whatever is built must be shown failing against a deliberately
broken input before it is trusted, per this project's non-vacuity doctrine. A
gate that cannot fail is not a gate, and a preservation mechanism that silently
preserves nothing is the failure mode this RDR exists to prevent.

## Validation

Whatever is chosen, the RDR should state which of §RF-8's six operations apply
at each of T1→T2, T2→T3, and T2-expiry, and the expiry boundary should no
longer be able to destroy a row that no operation has processed.

## Finalization Gate

Not run. Filed as draft.

## References

- RDR-057 §Problem Statement, §RF-3, §RF-8, §RF-12, §Cut and Deferred,
  §Instrumentation — `docs/rdr/rdr-057-progressive-formalization-memory-tiers.md`
- RDR-131 (`memory_summaries` shape), RDR-132, RDR-194 §A14, RDR-128 P3
- `src/nexus/hooks.py:440`; `src/nexus/db/t2/__init__.py:735`;
  `src/nexus/db/t2/http_memory_store.py:481`
- arxiv 2604.01707 (the relevance-decay formula RDR-057 §RF-3 inverted)
- conexus beads: `conexus-61pz` (their half), `conexus-j2jf` (the `ttl=30`
  default)

## Revision History

| Date | Change |
| --- | --- |
| 2026-09-12 | Filed as draft. Text relayed from conexus; prior-art scan, house-format sections, and the failure-mode-5 correction added on filing. Lifecycle transitions are Sam's; nothing here is accepted. |
