---
title: "Heat-Based Graduation Between Memory Tiers"
id: RDR-209
type: Feature
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-09-14
accepted_date:
related_issues: []
related_rdrs: [RDR-057, RDR-207, RDR-131, RDR-132, RDR-194]
---

# RDR-209: Heat-Based Graduation Between Memory Tiers

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Split out of RDR-207 on 2026-09-14 at Sam's direction. RDR-207
decided what T2 expiry may destroy; this RDR takes candidate (c) from that
decision, promotion on heat as RDR-057 originally described it, and treats it
as the design problem it is.

## Problem Statement

RDR-057 opened with "no heat-based promotion" as its first gap and cited a
paper whose method promotes entries on a heat score, `heat =
f(access_frequency, recency)`, with `effective_ttl = base_ttl / (1 +
log(access_count + 1))`: access shortens a tier's TTL because a hot entry has
graduated upward and the lower copy is now redundant. What shipped inverted
the formula (`base_ttl * (1 + log(...))`, RDR-057 §RF-3) so that access
lengthens life in place. Heat became lifespan. It never became graduation.

Three years of the memory literature RDR-057 surveyed agree that the value of
a tiered memory is created at the transformation between tiers, not at the
storage of each tier. Nexus today has the tiers and the heat counter, and no
transformation driven by either. A row that is read fifty times in a month
survives longer in T2 and is otherwise treated exactly like a row read once.

### Enumerated gaps to close

#### Gap 1: Nothing consumes heat except the expiry formula

`nexus.memory` tracks `access_count` and `last_accessed` on every tracked
read, and the only reader of those columns is the heat-weighted TTL in
`expire()` and the idle-days filter in `flagStale`. There is no criterion
that says "this row is hot enough to graduate", no report that lists hot
rows, and no code path that moves one. The fix delivers a graduation
criterion and one consumer of it.

#### Gap 2: Graduation exists only as a manual, one-row verb

`nx memory promote <id> --collection C` copies one T2 row to T3, honouring
its remaining TTL. It requires a person to know the id. There is no batch
form, no heat-driven selection, and no record on the T2 row that it was
promoted. The fix delivers promotion that heat can trigger and a mark that
says it happened.

#### Gap 3: Post-graduation TTL semantics are unspecified

The source paper's division is a rule for what happens to the lower copy
after graduation: it should die sooner, because the upper copy is now the
one of record. Nexus has no rule. A promoted row keeps its T2 TTL, and its
T3 copy inherits the remaining window. The fix delivers a stated rule for
both copies.

#### Gap 4: RDR-057's instrumentation never ran

RDR-057 §Instrumentation gated its deferred items on three measurements and
named no owner, so none were taken. The first question here, what the heat
distribution of the live T2 actually looks like, has never been answered.
The fix delivers the measurement before any threshold is chosen.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-057 Progressive Formalization Across Memory Tiers | Origin | It promised graduation on heat and shipped heat-weighted survival instead; §RF-3 records the inversion in its own words. Its rationale for the inversion ("highly accessed entries survive longer") still holds as a survival rule and is not in conflict with graduation; the two compose. Its §Instrumentation questions are Gap 4 here. |
| RDR-207 Summarization at the T2 Expiry Boundary | Sibling, decided 2026-09-14 | It owns what expiry may destroy: rows are quarantined, and only rows carrying a rollup mark are reaped. This RDR owns what heat may move. Sequencing: RDR-207 ships first. If this RDR promotes at rollup time, it consumes RDR-207's summaries table and mark; if it promotes on a threshold, it needs only the existing `promote` path plus a mark column of its own. |
| RDR-131 T2 Session Rollup Summaries | Abandoned 2026-09-12 | Its consolidation half moved to RDR-207. Nothing here depends on it. |
| RDR-132 Scope-Routed T1 to T2 Promotion | Abandoned 2026-09-12 | It concerned namespace routing at the T1 to T2 boundary, not heat. Its abandon reason applies here as a warning: an unresearched stub is not how an idea stays alive, so this RDR carries its measurement plan in scope. |
| RDR-194 §A14 One TTL Semantics | Precedent | Any post-graduation TTL rule must keep one meaning for `ttl_days` (NULL is permanent, positive is days, zero is rejected). A "shortened" TTL is a smaller positive integer, never a sentinel. |

