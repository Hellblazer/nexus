---
title: "Memory Curation: Supersession, a Bounded Always-Loaded Index, and Procedures That Run Instead of Being Recalled"
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

# RDR-238: Memory Curation: Supersession, a Bounded Always-Loaded Index, and Procedures That Run Instead of Being Recalled

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
section (Technical Design 6).

#### Gap 5: Procedures are recalled or improvised instead of run

Gap 3 moves a procedure out of memory into a skill, but a skill is still text
a model reads and then carries out from its own reading. Many of the
procedures in memory have one right answer, and for those the safe form is a
command that does the work. Evidence from one session (2026-10-31, the session
that wrote this RDR):

- Six transcript scans were written by hand as throwaway heredocs (subagent
  model mix and cost, plugin and tool use, memory-file reads, a rule-violation
  baseline, the auto-memory census by Claude Code version, gate-record title
  mapping). This RDR's Phase 2 and its curation report need the same scans
  again. `scripts/skill_routing_census.py` (nexus-dg7i6.1) is one such
  instrument already, kept as a script for one question.
- `nx rdr preamble rdr-research -- add` takes the finding as bare shell
  arguments. A finding with semicolons was split by the shell into commands,
  one of which ran as `git stash 8 sessions...` (git refused it), and three
  records were truncated and had to be rewritten. A memory file names this
  exact hazard (`feedback_rdr_research_add_text_is_shell_parsed.md`); the rule
  was in memory and did not prevent it.
- Gate-record tags (nexus-qo7ie.1) are free text inside a `memory_put` call,
  so an agent can misspell or omit them, and the RDR-209 rollup inherits
  whatever was typed.
- The largest memory file, `feedback_test_runs_before_push.md` (32 KB), is a
  decision procedure for which tests to run before a push. Two selectors
  already compute part of it (`scripts/select_affected_integration_tests.py`,
  `scripts/select_docs_reader_tests.py`); the rest is recalled.

The harness survey points the same way: in its ablation, tools (+3.3 points)
and middleware (+2.2) carried the gain, and the system prompt alone made
results worse (Harness Engineering, p.66). The fix delivers a rule for when a
procedure becomes a command, a rule for which front door a command gets, and
the four commands this project needs first.

The front door matters because nexus already has two. Over the 30 days to
2026-10-31, sessions made about 21,000 nexus MCP tool calls (`memory_get`
9,394, `memory_put` 5,582, `memory_search` 2,108, `mailbox_send` 1,680) and
about 1,200 `nx` CLI calls (`nx rdr preamble` 333, `nx memory get` 132,
`nx memory list` 96, then the catalog verbs). Agents use MCP; the CLI is
mostly the RDR preamble and hand reads. Neither door is chosen by rule
today.

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

**Promotion, by what the memory holds (Gaps 3 and 5).** A memory file is a
promotion candidate when its "How to apply" is a procedure (ordered steps, or
more than about 8 KB) or a bright line. Its target depends on what it holds:

| The memory holds | Target | Why | Example |
| --- | --- | --- | --- |
| A procedure that needs judgment | Skill | Loads on demand; the model applies it | release checklist, review gate |
| A fixed sequence with a right answer | Command (an `nx` verb or a `scripts/` tool) | Runs the same way every time; nothing to misremember | what to run before a push |
| A bright line that must always hold | Hook | Claude Code's docs: memory is context, a hook is enforcement | no `git stash`, no full-disk `find` |

A nexus-repo procedure becomes a repo-local skill or command; a practice every
conexus user should follow ships in the plugin; Sam's personal rules stay as
memory. The target cites its source files, and the source shrinks to the rule
and its why, plus a pointer to the skill, command or hook.

**The first four commands (Gap 5).** In order of payoff:

1. `nx transcripts`: named, read-only queries over Claude Code session
   transcripts. It absorbs `scripts/skill_routing_census.py` and the six scans
   above, and is the measurement layer for Phase 2, the curation report and
   RDR-209's heat work until its read counter lands.
2. `nx gate record`: writes a gate record from structured fields and derives
   its title and tags, so no agent types a tag.
3. `nx preflight`: reads the diff, names its change class, and runs the right
   checks, composing the existing selectors.
4. Text from stdin or a file for every verb that stores prose (`nx rdr
   preamble rdr-research -- add`, `nx memory put`, and the bead text the
   skills write through `bd`), so prose never passes through shell parsing.

**One implementation, two front doors (Gap 5).** Every capability is one
Python function. The MCP tool and the CLI verb are thin wrappers over it, and
which wrappers exist follows a rule:

