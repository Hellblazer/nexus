---
title: "Heat-Based Graduation Between Memory Tiers"
id: RDR-209
type: Feature
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-09-14
accepted_date: 2026-11-16
related_issues: ["nexus-2k66t.1", "nexus-qo7ie"]
related_rdrs: [RDR-057, RDR-207, RDR-131, RDR-132, RDR-194, RDR-238, RDR-240]
---

# RDR-209: Heat-Based Graduation Between Memory Tiers

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Split out of RDR-207 on 2026-09-14 at Sam's direction. RDR-207
decided what T2 expiry may destroy; this RDR takes candidate (c) from that
decision, promotion on heat as RDR-057 originally described it, and treats it
as the design problem it is.

**Scope, narrowed 2026-11-16.** Sam approved narrowing this RDR in the draft
triage of 2026-11-16 (T2 `nexus/draft-rdr-triage-2026-11-16`). The in-scope
work is two pieces: **Phase 0b**, a read counter that counts only the rows a
caller actually received, and **Phase 3 Steps 1 to 3**, a pointer record in T3
for a closed phase's gate records, a loss check, and then a 30-day TTL on those
records. Both fix a live problem and neither needs a threshold. **Phases 1 and
2** (the graduation criterion, the mark on the T2 row, and the post-graduation
TTL rule) keep their text below and are **deferred**. The trigger that revives
them is: *a re-census on the Phase 0b counter shows a usable tail*. Nothing in
the in-scope plan depends on them. Accepting this RDR therefore graduates no
row from T2 to T3; the title describes the deferred half.

Three pieces are already shipped and are recorded as such, not planned:
RDR-207's `rolled_up_at` mark, Phase 3 Step 0 (the gate-record tags), and Phase
3 Step 4 (the census flag in `nx memory curate --t2`). See "Already shipped".

**Terms used below.** *T2* is the persistent notes store (Postgres, behind the
engine, the Java service); *T3* is the permanent store with embeddings. *Heat*
is the pair of T2 columns `access_count` and `last_accessed`. A *gate record*
is the written verdict a reviewer leaves for a unit of work. *Quarantine* is
RDR-207's hidden-but-kept state for an expired row. The *wire ledger* is
`docs/wire-contract-pending.md`, which lists changes where the engine and the
client must stay compatible while only one of them has been released.

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

The census of 2026-09-14 then showed that the counter does not measure reads,
which makes every heat-based step premature. That finding (Gap 5) and the
unexpiring gate records (Gap 6) are what this RDR now fixes. The graduation
gaps (1 to 4) stay on record and are deferred.

### Enumerated gaps to close

#### Gap 1: Nothing consumes heat except the expiry formula

Status: deferred with Phases 1 and 2.

`nexus.memory` tracks `access_count` and `last_accessed` on every tracked
read, and the only readers of those columns are the heat-weighted TTL in
`expire()` and the idle-days filter in `flagStale`
(`MemoryRepository.java:679-709` and `:914`). There is no criterion that says
"this row is hot enough to graduate", no report that lists hot rows, and no
code path that moves one. The fix delivers a graduation criterion and one
consumer of it.

#### Gap 2: Graduation exists only as a manual, one-row verb

Status: deferred with Phase 1.

`nx memory promote <id> --collection C` copies one T2 row to T3, honouring
its remaining TTL (`src/nexus/commands/memory.py:527-666`). It requires a
person to know the id. There is no batch form, no heat-driven selection, and
no record on the T2 row that it was promoted. The fix delivers promotion that
heat can trigger and a mark that says it happened.

#### Gap 3: Post-graduation TTL semantics are unspecified

Status: deferred with Phase 2.

The source paper's division is a rule for what happens to the lower copy
after graduation: it should die sooner, because the upper copy is now the
one of record. Nexus has no rule. A promoted row keeps its T2 TTL, and its
T3 copy inherits the remaining window. The fix delivers a stated rule for
both copies.

#### Gap 4: RDR-057's instrumentation never ran

Status: answered once by hand; the repeatable measurement is deferred with
Phase 1.

RDR-057 §Instrumentation gated its deferred items on three measurements and
named no owner, so none were taken. The first question, what the heat
distribution of the live T2 looks like, was answered by hand on 2026-09-14
(T2 `nexus_rdr/209-research-1`). A repeatable `nx memory heat` command is not
built and is not needed for the in-scope work: the re-census that revives
Phases 1 and 2 reuses the queries recorded in that research entry.

#### Gap 5: The heat counter measures search matches, not reads

Status: in scope, Phase 0b.

Found by the Phase 0 census (Research Findings). The engine's memory search
increments `access_count` on every row that matches the query, not on the
rows the caller sees, so the counter is dominated by how often a row's words
appear in other people's searches. The MCP `memory_search` tool fetches every
match and slices its page afterwards in Python (`src/nexus/mcp/core.py:8252-8258`),
so a page of twenty can stamp two hundred rows. This counter also feeds the
heat-weighted TTL that RDR-057 shipped (`MemoryRepository.java:697`): a row
survives longer for matching common searches. Two accepted or closed RDRs
wait for a better counter: RDR-238's index audit and RDR-240's transcript
queries use a transcript count until it lands. The fix delivers a counter
that means "a caller received this row".

#### Gap 6: Gate records never enter expiry (Amendment A1, 2026-10-31)

Status: in scope, Phase 3 Steps 1 to 3 (Steps 0 and 4 shipped).

Reviewers write their own gate records to T2 (`nexus/review-*`,
`nexus/critique-*`, `nexus/confirm-*`, `nexus/fixround-*`), about 900 to 1,150
a month and about 2,500 in all, with no TTL. RDR-207's quarantine and rollup
act only on rows that carry one, so none of them ever reaches it. The 30-day
read pattern says they are working state: 1,718 gets against 1,066 puts, and
of the reads whose title carries a date, 91% came within two days and 27 came
after a week (memory survey, 2026-10-31). The papers warn against rolling them
up without a check: memory construction loses information (Memory Tax), and
a compaction nobody checks can fall below the no-context baseline (Agentic
Context Management). The fix delivers a phase-close rollup that keeps each
record's full text in T3 and lists the records in a pointer, and a TTL on the
records it covers.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-057 Progressive Formalization Across Memory Tiers | Origin | It promised graduation on heat and shipped heat-weighted survival instead; §RF-3 records the inversion in its own words. Its rationale for the inversion ("highly accessed entries survive longer") still holds as a survival rule and is not in conflict with graduation; the two compose. Its §Instrumentation questions are Gap 4 here. |
| RDR-207 Summarization at the T2 Expiry Boundary | Sibling, closed 2026-09-15 | It owns what expiry may destroy: rows are quarantined, and only rows carrying a rollup mark are reaped. Its prerequisite for this RDR is met: the `rolled_up_at` mark exists (`MemoryRepository.java:722`, `:751-756`, `:859`). Phase 3 consumes its quarantine, rollup and reap path unchanged. If the deferred Phase 1 later promotes at rollup time it consumes the same mark. |
| RDR-238 Memory Curation | Consumer, accepted 2026-11-01 | Its index audit would use Phase 0b's counter for T2 reads; until then it counts reads with RDR-240's `memory` transcript query. It built the `nx memory curate --t2` command that carries Step 4's census flag (shipped). It reads this RDR's Phase 3 close-marker rule. |
| RDR-240 Procedures as Sandboxed Lua Scripts | Consumer, closed 2026-11-16 | Its `gate_record` procedure writes the tags and the field header Phase 3 reads (Step 0, shipped). Its transcript queries are the read-count source until Phase 0b ships. |
| RDR-131 T2 Session Rollup Summaries | Abandoned 2026-09-12 | Its consolidation half moved to RDR-207. Nothing here depends on it. |
| RDR-132 Scope-Routed T1 to T2 Promotion | Abandoned 2026-09-12 | It concerned namespace routing at the T1 to T2 boundary, not heat. Its abandon reason applies here as a warning: an unresearched stub is not how an idea stays alive, so this RDR carries its measurement plan in scope. |
| RDR-194 §A14 One TTL Semantics | Precedent | Any post-graduation TTL rule must keep one meaning for `ttl_days` (NULL is permanent, positive is days, zero is rejected). A "shortened" TTL is a smaller positive integer, never a sentinel. Phase 3's 30-day TTL is an ordinary positive integer. |

Bead nexus-2k66t.1 (automatic memory capture with an attended consolidation
step) was deferred behind this RDR. Its trigger was first recorded as "RDR-209
accepted". The triage restated it, and the bead's 2026-11-16 comment records
the restatement: **undefer when RDR-209 Phase 1 is built**. A narrowed accept
of this RDR does not release it, because the narrowed scope graduates nothing.

Searched the 209-RDR corpus for: heat, graduat, promot, access_count, tier.
The seven above are every hit with real overlap.

## Context

### Background

Nexus keeps three memory tiers. T1 is session scratch, T2 is persistent notes
in Postgres behind the engine, T3 is permanent knowledge with embeddings. A
row can move from T1 to T2 when it is flagged (the session-end flush writes
flagged entries to T2 as permanent rows, `src/nexus/hooks/__init__.py:656`)
and from T2 to T3 by `nx memory promote`. Every T2 read that tracks access
increments `access_count` and sets `last_accessed`. That counter is the heat
this RDR is about.

### Already shipped

- **RDR-207's `rolled_up_at` mark** and the quarantine, rollup and reap path
  (`MemoryRepository.java:679-756`, `:859`). This RDR's old prerequisite.
- **Phase 3 Step 0** (nexus-qo7ie.1): reviewer and developer write-backs carry
  `kind:gate`, `bead:<id>` and `rdr:<NNN>` tags, built by the `gate_record`
  procedure (`src/nexus/procedures/gate_record.lua:79-88`) and pinned by
  `tests/test_gate_record_tags_lint.py`.
- **Phase 3 Step 4** (nexus-qo7ie.28): the census flag in `nx memory curate
  --t2` (`_census`, `src/nexus/memory_curate/__init__.py:586-615`), with the
  closed-phase rule (`:516-521`, `:646`).

### Technical Environment

- Engine: `MemoryRepository` (jOOQ, tenant-scoped), `MemoryHandler`
  (`/v1/memory/*`), Liquibase changelogs for `nexus.memory`.
- Client: `HttpMemoryStore` (`src/nexus/db/t2/http_memory_store.py`),
  `T2Database` (`src/nexus/db/t2/__init__.py`), `nx memory` verbs, MCP
  `memory_*` tools, session-end flush in the `src/nexus/hooks/` package
  (`session_end_flush`, `__init__.py:526`; `hooks.py` is now that package).
- The T2 to T3 promote path resolves the T3 write endpoint, pre-registers
  the catalog entry, and fires post-store hooks (deferred Phase 1 reuses it).

## Research Findings

### Investigation

