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
related_rdrs: [RDR-057, RDR-131, RDR-132, RDR-194, RDR-128, RDR-209]
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

### Enumerated gaps to close

#### Gap 1: Expiry destroys rows that no manage-phase operation has processed

`expire()` deletes every row past its heat-weighted TTL. Nothing checks
whether the row was summarized, promoted, or even read. The fix delivers an
expiry that cannot delete an unprocessed row: absence of a rollup mark blocks
deletion instead of permitting it.

#### Gap 2: Loss is unrecoverable and invisible

A deleted row leaves no trace. The only evidence of historical loss is the
survival ratio by creation month in the Research Findings. The fix delivers a
cold state a row enters instead of being deleted, a way to list what is there,
and a way to bring a row back.

#### Gap 3: No manage-phase operation exists except delete

Of RDR-057 §RF-8's six operations only forgetting shipped. The fix delivers
the compression operation (a rollup summary that marks its sources) as an
attended command with a verification step, on a cadence that is never session
end, plus a table for the summaries it produces.

## Relationship to Prior RDRs

| RDR | Status | Relationship |
| --- | --- | --- |
| RDR-057 Progressive Formalization Across Memory Tiers | closed | **Origin.** It created the expiry this RDR questions, and it named this RDR as the precondition for revisiting the cut. Its stated rationale for cutting — "No failure mode analysis" — has expired: the analysis is below. That is the strongest evidence for reopening, and it is the reason this is a new RDR rather than an amendment. |
| RDR-131 T2 Session Rollup Summaries (MemTree-Lite) | abandoned (2026-09-12; its live half moved here) | **Adjacent draft, and thinner than it looks.** 123 lines from a single 2026-05-27 stub commit, no beads, nothing implemented, and its own rationale/alternatives/test-plan sections read "to be completed during research". It SKETCHES the `memory_summaries` shape design (a) would consume rather than specifying it. Scope boundary: RDR-131 owns WHAT a rollup is and how it is produced for context injection; RDR-207 owns WHETHER expiry may delete a row that no rollup covers. RDR-131 can ship without RDR-207 (summaries with no gate); RDR-207's design (a) cannot ship without RDR-131's shape, so it sequences after. |
| RDR-132 Scope-Routed T1 to T2 Promotion | draft | **Adjacent draft.** Concerns the T1→T2 boundary and namespace scoping; RDR-207 concerns the T2-expiry boundary. They meet only in §RF-8's table, which both should fill in for their own row. No sequencing dependency. |
| RDR-194 Post-RDR-187 FK Census (§A14, "One TTL Semantics") | closed | **Precedent.** It already ruled that unit-less TTL naming is half the reason `0` reads as "no TTL", and unified the semantics rather than accepting a half-measure. RDR-207's out-of-scope item (the `ttl=30`-by-omission default) is the same family of defect at the API surface rather than the schema, so that fix has precedent here rather than needing to argue from first principles. |
| RDR-128 T2 Single-Writer Enforcement | closed | **Context, not overlap.** Its P3 routes the session-end flush and TTL sweep through the T2 daemon because the detached SessionEnd grandchild can outlive the MCP lifespan. That routing is why failure mode 1 below is about execution context rather than about correctness. |
| RDR-209 Heat-Based Graduation Between Memory Tiers | draft | **Sibling, split out 2026-09-14.** Candidate (c) below (promotion on heat, the thing RDR-057 originally promised) is its own design problem: it needs a graduation criterion and a destination before any boundary question is meaningful. RDR-209 owns graduation; RDR-207 owns what expiry may destroy. RDR-207 ships first and does not depend on RDR-209; RDR-209's rollup-time promotion, if chosen, consumes this RDR's summaries table and mark. |

Searched the 208-RDR corpus for: memory, summar, tier, expiry/expire,
consolidat, forget, promot, ttl, relevance. The five above are every hit with
real overlap.

## Context

### Background

