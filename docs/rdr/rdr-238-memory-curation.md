---
title: "Memory Curation: Supersession by Deletion, a Bounded Always-Loaded Index, and Habits That Run Themselves"
id: RDR-238
type: Feature
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-10-31
accepted_date: 2026-11-01
related_issues: [nexus-qo7ie]
related_rdrs: [RDR-057, RDR-207, RDR-209, RDR-074, RDR-192, RDR-205, RDR-239, RDR-240]
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
(5,435 rows in project `nexus`; RDR-209's census on 2026-09-14 found 39
rows with a TTL among 6,064 across all projects, T2 `209-research-1`).
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
LLMs Follow at Once") finds its threshold-type models holding up to about 150
simultaneous instructions, on a keyword task the survey says does not map
directly to ours, and between the two `CLAUDE.md` files and the index we carry
well over 100 rules. The index is under both of Claude Code's limits today,
so the case is its growth and that knee, not a harm measured here. The fix
delivers a token budget for the index, a stated admission rule, and an
on-demand tier for everything else.

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
15M tokens over about 32 days of transcripts, research 5); thirteen plugins unused for 30 days (about
4K tokens per session start); 19 detached `nx upgrade --auto` walkers
saturating the cloud edge (nexus-75w5d); about 2,500 permanent gate records;
and the RDR-205 session ledger, whose writer and readers 7.70.0 deleted while
its engine template and 40,785 rows stayed (nexus-onjvw). Sam: "we want to be
sure we're paying attention to things and ensuring we're not generating a lot
of heat and waste." The fix also delivers a last-use signal, read on a
schedule, for the four kinds of store the curation report's cold-state
section lists (Technical Design 4): tuple subspaces, T2 namespaces and memory
files, MCP tools and CLI verbs, and injected context.

Procedures stored as memory, and procedures recalled or improvised instead of
run, moved to their own RDR on 2026-10-31 (nexus-qo7ie.2): they become
sandboxed Lua scripts on a Lua runtime (nexus-mdbds.1), not new Python verbs
or MCP tools. That RDR is now RDR-240, and it is a prerequisite of this
one's Phase 2 and of `nx memory curate` in Phase 3: its `transcripts`
procedure is the one transcript scanner both use (Technical Design 5).
RDR-240 in turn waits on the Lua runtime RDR (nexus-mdbds.1), not yet
filed. The removed text stays in the history of branch
`feature/nexus-qo7ie-memory-curation-rdr`, in the draft as filed before this
revision.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-057 Progressive Formalization Across Memory Tiers | Origin of the tier model | Its "graduation" idea is the T2 to T3 case. This RDR is the curation case beside it: replacement and admission to context. |
| RDR-207 Summarization at the T2 Expiry Boundary | Beside, no shared path | It owns what expiry may destroy (quarantine, rollup, reap only rolled-up rows). Replacement is not expiry: a writer names a row and the engine deletes it in the same transaction. A quarantined target is refused, as `mergeMemories` refuses a quarantined id (Technical Design 1). |
| RDR-209 Heat-Based Graduation Between Memory Tiers | Sibling, draft | It owns heat and graduation, and takes the gate-record rollup as an amendment (same date). Its Phase 0b, a read counter that means "a caller read this row", is the signal this RDR's index audit would use for T2; until it lands, the audit counts T2 reads with RDR-240's `memory` transcript query: per title from `memory_get` calls, per project from `memory_search` calls (Technical Design 5). |
| RDR-240 Procedures as Sandboxed Lua Scripts | Prerequisite of Phase 2 and of `nx memory curate` | Split from this RDR. Its `transcripts` procedure owns transcript scanning; this RDR calls four of its queries and builds no scanner (Technical Design 5). Phase 1 does not depend on it. |
| RDR-239 The Tuple-Space Dictionary | Accepted; prerequisite of one `--cold` row | Its dictionary view (`writers`, last post time per subspace) is the source of the report's tuple-subspace row; that row waits for it (Technical Design 4). |
| RDR-205 Linda Tuple Space over Postgres | Closed; its ledger template is being retired | Its `ledger/<session_id>` template and 40,785 rows outlived their writer (nexus-onjvw): one of the cases that motivate `--cold`. |
| RDR-074 Permanence Mode | Deferred, compatible | Permanence guards a row against expiry only. Naming a permanent row in `replaces` deletes it, because explicit deletion is always the project's prerogative (Sam, 2026-10-31: "expiry is the def; explicit del is always our collective prerogative"). |
| RDR-192 Superseded Note Chunks Outlive Manifest-Less Is-Live | Different layer | It concerns T3 chunks a re-put replaced. This RDR concerns whole T2 rows and memory files a writer replaced. No shared code. |

Searched the RDR corpus for: rollup, quarantine, expiry, consolidation,
supersed, obsolete, MEMORY.md, file memory, skill promotion. RDR-057, -207,
-209, -074 and -192 are every hit with real overlap; RDR-205, -239 and -240
are rows added since for dependencies, not search hits. Supersession hits outside RDR-192 were
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
- **Documented (paper):** an obsolete version of a fact in context took
  Qwen2.5 from 100% to 44.6% (Obsolete Facts). *Keyword extraction, page not
  spot-checked.*