Searched the 209-RDR corpus for: heat, graduat, promot, access_count, tier.
The five above are every hit with real overlap.

## Context

### Background

Nexus keeps three memory tiers. T1 is session scratch, T2 is persistent notes
in Postgres behind the engine, T3 is permanent knowledge with embeddings. A
row can move from T1 to T2 when it is flagged (the session-end flush writes
flagged entries to T2 as permanent rows) and from T2 to T3 by `nx memory
promote`. Every T2 read that tracks access increments `access_count` and sets
`last_accessed`. That counter is the heat this RDR is about.

### Technical Environment

- Engine: `MemoryRepository` (jOOQ, tenant-scoped), `MemoryHandler`
  (`/v1/memory/*`), Liquibase changelogs `memory-001` to `memory-003`, and
  after RDR-207 `memory-004`.
- Client: `HttpMemoryStore`, `T2Database`, `nx memory` verbs, MCP `memory_*`
  tools, session-end flush in `hooks.py`.
- The T2 to T3 promote path resolves the T3 write endpoint, pre-registers
  the catalog entry, and fires post-store hooks.

## Research Findings

### Investigation

Read on 2026-09-14 at commit 05327a277, during RDR-207's research. Nothing
below has been measured on the live store yet; Gap 4 is the first phase.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `MemoryRepository` access tracking | Yes | `access_count` and `last_accessed` are incremented on tracked get, search and prefix hits; `flagStale` filters on `last_accessed` with a `timestamp` fallback |
| `nx memory promote` | Yes | one row by id; copies to T3 with the remaining TTL as a shrunk `ttl_days`; pre-registers the catalog entry; no mark written back to T2 |
| session-end flush | Yes | flagged T1 entries are written to T2 with `ttl=None`; unflagged entries are not flushed |

### Key Discoveries

- **✅ Verified** (source search) — Heat is tracked and has exactly two
  consumers: the effective-TTL computation in `expire()` and the idle filter
  in `flagStale`. No promotion path reads it.
  *Source: `service/src/main/java/dev/nexus/service/db/MemoryRepository.java`
  (access tracking around lines 109-160 and 365-373; `expire` at 566-598;
  `flagStale` at 601-610).*
- **✅ Verified** (source search) — `nx memory promote` is the only T2 to T3
  path. It is manual, single-row, honours the remaining TTL by shrinking
  `ttl_days` (T3 computes expiry as `indexed_at + ttl_days` and accepts no
  `expires_at`), and writes nothing back to the T2 row.
  *Source: `src/nexus/commands/memory.py:278-345`.*
- **✅ Verified** (source search) — RDR-057's first gap ("all unpromoted T1
  entries are silently lost at session close") is now half true: flagged
  entries are flushed to T2 as permanent rows; unflagged entries are still
  lost. Heat plays no part in which is which.
  *Source: `src/nexus/hooks.py:569-583`.*
- **❓ Assumed** — The live T2 heat distribution is heavy-tailed enough that a
  threshold separates a small hot set from the rest. If most rows have
  `access_count` of zero or one and a few have hundreds, a threshold works;
  if the distribution is flat, heat is not a useful signal and this RDR
  should say so and stop. **This is the load-bearing assumption and it is
  measured in Phase 0 before any design is locked.**
- **⚠️ Documented** — The source paper's formula divides the lower tier's
  TTL by the heat term after graduation. Whether that rule fits a store
  whose T2 rows are mostly permanent (the 2026-09-12 sweep) is not obvious:
  dividing a NULL TTL does nothing. Any rule here has to say what
  graduation means for a permanent row.
  *Source: RDR-057 §RF-3, arxiv 2604.01707.*

### Critical Assumptions