The manage phase is the work done to memory between writing it and reading it
— summarizing, deduplicating, scoring, resolving contradictions, and deleting.
T2 is this project's middle memory tier: persistent notes that outlive a
session, stored in Postgres behind the engine. A row's TTL (time to live) is
how long it survives without being touched; heat (the row's access count)
stretches it.

Today the only manage-phase operation T2 performs is deletion. The problem
was found by conexus, a sibling installation sharing the same hosted store,
when it measured the surviving population on 2026-09-12 (Research Findings)
and saw that older months were "permanent" only because everything else from
those months was already gone.

### Technical Environment

- Engine: `MemoryRepository` (jOOQ over `nexus.memory`, tenant-scoped through
  row-level security), `MemoryHandler` for `/v1/memory/*`, Liquibase
  changelogs `memory-001` to `memory-003`. All DDL goes through Liquibase.
- Client: `HttpMemoryStore` (HTTP client for the memory routes), `T2Database`
  (the facade the hooks and CLI use), `nx memory` verbs, the MCP `memory_*`
  tools, and the session-end hook that flushes flagged scratch and calls
  expire.
- Deployment: the engine ships as its own tagged binary ahead of the client
  release; a wire change is recorded in `docs/wire-contract-pending.md` with a
  direction-safety token that decides whether the engine can deploy first.

## Research Findings

### Investigation

Measurements taken by conexus against the live hosted T2 (tenant `nexus`) on
2026-09-12. Code claims re-verified in this repo at commit `798bb70e7` before
filing; the 2026-09-14 findings were read at `05327a277` and `f8c708d73`.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `MemoryRepository.expire` | Yes | selects `ttl_days IS NOT NULL`, computes the heat-weighted age in Java, deletes; no other predicate (finding 1) |
| `MemoryHandler` expire route | Yes | body ignored, returns `deleted_ids` (finding 2) |
| `HttpMemoryStore.expire` | Yes | reads `deleted_ids` through `.get` with an empty default (finding 2) |
| `MemoryRepository` read paths | Yes | twelve read entry points plus two read-then-write methods, enumerated (finding 4) |
| Liquibase grants and RLS | Yes | service grants are schema-wide with default privileges; RLS is per table and must be copied (finding 4) |
| `memory_put` TTL default | Yes | permanent on omission since 2026-09-12 (finding 3) |

### Key Discoveries

**Verified — the sweep is continuous, not a future cliff.**
`MemoryRepository.expire()` has no scheduler; its only trigger is
`POST /v1/memory/expire`, and `src/nexus/hooks.py:583` calls it at **every
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

**Superseded within hours of filing — read the next paragraph before acting on the numbers above.** On 2026-09-12, after this RDR was filed, every remaining TTL-bearing row in tenant `nexus` was swept to `ttl_days=NULL` under Sam's authorization: 2,269 rows, on top of the 105 below. The tenant's T2 memory is now entirely permanent (total 5,857 unchanged, ttl-bearing 2,269 to 0, verified post-commit in a fresh connection). So "2,269 remain, 175 past 0.75" describes the state at filing and nothing since. The exposure is stopped. The `ttl=30`-by-omission default that produced most of the loss (1,183 of the swept rows carried exactly that) was reversed the same day under Sam's ruling (nexus-473mx, research finding 3 below): omitting `ttl` now means permanent on the MCP tool, the client store and the CLI. The population therefore regenerates only from explicit TTLs, and the boundary question this RDR decides is about those. Reversal records with the exact id-to-ttl mapping are in T2 as `conexus/ttl-full-sweep-reversal-record-2026-09-12` parts 1 and 2, both permanent. Reversal is a decision to discard rather than a rollback: many rows were already past their effective TTL and would delete at the next session end.

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

- **✅ Verified** (source search, 2026-09-14, tree at 05327a277) — Candidate (a)
  does not depend on RDR-131, and its minimal shape is cheap. RDR-131 has been
  `status: abandoned` since 2026-09-12 (its abandon reason says its live half
  moved here); the relationship table above is corrected to say so. Its
  `memory_summaries` shape was one sentence, `memory_summaries(project,
  span_start, span_end, content, entry_ids)`, with no tenant, session, model or
  produced-at column. `nexus.memory` today carries id, tenant_id, project,
  title, session, agent, content, tags, timestamp, ttl_days, access_count,
  last_accessed and fts_vector, and no mark or state column.
  `MemoryRepository.expire()` selects `WHERE ttl_days IS NOT NULL`, computes the
  heat-weighted TTL in Java and deletes; there is no other predicate. The
  minimal shape for (a) is one nullable mark column on `nexus.memory` (for
  example `rolled_up_at`), one added predicate in `expire()`, and a separate
  `nexus.memory_summaries` table (tenant_id, project, source ids, content,
  produced_at, model) modelled on the append-only `aspect_promotion_log`
  pattern: one or two Liquibase changesets and one jOOQ predicate. What is not
  cheap, and not needed for the gate, is the rollup producer, the
  session-end-flush wiring and the drill-down injection RDR-131 sketched. So (a)
  must specify the summaries shape in this RDR; there is no RDR to sequence
  behind. No prior T2 record decides any of this.
  *Source: `docs/rdr/rdr-131-t2-session-rollup-summaries.md:5-6,103-104`;
  `service/src/main/resources/db/changelog/memory-001-baseline.xml:85-97`,
  `memory-003-ttl-days.xml`; `MemoryRepository.java:566-598`;
  `aspects-001-baseline.xml:9,32`. T2 `nexus_rdr/207-research-1`.*

- **✅ Verified for (b), ❓ Assumed for (a)** (source search, 2026-09-14) —
  What the wire change looks like. The current contract is `POST
  /v1/memory/expire` with the body ignored and `{"deleted_ids": [...]}` back
  (`MemoryHandler.java:382-386`, `http_memory_store.py:481-484`). Three real
  callers of the client method: `T2Database.expire` (`db/t2/__init__.py:737`,
  which also logs `memory_deleted` from that list), `hooks.py:583` inside the
  session-end flush-then-expire, and `nx memory expire`. Under (a) the shape can
  stay identical and an old client simply sees fewer ids, which reads
  `[additive]` in the wire ledger's sense (old client plus new engine safe),
  provided the mark write is a separate endpoint the client never needs for
  expire to work. That separation is not yet specified here, so (a)'s
  additivity is assumed, not verified. Under (b) the response changes meaning:
  `deleted_ids` either keeps its name while the rows still exist, or becomes
  `quarantined_ids`, and the session-end path relies on rows actually being
  gone. That is a real narrowing, so (b) is not additive and needs the paired
  non-additive choreography. (b) also adds a new `nx memory` verb; the group has
  put, get, search, list, delete, expire and promote today. No memory-row
  quarantine exists; the term itself already names a T3 lifecycle state (see
  finding 3). Tests pinning the shape:
  `tests/db/test_http_memory_store.py:309`,
  `tests/db/test_t1_cli_dedicated_session.py:152`,
  `tests/db/test_mvv_memory_service.py:592-651`.
  *Source: files above; `docs/wire-contract-pending.md:54-62` for the
  `[additive]` rule. T2 `nexus_rdr/207-research-2`.*

- **✅ Verified** (source search, 2026-09-14) — Two corrections to this RDR's
  own text. First, the `memory_put` TTL default was reversed on 2026-09-12
  under Sam's ruling (nexus-473mx): omitting `ttl` now means permanent on the
  MCP tool, on the client store's two `put` paths, and on `nx memory put`. The
  "population regenerates by omission" sentence above is therefore historical
  as of the day it was written; explicit TTLs still delete, and that is what
  this RDR now decides about. Second, finding 2 originally said the word
  "quarantine" had no hit in this repo. Wrong: a `quarantine-<type>` collection
  prefix is already a lifecycle state owned by the T3 orphan garbage collector
  (nexus-xukbj), reported as the base content type with a flag. No memory-row
  quarantine exists. The term is precedent for the meaning candidate (b)
  wants, a cold state a separate reaper owns, so the design below reuses it.
  *Source: `src/nexus/mcp/core.py:5469-5490`; `src/nexus/db/t2/http_memory_store.py:107,606`;
  `src/nexus/commands/memory.py:25`; `src/nexus/collection_shape.py:168-190`.
  T2 `nexus_rdr/207-research-3`.*

- **✅ Verified** (source search, 2026-09-14) — Assumptions A1 and A3 below.
  `MemoryRepository` has twelve public read entry points (find by project,
  title and id, title resolution, two search overloads, list, project
  prefixes, glob search, tag search, get-all, stale flagging) and two methods
  that read before writing (merge, put-or-merge). Each is a tenant-scoped
  jOOQ query on the memory table, so one shared `quarantined_at IS NULL`
  condition fits all of them; the test that pins this derives the set by
  reflection, not from this sentence. The service role's grants are
  schema-wide with default privileges for future tables, so a new summaries
  table is covered without a per-table line; the diagnostic role's grants are
  per table. Row-level security is per table and does not inherit: the
  summaries table must carry its own copy of the `tenant_isolation` policy.
  *Source: `MemoryRepository.java` public signatures (lines 82-872);
  `grants-nexus-svc.xml:143-147,190-191`; `memory-001-baseline.xml:113-127`.
  T2 `nexus_rdr/207-research-4`.*

### Critical Assumptions

- [ ] A1: every read path in `MemoryRepository` (get, search, list, prefix
  resolution, stale-flagging) can carry a `quarantined_at IS NULL` predicate,
  and the set of read paths is derived by the implementation and pinned by a
  test that inserts a quarantined row and asserts no read returns it.
  **Status**: Verified (finding 4: twelve read entry points, all jOOQ on one
  table). **Method**: Source Search.
- [ ] A5: `MemoryRepository` has seven write entry points; five reach an
  existing row in one of three shapes, and the two explicit deletes are
  named at the end of this item. The shapes: an `ON CONFLICT (tenant_id,
  project, title) DO UPDATE` branch (`upsert`, `importRow`, `importBatch`),
  a content-similarity scan over the project (`putOrMerge`), or explicit ids
  (`mergeMemories`). The rule in the Technical Design is stated per shape,
  not per method, and the test derives the conflict-branch set from the
  repository source rather than from this sentence.
  **Status**: Verified (finding 7, correcting finding 6: the two import
  paths carry the same conflict key as `upsert`; `upsert`'s branch sets
  content, tags, session, agent, timestamp and TTL, the import branches set
  those plus the two access columns, and none sets a state column; the scan
  filters on project and title only; merge updates by id). The repository's
  two remaining write entry points, `delete` by key and `deleteById`, are
  unconditional deletes that predate this RDR and stay outside the
  two-label invariant on purpose: they are the explicit manual override
  (finding 8). **Method**: Source Search.
- [ ] A2: keeping `deleted_ids` with its current meaning (rows actually gone)
  and adding `quarantined_ids` beside it is `[additive]`: an old client reads
  `deleted_ids` only, through `resp.get("deleted_ids", [])`, and ignores the
  new key. **Status**: Verified. **Method**: Source Search
  (`http_memory_store.py:484`, finding 2).
- [ ] A3: the new `nexus.memory_summaries` table takes the same tenant
  row-level-security policy and service grants as `nexus.memory`.
  **Status**: Verified with one correction (finding 4): service grants arrive
  through schema default privileges, the RLS policy must be copied per table,
  and the diagnostic role needs an explicit line if it is to read summaries.
  **Method**: Source Search. The PITR-fork walk before deploy remains the gate
  that catches a grants defect in practice (v0.1.78).
- [ ] A4: a summary can be checked against its sources without an LLM. The
  check is that the summary text contains every source row's title.
  **Status**: Assumed. It is a floor, not a fidelity proof; the test plan
  shows it refusing a summary that omits a title.

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

### Approach

Split the one destructive step into two labelled steps and put the only slow,
fallible step on an attended cadence. Expiry becomes quarantine; a mark
written with a summary is what lets a reaper delete; the summary is produced
by a command a person runs. The decision and the design that carries it are
below.

### Decision (Sam, 2026-09-14)

Candidates (a) and (b) below, composed. Candidate (c) is split out as RDR-209.
"Do nothing" is rejected: the analysis is in hand and it says deletion of an
unprocessed row is wrong.

The composition is this. **Expiry never deletes.** A row past its heat-weighted
TTL enters quarantine (a cold state: hidden from reads, kept in the table).
**Reaping deletes only quarantined rows that carry a rollup mark.** The mark is
written in the same transaction as the summary that covers the row.
**Rollup is an attended command**, never a session-end side effect, and it
marks nothing it cannot verify. Unmarked quarantined rows accumulate, visibly,
until someone rolls them up or restores them. Storage is not a constraint (the
71 MB census above).

Why compose rather than pick: (b) alone defers the summarization question and
still needs a rule for when cold rows die; (a) alone, with no rollup job
running, is "nothing expires", which is the state the tourniquet already
produced. Together, each step of the boundary is gated by a label that has to
be present: quarantine is what expiry does when there is no mark, and the mark
is what lets the reaper act.

### Technical Design

**Schema, one Liquibase changeset** (`memory-004-quarantine-and-rollup.xml`,
after `memory-003-ttl-days.xml` in the master changelog; all DDL through
Liquibase, every reference schema-qualified):

- `nexus.memory` gains two nullable `TIMESTAMPTZ` columns, `quarantined_at`
  and `rolled_up_at`, and a partial index on `(tenant_id, quarantined_at)`
  where `quarantined_at IS NOT NULL`. No existing row changes; no
  `DATA EFFECT:` line is needed.
- `nexus.memory_summaries`: `id BIGSERIAL`, `tenant_id`, `project`, `content
  TEXT NOT NULL CHECK (length(content) > 0)`, `source_ids BIGINT[] NOT NULL
  CHECK (cardinality(source_ids) > 0)`, `produced_at TIMESTAMPTZ NOT NULL
  DEFAULT now()`, `model TEXT NOT NULL`, `produced_by TEXT`. Row-level
  security and grants copy the `nexus.memory` pattern (assumption A3).
  `source_ids` is an array and not a join table with a foreign key on
  purpose: the source rows are meant to be deleted after the summary exists,
  and the id list is provenance of what was summarized, not a live reference.
  RDR-194's foreign-key census applies to live references; this is not one.

**Engine, `MemoryRepository` and `MemoryHandler`:**

- `expire(tenant)` keeps its candidate selection (`ttl_days IS NOT NULL`, the
  heat-weighted age test) but, for rows past their effective TTL with
  `quarantined_at IS NULL`, sets `quarantined_at = now()` instead of
  deleting. It returns two lists: `deleted_ids`, always empty from this
  engine on, kept so an old client keeps reading a valid shape, and
  `quarantined_ids`.
- `reap(tenant)`: `DELETE FROM nexus.memory WHERE quarantined_at IS NOT NULL
  AND rolled_up_at IS NOT NULL`, returning the ids. There is no age horizon
  for unmarked rows; a horizon would reintroduce unlabelled destruction
  (Alternatives, below).
- `restore(tenant, id)`: clears `quarantined_at`, clears `rolled_up_at`, and
  sets `ttl_days` to `NULL`. Restoring is a decision to keep the row, so it
  becomes permanent rather than re-entering the same clock, and it carries no
  label from its earlier cycle: if it is ever given a TTL again and
  re-quarantined, it must be summarized again before a reap can touch it.
- `insertSummary(tenant, project, content, sourceIds, model, producedBy)`:
  in one transaction, inserts the summary row and sets `rolled_up_at = now()`
  on every source row. Refuses, writing nothing, if any source id does not
  exist in that tenant and project. A row already marked may be covered
  again; its mark timestamp moves.
- Every read path (`get`, `search`, `list`, prefix resolution, the stale
  sweep) adds `quarantined_at IS NULL` (assumption A1). A quarantined row is
  therefore invisible exactly as a deleted row was, which keeps the meaning
  of the TTL its author set. `listQuarantined(tenant, project)` is the one
  read that sees them.
- Write paths that can reach an existing row (assumption A5) each state
  what they do to a quarantined one, by shape. Conflict branch (`upsert`,
  `importRow`, `importBatch`): a write that names an existing title is a
  decision to keep that title with the written content, so every `DO
  UPDATE` branch on the title key also sets `quarantined_at` and
  `rolled_up_at` to `NULL` in the same statement; the row comes back with
  the TTL the write gave it, where `restore` makes the row permanent because
  it has no TTL argument and a write names its own. Similarity scan (`putOrMerge`): the scan adds
  `quarantined_at IS NULL`, so a hidden row is never a merge target; its
  same-title branch is the conflict branch above. Explicit ids
  (`mergeMemories`): refuses when the kept id or any deleted id names a
  quarantined row, so the caller restores first; refusal is the safe
  direction and the ids are explicit. A test greps the repository source
  for every conflict branch and asserts each carries the two clears, then
  drives each of the three through a quarantined row.
- Routes, all new or additive: `POST /v1/memory/expire` (response gains
  `quarantined_ids`), `POST /v1/memory/reap`, `POST /v1/memory/{id}/restore`,
  `GET /v1/memory/quarantined?project=`, `POST /v1/memory/summaries`,
  `GET /v1/memory/summaries?project=`. Each carries an `[additive]` wire-ledger
  entry: an old client keeps working against the new engine, so the engine
  deploys before the client tag (the nexus-1emxn additive branch).

**Client, `HttpMemoryStore`, `T2Database`, `nx memory`, MCP:**

- `expire()` returns both lists; `T2Database.expire` logs
  `memory_quarantined` beside `memory_deleted`; the session-end message says
  "quarantined N", not "expired N".
- New verbs: `nx memory reap`, `nx memory restore <id>`, `nx memory list
  --quarantined [--project]`, `nx memory summaries [--project]`, `nx memory
  rollup --project P [--dry-run]`.
- `nx doctor` gains an informational row: quarantined rows without a mark,
  per tenant. Not applicable on a virgin box.
- MCP `memory_get` and `memory_search` do not change shape. Quarantined rows
  are hidden by the engine, and summaries are not injected into reads here;
  retrieval-time use of summaries is RDR-209's rollup-time question, or a
  later RDR.

**Rollup producer, `nx memory rollup`:**

Selects quarantined, unmarked rows for one project, groups them by creation
month, and for each group asks the existing operator dispatch (the same
`claude -p` path `operator_summarize` uses) for one summary. Before marking,
it checks the summary against its sources with the deterministic floor in
assumption A4: every source title must appear in the summary text. A group
that fails the check is reported and left unmarked. A group that passes is
sent to `POST /v1/memory/summaries`, one transaction per group, so a failure
in group 3 leaves groups 1 and 2 marked and group 3 quarantined (this is the
partial-batch rule failure mode 6 asked for). `--dry-run` prints the groups
and summaries and writes nothing. The command is attended: it runs when
someone runs it, and its cost is the number of groups it prints.

**The six failure modes, revisited against this design:**

1. Session end: the LLM call moved to an attended command. Session end still
   only quarantines, which is one `UPDATE`.
2. One-way door: the delete is a separate explicit step (`reap`) after the
   mark, so a bad summary can be caught between the two, and `restore` exists.
3. Hallucinated summary: the summary is a separate row that never replaces
   its sources, the title-containment check refuses the worst cases, and the
   sources remain until someone reaps.
4. Unbounded cost: none on the background path; the attended command prints
   what it will spend before spending it.
5. Silent failure: a producer failure means no mark, and no mark means no
   deletion. The safe direction is the default.
6. Partial batch: per-group transactions; unmarked rows stay quarantined.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| quarantine state | `collection_shape.py` `quarantine-<type>` (T3 orphan GC) | Reuse the term and the meaning; different table, no shared code |
| summaries table | `aspects-001-baseline.xml` `aspect_promotion_log` (append-only log) | Reuse the pattern (append-only, tenant RLS), new table |
| rollup dispatch | `operator_summarize` dispatch path | Reuse the dispatch; new command around it |
| expire trigger | `hooks.py:583` `_flush_and_expire` | Extend: same call, new return shape |
| restore semantics | `nx memory put --ttl permanent` | Reuse the permanent sentinel (`ttl_days NULL`) |

## Alternatives Considered

Three candidate designs were on the table when this RDR was filed. The
decision above chose (a) and (b) composed and moved (c) to RDR-209; the
candidates are kept as written so the choice is readable against them.

- **(a) Mark-and-gate.** A separate rollup job (RDR-131 sketches a
  `memory_summaries` shape in one sentence; it is a stub, not a
  specification, so this candidate requires that design to be written)
  writes summaries and marks source rows.
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

**Do nothing** remained legitimate until decided: if the answer were "deletion
is correct and summarization is not worth it", that would be a valid outcome.
It was decided against on 2026-09-14 with the analysis in hand.

### Briefly Rejected

- **Reap with an age horizon for unmarked rows** (delete anything quarantined
  longer than N days): reintroduces exactly the unlabelled destruction Gap 1
  names, one step later.
- **Quarantined rows stay readable**: then quarantine is indistinguishable
  from permanent, and the TTL an author set means nothing.
- **Summaries with a foreign key to their sources**: the sources are meant to
  be deleted; a cascading key would erase the provenance the summary exists
  to keep.
- **Rename `deleted_ids` to `quarantined_ids`**: not additive (finding 2); an
  old client would read rows as gone that are not.

## Trade-offs

### Consequences

- Nothing in T2 is deleted by the automatic path without two labels on it,
  one from expiry and one from a summary; an explicit `nx memory delete`
  remains the manual override. That is the point, and it is also a standing
  population of cold rows until someone rolls them up.
- Every read path carries one more predicate and every write path that can
  reach an existing row by key, scan or merge carries a stated rule, with
  the two explicit deletes as the override. Cheap per query, but a read
  path added later that forgets the predicate silently resurrects cold rows,
  and a write path added later that forgets its rule silently writes into
  them; the reflection-driven test pins the read set, the source-grep test
  pins the conflict-branch set, and the scan and id paths are tested by
  name.
- The session-end message changes wording; a person reading "quarantined 3"
  learns something that "expired 3" hid.
- A summary is a separate row, so the store grows by summaries rather than
  shrinking by deletions until reap runs. Storage is not a constraint.

### Risks and Mitigations

- **Risk**: nobody runs rollup, so quarantine fills and the store is
  effectively permanent with a hidden tail.
  **Mitigation**: the `nx doctor` row reports the count; that is visible
  where today's loss was invisible, and it is the safe direction.
- **Risk**: the title-containment check passes a summary that is wrong in
  substance.
  **Mitigation**: it is a floor (assumption A4); the sources stay until a
  separate reap, and restore exists. A stronger check is a follow-up, not a
  reason to keep deleting unprocessed rows today.
- **Risk**: an old client on a new engine reads `deleted_ids` as empty and
  reports "expired 0" while rows were quarantined.
  **Mitigation**: accurate, if uninformative; nothing is lost, and the client
  half ships in the next release.
- **Risk**: the new table's grants or policy are wrong on production.
  **Mitigation**: the PITR-fork rehearsal before deploy, the gate that caught
  v0.1.78.

### Surfaces touched

Both halves imply a wire change. Flagged explicitly:

| Surface | Change |
| --- | --- |
| Engine | `MemoryRepository.expire()`, `MemoryHandler POST /v1/memory/expire` |
| Client | `src/nexus/hooks.py:583` (session-end trigger), `T2Database.expire` (`db/t2/__init__.py`), `http_memory_store.py`, `commands/memory.py` (`expire_cmd`, `promote_cmd`) |
| Wire contract | **Yes, all `[additive]`.** `POST /v1/memory/expire` gains `quarantined_ids` and keeps `deleted_ids`; five new routes. An old client keeps working against the new engine. |
| Plugin / marketplace | Not touched. |
| Paired-release choreography | Additive branch: engine deploys before the client tag; no arming needed. |

## Explicitly out of scope

The `ttl=30`-by-omission defect was a separate, much cheaper fix and was not
bundled here. The nexus half landed on 2026-09-12 (nexus-473mx, finding 3);
conexus tracks its own copies as `conexus-j2jf`. RDR-194 §A14 is the precedent
for treating TTL-semantics ambiguity as a defect rather than a preference.
Also out of scope: injecting summaries into `memory_get`/`memory_search`
results, and any automatic promotion of hot rows (RDR-209).

## Implementation Plan

### Prerequisites

- [ ] A1 and A3 verified by source search at the start of Phase 1.
- [ ] Sam's acceptance of this RDR (lifecycle transitions are Sam's).

