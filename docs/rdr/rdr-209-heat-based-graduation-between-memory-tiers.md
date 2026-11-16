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
Context Management). The fix delivers a phase-close rollup that keeps its
sources, and a TTL on the records it covers.

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
recorded in T2 `nexus_rdr/209-research-4` to `-8`, one entry per group of
claims, each written before the sentences that rest on it.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `MemoryRepository.search` and `/v1/memory/search` | Yes (research-4) | fetches every match with no limit and stamps all of them (`MemoryRepository.java:393-429`); the handler reads no `limit` (`MemoryHandler.java:309-331`) |
| MCP `memory_search`, CLI `nx memory search` | Yes (research-4) | the MCP tool slices client-side (`mcp/core.py:8252-8258`); the CLI prints every match (`commands/memory.py:169-187`) |
| engine-side narrowing precedents | Yes (research-5) | `title_kinds` on `/v1/memory/all` (`MemoryRepository.java:592-634`, `http_memory_store.py:735-767`) and `limit` with `matching_total` on `/v1/memory/list` (`MemoryHandler.java:337-361`) |
| `expire`, upsert, promote | Yes (research-6) | the TTL formula reads `access_count` (`MemoryRepository.java:697`); upsert keeps `access_count`, restarts the age clock, clears the quarantine stamps (`:1444-1485`); no route sets only a TTL (`MemoryHandler.java:27-47`) |
| Phase 3 state | Yes (research-7) | Steps 1 to 3 are not built; no whole-tag list filter exists (`MemoryRepository.java:556-567`, `:453-467`, `:592-634`) |
| readers of RDR gate rows | Yes (research-8) | `*-gate-latest` rows are read by the rdr hook and the plans lifecycle stream (`hooks/rdr_verb.py:539-562`, `plans/lifecycle_stream.py:93`, `:396-406`) |

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
- **Verified** (research-6) The engine has no route that sets only a row's
  TTL. Giving an existing row a TTL means writing it again with the same
  content. That write keeps `access_count`, restarts the row's age clock, and
  clears any quarantine stamps. The expiry formula then lets a row live
  `ttl_days * (1 + ln(access_count + 1))` days, so a gate record with a 30-day
  TTL that was counted 50 times lives about 147 days.
- **Verified** (research-8) The RDR gate flow's `{id}-gate-latest` rows are
  read by live code for every RDR in a project, closed ones included. A TTL
  on them would remove a row those readers look up.
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
- [ ] A7: an engine filter that matches one whole tag selects exactly the rows
  the client's `split_tags` (`src/nexus/tables/audit_tags.py:89`, which splits
  on commas and trims spaces) calls tagged. **Status**: Unverified. The
  existing SQL pattern in `searchByTag` (`(',' || tags || ',') LIKE
  '%,tag,%'`, `MemoryRepository.java:565`) does not trim spaces, so it would
  differ on a stored value such as `kind:gate, bead:x`. **Method**: a test
  seeds that exact row and pins both sides to the same answer; the engine
  pattern allows optional spaces around the tag.
- [x] A8: the loss check can resolve every listed title without stamping
  access. **Status**: Verified by source search: `getAll` never calls the
  tracking helpers (`MemoryRepository.java:623-634`), so a check built on
  `get_all` leaves `access_count` alone, where a `get` per title would add one
  to each record.