- **Documented (paper):** IFScale's threshold-type models hold up to about
  150 simultaneous instructions on a keyword-insertion task, a number the
  survey says does not map directly to nexus (How Many Instructions Can LLMs
  Follow at Once). *Keyword extraction, page not spot-checked.*
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
  per-sha series (the census names no kinds; commit reviews tagged
  `commit-review` are one such series in project `nexus`). This measures how often chains occur, not that a replaced
  row's content is unneeded, so it does not verify A2. Its "single link"
  wording describes the abandoned link-column design. *Source: T2
  `238-research-3`.*
- **✅ Verified** (spike): a 14-day window-sizing scan over 188 main sessions
  (not Phase 2's baseline, which is taken at the split): em dash in
  prose 11.2 per 100 sessions; `git stash` 4.3; full-disk `find` 2.1;
  Claude-Session trailer 1.1; backtick in a quoted nx or bd argument 1.1. Only
  the em dash rule has the volume for a 14-day comparison. *Source: T2
  `238-research-4`.*
- **Assumed:** moving a rule out of the always-loaded index into an
  on-demand tier does not lower compliance with it. No paper measures this
  for rules over a long session. This is A3, measured in Phase 2.
- **2026-11-01, Implementation (Phase 1):** the K4 title-pattern backstop was
  chosen by Sam as option (a). `replaces` also refuses a target whose title
  carries one of thirteen tokens: `review`, `code-review`, `commit-review`,
  `critique`, `critic`, `gate`, `fix-check`, `fixcheck`, `confirm`,
  `confirmation`, `rereview`, `codereview`, `audit`, or the series suffix
  `-research-N` at the end of the title or before `.`, `:` or a space. A token is a whole word bounded by the
  start or end of the title or by `-`, `_`, `.`, `:` or a space; `reviewer-notes`
  and `preview` do not match. Matching is case-insensitive and ASCII-only, a
  search not a full match, and the one list lives in
  `src/nexus/tables/audit-tags.toml`, from which the engine's `AuditTags.java`
  is generated. The K5 limit: a replace is never retried, so a response lost to a
  replace that landed loses the echo; the new row exists and the old one is
  quarantined (see the 2026-11-02 entry below), so its id comes from
  `nx memory list --quarantined`.
  Review round 1 added two refusals: a replace whose new title already names
  another row (live or quarantined) is 409, because overwriting it would remove
  a row without echoing it; a present but blank `replaces` is 400. Two replaces
  that swap titles can deadlock on the row locks; PostgreSQL aborts one, and
  nothing is written by the aborted one.
- **2026-11-02, A2 measured, then Phase 1 changed to quarantine (nexus-qo7ie.8,
  nexus-ov3zi):** *A2 outcome.* Sam could not judge the rows, so Step 3 was run
  as a measurement and an agent read (T2 `nexus/rdr-238-a2-measure-2026-11-02`).
  The population was 460 earlier chain members, 81 of them replaceable (no audit
  tag, no audit title). An agent read the cards of the first 20 replacement-type
  rows: 6 yes (the earlier row held content the newest lacks and wanted), 11
  maybe, 3 no. By Step 3's stop rule, one "needed" stops the feature, so the gate
  tripped. Transcript fetches by name: 408 ever for earlier members, 23 after the
  newest row existed, on 19 of the 460 rows; of the 81 replaceable rows, 2 were
  real replacements fetched after the newest existed (`224-continuation` fetched
  by its bare title from a bead pointer, and `224-win-guest-walk-followups`).
  *Caveats:* the first 20 are not a random sample; the yes/maybe/no marks are an
  agent's first pass, not Sam's; the transcripts are the laptop's only (not
  chas, hellmini or other harnesses); and use through search is unmeasured, which
  is the channel a hidden row closes. Reading: content in earlier rows is often
  unique and rarely fetched by name; A2 is neither confirmed nor refuted, and the
  loss it risks is irreversible under delete. *Decision, Sam, 2026-11-02:* a
  replace QUARANTINES its target instead of deleting it (Alternative 1 was not
  taken). This entry rules for Phase 1; the sections below that say "deletes"
  are superseded as listed at its end.
  *What was built.* The engine, in the replace transaction, stamps
  `quarantined_at` on the target and sets its `ttl_days` and `rolled_up_at` to
  NULL, so every read hides the row. `nx memory restore <id>` brings it back with
  its content, tags and title, permanent, as for an expired row. The response
  gains `replaced_id` beside `replaced_title` and `replaced_content`, and the
  `memory_put` result names the quarantined row, its id and the restore command.
  The refusals, the audit backstop, the lineage line, the FOR UPDATE locks, no
  Liquibase changeset and no CLI flag stand; a quarantine that affects other than
  one row is a 409. *Rollup rule.* `reap` deletes only rows that are quarantined
  and rolled up, and the attended `nx memory rollup` would have marked a replaced
  row with the rest of the project's quarantined rows. So a replaced row is
  distinguishable: it is quarantined with `ttl_days` NULL, a shape only a replace
  produces, because expiry stamps only rows that have a ttl and every other
  writer of `quarantined_at` clears it. `plan_groups` skips such rows, and
  `POST /v1/memory/summaries` refuses any id of such a row with 409 and code
  `replaced_source_id`, writing nothing. A replaced row is therefore never
  summarized, marked or reaped; it leaves only by `restore` or an explicit
  `delete` by id. The check and the mark take no row lock, so `reap` itself
  also skips a quarantined row with `ttl_days` NULL, and the mark skips one: a
  rollup that races a replace cannot delete the replaced row. *Taxonomy:* the facade does not purge the replaced title's
  topic assignments, because expiry quarantine does not either (`expire` runs no
  taxonomy cascade; only `delete` purges); the taxonomy tables hold no reference
  to the memory table, so no taxonomy read filters quarantined rows, as for an
  expired row today; and, unlike an expired row, a replaced row is never reaped,
  so its topic membership and `doc_count` stay until it is restored or deleted
  (nexus-xs3jy). *Known limit, not changed:* the quarantined row keeps its
  title, so a later plain put at the old title overwrites it in place (RDR-207
  behaviour: the write clears the stamps and reuses the row's id), and the
  `replaced_id` the replacing writer was given then names that new live row, not
  the content it replaced. A replace whose new title is a replaced row's title
  (A to B, then B to A) is 409; restoring does not help, and the way out is to
  delete the quarantined row by id. A second replace of the same target is 409
  (quarantined). A reader who fetches the old title gets not-found plus a line
  naming the quarantined row, its id and `nx memory restore`.
  *Supersedes for Phase 1,* where it disagrees with this entry: the title and the
  Approach ("supersession by deletion"), A2, Technical Design 1 and Steps 1 and 2
  ("deletes", "the deleted row's content"), Step 3 (its stop rule is the measure
  above), the Problem Statement ("the replaced entry is deleted"), the Decision
  Rationale, the Trade-offs, Risks and Failure Modes, the Minimum Viable
  Validation ("A is gone"), the Phase 1 heading ("replacement by deletion") and
  body, the Test Plan engine bullet ("old row gone") and client bullet, the
  Finalization Gate answers, the Cross-Cutting "Irreversibility" bullet (a
  replaced row is restorable), Performance ("one delete"), the Day 2
  Operations table row "List: none; the row is gone" (List is
  `nx memory list --quarantined`, Backup is `nx memory restore <id>`), the
  Contradiction Check, and the RDR-207 and RDR-074 rows of the prior-RDR table
  (a replace now quarantines, and naming a permanent row quarantines it). This entry rules for Phase 1.
- **2026-11-03, a T2 memory row has no topic membership (nexus-xs3jy closed).**
  The *Taxonomy* clause of the 2026-11-02 entry is wrong in its premise: a
  replaced row has no topic membership or `doc_count` to keep.
  `nexus.topic_assignments.doc_id` is a chunk chash with a foreign key to
  `nexus.chunks` (`taxonomy-012-doc-id-chunk-fk.xml`, RDR-194 D1), every
  assignment writer clusters T3 collections only, and T2 memory is never a
  source, so no assignment can name a memory row. Quarantine, replace, restore
  and reap therefore change no taxonomy count. Clustering T2 memory would need
  its own design (an assignment that can name a memory row); none is planned.
- **2026-11-03, Sam's answers to S-7 and K6 (nexus-qo7ie.5 closed).** *S-7:* a
  memory file that supersedes another says `replaces: <slug>` in its
  frontmatter, and the writer learns this from an always-loaded line in Sam's
  global CLAUDE.md. `nx memory curate --files` gets no heuristic for a file
  replaced without saying so; it reports a declared target that is still
  present, and lists likely near-duplicate files for Sam to review, never acting
  on them. *K6:* always-loaded items (`MEMORY.md` as a whole, CLAUDE.md, tool
  definitions) are excluded from `--cold`, and the "never used" claim is dropped
  for them; a `MEMORY.md` index line counts as used when its linked file is
  read. Where Phase 3 Steps 1-3 or Technical Design 4 say otherwise, this entry
  rules.
- **2026-11-03, Sam's S-8 minimum (nexus-qo7ie.4 closed).** Phase 2's
  compliance test counts only measured rules whose index line moved out of the
  always tier. At least 3 of the 5 must move; with fewer, Phase 2 Step 3
  records A3 as still assumed rather than tested.
- **2026-11-03, S-10 mapping and the near-duplicate measure as built
  (nexus-qo7ie.17).** The memory directory comes from one stdlib-only function,
  `nexus._hook_runtime.memory_dir.memory_dir` (stdlib at call time too: no
  process is spawned), shared by `nx memory curate --files` and the SessionStart
  check. A custom path wins: `autoMemoryDirectory`
  from `.claude/settings.local.json` or `.claude/settings.json` (working
  directory, then the primary checkout) or `<config dir>/settings.json`, when it
  is an absolute or `~/` path to an existing directory. Otherwise it is
  `<config dir>/projects/<key>/memory`, with `<config dir>` = `$CLAUDE_CONFIG_DIR`
  or `~/.claude` and `<key>` the primary checkout's absolute path with every
  non-alphanumeric character replaced by `-`; the primary is the parent of the
  git common directory, found by reading `.git` (a directory, or a `gitdir:`
  file and its `commondir`), and by `git rev-parse --git-common-dir` only when
  `GIT_DIR` or `GIT_COMMON_DIR` is set, a `GIT_CEILING_DIRECTORIES` entry lies above the working
  directory, or the layout is unrecognised; that call runs in the caller's environment. A missing
  directory, a non-repository or a failed fallback git call gives nothing. Near-duplicates (S-7) are file pairs whose
  frontmatter name and description tokens overlap at Jaccard 0.5 or more; files
  with fewer than 4 tokens are skipped. The `--t2` census reads "phase closed" as
  the RDR's frontmatter `status: closed`, or (nexus-qo7ie.28, 2026-11-06) as the
  phase's own heading carrying the close marker, with the record's phase taken from
  its title; RDR-209 Phase 3 Step 4 states the rule. Technical Design 3's mapping
  sentence is read with this entry.
- **2026-11-05, Sam's answer on hub-linked files (nexus-qo7ie.29).** A memory
  file counts as indexed when either index file names it, or when a memory file
  that an index names links it with a markdown link: one hub level, no deeper,
  and a bare file name in a hub's prose does not count. A hub link counts only
  when it points into the memory directory (`x.md` or `./x.md`); `archive/x.md`
  and `../x.md` name other files. On Sam's memory directory the 27 files linked
  from `project_key_technical_facts.md` had read as unindexed; with this rule
  the count is 0. `nx memory curate --files` applies it through
  `nexus._hook_runtime.memory_scan.scan`, and the SessionStart check
  (nexus-qo7ie.16, not yet built) is to call the same function. Technical
  Design 3's "Indexed" sentence was amended to match.
- **2026-11-06, the report title of a partial run (nexus-qo7ie.30).** As
  built, a run that selects fewer than all three of `--files`, `--t2` and
  `--agents` (a `--cold`-only run included) stores its report as `<repo>/curation-report-<date>-<sections>`, so it never
  replaces that day's full report. `docs/cli-reference.md`, the command's help
  and `report_title` said so; TD4 item 4 is amended to match.

### Critical Assumptions

- **A1:** Claude Code loads `MEMORY.md` into each session's and subagent's
  context, whole, up to a documented line limit, and loads no other file in
  the memory directory unprompted. **Resolved 2026-10-31:** the limit is 200
  lines or 25 KB; topic files load on demand; subagents get the index at
  2.1.316 although the docs say they should not (research 1 and 2).
- **A2 (Assumed):** Deleting the replaced row, and keeping only its name on
  the new row, loses nothing anyone needs, for the row kinds `replaces` may
  target. Audit records are excluded: `replaces` refuses a target carrying
  any of the tags `kind:gate`, `gate`, `critique`, `research`, `fix-check` or `commit-review`, matched as whole tags (Technical Design 1), so
  a round-2 critique cannot delete round 1. Replaced rows are a small share
  (research 3). Verification: Phase 1 Step 3, before `replaces` is announced
  to agents: research 3's title-chain method is rerun, Sam reads 20 of the
  earlier chain members it lists that carry none of those tags, and marks
  each "content needed" or not; one "needed" brings the bitemporal
  alternative back before anything is deleted.
- **A3:** Rules moved to the on-demand tier keep their compliance.
  Verification: Phase 2's before-and-after sample.

## Proposed Solution

### Approach

Delete what is replaced, bound the index, then make both run without anyone
remembering them.

**Supersession by deletion (Gap 1).** A T2 write may name the row it
replaces. In the same transaction the engine deletes the old row and writes
the new one, whose content ends with one line of lineage, `replaces: <old
title>`. Memory files do
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
checked at SessionStart and over budget prints one line; a memory file that
replaces another names it in `replaces`, and the same hook reports any file
that another file names in `replaces` and that still exists, and any index
entry naming a missing file; and a curation report runs at a release step
after the release skill's Step 12, listing unread files, unindexed files,
oversized files and live rows a later row appears to replace without naming
them.

### Technical Design

1. **T2 write.** `memory_put` and `/v1/memory` gain an optional `replaces`:
   an exact title, looked up in the put's own tenant and project. It takes no
   id, so a numeric title such as `238` is never read as an id, and a target
   in another project cannot be named. `HttpMemoryStore`, the client the
   MCP tool and the CLI share, appends `replaces: <old title>` as the
   content's last line before sending (not repeated if the
   content already ends with it), so the stored content equals what was sent
   and `_verify_t2_write_landed` (`src/nexus/mcp/core.py`) still passes. In
   one transaction the engine deletes the target row and writes the new row.
   Refused, with nothing written or deleted: a missing target, the row being
   written, a quarantined target (as `mergeMemories` refuses a quarantined
   id), and a target carrying any tag in the set `AUDIT_TAGS` (`kind:gate`, `gate`, `critique`, `research`, `fix-check` or `commit-review`),
   matched as whole tags, as RDR-209 Phase 3 Step 0 matches `kind:gate`. One
   named constant holds the set, and the `--t2` list reads the same one.
   The put response echoes the replaced title and the deleted row's content,
   and `memory_put`'s result carries that content to the writer;
   the client fails the call when it sent `replaces` and gets no echo, so a
   new client against an engine without the feature (whose `MemoryHandler`
   ignores unknown fields) errors instead of reporting "Stored" with the old
   row alive. Such an old engine has already written the new row by then;
   the error says the old row is still alive. The echo is the row-level
   recovery: re-put its content. No
   schema change and no Liquibase changeset: the lineage is a line of
   content, not a column. Additive on the wire; the wire-contract ledger
   entry states both directions (old client: never sends it; new client, old
   engine: fails on the missing echo).
