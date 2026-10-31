---
title: "Memory Curation: Supersession, a Bounded Always-Loaded Index, and Promotion of Procedures to Skills"
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

# RDR-238: Memory Curation: Supersession, a Bounded Always-Loaded Index, and Promotion of Procedures to Skills

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** From the 2026-10-31 memory survey (artifact
`https://claude.ai/artifact/9gEbyn1wFYhWh9sTJvoT8a`; T2
`nexus/memory-survey-papers-agent-memory-2026-10-31` and
`nexus/memory-survey-papers-context-load-2026-10-31`). Sam approved the
direction the same day: one RDR for supersession, the index and promotion to
skills; gate records go to RDR-209 as an amendment.

## Problem Statement

Nexus keeps memory in four places: T1 session scratch, T2 notes in the engine,
T3 permanent knowledge, and Claude Code's file memory
(`~/.claude/projects/<repo>/memory/`, one rule per file, indexed by
`MEMORY.md`). We have machinery for writing memory and, since RDR-207, for
expiring it. We have none for curating it: saying that one entry replaces
another, deciding what is worth loading into every context, or turning a rule
that has grown into a procedure into the thing that holds procedures, a skill.
Sam's phrase for the job is garbage collection. The collector has no roots,
no marks and no schedule.

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
says what replaced it. The fix delivers a supersession mark on T2 rows and on
memory files, and reads that skip superseded entries by default.

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

#### Gap 3: Procedures are stored as memory

The largest memory files are procedures, not rules:
`feedback_test_runs_before_push.md` (32 KB), `feedback_release_and_develop_ci.md`
(27 KB), `feedback_hand_kept_enumerations.md` (22 KB),
`feedback_destructive_action_guards.md` (14 KB). They are read when someone
remembers to read them, and each index line has to summarise a whole
procedure. Skills are where this project keeps procedures: they load on
demand from a one-line listing. "Lifelong Context Adaptation through
Bidirectional Memory–Skill Learning" is the one paper that tests promotion:
memory and skills together reached 63.2% on ALFWorld against 50.5% for skills
alone, so the source episodes stay as evidence, and admission is gated (a
skill passes 10 of 13 audit checks). No nexus path promotes a memory to a
skill. The fix delivers a promotion criterion, a target for each kind of
procedure, and a loss check before the source shrinks.

#### Gap 4: Curation depends on the writer remembering to do it

Every rule above is a habit today, and habits drift: the index grew one line
per memory, files were split from a bundle and never indexed (a few dozen
files are reachable only through another file), and no one measures any of
it. Sam: "we need to somehow get that ensconced in our habits." The fix
delivers checks that run without anyone remembering them, and a measurement
that says whether the curation helped.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-057 Progressive Formalization Across Memory Tiers | Origin of the tier model | Its "graduation" idea is the T2 to T3 case. This RDR is the curation case beside it: replacement, admission to context, and promotion out of memory into skills. |
| RDR-207 Summarization at the T2 Expiry Boundary | Reused | It owns what expiry may destroy (quarantine, rollup, reap only rolled-up rows). A superseded T2 row is handed to that machinery by giving it a TTL; this RDR adds no deletion path of its own. |
| RDR-209 Heat-Based Graduation Between Memory Tiers | Sibling, draft | It owns heat and graduation, and takes the gate-record rollup as an amendment (same date). Its Phase 0b, a read counter that means "a caller read this row", is the signal this RDR's index audit would use for T2; until it lands, the audit counts reads from transcripts. |
| RDR-074 Permanence Mode | Deferred, compatible | A permanent row can still be superseded. Supersession is a statement about currency, not lifetime. |
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
- Skills for this repo live in two places: conexus plugin skills
  (`conexus/skills/`, shipped to every conexus user) and repo-local skills
  (`.claude/skills/`: `release`, `engine-release`, `site-page`,
  `test-authoring`).

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
- **A2:** A replacement link (one column) is enough for T2. Superseded rows
  are a small share and nobody needs "what was true on date D" queries.
  Verification: a census of rows a reader would mark superseded. If
  as-of-date queries turn out to matter, the bitemporal alternative comes
  back.
- **A3:** Rules moved to the on-demand tier keep their compliance.
  Verification: Phase 2's before-and-after sample.