### Minimum Viable Validation

On a test substrate: put a row with `ttl=1` and a backdated timestamp; run
expire; assert the row is in `quarantined_ids`, not in `deleted_ids`, absent
from get, search and list, present in the quarantined list; restore it and
assert it reads back permanent. Then, with a stubbed summarizer that returns
a summary naming the source title: rollup marks it, reap deletes it, and the
summaries list carries its id. Then the forced failure: a stubbed summarizer
that omits the title marks nothing, and reap deletes nothing. All three legs
run in one test module against the engine substrate, in the default suite.

### Phase 1: Engine

1. Changeset `memory-004-quarantine-and-rollup.xml` with the two columns, the
   partial index, the summaries table, its RLS policy and grants; included
   in the master changelog after `memory-003`.
2. `MemoryRepository`: `expire` returns the two-list result; `reap`,
   `restore`, `insertSummary`, `listQuarantined`, `listSummaries`; the
   `quarantined_at IS NULL` predicate on every read path, with a test that
   inserts a quarantined row and walks every public read method; the
   write-path rules by shape (every conflict branch on the title key,
   `upsert`, `importRow` and `importBatch`, clears both stamps;
   `putOrMerge`'s scan excludes quarantined rows; `mergeMemories` refuses on
   a quarantined id), with the source-grep test over conflict branches.
