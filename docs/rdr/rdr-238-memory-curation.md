---
title: "Memory Curation: Supersession by Deletion, a Bounded Always-Loaded Index, and Habits That Run Themselves"
id: RDR-238
type: Feature
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-10-31
accepted_date:
related_issues: [nexus-qo7ie]
related_rdrs: [RDR-057, RDR-207, RDR-209, RDR-074, RDR-192]
---

# RDR-238: Memory Curation: Supersession by Deletion, a Bounded Always-Loaded Index, and Habits That Run Themselves

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** From the 2026-10-31 memory survey (artifact
`https://claude.ai/artifact/9gEbyn1wFYhWh9sTJvoT8a`; T2
`nexus/memory-survey-papers-agent-memory-2026-10-31` and
`nexus/memory-survey-papers-context-load-2026-10-31`). Sam approved the
direction the same day: one RDR for supersession, the index and promotion to
skills; gate records go to RDR-209 as an amendment. Sam's review the same day
changed supersession to deletion and moved promotion out (see Revision
History).

## Problem Statement

Nexus keeps memory in four places: T1 session scratch, T2 notes in the engine,
T3 permanent knowledge, and Claude Code's file memory
(`~/.claude/projects/<repo>/memory/`, one rule per file, indexed by
`MEMORY.md`). We have machinery for writing memory and, since RDR-207, for
expiring it. We have none for curating it: saying that one entry replaces
another, and deciding what is worth loading into every context. Sam's phrase
for the job is garbage collection. The collector has no roots, no marks and no
schedule.

### Enumerated gaps to close

#### Gap 1: A replaced entry keeps being served