| Capability | Front door | Why |
| --- | --- | --- |
| Hot, agent-called, structured input (memory, search, store, scratch, mailbox, tuples) | MCP, with a CLI twin | Agents call it thousands of times; it must work where there is no shell |
| Agent-written prose (gate records, research findings) | MCP first, CLI twin with stdin | MCP arguments are structured JSON, so no shell parsing |
| Human, CI and admin work (doctor, upgrade, index, preflight, transcripts) | CLI only | It runs in a shell anyway; an MCP schema would be dead weight |
| An MCP tool nobody calls | Dropped from MCP | Even deferred, it costs its name in every context and its upkeep |

The reasons behind the rule, as measured or documented:

- **Reach.** A verb needs a shell and an installed `nx`. Claude Desktop chat,
  claude.ai on the web, Cowork and mobile have no shell, so MCP is the only
  door there.
- **Input safety.** Gap 5's shell split happened on the CLI path; the same
  text through `memory_put` arrives byte for byte.
- **Enforcement.** A hook matches an MCP tool by its exact name; on Bash it
  parses command strings, which is where false refusals come from (the
  subagent git guard denied read-only `git merge-base` lines in four of five
  Haiku runs on 2026-10-31, and the primary-checkout guard refused two
  worktree commits from this session because a compound command began with
  `cd`).
- **Context cost.** Claude Code defers most MCP tools to a name, so the cost
  gap is the always-loaded set (8 nexus tools, 4.4K tokens) and about 0.4K of
  deferred names, not the full catalogue.

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
   `--cold [--days N]`: every store of state with no write and no read in N
   days (default 30), wherever it lives. Each row gives the store, its last
   write, its last read, its size and its owner if known:
   - tuple subspaces, from the census's last post time and from the
     dictionary's `writers` (the tuple-space dictionary draft, nexus-mdbds); a template whose `writers` is empty
     and that has no live subspace is listed by name;
   - T2 namespaces and memory files, from read and write counts;
   - MCP tools and CLI verbs, from `nx transcripts tools` (the front-door
     census, Phase 5 Step 4);
   - context injected per session and per dispatch, from `nx transcripts
     loads`, so a large always-loaded item with no use shows as tokens.
   Report only: retiring a cold store is a person's decision, and one that
   deletes data rides its own change (as nexus-onjvw does for the ledger).
   The report runs at each release's post-release step (Phase 4), so waste
   shows while it is small.
7. **Promotion recipe.** A section in the `writing-nx-skills` skill: the
   criterion, the target choice, the citation of sources, and a loss check in
   which `substantive-critic` compares the skill with its source files and
   lists every rule the skill lacks. A promotion lands only with an empty
   list. The same loss check applies when the target is a command or a hook.
8. **`nx transcripts <query> [--since D] [--project P] [--json]`.** Reads
   `~/.claude/projects/*/` session and subagent transcripts read-only; never
   writes there and never prints message bodies by default (counts, names,
   ids, sizes), since transcripts hold everything a session saw. Queries,
   each a function with a fixture test: `agents` (dispatches by agent type
   and model, starting context, turns, errors, estimated cost); `tools` (calls
   per tool and MCP server, deferred versus called); `skills` (the
   skill-routing census, ported); `memory` (memory-file reads and writes, and
   what loaded into a subagent's instructions); `violations` (named rule
   detectors, the Phase 2 baseline); `loads` (instruction files and their
   types per dispatch, the auto-memory census). Cost uses one price table in
   code, dated, and says it is an estimate.
9. **`nx gate record --kind review|critique|confirm|fixround --bead B [--rdr N]
   --sha S --outcome justified|partial|not-justified (--body-file F | -)`.**
   Writes T2 project `<repo>` with the title
   `<kind>-<sha>-<slug>-<date>`, tags `kind:gate,bead:B[,rdr:N]`, and no TTL,
   through the same MCP and CLI paths, and refuses an outcome outside the
   verdict vocabulary the review agents already mandate. Under the
   front-door rule it is agent-written prose, so its first door is an MCP
   tool, `gate_record`, with the same fields; the CLI verb is the twin. The
   agents' T2 write-back line then names `gate_record` instead of a
   free-text tag string, and `tests/test_gate_record_tags_lint.py` moves to
   pin its use.
10. **`nx preflight [--base REF] [--run]`.** Classifies the diff (Python, engine
    `service/`, plugin surface, docs, workflow YAML, schema changeset) and
    prints the checks each class needs: ruff on touched files, the change
    class's tripwire tests, the affected-integration and docs-reader selectors,
    the drift ledger when the plugin surface changed, the gate-jar staleness
    check when `service/` changed, actionlint for workflows. `--run` runs the
    light ones locally and prints the `lab-remote` line for heavy ones, never
    running a heavy one on the laptop. The classes and their checks are a
    checked table in `src/nexus/tables/`, so a new class is a row, not prose.