- [x] A9: a TTL set by writing the record again changes nothing but the
  timestamp and TTL. **Status**: Verified by source search (research-6):
  content, tags, session and agent come back as the caller sends them, so the
  caller must send every stored field unchanged. The write is checked by
  reading the row back.
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
phase close, `nx memory gate-rollup` lists the phase's gate records in a
pointer record in T3, checks that nothing the records say is lost, and only
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
  "matching_total": N}`. `N` comes from the same statement as the rows (a
  window count), so the page and the total cannot disagree under a concurrent
  write. A `limit` below 1 is a 400.
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

**Step 1 part (a), the whole-tag list filter.** `GET /v1/memory/all` gains an
optional `tags` parameter, a comma-separated list of whole tags that a row must
all carry. It follows the `title_kinds` precedent (`MemoryRepository.java:592-634`,
`http_memory_store.py:735-767`): the engine filters, the response carries
full rows including `tags`, and the client re-cuts the result so that an engine
that ignores the parameter (or answers 400) still yields exactly the matching
rows. The match is on one whole tag, with optional spaces around it, so
`bead:nexus-1` does not match `bead:nexus-12` and an untagged record that
mentions `kind:gate` in its body is not returned (A7). The tag is escaped as
`searchByTag` escapes it (`:559-562`). Quarantined rows stay hidden. The
change is additive in the wire ledger's sense: an old client never sends
`tags`; a new client on an old engine gets every row and cuts it.

**Step 1 part (b), the command and the pointer record.** `nx memory
gate-rollup --rdr N (--phase P --beads b1,b2,... | --all) [--project REPO]
[--include-titled] [--dry-run]`. The bead list for a phase comes from the
`phase-review-gate` close step, which already writes `Item N = <bead-id>` for
every item; the command does not call `bd`, because bead-to-phase membership
lives in the issue tracker, not in T2.

- *Which records.* For each listed bead, `get_all(<repo>, tags=[kind:gate,
  bead:<id>])`. With `--all` (used at `rdr-close`), `tags=[kind:gate,rdr:<NNN>]`
  instead. Older records carry no tags; `--include-titled` adds one unfiltered
  `get_all` of the project and matches them by a listed bead id in their
  title, the 72% the title mapping reaches (research-2). Records that map to
  no listed bead are not touched. The RDR gate flow's own rows in
  `<repo>_rdr` (tags `rdr,gate,...`, titled by RDR number) are per RDR, not per
  phase, so only an `--all` run lists them, read by project and the `gate` tag
  and mapped to the RDR by the number leading the title. That skill is not
  retagged.
- *The pointer record.* One T3 record per phase (per RDR for `--all`), in the
  existing knowledge subject `nexus-rdr-research` (`docs/collections.md`
  Rule 1: reuse a subject, and RDR postmortems already live in a subject under a
  category), `category=gate_pointer`, titled `gate-records-rdr-<NNN>-phase-<P>`
  or `-all`, so a rerun replaces it. It lists, for each record, its project,
  title, kind, bead, the sha it reviewed and its verdict, taken from the
  field header `gate_record` writes (`gate_record.lua:79-88`) or, for an older
  record, from its last `outcome` line. It is a pointer: it keeps its sources
  by title and copies no body. Records whose verdict or bead cannot be read
  are listed under "unmapped" and keep no TTL.
- *The trigger.* The `phase-review-gate` skill's close step (`SKILL.md:24`) and
  the `rdr-close` skill name the command. Nothing blocks a close without it;
  Step 4's census is what shows a missed run.

**Step 2, the loss check.** Before any TTL is set, the command stops and names
the cause if any of these fail:

1. The phase is closed: the RDR's frontmatter status is `closed`, or every
   phase heading it names carries the close marker (the rule Step 4 uses,
   `memory_curate/__init__.py:516-521`, `:646`). A record for an open phase is
   never touched.
2. Every listed title is present in the read that listed it, and the pointer
   record reads back from T3 by its title with the same list. These reads do
   not stamp access (A8).
3. For each listed bead, its newest record (by header date, then round) has
   the verdict `justified`. A bead whose newest record is `partial` or
   `not-justified` stops the TTL step and is named, unless the run names it
   with `--accept-open <bead>`, which the pointer records.

This is narrower than Amendment A1's wording, "each ship-blocker named in a
gate record is either fixed by a listed sha or names its bead". A ship-blocker
is free text and a script cannot tell one from a quoted example, so the check
runs on the verdict chain per bead, which is structured, and prints each
non-justified record for a person to read. The reviewer stays accountable for
what the text says.

**Step 3, the TTL.** For each listed record that passed Step 2 and has no TTL,
write it again with `ttl=30` and every other stored field (project, title,
content, tags, agent, session) unchanged, then read it back and compare. A
mismatch is reported and the record is left as it is.

- *Why a write and not a TTL route.* The engine has no TTL-only route
  (research-6). Adding one would be a second wire change; the write is
  client-only and gives the side effect we want: the 30 days start at the
  rollup, not at the record's creation, so a person who looks for a record
  has 30 days after the pointer exists.
- *What expiry does.* Quarantine only, as RDR-207 built it: the record is
  hidden, restorable with `nx memory restore ID`, and deleted only after an
  attended `nx memory rollup` marks it and `nx memory reap` runs. A record's
  heat lengthens its life by `1 + ln(access_count + 1)`, so a much-read
  record outlives 30 days; that is the existing formula, not a change.
- *Rows that never get a TTL.* `{id}-gate-latest` rows keep none: the rdr hook
  and the plans lifecycle stream read them for every RDR, closed ones
  included (research-8). They are listed in the pointer, never TTL'd. Step 4's
  census currently flags them forever, so this step also makes `_census` skip
  titles ending `-gate-latest`.
- *Rerun.* The command is idempotent: the pointer is replaced, records that
  already have a TTL are skipped.
- *Residual race.* The write sends the content read a moment earlier. A
  reviewer who rewrote the same title in that moment would be overwritten.
  The phase is closed and its reviewers are done, and the engine offers no
  compare-and-set, so this is accepted and stated.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| page-aware search | `MemoryRepository.search`, `/v1/memory/search` | Extend: optional `limit`/`offset`, same ordering |
| client page call | `HttpMemoryStore.search`, `T2Database.search` | Add `search_page`; leave `search` alone |
| matching total | `/v1/memory/list` `matching_total` | Same idea, object envelope because a page can be empty |
| whole-tag filter | `/v1/memory/all` `title_kinds` (nexus-pxp44) | Extend: `tags` parameter, client re-cut |
| pointer record | `nexus-rdr-research` knowledge subject, `category` | Reuse the subject, new category |
| loss check reads | `get_all` (no access stamping) | Reuse |
| 30-day TTL | upsert via `HttpMemoryStore.put` | Reuse: a write with `ttl=30` |
| retirement | RDR-207 quarantine, rollup, reap | Reuse unchanged |
| missed-close visibility | `_census` in `memory_curate` (Step 4) | Reuse; skip `-gate-latest` titles |
| heat census (deferred) | `MemoryRepository` access columns | Reuse: one aggregate query |
| graduation copy (deferred) | `commands/memory.py` promote path | Reuse: call it per row |
| mark on T2 row (deferred) | RDR-207's `rolled_up_at` pattern | Extend: sibling columns |
| TTL rule (deferred) | `expire()` heat formula | Reuse the same heat term |

### Decision Rationale

A counter that counts rows nobody saw cannot carry a threshold, and it is
already feeding the shipped TTL and RDR-238's audit, so fixing it is
worthwhile before graduation is. The gate records are a separate, live
problem that needs no threshold at all: they are written about 1,000 times a
month and never expire. Each piece is a day of work against known code, and
the engine changes for both ship in one engine release. Graduation needs a
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

### Alternative 4: Search the tags by their own text instead of adding a filter

**Description**: for Step 1, call `search_by_tag(query=<the tag text>,
tag=<the tag>)` and rely on the exact-tag condition for precision.

**Pros**: no engine change.

**Cons**: the query still has to match the full-text index, which tokenizes
tags such as `kind:gate` and `bead:nexus-qo7ie.1` by Postgres's own rules; the
RDR has not verified that every bead id tokenizes the same way on both sides.
The 2026-10-31 survey already found full-text matching on tag words returns
untagged records.

**Reason for rejection**: it works only if an unmeasured tokenizer behaviour
holds, and the engine change is small and has a precedent.

### Alternative 5: Gate-record index as a tuple-space template

**Description**: research-3 (2026-10-31, from linda) proposed an append-only
tuple template keyed by (kind, rdr, bead) with pointer bodies.

**Reason for rejection**: not adopted and not pursued here. It would couple
this work to the tuple space (the RDR-110 to 119 arc warns against bundling
them), ride its own engine template cut, and the whole-tag filter plus a T3
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
- A gate record rewritten for its TTL moves its timestamp forward, so it
  drops out of Step 4's census (the census wants an old record with no TTL, and
  it now has one).
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
- **Risk**: the TTL write overwrites a record rewritten in the same moment.
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
- [ ] A7 verified by its test during Phase 3 Step 1.
- [ ] Bead filing waits for accept (no new beads while the burn-down runs).

### Minimum Viable Validation

Two proofs, both in the default suite against the engine substrate. Counter:
seed 30 rows that match one query, search with `limit=5`; exactly those five
rows have `access_count` 1 and the other 25 have 0; the next page stamps its
five; a `silent` search stamps none; a search with no `limit` stamps all 30.
Gate records: a seeded closed phase with three gate records gets one pointer
record that lists all three titles, all three then carry a 30-day TTL and read
back unchanged; a seeded open phase is untouched; a planted unresolvable
title stops the TTL step and names the title; an untagged record whose body
says `kind:gate` is never returned by the tag filter.

### Phase 0b: a read counter that means received

Sized at about a day of work plus the engine release it shares with Phase 3.

#### Step 1: Engine, page-aware search

`MemoryRepository.search` takes an optional page and stamps only the returned
rows; the handler reads `limit` and `offset`, answers the object form, and
returns 400 for a `limit` below 1. Tests in the repository and in
`MemoryHandlerTest.java` (the search tests start at `:288`): the MVV counter
proof, an empty page past the end (no error, no stamp, total reported), the
no-`limit` request unchanged, `silent` stamps nothing, tenant isolation.

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

Does not depend on Phase 0b. Sized at about a day, in the same engine release
as Phase 0b. Research 2 (2026-10-31) shaped it: only about 72% of the 2,320
gate-record titles name a bead, so a title-only rollup misses about 23%; and
the `phase-review-gate` skill says nothing enforces its close step, so the
trigger is `rdr-close` and the close of the phase's review bead, not the skill
alone.

#### Step 0 (shipped): tags at write time

Reviewer and developer write-backs carry `kind:gate`, `bead:<id>` and
`rdr:<NNN>` tags (`rdr:` dropped when the bead serves no RDR), shipped by
nexus-qo7ie.1 and pinned by `tests/test_gate_record_tags_lint.py`, so new gate
records map to a phase without parsing the title. Older records are mapped by
title; the ones that do not map are listed as unmapped in the pointer record
and keep no TTL. The lookup is an exact tag match, not a search: `memory_search`
tokenizes, and a search for `kind:gate` on 2026-10-31 returned 273 hits, the
first five untagged critiques matched on words in their bodies (the
nexus-qo7ie.1 critique). That is why Step 1 adds a whole-tag filter.

#### Step 1: The tag filter, the command and the pointer record

As designed under Technical Design: part (a) the `tags` parameter on
`/v1/memory/all` with the client re-cut (an `[additive]` ledger entry and the
same engine release as Phase 0b); part (b) `nx memory gate-rollup`, the pointer
record in T3, and the one-line change to the two skills that name it. Tests: the
filter returns only whole-tag matches and not an untagged record that mentions
`kind:gate`; `bead:nexus-1` does not match `bead:nexus-12`; a row stored as
`kind:gate, bead:x` matches on both the engine and the client (A7); a tag with
`_` or `%` is escaped; quarantined rows are hidden; another tenant's rows are
not returned; the engine-ignores-`tags` fallback against a stub; a seeded closed
phase yields one pointer listing its three records.

#### Step 2: The loss check

As designed. Tests: an open phase is refused; a planted unresolvable title
stops the run and names it; a bead whose newest record is `not-justified`
stops the run and is named, and `--accept-open` lets it through and appears in
the pointer; the check leaves every record's `access_count` unchanged (A8).

#### Step 3: The 30-day TTL

As designed. Tests: three seeded records each get `ttl=30` with content, tags,
agent and session unchanged and `access_count` unchanged; a rerun changes
nothing; a forced pointer-write failure leaves no TTL; a `-gate-latest` row is
listed but gets no TTL and `_census` no longer flags it; an `--all` run lists
the `<repo>_rdr` critique and fix-check rows; a planted record without a
readable verdict is unmapped and keeps no TTL.

#### Step 4 (shipped): the census flag

A census flag in `nx memory curate --t2` (RDR-238) lists gate records older
than 30 days whose phase is closed and that have no TTL, so a missed close step
is visible. A phase is closed when its RDR's status is `closed`, or when its own
phase heading carries the close marker the `phase-review-gate` close step writes
(`### Phase N (CLOSED, YYYY-MM-DD)`); a record's phases are the `-p<N>` /
`-phase-<N>` pieces of its title, and each must be closed (a sub-phase such as
3a is closed when Phase 3 is) (nexus-qo7ie.28, 2026-11-06). Step 2 reuses this
rule. Step 3 adds one exclusion to the flag, the `-gate-latest` titles.

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
- **Scenario**: whole-tag filter; **Verify**: only whole-tag matches; an
  untagged record mentioning `kind:gate` is absent; `bead:nexus-1` is not
  `bead:nexus-12`; spaces around a stored tag match on engine and client; `_`
  and `%` escaped; quarantined hidden; AND across several tags; the
  engine-ignores-`tags` fallback.