- [ ] A1: heat is a usable signal on the live store (heavy-tailed
  distribution). **Status**: Unverified. **Method**: Spike, Phase 0 census.
- [ ] A2: a promoted row can carry a mark on T2 (`promoted_at`,
  `promoted_to`) without changing any read path. **Status**: Unverified.
  **Method**: Source Search against `MemoryRepository` reads.
- [ ] A3: the T3 side can receive batch promotions through the existing
  `promote` machinery without a new endpoint. **Status**: Unverified.
  **Method**: Source Search, `commands/memory.py` promote path and
  `HttpVectorClient.put`.

## Proposed Solution

### Approach

Measure first, then graduate on a threshold, with the graduation recorded on
the source row and a stated TTL rule for both copies.

**Phase 0, instrumentation.** `nx memory heat [--project P]` prints the
distribution of `access_count` and of days since `last_accessed` for the
tenant: counts per bucket, the top twenty rows by heat, and the share of rows
never read. This answers Gap 4 and assumption A1, and its output is recorded
in this RDR's Research Findings before Phase 1 is planned in detail.

**Phase 1, graduation criterion and mark.** A row graduates when its heat
exceeds a threshold chosen from the Phase 0 measurement (a candidate shape:
`access_count >= N` and `last_accessed` within D days; both from data, not
from this text). Graduation copies the row to T3 through the existing
promote path into a collection named by the caller, and writes `promoted_at`
and `promoted_to` (the T3 document id) on the T2 row. The mark makes the
graduation visible and idempotent: a marked row is not promoted twice.

**Phase 2, post-graduation TTL rule.** For a row with a positive `ttl_days`,
apply the source paper's division at graduation: the T2 copy's `ttl_days`
becomes `ceil(ttl_days / (1 + ln(access_count + 1)))`, minimum one, and the
T3 copy is permanent because it is now the copy of record. For a permanent T2
row, graduation changes nothing about its TTL; the T2 row stays as a
permanent, marked row. That keeps RDR-194's one TTL semantics and makes
the rule say something for both kinds of row.

**Cadence.** Graduation is an attended command, `nx memory graduate
--project P --collection C [--dry-run]`, on the same footing as RDR-207's
rollup: it prints what it will move before moving it. Whether it also runs at
rollup time, promoting hot rows instead of summarizing them, is decided after
Phase 0 and recorded here.

### Technical Design

Deferred until Phase 0 reports. What is fixed now: the mark columns
(`promoted_at TIMESTAMPTZ`, `promoted_to TEXT`) are one Liquibase changeset
on `nexus.memory`; the promote path is reused, not duplicated; every route
added is additive in the wire ledger's sense; no session-end path calls an
LLM or promotes anything.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| heat census | `MemoryRepository` access columns | Reuse: one aggregate query |
| graduation copy | `commands/memory.py` promote path | Reuse: call it per row |
| mark on T2 row | RDR-207's `rolled_up_at` pattern | Extend: sibling columns |
| TTL rule | `expire()` heat formula | Reuse the same heat term |

### Decision Rationale

Graduation is what RDR-057 promised and what the literature it cited
describes. Measuring before choosing a threshold is the lesson of RDR-057's
own §Instrumentation, which named the right questions and no owner. Reusing
the promote path keeps the T3 side untouched.

## Alternatives Considered

### Alternative 1: Recommendation only

**Description**: `nx memory heat` lists hot rows and a person promotes them.

**Pros**: no new write path; nothing can go wrong automatically.

**Cons**: RDR-057's gap stays open; in practice nobody runs it.

**Reason for rejection**: it is Phase 0 of the approach, kept, but not the
whole answer.

### Alternative 2: Promote at rollup time

**Description**: RDR-207's rollup, when it finds a hot row in a group,
promotes it to T3 instead of summarizing it.

**Pros**: one attended command for the whole manage phase.

**Cons**: couples two decisions; a rollup group's summary then omits its
hottest rows, which is the opposite of what a summary should carry.