3. `MemoryHandler`: the six routes; `handleExpire` emits both keys.
4. Wire ledger: one `[additive]` entry per commit touching the surface.
5. Full Java suite (schema change), `SchemaMigratorIntegrationTest` walk, the
   PITR-fork rehearsal before deploy (this changeset adds a table with grants,
   the surface the v0.1.78 defect came from).

### Phase 2: Client

1. `HttpMemoryStore.expire` returns both lists; `T2Database.expire` logs
   both counts; the session-end message names quarantine.
2. `nx memory reap|restore|list --quarantined|summaries`; `nx doctor` row.
3. Contract tests in `tests/db/test_http_memory_store.py` and the MVV module
   above; `tests/db/test_mvv_memory_service.py::TestMVVExpire::test_expire_ttl`
   is rewritten to the quarantine semantics (it asserts the row is in
   `deleted_ids` and gone after expire today); the CLI reference gains the
   verbs.

### Phase 3: Rollup producer

1. `nx memory rollup --project P [--dry-run]` with month grouping, the
   operator dispatch, the title-containment check, per-group transactions.
2. Tests use a stubbed dispatcher (deterministic); one test forces the
   check to fail and asserts no mark and no reap; one forces the dispatch to
   raise on group 2 of 3 and asserts groups 1 and 3 outcomes.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| quarantined memory rows | `nx memory list --quarantined` | `nx memory get` does not show them by design; the list shows title, project, quarantined_at | `nx memory reap` (marked only); `nx memory delete <id>` for an explicit single deletion | `nx doctor` row | PG backup, as `nexus.memory` |