- **A4:** The largest memory files can be turned into skills without losing
  a rule. Verification: Phase 3's loss check on each promotion.

## Proposed Solution

### Approach

Mark, then bound, then promote, then make the three run without anyone
remembering them.

**Supersession (Gap 1).** A T2 write may name the row it replaces. The
engine records the link on the old row in the same transaction, and reads
skip superseded rows unless asked. A superseded row gets a TTL, so RDR-207's
quarantine and rollup retire it on the existing path. Memory files get the
same idea in frontmatter: the new file names the files it replaces, and the
old file is deleted or reduced to a pointer.

**A bounded index (Gap 2).** `MEMORY.md` keeps an **always** tier under a
token budget (proposed: 1.5K tokens, about 40 lines). A rule enters it only if
it binds on most turns of most sessions and is not already stated in a
`CLAUDE.md`, a hot rule or a skill. Every other memory moves to an
**on-demand** index file in the same directory, which `MEMORY.md` names in one
line so a session knows where to look.

**Promotion to skills (Gap 3).** A memory file is a promotion candidate when
its "How to apply" is a procedure (ordered steps, or more than about 8 KB). A
nexus-repo procedure becomes a repo-local skill under `.claude/skills/`; a
practice every conexus user should follow becomes a conexus plugin skill;
Sam's personal rules stay as memory. The skill cites its source files, and
the source shrinks to the rule and its why, plus a pointer to the skill.