- **Scenario**: pointer and loss check; **Verify**: closed phase with three
  records gets one pointer; all three titles resolve from it; an open phase is
  refused; an unresolvable title stops the TTL step and names it; a
  not-justified newest record stops it; `--accept-open` passes and is recorded.
- **Scenario**: TTL; **Verify**: three records get `ttl=30` with every other
  field and `access_count` unchanged; rerun is a no-op; a forced pointer
  failure leaves no TTL; `-gate-latest` rows get none and the census skips them.
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
is deleted. The tag-filter tests include the untagged-body record, which is
the case that motivated the filter. A new check (the filter's A7 parity test,
the loss check) is run live once and its first run, with the counts examined
and skipped, goes in its bead before close.

### Performance Expectations

Not estimated. A paged search returns fewer rows than today (the whole match
set), and the window count runs in the same statement as the page, so it adds
no second scan. The tag filter narrows the read in the engine: a project of
several thousand rows crosses the wire as the gate records wanted, as
`title_kinds` did for the rdr hook (a project of 1,527 rows and 4.6 MB read as
378, `src/nexus/db/t2_reads.py:50-60`). The command's cost is one filtered read
per listed bead and one write per record.

## Finalization Gate

> Written by the author of this revision on 2026-11-16, before the formal
> gate. The gate's own critique is recorded separately.