The Phase 0 census was run on 2026-09-14 by conexus-e9 against the live
hosted store (tenant `nexus`, read-only, 14:37Z); its queries and full tables
are in T2 `nexus_rdr/209-research-1`. The current-code claims below were
re-read from the working tree at commit 2e09b8cfc on 2026-11-16 and are
recorded in T2 `nexus_rdr/209-research-4` to `-11`, one entry per group of
claims, each written before the sentences that rest on it.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `MemoryRepository.search` and `/v1/memory/search` | Yes (research-4) | fetches every match with no limit and stamps all of them (`MemoryRepository.java:393-429`); the handler reads no `limit` (`MemoryHandler.java:309-331`) |
| MCP `memory_search`, CLI `nx memory search` | Yes (research-4) | the MCP tool slices client-side (`mcp/core.py:8252-8258`); the CLI prints every match (`commands/memory.py:169-187`) |
| engine-side narrowing precedents | Yes (research-5) | `title_kinds` on `/v1/memory/all` (`MemoryRepository.java:592-634`, `http_memory_store.py:735-767`) and `limit` with `matching_total` on `/v1/memory/list` (`MemoryHandler.java:337-361`) |
| `expire`, upsert, promote | Yes (research-6) | the TTL formula reads `access_count` (`MemoryRepository.java:697`); put keeps `access_count`, restarts the age clock, clears the quarantine stamps and overwrites agent and session (`:1444-1485`); the import route copies every field as sent (`:1393-1442`, research-10); no route sets only a TTL (`MemoryHandler.java:27-47`) |
| Phase 3 state | Yes (research-7, -11) | Steps 1 to 3 are not built; no whole-tag list filter exists in the engine (`MemoryRepository.java:556-567`, `:453-467`, `:592-634`); the census lists tagged rows with `get_all` and `split_tags` (`memory_curate/__init__.py:560`, `:600`) |
| readers of RDR gate rows | Yes (research-8, -9) | `*-gate-latest` rows are read by the rdr hook and the plans lifecycle stream (`hooks/rdr_verb.py:539-562`, `plans/lifecycle_stream.py:93`, `:396-406`); critique and fix-check rows by the accept guard, the re-gate preamble and the loop-health count (`commands/rdr.py:1011-1020`, `:2176-2193`, `:3895-3924`) |

### Key Discoveries

- **Verified** (source search, research-4) Heat is tracked and has exactly
  two consumers: the effective-TTL computation in `expire()` and the idle
  filter in `flagStale`. No promotion path reads it.
  *Source: `MemoryRepository.java:679-709`, `:914`.*
- **Verified** (source search, research-6) `nx memory promote` is the only T2
  to T3 path. It is manual, single-row, honours the remaining TTL by shrinking
  `ttl_days` (T3 computes expiry as `indexed_at + ttl_days` and accepts no
  `expires_at`), and writes nothing back to the T2 row.
  *Source: `src/nexus/commands/memory.py:527-666`.*
- **Verified** (source search, research-6) RDR-057's first gap ("all
  unpromoted T1 entries are silently lost at session close") is now half true:
  flagged entries are flushed to T2 as permanent rows; unflagged entries are
  still lost. Heat plays no part in which is which.
  *Source: `src/nexus/hooks/__init__.py:526`, `:656`; `src/nexus/mcp/core.py:2634`.*
- **Verified, and it changes the premise** (spike, live census 2026-09-14,
  re-confirmed in code 2026-11-16) The distribution has a tail, but the
  counter does not measure reads. Of the tenant's rows, most have been touched
  a few times and a few dozen many times (median 3, ninetieth percentile 12,
  ninety-ninth 32; the top one percent of rows hold under a tenth of all
  accesses). That much would support a threshold. But the engine's memory
  search stamps access on every row that matches the full-text query, with no
  limit in the SQL, while the MCP tool pages the result client-side. So a
  search that shows eight rows bumps two hundred. More than half of all
  accessed rows share their `last_accessed` stamp with ten or more other rows,
  in batches of up to eight hundred; one batch of two hundred and six was the
  census-taker's own search minutes earlier. `access_count` therefore counts
  "matched some search", not "was read". Graduating on it would promote rows
  that contain common words.
  *Source: `MemoryRepository.java:393-429` (search fetches every match, then
  `batchTrackAccess` over all of them); `src/nexus/mcp/core.py:8252-8258` (the
  tool slices the page after the call). T2 `nexus_rdr/209-research-1` and
  `-4`.*
- **Verified** (research-4) Only one caller receives fewer rows than the
  engine stamps: the MCP `memory_search` tool. The CLI prints every match, so
  for it "matched" and "received" are the same set. The Lua verb, the T1
  dedup scan and the board hook search with `access=silent` and stamp nothing.
  Direct reads (by title, id, or unique title prefix) stamp only the one row
  returned.
- **Verified** (same census) Seven hundred and thirty-five rows have never
  been accessed, and nearly nine in ten of those are older than thirty days.
  Only thirty-nine rows carried a TTL after the 2026-09-12 sweep.
  *Source: T2 `nexus_rdr/209-research-1`, queries q1 and q6.*
- **Verified** (research-6, -10) The engine has no route that sets only a row's
  TTL. Giving an existing row a TTL means writing it again. A write through
  `put` replaces a missing agent or session with the writer's own, restarts the
  row's age clock and clears any quarantine stamps; a write through the
  verbatim import route (`/v1/memory/import`) stores content, tags, agent,
  session, TTL, timestamp, `access_count` and `last_accessed` as sent, and
  also clears the quarantine stamps. The expiry formula lets a row live
  `ttl_days * (1 + ln(access_count + 1))` days, so a gate record with a 30-day
  TTL that was counted 50 times lives about 147 days.
- **Verified** (research-8, -9) The RDR gate flow's rows in `<repo>_rdr` are
  read by live code for every RDR in the project, closed ones included: the
  `{id}-gate-latest` rows by the rdr hook and the plans lifecycle stream, the
  fix-check row a gate-latest names by the accept guard and the re-gate
  preamble, and every critique row by the re-gate preamble and by the
  loop-health count, which walks all gate-latest rows with no filter on the
  RDR's status. A TTL on any of them would remove a row a reader looks up.
- **Documented** The source paper's formula divides the lower tier's TTL by
  the heat term after graduation. Whether that rule fits a store whose T2 rows
  are mostly permanent (the 2026-09-12 sweep) is not obvious: dividing a NULL
  TTL does nothing. Any rule here has to say what graduation means for a
  permanent row. This bears on the deferred Phase 2 only.
  *Source: RDR-057 §RF-3, arxiv 2604.01707.*

### Critical Assumptions

- [ ] A1: heat is a usable signal on the live store. **Status**: Refuted as
  the counter stands (Phase 0 census): the shape is usable, the meaning is
  not, because search tracking stamps every match. **Method**: Spike.
  Consequence: Phases 1 and 2 cannot start on `access_count` as it is. Phase
  0b gives the counter a meaning; the question "is the shape still usable" is
  asked again at the deferral trigger, on the new counter.
- [x] A5: the fix to the counter is to track only the rows actually returned
  to the caller. **Status**: Verified by source search, research-4 and -5.
  The engine can know the page because the caller can send it: the search
  route gains `limit` and `offset`. A separate counter for direct gets was
  considered and rejected (Alternatives).
- [x] A6: an engine that predates the change ignores a `limit` field in the
  search body and answers a bare list. **Status**: Verified by source search:
  the handler reads only `query`, `project` and `access`
  (`MemoryHandler.java:317-331`), the same reason the plan-search
  `any_lexeme` field was additive (wire ledger, nexus-vi8fp). The client
  therefore must cut the page itself when it gets a list back.
- [x] A8: the loss check can resolve every listed title without stamping
  access. **Status**: Verified by source search: `getAll` never calls the
  tracking helpers (`MemoryRepository.java:623-634`), so a check built on
  `get_all` leaves `access_count` alone, where a `get` per title would add one
  to each record.
- [x] A9: a TTL set through the verbatim import route changes the TTL and
  the timestamp the step chooses, and nothing else. **Status**: Reopened
  2026-11-16 and restated. The first revision's claim was about `put` and was
  false for a row with a null agent or session: `put` fills them from the
  writer's environment and the engine stores that (research-10). For the
  import route it is Verified by source search (research-10): content, tags,
  agent, session, TTL, timestamp, `access_count` and `last_accessed` are stored
  as sent, and an agent or session that is not sent stays NULL. The route also
  clears `quarantined_at` and `rolled_up_at`, which is harmless for a row read
  live, because `get_all` hides quarantined rows. The null-attribution seed in
  Step 3's tests pins it. How many gate records have a null agent or session:
  not measured.
- [ ] A2: a promoted row can carry a mark on T2 (`promoted_at`,
  `promoted_to`) without changing any read path. **Status**: Unverified,
  deferred with Phase 1. **Method**: Source Search against `MemoryRepository`
  reads.
- [ ] A3: the T3 side can receive batch promotions through the existing
  `promote` machinery without a new endpoint. **Status**: Unverified, deferred
  with Phase 1. **Method**: Source Search, `commands/memory.py` promote path
  and the T3 put.

## Proposed Solution

### Approach

Fix the meaning of the counter, then retire the records that never expire.
Graduation itself waits for the counter to have a history.

**Phase 0b, a counter that means received.** The engine's search route takes
a page (`limit`, `offset`) and, when it is given one, stamps only the rows of
that page and says how many rows matched in all. The MCP `memory_search` tool
sends its page to the engine instead of slicing afterwards. Callers that send
no page get what they get today. The existing `access_count` is not reset: it
is what the heat-weighted TTL has been using, and the census records what it
meant. A wire-ledger entry records the change.

**Phase 3 Steps 1 to 3, retire the gate records without losing them.** At a
phase close, `nx memory gate-rollup` keeps each of the phase's gate records in
full in T3, lists them in a pointer record, checks that nothing the records say is lost, and only
then gives the listed T2 records a 30-day TTL, so RDR-207's quarantine,
rollup and reap retire them on the path it already built. A missing close step
stays visible through Step 4's census.

**Phases 1 and 2 (deferred).** Graduation on a threshold, the mark on the T2
row, and the post-graduation TTL rule, as written under Implementation Plan.
Revived by the trigger in the Scope paragraph.

### Technical Design

#### Phase 0b: the page goes to the engine

**Choice.** The engine stamps the rows it returns. The alternative, a client
that reports back the ids it received, is rejected below.

**Wire change.** `POST /v1/memory/search` accepts two new optional body
fields, `limit` (an integer of 1 or more) and `offset` (an integer of 0 or
more, default 0, used only with `limit`).

- No `limit`: unchanged. The answer is a bare JSON list of every match, and
  with `access` of `track` every match is stamped. A caller that receives all
  matches has received them all, so the stamp is true to the new meaning.
- With `limit`: the engine keeps the same order as today (full-text rank, then
  id), takes the rows from `offset` for `limit` rows, stamps only those (when
  `access` is `track`), and answers an object, `{"rows": [...],
  "matching_total": N}`. `N` and the rows come from ONE SQL statement: a CTE
  of the matches, a one-row count over it, and the page left-joined to that
  count, so a page past the end still returns the row that carries `N` (a
  window count rides on returned rows and would lose it there). One statement
  reads one snapshot even at READ COMMITTED, so a concurrent insert, delete or
  quarantine cannot make the page and the total disagree; a second count
  statement in the same transaction would not give that. A `limit` below 1 or
  not an integer is a 400, and a `limit` above 10,000 is clamped to 10,000, as
  `/v1/memory/list` clamps its `limit`; an `offset` below 0 or not an integer
  is a 400.
- An empty page past the end answers `{"rows": [], "matching_total": N}` and
  stamps nothing. The "no searchable terms" refusal fires only when `N` is
  0, as it does today when no row matches
  (`MemoryRepository.java:416-418`), never for a page past the end.

The answer is an object, not a bare list with a per-row `matching_total` as
`/v1/memory/list` does (`MemoryHandler.java:337-361`), because an empty page
has no row to carry the total, and the MCP tool prints "No results at offset N
(total T)" (`mcp/core.py:8260`). Only a request that sends `limit` receives
the object, and only new clients send it.

**Client.** `HttpMemoryStore` gains `search_page(query, project, limit,
offset, access)` returning the rows and `matching_total`. `search()` keeps its
signature and behaviour. `search_page` cuts the page itself when the engine
answers a bare list (A6): rows `[offset:offset+limit]`, total `len(list)`;
that engine is an older one that also stamped every match, which the client
cannot undo. The `T2Database` facade (`__init__.py:529-534`) passes the
method through. The MCP `memory_search` tool calls it with its own `limit` and
`offset` (`mcp/core.py:8252-8258`) and treats a `limit` below 1 as a usage
error before any request.