Neither T2 nor file memory can say "this replaces that". When a rule is
reversed or a note is superseded, the old one stays readable and searchable
until someone deletes it by hand, and T2 has no deletion habit at all
(5,435 rows in project `nexus`; RDR-209's census found 39 rows with a TTL).
"When Retrieval Is Not Enough: Obsolete Facts in Agent Memory" measured why
this matters: an obsolete version of a fact in context did more harm than an
unrelated one, taking Qwen2.5 from 100% to 44.6%, and every harmful flip in
its final intervention was a stale selection. A TTL ages an entry; it never
says what replaced it. The fix delivers a way for a write to name the entry it
replaces, and the replaced entry is deleted, on T2 rows and on memory files.
The new entry keeps one line of lineage naming what it replaced.

#### Gap 2: The always-loaded index has no budget and no admission rule

`MEMORY.md` is loaded into every main session and every subagent: 136 lines,
about 15 KB, about 3.6K tokens of a subagent's 77K starting context
(measured 2026-10-31). It indexes 151 files, and every new memory adds a
line. Nothing decides which rules must be in every context and which only need
to be findable. Over 30 days the files took 225 writes and 68 reads, and the
reads touched 32 of the 151 files, so most of the value is in the index lines
and most index lines are rarely acted on. IFScale ("How Many Instructions Can
LLMs Follow at Once") puts the knee for its best models near 150 simultaneous
instructions, and between the two `CLAUDE.md` files and the index we carry
well over 100 rules. The fix delivers a token budget for the index, a stated
admission rule, and an on-demand tier for everything else.

#### Gap 3: Curation depends on the writer remembering to do it

Every rule above is a habit today, and habits drift: the index grew one line
per memory, files were split from a bundle and never indexed (a few dozen
files are reachable only through another file), and no one measures any of
it. Sam: "we need to somehow get that ensconced in our habits." The fix
delivers checks that run without anyone remembering them, and a measurement
that says whether the curation helped.

The same drift reaches beyond memory, to anything that keeps running or
keeping after its purpose ends with nobody assigned to notice. Found on
2026-10-31 alone: the main `MEMORY.md` injected into every subagent (about
15M tokens in 60 days, research 5); thirteen plugins unused for 30 days (about
4K tokens per session start); 19 detached `nx upgrade --auto` walkers
saturating the cloud edge (nexus-75w5d); about 2,500 permanent gate records;
and the RDR-205 session ledger, whose writer and readers 7.70.0 deleted while
its engine template and 40,785 rows stayed (nexus-onjvw). Sam: "we want to be
sure we're paying attention to things and ensuring we're not generating a lot
of heat and waste." The fix also delivers a last-use signal for every store of
state this project owns, read on a schedule: the curation report's cold-state
section (Technical Design 4).

Procedures stored as memory, and procedures recalled or improvised instead of
run, moved to their own RDR on 2026-10-31 (nexus-qo7ie.2): they become
sandboxed Lua scripts on a Lua runtime (nexus-mdbds.1), not new Python verbs
or MCP tools. The removed text stays in the history of branch
`feature/nexus-qo7ie-memory-curation-rdr`, in the draft as filed before this
revision.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-057 Progressive Formalization Across Memory Tiers | Origin of the tier model | Its "graduation" idea is the T2 to T3 case. This RDR is the curation case beside it: replacement and admission to context. |
| RDR-207 Summarization at the T2 Expiry Boundary | Beside, no shared path | It owns what expiry may destroy (quarantine, rollup, reap only rolled-up rows). Replacement is not expiry: a writer names a row and the engine deletes it in the same transaction. Phase 1 Step 1 confirms the delete takes the path an explicit `memory_delete` takes. |
| RDR-209 Heat-Based Graduation Between Memory Tiers | Sibling, draft | It owns heat and graduation, and takes the gate-record rollup as an amendment (same date). Its Phase 0b, a read counter that means "a caller read this row", is the signal this RDR's index audit would use for T2; until it lands, the audit counts reads from transcripts. |
| RDR-074 Permanence Mode | Deferred, compatible | Permanence guards a row against expiry only. Naming a permanent row in `replaces` deletes it, because explicit deletion is always the project's prerogative (Sam, 2026-10-31: "expiry is the def; explicit del is always our collective prerogative"). |
| RDR-192 Superseded Note Chunks Outlive Manifest-Less Is-Live | Different layer | It concerns T3 chunks a re-put replaced. This RDR concerns whole T2 rows and memory files a writer replaced. No shared code. |

Searched the RDR corpus for: rollup, quarantine, expiry, consolidation,
supersed, obsolete, MEMORY.md, file memory, skill promotion. The five above
are every hit with real overlap. Supersession hits outside RDR-192 were
catalog and chunk tombstones (RDR-106, RDR-107), which are deletion marks,
not replacement links.

## Context

### Background

The survey counted 30 days of transcripts (5,285 sessions, all repos) and the
live T2 listing:

| Store | 30-day traffic | Reading |
| --- | --- | --- |
| T1 scratch | 263 put, 395 search | Used by a few sessions. |
| T2 notes | 9,618 get, 5,585 put | The working store; about 1,800 new rows a month. |
| T2 gate records | 1,718 get, 1,066 put | Working state: 91% of dated reads within two days. |
| T3 knowledge | 118 put, 171 get | Sparing and deliberate. |
| File memory | 225 write, 68 read | Write-mostly; the index does the work. |

### Technical Environment

- T2 rows live in `nexus.memory` behind the engine's `/v1/memory` routes;
  schema changes go through Liquibase (`memory-005-live-project-timestamp-index.xml`
  is the latest), never Python DDL (the NO-SQLITE hot rule).
- File memory is Claude Code's: plain markdown files with `name`,
  `description` and `metadata.type` frontmatter, and `MEMORY.md` as the
  index. Sessions in sibling worktrees share the primary repo's memory
  directory (no `nexus-wt` project directory has its own, checked
  2026-10-31).

## Research Findings

### Investigation

The survey's two research passes read 16 papers from Sam's DEVONthink inbox
through keyword extraction (T2 records named in Provenance). The results used
here are the ones each pass found on a named page or table.

### Key Discoveries

- **Verified (paper):** an obsolete competitor harms more than an unrelated
  one, and lexical retrieval surfaces it (Obsolete Facts, abstract and
  Table 17). Supersession has to be filtered at retrieval, not left to the
  reader.
- **Verified (paper):** unvalidated compaction can fall below the
  no-context baseline: one compression from 18,282 to 122 tokens dropped
  accuracy from 66.7% to 57.1% (Agentic Context Management, p.6). A promotion
  that shrinks its source needs a loss check.
- **Verified (paper):** promoted skills work alongside their sources, not
  instead of them, and short procedural skills beat exhaustive prose
  (Bidirectional Memory–Skill Learning, Table 4a; SkillsBench, Finding 6).
- **Verified (paper):** surveyed coding harnesses keep a small eager index and
  load bodies on demand (Harness Engineering, p.48-49).
- **Verified (local):** worktree sessions share one memory directory.
- **⚠️ Documented** (docs only): Claude Code loads the first 200 lines or
  25 KB of `MEMORY.md`, whichever comes first, and reads topic files only on
  demand. The directory is keyed on the git repository. After a write, Claude
  Code itself reminds the writer to shorten the index near those limits. Our
  index is 136 lines and 15 KB, under both. *Source: code.claude.com/docs/en/memory,
  read 2026-10-31; T2 `238-research-1`.*
- **✅ Verified** (spike): the docs say a subagent does not get the main
  conversation's auto memory (forks excepted), but at Claude Code 2.1.316, 55
  of 58 subagents dispatched after the 2026-10-31 restart had `MEMORY.md` in
  their instructions. This is version-dependent, so the budget is justified by
  main sessions alone, and the subagent saving is a bonus while it lasts.
  *Source: subagent transcripts; T2 `238-research-2`.*
- **✅ Verified** (spike): about 200 of 3,115 non-gate T2 rows (about 6%) are
  earlier members of a title chain a later row replaces, mostly round-N and
  per-sha series. A single link column is enough (A2 holds). *Source: T2
  `238-research-3`.*
- **✅ Verified** (spike): 14-day baseline over 188 main sessions: em dash in
  prose 11.2 per 100 sessions; `git stash` 4.3; full-disk `find` 2.1;
  Claude-Session trailer 1.1; backtick in a quoted nx or bd argument 1.1. Only
  the em dash rule has the volume for a 14-day comparison. *Source: T2
  `238-research-4`.*
- **Assumed:** moving a rule out of the always-loaded index into an
  on-demand tier does not lower compliance with it. No paper measures this
  for rules over a long session. This is A3, measured in Phase 2.

### Critical Assumptions

- **A1:** Claude Code loads `MEMORY.md` into each session's and subagent's
  context, whole, up to a documented line limit, and loads no other file in
  the memory directory unprompted. **Resolved 2026-10-31:** the limit is 200
  lines or 25 KB; topic files load on demand; subagents get the index at
  2.1.316 although the docs say they should not (research 1 and 2).
- **A2:** Deleting the replaced row, and keeping only its name on the new row,
  loses nothing anyone needs. Replaced rows are a small share (research 3) and
  nobody needs "what was true on date D" queries, or the old row's content,
  once the new row exists. Verification: a census of rows a reader would mark
  replaced. If as-of-date queries turn out to matter, the bitemporal
  alternative comes back.
- **A3:** Rules moved to the on-demand tier keep their compliance.
  Verification: Phase 2's before-and-after sample.

## Proposed Solution

### Approach

Delete what is replaced, bound the index, then make both run without anyone
remembering them.

**Supersession by deletion (Gap 1).** A T2 write may name the row it
replaces. In the same transaction the engine deletes the old row and writes
the new one with one line of lineage, `replaces: <old title>`. Memory files do
the same: the new file names what it replaces in frontmatter, and the old file
is deleted. Lineage is that one line and nothing else; the replaced content is
gone.

**A bounded index (Gap 2).** `MEMORY.md` keeps an **always** tier under a
token budget (proposed: 1.5K tokens, about 40 lines). A rule enters it only if
it binds on most turns of most sessions and is not already stated in a
`CLAUDE.md`, a hot rule or a skill. Every other memory moves to an
**on-demand** index file in the same directory, which `MEMORY.md` names in one
line so a session knows where to look.

**Habits that run themselves (Gap 3).** Three checks: the index budget is
checked at SessionStart and over budget prints one line; a `replaces` field is
required when a memory file reverses another, and the old file must be gone
(checked by the same hook, which compares a new file's text against what it
says it replaces); and a curation report runs at each release's post-release
step, listing unread files, unindexed files, oversized files and live rows a
later row appears to replace without naming them.

### Technical Design

1. **T2 write.** `memory_put` and `/v1/memory` gain an optional `replaces`
   (an id or an exact title in the same tenant and project). In one
   transaction, the engine deletes the target row and writes the new row with
   `replaces: <old title>` as the first line of its content (not repeated if
   the content already starts with it). A missing target, a target in another
   project, or the row being written is refused, and nothing is written or
   deleted. No schema change and no Liquibase changeset: the lineage is a line
   of content, not a column. Additive on the wire; recorded in the
   wire-contract ledger.
2. **File-memory frontmatter.** An optional `replaces: [name, ...]`. The
   index check (3) reads it.
3. **SessionStart curation check.** A conexus hook verb (stdlib only, under
   its existing SessionStart budget) that reads the repo's memory directory
   and prints at most one line: over budget, an unindexed file, or a file
   named in another file's `replaces` that still exists. Silent when clean.
   Claude Code already reminds a writer near its own 200-line and 25 KB
   limits (research 1); this check is the tighter budget we choose, plus the
   two checks Claude Code does not make.
4. **Curation report.** `nx memory curate [--files] [--agents] [--t2]`. `--agents`
   covers the reviewers' own memory directories under
   `~/.claude/agent-memory/` (nexus-qo7ie.1 gave both reviewers `memory: user`):
   index length against the 40-line cap their definitions state, entries with
   no repo named, and concurrent-write damage, since two sessions running the
   same reviewer edit one `MEMORY.md` and the last writer wins. `--files`: unread files
   over N days (read counts from transcripts until RDR-209 Phase 0b gives T2
   a true read counter), unindexed files, files over 8 KB, and
   live T2 rows that a later row names in its title or body as replaced
   (a heuristic list for a person to confirm, never an automatic delete).
   `--cold [--days N]`: every store of state with no write and no read in N
   days (default 30), wherever it lives. Each row gives the store, its last
   write, its last read, its size and its owner if known:
   - tuple subspaces, from the census's last post time and from the
     dictionary's `writers` (the tuple-space dictionary draft, nexus-mdbds); a template whose `writers` is empty
     and that has no live subspace is listed by name;
   - T2 namespaces and memory files, from read and write counts;
   - MCP tools and CLI verbs, from the transcript queries of the procedures
     RDR (nexus-qo7ie.2);
   - context injected per session and per dispatch, from the same transcript
     queries, so a large always-loaded item with no use shows as tokens.
   Report only: retiring a cold store is a person's decision, and one that
   deletes data rides its own change (as nexus-onjvw does for the ledger).
   The report runs at each release's post-release step (Phase 3), so waste
   shows while it is small.

### Existing Infrastructure Audit

| Need | Exists | Plan |
| --- | --- | --- |
| Delete a T2 row | Explicit `memory_delete` | Reuse its path inside the replacing put's transaction |
| Schema change | Liquibase memory changesets 001-005 | None needed; lineage is a content line |
| SessionStart hook verbs | conexus `nx-hook` dispatch | One more verb |
| Transcript read counts | Ad hoc scan (survey, 2026-10-31) | Make it `nx memory curate --files` |

### Decision Rationale

Replacement is deletion because the paper's failure is at retrieval, and the
cost shows whenever the stale copy is retrievable at all: the reader cannot
tell the obsolete fact from the current one once both are in context
(Obsolete Facts, Table 17). A mark plus a filter keeps the stale row one flag
and one direct read away from that failure, and makes every reader and every
future code path keep the promise to skip it. Deleting the row removes the
failure instead of guarding against it, and the `replaces:` line keeps the
only part of the lineage anyone has asked for, the name of what was replaced.
The index is bounded by a budget, not curated by taste, because a budget
checked by a hook survives a busy week and taste does not.

## Alternatives Considered

### Alternative 1: Bitemporal T2

Record valid time and transaction time on every row, as the graph-native
bitemporal store does, and serve the current view.

**Pros:** answers "what did we believe on date D"; supersession is a special
case.

**Cons:** two time columns on every row, and query changes on every read
path, to answer a question nobody has asked of T2.

**Reason for rejection:** A2. Revisit if as-of-date queries appear.

### Alternative 2: Retrieve memories per prompt

A UserPromptSubmit hook searches file memory for the prompt and injects the
top few, replacing the always tier.

**Pros:** loads only what is relevant.

**Cons:** adds latency and a search to every prompt; rules that must bind on
every turn (no `git add -A`, no AI attribution) can fail to match; the
routing papers show implicit retrieval failing at scale.

**Reason for rejection:** not rejected; deferred until Phase 2 measures A3.
If the on-demand tier works, this is a refinement of it.

### Briefly Rejected

- **Mark the old row and filter reads (the first draft):** leaves the stale
  row stored and retrievable by flag or by direct read; see Decision
  Rationale.

## Trade-offs

### Consequences

- An additive wire field (`replaces`) and no engine changeset.
- A replaced row is gone; only its name survives, on the new row.
- A session sees fewer rules up front and has to look in the on-demand index
  for the rest.

### Risks and Mitigations

- **A rule drops out of the always tier and stops being followed.**
  Mitigation: Phase 2 measures compliance before and after, and the rule goes
  back if its violations rise.
- **A write names the wrong row and deletes it.** Mitigation: the target must
  be an exact title or id in the same tenant and project, the new row records
  the name it replaced, and the engine has point-in-time recovery.
- **The heuristic report marks a live row as replaced.** Mitigation: the
  report only lists; the writer names the replacement and the engine deletes
  only a row a write names.
- **The SessionStart line becomes noise.** Mitigation: at most one line,
  silent when clean, and the budget is set from the Phase 2 measurement.

### Failure Modes

- Replace target missing, in another project, or the row being written:
  refused, nothing written or deleted, error names the target.
- Hook cannot read the memory directory: silent, logged to `hook.log`;
  never blocks a session.

## Implementation Plan

### Prerequisites

- [ ] A1 verified (Claude Code's loading of `MEMORY.md`).
- [ ] RDR-209 amendment for gate records filed (this RDR does not depend on
      it landing).

### Minimum Viable Validation

On a test substrate: write row A, then row B with `replaces=A`. A is gone:
`memory_get(A)` finds nothing and search does not return it. B exists and its
content starts with the line `replaces: A`. A `replaces` naming a row in
another project is refused, and B is not written.

### Phase 1: T2 replacement by deletion

#### Step 1: Engine write path

The delete and the write in one put transaction, the lineage line, the
refusal cases (missing target, other project, self), confirmation that the
delete takes the path of an explicit `memory_delete`, Java tests against PG.
No changeset.

#### Step 2: Client and MCP

`memory_put(replaces=...)`, wire-ledger entry, Python tests against the
engine.

### Phase 2: The bounded index

#### Step 1: Measure

Before any move: count violations per 100 main sessions for rules whose
violations show in transcripts. The baseline is already taken (research 4).
The em dash rule has the volume for a 14-day window. The rare rules (`git
stash`, full-disk `find`, the Claude-Session trailer, backticks in quoted
arguments) are pooled into one rate over 60 days, because at about one
session in a hundred a 14-day window cannot tell a change from noise.

#### Step 2: Split

Write the always tier under the budget and the on-demand index file; move
the rest. Record what moved and why.

#### Step 3: Re-measure

Count the same rules over the same windows after the split. A rule whose
rate rises returns to the always tier.

### Phase 3: Habits

The SessionStart check, `nx memory curate`, and a curation step in the
release skill's post-release section.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Replaced T2 rows | none; the row is gone, its name is on the new row | `nx memory get` of the new row | the replacing write | `nx memory curate --t2` | engine PITR |
| Memory files | `ls` the memory directory | the file | by the writer, with `replaces` | SessionStart check, `nx memory curate --files` | Time Machine |

### New Dependencies

None.

## Test Plan

- Engine: replace in one transaction (old row gone, new row has the lineage
  line); refusal (missing target, other project, self) leaves both rows
  unchanged; a chain A, B, C leaves only C with `replaces: B`.
- Client: after a replacing put, search and `memory_get` no longer find the
  old row.
- Hook: over budget prints one line; clean prints nothing; an unreadable
  directory is silent and exits 0; each case fails with its check deleted.
- Curate: each report section on a fixture directory with one planted case
  and one clean case.

## Validation

### Testing Strategy

Engine and client tests on real PG; hook tests on fixture directories; Phase 2
compliance counts from transcripts, stored in T2 with the scan script.

### Performance Expectations

A replacing put adds one indexed lookup and one delete to the put. The
SessionStart check reads one directory of about 150 small files, well under
its hook budget.

## Finalization Gate

### Contradiction Check

### Assumption Verification

#### API Verification

### Scope Verification

### Cross-Cutting Concerns

### Proportionality

## References

- Survey artifact: `https://claude.ai/artifact/9gEbyn1wFYhWh9sTJvoT8a`
- T2 `nexus/memory-survey-papers-agent-memory-2026-10-31`,
  `nexus/memory-survey-papers-context-load-2026-10-31`
- When Retrieval Is Not Enough: Obsolete Facts in Agent Memory (DEVONthink
  BB7DF785)
- Lifelong Context Adaptation through Bidirectional Memory–Skill Learning
  (4BF492EB)
- Agentic Context Management (DC16C60E)
- How Many Instructions Can LLMs Follow at Once (AAC1630D)
- SkillsBench (602481F7)
- Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents
  (D0B8FF87)

## Revision History

| Date | Change |
| --- | --- |
| 2026-10-31 | Filed as draft from the memory survey at Sam's direction (nexus-qo7ie). Gate-record rollup routed to RDR-209 as an amendment. |
| 2026-10-31 | Gap 5 added at Sam's direction: procedures that run instead of being recalled. Promotion gains three targets (skill, command, hook), and Phase 5 builds `nx transcripts`, prose on stdin, `nx gate record` and `nx preflight`. Title widened to match. |
| 2026-10-31 | Front-door rule added to Gap 5 at Sam's direction: one implementation, MCP and CLI as thin wrappers, chosen by caller and reach; `nx gate record` becomes the MCP tool `gate_record` first; Phase 5 Step 4 is a front-door census. |
| 2026-10-31 | Cold-state section added at Sam's direction (heat and waste): Gap 4 names the day's five cases, and `nx memory curate --cold` reports every store with no write and no read in N days, report only. |
| 2026-10-31 | Sam's review: supersession becomes deletion; Gaps 3 and 5 (procedures) move to nexus-qo7ie.2 as Lua scripts on the runtime of nexus-mdbds.1; gaps renumbered; title changed. |