| `nexus.memory_summaries` | `nx memory summaries` | by id, same verb | Deferred: a summary is provenance for rows the reaper deleted, so removing one is a data-hygiene decision an operator takes in SQL; no verb is promised | count in `nx doctor` row | PG backup |

### New Dependencies

None.

## Test Plan

- **Scenario**: row past effective TTL, unmarked, expire runs — **Verify**:
  `quarantined_ids` names it, `deleted_ids` is empty, the row exists with
  `quarantined_at` set.
- **Scenario**: quarantined row, every read path — **Verify**: get by id, get
  by title, prefix resolution, search, list and flag-stale all omit it; the
  quarantined list includes it. The read paths are enumerated by the test
  from the repository's public methods, not typed by hand.
- **Scenario**: reap with unmarked quarantined rows only — **Verify**: deletes
  nothing, returns an empty list.
- **Scenario**: reap with a marked quarantined row and an unmarked one —
  **Verify**: exactly the marked id is deleted.
- **Scenario**: insertSummary with one unknown source id — **Verify**: refused,
  no summary row, no mark on the known ids.
- **Scenario**: restore — **Verify**: `quarantined_at` null, `rolled_up_at`
  null, `ttl_days` null, row readable.
- **Scenario**: quarantine, rollup marks, restore, a put gives the row a new
  TTL, expire re-quarantines it, reap — **Verify**: reap deletes nothing; the
  row is quarantined and unmarked.