### Contradiction Check

One tension was found and resolved. Amendment A1 asks the loss check to match
each ship-blocker named in a gate record to a fix, but ship-blockers are free
text and the structured field is the verdict. The Proposed Solution therefore
checks the verdict chain per bead and prints non-justified records for a
person, and the Step 2 text says it is narrower than A1's wording. A second
tension: Step 4's census flags `-gate-latest` rows, which Step 3 must not give
a TTL because live code reads them (research-8); the plan makes the census
skip them. Otherwise no contradictions found between the research findings,
the design principles and the proposed solution: the deferred phases are the
only part that rests on an unresolved assumption (A1, A2, A3), and none of the
in-scope work depends on them.

### Assumption Verification

A5, A6, A8 and A9 are verified by source search and recorded in research-4
to -6. A7 (engine and client agree on what a whole tag is) is unverified and is
verified by its own test in Phase 3 Step 1, before the filter is relied on; the
engine pattern is written to match `split_tags` rather than copying
`searchByTag`'s pattern, which does not trim spaces. A1 is refuted as the
counter stands and is re-asked after Phase 0b, at the deferral trigger. A2 and
A3 belong to the deferred Phase 1 and stay unverified until it is revived.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `POST /v1/memory/search` body fields | engine handler (`MemoryHandler.java:317-331`) | Source Search: reads only `query`, `project`, `access`; an older engine ignores `limit` |
| `GET /v1/memory/all` params | engine handler (`MemoryHandler.java:455-462`) | Source Search: `title_kinds` precedent; `tags` is new |
| `HttpMemoryStore.get_all` re-cut | `http_memory_store.py:735-767` | Source Search: client already re-cuts for `title_kinds` |
| `MemoryRepository.getAll` does not stamp access | engine (`MemoryRepository.java:623-634`) | Source Search |
| upsert keeps `access_count`, restarts the clock | engine (`MemoryRepository.java:1444-1485`) | Source Search |
| T3 pointer write by title | `store_put` / `nx store put` | Docs Only (`docs/collections.md` Rule 1; a titled re-put replaces); first run confirms |