**Habits that run themselves (Gap 4).** Three checks: the index budget is
checked at SessionStart and over budget prints one line; a `supersedes` field
is required when a memory file reverses another (checked by the same hook,
which compares a new file's text against what it says it replaces); and a
curation report runs at each release's post-release step, listing unread
files, unindexed files, oversized files and superseded rows still served.

### Technical Design

1. **T2 schema.** Changeset `memory-006-supersession.xml`: nullable
   `superseded_by BIGINT` referencing `nexus.memory(id)` within the same
   tenant and project, `superseded_at TIMESTAMPTZ`, and a partial index on
   live rows. All DDL through Liquibase.
2. **T2 write.** `memory_put` and `/v1/memory` gain an optional `supersedes`
   (an id or an exact title in the same project). In one transaction, the
   engine writes the new row and stamps the old one, and sets the old row's
   TTL to 30 days if it had none. A missing or cross-project target is
   refused, and the new row is not written. Additive on the wire; recorded in
   the wire-contract ledger.
3. **T2 read.** Search and list skip superseded rows by default and take
   `include_superseded`. `memory_get` on a superseded row returns it with a
   first line naming its replacement, so an old link still resolves and says
   where to go.
4. **File-memory frontmatter.** An optional `supersedes: [name, ...]`. The
   index check (5) reads it.
5. **SessionStart curation check.** A conexus hook verb (stdlib only, under
   its existing SessionStart budget) that reads the repo's memory directory
   and prints at most one line: over budget, an unindexed file, or a file
   named in another file's `supersedes` that still exists. Silent when clean.
   Claude Code already reminds a writer near its own 200-line and 25 KB
   limits (research 1); this check is the tighter budget we choose, plus the
   two checks Claude Code does not make.
6. **Curation report.** `nx memory curate [--files] [--agents] [--t2]`. `--agents`
   covers the reviewers' own memory directories under
   `~/.claude/agent-memory/` (nexus-qo7ie.1 gave both reviewers `memory: user`):
   index length against the 40-line cap their definitions state, entries with
   no repo named, and concurrent-write damage, since two sessions running the
   same reviewer edit one `MEMORY.md` and the last writer wins. `--files`: unread files
   over N days (read counts from transcripts until RDR-209 Phase 0b gives T2
   a true read counter), unindexed files, files over the promotion size, and
   live T2 rows that a later row names in its title or body as replaced
   (a heuristic list for a person to confirm, never an automatic mark).
7. **Promotion recipe.** A section in the `writing-nx-skills` skill: the
   criterion, the target choice, the citation of sources, and a loss check in
   which `substantive-critic` compares the skill with its source files and
   lists every rule the skill lacks. A promotion lands only with an empty
   list.

### Existing Infrastructure Audit

| Need | Exists | Plan |
| --- | --- | --- |
| Retire a T2 row | RDR-207 quarantine and rollup | Reuse by setting a TTL on supersede |
| Schema change | Liquibase memory changesets 001-005 | Add 006 |
| SessionStart hook verbs | conexus `nx-hook` dispatch | One more verb |
| Transcript read counts | Ad hoc scan (survey, 2026-10-31) | Make it `nx memory curate --files` |
| Skill authoring rules | `writing-nx-skills` skill | Add the promotion section |

### Decision Rationale

Supersession is filtered at the read, because the paper's failure is at
retrieval: the reader cannot tell the obsolete fact from the current one once
both are in context. Retirement reuses RDR-207 so there is one deletion path.
The index is bounded by a budget, not curated by taste, because a budget
checked by a hook survives a busy week and taste does not. Promotion keeps
its sources because the one paper that tests it found sources and skills
together beat skills alone.

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

- **Delete on supersede:** loses the lineage the papers keep, and the
  history the fix-round reads use.
- **Move every procedure into conexus plugin skills:** ships Sam's personal
  and nexus-only rules to every conexus user.

## Trade-offs

### Consequences

- One more engine changeset and an additive wire field.
- A session sees fewer rules up front and has to look in the on-demand index
  for the rest.
- Promotion adds skills to the listing (about 30 to 100 tokens each), against
  the index lines it removes.

### Risks and Mitigations

- **A rule drops out of the always tier and stops being followed.**
  Mitigation: Phase 2 measures compliance before and after, and the rule goes
  back if its violations rise.
- **The heuristic report marks a live row as replaced.** Mitigation: the
  report only lists; a person or the writer sets the mark.
- **The SessionStart line becomes noise.** Mitigation: at most one line,
  silent when clean, and the budget is set from the Phase 2 measurement.

### Failure Modes

- Supersede target missing: refused, nothing written, error names the
  target.
- Hook cannot read the memory directory: silent, logged to `hook.log`;
  never blocks a session.

## Implementation Plan

### Prerequisites

- [ ] A1 verified (Claude Code's loading of `MEMORY.md`).
- [ ] RDR-209 amendment for gate records filed (this RDR does not depend on
      it landing).

### Minimum Viable Validation

On a test substrate: write row A, then row B with `supersedes=A`; search
returns B and not A; `memory_get(A)` names B on its first line; A carries a
30-day TTL; a `supersedes` naming a row in another project is refused and B is
not written.

### Phase 1: T2 supersession

#### Step 1: Changeset and engine write path

`memory-006-supersession.xml`, the stamp in the put transaction, the refusal
cases, Java tests against PG.

#### Step 2: Client and MCP

`memory_put(supersedes=...)`, `include_superseded` on search and list, the
`memory_get` first line, wire-ledger entry, Python tests against the engine.

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

### Phase 3: Promotion

Promote the four largest procedural files, one at a time, each with its loss
check, starting with `feedback_test_runs_before_push.md`.

### Phase 4: Habits

The SessionStart check, `nx memory curate`, and a curation step in the
release skill's post-release section.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Superseded T2 rows | `nx memory list --include-superseded` | `nx memory get` | RDR-207 expiry | `nx memory curate --t2` | engine PITR |
| Memory files | `ls` the memory directory | the file | by hand, after `supersedes` | SessionStart check, `nx memory curate --files` | Time Machine |

### New Dependencies

None.

## Test Plan

- Engine: supersede in one transaction; refusal leaves both rows unchanged;
  partial index used by the live-row search (EXPLAIN).
- Client: default search hides superseded rows; `include_superseded` shows
  them; `memory_get` first line.
- Hook: over budget prints one line; clean prints nothing; an unreadable
  directory is silent and exits 0; each case fails with its check deleted.
- Curate: each report section on a fixture directory with one planted case
  and one clean case.

## Validation

### Testing Strategy

Engine and client tests on real PG; hook tests on fixture directories; Phase 2
compliance counts from transcripts, stored in T2 with the scan script.

### Performance Expectations

The supersede stamp adds one indexed update to a put. The SessionStart check
reads one directory of about 150 small files, well under its hook budget.

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