The CLI `nx memory search` (`commands/memory.py:169-187`) prints every match
and so keeps stamping every match. That is accurate under the new meaning. A
later CLI `--limit` would use the same two fields.

**What a read count means after this.** One increment per time a caller
received the row: in a search page, by title, by id, or by unique title
prefix. A page requested twice counts twice. A row a caller searched for
and never saw, because it ranked past the page, is not counted.

**Baseline for the re-census.** Counts taken before the deploy stay as they
were. A re-census that wants the new meaning reads the change since a baseline,
so Step 5 records one at the deploy.

**Wire ledger.** One `## Unshipped` entry in `docs/wire-contract-pending.md`,
marked `[additive]`, naming the owning bead and the engine tag that carries
the engine half. Direction safety: an OLD client plus a NEW engine is safe,
because an old client never sends `limit` and gets the unchanged bare list. A
NEW client plus an OLD engine is safe, because the old engine ignores `limit`,
answers a bare list, and the client cuts the page itself; the counter stays
inflated until the engine is upgraded, which is the status quo and not a new
fault. The change is a both-halves commit (engine and client wire surface), so
the ledger lint requires the entry; the engine deploys before the client tag.

#### Phase 3: a pointer record, a loss check, a TTL

**Step 1, the command and the pointer record.** `nx memory gate-rollup --rdr N
(--phase P --beads b1,b2,... | --all) [--project REPO] [--dry-run]`. The bead
list for a phase comes from the `phase-review-gate` close step, which already
writes `Item N = <bead-id>` for every item; the command does not call `bd`,
because bead-to-phase membership lives in the issue tracker, not in T2.

- *Which records.* One `get_all` of `<repo>`, the read `nx memory curate --t2`
  already makes (`memory_curate/__init__.py:560`), split on the client by
  `split_tags` (`:600`; `src/nexus/tables/audit_tags.py:89-93`, which splits
  on commas, trims and drops empties). A record is a gate record when its whole
  tags include `kind:gate`; it belongs to a listed bead when they include
  `bead:<id>`, or, with `--all` (used at `rdr-close`), when they include
  `rdr:<NNN>`. That match is exact by construction (research-11), so the
  engine needs no tag filter and Phase 3 adds no wire change. An
  untagged record whose body mentions `kind:gate` is never matched, because
  only the tags are read. Older records carry no tags; with `--phase` the same
  read lets the command match them by a listed bead id in their title, the 72%
  the title mapping reaches (research-2). The match is always on (the fix round
  dropped `--include-titled`) and has one predicate. The row has no tags at all
  (`split_tags` is empty: a tagged row that is not a gate record is never read,
  and a `kind:gate` row with no bead is unmapped, never title-matched), and its
  title holds the listed bead id as a whole token: not preceded by a character
  of `[A-Za-z0-9.]`, and not followed by one of `[A-Za-z0-9]` or by a `.` and
  one, that is `(?<![A-Za-z0-9.])<id>(?!\.?[A-Za-z0-9])`, case-sensitive because
  bead ids are lower case. So `nexus-1` is not found in `nexus-12` or
  `nexus-1.2`, a parent bead is not found in its child's id, and `nexus-abc` is
  not found in `conexus-abc`; a trailing sentence period still matches. A
  hyphen is not an id character, because gate titles join words with it
  (`review-<sha>-<bead>-<date>`). A title that names two listed beads is
  unmapped. `--all` has no bead list, so it matches no untagged row. Records
  that map to no listed bead are not touched. The "unmapped" list in the pointer holds the records the run can
  select but cannot tie to a bead: a `kind:gate` record that carries
  `rdr:<NNN>` and has no `bead:` tag (with `--phase P` only when its title also
  names phase P by the `-p<N>` / `-phase-<N>` rule of Step 4, a sub-phase such
  as 3a counting toward phase 3; with `--all` whenever it carries the RDR's
  tag), and a listed record whose verdict cannot be read. A `kind:gate` record
  with neither a `bead:` nor an `rdr:` tag is in no run's scope and is left
  alone; the census counts it as its own unmapped number. Unmapped records take
  no part in Step 2.3 or Step 3, because both work per bead.
  The cost is one read of the whole project per run, as the census pays; the
  nexus project held 3,158 rows on 2026-09-14 (research-1), and its size today
  is not measured.
- *Which project.* `<repo>` only. The RDR gate flow's rows in `<repo>_rdr`
  (`{id}-gate-latest`, `{id}-gate-critique-*`, `{id}-fix-check-*`) are left
  alone: no listing in the pointer and no TTL. Live code reads all three kinds
  for every RDR, closed ones included, and keys on the RDR number alone
  (research-8, -9). Exempting only some of them would mean tracking which
  fix-check row each kept gate-latest names and which critique rows the
  loop-health count reads, so the command does not touch the family. This
  narrows Amendment A1, which put those rows in scope; the cost is that they
  stay permanent, as they are today.