### Scope Verification

The MVV is in scope and is not deferred: it covers both in-scope pieces and
runs in the default suite. Phases 1 and 2 are deferred inside this RDR with a
named trigger, and nothing the MVV or the in-scope phases do reads a mark,
a threshold or a graduated row.

### Cross-Cutting Concerns

- **Versioning**: both wire changes are additive (`[additive]` ledger entries,
  direction safety stated for each direction); the engine deploys before the
  client tag.
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: the engine change rides the next engine release, one
  release for both pieces; local and cloud engines run the same handler.
  Nothing is migrated at boot and no data is rewritten by a release (Sam's
  2026-11-10 rule): the counter's history is kept and Step 5's baseline is a
  recorded read, not a rewrite.
- **IDE compatibility**: N/A.
- **Incremental adoption**: a client that never sends `limit` or `tags` keeps
  today's behaviour; the rollup is an attended command nobody has to run.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: the paged search returns less than today; the tag
  filter narrows the read; `--include-titled` does one unfiltered read of a
  project of a few thousand rows, the same read Step 4's census already makes.

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
  `:679-709` expire, `:1444-1485` upsert)
- `service/src/main/java/dev/nexus/service/http/MemoryHandler.java`
  (`:309-331` search, `:337-361` list, `:455-462` all)