- **Scenario**: put on a title that names a quarantined, marked row —
  **Verify**: the row is readable again with the new content and the put's
  TTL, `quarantined_at` and `rolled_up_at` both null, same id.
- **Scenario**: importRow, and separately importBatch, with a row whose
  title names a quarantined, marked row — **Verify**: the row is readable
  again with the imported content and fidelity fields, both stamps null,
  same id.
- **Scenario**: putOrMerge with a quarantined row as the only near-duplicate
  — **Verify**: a new row is created; the quarantined row is untouched and
  still hidden.
- **Scenario**: mergeMemories with a quarantined id among the deletes, and
  again as the kept id — **Verify**: refused both times, nothing written.
- **Scenario**: old-client shape — **Verify**: a client reading only
  `deleted_ids` parses the new response (the existing contract test at
  `tests/db/test_http_memory_store.py:309` keeps passing unchanged).
- **Scenario**: rollup, summary omits a source title — **Verify**: group
  reported as failed, no mark, no summary row.
- **Scenario**: rollup, dispatch raises on group 2 of 3 — **Verify**: groups 1
  and 3 marked with summary rows, group 2 quarantined and unmarked.
- **Scenario**: tenant isolation — **Verify**: a second tenant's quarantined
  rows and summaries are invisible to the first.