- *The pointer record.* One T3 record per phase (per RDR for `--all`), in the
  existing knowledge subject `nexus-rdr-research` (`docs/collections.md`
  Rule 1: reuse a subject, and RDR postmortems already live in a subject under a
  category), `category=gate_pointer`, titled `gate-records-rdr-<NNN>-phase-<P>`
  or `-all`, so a rerun replaces it. It lists, for each record, its project,
  title, kind, bead, the sha it reviewed, its verdict and its timestamp, the
  verdict and sha taken from the field header `gate_record` writes
  (`gate_record.lua:79-88`) or, for an older record, the verdict from its last
  `outcome` line. The timestamp is the row's, exactly as `get_all` returned it
  (to the second): the creation date a record has, which Step 3's import keeps
  and which the pointer therefore still shows after the TTL write and the reap.
  An unmapped record carries its timestamp too. The pointer also names each
  listed record's archive and its part count (below), whether a run gave it a
  TTL (`ttl_set`), whether a person restored it (`restored`) and, for a record
  the read no longer lists, its `state` (Step 2.2, Step 3). Records whose verdict or
  bead cannot be read are listed under "unmapped" and keep no TTL. A pointer
  over the store_put cap
  (`QUOTAS.MAX_DOCUMENT_BYTES`, 16,384 bytes) is written in parts, split at line
  boundaries: the first keeps the title, the rest are titled `<title> (part 2)`
  and so on, and a `parts: k` header line in the first names how many, so a
  reader never guesses. A rerun that needs fewer parts names the older trailing
  ones as stale and does not delete them (a T3 delete is a catalog tombstone
  path, not part of this command). A run that lists nothing writes no pointer.
  `--project` defaults to the current repository and must be it: a name ending
  `_rdr` is refused (the RDR gate flow's project is never read or changed), and
  so is any project that is not the working directory's repository. The
  open-phase check reads that checkout's RDR files, there is no mapping from a
  project name to another checkout, and a project that did not match would be
  checked against the wrong RDR. The choice is to refuse, not to look the
  repository up, because the command runs from the checkout it rolls up.
- *The archive.* Sam, 2026-11-16: "Keep findings in full". The pointer is a
  pointer, and each listed record's full text is kept beside it as a T3 note of
  its own in the same subject, `category=gate_record`, titled
  `<project>/<T2 title>` (the project, because the subject is shared and two
  repositories' titles can be equal), with the content exactly as `get_all`
  returned it, header and body. A record over the store_put cap is split at
  line boundaries into `<title>`, `<title> (part 2)` and so on, and the
  pointer's `parts` field says how many. The run writes each archive through the
  same writer, reads it back and compares it with the record, and writes the
  pointer only after every archive reads back whole; an archive that already
  reads back whole is not rewritten, so a rerun costs no embedding. Unmapped
  records are not archived: they get no TTL, so nothing retires them. What
  survives RDR-207's reap is therefore each record's full text, not a title that
  points at nothing.
- *The trigger.* The `phase-review-gate` skill's close step (`SKILL.md:24`) and
  the `rdr-close` skill name the command. Nothing blocks a close without it;
  Step 4's census is what shows a missed run.

**Step 2, the loss check.** Before any TTL is set, the command stops and names
the cause if any of these fail:

1. The phase is closed: the RDR's frontmatter status is `closed`, or every
   phase heading it names carries the close marker (the rule Step 4 uses,
   `memory_curate/__init__.py:516-521`, `:646`; `--all` needs the RDR itself
   closed). A record for an open phase is never touched: the refusal comes
   before anything is written, the archives and the pointer included.
2. Every title the run lists resolves. A record of this read resolves by being
   in it. A record an earlier pointer (same title) listed that this read no
   longer lists is carried into the new pointer, so no record an earlier run
   listed drops out of it. One the expire sweep quarantined (found in the
   quarantined listing, which `get_all` hides) is marked `state: quarantined`;
   its row is restorable, so it is carried whatever its archive. One that is
   neither live nor quarantined, because the reap deleted its row, is marked
   `state: absent` provided its archive still reads back whole; if it does not,
   the run stops and names the title, because that record's full text would be
   lost. Carried records take no part in Step 2.3 or the TTL step: they
   already have one. After the pointer is written it is read back from T3 by its
   title and must list the same records with the same marks (`ttl_set`,
   `restored`, `state`: the next run decides from them), and every listed record's archive
   reads back whole and equal to the record, so no record gets a TTL until its
   full text reads back from T3. These reads do not stamp access (A8).
3. For each listed bead, its newest record has the verdict `justified`. Newest
   is by header date, then round, then title. A record with no field header
   (an older one) is dated from the first `YYYY-MM-DD` in its title, else from
   its timestamp's day; the round is the title's trailing `-r<N>` (a number,
   so `-r10` follows `-r9`), 0 when there is none, because `gate_record`
   writes no round field. Dates are day-granular and the round is optional, so
   when more than one record shares the newest date and round the worse
   verdict decides (`not-justified` over `partial` over `justified`). A
   terminal `partial` is not a pass: it stops the run like `not-justified`
   (whether a partial that states no ship-blocker may pass is Sam's call, not
   this text's; the blocking rule is kept as designed). A bead whose newest
   record is `partial` or `not-justified` stops the run and is named, unless the run names it with
   `--accept-open <bead>` (repeatable), which the pointer records in an
   `accepted-open:` header line; a name that matches no listed bead is
   refused. Every non-justified record of a listed bead is printed. A record
   with no bead has no chain to check; it is "unmapped" and gets no TTL.

`--dry-run` applies checks 1 and 3 and the earlier-pointer half of check 2 (it
reads the earlier pointer and the quarantined listing, and writes nothing), and
refuses exactly as the real run does. It skips the archives, the pointer write
and its read-back, and says so in its output. Its candidate set is the listed
records that pass those checks and have no TTL, minus unmapped and restored
records, and it never stamps access: it reads with `get_all`.

This is narrower than Amendment A1's wording, "each ship-blocker named in a
gate record is either fixed by a listed sha or names its bead". A ship-blocker
is free text and a script cannot tell one from a quoted example, so the check
runs on the verdict chain per bead, which is structured, and prints each
non-justified record for a person to read. The reviewer stays accountable for
what the text says.

**Step 3, the TTL.** For each listed record that passed Step 2 and has no TTL,
write it through the verbatim import route (`HttpMemoryStore.import_entry`,
`POST /v1/memory/import`; research-10) with `ttl=30` and the values `get_all`
returned for project, title, content, tags, agent, session, `access_count`,
`last_accessed` and timestamp. A null agent or session is sent as absent, so it
stays NULL, and a `last_accessed` of `""` is sent as `None`. The route is not
a pure copy: it also clears `quarantined_at` and `rolled_up_at`, re-stamps the
row's identity with the caller, and truncates the timestamp to the second (which
`get_all` already returns).
The values come from one `get_all` taken right before the writes, after the
pointer is written and read back, so a reader's access that landed after the
listing keeps its count (a per-row `get` would stamp access, A8); a row that now
has a TTL, or is gone, is skipped. Then read the rows back through one
`get_all` and compare every field. The import keeps the row's own timestamp,
so an old row can be quarantined by the sweep before it is read back, and
`get_all` hides it: a row missing from that read is looked up in the
quarantined listing before it is called missing, reported as `quarantined
(restorable: nx memory restore <id>)`, and still compared field by field. A
mismatch is reported with the title and the field, and the original row's
values are printed so a person can write them back; the command does not undo
it, and exits nonzero. The read cost per run is one `get_all` before the writes
and one after, on top of the listing's, plus up to two quarantined listings
(one for the carry, one made afresh for the read-back, because the carry's
predates the writes) and one read of the earlier pointer from T3, each made
once and only when needed; it does not grow with the number of records.

- *Why import and not put.* A `put` fills a missing agent or session from the
  writer's environment and the engine stores that, so it would give a record
  with null attribution the runner's name (research-10). The import route
  stores what it is sent, so attribution, `access_count` and `last_accessed`
  survive. It is an existing route and client method, so this step adds no
  wire change.
- *Which timestamp.* The row's own, so the import changes only the TTL. The
  timestamp is the only creation date a record has, and the wire already carries
  it to the second. The census's supersession test compares timestamps
  (`memory_curate/__init__.py:569-570`); that RDR-207's rollup groups by the
  record's month is inferred, not read (gate critique S6). Sending the time of
  the run would give a person 30 days after the rollup, at the price of that
  date. The consequence of the choice: a record already older than its
  effective TTL, `ttl_days * (1 + ln(access_count + 1))`
  (`MemoryRepository.java:697`), is quarantined by the next expire sweep, which
  the session-end hook runs (`hooks/__init__.py:650-659`). Quarantine hides and
  deletes nothing, `nx memory restore ID` undoes it, and the pointer, written
  and read back first, names every title. That fits the purpose of Gap 6: these
  are working state read within two days of being written, and a record past
  30 days in a closed phase is the one A1 asks to retire. How many records are
  already past their effective TTL: not measured. Sam chose this on 2026-11-16
  (by dialog in halcyon's session, relayed by halcyon: "Keep timestamps, count
  first"), with a count step before the first real run (Implementation Plan,
  Phase 3 Step 3, the 30-day TTL: Count first).
- *What expiry does.* Quarantine only, as RDR-207 built it: the record is
  hidden, restorable, and deleted only after an attended `nx memory rollup`
  marks it and `nx memory reap` runs. A record's heat lengthens its life by
  `1 + ln(access_count + 1)`, so a much-read record outlives 30 days; that is
  the existing formula, not a change.
- *Census.* `census_projects` is `{repo, f"{repo}_rdr"}`
  (`memory_curate/__init__.py:558`). The rows left alone in `<repo>_rdr` would
  be flagged for ever, so this step removes `<repo>_rdr` from it, deletes the
  census's `<repo>_rdr` title-number branch and `_RDR_TITLE`, and narrows its
  predicate from the tags `gate` or `kind:gate` to `kind:gate`, the one tag the
  rollup lists (a row tagged only `gate` is neither listed nor flagged). Three
  shipped census tests pinned `<repo>_rdr` rows as flagged and change with it:
  `<repo>_rdr` rows are still seeded and asserted not flagged, the test that
  read "only the repo and repo_rdr projects" is renamed to say it reads only
  `<repo>`, and the sub-phase rule (3a counts as closed when phase 3 is) is now
  pinned on a `<repo>` row, so its coverage is kept.
- *Rerun.* The command is idempotent: the pointer is replaced, records that
  already have a TTL are skipped, and records an earlier pointer listed that
  the read no longer lists are carried (Step 2.2). A person's restore is not
  undone. `nx memory restore` makes a row permanent (`ttl` NULL), which looks
  exactly like a record never given a TTL, and the restore also clears the
  rollup mark, so the earlier pointer is the only evidence. Each listed record
  therefore carries `ttl_set`: the first pointer is written before the TTL
  writes, with the mark false, and once the writes and their read-back are done
  the pointer is written again with the mark true for each record a run gave a
  TTL (a record that already had a TTL and was listed before counts as ours). A
  rerun that finds a listed record with `ttl_set` true and no TTL leaves it
  alone, prints `restored, left alone: <title>` and keeps it in the pointer
  marked `restored`, so the next rerun skips it too. A record the earlier
  pointer listed with the mark false whose TTL write stopped part way is not
  a restore: it is a candidate again. A missing or unreadable earlier pointer
  reads as a first run, which gives a record nobody listed before a TTL. If the
  second pointer write fails after the TTLs were written the run says so and a
  rerun writes the marks. The limit: the marks live in one pointer, so they are
  per scope. A record restored after a `--phase` run is not protected from a
  later `--all` run of the same RDR, whose pointer has no mark for it, so that
  run gives it a TTL again (the archive keeps its text either way, and a
  restore is one command). The close step runs the phase rollup before the
  `--all` one, so restore after the last run that lists the record.
- *Residual race.* The write sends the content read a moment earlier. A
  reviewer who rewrote the same title in that moment would be overwritten, and
  a row quarantined in that moment would be restored, because the import clears
  both quarantine stamps (research-10). The phase is closed and its reviewers
  are done, and the engine offers no compare-and-set, so this is accepted and
  stated. The fresh read narrows the same race for `access_count`: a read that
  lands between that read and a record's import loses its count, a window the
  length of the write loop; closing it needs a compare-and-set, which the
  engine does not have. A rewrite that lands before the fresh read is caught:
  the archive and the pointer hold the text of the listing, so a candidate whose
  fresh content or timestamp differs from it is skipped, named
  (`changed since it was archived, no TTL`), and left without a TTL, because
  retiring it would retire text that has no archive; a rerun archives the new
  text and then gives it the TTL. Only a rewrite inside the write loop is left
  to the race above.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| page-aware search | `MemoryRepository.search`, `/v1/memory/search` | Extend: optional `limit`/`offset`, same ordering |
| client page call | `HttpMemoryStore.search`, `T2Database.search` | Add `search_page`; leave `search` alone |
| matching total | `/v1/memory/list` `matching_total` | Same idea, object envelope because a page can be empty |
| tagged-record listing | `get_all` plus `split_tags`, as `_census` does (`memory_curate/__init__.py:560`, `:600`) | Reuse: client-side partition, no engine change |
| pointer record | `nexus-rdr-research` knowledge subject, `category` | Reuse the subject, new category |
| loss check reads | `get_all` (no access stamping) | Reuse |
| 30-day TTL | `HttpMemoryStore.import_entry`, `POST /v1/memory/import` | Reuse: a verbatim write with `ttl=30` |
| retirement | RDR-207 quarantine, rollup, reap | Reuse unchanged |
| missed-close visibility | `_census` in `memory_curate` (Step 4) | Reuse; drop `<repo>_rdr` from its projects |
| heat census (deferred) | `MemoryRepository` access columns | Reuse: one aggregate query |
| graduation copy (deferred) | `commands/memory.py` promote path | Reuse: call it per row |
| mark on T2 row (deferred) | RDR-207's `rolled_up_at` pattern | Extend: sibling columns |
| TTL rule (deferred) | `expire()` heat formula | Reuse the same heat term |

### Decision Rationale

A counter that counts rows nobody saw cannot carry a threshold, and it is
already feeding the shipped TTL and RDR-238's audit, so fixing it is
worthwhile before graduation is. The gate records are a separate, live
problem that needs no threshold at all: they are written about 1,000 times a
month and never expire. Each piece is a day of work against known code; only the
counter needs an engine change. Graduation needs a
counter with a history first, which is why Phases 1 and 2 wait and why the
RDR does not guess a threshold.

## Alternatives Considered

### Alternative 1: The client reports the ids it received

**Description**: the search stays as it is except that the MCP tool searches
`silent`, slices its page, and then calls a new route, `POST
/v1/memory/touch`, with the page's ids.

**Pros**: works for any client-side filtering after the search.

**Cons**: two round trips per search; a new write route that lets any caller
raise any row's count without reading it, where today a count at least
follows a search; a forgotten call undercounts with no sign; the ids can go
stale between the two calls (a row quarantined in between).

**Reason for rejection**: the engine already knows what it returned. Letting
the caller send the page is one optional field and keeps the stamp inside the
read.

### Alternative 2: A separate counter for direct gets only

**Description**: leave search tracking alone and add a `read_count` that only
by-title and by-id reads raise.

**Pros**: no change to the search route.

**Cons**: a schema change and an engine release for one column; the
heat-weighted TTL and the audits read `access_count`, so two counters split
the signal; and most reads are search pages, which this would stop counting.

**Reason for rejection**: it fixes the counter by discarding the main way rows
are read.

### Alternative 3: Search never tracks

**Description**: make every search `silent`, as the Lua verb and the T1 dedup
scan already are.

**Pros**: smallest change; no counter can be wrong.

**Cons**: an agent that finds a row by search and reads its snippet leaves no
trace, so only by-title reads would count.

**Reason for rejection**: it loses the signal rather than repairing it. This
is the fallback if the page-aware counter proves too noisy at the re-census.

### Alternative 4: How Step 1 lists a phase's gate records

**Description**: three ways were weighed. (a) `get_all` of the project and a
client-side partition with `split_tags`, the way `_census` already lists
tagged rows (`memory_curate/__init__.py:560`, `:600`; research-11). (b) A
`tags` parameter on `GET /v1/memory/all`, following `title_kinds`. (c)
`search_by_tag(query=<the tag text>, tag=<the tag>)`.

**Pros**: (a) is exact by construction, needs no engine change, no second
wire change, no ledger entry and no parity test between a SQL pattern and
`split_tags`. (b) would narrow the read to the matching rows. (c) needs no
engine change either.

**Cons**: (a) reads the whole project, which held 3,158 rows for the nexus
project on 2026-09-14; its size today is not measured. (b) adds a wire change
and needs the engine pattern to trim spaces as `split_tags` does, where
`searchByTag`'s pattern does not (`MemoryRepository.java:565`). (c) still has to
match the full-text index, which tokenizes tags such as `kind:gate` and
`bead:nexus-qo7ie.1` by Postgres's own rules; the RDR has not verified that every
bead id tokenizes the same way on both sides, and the 2026-10-31 survey found
full-text matching on tag words returns untagged records.

**Reason for choosing (a)**: the command is attended and runs once per closed
phase, so one read of the project is cheap against a second wire change. If a
run proves slow on the live project, (b) is the follow-up, and the client
already has the re-cut pattern.

### Alternative 5: Gate-record index as a tuple-space template

**Description**: research-3 (2026-10-31, from linda) proposed an append-only
tuple template keyed by (kind, rdr, bead) with pointer bodies.

**Reason for rejection**: not adopted and not pursued here. It would couple
this work to the tuple space (the RDR-110 to 119 arc warns against bundling
them), ride its own engine template cut, and a client-side partition plus a T3
pointer reaches the same lookup with less. It stays a recorded alternative.

### Briefly Rejected

- **Restore the division formula in `expire()` for all rows**: shortens the
  life of hot rows that were never promoted, which is loss, not graduation.
- **Automatic graduation at session end**: RDR-207's failure-mode analysis
  applies unchanged; session end is the wrong context for anything slow.
- **Reset `access_count` when Phase 0b ships**: destroys the history the
  heat-weighted TTL already used and makes the change a data rewrite. The
  baseline in Step 5 handles the re-census instead.
- **Promote at rollup time (the old Alternative 2)**: couples two decisions,
  and a rollup group's summary would omit its hottest rows. Deferred with
  Phase 1; revisit if the re-census shows hot rows and quarantined rows
  overlap.

## Trade-offs

### Consequences

- After Phase 0b, a row that was kept alive by incidental matches earns fewer
  increments, so rows with a TTL will tend to expire a little sooner than
  before. Only rows with a TTL feel this (39 on 2026-09-14), plus the gate
  records once Step 3 gives them one. The counts already accumulated stay.
- A gate record given its TTL keeps its own timestamp, so one already older than
  its effective TTL is quarantined at the next expire sweep (hidden, restorable
  with `nx memory restore`). It drops out of Step 4's census because it now has
  a TTL. A record a person restores has no TTL again, so the census flags it
  again; the rerun leaves it alone (Step 3, Rerun), and the flag is the
  reminder that a person decided to keep it.
- The counter's history is mixed: before the deploy it counts matches, after
  it counts receipts. Only a delta since the baseline has the new meaning.
- Phases 1 and 2 are not built, so nothing graduates and bead nexus-2k66t.1
  stays deferred.

### Risks and Mitigations

- **Risk**: an old engine stays deployed while new clients send `limit`.
  **Mitigation**: the client cuts the page itself, so the tool still works; the
  counter is then as inflated as today. The engine deploys before the client
  tag under the ledger rule.
- **Risk**: the page-aware counter is still too flat to support a threshold.
  **Mitigation**: that is a valid outcome of the re-census; the RDR's deferred
  half is then abandoned with the measurement recorded, or Alternative 3 is
  taken.
- **Risk**: the loss check is a verdict-chain check, not a read of the
  findings, so a ship-blocker named only in prose could pass.
  **Mitigation**: the command prints each non-justified record for a person;
  the TTL only hides records (quarantine), it deletes nothing, and
  `nx memory restore` brings one back.
- **Risk**: the TTL write overwrites a record rewritten, or restores one quarantined, in the same moment.
  **Mitigation**: phases are closed before the run; stated as accepted above.
- **Risk**: gate records outside the title and tag conventions are missed.
  **Mitigation**: they stay unmapped and keep no TTL; Step 4's census shows
  them.

### Failure Modes

A pointer write that fails sets no TTL. A loss-check failure sets no TTL and
names the title or bead. A TTL write that fails part way leaves some records
with the TTL and some without; a rerun finishes the rest. The silent failure to
guard is a TTL set without a pointer: the pointer is written and read back
first, in that order, and the test plan forces the pointer write to fail and
asserts no record gets a TTL. On the counter side, the visible failure is an
older engine that ignores `limit`; the client's local cut keeps the tool
working and the unit test of that fallback runs against a stub engine that
returns a bare list. The silent failure is a counter that stamps outside the
page; the test seeds 30 matching rows and asserts the 25 outside the page keep
a count of 0.

## Implementation Plan

### Prerequisites

- [x] RDR-207 landed (the `rolled_up_at` mark; closed 2026-09-15).
- [x] Phase 0 census recorded (research-1) and A1 resolved as refuted.
- [x] Phase 3 Steps 0 and 4 shipped (nexus-qo7ie.1, nexus-qo7ie.28).
- [ ] Bead filing waits for accept (no new beads while the burn-down runs).

### Minimum Viable Validation

Two proofs, both in the default suite against the engine substrate. Counter:
seed 30 rows that match one query, search with `limit=5`; exactly those five
rows have `access_count` 1 and the other 25 have 0; the next page stamps its
five; a `silent` search stamps none; a search with no `limit` stamps all 30.
Gate records: a seeded closed phase with three gate records gets one pointer
record that lists all three titles, all three then carry a 30-day TTL and read
back unchanged, a record seeded with a null agent and session keeping both
null; a seeded open phase is untouched; a planted unresolvable
title stops the TTL step and names the title; an untagged record whose body
says `kind:gate` is never listed as a gate record.

### Phase 0b: a read counter that means received

Sized at about a day of work plus an engine release.

#### Step 1: Engine, page-aware search

`MemoryRepository.searchPage` (a delegate to `MemorySearchPage`; `search` keeps
the bare list) takes a page and stamps only the returned rows; the handler
reads `limit` and `offset`, answers the object form, and returns 400 for a
`limit` below 1. Tests in `MemorySearchPageTest` (repository and wire): the MVV
counter proof, an empty page past the end (no error, no stamp, total reported),
the no-`limit` request unchanged, `silent` stamps nothing, tenant isolation, a
`limit` above the ceiling clamped, rank order across pages, a quarantined match
not counted, the degenerate-query guard keyed to the total. The one-statement
shape is pinned by `MemorySearchPageStatementsTest`, which records the SQL a
page runs, and the quarantine predicate on the total by
`MemoryRepositoryQuarantineReadPathTest`.

#### Step 2: Client, `search_page` and the MCP tool

`HttpMemoryStore.search_page`, the `T2Database` pass-through, and the MCP
`memory_search` change (`mcp/core.py:8252-8258`). Tests: the page and total
against the engine substrate; the old-engine fallback against a stub that
answers a bare list; `limit` below 1 is a usage error with no request.

#### Step 3: Wire ledger and changelog

One `[additive]` entry under `## Unshipped` in `docs/wire-contract-pending.md`
naming the bead and the engine tag, with the direction-safety text above; one
`changelog/unreleased/<bead-id>.md` entry. The ledger lint
(`tests/test_wire_contract_pairing_lint.py`) is the check.

#### Step 4: Docs

State the counter's new meaning where `access_count` is described, and in
RDR-238's audit note that the counter is now the source it was waiting for.

#### Step 5: Baseline at the deploy

When the engine release deploys, run the research-1 queries once and record
the bucket table and a per-row `(id, access_count)` listing with the deploy
time (a T2 entry, split if it is too long for one). That listing is what the
re-census subtracts. The counter works without it; only the re-census needs
it.

### Phase 3: Gate-record rollup at phase close (Amendment A1)

Does not depend on Phase 0b. Sized at about a day, client only: it needs no
engine change. Research 2 (2026-10-31) shaped it: only about 72% of the 2,320
gate-record titles name a bead, so a title-only rollup misses about 23%; and
the `phase-review-gate` skill says nothing enforces its close step, so the
trigger is `rdr-close` and the close of the phase's review bead, not the skill
alone.

#### Step 0 (shipped): tags at write time

Reviewer and developer write-backs carry `kind:gate`, `bead:<id>` and
`rdr:<NNN>` tags (`rdr:` dropped when the bead serves no RDR), shipped by
nexus-qo7ie.1 and pinned by `tests/test_gate_record_tags_lint.py`, so new gate
records map to a phase without parsing the title. Older records are mapped by
title; a tagged gate record that carries the RDR but no bead is listed as
unmapped in the pointer record and keeps no TTL (Step 1 says which rows reach
that list). The lookup is an exact tag match, not a search: `memory_search`
tokenizes, and a search for `kind:gate` on 2026-10-31 returned 273 hits, the
first five untagged critiques matched on words in their bodies (the
nexus-qo7ie.1 critique). That is why Step 1 reads whole tags with `split_tags` and does not search.

#### Step 1: The command and the pointer record

As designed under Technical Design: `nx memory gate-rollup`, which lists a
phase's gate records from one `get_all` of `<repo>` partitioned by `split_tags`,
the pointer record in T3, and the one-line change to the two skills that name
the command. Tests: only whole-tag matches are listed, and an untagged record
that mentions `kind:gate` in its body is not; `bead:nexus-1` does not match
`bead:nexus-12`; a stored value `kind:gate, bead:x` (a space after the comma)
matches; an older untagged record whose title names a listed bead is listed,
`nexus-1` is not found in a title naming `nexus-12`, a parent id is not found in
its child's, a hyphen-joined title and a trailing period match, a tagged row is
never title-matched and a title naming two listed beads is unmapped; a
`kind:gate` record with `rdr:<NNN>` and no `bead:` tag is listed as unmapped
under `--all`, and under `--phase P` only when its title names phase P (`-p3-`
and `-p3a-` for phase 3, not `-p2-` or `-p13-`); a record with neither tag is
not listed; an unmapped record gets no TTL; a quarantined record is not listed;
another tenant's rows are not listed; the `<repo>_rdr` rows are neither listed
nor changed; a seeded closed phase yields one pointer listing its three
records, each with its timestamp as `get_all` returned it; a pointer over the
cap is split and still lists every record, and a
rerun that needs fewer parts names the stale ones; a rerun replaces the pointer;
each listed record's full text is a T3 note of its own that reads back equal to
the record (header and body), with a body over 16,384 bytes split into titled
parts that join exactly, in the `gate_record` category, while an unmapped
record is not archived; a rerun rewrites no archive that already reads back
whole; `--dry-run` writes no TTL and no T3 note, and its one T2 write is the
count row (Step 3, Count first); no run changes a gate record's text, tags or
timestamp, and the TTL write is the only run that changes a gate record's row.

#### Step 2: The loss check

As designed. Tests: an open phase is refused; a planted unresolvable title
stops the run and names it; a bead whose newest record is `not-justified`
stops the run and is named, and `--accept-open` lets it through and appears in
the pointer, while an older not-justified record under a justified newest one
does not stop it; a record with no bead takes no part in the chain and gets no
TTL; a record an earlier pointer listed that the read no longer lists stays in
the new pointer when its archive is whole; the pointer reads back with the same
list; a phase with the close marker passes in an open RDR and a sub-phase
follows its phase, and `--dry-run` refuses an open phase as the real run does;
the check leaves every record's `access_count` unchanged (A8).

#### Step 3: The 30-day TTL

Count first (Sam, 2026-11-16). Before the command's first run that writes a
TTL, a dry run lists the records the run would give a TTL and, for each, its
timestamp, `access_count` and whether `ttl_days * (1 + ln(access_count + 1))`
with `ttl_days = 30` is already past (`MemoryRepository.java:697`). It writes
nothing and reports one number: how many of those records the next expire
sweep would quarantine. That number goes to Sam (through the session that
coordinates the work) before any TTL is written, and the real run waits for
his word. The dry run is the command with `--dry-run`; it reads with the same
`get_all` as Step 1 and computes the same rule as the engine's expire sweep
(`MemoryRepository.java:686-701`), with an injected clock: a row with no
timestamp is skipped (`:695`; the import route cannot store one, so it only
arises from a hand-made row); the age is whole seconds over 86,400 (`:698`);
the effective TTL is `ttl * (1 + ln(access_count + 1))` with the natural log
(`:697`); and the row is quarantined only when the age is strictly greater
(`:699`). It prints
each candidate as `candidate  <timestamp>  access_count=N  effective TTL X d
past|within its effective TTL  <title>` and ends with `would be quarantined by
the next expire sweep: N of K candidate record(s)`.

Sam's word before the real run is a process step, which the two skills state;
the code cannot hear it. What the code requires (nexus-1gdv0.2.13) is a current,
recorded count, and it is keyed per RDR and phase (Sam, 2026-11-16:
"Per RDR and phase"): every batch, each `--rdr`/`--phase` or `--all`, reports
its own count before its real run. The dry run records its count as one T2 row
in `<repo>` (never `<repo>_rdr`, never SQLite), titled
`gate-rollup-count-rdr-<NNN>-phase-<P>` or `-all` like the pointer, with the
count, the candidate titles, the date and the commit; a later dry run replaces
it. The row carries the tag `gate-rollup-count`, neither `kind:gate` nor `gate`,
so the rollup never lists it and the census never flags it. A real run that
would write a TTL refuses, writing nothing, unless (a) that row exists, (b) the
run passes `--counted N` and N equals the recorded count, the number that was
reported to Sam, (c) every candidate is a title the row lists, so a record
that appeared after the dry run needs a new one, and (d) the count still holds:
the run recomputes how many candidates the engine's rule has past their
effective TTL at the time of the real run and refuses when that differs from
the recorded count, because the count is a snapshot and a record that crossed
its effective TTL since the dry run would otherwise be quarantined without
Sam having been told. The flag is evidence that the
caller has the number, not that Sam said yes; his word stays outside the code,
and the skills tell the session to wait for it. A run with no candidate writes
no TTL and needs no count, so a rerun that finishes after everything has its TTL
passes. This changes "the dry run writes nothing" to what is true: it writes no
TTL and no T3 note, and one count row.

As designed. Tests: the dry run writes no TTL and no T3 note and reports the count, with one
seeded record past its effective TTL and one not, counting exactly one; a record
with `access_count` 3 that is 40 days old (inside its 71.6-day effective TTL) is
not counted and one at 72 days is, a record exactly at its effective TTL is not
counted and one second older is, an age is whole seconds, and a row with no
timestamp is not counted (the heat term, the strict comparison and the
truncation each fail their test when removed); three seeded records each get `ttl=30` with content, tags,
agent, session, timestamp, `access_count` and `last_accessed` unchanged; a
record seeded with a null agent and a null session still has both null after the
step, with `NX_AGENT` and a resolvable session (`NX_SESSION_ID`) set so the
control is real: the test first asserts that a `put` under those settings writes
both non-null, because in a bare test process neither resolves and a `put` would
also leave them null (the test fails for that reason when the step is switched
to `put`, and refuses to pass when the control writes nothing); a seeded record with a never-accessed
`last_accessed` stays never-accessed; a rerun changes nothing; a forced
pointer-write failure leaves no TTL; the `<repo>_rdr` rows (gate-latest,
critique, fix-check) get no TTL and `_census` no longer flags them; a planted
record without a readable verdict is unmapped and keeps no TTL; a pointer that
does not read back leaves no TTL; an access that lands between the listing and
the import keeps its count; a field that does not read back is named with the
original row and the run exits nonzero; a row the sweep quarantined right after
the write is reported as quarantined, not as a mismatch, and a rerun carries it
marked `quarantined`; a record a person restored (no TTL again, listed with
`ttl_set` by an earlier run) is not given a TTL again, is named and stays marked
`restored`, while a record no earlier pointer lists still gets one; a rerun
after a TTL write that stopped part way finishes the rest and does not call them
restored; the pointer still lists each timestamp after the TTL write. The guard:
a real run with no recorded count refuses, writes nothing and names the dry run;
a dry run writes exactly one count row in `<repo>` and none in `<repo>_rdr`, and
the real run then proceeds with `--counted N`; a missing or different `--counted`
refuses; a count recorded for another phase, or for `--all`, does not unlock
this scope; a record the dry run did not count refuses; a run with nothing to
write needs no count; the count row is never listed and never flagged by the
census.

#### Step 4 (shipped): the census flag

A census flag in `nx memory curate --t2` (RDR-238) lists gate records older
than 30 days whose phase is closed and that have no TTL, so a missed close step
is visible. A phase is closed when its RDR's status is `closed`, or when its own
phase heading carries the close marker the `phase-review-gate` close step writes
(`### Phase N (CLOSED, YYYY-MM-DD)`); a record's phases are the `-p<N>` /
`-phase-<N>` pieces of its title, and each must be closed (a sub-phase such as
3a is closed when Phase 3 is) (nexus-qo7ie.28, 2026-11-06). Step 2 reuses this
rule. Step 3 removes `<repo>_rdr` from the projects the flag reads.

### Deferred: Phases 1 and 2

Kept as written and **not scheduled**. Trigger: *a re-census on the Phase 0b
counter shows a usable tail*. The re-census reads the change in `access_count`
since the Step 5 baseline, with the queries of research-1, after enough time
that the deltas mean something; what counts as a usable tail, and the
threshold, are Sam's call at that point on the bucket table, and are not
chosen here. Nothing in Phase 0b or Phase 3 depends on these phases, and
bead nexus-2k66t.1 waits for Phase 1 to be built.

#### Phase 1 (deferred): Criterion and mark

A row graduates when its heat exceeds a threshold chosen from the census (a
candidate shape: `access_count >= N` and `last_accessed` within D days; both
from data, not from this text). Graduation copies the row to T3 through the
existing promote path into a collection named by the caller, and writes
`promoted_at` (`TIMESTAMPTZ`) and `promoted_to` (the T3 document id) on the T2
row, one Liquibase changeset on `nexus.memory`. The mark makes the graduation
visible and idempotent: a marked row is not promoted twice. The mark is written
only after the T3 write and catalog registration return, in that order. The
command is attended: `nx memory graduate --project P --collection C
[--dry-run]`, printing what it will move before moving it. A `nx memory heat`
census command makes the measurement repeatable (counts per bucket of
`access_count` and of days since `last_accessed`, the top twenty rows, the
share never read, and the share of `last_accessed` stamps shared by ten or
more rows). Whether graduation also runs at rollup time is decided after the
re-census.

#### Phase 2 (deferred): TTL rule

For a row with a positive `ttl_days`, apply the source paper's division at
graduation: the T2 copy's `ttl_days` becomes `ceil(ttl_days / (1 + ln(
access_count + 1)))`, minimum one, and the T3 copy is permanent because it is
now the copy of record. For a permanent T2 row, graduation changes nothing
about its TTL; the T2 row stays as a permanent, marked row. That keeps
RDR-194's one TTL semantics and makes the rule say something for both kinds of
row.

The deferred phases' own test scenarios (census on a seeded store, graduate at
a threshold, rerun, forced T3 failure leaving no mark, the TTL rule for both
kinds of row, tenant isolation) stay in the Test Plan, marked deferred.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| gate-record pointer (T3) | `nx store list` on `nexus-rdr-research`, `gate_pointer` | `nx store get` by title | a rerun replaces it; removal is a person's `nx store` call | the loss check reads it back at write time | T3 backup |
| dry-run count (T2) | `nx memory list` in `<repo>`, titles `gate-rollup-count-rdr-<NNN>-...` | `nx memory get` | a later dry run replaces it; removal is `nx memory delete` | the real run reads it, `--counted` must equal it | PG backup |
| gate-record archive (T3) | `nx store list` on `nexus-rdr-research`, `gate_record` | `nx store get` by `<project>/<T2 title>` | a rerun rewrites none that reads back whole; removal is a person's `nx store` call | the run and the loss check read each back and compare it with the record | T3 backup |
| records given a 30-day TTL (T2) | `nx memory list` before expiry; `nx memory list --quarantined` after | `nx memory get` | RDR-207's reap, after an attended rollup | Step 4's census flag lists any still without a TTL | PG backup |
| read counter | not a new resource | `nx memory get` shows the count | not applicable | the MVV test | PG backup |
| promotion marks (deferred) | `nx memory list --promoted` | on the row | clearing a mark is `nx memory update`, in scope when built | count in `nx doctor` | PG backup |

### New Dependencies

None.

## Test Plan

In scope:

- **Scenario**: page-aware search stamps only the page; **Verify**: 30
  matching rows, `limit=5`: five rows at count 1, 25 at 0; next page stamps its
  five.
- **Scenario**: `silent` and no-`limit` requests; **Verify**: `silent`
  stamps nothing; a request with no `limit` returns the bare list and stamps
  all matches, byte-for-byte as before.
- **Scenario**: page past the end; **Verify**: empty rows, correct total, no
  stamp, no "no searchable terms" error; a query with no match at all still
  raises it.
- **Scenario**: old engine; **Verify**: a stub that ignores `limit` and
  answers a bare list yields the same page and total through `search_page`.
- **Scenario**: bad `limit`; **Verify**: the engine answers 400; the MCP tool
  reports a usage error without sending a request.
- **Scenario**: direct reads; **Verify**: by title, id and unique prefix each
  add one to the one row returned (unchanged).
- **Scenario**: gate-record listing; **Verify**: only whole-tag matches are
  listed; an untagged record mentioning `kind:gate` is not; `bead:nexus-1` is
  not `bead:nexus-12`; a stored `kind:gate, bead:x` matches; an older untagged
  record whose title names a listed bead is listed; a bead-less `kind:gate`
  record of the RDR is unmapped (under `--phase`, when its title names the
  phase); quarantined rows are not listed; the `<repo>_rdr` rows are neither
  listed nor changed.
- **Scenario**: pointer and loss check; **Verify**: closed phase with three
  records gets one pointer; all three titles resolve from it and each record's
  full text reads back from T3, a body over 16,384 bytes included; an open phase is
  refused; an unresolvable title stops the TTL step and names it; a
  not-justified newest record stops it; `--accept-open` passes and is recorded;
  a record with no bead takes no part in the chain and gets no TTL.
- **Scenario**: TTL; **Verify**: three records get `ttl=30` with every other
  field, `access_count` and timestamp unchanged; a record seeded with a null
  agent and session keeps both null, and the test fails when the step is
  switched to `put`, with `NX_AGENT` and a session set so a `put` control
  writes both; a never-accessed record stays never-accessed; rerun is a no-op;
  a forced pointer failure leaves no TTL; the `<repo>_rdr` rows get none and the
  census no longer flags them (`<repo>` only, `kind:gate` only); a row the sweep
  quarantined right after the write reads back as quarantined, not as a
  mismatch, and a rerun keeps it in the pointer; a record a person restored is
  not given a TTL again.
- **Scenario**: dry run and count-first guard; **Verify**: the dry run applies
  the closed-phase check, the verdict chain and the earlier-pointer check, writes
  no TTL and no T3 note, stamps no access, and says it skipped the pointer write
  and read-back; it reports exactly the number of candidates past `ttl_days * (1
  + ln(access_count + 1))`, the engine's rule, with a heat record inside its
  effective TTL not counted, a record exactly at it not counted (strictly
  greater), and a row with no timestamp not counted; a real run with no recorded
  count, a missing or different `--counted`, or a candidate the dry run did not
  count refuses and writes nothing; a count recorded for another phase or for
  `--all` does not unlock it.
- **Scenario**: tenant isolation; **Verify**: another tenant's rows are
  neither stamped, listed nor given a TTL.

Deferred with Phases 1 and 2:

- **Scenario**: census on a seeded store; **Verify**: buckets and top rows
  match the seed exactly.
- **Scenario**: graduate at threshold; **Verify**: only rows over the
  threshold are promoted; marks written; T3 documents exist and are
  catalogued.
- **Scenario**: rerun; **Verify**: nothing promoted; marks unchanged.
- **Scenario**: forced T3 failure; **Verify**: no mark, error reported, T2
  row unchanged.
- **Scenario**: TTL rule; **Verify**: a positive-TTL row's `ttl_days`
  shrinks by the stated formula with minimum one; a permanent row is
  unchanged; the T3 copy is permanent.

## Validation

### Testing Strategy

The MVV above in the default suite against the engine substrate. The
page-bound counter leg must fail for its stated reason when the page limit is
removed from the engine (the 25 outside rows then show a count of 1). The
pointer-before-TTL leg must fail for its stated reason when the ordering guard
is deleted. The listing tests include the untagged-body record, which is
the case that motivated reading whole tags. The null-attribution TTL test must
fail for its stated reason when the step is switched to `put`. The dry-run count
test must fail when the dry run stops matching the engine's rule: when the heat
term is dropped, when the comparison becomes non-strict or when the age is not
truncated to seconds. The count-first guard test must fail when the guard is
deleted. A new check (the
loss check) is run live once and its first run, with the counts examined
and skipped, goes in its bead before close. The first live run is the first run
without `--dry-run` that passes the loss check: it is the one that writes the
pointer and reads it back, so checks 1 to 3 all run (the dry run skips the
pointer write and read-back). Its counts are the records examined (the run
prints `examined N row(s) of <repo>, G with the tag kind:gate` first, and a
note when none of the gate rows is in the run, so a wrong `--beads` or
`--project` reading "0 listed" is visibly not a pass), those skipped by cause
(open phase, unresolvable title, verdict chain, unmapped, restored, changed since
archived) and those given a TTL; in the plan that run is nexus-1gdv0.2.17.

### Performance Expectations

Not estimated. A paged search returns fewer rows than today (the whole match
set), and the total is counted in the same statement as the page (one pass
over the matches), so it adds no second statement. The rollup's read is not
narrowed: it is one `get_all` of the project per run, as the census makes (the
nexus project held 3,158 rows on 2026-09-14; its size today is not measured).
The command's cost is that read and one write per record.

## Finalization Gate

> Written by the author of this revision on 2026-11-16, before the formal
> gate. The gate's own critique is recorded separately.

### Contradiction Check

One tension was found and resolved. Amendment A1 asks the loss check to match
each ship-blocker named in a gate record to a fix, but ship-blockers are free
text and the structured field is the verdict. The Proposed Solution therefore
checks the verdict chain per bead and prints non-justified records for a
person, and the Step 2 text says it is narrower than A1's wording. A second
tension: Step 4's census flags the `<repo>_rdr` gate rows, which Step 1 leaves
alone because live code reads them (research-8, -9); the plan removes
`<repo>_rdr` from the census projects. Otherwise no contradictions found between the research findings,
the design principles and the proposed solution: the deferred phases are the
only part that rests on an unresolved assumption (A1, A2, A3), and none of the
in-scope work depends on them.

### Assumption Verification

A5, A6 and A8 are verified by source search and recorded in research-4
to -6. A9 was reopened by the gate (the first revision's claim was false for a
row with a null agent or session) and is restated against the import route,
verified by source search in research-10 and pinned by the null-attribution test.
A1 is refuted as the
counter stands and is re-asked after Phase 0b, at the deferral trigger. A2 and
A3 belong to the deferred Phase 1 and stay unverified until it is revived.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `POST /v1/memory/search` body fields | engine handler (`MemoryHandler.java:317-331`) | Source Search: reads only `query`, `project`, `access`; an older engine ignores `limit` |
| `get_all` plus `split_tags` lists tagged rows | `memory_curate/__init__.py:560`, `:600`; `audit_tags.py:89-93` | Source Search (research-11) |
| `POST /v1/memory/import` stores every field as sent | engine (`MemoryHandler.java:660-667`, `:799-839`; `MemoryRepository.java:1393-1442`), client `http_memory_store.py:342-410` | Source Search (research-10) |
| `MemoryRepository.getAll` does not stamp access | engine (`MemoryRepository.java:623-634`) | Source Search |
| `put` fills attribution and overwrites agent and session, restarts the clock | `http_memory_store.py:258`, engine (`MemoryRepository.java:1444-1485`) | Source Search (research-10) |
| T3 pointer write by title | `store_put` / `nx store put` | Docs Only (`docs/collections.md` Rule 1; a titled re-put replaces); first run confirms |

### Scope Verification

The MVV is in scope and is not deferred: it covers both in-scope pieces and
runs in the default suite. Phases 1 and 2 are deferred inside this RDR with a
named trigger, and nothing the MVV or the in-scope phases do reads a mark,
a threshold or a graduated row.

### Cross-Cutting Concerns

- **Versioning**: the one wire change, the search page, is additive (an
  `[additive]` ledger entry, direction safety stated for each direction); the
  engine deploys before the client tag. Phase 3 changes no wire.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: the counter change rides the next engine release, and
  Phase 3 needs none; local and cloud engines run the same handler.
  Nothing is migrated at boot and no data is rewritten by a release (Sam's
  2026-11-10 rule): the counter's history is kept and Step 5's baseline is a
  recorded read, not a rewrite.
- **IDE compatibility**: N/A.
- **Incremental adoption**: a client that never sends `limit` keeps
  today's behaviour; the rollup is an attended command nobody has to run.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: the paged search returns less than today; the rollup
  does one unfiltered read of a project of a few thousand rows, the same read
  Step 4's census already makes.

### Proportionality

The document is long because it carries the deferred phases' full text and
three research entries' worth of citations. The in-scope design is two pieces
of about a day each. The Alternatives section could shed Alternative 5 (the
tuple template) to a one-line pointer at research-3 if the gate finds it
heavy; it is kept because it was a design input Sam was to weigh.

## References

- RDR-057 §Problem Statement item 1, §RF-3, §Instrumentation
- RDR-207 §Decision, §Validation table
- RDR-238 (index audit, the `--t2` command); RDR-240 (the `gate_record` procedure)
- `service/src/main/java/dev/nexus/service/db/MemoryRepository.java`
  (`:393-429` search, `:556-567` searchByTag, `:592-634` title kinds and getAll,
  `:679-709` expire, `:1393-1442` import, `:1444-1485` upsert)
- `service/src/main/java/dev/nexus/service/http/MemoryHandler.java`
  (`:309-331` search, `:337-361` list, `:455-462` all, `:660-667` import)
- `src/nexus/db/t2/http_memory_store.py:258`, `:342-410`, `:531-591`, `:735-767`;
  `src/nexus/mcp/core.py:8252-8258`; `src/nexus/commands/memory.py:169-187`,
  `:527-666`; `src/nexus/hooks/__init__.py:526`, `:656`
- `src/nexus/memory_curate/__init__.py:516-521`, `:586-615`, `:646`;
  `src/nexus/procedures/gate_record.lua:79-88`
- `conexus/skills/phase-review-gate/SKILL.md:24`; `docs/wire-contract-pending.md`
- T2 `nexus_rdr/209-research-1` to `-11`; T2 `nexus/draft-rdr-triage-2026-11-16`
- arxiv 2604.01707 (heat-based promotion and the division formula)

## Revision History

| Date | Change |
| --- | --- |
| 2026-09-14 | Filed as draft, split out of RDR-207 candidate (c) at Sam's direction. Phase 0 census is the first work; no threshold is chosen in this text. |
| 2026-09-14 | Phase 0 census run by conexus-e9 on the live tenant (T2 `209-research-1`). A1 refuted as the counter stands: search tracking stamps every match, so `access_count` measures search matches, not reads. Gap 5 and Phase 0b added; A5 added. |
| 2026-10-31 | Amendment A1 at Sam's direction (memory survey, nexus-qo7ie): Gap 6 and Phase 3, a phase-close rollup of gate records that keeps its sources, then a 30-day TTL so RDR-207 retires them. Sibling RDR-238 (memory curation) takes supersession, the index budget and promotion to skills, and uses Phase 0b's read counter when it lands. |
| 2026-11-16 | Narrowed per Sam's draft triage: in scope are Phase 0b and Phase 3 Steps 1 to 3; Phases 1 and 2 are kept and deferred with the trigger "a re-census on the Phase 0b counter shows a usable tail"; RDR-207, Phase 3 Step 0 and Step 4 recorded as shipped; Phase 0b designed (the engine takes `limit` and `offset` and stamps the returned page; `matching_total` in an object answer; wire-ledger entry) and Phase 3 Step 1 given a whole-tag filter on `/v1/memory/all`; the loss check and the `-gate-latest` exclusion added; stale anchors fixed (`hooks.py` is now the `src/nexus/hooks/` package) and every current-code claim re-read at 2e09b8cfc with research-4 to -8 recorded; bead nexus-2k66t.1's trigger noted as "Phase 1 built". |
| 2026-11-16 | Fix round after gate round 1, Sam's ruling "Fix the three, then accept" (S1, S2, S4 only; S3 and S5 to S8 stay in the critique record as implementation work). Step 3 writes through the verbatim import route and keeps the row's own timestamp, and A9 is reopened and restated (research-10); `--all` and Step 1 leave the `<repo>_rdr` gate rows alone, with research-9 recording their readers; the engine tag filter and A7 are dropped for a client-side `get_all` plus `split_tags` partition, so the search page is the only wire change (research-11). |
- 2026-11-16: Gate round 1 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); blob `43b12b69464f`; critique `nexus_rdr/209-gate-critique-2026-11-16-r1`.
| 2026-11-16 | Accepted (Sam, by dialog in halcyon's session: "Keep timestamps, count first"), with the Count first step (fix check `nexus_rdr/209-fix-check-78cc9b0e4776` PASS). Epic nexus-1gdv0. Residual dispositions: gate S3 nexus-1gdv0.1.2, S5 nexus-1gdv0.2.7, S6 nexus-1gdv0.2.4, S7 nexus-1gdv0.2.5, S8 nexus-1gdv0.3; fix check d021: nexus-1gdv0.2.2, .2.3, .2.9, .2.10, .2.11; fix check 78cc: nexus-1gdv0.2.12, .2.13, .2.14, .2.15. |
| 2026-11-18 | Gate finding S3 closed with the engine search page (nexus-1gdv0.1.1, nexus-1gdv0.1.2): the total is one statement (a CTE, a one-row count and the page left-joined), not a window count, so a page past the end still carries it and the page and the total share one snapshot; `limit` is clamped to 10,000 as `/v1/memory/list` clamps it and a bad `offset` is a 400. Wording amended under standing grant 3 to match the code reviewed at the nexus-1gdv0.1.3 gate (`MemorySearchPage`, tests in `MemorySearchPageTest`, `MemorySearchPageStatementsTest` and `MemoryRepositoryQuarantineReadPathTest`). |
| 2026-11-19 | Phase 3 Step 1, the command (nexus-1gdv0.2.1). `nx memory gate-rollup` is built with the pointer record as designed. The size question the gate left to the implementation (critique observation 4) is settled as split, not refuse: a pointer over 16,384 bytes is written in titled parts that the first part counts, and a rerun that needs fewer parts names the stale ones (a T3 delete is a catalog tombstone path, so none is deleted). A run that lists nothing writes no pointer. Wording only; the design is unchanged. |
| 2026-11-19 | Gate finding d021dc53c85b, first half (nexus-1gdv0.2.2): the "unmapped" list was unreachable, because under `--phase` a record was selected by a bead and under `--all` by `rdr:<NNN>`, so a record with no bead was never selected. The rule is now written in Step 1 and built: a `kind:gate` record that carries `rdr:<NNN>` and has no `bead:` tag is unmapped under `--all`, and under `--phase P` when its title also names phase P by the `-p<N>` / `-phase-<N>` rule (sub-phase 3a counts toward 3); a listed record with no readable verdict is unmapped; a `kind:gate` record with neither tag is in no run's scope. An untagged record whose title names no listed bead is not unmapped, because it has no `rdr:` tag to put it in scope, so the earlier wording "or a title that names none" is narrowed to match. Unmapped records get no TTL. |
| 2026-11-19 | Gate finding d021dc53c85b, second half (nexus-1gdv0.2.3): the fix round dropped `--include-titled`, so title matching became unconditional, and no predicate for "a title that names a listed bead" was written. It is now named in Step 1 and built (`title_names_bead`): the row has no tags at all, and its title holds the bead id as a whole token, `(?<![A-Za-z0-9.])<id>(?!\.?[A-Za-z0-9])`, case-sensitive, with a hyphen not counted as an id character because gate titles join words with it. It is on only under `--phase` (`--all` has no bead list), and a title naming two listed beads is unmapped. The Step 1 test list gains the negative cases. The predicate was checked on the live store: 628 of the 646 untagged gate-shaped titles that carry a bead-shaped id match each id they carry (the other 18 are an id cut short by the checking pattern, not by the predicate). |
| 2026-11-19 | Gate finding S6, pointer half (nexus-1gdv0.2.4): the pointer now lists each record's timestamp exactly as `get_all` returned it, unmapped records included, because the pointer was the one place that would keep a record's date after the TTL write and the reap and did not carry it. The second half of S6, what a rerun does with a record a person restored (`nx memory restore` clears the TTL), belongs to Step 3 with the TTL write and is not settled here. |
| 2026-11-19 | Gate finding S7 and Sam's ruling "Keep findings in full" (nexus-1gdv0.2.5): the claim that Steps 1 to 3 retire the gate records "without losing them", and the line "it keeps its sources by title and copies no body", overstated what survives, because after the TTL, quarantine, rollup and reap only the pointer and RDR-207's summary were left. The one-line excerpt default is withdrawn. Step 1 now writes each listed record's full text to T3 as a note of its own (subject `nexus-rdr-research`, `category=gate_record`, titled `<project>/<T2 title>`, split into titled parts when over 16,384 bytes), reads it back and compares it before the pointer is written, and the pointer names each archive and its part count; unmapped records are not archived. Step 2 reads every archive back before any TTL. Wording amended in Gap 6, the Approach, Step 1, Step 2, the test lists and the Day 2 table. The loss check's own read of the archive is Step 2 and is not built here. |
| 2026-11-19 | Phase 3 Step 2, the loss check (nexus-1gdv0.2.6), built before any write. The run refuses an open phase before it writes anything, the archives and the pointer included, by the census flag's own rule (closed status, or the phase heading's close marker; `--all` needs the RDR closed). Check 2 gains a meaning the earlier text lacked, because a title taken from the read that listed it is present by construction: a record an earlier pointer listed that the read no longer lists is carried into the new pointer (`state: absent`) when its archive reads back whole, and an archive that does not stops the run naming the title; so a record the reap deleted never drops out of the listing. The pointer is read back with the same list. A not-justified or partial newest record stops the run unless `--accept-open <bead>` (repeatable, recorded in the pointer, refused when it names no listed bead). The command moved out of `commands/memory.py` (now back under its 800-line cap) into `memory_curate/gate_cli.py`, registered in `cli.py`; the loss check lives in `memory_curate/gate_ttl.py`. |
| 2026-11-19 | Gate finding S5 (nexus-1gdv0.2.7): Step 2.3 said "newest record (by header date, then round)", but the round is optional, dates are day-granular and an older record has no header. The order is now written and built: header date, then round, then title; a headerless record is dated from the first date in its title, else its timestamp's day; the round is the title's `-r<N>` as a number, 0 when absent; when records tie on date and round the worse verdict decides. A terminal `partial` keeps the blocking rule as designed and says so; letting a partial that states no ship-blocker pass would change the design and is left to Sam. |
| 2026-11-19 | Phase 3 Step 3, the TTL write (nexus-1gdv0.2.8), built. A real run writes `ttl=30` through `import_entry` on each record that passed Step 2 and has no TTL, after the pointer is written and read back; every other field goes as `get_all` returned it, and a read-back compares each field and names a mismatch with the original row (exit nonzero, no undo). The values come from one `get_all` taken right before the writes, not one per record: a per-row `get` stamps access (A8) and a filtered `get_all` does not exist, so audit residual (2) is narrowed to the length of the write loop and stated beside the residual race rather than closed. `--dry-run` prints each candidate with its timestamp, `access_count` and whether the engine rule has it past its effective TTL, and one count. The census change is nexus-1gdv0.2.9. |
| 2026-11-19 | Fix check d021dc53c85b, defects B and C (nexus-1gdv0.2.9) and audit residual (4): the census change of Step 3 is built. `_census` reads only `<repo>`, its `<repo>_rdr` title-number branch and `_RDR_TITLE` are deleted, and its predicate is `kind:gate` only (a row tagged only `gate` is neither listed by the rollup nor flagged). The three census tests that pinned `<repo>_rdr` rows as flagged now seed them and assert they are not flagged; one is renamed; the sub-phase case moves to a `<repo>` row so its coverage is kept. Step 3 names the change. |
| 2026-11-19 | Fix check d021dc53c85b, defects A and C (nexus-1gdv0.2.10): the null-attribution test of Step 3 could not tell `put` from import, because in a bare test process `NX_AGENT` and the session both resolve to None and a `put` would also leave them null. The test now sets `NX_AGENT` and `NX_SESSION_ID` (and stops the config file leaking in), asserts first that a `put` under those settings writes both non-null, then asserts the import keeps both null; it refuses to pass when the control writes nothing. |
| 2026-11-19 | Fix check d021dc53c85b, defects B and C, A on cost (nexus-1gdv0.2.11): keeping the row's own timestamp means the expire sweep can quarantine an old record before it is read back, and `get_all` hides a quarantined row. The read-back now looks a missing row up in the quarantined listing before calling it missing and reports it as quarantined, restorable with `nx memory restore <id>`, still comparing its fields. A rerun carries a record an earlier pointer listed that the read no longer lists: `state: quarantined` when it is in the quarantined listing (whatever its archive, its row is restorable), `state: absent` when it is neither live nor quarantined and its archive reads back whole (the reaped-record rule of nexus-1gdv0.2.6), and stops naming it when that archive is gone. A run whose read lists nothing still refreshes an earlier pointer. The read cost per run is stated in Step 3: one `get_all` before and one after the writes, one quarantined listing and one pointer read, none per record. |
| 2026-11-19 | Gate finding S6, second half (nexus-1gdv0.2.4): `nx memory restore` makes a row permanent again (`ttl` NULL) and clears the rollup mark, so a restored gate record looks exactly like one never given a TTL, and a rerun would have given it a TTL again, undoing a person's decision. The rule is written in Step 3 (Rerun) and built, in the form the bead recommended (skip and name): each listed record in the pointer carries `ttl_set`, written false before the TTL writes and true after them for each record a run gave a TTL (the pointer is written a second time for that), so a rerun that finds a listed record with `ttl_set` and no TTL leaves it alone, prints `restored, left alone: <title>` and keeps it marked `restored`. A record the earlier pointer lists unmarked, whose write stopped part way, stays a candidate, so a partial write is not mistaken for a restore. The Consequences bullet now says Step 4's census flags a restored record again and why that is left as the reminder. |
| 2026-11-19 | Fix check 78cc9b0e4776, defect B and observations A and C (nexus-1gdv0.2.12): the RDR did not say which Step 2 checks the dry run applies, though it writes no pointer, nor which run is the loss check's first live run. Step 2 now says: `--dry-run` applies the closed-phase check, the verdict chain and the earlier-pointer half of check 2 (reading the earlier pointer and the quarantined listing), refuses exactly as the real run does, skips the archives, the pointer write and its read-back and says so, and counts the listed records that pass and have no TTL, minus unmapped and restored ones. The Testing Strategy defines the first live run as the first run without `--dry-run` that passes the loss check, with its counts (examined, skipped by cause, given a TTL) recorded in its bead. |
| 2026-11-19 | Fix check 78cc9b0e4776, defect A and observations B and C (nexus-1gdv0.2.13), with Sam's 2026-11-16 ruling "Per RDR and phase": "the real run waits for his word" had no mechanism, and the `phase-review-gate` and `rdr-close` skills name the command at close, so a session could run the real thing first. The dry run now records its count as one T2 row in `<repo>` (`gate-rollup-count-rdr-<NNN>-phase-<P>` or `-all`, tag `gate-rollup-count`, never `kind:gate` or `gate`), and a real run that would write a TTL refuses, writing nothing, unless the row exists, `--counted N` equals its count and every candidate is a title it lists; a run with no candidate needs none. The scope of "first run" is per RDR and phase, keyed like the pointer. The `--counted` flag was an open choice for Sam in the plan; the default taken here is that it is required, as evidence the caller has the number, and the RDR says it is not Sam's yes. "The dry run writes nothing" is corrected to no TTL, no T3 note and one count row. Both skills say to run `--dry-run` first, tell Sam its count and wait for his word. |
| 2026-11-19 | Fix check 78cc9b0e4776, defect C and observations A and B (nexus-1gdv0.2.14): the dry-run test seeded "one past and one not", which pinned neither the heat term nor the boundary, so a dry run that dropped `ln(access_count + 1)` or used `>=` would pass. Step 3 now states the engine rule the dry run computes (null timestamp skipped, age in whole seconds over 86,400, `ttl * (1 + ln(access_count + 1))`, strict comparison) and the test list pins it with a fixed clock: a heat record inside its effective TTL is not counted, a record exactly at it is not and one second older is, truncation to seconds, a null timestamp, and a mixed set. Each fails when its term is removed. |
| 2026-11-19 | Fix check 78cc9b0e4776, defect B (nexus-1gdv0.2.15), doc only: the dry-run test appeared only in the Implementation Plan, not in the Test Plan or the Testing Strategy. The Test Plan gains the dry-run and count-first-guard scenario and extends the TTL and loss-check scenarios to what was built (the real put control, the census narrowing, the quarantine read-back, the restored record); the Testing Strategy names the two legs that must fail when their code is changed (the dry-run count against the engine rule, the guard when deleted); and the reference "(Step 3, Count first)" in the timestamp bullet, which read as Technical Design's own Step 3, now points at the Implementation Plan, Phase 3 Step 3. The wording was copied from the merged code and tests of nexus-1gdv0.2.12 to .14, not from the beads. |
| 2026-11-19 | Review round 1 (nexus-1gdv0.2.16), blocker 1: the TTL write imported the fresh read's content while the archive and the pointer held the listing's, so a record rewritten between them got a TTL with no archive of its new text. A candidate whose fresh content or timestamp differs from the listing's is now skipped, named and left without a TTL, and a rerun archives its new text first; the residual-race paragraph says so. |
| 2026-11-19 | Review round 1, blocker 2: the quarantined listing the carry reads before the writes was reused by the read-back, so a row the sweep quarantined after its import (not in that older listing) read as "not in the read after the write". The read-back now makes its own listing; Step 3's read cost says up to two. |
| 2026-11-19 | Review round 1, blocker 4: the count guard compared only the recorded number, so time passing (a record crossing its effective TTL after the dry run) left the count Sam was told stale while the guard still passed. The real run now recomputes the engine rule over its candidates at its own clock and refuses when the number differs from the recorded count; the guard paragraph says so. |
| 2026-11-19 | Review round 1, blocker 5: `--project` accepted `<repo>_rdr` and any other project, so the open-phase check could read one repository's RDR files for another repository's records. A project ending `_rdr` is refused, and so is a project that is not the working directory's repository (the check reads that checkout's RDR files and nothing maps a project name to another checkout; refusing was chosen over a lookup because the command runs from the checkout it rolls up). |
| 2026-11-19 | Review round 1, blocker 6: the pointer read-back compared only (project, title), so a pointer that came back with different `ttl_set`, `restored` or `state` marks, the ones the next run decides from, passed. It now compares those too. |
| 2026-11-19 | Review round 1, blocker 7: a wrong `--beads` or `--project` printed "0 listed", which read as a pass. The output now starts with the rows examined and the `kind:gate` rows seen, and says so when none of the gate rows is in the run; the first live run's counts (nexus-1gdv0.2.17) come from these lines. |
| 2026-11-19 | Review round 1, wording (nexus-1gdv0.2.16): the Step 1 test-list sentence "no run writes anything to T2" is corrected (the dry run writes one count row; no run changes a gate record's text, tags or timestamp); the count row is in the Day 2 table; "is a mechanism, not a process rule" now says what the code requires (a current, recorded count) and that Sam's word is a process step the skills state; "every other field" is narrowed, because the import route also clears the quarantine stamps, re-stamps the row's identity with the caller and truncates the timestamp to the second; and the cross-scope limit of the restore rule is stated (a restore after a `--phase` run is not protected from a later `--all` run). |