- `src/nexus/db/t2/http_memory_store.py:531-591`, `:735-767`;
  `src/nexus/mcp/core.py:8252-8258`; `src/nexus/commands/memory.py:169-187`,
  `:527-666`; `src/nexus/hooks/__init__.py:526`, `:656`
- `src/nexus/memory_curate/__init__.py:516-521`, `:586-615`, `:646`;
  `src/nexus/procedures/gate_record.lua:79-88`
- `conexus/skills/phase-review-gate/SKILL.md:24`; `docs/wire-contract-pending.md`
- T2 `nexus_rdr/209-research-1` to `-8`; T2 `nexus/draft-rdr-triage-2026-11-16`
- arxiv 2604.01707 (heat-based promotion and the division formula)

## Revision History

| Date | Change |
| --- | --- |
| 2026-09-14 | Filed as draft, split out of RDR-207 candidate (c) at Sam's direction. Phase 0 census is the first work; no threshold is chosen in this text. |
| 2026-09-14 | Phase 0 census run by conexus-e9 on the live tenant (T2 `209-research-1`). A1 refuted as the counter stands: search tracking stamps every match, so `access_count` measures search matches, not reads. Gap 5 and Phase 0b added; A5 added. |
| 2026-10-31 | Amendment A1 at Sam's direction (memory survey, nexus-qo7ie): Gap 6 and Phase 3, a phase-close rollup of gate records that keeps its sources, then a 30-day TTL so RDR-207 retires them. Sibling RDR-238 (memory curation) takes supersession, the index budget and promotion to skills, and uses Phase 0b's read counter when it lands. |
| 2026-11-16 | Narrowed per Sam's draft triage: in scope are Phase 0b and Phase 3 Steps 1 to 3; Phases 1 and 2 are kept and deferred with the trigger "a re-census on the Phase 0b counter shows a usable tail"; RDR-207, Phase 3 Step 0 and Step 4 recorded as shipped; Phase 0b designed (the engine takes `limit` and `offset` and stamps the returned page; `matching_total` in an object answer; wire-ledger entry) and Phase 3 Step 1 given a whole-tag filter on `/v1/memory/all`; the loss check and the `-gate-latest` exclusion added; stale anchors fixed (`hooks.py` is now the `src/nexus/hooks/` package) and every current-code claim re-read at 2e09b8cfc with research-4 to -8 recorded; bead nexus-2k66t.1's trigger noted as "Phase 1 built". |