2. **File-memory frontmatter.** An optional `replaces: [name, ...]`. The
   index check (3) reads it.
3. **SessionStart curation check.** A conexus hook verb (stdlib only, under
   its existing SessionStart budget) that reads the repo's memory directory
   and prints at most one line: over budget, an unindexed file, a file named
   in another file's `replaces` that still exists, or an index entry naming a
   missing file. Silent when clean. It is opt-in per repo: it runs only where
   the on-demand index file `MEMORY-ondemand.md` exists, so a repo with
   another layout never sees it. It finds the memory directory from the git
   common directory, so a linked worktree checks its primary's directory.
   "Indexed" means named in `MEMORY.md` or `MEMORY-ondemand.md`, or linked
   by a markdown link from a memory file one of them names (one hub level;
   Key Discoveries 2026-11-05); the two index files are not themselves
   checked. The check calls `nexus._hook_runtime.memory_scan.scan`, the
   function `nx memory curate --files` uses, so the two cannot disagree. Size is `MEMORY.md`'s bytes
   divided by 4, against `memory.index_budget_tokens` in `.nexus.yml`
   (default 1500), read by a stdlib line scan for that one key under a
   top-level `memory:` block, since the hook cannot import a YAML parser; a
   missing, non-integer or non-positive value means the default. Claude Code already reminds a writer near its own
   200-line and 25 KB limits (research 1); this check is the tighter budget
   we choose, plus the checks Claude Code does not make.