## Validation

Which of §RF-8's six operations apply at each boundary after this RDR:

| Boundary | Operations | Status |
| --- | --- | --- |
| T1 to T2 | updating (flagged entries flushed permanent at session end) | shipped, unchanged here |
| T2 expiry | forgetting, split into quarantine (this RDR) and reap (marked rows only) | this RDR |
| T2 rollup | compression (summary rows), with marking | this RDR, attended |
| T2 to T3 | updating (`nx memory promote`, manual) | unchanged; graduation is RDR-209 |
| any | consolidation, contradiction resolution | not addressed; RDR-057's deferred items remain deferred |

Done means: the MVV passes in the default suite, the forced-failure legs fail
for the reason stated when their check is deleted, and no read path returns
a quarantined row.

## Finalization Gate

### Contradiction Check

One tension, stated rather than smoothed: the Research Findings' urgency
argument was written when the omission default still put rows on a clock,
and finding 3 records that the default was reversed the same day. The
decision does not rest on urgency; it rests on the failure-mode analysis and
on the survival-by-month measurement, which is history that the default
reversal does not change. No contradiction between the findings, the six
failure modes, and the composed design: each failure mode is answered by a
named part of the design (§Technical Design, "revisited").

### Assumption Verification

A1, A2 and A3 are verified by source search (findings 2 and 4). A4, that a
summary can be checked without an LLM, is assumed and stated as a floor; the
test plan shows the check refusing a summary that omits a title, which is
the behaviour the design needs, not a fidelity proof. It is carried into
implementation as assumed, with the reap step and restore as the backstop.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `POST /v1/memory/expire` response shape | engine `MemoryHandler` | Source Search |
| `HttpMemoryStore.expire` parsing | client | Source Search |
| `MemoryRepository` read paths | engine (jOOQ) | Source Search, enumerated |
| service grants and RLS | Liquibase changelogs | Source Search |
| operator dispatch for rollup | `operator_summarize` path | Documented (existing, not re-read here); Phase 3 reads it |