11. **Prose on stdin.** `--body-file PATH` and `-` (stdin) on `nx rdr preamble
    rdr-research -- add`, `nx memory put` and `nx gate record`; the skills that
    record findings or write bead text pass prose that way, and a lint flags a
    skill example that puts prose in a bare argument.

### Existing Infrastructure Audit

| Need | Exists | Plan |
| --- | --- | --- |
| Retire a T2 row | RDR-207 quarantine and rollup | Reuse by setting a TTL on supersede |
| Schema change | Liquibase memory changesets 001-005 | Add 006 |
| SessionStart hook verbs | conexus `nx-hook` dispatch | One more verb |
| Transcript read counts | Ad hoc scan (survey, 2026-10-31) | Make it `nx memory curate --files` |
| Skill authoring rules | `writing-nx-skills` skill | Add the promotion section |
| Transcript instruments | `scripts/skill_routing_census.py`; six ad hoc scans | Fold into `nx transcripts` |
| Test selection | `scripts/select_affected_integration_tests.py`, `scripts/select_docs_reader_tests.py` | Compose in `nx preflight` |
| Verdict vocabulary | `tests/test_review_agent_verdict_vocabulary_lint.py` | `nx gate record` enforces it at write |
| Closed vocabularies | `src/nexus/tables/` | Preflight's change classes become a table |

### Decision Rationale

Supersession is filtered at the read, because the paper's failure is at
retrieval: the reader cannot tell the obsolete fact from the current one once
both are in context. Retirement reuses RDR-207 so there is one deletion path.
The index is bounded by a budget, not curated by taste, because a budget
checked by a hook survives a busy week and taste does not. Promotion keeps
its sources because the one paper that tests it found sources and skills
together beat skills alone. A fixed sequence becomes a command because a
procedure that is read and then carried out can be misread, and today's
evidence (Gap 5) is that it was, with the rule against it sitting in memory.

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

### Alternative 3: Keep procedures as skills only

Promote every procedure to a skill and build no commands.

**Pros:** no code; skills are already on-demand.

**Cons:** a skill is still read and then carried out; the shell-parsing
failure in Gap 5 happened with the rule in memory, and a skill would have been
the same text one level up.

**Reason for rejection:** a procedure with one right answer is cheaper to run
than to reread. Skills stay the target for procedures that need judgment.

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

### Phase 5: Commands (Gap 5)

Can start before Phases 1 to 4; Phase 2's measurement uses step 1.

#### Step 1: `nx transcripts`

Port the skill-routing census and the six scans as queries, each with a
fixture transcript test. Done when this RDR's research 2 to 5 can be
re-derived from it.

#### Step 2: Prose on stdin

`--body-file` and `-` on the three verbs; update the skills' examples; add
the lint.

#### Step 3: `nx gate record`

The verb, the agents' write-back lines naming it, and the lint moved to pin
it.

#### Step 4: Front-door census

With `nx transcripts tools`, list every nexus MCP tool and CLI verb with its
30-day call count and caller (main session, subagent, human). Apply the
front-door table: each MCP tool nobody calls is a drop candidate, each hot
capability without a twin gets one. The result is a list for Sam to approve,
never an automatic removal, and dropping an MCP tool is a wire change with a
ledger entry.

#### Step 5: `nx preflight`

The change-class table, the classifier, and the composed selectors. Done
when its output for the last ten develop pushes matches what was actually
run or should have been.

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
- Transcripts: each query against a fixture transcript with known counts; a
  test that no query prints a message body unless asked.
- Gate record: title and tags derived exactly; an outcome outside the
  vocabulary refused with nothing written.
- Preflight: each change class against a fixture diff; a heavy check is never
  run locally.
- Stdin: a finding containing `;`, backticks and `$(...)` stored byte for
  byte.
- Front doors: a lint that every MCP tool and CLI verb wrapping one
  capability call the same function, so the twins cannot drift.

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
| 2026-10-31 | Gap 5 added at Sam's direction: procedures that run instead of being recalled. Promotion gains three targets (skill, command, hook), and Phase 5 builds `nx transcripts`, prose on stdin, `nx gate record` and `nx preflight`. Title widened to match. |
| 2026-10-31 | Front-door rule added to Gap 5 at Sam's direction: one implementation, MCP and CLI as thin wrappers, chosen by caller and reach; `nx gate record` becomes the MCP tool `gate_record` first; Phase 5 Step 4 is a front-door census. |
| 2026-10-31 | Cold-state section added at Sam's direction (heat and waste): Gap 4 names the day's five cases, and `nx memory curate --cold` reports every store with no write and no read in N days, report only. |