4. **Curation report.** `nx memory curate [--files] [--t2] [--agents]
   [--cold] [--days N]`, printed to stdout and stored in T2 as
   `<repo>/curation-report-<date>` with a 90-day TTL; a run that selects fewer
   than all three of `--files`, `--t2` and `--agents` stores
   `<repo>/curation-report-<date>-<sections>` (for example `-files`), so a
   partial run never replaces the day's full report. `--days N` (default 30)
   sets the window for `--files` and `--cold`; for counts read from
   transcripts, which are kept about 30 days (inferred from file dates,
   research 9), N is capped at that span and the report prints the span it
   covered. `--agents`
   covers the reviewers' own memory directories under
   `~/.claude/agent-memory/` (nexus-qo7ie.1 gave both reviewers `memory: user`):
   index length against the 40-line cap their definitions state, entries with
   no repo named, and concurrent-write damage, since two sessions running the
   same reviewer edit one `MEMORY.md` and the last writer wins. `--files`: unread files
   over N days (read counts from RDR-240's `memory` query), unindexed files,
   and files over 8 KB other than the two index files.
   `--t2`: live T2 rows that a later row names in its title or body as
   replaced, a heuristic list for a person to confirm and never an automatic
   delete; it skips rows carrying an `AUDIT_TAGS` tag, which `replaces` may
   not target. `--t2` also carries RDR-209 Phase 3 Step 4's census flag:
   gate records older than 30 days whose phase is closed and that have no
   TTL. RDR-209 owns that flag's rule; this RDR builds the command it runs in.
   `--cold`: each store of the four kinds below with no write and no use in
   N days. Each row gives the store, its last write where the store has writes, its
   last use, its size and its owner if known:
   - tuple subspaces, from RDR-239's dictionary view (each subspace's last
     post time and its template's `writers`); a template whose `writers` is
     empty and that has no live subspace is listed by name. This row waits
     for that view;
   - T2 namespaces and memory files, from read and write counts;
   - MCP tools and CLI verbs, from RDR-240's `tools` query; `nx memory
     curate` builds the full list itself (the MCP servers' tool registries
     and the CLI's command tree) and diffs it in Python, so a tool or verb
     never called is listed;
   - context injected per main session and per dispatch, from RDR-240's
     `loads` query, joined with the `memory` and `tools` queries for use: an
     injected item is used when a later read or call in the same session
     names it (rows carry a session id for this join), so a large always-loaded item never used shows as tokens.
   Report only: retiring a cold store is a person's decision, and one that
   deletes data rides its own change (as nexus-onjvw does for the ledger).
   The report runs at a release step after the release skill's Step 12, once
   the local tool carries the release (Phase 3), so waste shows while it is small. It never blocks a release.
5. **Transcript queries (the interface to RDR-240).** This RDR builds no
   transcript scanner. RDR-240's `transcripts` procedure is the one owner,
   and this RDR calls it through `run_procedure("transcripts", {query, since,
   until, project})` from Python, so `nx memory curate` adds no Lua. It uses four
   queries:
   - `memory`: memory-file reads, and T2 reads per title from `memory_get`
     calls and per project from `memory_search` calls (a search call carries
     no title), for `--files`, `--cold` and T2 read counts;
   - `tools`: calls per MCP tool, and per `nx` CLI verb called through Bash,
     for `--cold`; it reads the Bash command text inside the procedure;
   - `loads`: instruction files loaded per main session and per dispatch,
     with their sizes, for `--cold`;
   - `violations`: the rule detectors, counted per 100 main sessions, for
     Phase 2 Steps 1 and 3.
   Requirements this RDR places on RDR-240, which RDR-240's Technical
   Design 4 and 5 now carry (RDR-240's gate settles their shape): the
   argument object `{query, since, until, project}`; session records that
   carry each tool call's input (a `memory_get` title, a Bash command), which
   is not a message body; the T2 reads in `memory`, per title from
   `memory_get` and per project from `memory_search`; main sessions and
   sizes in `loads`; the `nx` CLI verb count in `tools`, which reads Bash
   command text inside the procedure; and `violations` reading message
   bodies inside the procedure, since its detectors scan assistant text and
   Bash inputs; and the row shape: each `memory`, `tools` and `loads` row
   carries a session id, a name, a count, a last-use time and a size, and
   each `violations` row carries a rule, a count and the number of main
   sessions in the window.

### Existing Infrastructure Audit

| Need | Exists | Plan |
| --- | --- | --- |
| Delete a T2 row | Explicit `memory_delete`, which calls `MemoryRepository.delete(tenant, project, title)` (`MemoryHandler.java:449`) with no quarantine check | A new repository method: look up by (tenant, project, title), refuse quarantined and audit-tagged targets, delete and write in one transaction, as `mergeMemories` does |
| Schema change | Liquibase memory changesets 001-005 | None needed; lineage is a content line |
| SessionStart hook verbs | conexus `nx-hook` dispatch | One more verb |
| Transcript read counts | Ad hoc scan (survey, 2026-10-31) | RDR-240's `transcripts` procedure; `nx memory curate` calls it |

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
  Mitigation: Phase 2 measures compliance before and after for the five rules
  whose violations show in transcripts, and a rule goes back if its rate
  rises past the stated bound. The other moved rules (most of about 150) are
  not measured; their guard is that the on-demand index names them and that
  Phase 2 counts reads of moved files.
- **A write names the wrong row and deletes it.** Mitigation: the target must
  be an exact title in the writer's own tenant and project, audit records
  cannot be targets, the new row records the name it replaced, and the put
  response returns the deleted row's content so the writer can re-put it.
  Point-in-time recovery exists only on the managed cloud engine and is a
  cluster fork, too heavy for one row, so it is not the guard.
- **The heuristic report marks a live row as replaced.** Mitigation: the
  report only lists; the writer names the replacement and the engine deletes
  only a row a write names.
- **The SessionStart line becomes noise.** Mitigation: at most one line,
  silent when clean, opt-in per repo, and shipped only after the Phase 2
  split, with the proposed 1.5K-token budget; Phase 2's result may retune it.

### Failure Modes

- Replace target missing, the row being written, quarantined, or tagged as
  an audit record: refused, nothing written or deleted, error names the
  target and the reason.
- New client, old engine: no echo in the put response; the client fails the
  call and says the engine lacks `replaces`.
- `nx memory curate` without RDR-240's runtime, or with a query over its
  limit, or with no transcripts: the transcript-backed rows report "not
  available" with the reason; the checks that need no transcripts
  (unindexed and oversized files, `--t2`, `--agents`) still run, and the
  command exits 0, so the release step never blocks.
- Hook cannot read the memory directory: silent, logged to `hook.log`;
  never blocks a session.

## Implementation Plan

### Prerequisites

- [x] A1 verified (Claude Code's loading of `MEMORY.md`; research 1 and 2).
- [x] RDR-209 amendment for gate records filed (RDR-209 Revision History,
      2026-10-31; this RDR does not depend on it landing).
- [ ] RDR-239's dictionary view built. Only the `--cold` tuple-subspace row
      waits for it.
- [ ] RDR-240 Phase 1 Step 1 landed (the runner and the `transcripts`
      procedure). Phase 2 Steps 1 and 3, the transcript-backed rows of
      `nx memory curate` and the release step that runs it wait for it;
      Phase 1 does not. The SessionStart check waits for Phase 2 Step 2, so
      through Phase 2 Step 1 it waits for RDR-240 too.

### Minimum Viable Validation

On a test substrate: write row A, then row B with `replaces=A`. A is gone:
`memory_get(A)` finds nothing and search does not return it. B exists, its
content ends with the line `replaces: A`, and the put response echoes A's
title and content. A `replaces` naming a row in
another project is refused, and B is not written.

### Phase 1: T2 replacement by deletion

#### Step 1: Engine write path

A new `MemoryRepository` method: the lookup by (tenant, project, title), the
delete and the write in one put transaction, the refusal cases (missing
target, self, quarantined, audit-tagged), and the echo. The method is
classified in `MemoryRepositoryQuarantineReadPathTest` alongside the other
write paths. Java tests against PG. No changeset.

#### Step 2: Client and MCP

`HttpMemoryStore` appends the lineage line; `memory_put(replaces=...)` fails
on a missing echo and returns the deleted content in its result; the
wire-ledger entry states both directions; `tests/test_mcp_wire_snapshot.py`
is regenerated for the new parameter; Python tests against the engine.

#### Step 3: The A2 read

Before `replaces` is named in any skill or agent brief: rerun research 3's
title-chain method (group titles after stripping date, sha, round and
version suffixes), take 20 earlier chain members that carry no `AUDIT_TAGS`
tag, and Sam marks each "content needed" or not. One "needed" stops the
feature until the bitemporal alternative is reconsidered.

### Phase 2: The bounded index

#### Step 1: Measure

Before any move: count violations and main sessions for rules whose
violations show in transcripts, with RDR-240's `violations` query, over
windows that end on the day of the split. Research 4's ad hoc scan sized those windows
and is not the baseline, so before and after come from one scanner.
The measured domain is five rules: the em dash rule, which has the volume
for a 14-day window, and four rare rules (`git stash`, full-disk `find`, the
Claude-Session trailer, backticks in quoted arguments), pooled into one rate
over 30 days, the most transcripts keep (about 30 days, inferred; research
9). Each count is stored in T2 when it is taken.

#### Step 2: Split

Write the always tier under the budget and the on-demand index file; move
the rest. Record what moved and why.

#### Step 3: Re-measure

Count the same rules over windows of the same length that start on the day
after the split, with the same `violations` query, each taken when its window
closes. The test is on counts: the expected post-split count is the
pre-split rate per main session times the post-split main sessions, and a
rule returns to the always tier when its post-split count exceeds the upper
bound of the 95% Poisson interval on that expectation. The em dash rule is
tested alone. The four rare rules are tested as one group, and when the
group exceeds its bound all four return. Phase 2 ends 30 days after the split. The split is kept
when no rule returns, the always tier is under its budget, and the `memory`
query shows moved files being read on demand; if no moved file is read in
those 30 days, the on-demand tier is not working and the split is revisited.

### Phase 3: Habits

#### Step 1: SessionStart check

The hook verb of Technical Design 3, after Phase 2 Step 2.

#### Step 2: `nx memory curate --files`, `--t2` and `--agents`

The report sections that need no transcripts first, then the `--files`
read counts once RDR-240 Phase 1 Step 1 lands.

#### Step 3: `nx memory curate --cold`

After RDR-240 Phase 1 Step 1; the tuple-subspace row after RDR-239's
dictionary view.

#### Step 4: Release step

A curation step after the release skill's Step 12 (the local tool reinstall),
so the reinstalled `nx` carries the verb, that runs `nx memory curate` and
never blocks the release.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Replaced T2 rows | none; the row is gone, its name is on the new row | `nx memory get` of the new row | the replacing write | `nx memory curate --t2` | the deleted content `memory_put` returns to the writer, kept only if the writer keeps it |
| Memory files | `ls` the memory directory | the file | by the writer, with `replaces` | SessionStart check, `nx memory curate --files` | Time Machine |

### New Dependencies

None directly. `nx memory curate` uses RDR-240's runtime, which brings the
interpreter RDR-240's runtime RDR chooses.

## Test Plan

- Engine: replace in one transaction (old row gone, new row stored as
  sent, echo carries the old content); refusal (missing target, self,
  quarantined, and one case per `AUDIT_TAGS` tag, including a target tagged
  `kind:gate,bead:nexus-x` and one whose body mentions `kind:gate` but whose
  tags do not, which is not refused) leaves both rows unchanged; a numeric title `238` is read as a title; a title that
  exists only in another project is "missing"; a chain A, B, C leaves only C
  ending `replaces: B`.
- Client: after a replacing put, search and `memory_get` no longer find the
  old row, and `_verify_t2_write_landed` passes; against an engine stub that
  returns no echo, the call fails; the `memory_put` result carries the
  deleted content.
- Hook: over budget, an unindexed file, a replaced file still present and an
  index entry naming a missing file each print one line; clean prints
  nothing; a repo without `MEMORY-ondemand.md` prints nothing; an unreadable
  directory is silent and exits 0; each case fails with its check deleted.
- Curate: each report section (`--files`, `--t2`, `--agents`, and each
  `--cold` kind) on a fixture directory and a fixture transcript directory,
  with one planted case and one clean case; without the runtime, the
  transcript rows say "not available" and the command exits 0.

## Validation

### Testing Strategy

Engine and client tests on real PG; hook tests on fixture directories; Phase 2
compliance counts from RDR-240's `violations` query, stored in T2.

### Performance Expectations

A replacing put adds one indexed lookup and one delete to the put. The
SessionStart check reads one directory of about 150 small files, well under
its hook budget.

## Finalization Gate

### Contradiction Check

The three gaps, the design and the plan agree: a replacing write deletes
in one transaction, a budget bounds the index, and a hook plus a report run
the checks. This revision fixes one contradiction. The Approach said the
SessionStart hook compares a new file's text to detect a reversal, but
Technical Design 3 only checks that a replaced file is gone. A hook cannot
tell that one rule reverses another, so the Approach now states what the
hook checks. Two Key Discoveries (the loss check on promotion, skills
beside their sources) concern promotion, which Sam's review moved to
RDR-240. They stay as findings and drive no design item here. The round-1
gate's fix made RDR-240 a prerequisite of Phase 2 and the transcript-backed
report rows; the round-2 fix settled `replaces` as a title-only, same-project
target with an echo, excluded audit records, and gave the hook, the report
and Phase 2 their bounds.
Decision Rationale and the RDR-074 row agree that deletion overrides
permanence, as Sam ruled.

### Assumption Verification

A1 is verified (research 1 and 2): the index loads up to 200 lines or 25 KB
and topic files load on demand; subagents also get it at Claude Code
2.1.316, against the docs, so the budget rests on main sessions alone. A2 is
assumed: research 3 counts title chains, not whether a replaced row's
content is needed; audit records are excluded as targets, and Sam's read of
20 earlier chain members in Phase 1 Step 3 tests the rest before the feature
is announced. A3 is open on purpose:
no paper measures it, so Phase 2 measures compliance before and after the
split with one `violations` query (research 4 only sized the windows), and a
rule whose rate rises past its bound goes back.

#### API Verification

- `memory_put` (MCP, `src/nexus/mcp/core.py`) and the engine's
  `/v1/memory` put take no `replaces` today; the field is new and additive.
- An explicit delete exists: MCP `memory_delete(project, title)`, engine
  `DELETE /v1/memory/delete` (`MemoryHandler`), `MemoryRepository.delete`
  and `deleteById`. `memory_delete` calls `delete(tenant, project, title)`
  (`MemoryHandler.java:449`); neither it nor `deleteById` checks
  quarantine, so Phase 1 Step 1 adds a method instead of reusing them.
- `MemoryHandler` sets `FAIL_ON_UNKNOWN_PROPERTIES` to false
  (`MemoryHandler.java:107`), so an engine without the feature ignores
  `replaces`; the echo is what lets a new client notice.
- A delete-and-write in one transaction already exists as a pattern:
  `MemoryRepository.mergeMemories(tenant, keepId, deleteIds, content)`, which
  refuses a quarantined id.
- The latest memory changeset is `memory-005-live-project-timestamp-index.xml`,
  and the design needs no new one.

### Scope Verification

Gap 1 maps to Phase 1 (engine and client) and the frontmatter field, Gap 2
to Phase 2 (measure, split, re-measure), and Gap 3 to Phase 3 (Step 1 the
SessionStart check, Step 2 `--files`, `--t2` and `--agents`, Step 3
`--cold`, Step 4 the release step). `--agents` and `--cold` answer Gap 3's
second paragraph, the drift beyond memory that Sam asked the report to
watch. Phase 2 Steps 1 and 3 and the transcript-backed report rows need
RDR-240's `transcripts` procedure, a prerequisite, and the `--cold`
tuple-subspace row needs RDR-239's dictionary view, so this RDR builds no transcript scanner. Procedures
and promotion are out of scope (RDR-240), and so is the gate-record
rollup (RDR-209 Amendment A1). Retiring a cold store is out of scope too:
the report lists it, and any deletion of data rides its own change.

### Cross-Cutting Concerns

- **Wire contract.** `replaces` is an additive field on the memory put, with
  a wire-contract ledger entry for both directions: an old client never
  sends it, and a new client against an old engine fails on the missing
  echo.
- **Tenancy.** The target is looked up only in the writer's tenant and
  project, so a replace cannot cross either; the delete runs under the same
  RLS as the put.
- **Irreversibility.** A replaced row is gone. The guards are the
  exact-title, same-project rule, the audit-record exclusion, the lineage
  line on the new row, and the echo of the deleted content. Point-in-time
  recovery is cloud-only and is not relied on.
- **Hook cost.** The SessionStart check is stdlib only, opt-in per repo,
  reads one small directory, prints at most one line and never blocks a
  session.
- **Measurement.** Phase 2 counts compliance before and after the split
  with the same `violations` query, so the two are comparable.

### Proportionality

One optional field on an existing write and one repository method, one
frontmatter field, one hook verb, one report verb with four sections, and a
one-time split of one index file. No schema change. Gaps 1 and 2 and the
`--files` and `--t2` sections match problems measured in production: stale
rows that retrieval serves, an index that grows one line per memory, and
checks nobody runs. `--agents` and `--cold` are larger than those gaps and
lean on two other RDRs (RDR-239 and RDR-240); they are kept because Sam
asked for the cold-state watch, and they land last (Phase 3 Steps 2 and 3)
so the rest does not wait for them.

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
| 2026-10-31 | Pre-gate revision: Finalization Gate written; the Approach states what the SessionStart hook checks, matching Technical Design 3; A1 prerequisite ticked. |
| 2026-11-01 | Gate round 2: PASSED (0 Critical, 17 Significant, 0 ship-blockers); commit `0e6eab59c`; critique `nexus_rdr/238-gate-critique-2026-11-01-r2`. |
| 2026-11-01 | Gate round 3: PASSED (0 Critical, 10 Significant, 0 ship-blockers); commit `3f66127f0`; critique `nexus_rdr/238-gate-critique-2026-11-01-r3`. |
| 2026-11-01 | Accept residual dispositions (gate round 3): K1, K2, K3, K4, K5 -> nexus-qo7ie.3; S-8, S-9 -> nexus-qo7ie.4; S-7, K6, S-10 -> nexus-qo7ie.5. |
| 2026-11-02 | Sam reversed the 2026-10-31 "supersession becomes deletion" for Phase 1: replace quarantines. A2 was measured (first 20 replacement-type rows: 6 yes, 11 maybe, 3 no by an agent read), which trips Step 3's stop rule; Sam chose quarantine instead of deletion on the A2 read; Alternative 1 was not taken. A replaced row is quarantined with `ttl_days` NULL: the rollup skips it, the engine refuses it as a summary source, and `reap` never deletes it. A later plain put at the old title overwrites the quarantined row in place, and the `replaced_id` the writer was given then names that new live row. Key Discoveries 2026-11-02 rules for Phase 1; decision sections not rewritten (nexus-ov3zi). |