### Scope Verification

The Minimum Viable Validation is in scope: one test module against the
engine substrate in the default suite, with the quarantine leg, the
rollup-mark-reap leg and the forced-failure leg. Summary injection into
reads and heat graduation are out of scope and named as such.

### Cross-Cutting Concerns

- **Versioning**: engine half ships in the next engine tag; every wire entry
  is `[additive]`, so the engine deploys before the client tag.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: one changeset with a new table and policy; the
  PITR-fork rehearsal before deploy.
- **IDE compatibility**: N/A.
- **Incremental adoption**: an old client works unchanged; new verbs are
  additive; rollup is opt-in by invocation.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: N/A; the rollup command prints its groups before
  spending.

### Proportionality

Right-sized for a schema change and six routes. The relayed problem
statement and measurements are longer than the design and are kept whole
because they are the evidence; the superseded measurement paragraph is kept
rather than edited away for the reason stated in its revision note.

## References

- RDR-057 §Problem Statement, §RF-3, §RF-8, §RF-12, §Cut and Deferred,
  §Instrumentation — `docs/rdr/rdr-057-progressive-formalization-memory-tiers.md`
- RDR-131 (`memory_summaries` shape), RDR-132, RDR-194 §A14, RDR-128 P3
- `src/nexus/hooks.py:583`; `src/nexus/db/t2/__init__.py:735`;
  `src/nexus/db/t2/http_memory_store.py:481`
- arxiv 2604.01707 (the relevance-decay formula RDR-057 §RF-3 inverted)
- conexus beads: `conexus-61pz` (their half), `conexus-j2jf` (the `ttl=30`
  default)

## Revision History

| Date | Change |
| --- | --- |
| 2026-09-12 | Filed as draft. Text relayed from conexus; prior-art scan, house-format sections, and the failure-mode-5 correction added on filing. Lifecycle transitions are Sam's; nothing here is accepted. |
| 2026-09-12 | Research Findings amended hours after filing: the full tenant sweep to permanent landed, so the "2,269 remain" measurement is now historical. Recorded rather than edited away — a record that quietly drops a number a reader would re-measure teaches them to distrust the rest of it, and the measurement is still the evidence for why the boundary needs deciding. Also corrected the RDR-131 characterisation: it is a 123-line never-researched stub whose design sections read "to be completed during research", not an existing specification of the `memory_summaries` shape, so candidate (a) requires that design to be WRITTEN rather than merely sequenced behind RDR-131. |
| 2026-09-14 | Research Findings 1 and 2 added (T2 `207-research-1`, `207-research-2`): RDR-131 is abandoned, so candidate (a) must specify its own summaries shape; the wire change is assumed additive under (a) and verified non-additive under (b). Two stale facts corrected from those findings: RDR-131's status in the relationship table, and the session-end trigger line (`hooks.py:583`, not 440). |
| 2026-09-14 | Sam's decision recorded: candidates (a) and (b) composed, (c) split out as RDR-209, do-nothing rejected. Gap headings, technical design, implementation plan, test plan, validation table and critical assumptions written. Finding 3 added (T2 `207-research-3`): the `ttl=30` omission default was reversed 2026-09-12 (nexus-473mx), and `quarantine` already names a T3 lifecycle state; the out-of-scope section and the superseded-measurements paragraph corrected accordingly. |
| 2026-09-14 | Gate round 1 — BLOCKED (1 Critical, 1 Significant, 1 ship-blocker(s)); commit `abd90979f`; critique `nexus_rdr/207-gate-critique-2026-09-14`. |
| 2026-09-14 | Gate round 2 — BLOCKED (1 Critical, 0 Significant, 1 ship-blocker(s)); commit `7e23eb65e`; critique `nexus_rdr/207-gate-critique-2026-09-14b`. |
| 2026-09-14 | Gate round 3 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); commit `5710ac384`; critique `nexus_rdr/207-gate-critique-2026-09-14d`. |
| 2026-09-14 | Accept: the three round 3 residuals dispositioned by file change, commits `9124a2be7` (delete override named, summaries-delete verb struck, A5 import-branch wording) and `007aac28d` (A5 count); fix checks `nexus_rdr/207-fix-check-9124a2be7` and `nexus_rdr/207-fix-check-007aac28d`, both PASS. |