**Reason for rejection**: not rejected; deferred until Phase 0 says whether
hot rows and quarantined rows even overlap.

### Briefly Rejected

- **Restore the division formula in `expire()` for all rows**: shortens the
  life of hot rows that were never promoted, which is loss, not graduation.
- **Automatic graduation at session end**: RDR-207's failure-mode analysis
  applies unchanged; session end is the wrong context for anything slow.

## Trade-offs

### Consequences

- A row can exist in T2 and T3 at once with a mark saying so; readers of T2
  see the same row they always did.
- The threshold is a number chosen from one census; it will need re-checking
  as the store changes.

### Risks and Mitigations

- **Risk**: heat is a flat signal on this store.
  **Mitigation**: Phase 0 says so and the RDR is abandoned with the
  measurement recorded, which is a valid outcome.
- **Risk**: batch promotion floods a T3 collection with near-duplicate notes.
  **Mitigation**: `--dry-run` first; the mark makes the batch idempotent;
  the collection is named by the caller, never derived.

### Failure Modes

A promote that fails on the T3 side leaves the T2 row unmarked and unchanged,
so a rerun retries it. A mark written without a T3 copy is the silent failure
to guard against: the mark is written only after the T3 write and catalog
registration return, in that order, and the test plan forces the T3 write
to fail and asserts no mark.

## Implementation Plan

### Prerequisites

- [ ] RDR-207 Phase 1 landed (the mark-column pattern and changeset slot).
- [ ] Phase 0 census recorded in Research Findings and A1 resolved.

### Minimum Viable Validation

On a test substrate: three rows with access counts 0, 1 and 50; `nx memory
heat` reports them; `nx memory graduate` with a threshold of 10 promotes
exactly the third, writes its mark, and a second run promotes nothing. A
forced T3 write failure leaves no mark.

### Phase 0: Census

`nx memory heat`; run it against the live tenant; record the distribution
here.

### Phase 1: Criterion and mark

Changeset, `graduate` command over the promote path, mark write after the
T3 write, tests.

### Phase 2: TTL rule

The division rule for positive-TTL rows; tests for both kinds of row.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| promotion marks | `nx memory list --promoted` | on the row | clearing a mark is `nx memory update`, in scope | count in `nx doctor` | PG backup |

### New Dependencies

None.

## Test Plan

- **Scenario**: census on a seeded store — **Verify**: buckets and top rows
  match the seed exactly.
- **Scenario**: graduate at threshold — **Verify**: only rows over the
  threshold are promoted; marks written; T3 documents exist and are
  catalogued.
- **Scenario**: rerun — **Verify**: nothing promoted; marks unchanged.
- **Scenario**: forced T3 failure — **Verify**: no mark, error reported, T2
  row unchanged.
- **Scenario**: TTL rule — **Verify**: a positive-TTL row's `ttl_days`
  shrinks by the stated formula with minimum one; a permanent row is
  unchanged; the T3 copy is permanent.
- **Scenario**: tenant isolation — **Verify**: another tenant's rows are
  neither counted nor promoted.

## Validation

### Testing Strategy

The MVV above in the default suite against the engine substrate; the
forced-failure leg must fail for its stated reason when the mark-ordering
guard is deleted.

### Performance Expectations

Not estimated. The census is one aggregate query; graduation is bounded by
the number of rows over the threshold, which the census reports first.

## Finalization Gate

Not run. Filed as draft; Phase 0 precedes the gate.

## References

- RDR-057 §Problem Statement item 1, §RF-3, §Instrumentation
- RDR-207 §Decision, §Validation table
- `service/src/main/java/dev/nexus/service/db/MemoryRepository.java`
- `src/nexus/commands/memory.py:278-345`; `src/nexus/hooks.py:569-583`
- arxiv 2604.01707 (heat-based promotion and the division formula)

## Revision History

| Date | Change |
| --- | --- |
| 2026-09-14 | Filed as draft, split out of RDR-207 candidate (c) at Sam's direction. Phase 0 census is the first work; no threshold is chosen in this text. |
